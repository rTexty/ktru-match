"""Regression checks for the KTRU engine: real-snapshot seed regressions plus pure quantity rules.

Run: cd backend && PYTHONPATH=. uv run --with pytest pytest tests/ktru/test_regressions.py -q
Requires a built snapshot (see README); pure checks run without one.
"""

import os
import uuid
from pathlib import Path

import pytest

from src.ktru import contracts as C
from src.ktru.engine.text import quantities
from src.ktru.service.pipeline import ServiceError

ROOT = Path(__file__).resolve().parents[3]
SNAP = Path(os.environ.get("KTRU_SNAPSHOT_DIR", ROOT / ".ktru-build" / "snapshots" / "current"))
needs_snapshot = pytest.mark.skipif(not (SNAP / "manifest.json").exists(), reason="snapshot not built")


@pytest.fixture(scope="module")
def pipeline():
    from src.ktru.catalog.reader import Catalog
    from src.ktru.service.pipeline import Pipeline

    return Pipeline(Catalog(SNAP))


def _parse(p, text):
    return p.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))


def _final_code(st):
    return st.final.position_ref.code if st.final else None


# ---- real snapshot regressions ------------------------------------------------------------------------------------


@needs_snapshot
def test_metal_frame_chair_without_soft_seat(pipeline):
    """«без мягкого сиденья» must not be shown as a positive soft-seat field and must select the hard-seat position."""
    st = _parse(pipeline, "Стул на металлическом каркасе без мягкого сиденья")
    bad = [
        (f.label_ru, f.display_ru, f.status, f.source)
        for f in st.fields
        if f.label_ru == "Наличие мягкого сидения" and f.display_ru.lower().startswith("да")
    ]
    assert not bad, f"soft-seat field displayed as «Да»: {bad}"
    alive = {c.ref.code for c in st.candidates if c.status == "alive"}
    excluded = {c.ref.code for c in st.candidates if c.status == "excluded"}
    assert "31.01.11.150-00000001" in alive, f"alive={sorted(alive)}"
    assert "31.01.11.150-00000002" in excluded, f"excluded={sorted(excluded)}"


@needs_snapshot
def test_soft_back_without_soft_seat_not_third_position(pipeline):
    """With a soft back but no soft seat the hard-seat sibling must not win over the soft-back one."""
    st = _parse(pipeline, "Стул на металлическом каркасе с мягкой спинкой, без мягкого сиденья")
    code = _final_code(st)
    assert code != "31.01.11.150-00000003", f"final={code} ({st.final.name if st.final else None!r})"


@needs_snapshot
def test_hammer_package_no_plaster_type_and_no_handle_length(pipeline):
    st = _parse(pipeline, "Молоток длина 300 мм, в коробке 20 штук масса упаковки 12 кг")
    bad_type = [
        (f.label_ru, f.display_ru, f.status, f.source)
        for f in st.fields
        if f.label_ru == "Тип" and "штукатурный" in f.display_ru.lower()
    ]
    assert not bad_type, f"plaster-type field: {bad_type}"
    bad_handle = [
        (f.label_ru, f.display_ru, f.status, f.source)
        for f in st.fields
        if f.label_ru == "Длина рукоятки" and f.status == "from_text"
    ]
    assert not bad_handle, f"handle length filled from text: {bad_handle}"
    assert st.final is None, f"final={_final_code(st)} ({st.final.name if st.final else None!r})"


@needs_snapshot
@pytest.mark.parametrize(
    "text",
    ["Стол письменный высота 750 мм", "Стол письменный длина не менее 1200 мм", "Стол письменный ширина 600 мм"],
    ids=["height", "length", "width"],
)
def test_desk_dimensions_do_not_fill_thickness(pipeline, text):
    st = _parse(pipeline, text)
    bad = [
        (f.label_ru, f.display_ru, f.status)
        for f in st.fields
        if f.label_ru == "Толщина материала столешницы" and f.status == "from_text"
    ]
    assert not bad, f"thickness filled from {text!r}: {bad}"


@needs_snapshot
def test_hammer_head_mass_does_not_fill_hammer_mass(pipeline):
    st = _parse(pipeline, "Молоток масса бойка 300 г")
    bad = [
        (f.label_ru, f.display_ru, f.status)
        for f in st.fields
        if f.label_ru == "Масса молотка" and f.status == "from_text"
    ]
    assert not bad, f"hammer mass filled from head mass: {bad}"


@needs_snapshot
def test_office_chair_armed_and_unarmed_is_contradiction_not_ready(pipeline):
    st = _parse(pipeline, "Кресло офисное с подлокотниками и без подлокотников")
    diags = [(d.kind, d.message_ru) for d in st.diagnostics]
    assert any(kind == "contradiction" for kind, _ in diags), f"diagnostics={diags}"
    assert st.readiness != "ready", f"readiness={st.readiness!r}, diagnostics={diags}"


@needs_snapshot
@pytest.mark.parametrize(
    "text",
    ["Куртка утепленная размер 44 цвет черный", "Ножовка по дереву", "Дрель Makita HR2470"],
    ids=["coat", "saw", "drill"],
)
def test_family_name_contains_head_noun_and_no_final(pipeline, text):
    st = _parse(pipeline, text)
    if st.family is not None:
        # The family must be named after the product noun or its catalog synonym (ALIASES), never a later word.
        from src.ktru.engine.text import ALIASES

        word = text.split()[0].lower()
        heads = [word[:4]] + [v.split()[0][:4] for k, v in ALIASES.items() if word.startswith(k)]
        assert any(h in st.family.name.lower() for h in heads), f"heads={heads!r}, family={st.family.name!r}"
    assert st.final is None, f"final={_final_code(st)} ({st.final.name if st.final else None!r})"


@needs_snapshot
def test_warm_jacket_size_does_not_fill_sex_from_catalog(pipeline):
    st = _parse(pipeline, "Куртка утепленная размер 44")
    bad = [
        (f.label_ru, f.display_ru, f.status, f.source)
        for f in st.fields
        if f.label_ru == "Половой признак" and f.status == "from_catalog"
    ]
    assert not bad, f"sex guessed from catalog: {bad}"


@needs_snapshot
def test_tshirt_cotton_material_visible_in_fields_or_assertions(pipeline):
    st = _parse(pipeline, "Футболка женская хлопок")
    in_fields = [f.display_ru for f in st.fields if "хлоп" in f.display_ru.lower()]
    in_assertions = [a.value_raw for a in st.assertions if "хлоп" in (a.value_raw or "").lower()]
    assert in_fields or in_assertions, (
        f"no «хлоп» in fields={[f.display_ru for f in st.fields]}, "
        f"assertions={[(a.property_raw, a.value_raw) for a in st.assertions]}"
    )


@needs_snapshot
def test_unbound_conflict_is_resolvable_by_first_option(pipeline):
    st = _parse(pipeline, "Кресло офисное, высота 100 см, высота 200 см")
    if st.clarification is not None and st.clarification.kind == "conflict":
        st = pipeline.answer(
            C.AnswerRequest(
                session_id=st.session_id,
                request_id=uuid.uuid4().hex,
                expected_revision=st.revision,
                question_id=st.clarification.id,
                option_id=st.clarification.options[0].id,
            )
        )
        diags = [(d.kind, d.message_ru) for d in st.diagnostics]
        assert not any(kind == "contradiction" for kind, _ in diags), (
            f"contradiction survived the conflict answer: readiness={st.readiness!r}, diagnostics={diags}"
        )


@needs_snapshot
def test_nonzero_revision_on_new_session_does_not_leak_sessions(pipeline):
    before = len(pipeline._sessions)
    req = C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=5, text="Стул на металлическом каркасе")
    with pytest.raises(ServiceError) as exc:
        pipeline.parse(req)
    assert exc.value.status == 409, f"status={exc.value.status}, code={exc.value.body.code}"
    assert len(pipeline._sessions) == before, f"sessions before={before}, after={len(pipeline._sessions)}"


@needs_snapshot
def test_parse_same_request_id_returns_same_session(pipeline):
    req = C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text="Кресло офисное")
    first = pipeline.parse(req)
    second = pipeline.parse(req)
    assert first.session_id == second.session_id, (
        f"first={first.session_id}@{first.revision}, second={second.session_id}@{second.revision}"
    )


# ---- pure quantity rules (no snapshot) ----------------------------------------------------------------------------

QUANTITY_CASES = [
    ("Выпуск 2020 г.", "no_gram_unit"),
    ("80 г/м2", "no_gram_unit"),
    ("Робот 5 шт", "all_eq"),
    ("Стол 50х60х70 см", "three_cm"),
    ("10-5 см", "one_ordered_between"),
]


@pytest.mark.parametrize("text,kind", QUANTITY_CASES, ids=["year", "density", "count", "dimensions", "range"])
def test_quantity_regressions(text, kind):
    qs = quantities(text)
    observed = [(q.raw, q.op, str(q.a), str(q.b), q.unit_raw) for q in qs]
    if kind == "no_gram_unit":
        assert not any(q.unit_raw == "г" for q in qs), f"{text!r} -> {observed}"
    elif kind == "all_eq":
        assert qs and all(q.op == "eq" for q in qs), f"{text!r} -> {observed}"
    elif kind == "three_cm":
        cm = [q for q in qs if q.unit_raw == "см"]
        assert len(cm) == 3, f"{text!r} -> {observed}"
    elif kind == "one_ordered_between":
        assert len(qs) == 1 and qs[0].b is not None and qs[0].a < qs[0].b, f"{text!r} -> {observed}"
    else:  # pragma: no cover - guards parametrization typos
        raise AssertionError(f"unknown case kind {kind!r}")


def test_size_triple_takes_names_stated_elsewhere():
    from src.ktru.engine.text import SIZE_ORDER_DOUBT, quantities
    text = "Стол, габариты 1200х750х450 мм, высота 750 мм, глубина 450 мм, ширина 1200 мм"
    triple = [q for q in quantities(text) if q.start < text.index(", высота")]
    assert [(q.property_raw, str(q.a), q.doubt) for q in triple] == [
        ("ширина", "1200", None), ("высота", "750", None), ("глубина", "450", None)]
    assert [text[q.start:q.end] for q in triple] == ["1200", "750", "450 мм"]
    unnamed = quantities("Стол, габариты 1200х750х450 мм")
    assert all(q.doubt == SIZE_ORDER_DOUBT for q in unnamed)
