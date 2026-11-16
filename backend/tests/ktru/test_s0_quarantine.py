"""Catalog defects stay visible but cannot become questions or automatic required fills."""

import json
import sqlite3
import uuid

import pytest

from src.ktru import contracts as C
from src.ktru.catalog.build import DDL
from src.ktru.catalog.reader import Catalog
from src.ktru.service.pipeline import Pipeline


def catalog(tmp_path, rows):
    with sqlite3.connect(tmp_path / "catalog.sqlite") as db:
        db.executescript(DDL)
        for index, characteristics in enumerate(rows, 1):
            code = f"hammer-{index}"
            db.execute(
                "INSERT INTO positions (code,version,name,status,is_template,family_id,domain,supply_units) "
                "VALUES (?,1,'Молоток','ACTIVE',0,'hammer','tools','[]')", (code,),
            )
            for char_code, name, kind, allowed in characteristics:
                db.execute("INSERT INTO characteristics VALUES (?,1,?,?,NULL,?,1,0,1,?)",
                           (code, char_code, name, kind, json.dumps(allowed)))
            db.execute("INSERT INTO positions_fts VALUES (?,1,'молоток',NULL,'масса молотка')", (code,))
    (tmp_path / "manifest.json").write_text(json.dumps({"snapshot_id": "quarantine", "counts": {"final": len(rows)}}))
    return Catalog(tmp_path)


def range_value(lo, hi):
    return {"kind": "range", "lo": str(lo), "hi": str(hi), "hi_inclusive": False,
            "okei_code": "163", "okei_name": "г"}


def confirm(pipeline):
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text="Молоток"))
    question = state.clarification
    if state.search_scope.coverage_status == "family_confirmed":
        return state  # the description named the family completely
    assert question is not None and question.kind == "family"
    return pipeline.answer(C.AnswerRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision, question_id=question.id, option_id=question.options[0].id))


def test_bad_range_never_becomes_question_and_unknown_candidate_counts(tmp_path):
    rows = [[("mass", "Масса молотка", "2", [range_value(lo, hi)])]
            for lo, hi in ((400, 600), (600, 800), (800, 400))]
    pipeline = Pipeline(catalog(tmp_path, rows))
    state = confirm(pipeline)
    question = state.clarification
    assert question is not None and question.kind == "characteristic"
    values = [option for option in question.options if not option.is_dont_know]
    assert len(values) == 2 and question.expected_remaining_worst == 2
    assert all(option.remaining_count == 2 for option in values)
    assert all(isinstance(option.answer, C.RangeValue) and option.answer.lo in ("400", "600") for option in values)
    answered = pipeline.answer(C.AnswerRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision, question_id=question.id, value=C.NumberValue(x="500", unit_okei="163")))
    assert {candidate.ref.code for candidate in answered.candidates if candidate.status == "alive"} == {
        "hammer-1", "hammer-3",
    }
    assert answered.final is None


@pytest.mark.parametrize("allowed", [
    [range_value(800, 400)],
    [{"kind": "number", "x": "NaN", "okei_code": "163"}],
    [{"kind": "raw", "raw_xml": "unparsed catalog value"}],
    [{"kind": "number", "x": "500", "okei_code": "bogus"}],
])
def test_invalid_fixed_required_value_cannot_fill_or_mark_ready(tmp_path, allowed):
    pipeline = Pipeline(catalog(tmp_path, [[("mass", "Масса молотка", "1", allowed)]]))
    state = confirm(pipeline)
    assert state.final is not None
    field = next(field for field in state.fields if field.label_ru == "Масса молотка")
    assert field.required and field.status == "needs_answer" and field.value is None
    assert state.readiness != "ready"
    assert any(diagnostic.kind == "schema_defect" for diagnostic in state.diagnostics)
    assert state.clarification is None


@pytest.mark.parametrize("char_codes", [(None,), ("duplicate", "duplicate")], ids=["missing", "duplicate"])
def test_required_invalid_characteristic_ref_is_not_filled_or_questioned(tmp_path, char_codes):
    rows = [[(code, f"Масса {index}", "1", [{"kind": "number", "x": "500", "okei_code": "163"}])
             for index, code in enumerate(char_codes)]]
    state = confirm(Pipeline(catalog(tmp_path, rows)))
    fields = [field for field in state.fields if field.required]
    assert len(fields) == len(char_codes)
    assert all(field.characteristic_ref is None and field.status == "needs_answer" for field in fields)
    assert state.readiness != "ready" and state.clarification is None



@pytest.mark.parametrize("allowed", [
    [range_value(800, 1000), {"kind": "enum", "label": "Не указан"}],
    [{**range_value(800, 1000), "okei_code": "bogus"}],
    [range_value(800, 1000), {**range_value(800, 1000), "okei_code": "003"}],
    [range_value(800, 1000), {"kind": "range", "lo": "800", "hi": "1000"}],
], ids=["mixed-domain", "unknown-unit", "mixed-dimensions", "missing-unit"])
def test_unusable_schema_stays_unknown_in_options_and_numeric_answers(tmp_path, allowed):
    rows = [
        [("mass", "Масса молотка", "2", [range_value(400, 600)])],
        [("mass", "Масса молотка", "2", [range_value(600, 800)])],
        [("mass", "Масса молотка", "2", allowed)],
    ]
    pipeline = Pipeline(catalog(tmp_path, rows))
    state = confirm(pipeline)
    question = state.clarification
    assert question is not None
    values = [option for option in question.options if not option.is_dont_know]
    assert len(values) == 2 and question.expected_remaining_worst == 2
    assert all(isinstance(option.answer, C.RangeValue) and option.remaining_count == 2 for option in values)
    answered = pipeline.answer(C.AnswerRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision, question_id=question.id, value=C.NumberValue(x="500", unit_okei="163")))
    assert {candidate.ref.code for candidate in answered.candidates if candidate.status == "alive"} == {
        "hammer-1", "hammer-3",
    }
    assert answered.final is None
