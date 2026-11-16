"""Golden fixtures on a real pipeline: dialogue corrections (rename, move), card predicates and COMBO fixtures."""

import importlib.util
from pathlib import Path

import pytest

from src.ktru.service.pipeline import Pipeline
from src.ktru.service.stages import OpenModel

ROOT = Path(__file__).resolve().parents[3]
CATALOG = {
    "family": "mixed", "domain": "tools", "precondition": "test",
    "positions": [
        ("hammer-1", "Молоток", [("Масса", [{"kind": "range", "lo": "400", "hi": "1000", "okei_code": "163",
                                             "okei_name": "г", "value_code": "m1"}])]),
        ("chair-1", "Стул", [("Материал сиденья", [{"kind": "enum", "label": "Пластик", "value_code": "s1"}])]),
    ],
}


def load(name, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "eval"))
    spec = importlib.util.spec_from_file_location(name, ROOT / "eval" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def modules(monkeypatch):
    return load("golden_dialogues", monkeypatch), load("golden_combos", monkeypatch)


@pytest.fixture
def pipeline(modules, tmp_path):
    return Pipeline(modules[1].build_catalog(CATALOG, tmp_path))


def scenario(text, operation, required=(), forbidden=(), requirements=()):
    parse = {"action": "parse", "text": text, "session_id": None, "expected_revision": 0, "request_id_alias": "t1"}
    return {"group_id": "DLG-GT", "domain": "tools", "steps": [
        {"case_id": "DLG-T1", "turn": 1, "operation": parse, "required_state": [], "forbidden_state": [],
         "revision_delta": 1, "fixture_requirements": []},
        {"case_id": "DLG-T2", "turn": 2, "operation": {**operation, "request_id_alias": "t2"},
         "required_state": list(required), "forbidden_state": list(forbidden), "revision_delta": 1,
         "fixture_requirements": list(requirements)},
        {"case_id": "DLG-T3", "turn": 3, "operation": {"action": "undo", "request_id_alias": "t3"},
         "required_state": [], "forbidden_state": [], "revision_delta": 1, "fixture_requirements": []},
    ]}


def mass(subject_kind, **extra):
    return {"kind": "assertion", "property_aliases": ["Масса"], "subject_kind": subject_kind, "status": "supported",
            "operator": "eq", "polarity": "positive", "value": {"type": "number", "x": "800", "unit_name": "г"},
            **extra}


def test_subject_rename_runs_and_its_card_predicates_are_graded(modules, pipeline):
    rename = {"action": "edit", "subject_selector": {"kind": "item", "label": "стул"}, "new_label": "табурет"}
    rows = modules[0].run_scenario(scenario(
        "Стул, сиденье пластик.", rename, required=[{"kind": "subject", "subject_kind": "item", "label": "табурет"}],
        forbidden=[{"kind": "confirmed_family_from_prior_subject"}, {"kind": "final"}],
        requirements=["subject_name_edit_action"]), pipeline)
    assert [row["status"] for row in rows] == ["pass_checks"] * 3
    assert any(s["label"] == "табурет" for s in rows[1]["state"]["subjects"] if s["kind"] == "item")
    assert any(s["label"] == "стул" for s in rows[2]["state"]["subjects"] if s["kind"] == "item")


def test_wrong_subject_label_after_rename_fails(modules, pipeline):
    rename = {"action": "edit", "subject_selector": {"kind": "item", "label": "стул"}, "new_label": "табурет"}
    rows = modules[0].run_scenario(scenario(
        "Стул, сиденье пластик.", rename, required=[{"kind": "subject", "subject_kind": "item", "label": "кресло"}],
        requirements=["subject_name_edit_action"]), pipeline)
    assert rows[1]["status"] == "fail_checks" and rows[1]["failures"] == ["required_state[0] differs"]


def test_assertion_move_runs_and_the_old_subject_is_forbidden(modules, pipeline):
    move = {"action": "edit", "assertion_selector": mass("item"), "new_subject": {"kind": "package", "label": "кейс"}}
    rows = modules[0].run_scenario(scenario(
        "Молоток 800 г.", move, required=[mass("package", subject_label_aliases=["кейс"])],
        forbidden=[mass("item")], requirements=["assertion_subject_move_action"]), pipeline)
    assert [row["status"] for row in rows] == ["pass_checks"] * 3


@pytest.mark.parametrize("operation, reason", [
    ({"action": "edit", "assertion_selector": mass("item"), "new_status": "ambiguous", "extra_fact": mass("part")},
     "Operation adds a new fact together with the correction; no single edit supports it"),
    ({"action": "edit", "assertion_selector": mass("package"), "new_subject": {"kind": "item", "label": "молоток"}},
     "Declared assertion is not present in the current card"),
    ({"action": "edit", "subject_selector": {"kind": "item", "label": "табурет"}, "new_label": "стул"},
     "Declared subject is not present in the current card"),
])
def test_unsatisfiable_correction_stays_fixture_missing_with_precise_reason(modules, pipeline, operation, reason):
    rows = modules[0].run_scenario(scenario("Молоток 800 г.", operation), pipeline)
    assert [row["status"] for row in rows] == ["pass_checks", "fixture_missing", "fixture_missing"]
    assert rows[1]["reason"] == rows[2]["reason"] == reason


def test_card_predicates_read_the_contract_fields(modules):
    dialogues = modules[0]
    scope = {"coverage_status": "unconfirmed"}
    final = {"final": {"name": "x"}, "search_scope": scope, "subjects": [], "assertions": [], "text": "Стул"}
    assert dialogues._semantic_matches({"kind": "final_without_exhaustive_scope"}, final, None)
    assert not dialogues._semantic_matches({"kind": "final_without_exhaustive_scope"},
                                           {**final, "search_scope": {"coverage_status": "scope_exhaustive"}}, None)
    family = {"family": {"id": "f", "confirmed_by_text": True}, "subjects": [], "text": ""}
    assert dialogues._semantic_matches({"kind": "confirmed_family_from_prior_subject"}, family, family)
    assert not dialogues._semantic_matches({"kind": "confirmed_family_from_prior_subject"}, family,
                                           {"family": {"id": "other"}})
    blocked = {"diagnostics": [{"kind": "out_of_scope", "severity": "blocker"}], "subjects": [], "text": "Продам"}
    assert dialogues._semantic_matches({"kind": "request_description"}, blocked, None)
    assert not dialogues._semantic_matches({"kind": "request_description"}, {**blocked, "suggestion": {"name": "x"}},
                                           None)
    invented = {"subjects": [{"kind": "item", "label": "табурет"}], "text": "Продам вчера"}
    assert dialogues._semantic_matches({"kind": "invented_product"}, invented, None)
    assert not dialogues._semantic_matches({"kind": "invented_product"}, {**invented, "text": "Табурет"}, None)


def combo(case_id, text):
    return {"case_id": case_id, "text": text, "expected": {"required": [], "forbidden": [], "no_final": False}}


def test_same_request_replay_returns_the_cached_response(modules, pipeline):
    row = modules[1].run_combo(combo("COMBO-097", "Молоток 800 г."), pipeline)
    assert (row["status"], row["checked"], row["failures"], row["fixture"]) == ("pass_checks", 3, [], "request_replay")
    assert row["state"]["revision"] == 1


def test_changed_payload_replay_is_a_conflict_and_a_new_request_is_accepted(modules, pipeline):
    row = modules[1].run_combo(combo("COMBO-098", "Теперь дрель красная."), pipeline)
    assert (row["status"], row["checked"], row["failures"]) == ("pass_checks", 4, [])
    assert row["state"]["text"] == "Теперь дрель красная." and row["state"]["revision"] == 2


def test_cache_bypass_fails_the_replay_checks(modules, pipeline, monkeypatch):
    parse, seen = pipeline.parse, set()

    def recompute_retries(request):  # A retry under a fresh ID bypasses the request cache.
        retry = request.request_id in seen
        seen.add(request.request_id)
        return parse(request.model_copy(update={"request_id": request.request_id + "-new"}) if retry else request)

    monkeypatch.setattr(pipeline, "parse", recompute_retries)
    row = modules[1].run_combo(combo("COMBO-097", "Молоток 800 г."), pipeline)
    assert row["status"] == "fail_checks" and "exact_cached_response differs" in row["failures"]


def test_catalog_fixture_uses_the_evaluated_model_and_reports_its_absence(modules, tmp_path):
    def unavailable(text):
        raise RuntimeError("no recording")

    evaluated = Pipeline(modules[1].build_catalog(CATALOG, tmp_path), extractor=OpenModel(unavailable))
    case = {**combo("COMBO-015", "Стул металлический."), "expected": {"required": [], "forbidden": [],
                                                                        "preserve_quotes": ["Стул металлический"]}}
    row = modules[1].run_combo(case, evaluated, with_model=True)
    assert (row["status"], row["stage"]) == ("runtime_error", "parse+analyze")
    rules = modules[1].run_combo(case, evaluated)
    assert (rules["status"], rules["stage"]) == ("pass_checks", "parse_rules_only")
    assert rules["state"]["snapshot_id"] == "fixture-chair"


def test_records_without_a_reproducible_fixture_are_not_run(modules, pipeline):
    assert modules[1].run_combo(combo("COMBO-069", "Тип стула подтверждаю."), pipeline) is None


def presence(value):
    return {"kind": "assertion", "property_aliases": ["Наличие спинки"], "subject_kind": "item", "status": "supported",
            "operator": "eq", "polarity": "positive", "value": {"type": "bool", "v": value}}


def test_absent_item_field_is_added_by_its_label_not_dropped(modules, pipeline):
    correction = {"action": "edit", "value": False, "field_selector": {
        "field_label_aliases": ["Наличие спинки"], "subject_kind": "item", "match_count": 1}}
    rows = modules[0].run_scenario(scenario(
        "Стул, сиденье пластик.", correction, required=[presence(False)], forbidden=[presence(True)],
        requirements=["targeted_presence_correction_action"]), pipeline)
    assert [row["status"] for row in rows] == ["pass_checks"] * 3
    assert any(f["label_ru"] == "Наличие спинки" for f in rows[1]["state"]["fields"])


def test_absent_part_field_stays_blocked(modules, pipeline):
    edit = {"action": "edit", "patch": {"type": "set_field", "value": "50 см", "label_ru": "Ширина"},
            "field_selector": {"field_label_aliases": ["Ширина"], "subject_kind": "part",
                               "subject_label_aliases": ["сиденье"], "match_count": 1}}
    rows = modules[0].run_scenario(scenario("Стул, сиденье пластик.", edit), pipeline)
    assert rows[1]["reason"] == "Declared field is not present in the current card"
