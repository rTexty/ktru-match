"""A classifier ranks proposals. It cannot remove families or confirm coverage."""
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.ktru import contracts as C
from src.ktru.catalog.reader import FamilyHit
from src.ktru.ml.classify import FamilyClassifier


def hit(ident, name, head=True):
    return FamilyHit(ident, name, None, [], 0, 0, 0, head)


def classifier(label, confidence=0.9):
    manifest = C.ModelManifest(model="test", prompt_sha256="test", runtime="test")
    return FamilyClassifier(lambda text, labels: {"family": {"label": label, "confidence": confidence}}, manifest)


def test_choice_reorders_without_losing_any_family():
    pool = [hit("a", "Стул"), hit("b", "Кресло"), hit("c", "Стол")]
    ranked = classifier("Стол").rank("Стол для офиса", pool)
    assert [f.family_id for f in ranked] == ["c", "a", "b"]
    assert ranked[0] is pool[2]
    assert all(a.membership_complete for a in ranked)


@pytest.mark.parametrize("label,score", [("UNKNOWN", .9), ("Стол", .2)])
def test_abstention_keeps_lexical_order(label, score):
    pool = [hit("a", "Стул"), hit("b", "Стол")]
    assert classifier(label, score).rank("Стул", pool) == pool


def test_duplicate_names_cannot_choose_one_family():
    pool = [hit("a", "Стул"), hit("b", "Стол"), hit("c", "Стол")]
    assert classifier("Стол").rank("Стол", pool) == pool


def test_wrong_product_head_does_not_become_top_proposal():
    pool = [hit("a", "Стул"), hit("b", "Стол", False)]
    with pytest.raises(ValueError):
        classifier("Стол").rank("Стул", pool)


@pytest.mark.parametrize("label,score", [("invented", .9), ("Стол", float("nan")), ("Стол", 2)])
def test_invalid_model_response_is_reported(label, score):
    with pytest.raises(ValueError):
        classifier(label, score).rank("Стол", [hit("a", "Стол")])


def test_cache_is_bounded_and_pool_sensitive():
    calls = []
    def predict(text, labels):
        calls.append((text, tuple(labels)))
        return {"family": {"label": "UNKNOWN", "confidence": .8}}
    c = FamilyClassifier(predict, C.ModelManifest(model="test", prompt_sha256="test"), cache_size=2)
    pool = [hit("a", "Стол")]
    for text in ["one", "one", "two", "three", "one"]:
        c.rank(text, pool)
    assert len(calls) == 4
    c.rank("one", [replace(pool[0], name="Стул")])
    assert len(calls) == 5


def test_missing_local_weights_fail_before_inference(tmp_path):
    with pytest.raises(ValueError, match="local"):
        FamilyClassifier.from_local(tmp_path)


def test_local_loader_hashes_encoder_and_disables_remote_fetch(tmp_path, monkeypatch):
    from src.ktru.ml import classify
    for name in ("model.safetensors", "config.json", "tokenizer.json", "tokenizer_config.json",
                 "encoder_config/config.json"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    observed = []
    class Model:
        def to(self, **kwargs):
            assert kwargs == {"device": "cpu", "dtype": "float32"}
            return self
        def eval(self):
            return self
        def classify_text(self, text, schema, **kwargs):
            return {"family": {"label": "UNKNOWN", "confidence": .9}}
    def load(path, **kwargs):
        observed.append(kwargs)
        return Model()
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(set_num_threads=lambda n: None, float32="float32"))
    monkeypatch.setitem(sys.modules, "gliner2", SimpleNamespace(AutoExtractor=SimpleNamespace(from_pretrained=load)))
    monkeypatch.setattr(classify, "version", lambda name: "test")
    first = FamilyClassifier.from_local(tmp_path)
    (tmp_path / "encoder_config/config.json").write_text('{"changed":true}')
    second = FamilyClassifier.from_local(tmp_path)
    assert first.manifest.revision != second.manifest.revision
    assert all(o == {"local_files_only": True} for o in observed)
    assert first.manifest.model == "local/GLiNER-family-classifier"


def test_empty_pool_needs_no_inference():
    assert classifier("UNKNOWN").rank("Стул", []) == []


def test_cache_size_must_be_positive():
    with pytest.raises(ValueError):
        FamilyClassifier(lambda text, labels: {}, C.ModelManifest(model="test", prompt_sha256="test"), cache_size=0)
