"""CLASSIFY (FR-04 / FR-08): k-sample self-consistency intent + urgency, temperature-scaled Platt calibration, safe fallback."""
import asyncio
import difflib
import json
import math
import re
from collections import Counter, defaultdict
from functools import lru_cache
from typing import Any

from src.config import (CLASSIFY_MAX_TOKENS, CLASSIFY_TEMPERATURE, FALLBACK_INTENT, IDK, INTENTS, MANDATORY_INTENTS, PROMPTS_DIR,
                        TRAPDOOR, URGENCY, PlattScaling, load_prompt)
from src.llm import LLMClient, usage_update

_IDK_WORDS = {"i_dont_know", "idk", "unknown", "none", "unclassified_fallback", "unclassified", "other"}


# Input:  raw (float 0..1) – agreement-weighted model probability; ps (PlattScaling) – parameters from the calibration file
# What:   P(correct) = 1 / (1 + exp(-(a*z + b) / T)) with z = logit(raw)
# Output: float – calibrated confidence in 0..1
def platt(raw: float, ps: PlattScaling) -> float:
    r = min(max(raw, 1e-4), 1 - 1e-4)
    x = -(ps.slope_a * math.log(r / (1 - r)) + ps.intercept_b) / ps.temperature_T
    return 1.0 / (1.0 + math.exp(min(x, 700.0)))


# Input:  name (Any) – intent text produced by the model
# What:   Maps it onto an exact taxonomy name ("I Don't Know" variants -> IDK, small typos fixed by fuzzy matching)
# Output: str | None – canonical intent, IDK, or None when unusable
def canon_intent(name: Any) -> str | None:
    s = re.sub(r"[^a-z_]", "", re.sub(r"[\s\-]+", "_", str(name or "").strip().lower()))
    if not s or s in _IDK_WORDS:
        return IDK
    return s if s in INTENTS else next(iter(difflib.get_close_matches(s, INTENTS, n=1, cutoff=0.85)), None)


# Input:  v (Any) – a probability the model wrote (0..1, or a percentage)
# What:   Converts it to a safe float in 0..1 (0.5 when missing or unreadable)
# Output: float
def _prob(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.5
    return min(max(f / 100 if 1 < f <= 100 else f, 0.0), 1.0)


# Input:  d (dict) – one parsed model reply
# What:   Extracts the top intent, urgency and a probability map over the top intent + alternatives (normalised to <= 1)
# Output: tuple[str | None, str, dict[str, float]]
def _parse(d: dict) -> tuple[str | None, str, dict[str, float]]:
    top = canon_intent(d.get("intent"))
    dist = {top: _prob(d.get("confidence"))} if top not in (None, IDK) else {}
    for a in d.get("alternatives") or []:
        if isinstance(a, dict) and (k := canon_intent(a.get("intent"))) not in (None, IDK, top):
            dist[k] = max(dist.get(k, 0.0), _prob(a.get("probability")))
    total = sum(dist.values())
    urgency = str(d.get("urgency", "")).strip().lower()
    return top, urgency if urgency in URGENCY else "medium", {k: v / total for k, v in dist.items()} if total > 1 else dist


# Input:  none
# What:   Renders the FR-04 system prompt once: intent glossary + representative (paraphrased) few-shot examples
# Output: tuple[str, dict] – system prompt text and prompt metadata
@lru_cache
def _system_prompt() -> tuple[str, dict]:
    meta, tpl = load_prompt("FR-04_triage_classifier")
    shots = json.loads((PROMPTS_DIR / "FR-04_fewshot.json").read_text(encoding="utf-8"))
    intents = "\n".join(f"- {k}: {v}" for k, v in INTENTS.items())
    examples = "\n".join(f'Ticket: "{s["ticket"]}"\n-> {{"intent": "{s["intent"]}", "urgency": "{s["urgency"]}"}}' for s in shots)
    return tpl.safe_substitute(intents=intents, examples=examples), meta


# Input:  st (TicketState) – normalised ticket; llm (LLMClient); platt_params (PlattScaling) – from the calibration file;
#         samples (int) – k self-consistency draws
# What:   Asks the Triage Assistant k times in parallel, takes the majority intent, averages the probability each draw gave
#         that intent (agreement-weighted confidence), calibrates it and records the top-3 alternatives. Empty, unknown or
#         failed cases map to 'unclassified_fallback' with confidence 0; a keyword trapdoor forces human review.
# Output: dict – TicketState updates
async def classify_ticket(st: Any, *, llm: LLMClient, platt_params: PlattScaling, samples: int = 3) -> dict[str, Any]:
    trap = next(((cat, m[0]) for cat, rx in TRAPDOOR.items() if (m := rx.search(st.ticket_text()))), None)
    base = {"mandatory_category": trap[0] if trap else None, "mandatory_trigger": f"keyword:{trap[1].lower()}" if trap else None}

    def fallback(note: str, extra: dict | None = None) -> dict[str, Any]:
        return base | (extra or {}) | {"predicted_intent": FALLBACK_INTENT, "predicted_urgency": "medium", "raw_confidence": 0.0,
                                       "calibrated_confidence": 0.0, "considered_alternatives": [], "classification_note": note,
                                       "is_mandatory_escalation": bool(trap)}

    if "EMPTY_PAYLOAD_FLAGGED" in st.ingest_anomaly_flags:
        return fallback("empty payload")
    system, _ = _system_prompt()
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": st.ticket_block()}]
    outs = await asyncio.gather(*(llm.complete_json(msgs, temperature=CLASSIFY_TEMPERATURE, max_tokens=CLASSIFY_MAX_TOKENS, sample=i)
                                  for i in range(samples)), return_exceptions=True)
    ok = [o for o in outs if not isinstance(o, BaseException)]
    usage = usage_update(st, ok)
    if not ok:
        return fallback(f"LLM unavailable ({type(outs[0]).__name__})", usage)

    parsed = [_parse(o.data) for o in ok]
    votes = Counter(p[0] for p in parsed)
    top = max(votes, key=lambda i: (votes[i], sum(p[2].get(i, 0.0) for p in parsed)))
    if top in (None, IDK):
        return fallback("model answered I Don't Know (ambiguous, novel or unclassifiable ticket)", usage)

    dist: dict[str, float] = defaultdict(float)
    for _, _, d in parsed:
        for k, v in d.items():
            dist[k] += v / len(parsed)
    raw = dist[top]
    alts = [{"intent": k, "probability": round(platt(v, platt_params), 4), "raw_probability": round(v, 4)}
            for k, v in sorted(dist.items(), key=lambda kv: -kv[1])][:3]
    category = base["mandatory_category"] or (top if top in MANDATORY_INTENTS else None)
    return base | usage | {
        "predicted_intent": top, "predicted_urgency": Counter(p[1] for p in parsed).most_common(1)[0][0],
        "raw_confidence": round(raw, 4), "calibrated_confidence": round(platt(raw, platt_params), 4), "considered_alternatives": alts,
        "classification_note": f"{votes[top]}/{len(parsed)} samples agreed", "is_mandatory_escalation": bool(category), "mandatory_category": category,
    }
