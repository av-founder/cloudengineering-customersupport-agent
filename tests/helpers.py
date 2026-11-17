"""Deterministic fakes (LLM, embedder, reranker, NLI) and small builders shared by the test-suite."""
import hashlib
import re
from types import SimpleNamespace
from typing import Any

import numpy as np

from src.config import terms as _terms
from src.llm import LLMResult, LLMUnavailableError

DEPLOY_TITLE = "Container deployments failing during the health check phase"


# Input:  texts (list[str])
# What:   Hashing-trick bag-of-words embedder (deterministic, 384-d, L2-normalised)
# Output: np.ndarray
def fake_embed(texts: list[str]) -> np.ndarray:
    out = np.zeros((len(texts), 384), dtype=np.float32)
    for i, t in enumerate(texts):
        for w in _terms(t):
            out[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % 384] += 1
    return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)


class FakeRerank:
    # What: cross-encoder stand-in; explicit (query keyword, doc_id or None, score) rules, otherwise a low overlap score
    def __init__(self):
        self.rules: list[tuple[str, str | None, float]] = []
        self.queries: list[str] = []

    def __call__(self, pairs: list[tuple[str, str]]) -> np.ndarray:
        out = []
        for q, chunk in pairs:
            self.queries.append(q)
            doc = re.search(r"\[Doc ID: ([^ |]+)", chunk)[1]
            hit = next((s for kw, d, s in self.rules if kw in q.lower() and d in (None, doc)), None)
            qt = set(_terms(q))
            out.append(hit if hit is not None else round(0.2 * len(qt & set(_terms(chunk))) / max(len(qt), 1), 4))
        return np.array(out)


class FakeNLI:
    # What: NLI stand-in; a claim whose words are (almost) all in the premise is entailed (0.95), otherwise 0.31
    def __call__(self, pairs: list[tuple[str, str]]) -> np.ndarray:
        cov = lambda p, c: len(set(_terms(c)) & set(_terms(p))) / max(len(set(_terms(c))), 1)
        return np.array([[0.95, 0.01] if cov(p, c) >= 0.8 else [0.31, 0.05] for p, c in pairs])


CLASSIFY_RULES = [
    (r"ssh|compromis|unauthori[sz]ed|unknown ip", "security_incident", "high"),
    (r"gdpr|eu-only|auditor", "compliance_request", "medium"),
    (r"invoice|billing", "billing_query", "medium"),
    (r"roadmap|would be (?:great|useful)", "feature_request", "low"),
    (r"not working|call us|any update", "unclear_request", "medium"),
    (r"smtp|relay port|legacy configuration", "configuration_help", "medium"),
    (r"health check|502|bad gateway|deploy|rolls? back|dependency|build", "deployment_failure", "high"),
    (r"page size|pagination|all data|cursor", "api_usage_question", "low"),
]
TITLE_RULES = [
    (r"dependency|build", "Build failures caused by dependency resolution"),
    (r"health check|502|bad gateway|deploy|rolls? back", DEPLOY_TITLE),
    (r"page size|pagination|all data|cursor", "Pagination and handling large result sets"),
    (r"smtp|relay port", "Configuring environment variables and secrets"),
]


class FakeLLM:
    # What: role-aware LLM stand-in (classify / FR-06 title / generate) with switchable failure modes
    chain, available = "fake:fake-model", True

    def __init__(self):
        self.calls, self.backoff_seconds, self.failovers, self.cache_hits = 0, 0.0, 0, 0
        self.down, self.obey_injection, self.generate_override, self.messages = False, False, None, []

    async def complete_json(self, messages: list[dict], *, temperature: float, max_tokens: int, sample: int = 0) -> LLMResult:
        if self.down:
            raise LLMUnavailableError("simulated outage")
        self.calls += 1
        self.messages.append(messages)
        return LLMResult(respond(messages, self), "fake", "fake-model", 120, 30)

    def roles(self) -> list[str]:
        return [role_of(m) for m in self.messages]


# Input:  messages (list[dict])
# What:   Which prompt role a request belongs to
# Output: str – classify | title | generate
def role_of(messages: list[dict]) -> str:
    system = messages[0]["content"]
    return "classify" if "Triage Assistant" in system else "title" if "Semantic Search Assistant" in system else "generate"


# Input:  messages (list[dict]); fake (FakeLLM | None) – optional behaviour switches
# What:   Deterministic JSON answer for the given prompt role
# Output: dict
def respond(messages: list[dict], fake: FakeLLM | None = None) -> dict:
    user, role = messages[-1]["content"], role_of(messages)
    t = user.split("<untrusted_customer_ticket>")[-1].lower()
    if role == "classify":
        hit = next(((i, u) for rx, i, u in CLASSIFY_RULES if re.search(rx, t)), None)
        if not hit:
            return {"intent": "I Don't Know", "confidence": 0.3, "urgency": "medium", "alternatives": []}
        alts = [a for a in ("configuration_help", "onboarding", "integration_help") if a != hit[0]][:2]
        return {"intent": hit[0], "confidence": 0.92, "urgency": hit[1],
                "alternatives": [{"intent": alts[0], "probability": 0.05}, {"intent": alts[1], "probability": 0.02}]}
    if role == "title":
        return {"matched_title": next((title for rx, title in TITLE_RULES if re.search(rx, t)), "I Don't Know")}
    if fake and fake.generate_override:
        return fake.generate_override(user)
    ctx = user.split("<untrusted_customer_ticket>")[0]
    docs = re.findall(r'<doc id="([^"]+)" title="[^"]*">\n(.*?)\n</doc>', ctx, re.S)
    if fake and fake.obey_injection and "ignore all previous instructions" in t:
        return {"abstain": False, "draft_body": "Your account has been credited $10,000.", "cited_doc_ids": [docs[0][0]] if docs else []}
    if not docs or ("smtp" in t and "smtp" not in ctx.lower()):
        return {"abstain": True, "draft_body": "I Don't Know", "cited_doc_ids": []}
    steps = re.findall(r"(?m)^\d+\. (.+)$", docs[0][1])[:2]
    return {"abstain": False, "draft_body": "\n".join(steps), "cited_doc_ids": [docs[0][0]],
            "claim_to_doc_map": [{"sentence": s, "source_doc_id": docs[0][0]} for s in steps]}


# Input:  content (str) – JSON reply text
# What:   Minimal chat-completion response object (choices[0].message.content + usage) as returned by Groq / OpenRouter SDKs
# Output: SimpleNamespace
def chat_response(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))


# Input:  body (str) and optional ticket fields
# What:   Ticket payload in the development-set format
# Output: dict
def ticket(body: str, *, subject: str = "", channel: str = "email", tier: str = "standard", fluency: str = "fluent", tid: str = "T-1", **extra: Any) -> dict:
    return {"ticket_id": tid, "channel": channel, "subject": subject, "body": body, "customer_id": "CUST-1", "customer_tier": tier,
            "language_fluency": fluency, "received_at": "2026-11-16T09:00:00Z"} | extra


__all__ = ["DEPLOY_TITLE", "FakeLLM", "FakeNLI", "FakeRerank", "LLMUnavailableError", "chat_response", "fake_embed", "respond", "role_of", "ticket"]
