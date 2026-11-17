"""Behaviour specs for ROUTE (tests/features/route.feature)."""
import inspect
import uuid

import pytest
from pydantic import ValidationError
from pytest_bdd import given, parsers, scenarios, then, when

from src import route
from src.schemas import TicketState
from tests.helpers import ticket

scenarios("route.feature")

SENSITIVE_TEXT = {"security_incident": "unauthorized SSH login attempts on our account", "compliance_request": "our auditor needs access records",
                  "billing_query": "a question about our invoice", "feature_request": "is a staged rollout feature on the roadmap",
                  "unclear_request": "it is not working, please call us"}


# Input:  kw – TicketState overrides
# What:   A classified deployment ticket snapshot
# Output: TicketState
def _state(**kw) -> TicketState:
    base = {"ticket_id": str(uuid.uuid4()), "trace_id": "trc-test", "external_ticket_ref": "T-ROUTE", "sanitized_body": "Deploy fails at the health check",
            "predicted_intent": "deployment_failure", "predicted_urgency": "high", "calibration_version": "cal_test"}
    return TicketState(**(base | kw))


# Input:  doc_id (str); score (float); kb
# What:   A retrieved passage dict for an official article
# Output: dict
def _passage(doc_id, score, kb) -> dict:
    r = kb.registry[doc_id]
    return {"doc_id": doc_id, "chunk_id": f"{doc_id}::01", "article_title": r["title"], "section_heading": "Symptoms", "doc_link": r["doc_link"],
            "chunk_text": "...", "rerank_score": score}


# Input:  st (TicketState); thresholds
# What:   Stage 1 then Stage 2 of the router (pure functions)
# Output: TicketState
def _gates(st, thresholds) -> TicketState:
    st = st.model_copy(update=route.stage1(st, thresholds, "FULL_AUTONOMOUS"))
    return st.model_copy(update=route.stage2(st))


@given("the Route component operates as a pure Python deterministic function with zero generative randomness")
def _pure():
    code = inspect.getsource(route)
    assert not any(s in code for s in ("import random", "from src.llm", "complete_json", "time."))


@given(parsers.parse('the routing thresholds are loaded as static runtime constants from "{path}"'))
def _static(thresholds):
    with pytest.raises(ValidationError):
        thresholds.routing_thresholds.classification_confidence_min = 0.1


@given(parsers.parse('a "TicketState" classified with intent "{intent}" at "calibrated_confidence" {conf:f} (above 0.82)'), target_fixture="state")
def _classified(intent, conf):
    return _state(predicted_intent=intent, calibrated_confidence=conf)


@given(parsers.parse('"retrieved_passages" contains official article "{doc_id}" with "rerank_score" {score:f} (above 0.75)'), target_fixture="state")
def _retrieved(state, doc_id, score, kb):
    p = _passage(doc_id, score, kb)
    return state.model_copy(update={"retrieved_passages": [p], "attempted_sources": [p], "semantic_matched_title": p["article_title"],
                                    "top_retrieval_score": score, "retrieval_executed": True})


@when("the Route component evaluates the post-retrieval state", target_fixture="state")
def _post_retrieval(state, thresholds):
    return _gates(state, thresholds)


@then('the state transitions to "ROUTED_TO_GENERATE"')
def _to_generate(state):
    assert state.pending_action is None and state.routing_action is None


@then('"routing_reason_manager_text" records a plain-English explanation readable by a support manager stating both thresholds were satisfied')
def _both_thresholds(state, thresholds):
    text = route.manager_reason("AUTO_REPLY", None, state, thresholds)
    assert "0.91 >= 0.82" in text and "0.88 >= 0.75" in text and "DOC-DEPLOY-001" in text


@given('a fixed "TicketState" snapshot with identical classification confidence, intent, customer tier, and retrieval scores', target_fixture="state")
def _snapshot(kb):
    p = _passage("DOC-DEPLOY-001", 0.88, kb)
    return _state(calibrated_confidence=0.91, customer_tier="enterprise", retrieved_passages=[p], attempted_sources=[p],
                  semantic_matched_title=p["article_title"], top_retrieval_score=0.88)


@when('the Route component evaluates the exact same "TicketState" across 100 independent invocations', target_fixture="outcomes")
def _hundred(state, thresholds):
    def once() -> tuple:
        st = _gates(state, thresholds)
        st = st.model_copy(update=route.release(st, thresholds, "FULL_AUTONOMOUS"))
        return st.routing_action, st.priority_score, st.routing_reason_manager_text
    return {once() for _ in range(100)}


@then('all 100 invocations yield the exact same "routing_action", "priority_score", and "routing_reason_manager_text"')
def _identical(outcomes):
    assert len(outcomes) == 1 and next(iter(outcomes))[:2] == ("AUTO_REPLY", 95)


@given(parsers.parse('a "TicketState" is classified with sensitive intent "{intent}" at any confidence level'), target_fixture="payload")
def _sensitive(intent):
    return ticket(SENSITIVE_TEXT[intent])


@when("the Route component evaluates the Stage 1 pre-retrieval gate", target_fixture="state")
def _stage1_gate(payload, make_pipeline, run):
    return run(make_pipeline().process(payload))


@then('"routing_action" is immediately set to "ESCALATE_MANDATORY_INTENT"')
def _mandatory(state):
    assert state.routing_action == "ESCALATE_MANDATORY_INTENT" and not state.retrieval_executed


@then("the Generate component is strictly bypassed")
def _bypassed(llm, state):
    assert set(llm.roles()) == {"classify"} and not state.final_formatted_response


@then('"escalation_summary_markdown" is populated with the ticket context and policy bypass reason')
def _summary(state):
    md = state.escalation_summary_markdown
    assert "### Ticket Context" in md and "policy bypass" in md and "### Reason for Escalation" in md


@then(parsers.parse('"routing_reason_manager_text" records "{reason}"'))
def _exact_reason(state, reason):
    assert state.routing_reason_manager_text == reason


@given(parsers.parse('a "TicketState" has "calibrated_confidence" of {conf:f} and top retrieval score of {ret:f}'), target_fixture="state")
def _scores(conf, ret, kb, thresholds):
    p, tau = _passage("DOC-DEPLOY-001", ret, kb), thresholds.routing_thresholds.retrieval_relevance_min
    return _state(calibrated_confidence=conf, retrieved_passages=[p] if ret >= tau else [], attempted_sources=[p], top_retrieval_score=ret,
                  highest_rejected_retrieval_score=0.0 if ret >= tau else ret, semantic_matched_title=p["article_title"], retrieval_executed=True)


@when("the Route component evaluates the state", target_fixture="state")
def _evaluate(state, thresholds):
    st = _gates(state, thresholds)
    return st.model_copy(update=route.escalate(st, thresholds)) if st.pending_action else st


@then(parsers.parse('"routing_action" is set to "{action}"'))
def _action(state, action):
    assert state.routing_action == action


@then('"escalation_summary_markdown" contains a structured Markdown summary with Ticket Context, Attempted Docs, and Failure Reason')
def _structured(state):
    md = state.escalation_summary_markdown
    assert all(h in md for h in ("### Ticket Context", "### Attempted Docs", "### Reason for Escalation")) and "DOC-DEPLOY-001" in md


@then('"routing_reason_manager_text" records why the threshold was not met in manager-readable language')
def _why(state):
    t = state.routing_reason_manager_text
    assert ("0.74" in t and "0.82" in t) if state.routing_action == "ESCALATE_LOW_CONFIDENCE" else ("0.75" in t and "0.61" in t)


@given("two tickets arrive simultaneously in the processing queue", target_fixture="queue")
def _two():
    return {}


@given(parsers.parse('Ticket {name} has "customer_tier" set to "{tier}" and "predicted_urgency" set to "{urgency}"'))
def _ticket_ab(queue, name, tier, urgency):
    queue[name] = (tier, urgency)


@when('the Route component computes queue priority via "FR-08" rules', target_fixture="ranked")
def _rank(queue):
    pr = {k: route.priority(*v) for k, v in queue.items()}
    pos = route.queue_order([(pr[k][1], "2026-11-16T09:00:00Z") for k in queue])
    return {k: (*pr[k], p) for k, p in zip(queue, pos)}


@then('Ticket B is assigned "priority_level: critical", "priority_score: 95", and "queue_position: 1" ahead of Ticket A ("priority_score: 25")')
def _b_first(ranked):
    assert ranked["B"] == ("critical", 95, 1) and ranked["A"] == ("low", 25, 2)
