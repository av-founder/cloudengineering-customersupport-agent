"""FastAPI service (python -m src.api): ticket triage, agent feedback and governance audit endpoints + Prometheus /metrics."""
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import APIKeyHeader
from prometheus_client import Counter, Gauge

from src.config import ANSWERED_ACTIONS, get_settings, load_thresholds, setup_logging
from src.schemas import (AuditResponse, EscalationPayload, FeedbackRequest, FeedbackResponse, GovernanceMetadata, PriorityRouting,
                         RoutingDecision, TicketIngestRequest, TicketIngestResponse, TicketState, UIAssistPayload)

log = logging.getLogger(__name__)
_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False, description="Service API key (set API_KEY)")

# Input:  none
# What:   Business / governance counters exported on /metrics next to the standard HTTP metrics
# Output: prometheus_client collectors
ROUTED = Counter("cloudserve_routing_actions_total", "Final routing actions", ["action"])
BLOCKS = Counter("cloudserve_guardrail_blocks_total", "Guardrail activations by check", ["check"])
REDACTED = Counter("cloudserve_ingest_redactions_total", "PII / secret redactions at ingest", ["entity"])
PARITY = Gauge("cloudserve_fairness_parity_delta", "Auto-resolution rate gap, fluent minus non_fluent (process lifetime)")
_COHORTS = {"fluent": [0, 0], "non_fluent": [0, 0]}


# Input:  st (TicketState) – final state
# What:   Updates Prometheus counters and the live fluent vs non_fluent auto-resolution parity gauge
# Output: None
def _observe(st: TicketState) -> None:
    ROUTED.labels(st.routing_action).inc()
    for check in (st.validation_receipt or {}).get("failed_checks", []):
        BLOCKS.labels(check).inc()
    for entity, n in st.pii_redaction_counts.items():
        REDACTED.labels(entity).inc(n)
    if st.language_fluency in _COHORTS:
        _COHORTS[st.language_fluency][0] += st.routing_action in ANSWERED_ACTIONS
        _COHORTS[st.language_fluency][1] += 1
        rate = lambda c: _COHORTS[c][0] / _COHORTS[c][1] if _COHORTS[c][1] else 0.0
        PARITY.set(rate("fluent") - rate("non_fluent"))


# Input:  st (TicketState) – final state
# What:   Maps the internal state to the public API contract; drafts are only exposed for AUTO_REPLY / TIER1_DRAFT_ASSIST
# Output: TicketIngestResponse
def to_response(st: TicketState) -> TicketIngestResponse:
    answered, r = st.routing_action in ANSWERED_ACTIONS, st.validation_receipt or {}
    return TicketIngestResponse(
        ticket_id=st.ticket_id, trace_id=st.trace_id, decision_id=st.decision_id,
        priority_routing=PriorityRouting(priority_level=st.priority_level, priority_score=st.priority_score, queue_position=max(1, st.queue_position)),
        routing_decision=RoutingDecision(action=st.routing_action, reason=st.routing_reason_manager_text, predicted_intent=st.predicted_intent,
                                         calibrated_confidence=st.calibrated_confidence, considered_alternatives=st.considered_alternatives),
        ticket_ui_assist_payload=UIAssistPayload(**st.ticket_ui_assist_payload) if answered else UIAssistPayload(),
        escalation_payload=EscalationPayload(target_queue=st.target_queue, structured_summary_markdown=None if answered else st.escalation_summary_markdown,
                                             attached_candidate_sources=[] if answered else st.attempted_sources),
        governance_metadata=GovernanceMetadata(pii_redacted_at_ingest=st.pii_redacted_at_ingest, redacted_types=st.pii_redaction_types,
                                               outbound_pii_check="BLOCKED" if r.get("pii_found") else "PASSED", nli_groundedness_score=r.get("nli_min_score"),
                                               citation_verified=bool(r.get("citation_valid")), disclaimer_verified=bool(r.get("disclaimer_valid")),
                                               latency_ms=st.end_to_end_latency_ms))


# Input:  pipeline / store (optional) – injected components (tests); instrument (bool) – attach Prometheus HTTP metrics;
#         api_key (str | None) – overrides API_KEY (tests); an empty key disables auth in development only
# What:   Builds the FastAPI app. Without injected components the lifespan loads the frozen calibration artifact (fails fast if
#         it is missing), verifies + indexes the KB, opens the audit database and compiles the pipeline once.
# Output: FastAPI
def create_app(pipeline: Any = None, store: Any = None, instrument: bool = True, api_key: str | None = None) -> FastAPI:
    settings = get_settings()
    key = settings.api_key.get_secret_value() if api_key is None else api_key
    if not key:
        log.warning("API authentication is disabled (API_KEY is empty); use this only for local development")
    if not settings.customer_id_salt.get_secret_value():
        log.warning("CUSTOMER_ID_SALT is empty; customer ID hashes are not keyed. Set it before handling real tickets")

    async def require_api_key(x_api_key: str | None = Depends(_API_KEY_HEADER)) -> None:
        # Input:  x_api_key (str | None) – X-API-Key request header
        # What:   Constant-time check of the shared service key; no-op only when no key is configured (development)
        # Output: None (raises 401 when the key is missing or wrong)
        valid = False
        if x_api_key:
            try:
                valid = secrets.compare_digest(x_api_key.encode("utf-8"), key.encode("utf-8"))
            except (UnicodeError, TypeError):  # malformed header must be a 401, never a 500
                valid = False
        if key and not valid:
            raise HTTPException(status_code=401, detail="missing or invalid API key", headers={"WWW-Authenticate": "ApiKey"})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if pipeline is None:
            from src.graph import TriagePipeline
            from src.guardrails import SCRUBBER, NLIScorer
            from src.llm import LLMClient
            from src.logging_store import AuditStore
            from src.retrieve import KnowledgeBase
            s = get_settings()
            setup_logging()
            t, kb = load_thresholds(), KnowledgeBase.from_settings(s)
            app.state.store = AuditStore(s.async_db_url)
            await app.state.store.init(kb.registry)
            kb.excluded = await app.state.store.quarantined_doc_ids()
            app.state.pipe = TriagePipeline(settings=s, thresholds=t, kb=kb, llm=LLMClient(s), nli=NLIScorer(s.nli_model), scrubber=SCRUBBER, store=app.state.store)
            await app.state.pipe.warm_up()
        else:
            app.state.pipe, app.state.store = pipeline, store
            if store:
                await store.init(pipeline.kb.registry)
        yield
        if pipeline is None:
            await app.state.store.close()

    app = FastAPI(title="CloudServe Triage & Drafting Engine", version="3.3.0", lifespan=lifespan)
    if instrument:
        from prometheus_fastapi_instrumentator import Instrumentator
        Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    @app.post("/api/v1/tickets/ingest", response_model=TicketIngestResponse, dependencies=[Depends(require_api_key)])
    async def ingest_ticket(req: TicketIngestRequest, request: Request) -> TicketIngestResponse:
        # Input:  req (TicketIngestRequest) – one customer ticket
        # What:   Runs the full triage pipeline, computes the live queue position and returns the public contract
        # Output: TicketIngestResponse
        st = await request.app.state.pipe.process(req.model_dump())
        if st.decision_id and request.app.state.store:
            st.queue_position = await request.app.state.store.queue_position(st.ticket_id)
        _observe(st)
        return to_response(st)

    @app.post("/api/v1/tickets/{ticket_id}/feedback", response_model=FeedbackResponse, dependencies=[Depends(require_api_key)])
    async def ticket_feedback(ticket_id: UUID, req: FeedbackRequest, request: Request) -> FeedbackResponse:
        # Input:  ticket_id (UUID); req (FeedbackRequest) – agent or technical-writer correction
        # What:   Stores the correction against the decision it refers to (closes the human feedback loop)
        # Output: FeedbackResponse
        try:
            fid = await request.app.state.store.add_feedback(str(ticket_id), req.model_dump())
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return FeedbackResponse(feedback_id=fid, ticket_id=str(ticket_id), decision_id=str(req.decision_id))

    @app.get("/api/v1/governance/audit/{ticket_id}", response_model=AuditResponse, dependencies=[Depends(require_api_key)])
    async def governance_audit(ticket_id: UUID, request: Request) -> AuditResponse:
        # Input:  ticket_id (UUID)
        # What:   Returns the latest decision with its sanitised input and verifies the whole SHA-256 hash chain
        # Output: AuditResponse
        rec = await request.app.state.store.latest_decision(str(ticket_id))
        if not rec:
            raise HTTPException(status_code=404, detail="no decision recorded for this ticket")
        return AuditResponse(hash_chain_verified=await request.app.state.store.verify_chain(), audit_record=rec)

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        # Input:  none
        # What:   Liveness plus the frozen calibration version, routing mode and LLM provider chain
        # Output: dict
        p = request.app.state.pipe
        return {"status": "ok", "calibration_version": p.t.calibration_version, "routing_mode": p.mode, "llm_chain": getattr(p.llm, "chain", "n/a")}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    cfg = get_settings()
    uvicorn.run(app, host=cfg.api_host, port=cfg.api_port, log_level=cfg.log_level.lower())
