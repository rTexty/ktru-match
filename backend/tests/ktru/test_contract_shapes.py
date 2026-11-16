"""Public contract rejects invalid offsets and undeclared funnel count scopes."""

import pytest
from pydantic import ValidationError

from src.ktru import contracts as C


@pytest.mark.parametrize("data", [
    {"start": -1, "end": 1, "text": "ab", "text_revision": 1},
    {"start": 2, "end": 1, "text": "", "text_revision": 1},
    {"start": 0, "end": 2, "text": "🔧", "text_revision": 1},
    {"start": 0, "end": 1, "text": "a", "text_revision": -1},
])
def test_span_shape_rejects_invalid_code_point_offsets(data):
    with pytest.raises(ValidationError):
        C.Span(**data)


def test_fix_patch_rejects_untyped_action():
    with pytest.raises(ValidationError):
        C.Fix(id="bad", label_ru="Исправить", patch={"type": "unknown", "command": "x"})


def test_funnel_stage_requires_declared_scope():
    with pytest.raises(ValidationError):
        C.FunnelStage(label_ru="Позиции", count=1, trigger="retrieval")


def test_source_map_range_rejects_reversed_offsets():
    with pytest.raises(ValidationError):
        C.SourceMapSegment(original_start=3, original_end=2,
            transformed_start=0, transformed_end=1, kind="typo")


def _valid_card():
    return C.CardState(session_id="session", revision=1, request_id="request", snapshot_id="snapshot",
        status="provisional", text="abc", readiness="no_code", assertions=[C.Assertion(id="fact",
            property_raw="Название", value_raw="abc", value=C.TextValue(s="abc"), origin="text", extractor="rules",
            span=C.Span(start=0, end=3, text="abc", text_revision=1))])


@pytest.mark.parametrize("tamper", ["span", "binding", "field", "decision", "scope"])
def test_card_rejects_false_stage_output_references(tamper):
    card = _valid_card().model_dump(mode="json")
    foreign = {"snapshot_id": "other", "code": "position", "version": 1}
    if tamper == "span":
        card["assertions"][0]["span"].update(text="xyz", text_revision=999)
    elif tamper == "binding":
        card["bindings"] = [{"assertion_id": "missing", "relation": "none", "subject_match": "unknown", "rule": "x"}]
    elif tamper == "field":
        card["fields"] = [{"id": "field", "label_ru": "Название", "source": "new", "display_ru": "abc",
            "status": "from_text", "assertion_ids": ["missing"]}]
    elif tamper == "decision":
        card["final"] = {"position_ref": foreign, "name": "abc", "basis_ru": [], "conditional_on_family": False}
    else:
        card["search_scope"] = {"snapshot_id": "other", "eligible_count": 0, "examined_count": 0,
            "retained_count": 0, "family_ids": [], "family_membership_complete": True, "retrieval_truncated": False,
            "coverage_status": "unconfirmed", "reason_ru": "abc"}
    with pytest.raises(ValidationError):
        C.CardState.model_validate(card)


def test_map_rejects_empty_source_range():
    with pytest.raises(ValidationError):
        C.SourceMapSegment(original_start=1, original_end=1,
            transformed_start=1, transformed_end=2, kind="typo")


@pytest.mark.parametrize("tamper", ["characteristic", "excluded", "template", "unconfirmed"])
def test_card_rejects_false_candidate_result_or_final(tamper):
    card = _valid_card().model_dump(mode="json")
    ref = {"snapshot_id": "snapshot", "code": "position", "version": 1}
    candidate = {"ref": ref, "name": "abc", "is_template": False, "status": "alive"}
    card["candidates"] = [candidate]
    if tamper == "characteristic":
        candidate["supporting"] = [{"position_ref": ref, "assertion_ids": ["fact"], "result": "compatible",
            "reason_ru": "abc", "characteristic_ref": {"snapshot_id": "snapshot", "position_code": "other-position",
                "position_version": 1, "char_code": "name"}}]
    else:
        scope = {"snapshot_id": "snapshot", "eligible_count": 1, "examined_count": 1, "retained_count": 1,
            "family_ids": ["family"], "family_membership_complete": True, "retrieval_truncated": False,
            "coverage_status": "family_confirmed", "reason_ru": "abc"}
        card["final"] = {"position_ref": ref, "name": "abc", "basis_ru": [], "conditional_on_family": True,
            "scope": scope}
        if tamper == "excluded":
            candidate["status"] = "excluded"
        elif tamper == "template":
            candidate["is_template"] = True
        else:
            scope["coverage_status"] = "unconfirmed"
    with pytest.raises(ValidationError):
        C.CardState.model_validate(card)


def test_diagnostic_cannot_certify_its_own_unknown_catalog_position():
    card = _valid_card().model_dump(mode="json")
    position = {"snapshot_id": "snapshot", "code": "missing-position", "version": 1}
    characteristic = {"snapshot_id": "snapshot", "position_code": "missing-position",
        "position_version": 1, "char_code": "name"}
    card["diagnostics"] = [{"id": "false-source", "kind": "schema_defect", "severity": "warning",
        "message_ru": "abc", "reason_ru": "abc", "characteristic_refs": [characteristic],
        "evidence": [{"type": "catalog", "position_ref": position, "characteristic_ref": characteristic}]}]
    with pytest.raises(ValidationError):
        C.CardState.model_validate(card)
