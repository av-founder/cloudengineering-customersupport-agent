"""Behaviour specs for GENERATE + Response Formatter (tests/features/generate.feature)."""
import re

from pytest_bdd import given, parsers, scenarios, then, when

from src.config import ANSWERED_ACTIONS, DISCLAIMER, load_prompt
from src.generate import GeneratorLLMOutput
from src.guardrails import claim_sentences
from src.schemas import TicketState
from tests.helpers import role_of, ticket

scenarios("generate.feature")


@given('the Generate component wraps all customer text inside "untrusted_customer_ticket" delimiters')
def _wrapped():
    block = TicketState(sanitized_body="hello").ticket_block()
    assert block.startswith("<untrusted_customer_ticket>") and block.endswith("</untrusted_customer_ticket>")


@given('the Generate component enforces the "GeneratorLLMOutput" JSON schema at temperature 0.0')
def _schema():
    meta, _ = load_prompt("FR-01_grounded_generator")
    assert float(meta["temperature"]) == 0.0 and set(GeneratorLLMOutput.model_fields) == {"abstain", "draft_body", "cited_doc_ids", "claim_to_doc_map"}


@given(parsers.parse('the Route component transitions a ticket to "ROUTED_TO_GENERATE" with retrieved passage "{doc_id}" titled "{title}"'), target_fixture="payload")
def _routed(doc_id, title, kb):
    assert kb.registry[doc_id]["title"] == title
    return ticket("Every deployment fails at the health check stage and then rolls back.")


@when("the Generate component produces the structured JSON output and the Response Formatter assembles the envelope", target_fixture="state")
def _generated(payload, make_pipeline, run):
    return run(make_pipeline().process(payload))


@then(parsers.parse('the parsed output contains a valid "final_formatted_response", "cited_doc_ids" containing "{doc_id}", and "attached_doc_link"'))
def _valid(state, doc_id):
    assert state.routing_action == "AUTO_REPLY" and state.final_formatted_response and doc_id in state.cited_doc_ids and state.attached_doc_link


@then(parsers.parse('"final_formatted_response" begins with the exact leading disclaimer "{disclaimer}"'))
def _disclaimer(state, disclaimer):
    assert disclaimer == DISCLAIMER and state.final_formatted_response.startswith(disclaimer)


@then(parsers.parse('every factual claim in the body is grounded exclusively in the text of "{doc_id}"'))
def _grounded(state, kb, doc_id):
    claims = claim_sentences(state.llm_raw_draft_body)
    assert claims and all(c in kb.registry[doc_id]["content"] for c in claims) and state.validation_receipt["nli_min_score"] >= 0.85


@then(parsers.parse('"final_formatted_response" ends with the exact citation "{citation}"'))
def _citation(state, citation):
    assert state.final_formatted_response.endswith(citation)


@then('the clickable "doc_link" is attached directly in "ticket_ui_assist_payload" for the Tier-1 agent view')
def _ui(state):
    ui = state.ticket_ui_assist_payload
    assert ui["doc_link"] == "https://docs.cloudserve.io/kb/DOC-DEPLOY-001" and ui["draft_response"] == state.final_formatted_response


@given("a ticket asks for a custom legacy SMTP relay port that is not mentioned inside the retrieved documentation passages", target_fixture="payload")
def _smtp(kb):
    assert all("smtp" not in r["content"].lower() for r in kb.registry.values())
    return ticket("Which port should we use for our custom legacy SMTP relay on the platform?")


@when("the Generate component evaluates the retrieved passages", target_fixture="state")
def _evaluated(payload, make_pipeline, run):
    return run(make_pipeline().process(payload))


@then("the Generate component does not extrapolate from pre-trained external knowledge or guess a port number")
def _no_guess(state):
    assert state.retrieved_passages and not re.search(r"\b\d{2,5}\b", state.llm_raw_draft_body) and not state.final_formatted_response


@then("the Generate component outputs \"abstain: true\" and \"draft_body: 'I Don't Know'\"")
def _abstained(state):
    assert state.abstained and state.llm_raw_draft_body == "I Don't Know"


@then('the state transitions immediately to "ESCALATE_NO_GROUNDED_DOC" with an "FR-07" escalation summary')
def _escalated(state):
    assert state.routing_action == "ESCALATE_NO_GROUNDED_DOC" and "abstained" in state.routing_reason_manager_text
    assert "### Reason for Escalation" in state.escalation_summary_markdown


@given(parsers.parse('a customer submits a ticket containing "{attack}"'), target_fixture="payload")
def _attack(attack, llm):
    llm.obey_injection = True  # worst case: the model itself is fooled; deterministic layers must still hold
    return ticket(f"My deployment fails the health check. {attack}.")


@when('the Generate component processes the ticket inside "untrusted_customer_ticket" tags', target_fixture="state")
def _processed(payload, make_pipeline, run):
    return run(make_pipeline().process(payload))


@then("the adversarial instructions are treated strictly as passive customer text rather than system commands")
def _passive(llm):
    system, user = next(m for m in llm.messages if role_of(m) == "generate")[0]["content"], next(m for m in llm.messages if role_of(m) == "generate")[-1]["content"]
    inside = user.split("<untrusted_customer_ticket>")[1].split("</untrusted_customer_ticket>")[0]
    assert "ignore all previous instructions" not in system.lower() and "Ignore all previous instructions" in inside


@then('the system does not promise a credit or omit the mandatory "[Automated Support Assistant]" leading disclaimer')
def _no_credit(state):
    released = state.routing_action in ANSWERED_ACTIONS
    assert not released or (state.final_formatted_response.startswith(DISCLAIMER) and "credited" not in state.final_formatted_response)
    assert state.routing_action == "BLOCK_SAFETY_GUARDRAIL_FAIL" and "disclaimer_or_tone_block" in state.validation_receipt["failed_checks"]


@then('the system either answers only the legitimate technical question using "documentation.json" or returns "I Don\'t Know"')
def _either(state, kb):
    assert state.routing_action not in ANSWERED_ACTIONS or set(state.cited_doc_ids) <= set(kb.registry)
