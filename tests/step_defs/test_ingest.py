"""Behaviour specs for INGEST (tests/features/ingest.feature)."""
import json

from pytest_bdd import given, parsers, scenarios, then, when

from src.config import CHANNELS, TIERS
from src.guardrails import SCRUBBER
from src.ingest import normalize_and_sanitize_ticket
from src.schemas import TicketState
from tests.helpers import ticket

scenarios("ingest.feature")

CORE = "My deployment keeps failing the health check"
# Input: quirk description from the Examples table -> (raw channel text, fragments that must disappear)
QUIRKS = {
    "HTML wrapper tags, MIME headers, and reply-chain signatures": (
        "Content-Type: text/html; charset=utf-8\n<html><body><p>My deployment keeps failing the health check.</p></body></html>\n--\nJane Doe\n"
        "On Mon, 3 Nov 2026 Jane wrote:\n> earlier quoted message", ["<p>", "Content-Type", "Jane Doe", "earlier quoted message", "wrote:"]),
    "fragmented multi-message timestamps and typing indicators": (
        "[10:42] My deployment keeps failing\n[10:43 AM] the health check.\nAgent Smith is typing...", ["[10:42]", "10:43", "is typing"]),
    "inline markdown anchor metadata and page widget breadcrumbs": (
        "Docs > Deployments > Health checks\nMy deployment keeps failing the [health check](#health-checks).{#comment-17}\nWas this page helpful? Yes / No",
        ["Docs >", "{#comment-17}", "(#health-checks)", "Was this page helpful"]),
    "BBCode quote blocks and forum thread pagination metadata": (
        "[quote=bob]Same problem here since Monday[/quote]\n[b]My deployment keeps failing the health check.[/b]\nPage 2 of 5",
        ["[quote", "Same problem here", "[b]", "Page 2 of 5"]),
}
ANOMALIES = {
    "an empty body and a populated subject line": {"subject": "Deployment keeps failing", "body": ""},
    "a completely empty body and missing subject field": {"body": ""},
    "null bytes, zero-width joiners, and malformed UTF-8 sequences": {"body": "Deploy\x00 fails\u200d at the health\ud800 check"},
    "missing customer tier and missing urgency metadata fields": {"body": "Deployment keeps failing"},
}
SECRETS = ("cs_live_98a7bc65d4e3f2a1b0", "AKIAIOSFODNN7EXAMPLE", "jane.doe@example.org")


@given("the Ingest Service is online and accepting payloads across all four supported channels")
def _online():
    assert CHANNELS == ("email", "chat", "forum", "docs_comment")


@given("the Pre-Persistence PII and Secret Scrubber is active at the ingestion boundary")
def _scrubber_active():
    assert SCRUBBER.scrub("write to a.b@example.com")[1]["EMAIL_ADDRESS"] == 1


@given(parsers.parse('a customer submits a support request via the "{channel}" channel containing "{quirk}"'), target_fixture="raw")
def _raw(channel, quirk):
    return ticket(QUIRKS[quirk][0], channel=channel)


@when('the Ingest component transitions state from "RAW_INGEST" to "INGESTED_NORMALIZED"', target_fixture="state")
def _ingest(raw):
    return TicketState(raw_payload=raw, **normalize_and_sanitize_ticket(raw, scrubber=SCRUBBER))


@then('a single canonical "TicketState" representation is produced')
def _canonical(state):
    assert isinstance(state, TicketState) and state.ticket_id and state.trace_id.startswith("trc-")


@then('the canonical state preserves the exact original text in "raw_body_preserved"')
def _raw_kept(state, raw):
    assert state.raw_body_preserved.get_secret_value() == raw["body"]
    assert "raw_body_preserved" not in state.model_dump() and raw["body"] not in repr(state)


@then(parsers.parse('the canonical state explicitly records "{canonical}" in "source_channel"'))
def _channel(state, canonical):
    assert state.source_channel == canonical


@then(parsers.parse('"{quirk}" is stripped from "sanitized_body" passed to downstream components'))
def _stripped(state, quirk):
    assert CORE in state.sanitized_body
    assert [f for f in QUIRKS[quirk][1] if f in state.sanitized_body] == []


@given(parsers.parse('an incoming ticket arrives from "{channel}" with "{anomaly}"'), target_fixture="raw")
def _anomalous(channel, anomaly):
    raw = ticket("placeholder", channel=channel) | ANOMALIES[anomaly]
    for key in {"a completely empty body and missing subject field": "subject", "missing customer tier and missing urgency metadata fields": "customer_tier"}.get(anomaly, "").split():
        raw.pop(key)
    return raw


@when("the Ingest component processes the incoming payload", target_fixture="state")
def _process(raw, make_pipeline, run):
    return run(make_pipeline().process(raw))


@then("the system does not raise an unhandled exception or return an HTTP 5xx error")
def _no_crash(state):
    assert state.routing_action and state.error is None and state.decision_id


@then('a valid "TicketState" representation is created with deterministic default fallbacks')
def _defaults(state):
    assert state.sanitized_body and state.customer_tier in TIERS and TicketState.model_validate(state.model_dump())


@then(parsers.parse('"ingest_anomaly_flags" contains "{flag}" for downstream routing'))
def _flag(state, flag):
    assert flag in state.ingest_anomaly_flags


@given('an incoming ticket contains a live CloudServe key "cs_live_98a7bc65d4e3f2a1b0", an AWS key, and a personal email address', target_fixture="raw")
def _secret_ticket():
    return ticket("Our key cs_live_98a7bc65d4e3f2a1b0 and AWS key AKIAIOSFODNN7EXAMPLE leaked; reply to jane.doe@example.org. "
                  "Also every deployment fails the health check.")


@when("the Ingest component normalizes the ticket", target_fixture="state")
def _normalize(raw, make_pipeline, run):
    return run(make_pipeline().process(raw))


@then('"sanitized_body" has all secrets and private identifiers replaced with "<REDACTED_API_KEY>" and "<REDACTED_EMAIL>"')
def _redacted(state):
    assert state.sanitized_body.count("<REDACTED_API_KEY>") == 2 and "<REDACTED_EMAIL>" in state.sanitized_body
    assert not any(s in state.sanitized_body for s in SECRETS)


@then("no raw secret or personal identifier is transmitted to the Classify or Retrieve layers")
def _not_transmitted(llm, reranker):
    sent = json.dumps(llm.messages) + " ".join(reranker.queries)
    assert llm.messages and reranker.queries and not any(s in sent for s in SECRETS)


@then('"pii_redacted_at_ingest" is set to true with "pii_redaction_types" populated in the audit state')
def _audit_state(state, store, run):
    assert state.pii_redacted_at_ingest and {"CLOUDSERVE_API_KEY", "AWS_SECRET_KEY", "EMAIL_ADDRESS"} <= set(state.pii_redaction_types)
    rec = run(store.latest_decision(state.ticket_id))
    assert rec["pii_redacted_at_ingest"] and "CLOUDSERVE_API_KEY" in rec["pii_redaction_types"] and not any(s in json.dumps(rec) for s in SECRETS)
