"""Dialogue evaluation must exercise saved requests and retain blocked turns."""

import importlib.util
import json
import re
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ktru import contracts as C
from src.ktru.catalog.build import DDL
from src.ktru.catalog.reader import Catalog
from src.ktru.service.errors import ServiceError, _err
from src.ktru.service.pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[3]
INITIAL = "Отвертка крестовая, длина 200 мм."
CHANGED = "Отвертка шлицевая, длина 100 мм."


@pytest.fixture
def dialogues(monkeypatch):
    path = ROOT / "eval/golden_dialogues.py"
    assert path.is_file(), "Dialogue evaluator is not implemented"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("ktru_dialogues_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def length_predicate(value):
    return {"kind": "assertion", "property_aliases": ["Длина"], "subject_kind": "item", "status": "supported",
            "operator": "eq", "polarity": "positive", "quote": f"длина {value} мм",
            "value": {"type": "number", "x": value, "unit_name": "мм"}}


def plan():
    ids = [f"DLG-{i:03}" for i in range(76, 81)]
    operations = [
        {"action": "parse", "request_id_alias": "req-a", "session_id": None, "expected_revision": 0,
         "save_exact_request_as": "req-a", "save_response_as": "req-a-result", "text": INITIAL},
        {"action": "replay", "saved_request": "req-a", "same_payload": True,
         "compare_response_to": "req-a-result", "request_id_alias": "turn-2"},
        {"action": "replay", "saved_request": "req-a", "same_payload": False, "override": {"text": CHANGED},
         "request_id_alias": "turn-3"},
        {"action": "parse", "request_id_alias": "req-b", "session_id_source": "current_card",
         "revision_source": "current_card", "save_response_as": "req-b-result", "text": CHANGED},
        {"action": "replay", "saved_request": "req-a", "same_payload": True,
         "compare_response_to": "req-a-result", "current_state_must_equal": "req-b-result",
         "request_id_alias": "turn-5"},
    ]
    required = [[length_predicate("200")], [{"kind": "exact_cached_response", "saved_response": "req-a-result"}],
                [length_predicate("200")], [length_predicate("100")],
                [{"kind": "exact_cached_response", "saved_response": "req-a-result"},
                 {"kind": "current_server_state_equals", "saved_response": "req-b-result"}]]
    forbidden = [[length_predicate("100")], [], [length_predicate("100")], [length_predicate("200")],
                 [{"kind": "cached_response_promoted_to_current_state"}]]
    steps = [{"case_id": case_id, "turn": index + 1, "operation": operations[index],
              "raw_input": INITIAL if index in (0, 1, 4) else CHANGED,
              "required_state": required[index], "forbidden_state": forbidden[index],
              "revision_delta": [1, 0, 0, 1, 0][index],
              "expected_error": {"http_status": 409, "code": "request_conflict"} if index == 2 else None,
              "fixture_requirements": ["read_current_session_state", "client_response_revision_guard"] if index == 4
              else []} for index, case_id in enumerate(ids)]
    return {"group_id": "DLG-G016", "case_ids": ids, "domain": "tools", "step_count": 5, "steps": steps}


def card(request, revision):
    text = request.text
    value = "200" if text == INITIAL else "100"
    quote = f"длина {value} мм"
    start = text.index(quote)
    subject = C.SubjectRef(id="item", kind="item", label="отвертка")
    assertion = C.Assertion(id="length", subject_kind="item", subject=subject, property_raw="Длина",
                            value_raw=f"{value} мм", value=C.NumberValue(x=value, unit_name="мм"), origin="text",
                            extractor="rules", evidence=[C.TextEvidence(span=C.Span(
                                start=start, end=start + len(quote), text=quote, text_revision=revision))])
    field = C.CardField(id="length", label_ru="Длина", source="new", value=assertion.value,
                        display_ru=assertion.value_raw, assertion_ids=[assertion.id], status="from_text",
                        subject=subject)
    return C.CardState(session_id="test", revision=revision, request_id=request.request_id, snapshot_id="test",
                       status="provisional", text=text, assertions=[assertion], subjects=[subject], fields=[field],
                       readiness="no_code")


class ReplayPipeline:
    """Behavior fake: a cached response does not mutate the current session."""

    def __init__(self, error_status=409, error_code="request_conflict"):
        self.calls = []
        self.cache = {}
        self.current = None
        self.error_status = error_status
        self.error_code = error_code

    def parse(self, request):
        payload = request.model_dump(mode="json")
        self.calls.append(payload)
        cached = self.cache.get(request.request_id)
        if cached:
            original, response = cached
            if original != payload:
                raise _err(self.error_status, self.error_code, "Conflicting test request")
            return response.model_copy(deep=True)
        expected_revision = self.current.revision if self.current else 0
        if request.expected_revision != expected_revision:
            raise _err(409, "stale_revision", "Stale test revision")
        self.current = card(request, expected_revision + 1)
        self.cache[request.request_id] = (payload, self.current.model_copy(deep=True))
        return self.current.model_copy(deep=True)

    def get(self, session_id):
        assert session_id == "test"
        return self.current.model_copy(deep=True)


class FieldPipeline(ReplayPipeline):
    """Behavior fake: field edits and undo mutate the current session and bump the revision."""

    def __init__(self):
        super().__init__()
        self.undo_text = []

    def _next(self, text, request_id, revision):
        request = C.ParseRequest(request_id=request_id, expected_revision=revision, text=text)
        self.current = card(request, revision)
        return self.current.model_copy(deep=True)

    def edit(self, request):
        self.calls.append(request.model_dump(mode="json"))
        if self.current is None or request.expected_revision != self.current.revision:
            raise _err(409, "stale_revision", "Stale test revision")
        self.undo_text.append(self.current.text)
        patch = request.patch
        if isinstance(patch, C.ReplaceTextPatch):
            text = patch.text
        else:
            number = re.search(r"\d+", patch.value)
            text = f"Отвертка крестовая, длина {number.group()} мм." if number else self.current.text
        return self._next(text, request.request_id, request.expected_revision + 1)

    def undo(self, request):
        self.calls.append(request.model_dump(mode="json"))
        if self.current is None or request.expected_revision != self.current.revision:
            raise _err(409, "stale_revision", "Stale test revision")
        if not self.undo_text:
            raise _err(409, "invalid_input", "Nothing to undo")
        return self._next(self.undo_text.pop(), request.request_id, request.expected_revision + 1)


def field_plan(step_count=3):
    """A plan whose edit step resolves a real current-card field by label and subject."""
    ids = [f"DLG-{i:03}" for i in range(76, 76 + step_count)]
    operations = [
        {"action": "parse", "request_id_alias": "req-a", "session_id": None, "expected_revision": 0,
         "save_response_as": "req-a-result", "text": INITIAL},
        {"action": "edit", "patch": {"type": "set_field", "value": "100 мм", "label_ru": "Длина"},
         "field_selector": {"field_label_aliases": ["Длина"], "subject_kind": "item", "match_count": 1,
                            "resolve_from": "current_card.fields"},
         "revision_source": "current_card", "request_id_alias": "edit-1"},
        {"action": "undo", "revision_source": "current_card", "request_id_alias": "undo-1"},
        {"action": "answer",
         "question_selector": {"kind": "characteristic", "char_name_aliases": ["Материал"], "subject_kind": "item",
                               "resolve_from": "current_card.clarification", "match_count": 1},
         "option_selector": {"label_aliases": ["Хлопок"], "match_count": 1, "resolve_from": "current_question.options"},
         "revision_source": "current_card", "request_id_alias": "answer-1"},
        {"action": "parse", "request_id_alias": "fresh", "session_id": None, "expected_revision": 0, "text": CHANGED},
    ]
    required = [[length_predicate("200")], [length_predicate("100")], [length_predicate("200")], [], []]
    forbidden = [[length_predicate("100")], [length_predicate("200")], [length_predicate("100")], [], []]
    deltas = [1, 1, 1, 1, 1]
    fixtures = [[], ["dynamic_current_field"], [], ["dynamic_current_question"], []]
    steps = [{"case_id": case_id, "turn": index + 1, "operation": operations[index],
              "raw_input": INITIAL if index != 4 else CHANGED, "required_state": required[index],
              "forbidden_state": forbidden[index], "revision_delta": deltas[index], "expected_error": None,
              "fixture_requirements": fixtures[index]} for index, case_id in enumerate(ids)]
    return {"group_id": "DLG-G016", "case_ids": ids, "domain": "tools", "step_count": step_count, "steps": steps}


def test_saved_request_replay_conflict_and_late_cache_are_checked(dialogues):
    pipeline = ReplayPipeline()
    rows = dialogues.run_scenario(plan(), pipeline)
    assert len(rows) == 5
    assert [row["case_id"] for row in rows] == plan()["case_ids"]
    assert all(row["status"] == "pass_checks" for row in rows)
    assert all(row["checked"] > 0 for row in rows)
    assert len(pipeline.calls) == 5
    assert pipeline.calls[0] == pipeline.calls[1] == pipeline.calls[4]
    assert pipeline.calls[2]["request_id"] == pipeline.calls[0]["request_id"]
    assert pipeline.calls[2]["text"] == CHANGED
    assert pipeline.calls[3]["session_id"] == "test" and pipeline.calls[3]["expected_revision"] == 1
    assert pipeline.current.text == CHANGED and pipeline.current.revision == 2
    # Backend success cannot claim that the browser rejected the stale response.
    assert rows[-1].get("full_behavior_status") != "pass"


@pytest.mark.parametrize("status, code", [(400, "request_conflict"), (409, "stale_revision")])
def test_expected_error_requires_status_and_code(dialogues, status, code):
    rows = dialogues.run_scenario(plan(), ReplayPipeline(error_status=status, error_code=code))
    assert len(rows) == 5 and rows[2]["status"] != "pass_checks"
    assert rows[2]["failures"]


@pytest.mark.parametrize("action", ["answer", "delete", "invented"])
def test_unavailable_action_blocks_later_turns_without_parse_substitution(dialogues, action):
    scenario = plan()
    scenario["steps"][1]["operation"] = {"action": action, "text": "Новый текст не является parse"}
    pipeline = ReplayPipeline()
    rows = dialogues.run_scenario(scenario, pipeline)
    assert len(rows) == 5
    assert rows[0]["status"] == "pass_checks"
    assert all(row["status"] == "fixture_missing" and row["checked"] == 0 for row in rows[1:])
    assert len(pipeline.calls) == 1


def test_current_card_field_edit_and_undo_execute_and_are_graded(dialogues):
    pipeline = FieldPipeline()
    rows = dialogues.run_scenario(field_plan(), pipeline)
    assert [row["status"] for row in rows] == ["pass_checks", "pass_checks", "pass_checks"]
    assert all(row["checked"] > 0 for row in rows)
    assert rows[1]["state"]["revision"] == 2 and rows[1]["state"]["text"] == "Отвертка крестовая, длина 100 мм."
    assert rows[2]["state"]["revision"] == 3 and rows[2]["state"]["text"] == INITIAL
    assert pipeline.calls[1]["patch"] == {"type": "set_field", "label_ru": "Длина", "value": "100 мм",
                                          "field_id": "length"}
    assert pipeline.calls[1]["session_id"] == "test" and pipeline.calls[1]["expected_revision"] == 1
    assert "patch" not in pipeline.calls[2] and pipeline.calls[2]["expected_revision"] == 2


def test_edit_step_required_quote_must_still_be_grounded(dialogues):
    scenario = field_plan()
    ungrounded = length_predicate("100")
    ungrounded["quote"] = "длина 999 мм"
    scenario["steps"][1]["required_state"] = [ungrounded]
    rows = dialogues.run_scenario(scenario, FieldPipeline())
    assert rows[1]["status"] == "fail_checks" and rows[1]["failures"]


def test_replace_text_edit_executes_on_the_current_session(dialogues):
    scenario = field_plan()
    scenario["steps"][1]["operation"] = {"action": "edit", "patch": {"type": "replace_text", "text": CHANGED},
                                         "revision_source": "current_card", "request_id_alias": "replace-1"}
    scenario["steps"][1]["fixture_requirements"] = []
    pipeline = FieldPipeline()
    rows = dialogues.run_scenario(scenario, pipeline)
    assert [row["status"] for row in rows] == ["pass_checks", "pass_checks", "pass_checks"]
    assert pipeline.calls[1]["patch"] == {"type": "replace_text", "text": CHANGED}
    assert rows[1]["state"]["text"] == CHANGED and rows[2]["state"]["text"] == INITIAL


def test_absent_field_uses_declared_unbound_label(dialogues):
    scenario = field_plan()
    scenario["steps"][1]["operation"] = {
        "action": "edit", "patch": {"type": "set_field", "value": "Синий", "label_ru": "Цвет"},
        "field_selector": {"field_label_aliases": ["Цвет"], "subject_kind": "item", "match_count": 1,
                           "resolve_from": "current_card.fields",
                           "if_absent": {"add_unbound_field": True, "label_ru": "Цвет", "subject_kind": "item"}},
        "revision_source": "current_card", "request_id_alias": "edit-color"}
    scenario["steps"][1]["required_state"] = []
    scenario["steps"][1]["forbidden_state"] = []
    pipeline = FieldPipeline()
    rows = dialogues.run_scenario(scenario, pipeline)
    assert rows[1]["status"] == "pass_checks" and rows[1]["state"]["revision"] == 2
    assert pipeline.calls[1]["patch"] == {"type": "set_field", "label_ru": "Цвет", "value": "Синий",
                                          "field_id": None}


def test_answer_for_absent_question_stays_fixture_missing_with_precise_reason(dialogues):
    scenario = plan()
    scenario["steps"][1]["operation"] = {
        "action": "answer",
        "question_selector": {"kind": "characteristic", "char_name_aliases": ["Материал"], "subject_kind": "item",
                              "resolve_from": "current_card.clarification", "match_count": 1},
        "option_selector": {"label_aliases": ["Хлопок"], "match_count": 1, "resolve_from": "current_question.options"},
        "revision_source": "current_card", "request_id_alias": "answer-1"}
    scenario["steps"][1]["fixture_requirements"] = ["dynamic_current_question"]
    pipeline = ReplayPipeline()
    rows = dialogues.run_scenario(scenario, pipeline)
    assert len(rows) == 5 and rows[0]["status"] == "pass_checks"
    assert all(row["status"] == "fixture_missing" and row["checked"] == 0 for row in rows[1:])
    assert all(row["reason"] == "Declared question is not present in the current card" for row in rows[1:])
    assert len(pipeline.calls) == 1


def test_blocked_turn_keeps_remaining_turns_blocked_without_fresh_session(dialogues):
    scenario = field_plan(5)
    steps = scenario["steps"]
    steps[2], steps[3] = steps[3], steps[2]
    for turn, step in enumerate(steps, 1):
        step["turn"], step["case_id"] = turn, scenario["case_ids"][turn - 1]
    pipeline = FieldPipeline()
    rows = dialogues.run_scenario(scenario, pipeline)
    assert [row["status"] for row in rows[:2]] == ["pass_checks", "pass_checks"]
    assert all(row["status"] == "fixture_missing" and row["checked"] == 0 for row in rows[2:])
    assert all(row["reason"] == "Declared question is not present in the current card" for row in rows[2:])
    # The later undo and fresh-session parse must not be substituted for the blocked answer.
    assert len(pipeline.calls) == 2


@pytest.mark.parametrize("missing", ["fixture", "predicate", "saved_request"])
def test_unreproduced_requirements_block_chain(dialogues, missing):
    scenario = plan()
    if missing == "fixture":
        scenario["steps"][1]["fixture_requirements"] = ["dynamic_current_question"]
    elif missing == "predicate":
        scenario["steps"][1]["required_state"] = [{"kind": "unimplemented_semantic_oracle"}]
    else:
        scenario["steps"][1]["operation"]["saved_request"] = "not-saved"
    pipeline = ReplayPipeline()
    rows = dialogues.run_scenario(scenario, pipeline)
    assert len(rows) == 5 and all(row["status"] == "fixture_missing" for row in rows[1:])
    assert len(pipeline.calls) == 1


def test_revision_delta_mismatch_is_a_failed_check(dialogues):
    scenario = plan()
    scenario["steps"][1]["revision_delta"] = 1
    rows = dialogues.run_scenario(scenario, ReplayPipeline())
    assert rows[1]["status"] == "fail_checks" and rows[1]["failures"]


def test_wrong_assertion_subject_or_polarity_cannot_pass(dialogues):
    scenario = plan()
    scenario["steps"][0]["required_state"][0]["subject_kind"] = "package"
    scenario["steps"][0]["required_state"][0]["polarity"] = "negative"
    rows = dialogues.run_scenario(scenario, ReplayPipeline())
    assert rows[0]["status"] == "fail_checks" and rows[0]["failures"]


class PromotedCachePipeline(ReplayPipeline):
    def parse(self, request):
        response = super().parse(request)
        self.current = response.model_copy(deep=True)
        return response


def test_late_cached_response_cannot_replace_current_server_state(dialogues):
    rows = dialogues.run_scenario(plan(), PromotedCachePipeline())
    assert len(rows) == 5
    assert rows[-1]["status"] == "fail_checks" and rows[-1]["failures"]


@pytest.fixture
def catalog(tmp_path):
    with sqlite3.connect(tmp_path / "catalog.sqlite") as db:
        db.executescript(DDL)
        db.execute("INSERT INTO positions (code,version,name,status,is_template,family_id,domain,supply_units) "
                   "VALUES ('screwdriver',1,'Отвертка','ACTIVE',0,'screwdriver','tools','[]')")
        db.execute("INSERT INTO positions_fts VALUES ('screwdriver',1,'отвертка',NULL,'длина')")
    (tmp_path / "manifest.json").write_text(json.dumps({"snapshot_id": "test-dialogues", "counts": {"final": 1}}))
    return Catalog(tmp_path)


def test_real_pipeline_keeps_current_session_after_old_request_replay(dialogues, catalog):
    scenario = deepcopy(plan())
    # This integration checks session behavior; model extraction is not part of this fixture.
    for step in scenario["steps"]:
        step["required_state"] = [item for item in step["required_state"] if item["kind"] != "assertion"]
        step["forbidden_state"] = [item for item in step["forbidden_state"] if item["kind"] != "assertion"]
    pipeline = Pipeline(catalog)
    rows = dialogues.run_scenario(scenario, pipeline)
    assert len(rows) == 5 and all(row["status"] == "pass_checks" for row in rows)
    assert rows[-1]["checked"] > 0


def test_golden_evaluation_uses_dialogue_plan_once_and_keeps_all_turns(dialogues):
    import golden

    scenario = plan()
    dataset = [{"case_id": step["case_id"], "group_id": scenario["group_id"], "domain": "tools",
                "tags": [f"turn_{step['turn']}"], "text": step["raw_input"], "precondition": "Prior turn",
                "expected": {"required": [], "forbidden": [], "no_final": False}}
               for step in scenario["steps"]]
    pipeline = ReplayPipeline()
    pipeline.catalog = SimpleNamespace(snapshot_id="test")
    report = golden.evaluate(dataset, pipeline, dialogues={"scenarios": [scenario]})
    assert report["total"] == 5 and len(report["cases"]) == 5
    assert [row["case_id"] for row in report["cases"]] == scenario["case_ids"]
    assert all(row["status"] == "pass_checks" for row in report["cases"])
    assert report["checked"] > 0 and len(pipeline.calls) == 5
    assert report["full_behavior_assessed"] == 0 and not report["independent_acceptance"]
    assert pipeline.current.text == CHANGED and pipeline.current.revision == 2


@pytest.mark.parametrize("wrong_card", ["response", "current", "both"])
def test_accepted_request_must_keep_exact_text_in_response_and_current_state(dialogues, wrong_card):
    class WrongTextPipeline(ReplayPipeline):
        def parse(self, request):
            response = super().parse(request)
            if wrong_card in {"current", "both"}:
                self.current = self.current.model_copy(update={"text": "Другой товар, длина 200 мм."}, deep=True)
            if wrong_card in {"response", "both"}:
                response = response.model_copy(update={"text": "Другой товар, длина 200 мм."}, deep=True)
            return response

    rows = dialogues.run_scenario(plan(), WrongTextPipeline())
    assert len(rows) == 5
    assert rows[0]["status"] != "pass_checks" and rows[0]["failures"]


@pytest.mark.parametrize("corruption", ["quote", "revision", "missing"])
def test_dialogue_typed_anchor_needs_current_source_evidence(dialogues, corruption):
    class CorruptEvidencePipeline(ReplayPipeline):
        def parse(self, request):
            response = super().parse(request)
            original = response.assertions[0]
            if corruption == "missing":
                evidence = []
            else:
                source = original.evidence[0]
                changes = {"text": "длина 999 мм"} if corruption == "quote" else {"text_revision": 0}
                evidence = [source.model_copy(update={"span": source.span.model_copy(update=changes)})]
            corrupted = original.model_copy(update={"evidence": evidence})
            self.current = response.model_copy(update={"assertions": [corrupted]}, deep=True)
            return self.current.model_copy(deep=True)

    rows = dialogues.run_scenario(plan(), CorruptEvidencePipeline())
    assert len(rows) == 5
    assert rows[0]["status"] != "pass_checks" and rows[0]["failures"]


def test_expected_conflict_cannot_change_server_state_at_same_revision(dialogues):
    class MutatingConflictPipeline(ReplayPipeline):
        def parse(self, request):
            try:
                return super().parse(request)
            except ServiceError:
                # No requested assertion or revision changed, but the rejected request still altered the card.
                self.current = self.current.model_copy(update={"readiness": "needs_answers"}, deep=True)
                raise

    rows = dialogues.run_scenario(plan(), MutatingConflictPipeline())
    assert len(rows) == 5
    assert rows[2]["status"] == "fail_checks" and rows[2]["failures"]


def test_required_length_cannot_use_valid_span_for_an_unrelated_product_word(dialogues):
    class UnrelatedEvidencePipeline(ReplayPipeline):
        def parse(self, request):
            response = super().parse(request)
            original = response.assertions[0]
            word = "Отвертка"
            unrelated = C.TextEvidence(span=C.Span(start=0, end=len(word), text=word,
                                                  text_revision=response.revision))
            corrupted = original.model_copy(update={"evidence": [unrelated]})
            self.current = response.model_copy(update={"assertions": [corrupted]}, deep=True)
            return self.current.model_copy(deep=True)

    rows = dialogues.run_scenario(plan(), UnrelatedEvidencePipeline())
    assert rows[0]["status"] == "fail_checks" and rows[0]["failures"]


def test_required_length_cannot_be_a_catalog_value_without_input_evidence(dialogues):
    class CatalogDefaultPipeline(ReplayPipeline):
        def parse(self, request):
            response = super().parse(request)
            default = response.assertions[0].model_copy(update={"origin": "catalog", "evidence": []})
            self.current = response.model_copy(update={"assertions": [default]}, deep=True)
            return self.current.model_copy(deep=True)

    rows = dialogues.run_scenario(plan(), CatalogDefaultPipeline())
    assert rows[0]["status"] == "fail_checks" and rows[0]["failures"]


def test_unsupported_forbidden_length_is_counted_despite_valid_required_length(dialogues):
    class HallucinatedLengthPipeline(ReplayPipeline):
        def parse(self, request):
            response = super().parse(request)
            fabricated = response.assertions[0].model_copy(update={
                "id": "fabricated", "value_raw": "100 мм", "value": C.NumberValue(x="100", unit_name="мм"),
                "evidence": [], "span": None})
            self.current = response.model_copy(update={"assertions": [*response.assertions, fabricated]}, deep=True)
            return self.current.model_copy(deep=True)

    rows = dialogues.run_scenario(plan(), HallucinatedLengthPipeline())
    assert rows[0]["status"] == "fail_checks" and rows[0]["failures"]
    assert any("forbidden" in failure for failure in rows[0]["failures"])


@pytest.mark.parametrize("quote", [None, "длина 999 мм"])
def test_missing_or_wrong_reviewed_required_quote_blocks_fixture(dialogues, quote):
    scenario = plan()
    predicate = scenario["steps"][0]["required_state"][0]
    if quote is None:
        predicate.pop("quote")
    else:
        predicate["quote"] = quote
    pipeline = ReplayPipeline()
    rows = dialogues.run_scenario(scenario, pipeline)
    assert len(rows) == 5
    assert all(row["status"] == "fixture_missing" and row["checked"] == 0 for row in rows)
    assert not pipeline.calls
