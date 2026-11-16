"""Full MVP checks: Portal fields, dimension parsing, real listings, grounding, LLM config and the analyze lifecycle.

Pipeline tests need a built snapshot (see README); ml.ground/config checks are pure.
Run: cd backend && PYTHONPATH=. uv run --with pytest pytest tests/ktru/test_full_mvp.py -q
"""

import os
import uuid
from pathlib import Path

import pytest

from src.ktru import contracts as C
from src.ktru.catalog.reader import ste_text
from src.ktru.engine.text import quantities
from src.ktru.ml import extract as ml
from src.ktru.service.pipeline import ServiceError

ROOT = Path(__file__).resolve().parents[3]
SNAP = Path(os.environ.get("KTRU_SNAPSHOT_DIR", ROOT / ".ktru-build" / "snapshots" / "current"))
needs_snapshot = pytest.mark.skipif(not (SNAP / "manifest.json").exists(), reason="snapshot not built")


@pytest.fixture(scope="module")
def pipeline():
    from src.ktru.catalog.reader import Catalog
    from src.ktru.service.pipeline import Pipeline

    return Pipeline(Catalog(SNAP))


def _rid() -> str:
    return uuid.uuid4().hex


def _parse(p, text):
    return p.parse(C.ParseRequest(request_id=_rid(), expected_revision=0, text=text))


def _analyze(p, st, extractor):
    return p.analyze(C.AnalyzeRequest(session_id=st.session_id, request_id=_rid(), expected_revision=st.revision),
                     extractor=extractor)  # fmt: skip


# ---- Portal fields ------------------------------------------------------------------------------------------------


@needs_snapshot
def test_portal_colour_field_on_chair(pipeline):
    st = _parse(pipeline, "Стул на металлическом каркасе, чёрный, мягкое сиденье, жёсткая спинка")
    question = st.clarification
    family_id = pipeline.catalog.position("31.01.11.150-00000002").family_id
    if question and question.kind == "family":
        index = next(i for i, family in enumerate(st.families_top) if family.id == family_id)
        st = pipeline.answer(C.AnswerRequest(session_id=st.session_id, request_id=_rid(),
            expected_revision=st.revision, question_id=question.id, option_id=question.options[index].id))
    else:
        assert st.family and st.family.id == family_id and st.search_scope.coverage_status == "family_confirmed"
    final = st.final.position_ref.code if st.final else None
    assert final == "31.01.11.150-00000002", (
        f"final={final!r}; diagnostics={[(d.kind, d.message_ru) for d in st.diagnostics]}"
    )
    hits = [f for f in st.fields if f.source == "portal" and f.label_ru == "Цвет" and f.portal_category is not None]
    assert hits, f"no portal colour field; fields={[(f.source, f.label_ru, f.portal_category) for f in st.fields]}"


@needs_snapshot
def test_portal_handle_material_field_on_hammer(pipeline):
    st = _parse(pipeline, "Молоток масса бойка 300 г, рукоятка деревянная")
    hits = [f for f in st.fields if f.source == "portal" and f.label_ru == "Материал рукоятки"]
    assert hits, f"no portal handle-material field; fields={[(f.source, f.label_ru, f.display_ru) for f in st.fields]}"


@needs_snapshot
def test_edit_portal_relation_replaces_previous_interpretation(pipeline):
    st = _parse(pipeline, "Молоток масса бойка 300 г, рукоятка деревянная")
    field = next(field for field in st.fields if field.source == "portal" and field.label_ru == "Материал рукоятки")
    result = pipeline.edit(C.EditRequest(session_id=st.session_id, request_id=_rid(), expected_revision=st.revision,
        patch=C.SetFieldPatch(label_ru=field.label_ru, field_id=field.id, value="стальная")))
    fields = [field for field in result.fields if field.label_ru == "Материал рукоятки"]
    assert len(fields) == 1 and fields[0].display_ru == "стальная" and fields[0].status == "confirmed"
    assert any(assertion.span and "деревянная" in assertion.span.text for assertion in result.assertions)


# ---- dimensions ---------------------------------------------------------------------------------------------------


@needs_snapshot
def test_dimensions_are_three_distinct_quantities_without_contradiction(pipeline):
    text = "Шкаф деревянный для документов 1500*900*400мм"
    st = _parse(pipeline, text)
    contradictions = [(d.kind, d.message_ru) for d in st.diagnostics if d.kind == "contradiction"]
    assert not contradictions, f"unexpected contradiction diagnostics: {contradictions}"

    qs = quantities(text)
    observed = [(q.raw, str(q.a), q.property_raw) for q in qs]
    assert len(qs) == 3, f"quantities={observed}"
    props = [q.property_raw for q in qs]
    assert len(set(props)) == 3, f"property_raw not distinct: {props}"


# ---- real listings ------------------------------------------------------------------------------------------------


@needs_snapshot
def test_examples_cover_domains_and_ste_text(pipeline):
    for domain in ("clothing", "furniture", "tools"):
        ex = pipeline.catalog.examples(domain, 1)
        assert len(ex) == 1, f"domain={domain}: examples({domain!r}, 1) returned {len(ex)}"

    tools = pipeline.catalog.examples("tools", 5)
    assert len(tools) == 5, f"examples('tools', 5) returned {len(tools)}"
    for item in tools:
        assert isinstance(item, tuple) and len(item) == 3, f"not a 3-tuple: {item!r}"
        ste_id, name, text = item
        assert ste_id and name, f"empty id/name: {item!r}"
        assert text.strip(), f"empty text for ste_id={ste_id!r}"

    got = ste_text("Стойка", "Масса 10.20000 кг | Тип стойка | Вес 0.00000 кг | Объем (не задано)")
    assert got == "Стойка. Масса 10,2 кг, тип стойка", f"ste_text={got!r}"


# ---- grounding (pure) ----------------------------------------------------------------------------------------------


def test_ground_keeps_verbatim_quote_and_rejects_the_rest():
    text = "Молоток слесарный, ручка из дерева"
    facts = [
        {"property": "Материал", "value": "дерево", "quote": "РУЧКА ИЗ ДЕРЕВА"},
        {"property": "Цвет", "value": "красный", "quote": "красная ручка"},
        {"property": "Тип", "value": "кувалда", "quote": "Молоток слесарный"},
        {"property": "", "quote": "x"},
    ]
    kept, rejected = ml.ground(facts, text)
    assert len(kept) == 1, f"kept={kept}"
    k = kept[0]
    assert (k["start"], k["end"]) == (19, 34), f"offsets={(k['start'], k['end'])}"
    assert k["quote"] == text[19:34] == "ручка из дерева", f"quote={k['quote']!r}"
    assert len(rejected) == 3, f"rejected={rejected}"
    empty_reasons = [r for r in rejected if not r["reason_ru"].strip()]
    assert not empty_reasons, f"empty reason_ru: {empty_reasons}"


# ---- LLM config (pure) ---------------------------------------------------------------------------------------------


def test_llm_config_refuses_proprietary_and_reports_status(monkeypatch):
    monkeypatch.setenv("KTRU_LLM_URL", "https://api.openai.com")
    monkeypatch.setenv("KTRU_LLM_MODEL", "x")
    with pytest.raises(ValueError):
        ml.config()

    monkeypatch.delenv("KTRU_LLM_URL", raising=False)
    monkeypatch.delenv("KTRU_LLM_MODEL", raising=False)
    assert ml.config() is None
    monkeypatch.setitem(ml._status, "at", 0.0)
    assert ml.status() == "disabled", f"status={ml.status()!r}"

    monkeypatch.setenv("KTRU_LLM_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("KTRU_LLM_MODEL", "x")
    monkeypatch.setitem(ml._status, "at", 0.0)
    got = ml.status()
    assert got == "unavailable", f"status={got!r}"


# ---- analyze -------------------------------------------------------------------------------------------------------

HAMMER = "Молоток слесарный, вес инструмента полкило"
HAMMER_FACT = {"property": "Масса молотка", "value": "0.5", "unit": "кг", "subject": "item",
               "quote": "вес инструмента полкило"}  # fmt: skip


@needs_snapshot
def test_analyze_with_fake_extractor_merges_llm_fact(pipeline):
    st = _parse(pipeline, HAMMER)
    out = _analyze(pipeline, st, lambda _t: [HAMMER_FACT])
    assert out.revision == st.revision + 1, f"revision={out.revision}, parse revision={st.revision}"
    assert out.status == "analyzed", f"status={out.status!r}; diagnostics={[d.kind for d in out.diagnostics]}"

    llm_assertions = [a for a in out.assertions if a.extractor == "llm"]
    assert llm_assertions, f"extractors={[(a.property_raw, a.extractor) for a in out.assertions]}"
    noted = [a.note_ru for a in llm_assertions if a.note_ru and "полкило" in a.note_ru]
    assert noted, f"note_ru={[a.note_ru for a in llm_assertions]}"

    fields = [(f.source, f.label_ru, f.display_ru) for f in out.fields if f.label_ru in ("Масса", "Масса молотка")]  # canonical property name
    assert fields, f"fields={[(f.source, f.label_ru, f.display_ru) for f in out.fields]}"


@needs_snapshot
def test_analyze_extractor_failure_is_partial_with_rules_result(pipeline):
    st = _parse(pipeline, HAMMER)

    def boom(_text):
        raise TimeoutError("model timed out")

    out = _analyze(pipeline, st, boom)
    assert out.status == "partial", f"status={out.status!r}"
    kinds = {d.kind for d in out.diagnostics}
    assert "model_unavailable" in kinds, f"diagnostics={[(d.kind, d.message_ru) for d in out.diagnostics]}"

    type_fields = [f for f in out.fields if f.label_ru == "Тип"]
    assert type_fields, f"rules field lost; fields={[(f.source, f.label_ru, f.display_ru) for f in out.fields]}"
    assert any(a.extractor == "rules" for a in out.assertions), (
        f"assertions={[(a.property_raw, a.extractor) for a in out.assertions]}"
    )


@needs_snapshot
def test_analyze_with_stale_expected_revision_raises_409(pipeline):
    st = _parse(pipeline, HAMMER)
    req = C.AnalyzeRequest(session_id=st.session_id, request_id=_rid(), expected_revision=st.revision - 1)
    with pytest.raises(ServiceError) as exc:
        pipeline.analyze(req, extractor=lambda _t: [])
    assert exc.value.status == 409, f"status={exc.value.status}"
    assert exc.value.body.code == "stale_revision", f"code={exc.value.body.code!r}"


@needs_snapshot
def test_analyze_after_text_replaced_uses_latest_revision(pipeline):
    st = _parse(pipeline, HAMMER)
    renewed = pipeline.parse(
        C.ParseRequest(
            session_id=st.session_id,
            request_id=_rid(),
            expected_revision=st.revision,
            text="Молоток слесарный, ручка деревянная",
        )  # fmt: skip
    )
    assert renewed.revision == st.revision + 1, f"revision={renewed.revision}"
    out = _analyze(pipeline, renewed, lambda _t: [])
    assert out.revision == renewed.revision + 1, f"revision={out.revision}"
    assert out.status == "analyzed", f"status={out.status!r}"


@needs_snapshot
def test_llm_result_for_old_text_is_ignored(pipeline):
    st = _parse(pipeline, HAMMER)
    analyzed = _analyze(pipeline, st, lambda _t: [HAMMER_FACT])
    assert any(a.extractor == "llm" for a in analyzed.assertions), "fake LLM fact was not published"

    after = pipeline.parse(
        C.ParseRequest(
            session_id=st.session_id, request_id=_rid(), expected_revision=analyzed.revision, text="Ножовка по дереву"
        )  # fmt: skip
    )
    stale = [(a.property_raw, a.extractor, a.note_ru) for a in after.assertions if a.extractor == "llm"]
    assert not stale, f"LLM fact for the old text leaked in: {stale}"
