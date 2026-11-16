"""Source and count contracts use a small catalog with no model service."""

import json
import sqlite3
import uuid

import pytest
from pydantic import ValidationError

from src.ktru import contracts as C
from src.ktru.catalog.build import DDL
from src.ktru.catalog.reader import Catalog
from src.ktru.service.pipeline import Pipeline


@pytest.fixture
def evidence_catalog(tmp_path):
    with sqlite3.connect(tmp_path / "catalog.sqlite") as db:
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
    (tmp_path / "manifest.json").write_text(json.dumps({"snapshot_id": "contract-evidence", "counts": {"final": 2}}))
    return Catalog(tmp_path)


def confirm_family(pipeline, state, request_id="family"):
    """Answer the family question when one is asked; a fully named family is already confirmed."""
    question = state.clarification
    if question is None or question.kind != "family":
        return state
    return pipeline.answer(C.AnswerRequest(session_id=state.session_id, request_id=request_id,
        expected_revision=state.revision, question_id=question.id, option_id=question.options[0].id))


def parse(catalog, text):
    return Pipeline(catalog).parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))


def assert_sources(state, diagnostic):
    by_id = {assertion.id: assertion for assertion in state.assertions}
    assert diagnostic.evidence
    assert all(ident in by_id for ident in diagnostic.assertion_ids)
    if diagnostic.subject:
        assert any(subject.id == diagnostic.subject.id for subject in state.subjects)
        assert all(by_id[ident].subject and by_id[ident].subject.id == diagnostic.subject.id
                   for ident in diagnostic.assertion_ids)
    for item in diagnostic.evidence:
        if isinstance(item, C.TextEvidence):
            span = item.span
            assert state.text[span.start:span.end] == span.text
            assert span.text_revision == state.revision
    for ref in diagnostic.characteristic_refs:
        assert ref.snapshot_id == state.snapshot_id
        assert any(isinstance(item, C.CatalogEvidence) and item.characteristic_ref == ref
                   for item in diagnostic.evidence)


def test_conflict_diagnostic_carries_all_input_sources_and_real_characteristics(evidence_catalog):
    state = parse(evidence_catalog, "Молоток, масса молотка 500 г, масса молотка 700 г")
    diagnostic = next(item for item in state.diagnostics if item.kind == "contradiction")
    assert_sources(state, diagnostic)
    assert diagnostic.subject and diagnostic.subject.kind == "item"
    assert {ref.position_code for ref in diagnostic.characteristic_refs} == {"hammer-1", "hammer-2"}
    assertions = [assertion for assertion in state.assertions if assertion.id in diagnostic.assertion_ids]
    assert len(assertions) == 2
    assert all(item in diagnostic.evidence for assertion in assertions for item in assertion.evidence)
    assert all(candidate.status == "alive" for candidate in state.candidates)


def test_implausible_diagnostic_keeps_source_and_declared_value(evidence_catalog):
    state = parse(evidence_catalog, "Молоток, масса молотка 500 кг")
    diagnostic = next(item for item in state.diagnostics if item.kind == "implausible")
    assert_sources(state, diagnostic)
    assert diagnostic.subject and diagnostic.characteristic_refs
    assert all(candidate.status == "alive" for candidate in state.candidates)
    assert any(assertion.value_raw == "500 кг" for assertion in state.assertions)


@pytest.mark.parametrize("char_code", ["mass", None])
def test_schema_diagnostic_uses_catalog_source_without_fake_reference(evidence_catalog, char_code):
    with sqlite3.connect(evidence_catalog.dir / "catalog.sqlite") as db:
        db.execute("UPDATE characteristics SET kind_raw='unknown',char_code=? WHERE position_code='hammer-2'",
                   (char_code,))
    state = parse(evidence_catalog, "Молоток, масса молотка 500 г")
    diagnostic = next(item for item in state.diagnostics if item.kind == "schema_defect")
    assert_sources(state, diagnostic)
    assert diagnostic.subject is None and diagnostic.assertion_ids == []
    assert [ref.char_code for ref in diagnostic.characteristic_refs] == (["mass"] if char_code else [])
    assert any(isinstance(item, C.CatalogEvidence) and item.position_ref.code == "hammer-2"
               for item in diagnostic.evidence)


def test_ambiguous_catalog_values_carry_input_and_field_sources(evidence_catalog):
    with sqlite3.connect(evidence_catalog.dir / "catalog.sqlite") as db:
        allowed = [{"kind": "range", "lo": lo, "hi": hi, "okei_code": "163", "value_code": code}
                   for lo, hi, code in (("400", "600", "a"), ("500", "700", "b"))]
        db.execute("UPDATE characteristics SET allowed=? WHERE position_code='hammer-1'", (json.dumps(allowed),))
    pipeline = Pipeline(evidence_catalog)
    state = pipeline.parse(C.ParseRequest(request_id="ambiguous", expected_revision=0,
        text="Молоток, масса молотка 500 г"))
    state = confirm_family(pipeline, state, "confirm")
    diagnostic = next(item for item in state.diagnostics if item.kind == "ambiguous")
    assert_sources(state, diagnostic)
    assert diagnostic.assertion_ids and diagnostic.subject and diagnostic.characteristic_refs


@pytest.mark.parametrize("tamper", ["assertion", "subject", "span", "revision", "catalog_ref",
                                   "assertion_span", "assertion_evidence", "subject_evidence"])
def test_card_contract_rejects_false_diagnostic_links(evidence_catalog, tamper):
    state = parse(evidence_catalog, "Молоток, масса молотка 500 г, масса молотка 700 г")
    payload = state.model_dump(mode="json")
    diagnostic = next(item for item in payload["diagnostics"] if item["kind"] == "contradiction")
    if tamper == "assertion":
        diagnostic["assertion_ids"] = ["missing-fact"]
    elif tamper == "subject":
        diagnostic["subject"] = {"id": "missing-subject", "kind": "item", "evidence": []}
    elif tamper in ("span", "revision"):
        quote = next(item for item in diagnostic["evidence"] if item["type"] == "text")
        if tamper == "span":
            quote["span"]["text"] = "?" + quote["span"]["text"][1:]
        else:
            quote["span"]["text_revision"] = 999
    elif tamper == "catalog_ref":
        diagnostic["characteristic_refs"][0]["position_code"] = "missing-position"
    else:
        assertion = next(item for item in payload["assertions"] if item["id"] in diagnostic["assertion_ids"])
        if tamper == "assertion_span":
            span = assertion["span"]
        else:
            sources = assertion["subject"]["evidence"] if tamper == "subject_evidence" else assertion["evidence"]
            span = next(item["span"] for item in sources if item["type"] == "text")
        span["text"] = "?" + span["text"][1:]
    with pytest.raises(ValidationError):
        C.CardState.model_validate(payload)


def test_diagnostic_with_several_subjects_preserves_ambiguity_and_all_sources(evidence_catalog):
    from src.ktru.engine.compatibility import diagnostic_context

    text = "масса упаковки 500 г; масса бойка 700 г"
    assertions = []
    for ident, kind, label, quote, value in (
        ("package-mass", "package", "упаковки", "масса упаковки 500 г", "500"),
        ("part-mass", "part", "бойка", "масса бойка 700 г", "700"),
    ):
        start = text.index(quote)
        evidence = C.TextEvidence(span=C.Span(start=start, end=start + len(quote), text=quote, text_revision=1))
        subject = C.SubjectRef(id=ident, kind=kind, label=label, evidence=[evidence])
        assertions.append(C.Assertion(id=ident, subject=subject, property_raw="Масса", value_raw=f"{value} г",
            value=C.NumberValue(x=value, unit_okei="163"), origin="text", extractor="rules", evidence=[evidence]))
    context = diagnostic_context(evidence_catalog, [], assertions)
    assert context["subject"] is None and context["characteristic_refs"] == []
    assert context["evidence"] == [item for assertion in assertions for item in assertion.evidence]


def test_funnel_counts_link_distinct_declared_universes_and_real_sources(evidence_catalog):
    with sqlite3.connect(evidence_catalog.dir / "catalog.sqlite") as db:
        db.execute("INSERT INTO positions (code,version,name,status,is_template,family_id,domain,supply_units) "
                   "VALUES ('other',1,'Стол','ACTIVE',0,'table','furniture','[]')")
        db.execute("INSERT INTO positions_fts VALUES ('other',1,'стол',NULL,'ширина')")
    evidence_catalog.manifest["counts"]["final"] = 3
    state = parse(evidence_catalog, "Молоток, масса молотка 500 г")
    scopes = {scope.id: scope for scope in state.funnel_scopes}
    assert {scope.kind for scope in scopes.values()} == {"catalog", "retrieved_families", "family"}
    assert len(scopes) == 3
    assert next(scope.count for scope in scopes.values() if scope.kind == "catalog") == 3
    for stage in state.funnel:
        scope = scopes[stage.scope_id]
        assert scope.snapshot_id == state.snapshot_id and 0 <= stage.count <= scope.count
        assert stage.evidence
        for item in stage.evidence:
            if isinstance(item, C.TextEvidence):
                assert state.text[item.span.start:item.span.end] == item.span.text
                assert item.span.text_revision == state.revision
    narrowed = next(stage for stage in state.funnel if stage.trigger == "assertion")
    family_scope = scopes[narrowed.scope_id]
    assert family_scope.kind == "family" and family_scope.count == 2 and narrowed.count == 1
    assert family_scope.family_ids == [state.family.id]
    # «Молоток» names the family completely, so its scope is confirmed and the single survivor is final.
    assert state.final is not None and state.search_scope.coverage_status == "family_confirmed"
    assert any(isinstance(item, C.CatalogEvidence) for item in narrowed.evidence)


@pytest.mark.parametrize("tamper", ["unknown_scope", "count", "snapshot", "source"])
def test_card_contract_rejects_false_funnel_scope_or_source(evidence_catalog, tamper):
    state = parse(evidence_catalog, "Молоток, масса молотка 500 г")
    payload = state.model_dump(mode="json")
    stage = next(item for item in payload["funnel"] if item["trigger"] == "assertion")
    if tamper == "unknown_scope":
        stage["scope_id"] = "missing-scope"
    elif tamper == "count":
        stage["count"] = 999
    elif tamper == "snapshot":
        payload["funnel_scopes"][0]["snapshot_id"] = "different-snapshot"
    else:
        quote = next(item for item in stage["evidence"] if item["type"] == "text")
        quote["span"]["text"] = "?" + quote["span"]["text"][1:]
    with pytest.raises(ValidationError):
        C.CardState.model_validate(payload)


def test_answer_and_edit_funnel_keep_family_universe_and_user_sources(evidence_catalog):
    pipeline = Pipeline(evidence_catalog)
    state = pipeline.parse(C.ParseRequest(request_id="user-source", expected_revision=0, text="Молоток"))
    question = state.clarification
    state = confirm_family(pipeline, state, "family-source")
    question = state.clarification
    answered = pipeline.answer(C.AnswerRequest(session_id=state.session_id, request_id="mass-source",
        expected_revision=state.revision, question_id=question.id, value=C.NumberValue(x="500", unit_okei="163")))
    stage = next(stage for stage in answered.funnel if stage.trigger == "answer")
    scope = next(scope for scope in answered.funnel_scopes if scope.id == stage.scope_id)
    assert scope.kind == "family" and scope.count == 2 and stage.count == 1
    assert any(isinstance(item, C.UserEvidence) and item.type == "user_answer" and item.question_id == question.id
               for item in stage.evidence)
    field = next(field for field in answered.fields if field.characteristic_ref)
    edited = pipeline.edit(C.EditRequest(session_id=answered.session_id, request_id="edit-source",
        expected_revision=answered.revision, patch=C.SetFieldPatch(field_id=field.id,
            label_ru=field.label_ru, value="700 г")))
    stage = next(stage for stage in edited.funnel if stage.trigger == "edit")
    assert stage.scope_id == scope.id and stage.count == 1
    assert any(isinstance(item, C.UserEvidence) and item.type == "user_edit" for item in stage.evidence)
    assert next(scope for scope in edited.funnel_scopes if scope.id == stage.scope_id).count == 2


@pytest.mark.parametrize("value", ["не менее 600 г", "500 г и 700 г"])
def test_retained_subject_quote_keeps_original_source_after_field_edit(evidence_catalog, value):
    pipeline = Pipeline(evidence_catalog)
    state = pipeline.parse(C.ParseRequest(request_id="retained-source", expected_revision=0,
        text="Молоток, масса молотка 500 г"))
    state = confirm_family(pipeline, state, "source-family")
    field = next(field for field in state.fields if field.characteristic_ref)
    result = pipeline.edit(C.EditRequest(session_id=state.session_id, request_id="source-edit",
        expected_revision=state.revision, patch=C.SetFieldPatch(field_id=field.id,
            label_ru=field.label_ru, value=value)))
    assertion = next(assertion for assertion in result.assertions if assertion.origin == "user_edit")
    assert assertion.subject.evidence
    for item in assertion.subject.evidence:
        if isinstance(item, C.TextEvidence):
            assert item.span.text_revision == result.revision
            assert result.text[item.span.start:item.span.end] == item.span.text
    for diagnostic in result.diagnostics:
        if assertion.id in diagnostic.assertion_ids:
            assert_sources(result, diagnostic)
    C.CardState.model_validate(result.model_dump(mode="json"))


def test_corrupt_retained_subject_quote_is_rejected_instead_of_restamped(evidence_catalog):
    from src.ktru.engine.binding import _user_assertions
    from src.ktru.engine.stage_types import Inputs

    quote = C.TextEvidence(span=C.Span(start=0, end=8, text="invented", text_revision=1))
    subject = C.SubjectRef(id="retained-item", kind="item", evidence=[quote])
    inp = Inputs(text="Молоток", revision=2, edits=[{"label_ru": "Масса", "value": "500 г",
        "subject": subject.model_dump(mode="json")}])
    with pytest.raises(ValueError, match="Retained subject evidence"):
        _user_assertions(inp, {}, {})


def test_unit_fix_targets_original_quantity_and_preserves_other_text(evidence_catalog):
    text = "Молоток, масса молотка 500 кг, индивидуальная гравировка"
    state = parse(evidence_catalog, text)
    diagnostic = next(diagnostic for diagnostic in state.diagnostics if diagnostic.kind == "implausible")
    fix = next(fix for fix in diagnostic.fixes if "500 г" in fix.label_ru)
    assert isinstance(fix.patch, C.ReplaceSpanPatch)
    span = fix.patch.span
    assert text[span.start:span.end] == span.text == "500 кг"
    corrected = text[:span.start] + fix.patch.replacement + text[span.end:]
    assert corrected == "Молоток, масса молотка 500 г, индивидуальная гравировка"
    assert fix.patch.snapshot_id == fix.snapshot_id == state.snapshot_id
    assert fix.patch.base_revision == fix.base_revision == span.text_revision == state.revision
