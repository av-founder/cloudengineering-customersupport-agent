"""Integration tests with the real local models (MiniLM, MS-MARCO cross-encoder, DeBERTa NLI). Run with RUN_INTEGRATION=1."""
import pytest

from src.config import get_settings
from src.guardrails import NLIScorer
from src.retrieve import KnowledgeBase

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def real_kb() -> KnowledgeBase:
    return KnowledgeBase.from_settings(get_settings(), persist=False)


@pytest.mark.parametrize("query, expected", [
    ("Our deployment reaches running and then rolls back after about a minute. The logs mention a health check timeout.", "DOC-DEPLOY-001"),
    ("how to get all data? page size big is reject. small is very slow.", "DOC-API-002"),
    ("builds that work last week are now fail during dependency resolution", "DOC-DEPLOY-003"),
])
def test_hybrid_search_finds_the_official_article(real_kb, query, expected):
    top3 = [c["doc_id"] for c in real_kb.search(query)[:3]]
    assert expected in top3


def test_nli_entails_paraphrase_and_rejects_invention():
    nli = NLIScorer(get_settings().nli_model)
    premise = "A locked account displays a red banner and unlocks automatically after thirty minutes."
    (ent_ok, _), (ent_bad, _) = nli([(premise, "The account unlocks by itself after thirty minutes."), (premise, "Run cloudserve unlock --force-reset-nat to unlock it.")])
    assert ent_ok > 0.85 > ent_bad
