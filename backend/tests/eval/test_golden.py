"""Behavior checks for public Golden Set data; these tests are not product acceptance."""

import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ktru import contracts as C

ROOT = Path(__file__).resolve().parents[3]
HEADER = ["case_id", "group_id", "domain", "tags", "priority", "precondition", "input_json",
          "expected_action", "must", "forbidden", "oracle_status"]


@pytest.fixture
def golden():
    path = ROOT / "eval/golden.py"
    assert path.is_file(), "Golden Set evaluator is not implemented"
    spec = importlib.util.spec_from_file_location("ktru_golden_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_bank(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, HEADER, delimiter="\t", quoting=csv.QUOTE_NONE, quotechar=None)
        writer.writeheader()
        for row in rows:
            writer.writerow({"priority": "P0", "precondition": "-", "expected_action": "extract",
                             "must": "Сохранить массу", "forbidden": "Не менять массу",
                             "oracle_status": "behavior_only", **row})


@pytest.fixture
def bank_dir(tmp_path):
    write_bank(tmp_path / "01-normal.tsv", [
        {"case_id": f"NORM-{i:03}", "group_id": f"NORM-G{(i - 1) // 2 + 1:03}", "domain": "tools",
         "tags": "equivalent;units", "input_json": json.dumps(f"Молоток, масса {i} г", ensure_ascii=False)}
        for i in range(1, 5)])
    write_bank(tmp_path / "06-dialogue.tsv", [
        {"case_id": f"DLG-{i:03}", "group_id": "DLG-G001", "domain": "tools",
         "tags": f"turn_{i};answer", "precondition": "Синтетический текущий вопрос",
         "input_json": json.dumps(f"Ответ {i}", ensure_ascii=False)} for i in range(1, 6)])
    return tmp_path


def number(x="500", unit="163"):
    return {"type": "number", "x": x, "unit_okei": unit, "unit_name": "г"}


def fact(**changes):
    return {"property": ["Масса", "Вес"], "value": number(), "operator": "eq", "polarity": "positive",
            "status": ["supported", "ambiguous"], "subject": {"kind": "item"},
            "quote": "масса 500 г", **changes}


def assertion(text="Молоток, масса 500 г", quote="масса 500 г", **changes):
    start = text.index(quote)
    evidence = C.TextEvidence(span=C.Span(start=start, end=start + len(quote), text=quote, text_revision=0))
    result = C.Assertion(id="mass", subject_kind="item", subject=C.SubjectRef(id="item", kind="item"),
                         property_raw="Масса", value_raw="500 г", value=C.NumberValue(**number()), origin="text",
                         extractor="rules", evidence=[evidence]).model_dump(mode="json")
    return {**result, **changes}


def observed(text="Молоток, масса 500 г", assertions=None, **changes):
    return {"text": text, "original_text": text, "assertions": assertions if assertions is not None else [assertion()],
            "subjects": [{"id": "item", "kind": "item", "label": "молоток", "parent_subject_id": None}],
            "final": None, "status": "analyzed", **changes}


def labelled(row):
    return {**row, "expected": {"required": [], "forbidden": [], "no_final": True}}


def selection():
    return {"NORM": ["NORM-G001", "NORM-G002"], "DLG": ["DLG-G001"]}


def manifest(rows):
    return {"schema_version": "adversarial-golden-1", "total": 9, "single": 4, "dialogue": 5,
            "groups": selection(), "sources": {r["source_file"]: r["source_hash"] for r in rows},
            "exposure": "public_synthetic_validation"}


def test_load_bank_preserves_text_tags_and_file_provenance(golden, tmp_path):
    text = '🔨 Молоток.\nЦитата: "500 г"; путь C:\\new\\tool\tмасса'
    path = tmp_path / "01-normal.tsv"
    write_bank(path, [{"case_id": "NORM-001", "group_id": "NORM-G001", "domain": "tools",
                      "tags": "unicode;format", "input_json": json.dumps(text, ensure_ascii=False)}])
    rows = golden.load_bank(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["text"] == text
    assert row["tags"] == ["unicode", "format"]
    assert row["must"] == "Сохранить массу" and row["forbidden"] == "Не менять массу"
    assert row["precondition"] == "-"
    assert Path(row["source_file"]).name == path.name
    assert row["source_hash"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(row["row_hash"]) == 64
    assert golden.load_bank(tmp_path) == rows


@pytest.mark.parametrize("input_json", ["null", "42", "[]", '"unterminated'])
def test_load_bank_rejects_invalid_text_cells(golden, tmp_path, input_json):
    write_bank(tmp_path / "01-normal.tsv", [{"case_id": "NORM-001", "group_id": "NORM-G001",
               "domain": "tools", "tags": "basic", "input_json": input_json}])
    with pytest.raises(ValueError):
        golden.load_bank(tmp_path)


def test_select_keeps_all_group_members_and_dialogue_turns(golden, bank_dir):
    rows = golden.load_bank(bank_dir)
    selected = golden.select(rows, {"NORM": ["NORM-G001"], "DLG": ["DLG-G001"]})
    assert {r["case_id"] for r in selected} == {"NORM-001", "NORM-002", *[f"DLG-{i:03}" for i in range(1, 6)]}
    assert [r["case_id"] for r in selected if r["case_id"].startswith("DLG-")] == [f"DLG-{i:03}" for i in range(1, 6)]


@pytest.mark.parametrize("groups", [{"NORM": ["missing"]}, {"DLG": ["NORM-G001"]}, {"NORM": ["NORM-001"]}])
def test_select_rejects_unknown_or_wrong_prefix_groups(golden, bank_dir, groups):
    with pytest.raises(ValueError):
        golden.select(golden.load_bank(bank_dir), groups)


def test_validate_accepts_complete_small_fixture_with_declared_counts(golden, bank_dir):
    rows = golden.load_bank(bank_dir)
    dataset = [labelled(r) for r in golden.select(rows, selection())]
    dataset[0] = {**dataset[0], "expected": {"required": [fact(value=number("1"), quote="масса 1 г")],
                                          "forbidden": [], "no_final": True}}
    assert golden.validate(dataset, manifest(rows), rows) == []


def test_judging_selection_keeps_one_single_but_requires_full_dialogue(golden, bank_dir):
    rows = golden.load_bank(bank_dir)
    groups = {"NORM": ["NORM-G001"], "DLG": ["DLG-G001"]}
    ids = ["NORM-002", *[f"DLG-{i:03}" for i in range(1, 6)]]
    selected = golden.select(rows, groups, ids)
    dataset = [labelled(row) for row in selected]
    metadata = {**manifest(rows), "schema_version": "judging-golden-2", "groups": groups,
                "case_ids": ids, "total": 6, "single": 1}
    assert golden.validate(dataset, metadata, rows) == []
    metadata = {**metadata, "case_ids": ids[:-1], "total": 5, "dialogue": 4}
    assert any("five" in error for error in golden.validate(dataset[:-1], metadata, rows))


@pytest.mark.parametrize("ids", [["NORM-001", "NORM-001"], ["NORM-003"], []])
def test_explicit_selection_rejects_duplicate_or_outside_case_ids(golden, bank_dir, ids):
    with pytest.raises(ValueError):
        golden.select(golden.load_bank(bank_dir), {"NORM": ["NORM-G001"]}, ids)


def test_correct_fact_in_wrong_domain_fails_judging_category_gold(golden):
    case = {"case_id": "x", "expected": {"domain": "tools", "required": [fact()]}}
    assert golden.grade(case, observed(domain="tools"))["status"] == "pass_checks"
    result = golden.grade(case, observed(domain="furniture"))
    assert result["status"] == "fail_checks" and result["failures"] == ["domain"]


@pytest.mark.parametrize("mutation", ["text", "row_hash", "source_hash", "duplicate", "partial", "count"])
def test_validate_rejects_changed_sources_duplicates_and_incomplete_groups(golden, bank_dir, mutation):
    rows = golden.load_bank(bank_dir)
    dataset = [labelled(r) for r in golden.select(rows, selection())]
    metadata = manifest(rows)
    if mutation in ("text", "row_hash", "source_hash"):
        dataset[0] = {**dataset[0], mutation: "changed"}
    elif mutation == "duplicate":
        dataset = [*dataset, deepcopy(dataset[0])]
    elif mutation == "partial":
        dataset = dataset[1:]
        metadata = {**metadata, "total": 8, "single": 3}
    else:
        metadata = {**metadata, "total": 300, "single": 240, "dialogue": 60}
    assert golden.validate(dataset, metadata, rows)


@pytest.mark.parametrize("changes", [{"value": {"type": "number", "x": "NaN"}},
                                    {"value": {"type": "number", "x": 500}}, {"operator": "guess"},
                                    {"polarity": "neutral"}, {"subject": {"kind": "product"}},
                                    {"quote": "not in source"}, {"status": ["rejected"]}])
def test_validate_rejects_invalid_gold_facts(golden, bank_dir, changes):
    rows = golden.load_bank(bank_dir)
    dataset = [labelled(r) for r in golden.select(rows, selection())]
    bad_fact = {**fact(value=number("1"), quote="масса 1 г"), **changes}
    dataset[0] = {**dataset[0], "expected": {"required": [bad_fact], "forbidden": [], "no_final": False}}
    assert golden.validate(dataset, manifest(rows), rows)


def test_match_fact_accepts_decimal_equivalence_and_property_alias(golden):
    actual = assertion(property_raw="Вес", value=number("500.000"))
    assert golden.match_fact(fact(), actual, observed(assertions=[actual]))


def test_match_fact_accepts_exact_mass_unit_conversion(golden):
    actual = assertion(value={"type": "number", "x": "0.5", "unit_okei": "166", "unit_name": "кг"})
    assert golden.match_fact(fact(value=number("500")), actual, observed(assertions=[actual]))


def test_match_fact_does_not_invent_a_missing_unit(golden):
    actual = assertion(value={"type": "number", "x": "500", "unit_okei": None, "unit_name": None})
    assert not golden.match_fact(fact(value=number("500")), actual, observed(assertions=[actual]))


def test_unknown_unit_name_cannot_impersonate_a_known_dimension(golden):
    actual = assertion(value={"type": "number", "x": "500", "unit_okei": "unknown", "unit_name": "mass"})
    assert not golden.match_fact(fact(), actual, observed(assertions=[actual]))


@pytest.mark.parametrize("changes", [{"operator": "ge"}, {"polarity": "negative"}, {"status": "rejected"},
                                      {"value": number("501")}, {"value": number("500", "166")},
                                      {"property_raw": "Длина"},
                                      {"subject_kind": "package", "subject": {"id": "package", "kind": "package"}}])
def test_match_fact_rejects_wrong_semantics(golden, changes):
    actual = assertion(**changes)
    assert not golden.match_fact(fact(), actual, observed(assertions=[actual]))


def test_match_fact_keeps_exact_decimal_precision(golden):
    target = "0.1234567890123456789012345678901"
    expected = fact(value=number(target))
    assert golden.match_fact(expected, assertion(value=number(target)), observed())
    assert not golden.match_fact(expected, assertion(value=number("0.1234567890123456789012345678902")), observed())


@pytest.mark.parametrize("value", [{"type": "enum", "label": "Сталь"}, {"type": "bool", "v": False},
                                   {"type": "text", "s": "нержавеющая сталь"}])
def test_match_fact_respects_all_typed_values(golden, value):
    assert golden.match_fact(fact(value=value), assertion(value=value), observed())
    assert not golden.match_fact(fact(value=value), assertion(value={"type": "unparsed", "s": "Сталь",
                                 "reason": "unknown"}), observed())


def test_match_fact_distinguishes_range_bounds(golden):
    value = {"type": "range", "lo": "400", "hi": "600", "lo_inclusive": True, "hi_inclusive": False,
             "unit_okei": "163", "unit_name": "г"}
    expected = fact(value=value, operator="between")
    actual = assertion(value=value, operator="between")
    assert golden.match_fact(expected, actual, observed())
    assert not golden.match_fact(expected, {**actual, "value": {**value, "hi_inclusive": True}}, observed())


def test_match_fact_checks_part_and_parent_subject(golden):
    text = "Стул: спинка из стали. Стол: ножка из стали."
    quote = "ножка из стали"
    part = {"id": "leg", "kind": "part", "label": "ножка", "parent_subject_id": "desk"}
    actual = assertion(text, quote, property_raw="Материал", value={"type": "enum", "label": "Сталь"},
                       subject_kind="part", subject_label="ножка", subject=part)
    state = observed(text, [actual], subjects=[part, {"id": "desk", "kind": "item", "label": "стол"},
                                              {"id": "chair", "kind": "item", "label": "стул"}])
    expected = fact(property=["Материал"], value={"type": "enum", "label": "Сталь"}, quote=quote,
                    subject={"kind": "part", "label": ["ножка", "опора"], "parent_label": ["стол"]})
    assert golden.match_fact(expected, actual, state)
    assert not golden.match_fact(expected, {**actual, "subject": {**part, "parent_subject_id": "chair"}}, state)
    assert not golden.match_fact(expected, {**actual, "subject": {**part, "parent_subject_id": "missing"}}, state)


def test_inline_subject_cannot_change_registry_identity(golden):
    actual = assertion(subject={"id": "chair", "kind": "item", "label": "молоток"})
    state = observed(assertions=[actual], subjects=[{"id": "chair", "kind": "item", "label": "стул"}])
    assert not golden.match_fact(fact(subject={"kind": "item", "label": ["молоток"]}), actual, state)


@pytest.mark.parametrize("dangling", ["subject", "parent"])
def test_dangling_subject_references_cannot_receive_credit(golden, dangling):
    subject = {"id": "leg", "kind": "part", "label": "ножка", "parent_subject_id": "missing"}
    actual = assertion(subject_kind="part", subject_label="ножка", subject=subject)
    subjects = [] if dangling == "subject" else [subject]
    state = observed(assertions=[actual], subjects=subjects)
    assert not golden.match_fact(fact(subject={"kind": "part", "label": ["ножка"]}), actual, state)


@pytest.mark.parametrize("bad_evidence", [[], [{"type": "catalog", "position_ref": {"code": "x", "version": 1}}],
    [{"type": "text", "span": {"start": 0, "end": 11, "text": "масса 500 г", "text_revision": 0}}],
    [{"type": "text", "span": {"start": 9, "end": 20, "text": "масса 999 г", "text_revision": 0}}]])
def test_match_fact_requires_valid_input_span(golden, bad_evidence):
    assert not golden.match_fact(fact(), assertion(evidence=bad_evidence), observed())


def test_match_fact_rejects_valid_but_unrelated_quote(golden):
    text = "Молоток, масса 500 г. Упаковка красная."
    actual = assertion(text, "Упаковка красная", value=number())
    assert not golden.match_fact(fact(), actual, observed(text, [actual]))


def test_match_fact_accepts_wider_valid_span_containing_gold_quote(golden):
    text = "🔨 Молоток, масса 500 г."
    actual = assertion(text, "Молоток, масса 500 г")
    assert golden.match_fact(fact(), actual, observed(text, [actual]))


def test_match_fact_uses_original_source_when_search_text_was_changed(golden):
    actual = assertion()
    state = observed(original_text="Молоток, масса 999 г")
    assert not golden.match_fact(fact(), actual, state)


def test_stale_span_revision_cannot_ground_a_current_fact(golden):
    state = observed(revision=1)
    assert not golden.match_fact(fact(), assertion(), state)


def test_grade_pass_requires_all_automated_checks(golden):
    case = {"case_id": "x", "text": "Молоток, масса 500 г", "expected": {
        "required": [fact()], "forbidden": [], "no_final": True}}
    result = golden.grade(case, observed())
    assert result["status"] == "pass_checks" and not result["failures"]
    assert result["checked"] == 2


def test_grade_reports_missing_required_and_wrong_final(golden):
    case = {"case_id": "x", "expected": {"required": [fact()], "forbidden": [], "no_final": True}}
    result = golden.grade(case, observed(assertions=[], final={"position_ref": {"code": "x", "version": 1}}))
    assert result["status"] == "fail_checks" and len(result["failures"]) == 2
    assert result["checked"] == 2


def test_changed_case_input_cannot_receive_a_pass(golden):
    case = {"case_id": "x", "text": "Молоток, масса 999 г", "expected": {
        "required": [fact()], "forbidden": [], "no_final": False}}
    result = golden.grade(case, observed())
    assert result["status"] != "pass_checks" and result["failures"]


def test_wide_unrelated_evidence_does_not_prove_fragment_was_retained(golden):
    text = "Молоток, масса 500 г. Примечание: титановая упаковка."
    actual = assertion(text, text)
    case = {"case_id": "x", "text": text, "expected": {
        "required": [], "forbidden": [], "preserve_quotes": ["титановая упаковка"], "no_final": False}}
    result = golden.grade(case, observed(text, [actual]))
    assert result["status"] == "fail_checks" and result["failures"]


def test_forbidden_predicate_matches_wildcard_value_but_respects_subject(golden):
    forbidden = {"property": ["Масса"], "operator": "eq", "polarity": "positive", "status": ["supported"],
                 "subject": {"kind": "package"}}
    case = {"case_id": "x", "expected": {"required": [], "forbidden": [forbidden], "no_final": False}}
    assert golden.grade(case, observed())["status"] == "pass_checks"
    package = assertion(subject_kind="package", subject={"id": "box", "kind": "package"}, value=number("999"))
    result = golden.grade(case, observed(assertions=[package], subjects=[{"id": "box", "kind": "package"}]))
    assert result["status"] == "fail_checks" and result["failures"]


def test_forbidden_value_types_allow_unparsed_but_block_numeric_fill(golden):
    forbidden = {"property": ["Масса"], "value_types": ["number", "range"], "operator": "eq",
                 "polarity": "positive", "status": ["supported"], "subject": {"kind": "item"}}
    case = {"case_id": "x", "expected": {"required": [], "forbidden": [forbidden], "no_final": False}}
    raw = assertion(value={"type": "unparsed", "s": "500 г?", "reason": "uncertain"})
    assert golden.grade(case, observed(assertions=[raw]))["status"] == "pass_checks"
    assert golden.grade(case, observed())["status"] == "fail_checks"


def test_forbidden_predicate_respects_active_status(golden):
    forbidden = {"property": ["Масса"], "operator": "eq", "polarity": "positive", "status": ["supported"],
                 "subject": {"kind": "item"}}
    case = {"case_id": "x", "expected": {"required": [], "forbidden": [forbidden], "no_final": False}}
    assert golden.grade(case, observed(assertions=[assertion(status="rejected")]))["status"] == "pass_checks"


def test_forbidden_quote_cannot_hide_a_semantic_claim_without_evidence(golden):
    case = {"case_id": "x", "expected": {"required": [], "forbidden": [fact()], "no_final": False}}
    actual = assertion(evidence=[], span=None)
    result = golden.grade(case, observed(assertions=[actual]))
    assert result["status"] == "fail_checks" and result["checked"] == 1
    assert any("forbidden" in failure for failure in result["failures"])


def test_catalog_origin_does_not_receive_required_text_fact_credit(golden):
    case = {"case_id": "x", "expected": {"required": [fact()], "forbidden": [], "no_final": False}}
    # A valid text span must not turn a catalog-origin assertion into a user fact.
    actual = assertion(origin="catalog")
    result = golden.grade(case, observed(assertions=[actual]))
    assert result["status"] == "fail_checks" and result["checked"] == 1
    assert any("required" in failure for failure in result["failures"])


@pytest.mark.parametrize("error", ["timeout", "provider unavailable"])
def test_runtime_errors_never_receive_a_pass(golden, error):
    case = {"case_id": "x", "expected": {"required": [], "forbidden": [], "no_final": True}}
    assert golden.grade(case, observed(), error=error)["status"] != "pass_checks"


def test_empty_labels_are_unassessed_not_a_pass(golden):
    case = {"case_id": "x", "expected": {"required": [], "forbidden": [], "no_final": False}}
    result = golden.grade(case, observed())
    assert result["status"] == "unassessed" and result["checked"] == 0


def test_malformed_value_with_aliases_returns_validation_errors(golden, bank_dir):
    rows = golden.load_bank(bank_dir)
    dataset = [labelled(r) for r in golden.select(rows, selection())]
    malformed = fact(value=1, quote="масса 1 г", value_aliases=["один"])
    dataset[0] = {**dataset[0], "expected": {"required": [malformed], "forbidden": [], "no_final": False}}
    assert golden.validate(dataset, manifest(rows), rows)


@pytest.mark.parametrize("scenario", ["single_precondition", "dialogue", "adjudication"])
def test_unreproduced_or_unresolved_cases_never_call_the_pipeline(golden, scenario):
    calls = []

    def parse(request):
        calls.append(request)
        raise AssertionError("Cases without approved fixtures must not call parse")

    pipeline = SimpleNamespace(catalog=SimpleNamespace(snapshot_id="test"), parse=parse)
    case = {"case_id": "NORM-001", "group_id": "NORM-G001", "domain": "tools", "tags": [],
            "text": "Молоток, масса 500 г", "precondition": "-",
            "expected": {"required": [fact()], "forbidden": [], "no_final": False}}
    if scenario == "single_precondition":
        case = {**case, "precondition": "После текущего вопроса о массе"}
    elif scenario == "dialogue":
        case = {**case, "case_id": "DLG-001", "group_id": "DLG-G001"}
    else:
        case = {**case, "annotation_status": "needs_adjudication"}
    result = golden.evaluate([case], pipeline, with_model=True)
    expected_status = "needs_adjudication" if scenario == "adjudication" else "fixture_missing"
    assert calls == []
    assert result["cases"][0]["status"] == expected_status
    assert result["cases"][0]["checked"] == 0
    assert result["checked"] == 0


@pytest.mark.parametrize("status, diagnostics", [("partial", []),
                                                 ("analyzed", [{"kind": "model_unavailable"}])])
def test_model_unavailable_partial_state_cannot_receive_passing_checks(golden, status, diagnostics):
    case = {"case_id": "NORM-001", "group_id": "NORM-G001", "domain": "tools", "tags": [],
            "text": "Молоток, масса 500 г", "precondition": "-",
            "expected": {"required": [fact()], "forbidden": [], "no_final": False}}
    data = observed(status=status, diagnostics=diagnostics)
    state = SimpleNamespace(session_id="test", revision=0, status=status,
                            diagnostics=[SimpleNamespace(**item) for item in diagnostics],
                            model_dump=lambda **kwargs: data)
    pipeline = SimpleNamespace(catalog=SimpleNamespace(snapshot_id="test"), parse=lambda request: state,
                               analyze=lambda request: state)
    result = golden.evaluate([case], pipeline, with_model=True)
    assert result["cases"][0]["status"] != "pass_checks"
    assert not result["independent_acceptance"]


def test_empty_dataset_fails_validation(golden):
    metadata = {"schema_version": "adversarial-golden-1", "total": 0, "single": 0, "dialogue": 0,
                "groups": {}, "sources": {}, "exposure": "public_synthetic_validation"}
    assert golden.validate([], metadata, [])


def test_empty_evaluation_cannot_prove_full_behavior(golden):
    pipeline = SimpleNamespace(catalog=SimpleNamespace(snapshot_id="test"))
    result = golden.evaluate([], pipeline)
    assert result.get("errors") or result.get("passed") is False
    assert not result["independent_acceptance"]


def test_nested_part_parent_can_receive_correct_material(golden):
    text = "Молоток: рукоятка с накладкой из резины."
    part = {"id": "pad", "kind": "part", "label": "накладка", "parent_subject_id": "handle"}
    handle = {"id": "handle", "kind": "part", "label": "рукоятка", "parent_subject_id": "hammer"}
    hammer = {"id": "hammer", "kind": "item", "label": "молоток", "parent_subject_id": None}
    actual = assertion(text, "накладкой из резины", property_raw="Материал", subject=part,
                       subject_kind="part", subject_label="накладка", value={"type": "enum", "label": "Резина"})
    expected = fact(property=["Материал"], value={"type": "enum", "label": "Резина"}, quote="накладкой из резины",
                    subject={"kind": "part", "label": ["накладка"], "parent_label": ["рукоятка"]})
    assert golden.match_fact(expected, actual, observed(text, [actual], subjects=[hammer, handle, part]))


@pytest.mark.parametrize("cycle", ["self", "ancestor"])
def test_cyclic_subject_graph_cannot_receive_credit(golden, cycle):
    subject = {"id": "item", "kind": "item", "label": "молоток",
               "parent_subject_id": "item" if cycle == "self" else "parent"}
    parent = {"id": "parent", "kind": "set", "label": "набор", "parent_subject_id": "item"}
    actual = assertion(subject=subject)
    assert not golden.match_fact(fact(), actual, observed(assertions=[actual], subjects=[subject, parent]))


def test_bool_false_still_requires_exact_polarity(golden):
    expected = fact(value={"type": "bool", "v": False}, polarity="negative")
    actual = assertion(value={"type": "bool", "v": False}, polarity="positive")
    assert not golden.match_fact(expected, actual, observed(assertions=[actual]))
    assert golden.match_fact({**expected, "polarity": "positive"}, actual, observed(assertions=[actual]))


def frozen_fixture(golden, tmp_path):
    bank = tmp_path / "bank"
    directory = tmp_path / "frozen"
    bank.mkdir()
    directory.mkdir()
    write_bank(bank / "01-normal.tsv", [
        {"case_id": f"NORM-{i:03}", "group_id": f"NORM-G{(i - 1) // 2 + 1:03}", "domain": "tools",
         "tags": "equivalent;units", "input_json": json.dumps(f"Молоток, масса {i} г", ensure_ascii=False)}
        for i in range(1, 241)])
    write_bank(bank / "06-dialogue.tsv", [
        {"case_id": f"DLG-{i:03}", "group_id": f"DLG-G{(i - 1) // 5 + 1:03}", "domain": "tools",
         "tags": f"turn_{(i - 1) % 5 + 1};answer", "precondition": "Текущий вопрос",
         "input_json": json.dumps(f"Ответ {i}", ensure_ascii=False)} for i in range(1, 61)])
    write_bank(bank / "03-quantities.tsv", [{"case_id": "NUM-001", "group_id": "NUM-G001", "domain": "tools",
               "tags": "numeric", "input_json": json.dumps("Молоток, масса 1 кг", ensure_ascii=False)}])
    rows = golden.load_bank(bank)
    groups = {"NORM": [f"NORM-G{i:03}" for i in range(1, 121)], "DLG": [f"DLG-G{i:03}" for i in range(1, 13)]}
    dataset = [labelled(row) for row in golden.select(rows, groups)]
    cases = directory / "cases.jsonl"
    cases.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in dataset), encoding="utf-8")
    (directory / "selection.json").write_text(json.dumps(groups), encoding="utf-8")
    scenarios = []
    source = {row["case_id"]: row for row in dataset}
    for group in groups["DLG"]:
        case_ids = [row["case_id"] for row in dataset if row["group_id"] == group]
        scenarios.append({"group_id": group, "case_ids": case_ids, "steps": [
            {"case_id": case_id, "turn": index + 1, "raw_input": source[case_id]["text"],
             "source_precondition": source[case_id]["precondition"], "source_must": source[case_id]["must"],
             "source_forbidden": source[case_id]["forbidden"], "operation": {
                 "action": "parse" if index == 0 else "answer", "request_id_alias": f"{group}-turn-{index + 1}",
                 **({"text": source[case_id]["text"], "expected_revision": 0, "session_id": None} if index == 0 else {})}}
            for index, case_id in enumerate(case_ids)]})
    (directory / "dialogues.json").write_text(json.dumps({"schema_version": "adversarial-dialogues-1",
                                                         "scenarios": scenarios}), encoding="utf-8")
    (directory / "curation-audit.json").write_text(json.dumps({"schema_version": "adversarial-audit-1",
                                                              "decisions": []}), encoding="utf-8")
    metadata = {"schema_version": "adversarial-golden-1", "total": 300, "single": 240, "dialogue": 60,
                "groups": groups, "sources": {r["source_file"]: r["source_hash"] for r in rows},
                "exposure": "public_synthetic_validation", "case_sha256": hashlib.sha256(cases.read_bytes()).hexdigest(),
                "artifact_sha256": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                                    for name in ("selection.json", "dialogues.json", "curation-audit.json")}}
    (directory / "manifest.json").write_text(json.dumps(metadata), encoding="utf-8")
    return directory, bank


def test_frozen_dataset_accepts_verified_files_and_release_counts(golden, tmp_path):
    directory, bank = frozen_fixture(golden, tmp_path)
    dataset, metadata, errors = golden.load_dataset(directory, bank)
    assert not errors
    assert len(dataset) == metadata["total"] == 300


@pytest.mark.parametrize("name", ["cases.jsonl", "selection.json", "dialogues.json", "curation-audit.json"])
def test_frozen_dataset_detects_changed_artifact_hash(golden, tmp_path, name):
    directory, bank = frozen_fixture(golden, tmp_path)
    with (directory / name).open("a", encoding="utf-8") as stream:
        stream.write("\n")
    _, _, errors = golden.load_dataset(directory, bank)
    assert errors and any("hash" in error for error in errors)


def test_missing_curation_audit_cannot_validate_frozen_evidence(golden, tmp_path):
    directory, bank = frozen_fixture(golden, tmp_path)
    (directory / "curation-audit.json").unlink()
    _, _, errors = golden.load_dataset(directory, bank)
    assert errors


@pytest.mark.parametrize("change", ["tamper_unselected", "omit_unselected", "extra_source"])
def test_frozen_manifest_must_pin_the_complete_source_bank(golden, tmp_path, change):
    directory, bank = frozen_fixture(golden, tmp_path)
    manifest_path = directory / "manifest.json"
    metadata = json.loads(manifest_path.read_text())
    if change == "tamper_unselected":
        with (bank / "03-quantities.tsv").open("a", encoding="utf-8") as stream:
            stream.write("\n")
    elif change == "omit_unselected":
        metadata["sources"].pop("03-quantities.tsv")
    else:
        metadata["sources"]["not-in-bank.tsv"] = "a" * 64
    manifest_path.write_text(json.dumps(metadata), encoding="utf-8")
    _, _, errors = golden.load_dataset(directory, bank)
    assert errors


def test_short_dialogue_dataset_returns_errors_without_index_crash(golden, tmp_path):
    directory, bank = frozen_fixture(golden, tmp_path)
    cases_path = directory / "cases.jsonl"
    rows = [json.loads(line) for line in cases_path.read_text().splitlines()]
    cases_path.write_text("".join(json.dumps(row) + "\n" for row in rows if row["case_id"] != "DLG-060"))
    metadata_path = directory / "manifest.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["case_sha256"] = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    metadata_path.write_text(json.dumps(metadata))
    _, _, errors = golden.load_dataset(directory, bank)
    assert errors


@pytest.mark.parametrize("damage", ["missing_subject", "dangling_parent", "cycle", "duplicate_subject"])
def test_forbidden_only_case_cannot_pass_with_invalid_subject_graph(golden, damage):
    forbidden = {"property": ["Масса"], "operator": "eq", "polarity": "positive", "status": ["supported"],
                 "subject": {"kind": "item"}}
    case = {"case_id": "x", "expected": {"required": [], "forbidden": [forbidden], "no_final": False}}
    actual = assertion()
    subjects = [{"id": "item", "kind": "item"}]
    if damage == "missing_subject":
        subjects = []
    elif damage in ("dangling_parent", "cycle"):
        parent = "missing" if damage == "dangling_parent" else "item"
        subjects = [{"id": "item", "kind": "item", "parent_subject_id": parent}]
        actual = {**actual, "subject": subjects[0]}
    else:
        subjects = [*subjects, {"id": "item", "kind": "package"}]
    result = golden.grade(case, observed(assertions=[actual], subjects=subjects))
    assert result["status"] != "pass_checks" and result["failures"]


def test_no_assertions_rejects_ambiguous_numeric_fill(golden):
    case = {"case_id": "x", "expected": {"required": [], "forbidden": [], "no_assertions": True}}
    actual = assertion(status="ambiguous")
    result = golden.grade(case, observed(assertions=[actual]))
    assert result["status"] == "fail_checks" and result["failures"]


@pytest.mark.parametrize("status", ["supported", "ambiguous"])
def test_no_assertions_allows_preserved_unparsed_literal(golden, status):
    text = "null"
    case = {"case_id": "x", "text": text, "expected": {
        "required": [], "forbidden": [], "no_assertions": True, "preserve_quotes": [text]}}
    actual = assertion(text, text, property_raw="Исходный текст", value_raw=text, status=status,
                       value={"type": "unparsed", "s": text, "reason": "No product fact"})
    result = golden.grade(case, observed(text, [actual]))
    assert result["status"] == "pass_checks" and not result["failures"]


@pytest.mark.parametrize("damage", ["cases", "manifest", "missing_artifact"])
def test_malformed_frozen_inputs_never_validate_as_success(golden, tmp_path, damage):
    directory, bank = frozen_fixture(golden, tmp_path)
    if damage == "cases":
        (directory / "cases.jsonl").write_text("{broken", encoding="utf-8")
    elif damage == "manifest":
        (directory / "manifest.json").write_text("null", encoding="utf-8")
    else:
        (directory / "dialogues.json").unlink()
    _, _, errors = golden.load_dataset(directory, bank)
    assert errors


def test_cli_preflight_validates_committed_assets_without_a_runtime(tmp_path):
    directory = ROOT / "eval/golden"
    if not (directory / "manifest.json").is_file():
        pytest.skip("Frozen production assets have not been written yet")
    result = subprocess.run([sys.executable, str(ROOT / "eval/run.py"), "golden", "--validate-only",
                             "--golden-dir", str(directory)], cwd=ROOT,
                            env={**os.environ, "KTRU_BUILD_DIR": str(tmp_path / "missing-runtime")},
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["validation_only"] and report["passed"] and not report["errors"]
    assert report["total"] == 300 and report["single"] == 280 and report["dialogue"] == 20
    assert not report["independent_acceptance"]
