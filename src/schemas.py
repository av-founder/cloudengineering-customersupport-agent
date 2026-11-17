"""Typed contracts: the LangGraph TicketState plus the REST API request/response models."""
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr

from src.config import FALLBACK_INTENT, IDK

Channel = Literal["email", "chat", "forum", "docs_comment"]
Tier = Literal["standard", "business", "enterprise"]
Fluency = Literal["fluent", "non_fluent", "unknown"]
Urgency = Literal["low", "medium", "high"]
PriorityLevel = Literal["low", "medium", "high", "critical"]
RoutingAction = Literal["AUTO_REPLY", "TIER1_DRAFT_ASSIST", "ESCALATE_MANDATORY_INTENT", "ESCALATE_LOW_CONFIDENCE", "ESCALATE_NO_GROUNDED_DOC", "BLOCK_SAFETY_GUARDRAIL_FAIL"]
Queue = Literal["TIER_1_HUMAN", "TIER_2_SPECIALIST"]


class TicketState(BaseModel):
    # Input:  built step by step by the LangGraph nodes (each node returns a partial update)
    # What:   The single typed record that flows through INGEST -> CLASSIFY -> ROUTE -> RETRIEVE -> GENERATE -> VALIDATE;
    #         raw customer text is kept only in memory (excluded from every dump, log and database write)
    # Output: TicketState
    raw_payload: dict[str, Any] = Field(default_factory=dict, exclude=True, repr=False)
    raw_subject_preserved: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    raw_body_preserved: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)

    ticket_id: str = ""
    trace_id: str = ""
    external_ticket_ref: str = ""
    source_channel: Channel = "email"
    received_at: str | None = None
    sanitized_subject: str = ""
    sanitized_body: str = ""
    ingest_anomaly_flags: list[str] = Field(default_factory=list)
    pii_redacted_at_ingest: bool = False
    pii_redaction_types: list[str] = Field(default_factory=list)
    pii_redaction_counts: dict[str, int] = Field(default_factory=dict)
    customer_id_hash: str = ""
    customer_tier: Tier = "standard"
    language_fluency: Fluency = "unknown"

    priority_level: PriorityLevel = "medium"
    priority_score: int = 50
    queue_position: int = 1

    predicted_intent: str = FALLBACK_INTENT
    predicted_urgency: Urgency = "medium"
    raw_confidence: float = 0.0
    calibrated_confidence: float = 0.0
    considered_alternatives: list[dict[str, Any]] = Field(default_factory=list)
    classification_note: str = ""
    is_mandatory_escalation: bool = False
    mandatory_category: str | None = None
    mandatory_trigger: str | None = None

    semantic_matched_title: str = IDK
    retrieval_executed: bool = False
    retrieved_passages: list[dict[str, Any]] = Field(default_factory=list)
    attempted_sources: list[dict[str, Any]] = Field(default_factory=list)
    highest_rejected_retrieval_score: float = 0.0
    top_retrieval_score: float = 0.0

    pending_action: str | None = None
    pending_reason: str | None = None
    routing_action: RoutingAction | None = None
    routing_reason_manager_text: str = ""
    target_queue: Queue | None = None

    abstained: bool = False
    llm_raw_draft_body: str = ""
    cited_doc_ids: list[str] = Field(default_factory=list)
    claim_to_doc_map: list[dict[str, Any]] = Field(default_factory=list)
    final_formatted_response: str = ""
    attached_doc_link: str | None = None
    ticket_ui_assist_payload: dict[str, Any] = Field(default_factory=dict)
    escalation_summary_markdown: str = ""
    validation_receipt: dict[str, Any] = Field(default_factory=dict)
    guardrail_passed: bool = False

    calibration_version: str = ""
    prompt_bundle_version: str = ""
    model_provider: str = "none"
    model_identifier: str = "none"
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    end_to_end_latency_ms: int = 0
    decision_id: str | None = None
    error: str | None = None
    failed_node: str | None = None

    # Input:  none
    # What:   The sanitised ticket text used for search queries
    # Output: str
    def ticket_text(self) -> str:
        return f"{self.sanitized_subject}\n{self.sanitized_body}".strip()

    # Input:  none
    # What:   Wraps the sanitised ticket in <untrusted_customer_ticket> tags so the LLM treats it as data, never as instructions
    # Output: str – prompt-safe block
    def ticket_block(self) -> str:
        return f"<untrusted_customer_ticket>\nSubject: {self.sanitized_subject or '(none)'}\nMessage: {self.sanitized_body}\n</untrusted_customer_ticket>"


# ---------------------------------------------------------------------------------------------------------------------
# REST API contracts (spec section 5)
# ---------------------------------------------------------------------------------------------------------------------

class TicketIngestRequest(BaseModel):
    external_ticket_ref: str = Field(min_length=1, max_length=128)
    channel: Literal["email", "chat", "forum", "docs_comment", "live_chat", "doc_comments", "web_form"]
    customer_id: str = Field(min_length=1, max_length=256)
    customer_tier: Tier = "standard"
    language_fluency: Fluency = "unknown"
    subject: str = Field("", max_length=20_000)
    body: str = Field("", max_length=100_000)


class PriorityRouting(BaseModel):
    priority_level: PriorityLevel
    priority_score: int = Field(ge=1, le=100)
    queue_position: int = Field(ge=1)


class RoutingDecision(BaseModel):
    action: RoutingAction
    reason: str
    predicted_intent: str
    calibrated_confidence: float
    considered_alternatives: list[dict[str, Any]]


class UIAssistPayload(BaseModel):
    draft_response: str | None = None
    doc_id: str | None = None
    article_title: str | None = None
    doc_link: str | None = None


class EscalationPayload(BaseModel):
    target_queue: Queue | None = None
    structured_summary_markdown: str | None = None
    attached_candidate_sources: list[dict[str, Any]] = Field(default_factory=list)


class GovernanceMetadata(BaseModel):
    pii_redacted_at_ingest: bool
    redacted_types: list[str]
    outbound_pii_check: Literal["PASSED", "BLOCKED"]
    nli_groundedness_score: float | None
    citation_verified: bool
    disclaimer_verified: bool
    latency_ms: int


class TicketIngestResponse(BaseModel):
    ticket_id: str
    trace_id: str
    decision_id: str | None
    priority_routing: PriorityRouting
    routing_decision: RoutingDecision
    ticket_ui_assist_payload: UIAssistPayload
    escalation_payload: EscalationPayload
    governance_metadata: GovernanceMetadata


class FeedbackRequest(BaseModel):
    decision_id: UUID
    agent_id: str = Field(min_length=1, max_length=64)
    agent_tier: Literal["TIER_1", "TIER_2", "TECH_WRITER"]
    agent_action: Literal["ACCEPTED_AS_IS", "EDITED_AND_SENT", "REJECTED_ESCALATED", "RESOLVED_BY_TIER2"]
    actual_true_intent: str | None = Field(None, max_length=128)
    flag_for_tech_writer: bool = False
    failure_attribution: Literal["DOC_MISSING", "DOC_OUTDATED", "AI_MISINTERPRETED", "NONE"] = "NONE"
    agent_notes: str | None = None
    customer_csat_score: int | None = Field(None, ge=1, le=5)


class FeedbackResponse(BaseModel):
    feedback_id: str
    ticket_id: str
    decision_id: str


class AuditResponse(BaseModel):
    hash_chain_verified: bool
    audit_record: dict[str, Any]
