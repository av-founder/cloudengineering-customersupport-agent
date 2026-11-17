"""Behaviour specs for CLASSIFY (tests/features/classify.feature)."""
import numpy as np
from pytest_bdd import given, parsers, scenarios, then, when

from evaluation.calibrate import apply_platt, ece, fit_platt
from src.classify import _system_prompt, classify_ticket
from src.guardrails import SCRUBBER
from src.ingest import normalize_and_sanitize_ticket
from src.schemas import TicketState
from tests.helpers import ticket

scenarios("classify.feature")


# Input:  text (str); llm (FakeLLM); thresholds (ThresholdArtifact); run (loop runner); fluency (str)
# What:   INGEST + CLASSIFY one ticket with the calibration parameters from the artifact
# Output: TicketState
def _classify(text, llm, thresholds, run, fluency="fluent") -> TicketState:
    raw = ticket(text, fluency=fluency)
    st = TicketState(raw_payload=raw, **normalize_and_sanitize_ticket(raw, scrubber=SCRUBBER))
    return st.model_copy(update=run(classify_ticket(st, llm=llm, platt_params=thresholds.platt_scaling, samples=3)))


@given(parsers.parse('the Classify component is initialized with representative few-shot examples and temperature scaling calibration loaded from "{path}"'))
def _initialised(thresholds):
    prompt, meta = _system_prompt()
    assert meta["id"] == "FR-04" and prompt.count("Ticket:") >= 22 and "$" not in prompt.split("Examples:")[0]
    assert thresholds.platt_scaling.temperature_T > 0


@given('a normalized "TicketState" describing a container deployment failing with a 502 Bad Gateway error', target_fixture="text")
def _deploy_502():
    return "Our container deployment fails with a 502 Bad Gateway error after every release."


@when('the Classify component transitions state from "INGESTED_NORMALIZED" to "CLASSIFIED"', target_fixture="state")
def _classified(text, llm, thresholds, run):
    return _classify(text, llm, thresholds, run)


@then('"predicted_intent" is set to "deployment_failure" and "predicted_urgency" is set to "high"')
def _intent_urgency(state):
    assert (state.predicted_intent, state.predicted_urgency) == ("deployment_failure", "high")


@then('"calibrated_confidence" is populated with a numeric score between 0.0000 and 1.0000 within 5 percentage points of empirical accuracy')
def _calibrated(state):
    assert 0.0 <= state.calibrated_confidence <= 1.0
    rng = np.random.default_rng(7)  # the calibration method itself must reach ECE <= 0.05 on held-out data with a known reliability curve
    z = rng.normal(0.5, 2.0, 6000)
    y = (rng.random(6000) < 1 / (1 + np.exp(-(1.3 * z - 0.4)))).astype(int)
    assert ece(apply_platt(z[4000:], fit_platt(z[:4000], y[:4000])), y[4000:])[0] <= 0.05


@then('"considered_alternatives" records at least the top 3 alternative intent categories considered with their probabilities')
def _alternatives(state):
    alts = state.considered_alternatives
    assert len(alts) >= 3 and alts[0]["intent"] == state.predicted_intent and all(0 <= a["probability"] <= 1 for a in alts)


@given(parsers.parse('a normalized "TicketState" containing customer phrasing related to "{topic}"'), target_fixture="text")
def _topic(topic):
    return f"We need help with {topic}."


@when("the Classify component evaluates the ticket", target_fixture="state")
def _evaluated(text, llm, thresholds, run, ctx):
    return _classify(text, llm, thresholds, run, ctx.get("fluency", "fluent"))


@then(parsers.parse('"predicted_intent" is set to the exact escalation intent "{intent}"'))
def _escalation_intent(state, intent):
    assert state.predicted_intent == intent


@then('"is_mandatory_escalation" is set to true')
def _mandatory(state):
    assert state.is_mandatory_escalation and state.mandatory_category


@then('"considered_alternatives" is recorded in the decision state')
def _recorded(state):
    assert len(state.considered_alternatives) == 3


@given('a normalized "TicketState" containing ambiguous, contradictory, empty, or novel zero-day failure text', target_fixture="texts")
def _ambiguous():
    return ["Quantum flux capacitor desynchronised with the zero-day hyperlane", ""]


@when("the Classify component cannot map the ticket to a known intent category or outputs \"I Don't Know\"", target_fixture="states")
def _unmappable(texts, llm, thresholds, run):
    out = [_classify(t, llm, thresholds, run) for t in texts]
    llm.down = True  # provider outage is a third unclassifiable path
    return out + [_classify("Every deployment fails at the health check", llm, thresholds, run)]


@then("the Classify component does not raise a runtime exception")
def _no_exception(states):
    assert len(states) == 3


@then('"predicted_intent" is set to the defined fallback "unclassified_fallback"')
def _fallback(states):
    assert {s.predicted_intent for s in states} == {"unclassified_fallback"}


@then('"calibrated_confidence" is set to 0.0000 (below the automated routing threshold)')
def _zero(states, thresholds):
    assert all(s.calibrated_confidence == 0.0 < thresholds.routing_thresholds.classification_confidence_min for s in states)


@then('"considered_alternatives" and the ambiguity reason are preserved for the audit log')
def _reason(states):
    assert all(isinstance(s.considered_alternatives, list) and s.classification_note for s in states)


@given('a normalized "TicketState" written in broken English using non-technical synonyms for a build dependency failure', target_fixture="text")
def _broken_english():
    return "builds that work last week are now fail during dependency resolution. we are having not change our code at all."


@given('"language_fluency" is tagged as "non_fluent"')
def _non_fluent(ctx):
    ctx["fluency"] = "non_fluent"


@then('"predicted_intent" is accurately assigned to "deployment_failure"')
def _accurate(state):
    assert state.predicted_intent == "deployment_failure" and state.language_fluency == "non_fluent"


@then('"calibrated_confidence" exhibits less than a 0.05 variance compared to an equivalent "fluent" cohort ticket')
def _parity(state, llm, thresholds, run):
    fluent = _classify("Builds that worked last week now fail during dependency resolution. We have not changed our code.", llm, thresholds, run)
    assert abs(fluent.calibrated_confidence - state.calibrated_confidence) < 0.05
