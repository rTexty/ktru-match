"""S0 behavior regressions on a real, small SQLite catalog (no model or large snapshot needed)."""

import json
import sqlite3
import threading
import uuid

import pytest
from pydantic import ValidationError

from src.ktru import contracts as C
from src.ktru.catalog.build import DDL
from src.ktru.catalog.reader import Catalog
from src.ktru.engine.text import OKEI, UNITS, norm
from src.ktru.ml import extract as ml
from src.ktru.service.pipeline import Pipeline, ServiceError


@pytest.fixture
def catalog(tmp_path):
    db = sqlite3.connect(tmp_path / "catalog.sqlite")
    db.executescript(DDL)
    for index, (lo, hi) in enumerate(((400, 600), (600, 800)), 1):
        code = f"hammer-{index}"
        db.execute(
            "INSERT INTO positions (code,version,name,status,is_template,family_id,domain,supply_units) "
            "VALUES (?,1,'Молоток','ACTIVE',0,'hammer','tools','[]')", (code,),
        )
        allowed = [{"kind": "range", "lo": str(lo), "hi": str(hi), "hi_inclusive": False,
                    "okei_code": "163", "okei_name": "г", "value_code": f"v{index}"}]
        db.execute("INSERT INTO characteristics VALUES (?,1,'mass','Масса молотка',NULL,'2',1,0,1,?)",
                   (code, json.dumps(allowed)))
        db.execute("INSERT INTO positions_fts VALUES (?,1,'молоток',NULL,'масса молотка')", (code,))
    db.commit()
    db.close()
    (tmp_path / "manifest.json").write_text(json.dumps({"snapshot_id": "test-s0", "counts": {"final": 2}}))
    return Catalog(tmp_path)


@pytest.fixture
def pipeline(catalog):
    return Pipeline(catalog)


@pytest.fixture
def material_pipeline(tmp_path):
    """Two bodies have different materials; neither schema describes the handle."""
    db = sqlite3.connect(tmp_path / "catalog.sqlite")
    db.executescript(DDL)
    for index, material in enumerate(("Деревянный", "Металлический"), 1):
        code = f"body-{index}"
        db.execute(
            "INSERT INTO positions (code,version,name,status,is_template,family_id,domain,supply_units) "
            "VALUES (?,1,'Молоток','ACTIVE',0,'hammer','tools','[]')", (code,),
        )
        allowed = [{"kind": "enum", "label": material, "value_code": f"material-{index}"}]
        db.execute("INSERT INTO characteristics VALUES (?,1,'material','Материал',NULL,'2',1,0,1,?)",
                   (code, json.dumps(allowed)))
        db.execute("INSERT INTO positions_fts VALUES (?,1,'молоток',NULL,'материал')", (code,))
    db.commit()
    db.close()
    (tmp_path / "manifest.json").write_text(json.dumps({"snapshot_id": "test-material", "counts": {"final": 2}}))
    return Pipeline(Catalog(tmp_path))


def confirm_family(pipeline, state, request_id="family"):
    """Answer the family question when one is asked; a fully named family is already confirmed."""
    question = state.clarification
    if question is None or question.kind != "family":
        return state
    return pipeline.answer(C.AnswerRequest(session_id=state.session_id, request_id=request_id,
        expected_revision=state.revision, question_id=question.id, option_id=question.options[0].id))


def parse(pipeline, text):
    return pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))


def analyze(pipeline, state, facts, request_id=None):
    request = C.AnalyzeRequest(session_id=state.session_id, request_id=request_id or uuid.uuid4().hex,
                               expected_revision=state.revision)
    return pipeline.analyze(request, extractor=lambda _: facts)


def test_unknown_family_preserves_open_facts_and_rejections(pipeline):
    text = "Ксилофон, масса 500 г, покрытие водоотталкивающее"
    state = parse(pipeline, text)
    result = analyze(pipeline, state, [
        {"property": "Покрытие", "value": "водоотталкивающее", "quote": "покрытие водоотталкивающее"},
        {"property": "Цвет", "value": "красный", "quote": "красный"},
    ])
    assert result.final is None
    assert any("водоотталкива" in a.value_raw for a in result.assertions)
    assert any(isinstance(a.value, C.NumberValue) for a in result.assertions)
    assert result.fields and result.rejected_outputs


def test_overlapping_quote_does_not_drop_other_property(pipeline):
    text = "Молоток, масса 500 г и покрытие водоотталкивающее"
    result = analyze(pipeline, parse(pipeline, text), [{
        "property": "Покрытие", "value": "водоотталкивающее", "quote": "масса 500 г и покрытие водоотталкивающее",
    }])
    assert any(a.property_raw == "Покрытие" for a in result.assertions)
    assert any("водоотталкива" in f.display_ru for f in result.fields)


def test_equivalent_mass_merges_sources_without_conflict(pipeline):
    state = parse(pipeline, "Молоток, масса молотка 500 г, масса молотка 0,5 кг")
    mass = [a for a in state.assertions if isinstance(a.value, C.NumberValue) and a.subject_kind == "item"]
    assert len(mass) == 1
    assert len(mass[0].evidence) == 2
    assert not any(d.kind == "contradiction" for d in state.diagnostics)


def test_distinct_values_survive_as_conflict(pipeline):
    state = parse(pipeline, "Молоток, масса молотка 500 г, масса молотка 700 г")
    assert any(d.kind == "contradiction" for d in state.diagnostics)
    assert sum(c.status == "alive" for c in state.candidates) == 2
    assert len([a for a in state.assertions if isinstance(a.value, C.NumberValue)]) == 2


def test_package_and_part_mass_never_bind_to_item(pipeline):
    state = parse(pipeline, "Молоток, масса упаковки 700 г, масса бойка 500 г")
    numbers = [a for a in state.assertions if isinstance(a.value, C.NumberValue)]
    assert {a.subject_kind for a in numbers} == {"package", "part"}
    assert sum(c.status == "alive" for c in state.candidates) == 2
    assert not any(d.kind == "contradiction" for d in state.diagnostics)


@pytest.mark.parametrize("value,unit,operator", [("600", "г", "eq"), ("500", "кг", "eq"), ("500", "г", "ge")])
def test_ground_rejects_number_unit_or_operator_invention(value, unit, operator):
    kept, rejected = ml.ground([{"property": "Масса", "value": value, "unit": unit, "operator": operator,
                                "subject": "item", "quote": "масса 500 г"}], "Молоток, масса 500 г")
    assert not kept and rejected


def test_ground_accepts_equivalent_units():
    kept, rejected = ml.ground([{"property": "Масса", "value": "0.5", "unit": "кг",
                                "quote": "масса 500 г"}], "Молоток, масса 500 г")
    assert kept and not rejected


def test_ground_keeps_the_text_subject_over_the_model_label():
    # A model labelling the package mass as the item's does not make it the item's: the source relation decides.
    kept, rejected = ml.ground([{"property": "Масса", "value": "600", "unit": "г", "subject": "item",
                                "quote": "масса упаковки 600 г"}], "Молоток, масса упаковки 600 г")
    assert not rejected and kept[0]["subject_ref"]["kind"] == "package"


def test_ground_repeated_quote_requires_offsets():
    text = "масса 500 г; масса 500 г"
    fact = {"property": "Масса", "value": "500", "unit": "г", "quote": "масса 500 г"}
    kept, rejected = ml.ground([fact], text)
    assert not kept and rejected
    start = text.rindex(fact["quote"])
    kept, rejected = ml.ground([{**fact, "start": start, "end": len(text)}], text)
    assert len(kept) == 1 and not rejected and kept[0]["start"] == start


def test_ground_does_not_invert_negation():
    kept, rejected = ml.ground([{"property": "Материал", "value": "металл", "negated": False,
                                "quote": "не металл"}], "Молоток, не металл")
    assert not kept and rejected


def test_truncated_search_requires_confirmation(pipeline, catalog, monkeypatch):
    original = catalog.search
    monkeypatch.setattr(catalog, "search", lambda text: (original(text)[0], 400))
    state = parse(pipeline, "Молоток, масса молотка 500 г")
    assert state.final is None and state.suggestion is not None
    assert state.search_scope.retrieval_truncated
    question = state.clarification
    assert question and question.kind == "family"
    result = confirm_family(pipeline, state, "family-answer")
    assert result.final and result.final.conditional_on_family
    assert result.final.scope.coverage_status == "family_confirmed"


def test_parse_is_provisional(pipeline):
    assert parse(pipeline, "Молоток, масса 500 г").status == "provisional"


def test_analyze_idempotency_checks_payload(pipeline):
    state = parse(pipeline, "Молоток, масса 500 г")
    request = C.AnalyzeRequest(session_id=state.session_id, request_id="analyze-once", expected_revision=state.revision)
    first = pipeline.analyze(request, extractor=lambda _: [])
    assert pipeline.analyze(request, extractor=lambda _: pytest.fail("must not call twice")) == first
    with pytest.raises(ServiceError) as error:
        pipeline.analyze(request.model_copy(update={"expected_revision": first.revision}), extractor=lambda _: [])
    assert error.value.body.code == "request_conflict"


def test_late_analyze_does_not_publish(pipeline):
    state = parse(pipeline, "Молоток, масса 500 г")
    started, release = threading.Event(), threading.Event()
    errors = []
    def extractor(_):
        started.set()
        assert release.wait(5)
        return []
    def run():
        try:
            pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id="slow",
                                             expected_revision=state.revision), extractor=extractor)
        except ServiceError as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(5)
        newer = pipeline.parse(C.ParseRequest(session_id=state.session_id, request_id="replace",
                               expected_revision=state.revision, text="Молоток, масса 700 г"))
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and errors and errors[0].body.code == "stale_revision"
    assert pipeline.get(state.session_id) == newer


@pytest.mark.parametrize("model,values", [
    (C.NumberValue, {"x": "NaN"}), (C.NumberValue, {"x": "Infinity"}),
    (C.RangeValue, {"lo": "2", "hi": "1"}),
    (C.RangeValue, {"lo": "1", "hi": "1", "hi_inclusive": False}),
])
def test_contract_rejects_invalid_decimal_values(model, values):
    with pytest.raises(ValidationError):
        model(**values)


def test_ground_rejects_numeric_property_invention():
    kept, rejected = ml.ground([{
        "property": "Ширина", "value": "80", "unit": "мм", "subject": "item", "quote": "длина 80 мм",
    }], "Молоток, длина 80 мм")
    assert not kept and rejected
    assert rejected[0]["reason_ru"]


def test_ground_rejects_negated_number_as_positive():
    kept, rejected = ml.ground([{
        "property": "Масса", "value": "500", "unit": "г", "subject": "item", "negated": False,
        "quote": "масса не 500 г",
    }], "Молоток, масса не 500 г")
    assert not kept and rejected


def test_ground_checks_later_numeric_relations():
    quote = "ширина 80 мм, длина 80 мм"
    kept, rejected = ml.ground([{"property": "Длина", "value": "80", "unit": "мм", "quote": quote}], quote)
    assert kept and not rejected


def test_ground_does_not_cross_text_property_relations():
    quote = "Материал дерево и покрытие металл"
    kept, _ = ml.ground([{"property": "Материал", "value": "металл", "quote": quote}], quote)
    assert not kept or kept[0]["status"] != "supported"


def test_ground_uses_subject_of_matching_number():
    quote = "масса упаковки 200 г, масса товара 500 г"
    kept, rejected = ml.ground([{"property": "Масса товара", "value": "500", "unit": "г",
                                "subject": "item", "quote": quote}], quote)
    assert kept and not rejected
    assert "упаковки" not in kept[0]["subject_context"]


def test_unit_fix_preserves_property(pipeline):
    state = parse(pipeline, "Молоток, масса молотка 500 кг")
    fix = next(fix for diagnostic in state.diagnostics for fix in diagnostic.fixes
               if "500 г" in fix.label_ru)
    assert isinstance(fix.patch, C.ReplaceSpanPatch)
    patch = fix.patch
    corrected = state.text[:patch.span.start] + patch.replacement + state.text[patch.span.end:]
    assert "масса молотка" in corrected


def test_parallel_duplicate_analysis_is_bounded(pipeline):
    state = parse(pipeline, "Молоток, масса молотка 500 г")
    request = C.AnalyzeRequest(session_id=state.session_id, request_id="duplicate-model", expected_revision=state.revision)
    started, release = threading.Event(), threading.Event()
    calls, results = [], []
    def extractor(_):
        calls.append(1)
        started.set()
        assert release.wait(3)
        return []
    thread = threading.Thread(target=lambda: results.append(pipeline.analyze(request, extractor=extractor)))
    thread.start()
    try:
        assert started.wait(3)
        with pytest.raises(ServiceError) as error:
            pipeline.analyze(request, extractor=lambda _: calls.append(1) or [])
        assert error.value.status == 503 and error.value.body.retryable
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and len(calls) == 1
    assert pipeline.analyze(request, extractor=lambda _: pytest.fail("must use cache")) == results[0]


def test_numeric_question_has_typed_options_and_bindings(pipeline):
    state = parse(pipeline, "Молоток")
    state = confirm_family(pipeline, state, "confirm-numeric-family")
    question = state.clarification
    assert question and question.char_name == "Масса молотка"
    assert question.accepts_value and question.value_unit_okei == "163"
    choices = [option for option in question.options if not option.is_dont_know]
    assert all(isinstance(option.answer, C.RangeValue) and option.effects for option in choices)
    assert all(effect.characteristic_ref.snapshot_id == state.snapshot_id for option in choices for effect in option.effects)
    result = pipeline.answer(C.AnswerRequest(session_id=state.session_id, request_id="numeric-answer",
        expected_revision=state.revision, question_id=question.id, value=C.NumberValue(x="500", unit_okei="163", unit_name="г")))
    assert {candidate.ref.code for candidate in result.candidates if candidate.status == "alive"} == {"hammer-1"}
    assertion = next(assertion for assertion in result.assertions if assertion.origin == "user_answer")
    assert assertion.evidence and isinstance(assertion.evidence[0], C.UserEvidence)


@pytest.mark.parametrize("value", ["1e9999999", "1e-9999999", "0e9999999"])
def test_numeric_contract_bounds_exponents(value):
    with pytest.raises(ValidationError):
        C.NumberValue(x=value)


def test_numeric_contract_bounds_unit_strings():
    with pytest.raises(ValidationError):
        C.NumberValue(x="500", unit_name="г" * 20000)


@pytest.mark.parametrize("code", list(OKEI))
def test_every_supported_canonical_unit_is_accepted_by_typed_answer(code):
    assert UNITS[norm(OKEI[code][2])][0] == code


def test_analyze_timings_include_model_and_resolution(pipeline, monkeypatch):
    from src.ktru.service import pipeline as service

    state = parse(pipeline, "Молоток, масса 500 г")
    clock = [100.0]
    original = service.resolve

    def measured_resolve(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] += .05
        return result

    def measured_model(_):
        clock[0] += .2
        return []

    monkeypatch.setattr(service.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(service, "resolve", measured_resolve)
    result = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id="timings",
        expected_revision=state.revision), extractor=measured_model)
    assert result.timings_ms.extract == 200 and result.timings_ms.resolve == 50
    assert result.timings_ms.total == 250


def test_numeric_question_uses_recognized_canonical_unit(pipeline, catalog):
    original = catalog.family

    def long_unit_name(*args, **kwargs):
        template, positions = original(*args, **kwargs)
        for position in positions:
            for characteristic in position.chars:
                characteristic.allowed = [{**value, "okei_name": "Грамм"} for value in characteristic.allowed]
        return template, positions

    catalog.family = long_unit_name
    state = parse(pipeline, "Молоток")
    question = state.clarification
    state = confirm_family(pipeline, state, "canonical-family")
    question = state.clarification
    assert question.value_unit_okei == "163" and question.value_unit_name == "г"
    result = pipeline.answer(C.AnswerRequest(session_id=state.session_id, request_id="canonical-value",
        expected_revision=state.revision, question_id=question.id,
        value=C.NumberValue(x="500", unit_okei=question.value_unit_okei, unit_name=question.value_unit_name)))
    assert result.final and result.final.position_ref.code == "hammer-1"


def test_numeric_option_counts_include_overlapping_catalog_intervals(pipeline, catalog):
    with sqlite3.connect(catalog.dir / "catalog.sqlite") as db:
        allowed = [{"kind": "range", "lo": "500", "hi": "800", "hi_inclusive": False,
                    "okei_code": "163", "okei_name": "г", "value_code": "overlap"}]
        db.execute("UPDATE characteristics SET allowed=? WHERE position_code='hammer-2'", (json.dumps(allowed),))
    state = parse(pipeline, "Молоток")
    question = state.clarification
    state = confirm_family(pipeline, state, "overlap-family")
    question = state.clarification
    assert question.expected_remaining_worst == 2
    assert all(option.remaining_count == 2 for option in question.options)
    assert "пересекаться" in question.why_ru


@pytest.mark.parametrize("code,name,ok", [(None, "кг", True), ("999", None, False),
                                         ("163", "кг", False), ("003", "мм", False)])
def test_typed_answer_resolves_and_validates_units(pipeline, code, name, ok):
    state = parse(pipeline, "Молоток")
    q = state.clarification
    state = confirm_family(pipeline, state, "family-unit")
    q = state.clarification
    request = C.AnswerRequest(session_id=state.session_id, request_id="answer-unit", expected_revision=state.revision,
        question_id=q.id, value=C.NumberValue(x="0.5", unit_okei=code, unit_name=name))
    if ok:
        result = pipeline.answer(request)
        assert {c.ref.code for c in result.candidates if c.status == "alive"} == {"hammer-1"}
    else:
        with pytest.raises(ServiceError) as error:
            pipeline.answer(request)
        assert error.value.status == 422


def test_unsupported_catalog_value_keeps_question(material_pipeline):
    state = parse(material_pipeline, "Молоток, материал пластик")
    state = analyze(material_pipeline, state, [{"property": "Материал", "value": "пластик", "quote": "материал пластик"}])
    state = confirm_family(material_pipeline, state, "unsupported-family")
    assert state.clarification and state.clarification.char_name == "Материал"
    assert any(d.kind == "value_not_allowed" for d in state.diagnostics)
    assert state.readiness != "ready"


def test_schema_rules_do_not_cross_property_relations(material_pipeline):
    state = parse(material_pipeline, "Молоток, материал дерево, покрытие металл")
    assert {c.ref.code for c in state.candidates if c.status == "alive"} == {"body-1"}
    assert not any(d.kind == "contradiction" for d in state.diagnostics)


@pytest.mark.parametrize("gross", ["500", "700"])
def test_net_and_gross_mass_keep_distinct_meanings_without_conflict(pipeline, gross):
    state = parse(pipeline, f"Молоток, масса нетто 500 г, масса брутто {gross} г")
    numbers = [a for a in state.assertions if isinstance(a.value, C.NumberValue) and a.status != "rejected"]
    assert len(numbers) == 2
    # Net mass belongs to the item, gross mass to its package: the subject keeps the two meanings apart.
    assert {a.subject.kind for a in numbers} == {"item", "package"}
    assert not any(d.kind == "contradiction" for d in state.diagnostics)
    assert not state.conflicts


def test_handle_material_does_not_become_item_material(material_pipeline):
    state = parse(material_pipeline, "Молоток, рукоятка деревянная")
    assert {candidate.ref.code for candidate in state.candidates if candidate.status == "alive"} == {
        "body-1", "body-2",
    }
    assert not any(field.label_ru == "Материал" and field.status in ("from_text", "confirmed")
                   for field in state.fields)
    assert not any(binding.characteristic_ref and binding.characteristic_ref.char_code == "material"
                   and binding.relation == "exact" for binding in state.bindings)
    assert any(a.span and "рукоятка деревянная" in a.span.text for a in state.assertions)


@pytest.mark.parametrize("text", ["Молоток, покрытие деревянное", "Молоток, материал покрытия деревянный",
                                  "Молоток, футляр деревянный", "Молоток, цвет деревянный"])
def test_schema_label_does_not_reassign_other_relation_to_body_material(material_pipeline, text):
    state = parse(material_pipeline, text)
    state = confirm_family(material_pipeline, state, "coating-family")
    assert {candidate.ref.code for candidate in state.candidates if candidate.status == "alive"} == {"body-1", "body-2"}
    assert state.final is None


@pytest.mark.parametrize("text,good,bad", [
    ("масса 0,5 кг - 800 г", "500-800", "0.5-800"),
    ("масса 500 г - 0,8 кг", "0.5-0.8", "0.8-500"),
])
def test_ground_normalizes_both_units_in_source_range(text, good, bad):
    unit = "г" if "800 г" in text else "кг"
    kept, rejected = ml.ground([{"property": "Масса", "value": good, "unit": unit, "quote": text}], text)
    assert kept and not rejected
    kept, rejected = ml.ground([{"property": "Масса", "value": bad, "unit": unit, "quote": text}], text)
    assert not kept and rejected


def test_item_coating_fact_is_preserved_without_becoming_body_material():
    text = "Молоток, покрытие водоотталкивающее"
    kept, rejected = ml.ground([{"property": "Покрытие", "value": "водоотталкивающее", "subject": "item",
                                "quote": "покрытие водоотталкивающее"}], text)
    assert kept and not rejected


@pytest.mark.parametrize("value", ["500 футов", "500 рублей", "500 В", "500"])
def test_source_without_valid_mass_unit_does_not_filter_mass(pipeline, value):
    state = parse(pipeline, f"Молоток, масса молотка {value}")
    state = confirm_family(pipeline, state, "unknown-source-unit")
    assert {candidate.ref.code for candidate in state.candidates if candidate.status == "alive"} == {"hammer-1", "hammer-2"}
    assert state.final is None


@pytest.mark.parametrize("value", ["500 футов", "500 рублей", "500 г и 700 г"])
def test_numeric_edit_preserves_invalid_or_multiple_value(pipeline, value):
    state = parse(pipeline, "Молоток, масса молотка 700 г")
    state = confirm_family(pipeline, state, "unknown-edit-unit-family")
    target = next(field for field in state.fields if field.label_ru == "Масса молотка")
    state = pipeline.edit(C.EditRequest(session_id=state.session_id, request_id="unknown-edit-unit",
        expected_revision=state.revision,
        patch=C.SetFieldPatch(label_ru=target.label_ru, field_id=target.id, value=value)))
    assert state.readiness != "ready" and state.final is None
    assert any(assertion.origin == "user_edit" and assertion.value_raw == value for assertion in state.assertions)


def test_unknown_family_edit_preserves_subject_and_user_evidence(pipeline):
    state = parse(pipeline, "Ксилофон, масса упаковки 700 г")
    target = next(field for field in state.fields if isinstance(field.value, C.NumberValue))
    assert target.subject and target.subject.kind == "package"
    result = pipeline.edit(C.EditRequest(
        session_id=state.session_id, request_id="unknown-family-edit", expected_revision=state.revision,
        patch=C.SetFieldPatch(label_ru=target.label_ru, field_id=target.id, value="800 г"),
    ))
    edited = [a for a in result.assertions if a.origin == "user_edit"]
    assert len(edited) == 1
    assertion = edited[0]
    assert isinstance(assertion.value, C.NumberValue) and assertion.value.x == "800"
    assert assertion.subject and assertion.subject.id == target.subject.id
    evidence = [item for item in assertion.evidence if isinstance(item, C.UserEvidence)]
    assert len(evidence) == 1 and evidence[0].type == "user_edit"
    assert (evidence[0].field_id, evidence[0].revision) == (target.id, result.revision)
    field = next(field for field in result.fields if assertion.id in field.assertion_ids)
    assert field.status == "confirmed" and field.subject.id == target.subject.id
    assert evidence[0] in field.evidence


@pytest.mark.parametrize("value,operator,code,value_type", [
    ("600–700 г", "between", "hammer-2", C.RangeValue),
    ("не менее 600 г", "ge", "hammer-2", C.NumberValue),
    ("не более 500 г", "le", "hammer-1", C.NumberValue),
])
def test_edit_preserves_range_or_bound_operator(pipeline, value, operator, code, value_type):
    state = parse(pipeline, "Молоток, масса молотка 500 г")
    target = next(field for field in state.fields if field.label_ru == "Масса молотка")
    result = pipeline.edit(C.EditRequest(
        session_id=state.session_id, request_id="quantity-edit", expected_revision=state.revision,
        patch=C.SetFieldPatch(label_ru=target.label_ru, field_id=target.id, value=value),
    ))
    assertion = next(a for a in result.assertions if a.origin == "user_edit")
    assert assertion.operator == operator and isinstance(assertion.value, value_type)
    if isinstance(assertion.value, C.RangeValue):
        assert (assertion.value.lo, assertion.value.hi) == ("600", "700")
    assert {candidate.ref.code for candidate in result.candidates if candidate.status == "alive"} == {code}


def test_concurrent_initial_parse_replay_never_caches_empty_session(pipeline, monkeypatch):
    """Pause the first request after reservation so a retry can reach the same session before publication."""
    first_reserved, release_first, retry_done = threading.Event(), threading.Event(), threading.Event()
    original_run = pipeline._run
    results, errors = [], []

    def interleave(*args, **kwargs):
        if threading.current_thread().name == "initial-parse":
            first_reserved.set()
            assert release_first.wait(3)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_run", interleave)
    request = C.ParseRequest(request_id="concurrent-new", expected_revision=0, text="Молоток, масса 500 г")

    def run(retry=False):
        try:
            results.append(pipeline.parse(request))
        except Exception as error:
            errors.append(error)
        finally:
            if retry:
                retry_done.set()

    first = threading.Thread(target=run, name="initial-parse", daemon=True)
    second = threading.Thread(target=lambda: run(retry=True), name="parse-retry", daemon=True)
    first.start()
    try:
        assert first_reserved.wait(3)
        second.start()
        # A correct implementation may serialize the retry until the initial request publishes.
        retry_done.wait(0.5)
    finally:
        release_first.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert not errors and len(results) == 2
    assert results[0].session_id == results[1].session_id
    assert all(result.text == request.text and result.revision == 1 for result in results)
    assert pipeline.get(results[0].session_id) == results[0] == results[1]
