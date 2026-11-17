"""Behaviour specs for RETRIEVE (tests/features/retrieve.feature)."""
import pytest
import sqlalchemy as sa
from pytest_bdd import given, parsers, scenarios, then, when

from src.config import KB_COLLECTION, ROOT, get_settings
from src.guardrails import SCRUBBER
from src.ingest import normalize_and_sanitize_ticket
from src.logging_store import kb_articles
from src.retrieve import KBContaminationError, _ntok, load_official_docs, retrieve_ticket
from src.schemas import TicketState
from tests.helpers import ticket

scenarios("retrieve.feature")


# Input:  text (str); llm; kb; tau (float); run; fluency (str)
# What:   INGEST + RETRIEVE one ticket with the artifact's relevance threshold
# Output: TicketState
def _retrieve(text, llm, kb, tau, run, fluency="fluent") -> TicketState:
    raw = ticket(text, fluency=fluency)
    st = TicketState(raw_payload=raw, **normalize_and_sanitize_ticket(raw, scrubber=SCRUBBER))
    return st.model_copy(update=run(retrieve_ticket(st, kb=kb, llm=llm, tau_ret=tau)))


# Input:  store (AuditStore)
# What:   Reads the kb_articles_registry table
# Output: dict[str, dict]
async def _registry(store) -> dict:
    async with store.engine.connect() as c:
        return {r.doc_id: r._asdict() for r in await c.execute(sa.select(kb_articles))}


@given('ChromaDB collection "cloudserve_official_kb_v1" is indexed exclusively with the 29 official articles from "documentation.json" using 512-token heading-aware chunks')
def _indexed(kb):
    got = kb.col.get(include=["metadatas"])
    assert kb.col.name.startswith(KB_COLLECTION) and len(kb.registry) == 29 and len(got["ids"]) == len(kb.chunks)
    assert all(_ntok(c["text"]) <= 512 and c["chunk_text"].startswith(f"[Doc ID: {c['doc_id']} | Article: ") for c in kb.chunks)


@given("historical ticket resolutions and private agent snippet files are strictly excluded from the index")
def _excluded(kb):
    metas = kb.col.get(include=["metadatas"])["metadatas"]
    assert {m["source_origin"] for m in metas} == {"OFFICIAL_KB_ONLY"} and {m["source_file"] for m in metas} == {"documentation.json"}
    assert {m["doc_id"] for m in metas} == set(kb.registry)


@given(parsers.parse('the minimum cross-encoder relevance threshold is loaded from "{path}"'), target_fixture="tau")
def _tau(thresholds):
    return thresholds.routing_thresholds.retrieval_relevance_min


@given('a classified "TicketState" asking why a container deployment rolls back after health check timeouts', target_fixture="text")
def _answerable():
    return "Why does my container deployment roll back after health check timeouts?"


@when('the Retrieve component transitions state from "PRE_ROUTED_FOR_RETRIEVAL" to "RETRIEVED"', target_fixture="state")
def _retrieved(text, llm, kb, tau, run):
    return _retrieve(text, llm, kb, tau, run)


@then('"retrieved_passages" contains a non-empty list ordered by descending "rerank_score"')
def _ordered(state):
    s = [p["rerank_score"] for p in state.retrieved_passages]
    assert s and s == sorted(s, reverse=True)


@then('every passage in "retrieved_passages" has a "rerank_score" greater than or equal to 0.75')
def _above(state, tau):
    assert tau == 0.75 and all(p["rerank_score"] >= tau for p in state.retrieved_passages)


@then('every passage contains verifiable "doc_id", "article_title", "section_heading", and "doc_link" resolving to "kb_articles_registry"')
def _verifiable(state, kb, store, run):
    run(store.init(kb.registry))
    reg = run(_registry(store))
    for p in state.retrieved_passages:
        assert p["section_heading"] and (p["article_title"], p["doc_link"]) == (reg[p["doc_id"]]["article_title"], reg[p["doc_id"]]["doc_link"])


@given(parsers.parse('a classified "TicketState" from a "{fluency}" speaker asking "{question}"'), target_fixture="text")
def _speaker(fluency, question, ctx):
    ctx["fluency"] = fluency
    return question


@when('the Retrieve component runs "FR-06" semantic title matching and hybrid search', target_fixture="state")
def _bridged(text, llm, kb, tau, run, ctx):
    return _retrieve(text, llm, kb, tau, run, ctx["fluency"])


@then(parsers.parse('"semantic_matched_title" resolves to the official "{title}" article title'))
def _title(state, title, kb):
    assert state.semantic_matched_title == title and title.lower() in kb.by_title


@then(parsers.parse('"retrieved_passages" returns the grounded official "{doc_id}" passages with "rerank_score" >= 0.75'))
def _grounded(state, doc_id):
    assert state.retrieved_passages and all(p["doc_id"] == doc_id and p["rerank_score"] >= 0.75 for p in state.retrieved_passages)


@given('a classified "TicketState" asking about a newly released third-party service mesh plugin not covered in the 29 official articles', target_fixture="text")
def _uncovered():
    return "How do I configure sidecar injection for the new third-party service mesh plugin?"


@given('the highest-scoring candidate passage achieves a "rerank_score" of 0.54 (below 0.75)')
def _best_is_054(reranker):
    reranker.rules.insert(0, ("service mesh", None, 0.54))


@when("the Retrieve component searches the official documentation corpus", target_fixture="state")
def _searched(text, llm, kb, tau, run):
    return _retrieve(text, llm, kb, tau, run)


@then('the Retrieve component filters out all candidates and sets "retrieved_passages" to an empty list "[]"')
def _filtered(state):
    assert state.retrieval_executed and state.retrieved_passages == [] and state.attempted_sources


@then('"highest_rejected_retrieval_score" is recorded as 0.54 in the state for auditability')
def _rejected(state):
    assert state.highest_rejected_retrieval_score == 0.54


@given("an answer to a customer's legacy configuration question exists only in an agent's private snippet file or a historical ticket", target_fixture="snippet")
def _snippet(tmp_path):
    p = tmp_path / "agent_snippets.txt"
    p.write_text("Legacy configuration fix: set LEGACY_MODE=zebra42 on the gateway.", encoding="utf-8")
    return p


@given('that answer does not exist within the 29 official articles in "documentation.json"')
def _absent(kb):
    assert not any("zebra42" in r["content"].lower() for r in kb.registry.values())


@when("the Retrieve component queries the vector store with filter \"source_origin == 'OFFICIAL_KB_ONLY'\"", target_fixture="state")
def _filtered_query(snippet, llm, kb, tau, run):
    lock = get_settings().kb_manifest_path
    for unofficial in (snippet, ROOT / "data" / "development_tickets.json"):
        with pytest.raises(KBContaminationError):
            load_official_docs(unofficial, lock)
    return _retrieve("How do I enable the legacy configuration LEGACY_MODE zebra42 on the gateway?", llm, kb, tau, run)


@then("zero passages from historical tickets or private agent text files are returned")
def _official_only(state, kb):
    assert all(c["doc_id"] in kb.registry for c in state.attempted_sources)
    assert not any("zebra42" in p["chunk_text"].lower() for p in state.retrieved_passages)


@then('"retrieved_passages" is empty "[]"')
def _empty(state):
    assert state.retrieved_passages == []
