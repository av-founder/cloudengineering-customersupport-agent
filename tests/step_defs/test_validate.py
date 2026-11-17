"""Behaviour specs for VALIDATE (tests/features/validate.feature)."""
from pytest_bdd import given, parsers, scenarios, then, when

from src.api import to_response
from src.config import ANSWERED_ACTIONS, DISCLAIMER, GUARDRAIL_CHECKS
from src.guardrails import SCRUBBER, validate
from tests.helpers import FakeNLI, ticket

scenarios("validate.feature")

DEPLOY_TICKET = "Every deployment fails at the health check stage."
GOOD_SENTENCE = "Confirm the container is listening on the port declared in the service definition."


@given("the Validate component is compiled as a non-bypassable synchronous node prior to response release")
def _non_bypassable(make_pipeline):
    edges = {(e.source, e.target) for e in make_pipeline(with_store=False).graph.get_graph().edges}
    assert ("validate", "release") in edges and {s for s, t in edges if t == "release"} == {"validate"}


@given("all four validation checks (Outbound PII, NLI Entailment, Citation Resolution, Disclaimer/Tone) are active")
def _four_checks():
    assert GUARDRAIL_CHECKS == ("outbound_pii_or_secret_block", "nli_hallucination_entailment_block", "citation_integrity_block", "disclaimer_or_tone_block")


@given('a candidate "final_formatted_response" begins with the mandatory "[Automated Support Assistant]" disclaimer', target_fixture="payload")
def _compliant():
    return ticket(DEPLOY_TICKET)


@given("the candidate response contains zero PII entities or credential patterns")
def _no_pii():
    assert not SCRUBBER.scrub(GOOD_SENTENCE)[1]


@given(parsers.parse('every claim sentence achieves an NLI entailment score >= 0.85 against retrieved passage "{doc_id}"'))
def _entailed(doc_id, kb):
    assert GOOD_SENTENCE in kb.registry[doc_id]["content"]


@given(parsers.parse('the trailing citation resolves to "{doc_id}" in "kb_articles_registry"'))
def _resolves(doc_id, kb):
    assert doc_id in kb.registry


@when("the Validate component inspects the candidate response", target_fixture="state")
def _inspected(payload, make_pipeline, run):
    return run(make_pipeline().process(payload))


@then('"validation_receipt.passed" is set to true')
def _passed(state):
    assert state.validation_receipt["passed"] is True and state.final_formatted_response.startswith(DISCLAIMER)


@then('the state transitions to "RELEASE_APPROVED_RESPONSE"')
def _released(state):
    assert state.routing_action == "AUTO_REPLY" and state.guardrail_passed


@then('"decision_audit_log" records the complete "validation_receipt" showing all four checks ran and passed')
def _receipt_logged(state, store, run):
    rec = run(store.latest_decision(state.ticket_id))
    r = rec["validation_receipt_json"]
    assert r["passed"] and r["checks_run"] == list(GUARDRAIL_CHECKS) and r["failed_checks"] == []
    assert rec["guardrail_passed"] and rec["citation_verified"] and rec["disclaimer_verified"] and not rec["pii_outbound_detected"]


@given("a candidate draft response inadvertently contains an internal IPv4 address, customer email, or API token string", target_fixture="payload")
def _leaky(llm):
    llm.generate_override = lambda user: {"abstain": False, "cited_doc_ids": ["DOC-DEPLOY-001"],
                                          "draft_body": f"{GOOD_SENTENCE} Contact ops at 10.20.30.40 or ops.lead@acme-corp.com with token cs_live_ABCDEFGHIJKLMNOPQRST."}
    return ticket(DEPLOY_TICKET)


@when("the Validate component executes Check 1 (Outbound PII and Secret Check)", target_fixture="state")
def _check1(payload, make_pipeline, run):
    return run(make_pipeline().process(payload))


@then('"validation_receipt.passed" is set to false and "validation_receipt.pii_found" is set to true')
def _pii_found(state):
    assert state.validation_receipt["passed"] is False and state.validation_receipt["pii_found"] is True


@then("the Validate component blocks the response from outbound release")
def _not_released(state):
    assert state.routing_action not in ANSWERED_ACTIONS and to_response(state).ticket_ui_assist_payload.draft_response is None


@then('"routing_action" transitions to "BLOCK_SAFETY_GUARDRAIL_FAIL" with an "FR-07" escalation summary for human review')
def _blocked_with_summary(state):
    assert state.routing_action == "BLOCK_SAFETY_GUARDRAIL_FAIL" and "### Reason for Escalation" in state.escalation_summary_markdown


@then('"decision_audit_log" records the blocked entity type and guardrail trigger reason')
def _pii_logged(state, store, run):
    rec = run(store.latest_decision(state.ticket_id))
    r = rec["validation_receipt_json"]
    assert rec["pii_outbound_detected"] and {"IPV4_ADDRESS", "EMAIL_ADDRESS", "CLOUDSERVE_API_KEY"} <= set(r["pii_entity_types"])
    assert r["blocked_reason"] == "outbound_pii_or_secret_block"
    stored = str(rec)  # the blocked draft is kept for review only in redacted form
    assert not any(s in stored for s in ("10.20.30.40", "ops.lead@acme-corp.com", "cs_live_ABCDEFGHIJKLMNOPQRST"))


@given(parsers.parse('a candidate draft response includes a CLI flag "{flag}" that does not appear in the retrieved "{doc_id}" passage'), target_fixture="payload")
def _hallucinated(flag, doc_id, llm, kb):
    assert flag not in kb.registry[doc_id]["content"]
    llm.generate_override = lambda user: {"abstain": False, "cited_doc_ids": [doc_id],
                                          "draft_body": f"{GOOD_SENTENCE}\nRun cloudserve deploy {flag} to reset the gateway quickly."}
    return ticket(DEPLOY_TICKET)


@when("the Validate component executes Check 2 (NLI Entailment Check) and scores the sentence at 0.31 (below 0.85)", target_fixture="state")
def _check2(payload, make_pipeline, run):
    return run(make_pipeline().process(payload))


@then('"validation_receipt.passed" is set to false and "validation_receipt.nli_min_score" is recorded as 0.31')
def _nli_score(state):
    assert state.validation_receipt["passed"] is False and state.validation_receipt["nli_min_score"] == 0.31


@then('the Validate component blocks the response from release and transitions to "BLOCK_SAFETY_GUARDRAIL_FAIL"')
def _nli_blocked(state):
    assert state.routing_action == "BLOCK_SAFETY_GUARDRAIL_FAIL" and "nli_hallucination_entailment_block" in state.validation_receipt["failed_checks"]


@then("the unsupported sentence is flagged in the escalation summary for the human agent")
def _flagged(state):
    md = state.escalation_summary_markdown
    assert "Unsupported sentences flagged" in md and "--force-reset-nat" in md and GOOD_SENTENCE not in md.split("flagged by the guardrail")[-1]


@given('a candidate draft response is missing the leading "[Automated Support Assistant]" disclaimer or cites a "doc_id" not in "retrieved_passages"', target_fixture="payload")
def _structural(llm):
    llm.generate_override = lambda user: {"abstain": False, "draft_body": GOOD_SENTENCE, "cited_doc_ids": ["DOC-BILL-001"]}
    return ticket(DEPLOY_TICKET)


@when("the Validate component executes Check 3 and Check 4", target_fixture="state")
def _check34(payload, make_pipeline, run, kb, thresholds, ctx):
    st = run(make_pipeline().process(payload))
    no_disclaimer = st.model_copy(update={"cited_doc_ids": ["DOC-DEPLOY-001"], "final_formatted_response": GOOD_SENTENCE + kb.citation("DOC-DEPLOY-001")})
    ctx["no_disclaimer"] = validate(no_disclaimer, nli=FakeNLI(), kb=kb, thresholds=thresholds.routing_thresholds, scrubber=SCRUBBER)
    return st


@then('"validation_receipt.passed" is set to false')
def _failed(state, ctx):
    assert state.validation_receipt["passed"] is False
    assert ctx["no_disclaimer"]["passed"] is False and ctx["no_disclaimer"]["failed_checks"] == ["disclaimer_or_tone_block"]


@then('the response is blocked and routed to "BLOCK_SAFETY_GUARDRAIL_FAIL"')
def _routed_block(state):
    assert state.routing_action == "BLOCK_SAFETY_GUARDRAIL_FAIL" and "citation_integrity_block" in state.validation_receipt["failed_checks"]


@then('the exact structural failure is persisted in "decision_audit_log"')
def _structural_logged(state, store, run):
    rec = run(store.latest_decision(state.ticket_id))
    assert rec["routing_action"] == "BLOCK_SAFETY_GUARDRAIL_FAIL" and not rec["citation_verified"] and rec["cited_doc_id"] == "DOC-BILL-001"
    assert "citation_integrity_block" in rec["validation_receipt_json"]["failed_checks"]
