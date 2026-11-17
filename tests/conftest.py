"""Shared pytest fixtures. All model calls use deterministic fakes (tests/helpers.py), so the suite runs offline in seconds."""
import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.pool import NullPool

from src.config import PlattScaling, RoutingThresholds, ThresholdArtifact, get_settings, load_thresholds
from src.graph import TriagePipeline
from src.guardrails import SCRUBBER
from src.logging_store import AuditStore
from src.retrieve import KnowledgeBase, load_official_docs
from tests.helpers import FakeLLM, FakeNLI, FakeRerank, fake_embed


# Input:  tag (str); function – generated scenario test
# What:   Spec traceability tags (@FR-01, @component:ingest ...) are documentation, not pytest markers
# Output: bool – True means "handled, apply nothing"
def pytest_bdd_apply_tag(tag: str, function: Any) -> bool:
    return True


# Input:  config; items (list) – collected tests
# What:   Skips tests that load the real local models unless RUN_INTEGRATION=1
# Output: None
def pytest_collection_modifyitems(config: Any, items: list) -> None:
    if os.getenv("RUN_INTEGRATION") != "1":
        for it in items:
            if "integration" in it.keywords:
                it.add_marker(pytest.mark.skip(reason="set RUN_INTEGRATION=1 to load the real local models"))


@pytest.fixture(scope="session")
def docs() -> list[dict]:
    s = get_settings()
    return load_official_docs(s.kb_path, s.kb_manifest_path)


@pytest.fixture(scope="session")
def reranker() -> FakeRerank:
    return FakeRerank()


@pytest.fixture(scope="session")
def kb(docs: list[dict], reranker: FakeRerank) -> KnowledgeBase:
    return KnowledgeBase(docs, embed=fake_embed, rerank=reranker, chroma_path=None, embed_id="fake-hash-embed")


@pytest.fixture(autouse=True)
def _reset_fakes(reranker: FakeRerank, kb: KnowledgeBase):
    reranker.rules = [("health check", "DOC-DEPLOY-001", 0.88), ("rolls back", "DOC-DEPLOY-001", 0.88), ("502", "DOC-DEPLOY-001", 0.86),
                      ("dependency", "DOC-DEPLOY-003", 0.9), ("page size", "DOC-API-002", 0.91), ("smtp", "DOC-DEPLOY-004", 0.8)]
    reranker.queries.clear()
    kb.excluded = set()
    yield


@pytest.fixture
def artifact_path(tmp_path: Path) -> Path:
    art = ThresholdArtifact(calibration_version="cal_test", fitted_timestamp_utc="2026-11-16T00:00:00+00:00", dev_dataset_sha256="0" * 64,
                            model_identifier_calibrated="fake-model", platt_scaling=PlattScaling(temperature_T=1.0, slope_a=1.0, intercept_b=0.0),
                            routing_thresholds=RoutingThresholds(classification_confidence_min=0.82, retrieval_relevance_min=0.75,
                                                                 nli_entailment_min=0.85, nli_contradiction_max=0.25))
    p = tmp_path / "thresholds_v1.json"
    p.write_text(art.model_dump_json(indent=2), encoding="utf-8")
    return p


@pytest.fixture
def thresholds(artifact_path: Path) -> ThresholdArtifact:
    return load_thresholds(str(artifact_path))


@pytest.fixture
def run():
    loop = asyncio.new_event_loop()
    yield loop.run_until_complete
    loop.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.db"


@pytest.fixture
def store(db_path: Path) -> AuditStore:
    return AuditStore(f"sqlite+aiosqlite:///{db_path.as_posix()}", poolclass=NullPool)


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def make_pipeline(thresholds: ThresholdArtifact, kb: KnowledgeBase, llm: FakeLLM, store: AuditStore, run):
    # What: builds a pipeline wired to the fakes (+ a fresh SQLite audit store unless with_store=False)
    def make(mode: str | None = None, with_store: bool = True, llm_client: Any = None) -> TriagePipeline:
        if with_store:
            run(store.init(kb.registry))
        return TriagePipeline(settings=get_settings(), thresholds=thresholds, kb=kb, llm=llm_client or llm, nli=FakeNLI(), scrubber=SCRUBBER,
                              store=store if with_store else None, mode=mode)
    return make


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}
