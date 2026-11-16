"""MVP checks: deterministic text rules, seed smoke cases on the real snapshot, and session state transitions.

Seed cases are dev smoke tests the rules were tuned against; they are not held-out evidence.
Run: cd backend && uv run pytest tests/ktru -q   (needs a built snapshot, see README)
"""

import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from src.ktru import contracts as C
from src.ktru.engine.resolve import Interval
from src.ktru.engine.text import fix_layout, norm, quantities

ROOT = Path(__file__).resolve().parents[3]
SNAP = Path(os.environ.get("KTRU_SNAPSHOT_DIR", ROOT / ".ktru-build" / "snapshots" / "current"))
SEEDS = [json.loads(line) for line in (ROOT / "eval" / "cases" / "seed.jsonl").read_text().splitlines() if line]
needs_snapshot = pytest.mark.skipif(not (SNAP / "manifest.json").exists(), reason="snapshot not built")


# ---- pure rules ---------------------------------------------------------------------------------------------------


def test_quantity_keeps_operator_and_exact_decimal():
    (q,) = quantities("ширина не менее 500 мм")
    assert (q.op, q.a, q.unit_okei) == ("ge", Decimal(500), "003")
    (big,) = quantities("длина 9007199254740993 мм")
    assert big.a == Decimal("9007199254740993")


def test_decimal_comma_range_is_one_clause():
    (q,) = quantities("масса 0,5–0,7 кг")
    assert (q.op, q.a, q.b, q.unit_raw) == ("between", Decimal("0.5"), Decimal("0.7"), "кг")


def test_model_numbers_are_not_quantities():
    assert quantities("картридж A4 модель 3M") == []


def test_boundary_inclusivity():
    first = Interval(Decimal("400"), True, Decimal("600"), False, "mass")
    second = Interval(Decimal("600"), True, Decimal("800"), False, "mass")
    point = Interval(Decimal("600"), True, Decimal("600"), True, "mass")
    assert not first.overlaps(point) and second.overlaps(point)


def test_layout_fix_only_for_wrong_layout():
    assert fix_layout("Cnek yf vtnfkkbxtcrjv rfhrfct") == "Стул на металлическом каркасе"
    assert fix_layout("Стул офисный") is None


# ---- real snapshot ------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pipeline():
    from src.ktru.catalog.reader import Catalog
    from src.ktru.service.pipeline import Pipeline

    return Pipeline(Catalog(SNAP))


def _parse(p, text):
    return p.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))


def _answer(p, st, label):
    q = st.clarification
    opt = next(o for o in q.options if norm(o.label_ru) == norm(label))
    return p.answer(
        C.AnswerRequest(
            session_id=st.session_id,
            request_id=f"a{st.revision}",
            expected_revision=st.revision,
            question_id=q.id,
            option_id=opt.id,
        )
    )


@needs_snapshot
@pytest.mark.parametrize("case", SEEDS, ids=[c["id"] for c in SEEDS])
def test_seed_case(pipeline, case):
    st = _parse(pipeline, case["text"])
    # Explicit gold family confirmation is part of the user scenario, separate from lexical ranking.
    expected = case["expect"]
    gold = (expected.get("final") or {}).get("code") or next(iter(expected.get("alive_codes_superset", [])), None)
    if gold and st.clarification and st.clarification.kind == "family":
        family_id = pipeline.catalog.position(gold).family_id
        index = next(i for i, family in enumerate(st.families_top) if family.id == family_id)
        q = st.clarification
        st = pipeline.answer(C.AnswerRequest(session_id=st.session_id, request_id=uuid.uuid4().hex,
            expected_revision=st.revision, question_id=q.id, option_id=q.options[index].id))
    for ans in case["answers"]:
        assert st.clarification and norm(st.clarification.char_name or "") == norm(ans["char_name"])
        st = _answer(pipeline, st, ans["option"])
    e = case["expect"]
    final = st.final.position_ref.code if st.final else None
    if "final" in e:
        assert final == (e["final"]["code"] if e["final"] else None)
        if e["final"]:
            assert st.final.position_ref.version == e["final"]["version"]
    if e.get("must_not_finalize"):
        assert final is None
    alive = {c.ref.code for c in st.candidates if c.status == "alive"}
    assert set(e.get("alive_codes_superset", [])) <= alive
    if "readiness" in e:
        assert st.readiness == e["readiness"]
    kinds = {d.kind for d in st.diagnostics} | {c.kind for c in st.corrections}  # Text fixes are applied up front.
    for d in e.get("diagnostics", []):
        assert d["kind"] in kinds
    for name in e.get("missing_required_names", []):
        assert any(f.label_ru == name and f.status == "needs_answer" for f in st.fields)
    for forbidden in e.get("forbidden", []):
        name = forbidden.get("char_name")
        assert not any(f.label_ru == name and f.status in ("from_text", "confirmed") for f in st.fields)


@needs_snapshot
def test_session_cas_idempotency_and_undo(pipeline):
    from src.ktru.service.pipeline import ServiceError

    st = _parse(pipeline, "Стул на металлическом каркасе")
    q = st.clarification
    opt = q.options[0]
    req = C.AnswerRequest(
        session_id=st.session_id, request_id="x1", expected_revision=0, question_id=q.id, option_id=opt.id
    )
    with pytest.raises(ServiceError) as stale:
        pipeline.answer(req)
    assert stale.value.status == 409 and stale.value.body.code == "stale_revision"

    ok = pipeline.answer(req.model_copy(update={"expected_revision": 1}))
    again = pipeline.answer(req.model_copy(update={"expected_revision": 1}))
    assert ok.revision == again.revision == 2  # same request id + payload → cached result

    with pytest.raises(ServiceError) as conflict:
        pipeline.answer(req.model_copy(update={"expected_revision": 2}))
    assert conflict.value.body.code == "request_conflict"

    with pytest.raises(ServiceError):  # old question id at the new revision
        pipeline.answer(req.model_copy(update={"request_id": "x2", "expected_revision": 2}))

    undone = pipeline.undo(C.UndoRequest(session_id=st.session_id, request_id="u1", expected_revision=2))
    assert undone.revision == 3 and undone.answers_count == 0 and not undone.can_undo

    with pytest.raises(ServiceError) as expired:
        pipeline.get("missing")
    assert expired.value.status == 404
