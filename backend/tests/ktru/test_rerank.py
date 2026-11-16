"""Optional family reranker: reorders only head-matching families of the served groups and never drops one."""

from types import SimpleNamespace

import pytest

from src.ktru import contracts as C
from src.ktru.ml.rerank import FamilyReranker, configured_reranker

MANIFEST = C.ModelManifest(model="test-reranker", prompt_sha256="0" * 64)


def family(name, head_ok=True, domain="tools"):
    member = SimpleNamespace(domain=domain, okpd2_name="Инструменты")
    return SimpleNamespace(name=name, head_ok=head_ok, members=[member], template=None)


def test_only_head_matching_group_families_change_places():
    hits = [family("Дрель ручная механическая"), family("Сверло", head_ok=False),
            family("Дрель ручная электрическая"), family("Дрель хирургическая", domain="unknown")]
    calls = []

    def call(query, documents):
        calls.append(documents)
        return [0.1, 0.9]  # the electric drill wins among the two head-matching group families

    ranked = FamilyReranker(call, MANIFEST).rank("Дрель ударная 750 Вт", hits)
    assert [h.name for h in ranked] == ["Дрель ручная электрическая", "Сверло", "Дрель ручная механическая",
                                        "Дрель хирургическая"]
    assert len(calls[0]) == 2


def test_a_single_candidate_needs_no_call():
    def call(query, documents):
        raise AssertionError("not called")

    hits = [family("Молоток"), family("Кувалда", head_ok=False)]
    assert FamilyReranker(call, MANIFEST).rank("Молоток 500 г", hits) == hits


def test_invalid_scores_are_an_error_not_a_silent_order():
    with pytest.raises(ValueError):
        FamilyReranker(lambda q, d: [0.5], MANIFEST).rank("Дрель", [family("Дрель А"), family("Дрель Б")])


def test_disabled_without_url_and_refuses_proprietary_hosts(monkeypatch):
    monkeypatch.delenv("KTRU_RERANK_URL", raising=False)
    assert configured_reranker() is None
    monkeypatch.setenv("KTRU_RERANK_URL", "https://api.openai.com")
    monkeypatch.setenv("KTRU_RERANK_MODEL", "x")
    with pytest.raises(ValueError):
        configured_reranker()
