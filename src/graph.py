"""LangGraph two-stage routing state machine + fault-isolated pipeline runner (every ticket ends with a logged decision)."""
import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from langgraph.graph import END, START, StateGraph

from src import classify, generate, guardrails, ingest, retrieve, route
from src.config import Settings, ThresholdArtifact, prompt_bundle_version
from src.schemas import TicketState

log = logging.getLogger(__name__)
Node = Callable[[TicketState], Awaitable[dict[str, Any]]]


# Input:  name (str) – node name; fn (Node) – node body
# What:   Skips work once an earlier step failed and turns any exception into a recorded error, so the graph always
#         reaches ESCALATE instead of crashing (the escalate node itself falls back to an emergency escalation)
# Output: Node – wrapped LangGraph node
def _safe(name: str, fn: Node) -> Node:
    async def node(st: TicketState) -> dict[str, Any]:
        if st.error and name != "escalate":
            return {}
        try:
            return await fn(st)
        except Exception as e:
            log.exception("node '%s' failed (trace %s)", name, st.trace_id)
            if name == "escalate":
                return route.emergency_escalation(st, type(e).__name__)
            return {"error": f"{type(e).__name__}: {str(e)[:300]}", "failed_node": name}
    return node


class TriagePipeline:
    # Input:  settings (Settings); thresholds (ThresholdArtifact) – frozen calibration; kb (KnowledgeBase); llm (LLMClient);
    #         nli (callable); scrubber (Scrubber); store (AuditStore | None); mode (str | None) – overrides ROUTING_MODE
    # What:   Compiles the state machine once and processes tickets end to end
    # Output: TriagePipeline
    def __init__(self, *, settings: Settings, thresholds: ThresholdArtifact, kb: Any, llm: Any, nli: Any, scrubber: Any,
                 store: Any = None, mode: str | None = None):
        self.s, self.t, self.kb, self.llm, self.nli, self.scrubber, self.store = settings, thresholds, kb, llm, nli, scrubber, store
        self.mode = mode or settings.routing_mode
        self.graph = self._build()

    def _build(self) -> Any:
        # Input:  none (uses the injected components)
        # What:   INGEST -> CLASSIFY -> ROUTE-1 -> RETRIEVE -> ROUTE-2 -> GENERATE -> VALIDATE, with conditional exits to ESCALATE;
        #         VALIDATE is compiled in as the only path to RELEASE, so no flag or environment variable can bypass it
        # Output: compiled LangGraph runnable
        g = StateGraph(TicketState)
        for name, fn in {"ingest": self._ingest, "classify": self._classify, "route_stage1": self._stage1, "retrieve": self._retrieve,
                         "route_stage2": self._stage2, "generate": self._generate, "validate": self._validate,
                         "escalate": self._escalate, "release": self._release}.items():
            g.add_node(name, _safe(name, fn))
        nxt = lambda target: (lambda s: "escalate" if s.error or s.pending_action else target)
        g.add_edge(START, "ingest")
        g.add_edge("ingest", "classify")
        g.add_edge("classify", "route_stage1")
        g.add_conditional_edges("route_stage1", lambda s: "escalate" if s.error or s.pending_reason in ("mandatory", "passthrough") else "retrieve",
                                {"escalate": "escalate", "retrieve": "retrieve"})
        g.add_edge("retrieve", "route_stage2")
        g.add_conditional_edges("route_stage2", nxt("generate"), {"escalate": "escalate", "generate": "generate"})
        g.add_conditional_edges("generate", nxt("validate"), {"escalate": "escalate", "validate": "validate"})
        g.add_conditional_edges("validate", nxt("release"), {"escalate": "escalate", "release": "release"})
        g.add_edge("escalate", END)
        g.add_edge("release", END)
        return g.compile()

    async def _ingest(self, st: TicketState) -> dict[str, Any]:
        return ingest.normalize_and_sanitize_ticket(st.raw_payload, scrubber=self.scrubber, salt=self.s.customer_id_salt.get_secret_value()) | {
            "calibration_version": self.t.calibration_version, "prompt_bundle_version": prompt_bundle_version()}

    async def _classify(self, st: TicketState) -> dict[str, Any]:
        if self.mode == "PASSTHROUGH_QUEUE":  # incident mode: no model is consulted at all
            return {}
        return await classify.classify_ticket(st, llm=self.llm, platt_params=self.t.platt_scaling, samples=self.s.classify_samples)

    async def _stage1(self, st: TicketState) -> dict[str, Any]:
        return route.stage1(st, self.t, self.mode)

    async def _retrieve(self, st: TicketState) -> dict[str, Any]:
        return await retrieve.retrieve_ticket(st, kb=self.kb, llm=self.llm, tau_ret=self.t.routing_thresholds.retrieval_relevance_min)

    async def _stage2(self, st: TicketState) -> dict[str, Any]:
        return route.stage2(st)

    async def _generate(self, st: TicketState) -> dict[str, Any]:
        return await generate.generate_draft(st, llm=self.llm, kb=self.kb)

    async def _validate(self, st: TicketState) -> dict[str, Any]:
        # What: runs the four blocking checks; a draft blocked for private data is redacted before it can reach the audit log
        r = await asyncio.to_thread(guardrails.validate, st, nli=self.nli, kb=self.kb, thresholds=self.t.routing_thresholds, scrubber=self.scrubber)
        upd: dict[str, Any] = {"validation_receipt": r}
        if r["pii_found"]:
            red = lambda s: self.scrubber.scrub(s)[0]
            r["unsupported_sentences"] = [red(s) for s in r["unsupported_sentences"]]
            upd |= {"final_formatted_response": red(st.final_formatted_response), "llm_raw_draft_body": red(st.llm_raw_draft_body), "ticket_ui_assist_payload": {}}
        return upd | ({} if r["passed"] else {"pending_action": "BLOCK_SAFETY_GUARDRAIL_FAIL", "pending_reason": "guardrail"})

    async def _escalate(self, st: TicketState) -> dict[str, Any]:
        return route.escalate(st, self.t)

    async def _release(self, st: TicketState) -> dict[str, Any]:
        return route.release(st, self.t, self.mode)

    async def warm_up(self) -> None:
        # Input:  none
        # What:   Loads the lazily initialised local models (Presidio/spaCy, embedder, cross-encoder, NLI) before the first real
        #         ticket, so no customer pays the one-off start-up cost (about 3.5 s for spaCy alone)
        # Output: None
        self.scrubber.scrub("warm-up from ops@example.com")
        await asyncio.to_thread(self.kb.search, "warm up the knowledge base index", None)
        await asyncio.to_thread(self.nli, [("The service is ready.", "The service is ready.")])

    async def process(self, payload: Any) -> TicketState:
        # Input:  payload (dict | Any) – one raw ticket
        # What:   Runs the state machine, times it, guarantees a terminal routing decision even if the graph itself crashes,
        #         then appends the decision to the hash-chained audit log
        # Output: TicketState – final state (decision_id set when persisted)
        t0 = time.perf_counter()
        try:
            st = TicketState.model_validate(await self.graph.ainvoke(TicketState(raw_payload=payload if isinstance(payload, dict) else {})))
        except Exception as e:
            log.exception("pipeline crashed; recording an emergency escalation")
            ref = str(payload.get("external_ticket_ref") or payload.get("ticket_id") or "")[:128] if isinstance(payload, dict) else ""
            st = TicketState(trace_id=f"trc-{uuid.uuid4().hex}", ticket_id=str(uuid.uuid4()), sanitized_body="[UNPROCESSED]",
                             external_ticket_ref=ref or f"AUTO-{uuid.uuid4().hex[:16]}", calibration_version=self.t.calibration_version,
                             prompt_bundle_version=prompt_bundle_version(), error=f"{type(e).__name__}: {str(e)[:300]}", failed_node="graph")
        if not st.routing_action:
            st = st.model_copy(update=route.emergency_escalation(st, st.error or "no terminal decision"))
        st.end_to_end_latency_ms = round((time.perf_counter() - t0) * 1000)
        if self.store:
            try:
                st.decision_id = await self.store.record(st)
            except Exception:
                log.exception("audit write failed for trace %s", st.trace_id)
        return st
