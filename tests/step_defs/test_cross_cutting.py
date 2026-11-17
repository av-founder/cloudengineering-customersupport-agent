"""Behaviour specs for cross-cutting governance (tests/features/cross_cutting.feature)."""
import json
import sqlite3
import time
from types import SimpleNamespace

import groq
import httpx
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from evaluation.harness import parse_args, run as harness_run
from scripts.verify_kb_manifest import main as verify_main
from src.api import create_app
from src.config import ROOT, get_settings
from src.llm import LLMClient, _Provider
from tests.helpers import FakeNLI, chat_response, respond, ticket

scenarios("cross_cutting.feature")

LABELS = lambda intent, route, docs, must_not=False: {"intent": intent, "expected_route": route, "answerable_from_docs": bool(docs),
                                                      "expected_doc_ids": docs, "must_not_auto_respond": must_not}


@given('an input file of N support tickets is supplied via "python -m evaluation.harness --input input_path --output output_path"', target_fixture="paths")
def _paths(tmp_path):
    return {"input": tmp_path / "tickets.json", "output": tmp_path / "out"}


@given(parsers.parse('static routing thresholds are loaded from "{path}"'))
def _static(artifact_path, paths):
    paths["thresholds"] = artifact_path


@given("the input batch includes normal answerable tickets, mandatory escalation tickets, malformed empty tickets, and tickets containing API keys")
def _batch(paths):
    batch = [ticket("Every deployment fails at the health check stage.", tid="H-1", tier="enterprise", labels=LABELS("deployment_failure", "auto_respond", ["DOC-DEPLOY-001"])),
             ticket("how to get all data? page size big is reject.", tid="H-2", fluency="non_fluent", labels=LABELS("api_usage_question", "auto_respond", ["DOC-API-002"])),
             ticket("We saw unauthorized SSH logins from an unknown IP.", tid="H-3", labels=LABELS("security_incident", "escalate", ["DOC-SEC-001"], True)),
             {"ticket_id": "H-4", "channel": "chat"},
             ticket("Our key cs_live_98a7bc65d4e3f2a1b0 fails the health check deploy.", tid="H-5", channel="forum"),
             None]
    paths["input"].write_text(json.dumps(batch), encoding="utf-8")
    paths["n"] = len(batch)


@when("the evaluation harness runs to completion without human intervention, manual retries, or restarts", target_fixture="result")
def _harness(paths, kb, llm, store, run):
    args = parse_args(["--input", str(paths["input"]), "--output", str(paths["output"]), "--thresholds-config", str(paths["thresholds"]), "--concurrency", "3"])
    code = run(harness_run(args, components={"kb": kb, "llm": llm, "nli": FakeNLI(), "store": store}))
    return {"code": code, "metrics": json.loads((paths["output"] / "metrics_report.json").read_text(encoding="utf-8"))}


@then('every single ticket in the input file is processed with zero silently dropped records ("tickets_processed == answered_automatically + escalated")')
def _no_drops(result, paths):
    v = result["metrics"]["volume"]
    assert result["code"] == 0 and v["tickets_processed"] == paths["n"] == v["answered_automatically"] + v["escalated"]
    preds = [json.loads(x) for x in (paths["output"] / "predictions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(preds) == paths["n"] and all("labels" not in p for p in preds) and {p["ticket_id"] for p in preds} >= {"H-1", "H-4", "INPUT-00006"}


@then('every ticket produces a corresponding immutable entry in "decision_audit_log" ("decisions_logged == tickets_processed")')
def _all_logged(result, paths):
    g = result["metrics"]["governance"]
    rows = (paths["output"] / "decision_audit_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert g["decisions_logged"] == paths["n"] == len(rows) and g["decision_log_coverage_rate"] == 1.0


@then('the harness programmatically writes "metrics_report.json" and "metrics_report.md" containing Volume, Business, Technical, and Governance figures')
def _reports(result, paths):
    m, md = result["metrics"], (paths["output"] / "metrics_report.md").read_text(encoding="utf-8")
    assert {"volume", "business", "technical", "governance"} <= set(m) and all(f"## {h}" in md for h in ("Volume", "Business", "Technical", "Governance"))
    pdd = m["governance"]["private_data_detections"]
    assert pdd["ingest_redactions_total"] >= 1 and pdd["outbound_leaked_occurrences"] == 0 and m["business"]["first_contact_resolution_rate"] > 0
    assert m["safety"]["must_not_auto_respond_violations"] == 0 and m["technical"]["latency_p95_ms"] >= 0


@given('a ticket has completed processing through the LangGraph pipeline and written a record to "decision_audit_log"', target_fixture="client")
def _processed(make_pipeline, store, ctx, request):
    client = TestClient(create_app(pipeline=make_pipeline(), store=store, instrument=False))
    client.__enter__()
    request.addfinalizer(lambda: client.__exit__(None, None, None))
    r = client.post("/api/v1/tickets/ingest", json={"external_ticket_ref": "AUD-1", "channel": "live_chat", "customer_id": "C-9", "customer_tier": "enterprise",
                                                    "subject": "Deploy broken", "body": "Every deployment fails at the health check stage."})
    assert r.status_code == 200
    ctx["resp"] = r.json()
    return client


@when('a compliance auditor queries "/api/v1/governance/audit/{ticket_id}"', target_fixture="audit")
def _query(client, ctx):
    r = client.get(f"/api/v1/governance/audit/{ctx['resp']['ticket_id']}")
    assert r.status_code == 200
    return r.json()


@then("the returned record contains the sanitized input, predicted intent, calibrated confidence, considered alternatives, retrieved sources, routing action, plain-English manager reason, and validation receipt")
def _reconstructable(audit):
    rec = audit["audit_record"]
    assert audit["hash_chain_verified"] is True and rec["routing_action"] == "AUTO_REPLY" and rec["retrieved_sources"]
    assert {"sanitized_subject", "sanitized_body", "predicted_intent", "calibrated_confidence", "considered_alternatives", "retrieved_sources",
            "routing_action", "routing_reason", "validation_receipt_json", "calibration_version", "prompt_bundle_version", "record_hash"} <= set(rec)


@then('when a database user attempts an "UPDATE" or "DELETE" statement on "decision_audit_log"', target_fixture="errors")
def _mutate(db_path):
    errors = []
    with sqlite3.connect(db_path) as con:
        for sql in ("UPDATE decision_audit_log SET routing_reason = 'tampered'", "DELETE FROM decision_audit_log"):
            try:
                con.execute(sql)
                errors.append(None)
            except sqlite3.DatabaseError as e:
                errors.append(str(e))
    return errors


@then(parsers.parse('the database trigger "{trigger}" aborts the transaction and raises a security violation exception'))
def _aborted(errors, db_path, trigger):
    assert errors and all(e and "SECURITY VIOLATION" in e for e in errors)
    with sqlite3.connect(db_path) as con:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert len(names) == 2 and all(n.startswith(trigger) for n in names)
        assert con.execute("SELECT COUNT(*) FROM decision_audit_log").fetchone()[0] == 1


@given('an Enterprise deployment failure ticket arrives during a primary provider ("Groq") HTTP 503 outage', target_fixture="setup")
def _outage():
    req, calls = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"), {"groq": 0, "openrouter": 0}

    async def groq_create(**kw):
        calls["groq"] += 1
        raise groq.InternalServerError("Service Unavailable", response=httpx.Response(503, request=req), body=None)

    async def openrouter_create(**kw):
        calls["openrouter"] += 1
        return chat_response(json.dumps(respond(kw["messages"])))

    client = LLMClient(get_settings().model_copy(update={"groq_api_key": "", "openrouter_api_key": "", "llm_cache_enabled": False}))
    wrap = lambda fn: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fn)))
    client.providers = [_Provider("groq", wrap(groq_create), "llama-3.3-70b-versatile"), _Provider("openrouter", wrap(openrouter_create), "meta-llama/llama-3.3-70b-instruct")]
    return {"client": client, "calls": calls, "payload": ticket("Every deployment fails at the health check stage.", tier="enterprise")}


@when("the Classify or Generate component invokes the model client", target_fixture="outcome")
def _invoke(setup, make_pipeline, run):
    msgs = [{"role": "system", "content": "Triage Assistant"}, {"role": "user", "content": "<untrusted_customer_ticket>deploy fails the health check</untrusted_customer_ticket>"}]
    t0 = time.perf_counter()
    first = run(setup["client"].complete_json(msgs, temperature=0.15, max_tokens=50))
    out = {"first": first, "first_s": time.perf_counter() - t0, "groq_attempts_first_call": setup["calls"]["groq"]}
    return out | {"state": run(make_pipeline(llm_client=setup["client"]).process(setup["payload"]))}


@then('the Tenacity circuit breaker trips after 2 retries within 800ms and routes the request to the fallback provider ("OpenRouter")')
def _tripped(outcome, setup):
    assert outcome["groq_attempts_first_call"] == 3 and outcome["first"].provider == "openrouter" and outcome["first_s"] < 0.8
    assert setup["client"].providers[0].open_until > 0  # circuit open after repeated failures


@then("the ticket completes processing within the 3.0-second p95 latency target without dropping the ticket")
def _completed(outcome):
    st = outcome["state"]
    assert st.routing_action == "AUTO_REPLY" and st.end_to_end_latency_ms < 3000 and st.model_provider == "openrouter" and st.decision_id


@given(parsers.parse('an engineer attempts to index records from "{tickets_file}" or a private agent ".txt" snippet file into ChromaDB'), target_fixture="candidates")
def _contaminated(tickets_file, tmp_path):
    snippet = tmp_path / "sofia_snippets.txt"
    snippet.write_text("Unofficial fix: restart the router twice.", encoding="utf-8")
    tampered = tmp_path / "documentation.json"
    docs = json.loads((ROOT / "data" / "documentation.json").read_text(encoding="utf-8")) + [{"doc_id": "DOC-SNIP-001", "title": "Agent snippet", "content": "Restart twice."}]
    tampered.write_text(json.dumps(docs), encoding="utf-8")
    return [ROOT / "data" / tickets_file, snippet, tampered]


@when('the GitHub Actions CI pipeline runs the "kb_manifest.lock" SHA-256 verification check', target_fixture="outcomes")
def _ci_gate(candidates, capsys):
    codes = [verify_main(["--kb-path", str(p)]) for p in candidates]
    return {"codes": codes, "stderr": capsys.readouterr().err, "clean": verify_main([])}


@then(parsers.parse('the ingestion pipeline halts with a fatal "{code}" error before the container image can be built'))
def _halted(outcomes, code):
    assert outcomes["codes"] == [1, 1, 1] and outcomes["stderr"].count(code) == 3 and outcomes["clean"] == 0
