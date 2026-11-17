"""ROUTE (DEP-01): deterministic two-stage routing, FR-08 priority scoring and FR-07 escalation briefs. No LLM, no randomness."""
from typing import Any

from src.config import FALLBACK_INTENT, IDK, MANDATORY_TIER2, PRIORITY_SCORES, REASONS, ThresholdArtifact


# Input:  tier (str) – customer tier; urgency (str) – predicted urgency
# What:   FR-08 queue priority: Enterprise + high urgency 95, other Enterprise 85, high urgency 85, medium 50, low 25
# Output: tuple[str, int] – (priority level, priority score 1..100)
def priority(tier: str, urgency: str) -> tuple[str, int]:
    high = urgency in ("high", "critical")
    score = (PRIORITY_SCORES["enterprise_high"] if high else PRIORITY_SCORES["enterprise_other"]) if tier == "enterprise" else \
        PRIORITY_SCORES["high"] if high else PRIORITY_SCORES.get(urgency, PRIORITY_SCORES["medium"])
    return ("critical" if score >= 95 else "high" if score >= 85 else "medium" if score >= 50 else "low"), score


# Input:  items (list[tuple[int, str | None]]) – (priority_score, received_at ISO) per ticket in arrival order
# What:   Ranks tickets highest priority first and, within the same priority, oldest first (FR-08 queue)
# Output: list[int] – 1-based queue position for each input item
def queue_order(items: list[tuple[int, str | None]]) -> list[int]:
    pos = [0] * len(items)
    for rank, i in enumerate(sorted(range(len(items)), key=lambda i: (-items[i][0], items[i][1] or "", i)), 1):
        pos[i] = rank
    return pos


# Input:  st (TicketState) – classified ticket; t (ThresholdArtifact); mode (str) – ROUTING_MODE kill switch
# What:   Stage 1 (pre-retrieval) gate: priority first, then the mandatory-intent bypass, then confidence vs tau_class
# Output: dict – priority fields + pending_action / pending_reason (None = continue to retrieval)
def stage1(st: Any, t: ThresholdArtifact, mode: str) -> dict[str, Any]:
    level, score = priority(st.customer_tier, st.predicted_urgency)
    if mode == "PASSTHROUGH_QUEUE":
        pending = ("ESCALATE_LOW_CONFIDENCE", "passthrough")
    elif st.is_mandatory_escalation:
        pending = ("ESCALATE_MANDATORY_INTENT", "mandatory")
    elif st.predicted_intent == FALLBACK_INTENT or st.calibrated_confidence < t.routing_thresholds.classification_confidence_min:
        pending = ("ESCALATE_LOW_CONFIDENCE", "low_confidence")  # retrieval still runs so the human gets candidate docs
    else:
        pending = (None, None)
    return {"priority_level": level, "priority_score": score, "pending_action": pending[0], "pending_reason": pending[1]}


# Input:  st (TicketState) – after retrieval
# What:   Stage 2 (post-retrieval) gate: keep a pending escalation, else require >=1 passage >= tau_ret and an FR-06 title match
# Output: dict – pending_action / pending_reason updates (empty = proceed to GENERATE)
def stage2(st: Any) -> dict[str, Any]:
    if st.pending_action:
        return {}
    if not st.retrieved_passages:
        return {"pending_action": "ESCALATE_NO_GROUNDED_DOC", "pending_reason": "no_passage"}
    if st.semantic_matched_title == IDK:
        return {"pending_action": "ESCALATE_NO_GROUNDED_DOC", "pending_reason": "no_title"}
    return {}


# Input:  action (str); why (str | None) – reason code; st (TicketState); t (ThresholdArtifact)
# What:   Fills the matching plain-English template so a support manager or auditor can read why the decision was made
# Output: str – routing_reason_manager_text
def manager_reason(action: str, why: str | None, st: Any, t: ThresholdArtifact) -> str:
    rt, top, r = t.routing_thresholds, (st.retrieved_passages or [{}])[0], st.validation_receipt or {}
    key = {"passthrough": "PASSTHROUGH", "error": "ERROR", "no_title": "NO_TITLE", "abstained": "ABSTAINED"}.get(why or "", action)
    if action == "ESCALATE_MANDATORY_INTENT":
        key = "MANDATORY_TIER2" if st.mandatory_category in MANDATORY_TIER2 else "MANDATORY_TIER1"
    text = REASONS[key].format(
        intent=st.mandatory_category or st.predicted_intent, conf=st.calibrated_confidence, tau_class=rt.classification_confidence_min,
        tau_ret=rt.retrieval_relevance_min, doc_id=top.get("doc_id", "-"), title=top.get("article_title", "-"), ret=top.get("rerank_score", 0.0),
        max_rejected=st.highest_rejected_retrieval_score, failed_check=r.get("blocked_reason"), failure_detail=r.get("failure_detail"),
        node=st.failed_node or "pipeline")
    note = st.classification_note if st.predicted_intent == FALLBACK_INTENT and key == "ESCALATE_LOW_CONFIDENCE" else ""
    trap = f" Trigger: {st.mandatory_trigger}." if action == "ESCALATE_MANDATORY_INTENT" and st.mandatory_trigger else ""
    return text + (f" Note: {note}." if note else "") + trap


# Input:  st (TicketState); action (str); queue (str); reason (str)
# What:   Deterministic FR-07 Markdown brief for the human agent: Ticket Context, Attempted Docs and Reason for Escalation
# Output: str – Markdown
def escalation_summary(st: Any, action: str, queue: str, reason: str) -> str:
    alts = ", ".join(f"{a['intent']} ({a['probability']:.2f})" for a in st.considered_alternatives) or "none recorded"
    quote = lambda s: "\n".join(f"> {ln}" for ln in (s or "(empty)").splitlines())
    docs = [f"| {d['doc_id']} | {d['article_title']} | {d['rerank_score']:.2f} | {d['doc_link']} |" for d in st.attempted_sources]
    flagged = (st.validation_receipt or {}).get("unsupported_sentences") or []
    return "\n".join([
        f"## Escalation Summary - {st.external_ticket_ref}",
        f"**Action:** `{action}` | **Target queue:** {queue} | **Priority:** {st.priority_level} ({st.priority_score})", "",
        "### Ticket Context",
        f"- **Channel / tier / fluency:** {st.source_channel} / {st.customer_tier} / {st.language_fluency}",
        f"- **Predicted intent:** {st.predicted_intent} (calibrated confidence {st.calibrated_confidence:.2f}, urgency {st.predicted_urgency})",
        f"- **Considered alternatives:** {alts}",
        f"- **Ingest flags:** {', '.join(st.ingest_anomaly_flags) or 'none'} | **PII redacted:** {', '.join(st.pii_redaction_types) or 'none'}",
        "- **Customer message (sanitised):**", quote(st.sanitized_subject), quote(st.sanitized_body), "",
        "### Attempted Docs",
        *(["| Doc ID | Article | Relevance | Link |", "|---|---|---|---|", *docs] if docs else
          ["_No documentation retrieval was attempted (policy bypass) or no candidate passages were found._"]), "",
        "### Reason for Escalation", reason,
        *(["", "**Unsupported sentences flagged by the guardrail:**", *(f"- {s}" for s in flagged)] if flagged else []),
    ])


# Input:  st (TicketState) with pending_action / pending_reason or an error; t (ThresholdArtifact)
# What:   Final escalation: action, manager reason, owning human queue and the FR-07 brief
# Output: dict – TicketState updates
def escalate(st: Any, t: ThresholdArtifact) -> dict[str, Any]:
    action = st.pending_action or "ESCALATE_LOW_CONFIDENCE"
    why = "error" if st.error else st.pending_reason
    queue = "TIER_2_SPECIALIST" if action == "ESCALATE_MANDATORY_INTENT" and st.mandatory_category in MANDATORY_TIER2 else "TIER_1_HUMAN"
    reason = manager_reason(action, why, st, t)
    receipt = (st.validation_receipt or {}) | ({"pipeline_error": st.error, "failed_node": st.failed_node} if st.error else {})
    return {"routing_action": action, "routing_reason_manager_text": reason, "target_queue": queue, "guardrail_passed": False,
            "validation_receipt": receipt, "escalation_summary_markdown": escalation_summary(st, action, queue, reason)}


# Input:  st (TicketState) – a draft that passed all four guardrails; t (ThresholdArtifact); mode (str)
# What:   Releases the response: AUTO_REPLY normally, TIER1_DRAFT_ASSIST when outward auto-reply is switched off
# Output: dict – TicketState updates
def release(st: Any, t: ThresholdArtifact, mode: str) -> dict[str, Any]:
    action = "TIER1_DRAFT_ASSIST" if mode == "ASSIST_AND_ESCALATE_ONLY" else "AUTO_REPLY"
    return {"routing_action": action, "routing_reason_manager_text": manager_reason(action, None, st, t), "guardrail_passed": True,
            "target_queue": "TIER_1_HUMAN" if action == "TIER1_DRAFT_ASSIST" else None, "pending_action": None, "pending_reason": None}


# Input:  st (TicketState); err (str) – what went wrong while escalating
# What:   Last-resort escalation that cannot fail (no templating), so a ticket still ends with a terminal decision
# Output: dict – TicketState updates
def emergency_escalation(st: Any, err: str) -> dict[str, Any]:
    return {"routing_action": st.pending_action or "ESCALATE_LOW_CONFIDENCE", "target_queue": "TIER_1_HUMAN", "guardrail_passed": False,
            "routing_reason_manager_text": f"Escalated to human agent: automated processing failed ({err}); no reply was sent.",
            "escalation_summary_markdown": f"## Escalation Summary - {st.external_ticket_ref}\nAutomated processing failed ({err}); please triage manually."}
