"""User corrections of readings (remove, move, modality, rename) on a small SQLite catalog: CAS, undo, evidence."""

import json
import sqlite3

import pytest
from pydantic import ValidationError

from src.ktru import contracts as C
from src.ktru.catalog.build import DDL
from src.ktru.catalog.reader import Catalog
from src.ktru.service.pipeline import Pipeline, ServiceError

MASS = [{"kind": "range", "lo": "400", "hi": "1000", "okei_code": "163", "okei_name": "г", "value_code": "m1"}]
SEAT = [{"kind": "enum", "label": "Пластик", "value_code": "s1"}]


@pytest.fixture
def pipeline(tmp_path):
    db = sqlite3.connect(tmp_path / "catalog.sqlite")
    db.executescript(DDL)
    for code, name, family, char_code, char_name, allowed in (
        ("hammer-1", "Молоток", "hammer", "mass", "Масса", MASS),
        ("stool-1", "Табурет", "stool", "seat", "Материал сиденья", SEAT),
        ("chair-1", "Стул", "chair", "seat", "Материал сиденья", SEAT),
    ):
        db.execute("INSERT INTO positions (code,version,name,status,is_template,family_id,domain,supply_units) "
                   "VALUES (?,1,?,'ACTIVE',0,?,'tools','[]')", (code, name, family))
        db.execute("INSERT INTO characteristics VALUES (?,1,?,?,NULL,'2',1,0,1,?)",
                   (code, char_code, char_name, json.dumps(allowed, ensure_ascii=False)))
        db.execute("INSERT INTO positions_fts VALUES (?,1,?,NULL,?)", (code, name.casefold(), char_name.casefold()))
    db.commit()
    db.close()
    (tmp_path / "manifest.json").write_text(json.dumps({"snapshot_id": "test-overrides", "counts": {"final": 3}}))
    return Pipeline(Catalog(tmp_path))


def parse(pipeline, text):
    return pipeline.parse(C.ParseRequest(request_id="parse-" + text, expected_revision=0, text=text))


def edit(pipeline, state, patch, request_id=None):
    return pipeline.edit(C.EditRequest(session_id=state.session_id, request_id=request_id or f"edit-{state.revision}",
                                       expected_revision=state.revision, patch=patch))


def undo(pipeline, state):
    return pipeline.undo(C.UndoRequest(session_id=state.session_id, request_id=f"undo-{state.revision}",
                                       expected_revision=state.revision))


def mass(state):
    return next(a for a in state.assertions if a.property_raw.casefold() == "масса"
                and isinstance(a.value, C.NumberValue) and a.value.x == "800")


def active(state, name):
    return [a for a in state.assertions if a.property_raw.casefold() == name and a.status != "rejected"]


def test_move_assertion_changes_only_its_subject_and_keeps_text_evidence(pipeline):
    state = parse(pipeline, "Молоток 800 г.")
    reading = mass(state)
    moved = edit(pipeline, state, C.MoveAssertionPatch(assertion_id=reading.id, subject_kind="package",
                                                       subject_label="коробка"))
    after = next(a for a in moved.assertions if a.id == reading.id)
    assert moved.revision == state.revision + 1 and moved.can_undo
    assert (after.subject.kind, after.subject.label, after.value) == ("package", "коробка", reading.value)
    assert after.span == reading.span.model_copy(update={"text_revision": moved.revision})
    assert any(isinstance(e, C.UserEvidence) and e.type == "user_edit" for e in after.evidence)
    assert any(s.kind == "package" and s.label == "коробка" for s in moved.subjects)
    restored = undo(pipeline, moved)
    assert next(a for a in restored.assertions if a.id == reading.id).subject.kind == "item"


def test_remove_field_makes_the_value_unknown_not_negative(pipeline):
    state = parse(pipeline, "Молоток 800 г.")
    edited = edit(pipeline, state, C.SetFieldPatch(label_ru="Масса", value="900 г"))
    field = next(f for f in edited.fields if f.label_ru == "Масса")
    removed = edit(pipeline, edited, C.RemoveFieldPatch(field_id=field.id))
    assert active(removed, "масса") == []
    rejected = [a for a in removed.assertions if a.property_raw.casefold() == "масса"]
    assert rejected and all(a.status == "rejected" and a.polarity == "positive" for a in rejected)
    restored = undo(pipeline, removed)
    assert [a.value.x for a in active(restored, "масса")] == ["900"]


def test_a_later_value_survives_an_earlier_removal(pipeline):
    state = parse(pipeline, "Молоток 800 г.")
    field = next(f for f in state.fields if f.label_ru == "Масса")
    removed = edit(pipeline, state, C.RemoveFieldPatch(field_id=field.id))
    readded = edit(pipeline, removed, C.SetFieldPatch(label_ru="Масса", value="700 г"))
    assert [a.value.x for a in active(readded, "масса")] == ["700"]


def test_modality_edit_weakens_a_reading_without_removing_it(pipeline):
    state = parse(pipeline, "Молоток 800 г.")
    reading = mass(state)
    weakened = edit(pipeline, state, C.AssertionModalityPatch(assertion_id=reading.id, status="ambiguous",
                                                               operator="approx"))
    after = next(a for a in weakened.assertions if a.id == reading.id)
    assert (after.status, after.operator, after.value) == ("ambiguous", "approx", reading.value)
    # A weakened reading excludes nothing; the named one-member family still gives its only code.
    assert all(c.status == "alive" for c in weakened.candidates)


def test_rename_item_searches_again_and_keeps_other_edits(pipeline):
    state = parse(pipeline, "Стул, сиденье пластик.")
    item = next(s for s in state.subjects if s.kind == "item")
    assert state.family.name == "Стул"
    renamed = edit(pipeline, state, C.RenameSubjectPatch(subject_id=item.id, label="табурет"))
    assert renamed.family.name == "Табурет"
    assert "табурет" in renamed.search_scope.reason_ru
    assert any(s.kind == "item" and s.label == "табурет" for s in renamed.subjects)
    back = edit(pipeline, renamed, C.RenameSubjectPatch(subject_id=item.id, label="стул"))
    assert back.family.name == "Стул" and back.revision == renamed.revision + 1


@pytest.mark.parametrize("patch", [
    C.MoveAssertionPatch(assertion_id="missing", subject_kind="package", subject_label="коробка"),
    C.AssertionModalityPatch(assertion_id="missing", status="ambiguous"),
    C.RemoveFieldPatch(field_id="missing"),
    C.RenameSubjectPatch(subject_id="missing", label="табурет"),
])
def test_a_target_missing_from_the_current_card_is_stale_and_changes_nothing(pipeline, patch):
    state = parse(pipeline, "Молоток 800 г.")
    with pytest.raises(ServiceError) as error:
        edit(pipeline, state, patch)
    assert (error.value.status, error.value.body.code) == (409, "stale_revision")
    assert pipeline.get(state.session_id) == state


def test_override_uses_revision_cas_and_request_idempotency(pipeline):
    state = parse(pipeline, "Молоток 800 г.")
    patch = C.AssertionModalityPatch(assertion_id=mass(state).id, status="ambiguous")
    first = edit(pipeline, state, patch, request_id="same")
    assert edit(pipeline, state, patch, request_id="same") == first
    with pytest.raises(ServiceError) as error:
        edit(pipeline, state, patch, request_id="other")
    assert error.value.body.code == "stale_revision"


def test_modality_patch_must_change_something_typed():
    with pytest.raises(ValidationError):
        C.AssertionModalityPatch(assertion_id="a")
    with pytest.raises(ValidationError):
        C.AssertionModalityPatch(assertion_id="a", value=C.UnparsedValue(s="x", reason="y"))


def test_explicit_no_in_a_new_presence_field_is_a_negative_item_fact(pipeline):
    state = parse(pipeline, "Стул, сиденье пластик.")
    edited = edit(pipeline, state, C.SetFieldPatch(label_ru="Наличие спинки", value="Нет"))
    fact = next(a for a in edited.assertions if a.property_raw == "Наличие спинки")
    assert (fact.value, fact.subject.kind, fact.origin) == (C.BoolValue(v=False), "item", "user_edit")
