"""Retrieval regressions: reuse, complete families, semantic uncertainty and safe fallback."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from src.ktru import contracts as C
from src.ktru.catalog.build import DDL
from src.ktru.catalog.reader import Catalog
from src.ktru.catalog.vector import VectorIndex
from src.ktru.service.pipeline import Pipeline
from src.ktru.service.stages import HybridRetriever, LexicalRetriever, configured_retriever


@pytest.fixture
def catalog(tmp_path):
    with sqlite3.connect(tmp_path / "catalog.sqlite") as db:
        db.executescript(DDL)
        for code, family, name in [("chair-1", "chair", "Стул"), ("chair-2", "chair", "Стул"),
                                   ("driver-1", "driver", "Шуруповерт")]:
            db.execute("INSERT INTO positions (code,version,name,status,is_template,family_id,domain,supply_units) "
                       "VALUES (?,1,?,'ACTIVE',0,?,'furniture','[]')", (code, name, family))
            db.execute("INSERT INTO positions_fts VALUES (?,1,?,NULL,'')", (code, name))
    (tmp_path / "manifest.json").write_text(json.dumps({"snapshot_id": "vector-test", "counts": {"final": 3}}))
    return Catalog(tmp_path)


def vector_index(call):
    return SimpleNamespace(search=call, metadata={"final_positions": 3},
                           manifest=C.ModelManifest(model="test-local", prompt_sha256="test"))


def test_schemas_are_loaded_only_after_ranking(catalog, monkeypatch):
    calls = []
    original = catalog.family
    def family(fid):
        calls.append(fid)
        return original(fid)
    monkeypatch.setattr(catalog, "family", family)
    result = catalog.search("Стул и шуруповерт", limit_families=1)
    assert len(calls) == 1
    assert result.families[0].membership_complete
    assert len(result.families[0].members) == 2


def test_named_product_keeps_lexical_order_without_embedding(catalog):
    def unexpected(*args):
        pytest.fail("A clearly named product must not run embedding inference")
    result = HybridRetriever(catalog, vector_index(unexpected)).retrieve("Стул", catalog.snapshot_id)
    assert [f.family_id for f in result.families] == ["chair"]
    assert len(result.families[0].members) == 2


def test_semantic_singleton_requires_user_confirmation(catalog):
    retriever = HybridRetriever(catalog, vector_index(lambda _: [("driver", .9)]))
    state = Pipeline(catalog, retriever=retriever).parse(C.ParseRequest(
        request_id="semantic", expected_revision=0, text="Электрический помощник для заворачивания крепежа"))
    assert state.families_top[0].id == "driver"
    assert state.final is None and state.search_scope.coverage_status == "unconfirmed"
    assert state.clarification and state.clarification.kind == "family"
    assert "Подтвердите группу" in state.readiness_reasons_ru[0]
    assert state.model_manifests[0].model == "test-local"


def test_cache_reuses_inference_and_remains_snapshot_bound(catalog):
    calls = []
    def search(text):
        calls.append(text)
        return [("driver", .9)]
    retriever = HybridRetriever(catalog, vector_index(search))
    first = retriever.retrieve("Электрический помощник", catalog.snapshot_id)
    again = retriever.retrieve("Электрический помощник", catalog.snapshot_id)
    assert len(calls) == 1 and first.families == again.families
    assert first.scope == again.scope
    with pytest.raises(ValueError, match="snapshot"):
        retriever.retrieve("Электрический помощник", "another-snapshot")


def test_inference_failure_retains_lexical_candidates(catalog):
    def fail(_):
        raise RuntimeError("CPU model unavailable")
    retriever = HybridRetriever(catalog, vector_index(fail))
    result = retriever.retrieve("Опора как стул", catalog.snapshot_id)
    assert result.families[0].family_id == "chair"
    assert result.status == "partial"
    assert any(d.kind == "model_unavailable" for d in result.diagnostics)


def test_explicit_missing_index_falls_back_with_a_diagnostic(catalog, monkeypatch, tmp_path):
    monkeypatch.setenv("KTRU_VECTOR_INDEX_DIR", str(tmp_path / "absent"))
    result = configured_retriever(catalog).retrieve("Стул", catalog.snapshot_id)
    assert result.families[0].family_id == "chair"
    assert any(d.kind == "model_unavailable" for d in result.diagnostics)


def test_lexical_mode_does_not_load_an_index(catalog, monkeypatch):
    monkeypatch.setenv("KTRU_SEARCH_MODE", "lexical")
    assert type(configured_retriever(catalog)) is LexicalRetriever


def test_foreign_snapshot_is_rejected_before_loading_a_model(catalog, tmp_path):
    pytest.importorskip("numpy")
    from src.ktru.catalog.vector import INDEX_VERSION
    from src.ktru.ml.embed import PREPROCESSING_SHA256
    path = tmp_path / "index"
    path.mkdir()
    (path / "manifest.json").write_text(json.dumps({"index_version": INDEX_VERSION,
        "preprocessing_sha256": PREPROCESSING_SHA256, "snapshot_id": "another-snapshot"}))
    with pytest.raises(ValueError, match="snapshot"):
        VectorIndex(catalog, path)
