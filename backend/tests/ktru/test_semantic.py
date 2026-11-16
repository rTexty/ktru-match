"""Optional semantic family retrieval: fusion keeps lexical evidence first, proposes a family on embedding evidence
alone only when confident, and recognises descriptions nearer to a family outside the served groups."""

import threading
import time

import pytest

from src.ktru import contracts as C
from src.ktru.catalog.reader import FamilyHit, Position
from src.ktru.ml.semantic import SemanticFamilyIndex, configured_semantic
from src.ktru.service.stages import fuse_semantic

MANIFEST = C.ModelManifest(model="test-embedder", prompt_sha256="0" * 64)


def member(domain="tools"):
    return Position(code="p", version=1, name="x", okpd2_code="", okpd2_name="", status="ACTIVE", is_template=False,
                    family_id="f", domain=domain, supply_units=[])


def hit(family_id, head_ok=True, domain="tools"):
    return FamilyHit(family_id, family_id, None, [member(domain)], 1.0, 1.0, 1.0, head_ok)


def test_a_family_named_by_its_noun_stays_ahead_of_a_merely_similar_one():
    fused = fuse_semantic(None, "Дрель 600 Вт", [hit("drill"), hit("saw", head_ok=False)],
                          [("saw", 0.55), ("drill", 0.54)])
    assert [f.family_id for f in fused][:1] == ["drill"]
    assert not any(f.semantic_pick for f in fused)  # 0.55 is below the proposal threshold


def test_a_confident_first_choice_may_be_proposed_without_a_head_match():
    fused = fuse_semantic(None, "электростеплер", [hit("pliers", head_ok=False), hit("stapler", head_ok=False)],
                          [("stapler", 0.71), ("pliers", 0.52)])
    assert fused[0].family_id == "stapler" and fused[0].semantic_pick


def test_a_close_second_family_blocks_the_proposal():
    fused = fuse_semantic(None, "x", [hit("a", head_ok=False), hit("b", head_ok=False)], [("a", 0.70), ("b", 0.69)])
    assert not any(f.semantic_pick for f in fused)


def test_a_nearer_family_outside_the_groups_marks_the_description_as_not_ours():
    fused = fuse_semantic(None, "Дрель хирургическая", [hit("drill")], [("drill", 0.62)],
                          outside=("surgical-drill", 0.70))
    assert fused[0].outside_group == "surgical-drill" and not fused[0].semantic_pick
    fused = fuse_semantic(None, "Дрель", [hit("drill")], [("drill", 0.62)], outside=("surgical-drill", 0.64))
    assert fused[0].outside_group is None  # 0.02 is within the margin: the served family stays


def test_index_returns_served_neighbours_and_the_nearest_outside_family(tmp_path):
    vectors = {"q": [1.0, 0.0], "drill": [0.9, 0.1], "saw": [0.1, 0.9], "surgical": [1.0, 0.05]}

    def call(texts):
        return [vectors["q"] if t.startswith("Instruct") else vectors[t] for t in texts]

    index = SemanticFamilyIndex(call, MANIFEST, cache_dir=tmp_path)
    documents = {"drill": "drill", "saw": "saw", "surgical": "surgical"}
    index._index(documents, {"drill", "saw"})  # what the background build does
    nearest, outside = index.rank("дрель", documents, {"drill", "saw"})
    assert [f for f, _ in nearest] == ["drill", "saw"]
    assert outside[0] == "surgical"
    assert list(tmp_path.glob("families-*.npy"))  # vectors are cached for the next start


def test_disabled_without_url_and_refuses_proprietary_hosts(monkeypatch):
    monkeypatch.delenv("KTRU_EMBED_URL", raising=False)
    assert configured_semantic() is None
    monkeypatch.setenv("KTRU_EMBED_URL", "https://api.openai.com")
    monkeypatch.setenv("KTRU_EMBED_MODEL", "x")
    with pytest.raises(ValueError):
        configured_semantic()


def test_a_request_never_builds_or_waits_for_the_index(tmp_path):
    built = threading.Event()

    def call(texts):
        if len(texts) > 1 or texts[0] == "drill":
            built.set()
        return [[1.0, 0.0] for _ in texts]

    index = SemanticFamilyIndex(call, MANIFEST, cache_dir=tmp_path)
    index._lock.acquire()  # a background build is running
    try:
        with pytest.raises(RuntimeError):
            index.rank("дрель", {"drill": "drill"}, {"drill"})
        assert not built.is_set()
    finally:
        index._lock.release()
    with pytest.raises(RuntimeError):  # not built yet: this request stays lexical and starts a background build
        index.rank("дрель", {"drill": "drill"}, {"drill"})
    assert built.wait(5)
    for _ in range(50):
        if index._matrix is not None:
            break
        time.sleep(0.05)
    assert index.rank("дрель", {"drill": "drill"}, {"drill"})[0][0][0] == "drill"


def test_a_failed_index_batch_is_retried(tmp_path):
    calls = []

    def call(texts):
        calls.append(len(texts))
        if len(calls) == 1:
            raise TimeoutError("slow batch")
        return [[1.0, 0.0] for _ in texts]

    index = SemanticFamilyIndex(call, MANIFEST, cache_dir=tmp_path)
    index._index({"a": "a", "b": "b"}, {"a"})
    assert calls == [2, 2] and index._matrix is not None
