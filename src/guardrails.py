"""Guardrails: shared PII/secret scrubber (ingest + egress, DEP-02) and the four synchronous blocking VALIDATE checks."""
import logging
import re
import threading
from collections import Counter
from functools import cached_property
from typing import Any, Callable

import numpy as np

from src.config import COMMITMENT_RX, DISCLAIMER, GUARDRAIL_CHECKS, PROHIBITED_PHRASES, RoutingThresholds, terms

log = logging.getLogger(__name__)
logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)

# Input:  none – fixed policy (spec NFR-04 / DEP-02 secret regexes)
# What:   Deterministic secret detectors that run before Presidio, each with its typed placeholder
# Output: list[tuple[str, re.Pattern, str]] – (entity type, pattern, placeholder)
_PEM = r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
SECRETS = [
    ("PEM_PRIVATE_KEY", re.compile(_PEM + r"(?:.*?" + _PEM.replace("BEGIN", "END") + r")?", re.S), "<REDACTED_PRIVATE_KEY>"),
    ("CLOUDSERVE_API_KEY", re.compile(r"\bcs_(?:live|test)_[A-Za-z0-9]{16,64}\b"), "<REDACTED_API_KEY>"),
    ("AWS_SECRET_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|(?i:aws\w{0,20}[\s_-]?(?:secret|key)\w*\s*[:=]\s*)[A-Za-z0-9/+=]{40}\b"), "<REDACTED_API_KEY>"),
    ("BEARER_TOKEN", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*", re.I), "<REDACTED_BEARER_TOKEN>"),
]
# Input:  none
# What:   Presidio entity -> (spec entity name, placeholder)
# Output: dict
PII = {"EMAIL_ADDRESS": ("EMAIL_ADDRESS", "<REDACTED_EMAIL>"), "IP_ADDRESS": ("IPV4_ADDRESS", "<REDACTED_IP_ADDRESS>"),
       "CREDIT_CARD": ("CREDIT_CARD", "<REDACTED_CREDIT_CARD>"), "PHONE_NUMBER": ("PHONE_NUMBER", "<REDACTED_PHONE_NUMBER>")}


class Scrubber:
    # Input:  none (Presidio + spaCy small model load lazily on first use)
    # What:   Finds and replaces API keys, tokens, private keys, emails, IPs, cards and phone numbers with typed placeholders
    # Output: Scrubber – one shared instance per process (SCRUBBER)
    def __init__(self):
        self._lock = threading.Lock()

    @cached_property
    def _engines(self) -> tuple[Any, Any, dict]:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_anonymizer import AnonymizerEngine
        from presidio_anonymizer.entities import OperatorConfig
        nlp = NlpEngineProvider(nlp_configuration={"nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}]}).create_engine()
        ops = {k: OperatorConfig("replace", {"new_value": ph}) for k, (_, ph) in PII.items()}
        return AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"]), AnonymizerEngine(), ops

    def scrub(self, text: str) -> tuple[str, Counter]:
        # Input:  text (str) – any customer or outbound text
        # What:   Regex pass for secrets, then Presidio pass for personal data; counts every detection by entity type
        # Output: tuple[str, Counter] – redacted text and detections per entity type
        counts: Counter = Counter()
        for ent, rx, ph in SECRETS:
            text, n = rx.subn(ph, text)
            counts[ent] += n
        if text.strip():
            with self._lock:
                analyzer, anonymizer, ops = self._engines
                hits = analyzer.analyze(text, language="en", entities=list(PII), score_threshold=0.35)
                if hits:
                    counts.update(PII[h.entity_type][0] for h in hits)
                    text = anonymizer.anonymize(text, hits, operators=ops).text
        return text, +counts


SCRUBBER = Scrubber()


class NLIScorer:
    # Input:  model_name (str) – local cross-encoder NLI model (cross-encoder/nli-deberta-v3-small)
    # What:   Scores how strongly a documentation premise entails or contradicts a draft sentence
    # Output: callable(pairs) -> np.ndarray[n, 2] of [entailment, contradiction] probabilities
    def __init__(self, model_name: str):
        from sentence_transformers import CrossEncoder
        self.model, self._lock = CrossEncoder(model_name, device="cpu"), threading.Lock()
        labels = {v.lower(): int(k) for k, v in self.model.model.config.id2label.items()}
        self.cols = [labels["entailment"], labels["contradiction"]]

    def __call__(self, pairs: list[tuple[str, str]]) -> np.ndarray:
        with self._lock:
            probs = self.model.predict(pairs, apply_softmax=True, batch_size=32, show_progress_bar=False)
        return np.asarray(probs)[:, self.cols]


# Input:  none
# What:   Sentences that are courtesy rather than factual claims (greetings, sign-offs, invitations to reply)
# Output: re.Pattern
_COURTESY = re.compile(r"(?i)^(?:thanks?\b|thank you|sorry|apolog|we apologi[sz]e|i hope|hope (?:this|that) helps|happy to help|hi\b|hello\b|dear\b|"
                       r"best regards|kind regards|feel free)|\b(?:let us know|reach out to (?:us|support)|contact (?:us|support)|reply to this)\b")


# Input:  body (str) – the LLM draft without disclaimer or footer
# What:   Splits the draft into sentences/steps and keeps only factual claims (drops greetings, sign-offs and questions)
# Output: list[str] – claims that must be supported by the cited documentation
def claim_sentences(body: str) -> list[str]:
    parts = (re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", s).strip() for s in re.split(r"(?<=[.!?])\s+|\n+", body or ""))
    return [s for s in parts if len(s.split()) >= 4 and not s.endswith("?") and not _COURTESY.search(s)]


# Input:  claims (list[str]); premises (list[str]) – sentence-level evidence from the cited articles; nli (callable);
#         k (int) – evidence sentences per claim; need (float) – entailment that already counts as supported
# What:   For every claim, NLI-scores the evidence sentences sharing the most words with it (2 first, the next ones only for claims
#         not yet supported, which gives the same decisions as scoring all k at about half the cost) and keeps the best entailment
#         plus the contradiction of that same evidence (no evidence at all = unsupported)
# Output: tuple[np.ndarray, np.ndarray] – entailment and contradiction per claim
def _support(claims: list[str], premises: list[str], nli: Callable, k: int = 4, need: float = 0.85) -> tuple[np.ndarray, np.ndarray]:
    if not claims or not premises:
        return np.zeros(len(claims)), np.ones(len(claims))
    pt = [set(terms(p)) for p in premises]
    jac = lambda ct, j: len(ct & pt[j]) / (len(ct | pt[j]) or 1)
    picks = [sorted(range(len(premises)), key=lambda j: -jac(ct, j))[:k] for ct in (set(terms(c)) for c in claims)]
    ent, con = np.full(len(claims), -1.0), np.ones(len(claims))
    for lo, hi in ((0, 2), (2, k)):
        todo = [(ci, j) for ci in range(len(claims)) if ent[ci] < need for j in picks[ci][lo:hi]]
        if todo:
            for (ci, _), (e, c) in zip(todo, np.asarray(nli([(premises[j], claims[ci]) for ci, j in todo]), dtype=float)):
                if e > ent[ci]:
                    ent[ci], con[ci] = e, c
    return np.maximum(ent, 0.0), con


# Input:  st (TicketState) with a formatted draft; nli (callable); kb (KnowledgeBase); thresholds (RoutingThresholds); scrubber (Scrubber)
# What:   Runs the four always-on blocking checks (outbound PII, NLI groundedness, citation integrity, disclaimer & tone) and
#         records every finding whether the draft passes or not (audit receipt)
# Output: dict – validation_receipt
def validate(st: Any, *, nli: Callable, kb: Any, thresholds: RoutingThresholds, scrubber: Scrubber) -> dict[str, Any]:
    final, cited = st.final_formatted_response, list(st.cited_doc_ids)
    pii = scrubber.scrub(final)[1]

    claims = claim_sentences(st.llm_raw_draft_body)
    ent, con = _support(claims, kb.premises(cited), nli, need=thresholds.nli_entailment_min)
    unsupported = [c for c, e, k in zip(claims, ent, con) if e < thresholds.nli_entailment_min or k > thresholds.nli_contradiction_max]

    retrieved = {p["doc_id"] for p in st.retrieved_passages}
    cite_ok = bool(cited) and all(d in kb.registry and d in retrieved and kb.citation(d) in final for d in cited)
    disc_ok = final.startswith(DISCLAIMER)
    tone = [p for p in PROHIBITED_PHRASES if p in final.lower()] + [m[0] for m in COMMITMENT_RX.finditer(final)]

    failed = [n for n, bad in zip(GUARDRAIL_CHECKS, (bool(pii), bool(unsupported), not cite_ok, not disc_ok or bool(tone))) if bad]
    nli_min = round(float(ent.min()), 4) if len(ent) else None
    details = {
        "outbound_pii_or_secret_block": f"private data detected: {', '.join(sorted(pii))}",
        "nli_hallucination_entailment_block": f"{len(unsupported)} sentence(s) not supported by the cited documentation "
                                              f"(min entailment {nli_min if nli_min is not None else 0:.2f} < {thresholds.nli_entailment_min:.2f})",
        "citation_integrity_block": f"cited {cited or 'nothing'}; citations must be official, retrieved in this trace and in the footer",
        "disclaimer_or_tone_block": "missing mandatory machine disclaimer" if not disc_ok else f"prohibited phrasing: {tone}",
    }
    return {
        "passed": not failed, "checks_run": list(GUARDRAIL_CHECKS), "failed_checks": failed,
        "pii_found": bool(pii), "pii_entity_types": sorted(pii),
        "nli_min_score": nli_min, "nli_max_contradiction": round(float(con.max()), 4) if len(con) else None,
        "claims_checked": len(claims), "unsupported_sentences": unsupported,
        "citation_valid": cite_ok, "cited_doc_ids": cited, "disclaimer_valid": disc_ok, "tone_violations": tone,
        "blocked_reason": failed[0] if failed else None, "failure_detail": details[failed[0]] if failed else None,
    }
