"""HTTP contract regressions with a small real catalog and no inference network calls."""

import pytest
from fastapi.testclient import TestClient

from src.api.ktru.app import app
from src.ktru import contracts as C
from src.ktru.ml import extract as ml
from tests.ktru.test_s0_completion import catalog as small_catalog  # noqa: F401

PREFIX = "/api/ktru"


@pytest.fixture
def client(small_catalog, monkeypatch):  # noqa: F811
    monkeypatch.setenv("KTRU_SNAPSHOT_DIR", str(small_catalog.dir))
    monkeypatch.delenv("KTRU_LLM_URL", raising=False)
    monkeypatch.delenv("KTRU_LLM_MODEL", raising=False)
    with TestClient(app) as connection:
        yield connection


def parse(client, text="Молоток", request_id="parse"):
    response = client.post(f"{PREFIX}/parse", json={
        "request_id": request_id, "expected_revision": 0, "text": text,
    })
    assert response.status_code == 200, response.text
    return response.json()


def mutation(state, request_id):
    return {"session_id": state["session_id"], "request_id": request_id,
            "expected_revision": state["revision"]}


def confirm_family(client, state):
    question = state["clarification"]
    if state["search_scope"]["coverage_status"] == "family_confirmed":
        return state  # the description named the family completely
    assert question and question["kind"] == "family"
    response = client.post(f"{PREFIX}/answer", json={
        **mutation(state, "family"), "question_id": question["id"], "option_id": question["options"][0]["id"],
    })
    assert response.status_code == 200, response.text
    return response.json()


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    body = C.ErrorResponse.model_validate(response.json()).error
    assert body.code == code and body.message_ru
    return body


def test_health_parse_and_position_return_current_contract(client):
    response = client.get(f"{PREFIX}/health")
    assert response.status_code == 200
    health = C.Health.model_validate(response.json())
    assert health.ready and health.snapshot_id == "test-s0"
    assert health.contract_version == C.CONTRACT_VERSION and health.llm == "disabled"
    assert health.counts["final"] == 2

    state = C.CardState.model_validate(parse(client, "Молоток, масса 500 г"))
    assert state.revision == 1 and state.status == "provisional"
    # «Молоток» names the only matching family completely: confirmed without a question, mass selects hammer-1.
    assert state.final and state.final.position_ref.code == "hammer-1"
    assert state.search_scope.coverage_status == "family_confirmed"
    mass = next(assertion for assertion in state.assertions if isinstance(assertion.value, C.NumberValue))
    assert mass.value.x == "500" and mass.value.unit_okei == "163"

    detail = client.get(f"{PREFIX}/positions/hammer-1", params={"snapshot_id": "test-s0", "version": 1})
    assert detail.status_code == 200
    position = C.PositionDetail.model_validate(detail.json())
    assert position.ref.code == "hammer-1" and position.characteristics[0].char_code == "mass"


def test_split_request_returns_independent_descriptions(client):
    response = client.post(f"{PREFIX}/split", json={"text": "молоток и гвозди"})
    assert response.status_code == 200
    assert response.json() == ["молоток", "гвозди"]
    descriptions = [f"Молоток, индивидуальная гравировка логотипа партии {i}, масса 500 г" for i in range(80)]
    text = "\n".join(descriptions)
    assert C.MAX_TEXT < len(text) < C.MAX_SPLIT_TEXT
    response = client.post(f"{PREFIX}/split", json={"text": text})
    assert response.status_code == 200
    assert len(response.json()) == len(descriptions)
    assert "партии 79" in response.json()[-1]
    assert all(len(row) <= C.MAX_TEXT for row in response.json())
    assert_error(client.post(f"{PREFIX}/split", json={"text": "x" * (C.MAX_SPLIT_TEXT + 1)}), 422, "invalid_input")


@pytest.mark.parametrize("version", ["0", "-1", "9" * 100])
def test_position_rejects_versions_outside_storage_range(client, version):
    response = client.get(f"{PREFIX}/positions/hammer-1", params={"snapshot_id": "test-s0", "version": version})
    assert_error(response, 422, "invalid_input")


def test_analyze_publishes_grounded_extra_and_rejection_without_network(client, monkeypatch):
    text = "Молоток, масса 500 г, покрытие водоотталкивающее"
    state = parse(client, text)
    calls = []
    monkeypatch.setattr(ml, "config", lambda: ("http://local-inference.invalid", "open-test-model"))

    def extract(accepted_text):
        calls.append(accepted_text)
        return [{"property": "Покрытие", "value": "водоотталкивающее", "quote": "покрытие водоотталкивающее"},
                {"property": "Цвет", "value": "красный", "quote": "красный"}]

    monkeypatch.setattr(ml, "call_model", extract)
    response = client.post(f"{PREFIX}/analyze", json=mutation(state, "analyze"))
    assert response.status_code == 200, response.text
    analyzed = C.CardState.model_validate(response.json())
    assert calls == [text] and analyzed.revision == state["revision"] + 1
    assert analyzed.status == "analyzed"
    coating = next(assertion for assertion in analyzed.assertions if assertion.property_raw == "Покрытие")
    assert coating.extractor == "llm" and coating.span.text == "покрытие водоотталкивающее"
    assert analyzed.rejected_outputs and analyzed.rejected_outputs[0].reason_ru
    assert any("водоотталкивающее" in field.display_ru for field in analyzed.fields)


def test_stale_mutation_returns_revision_and_keeps_accepted_state(client):
    state = parse(client, "Молоток, масса 500 г")
    response = client.post(f"{PREFIX}/edit", json={
        **mutation(state, "stale-edit"), "expected_revision": 0,
        "patch": {"type": "replace_text", "text": "Молоток, масса 700 г"},
    })
    error = assert_error(response, 409, "stale_revision")
    assert error.retryable and error.current_revision == state["revision"]
    assert error.request_id == "stale-edit"
    assert client.get(f"{PREFIX}/sessions/{state['session_id']}").json() == state


def test_initial_parse_request_id_replays_and_rejects_different_payload(client):
    first = parse(client, "Молоток, масса 500 г", "initial-once")
    assert parse(client, "Молоток, масса 500 г", "initial-once") == first
    response = client.post(f"{PREFIX}/parse", json={
        "request_id": "initial-once", "expected_revision": 0, "text": "Молоток, масса 700 г",
    })
    error = assert_error(response, 409, "request_conflict")
    assert error.request_id == "initial-once"
    assert client.get(f"{PREFIX}/sessions/{first['session_id']}").json() == first


def test_analyze_request_id_replays_without_second_model_call(client, monkeypatch):
    state = parse(client, "Молоток, масса 500 г")
    calls = []
    monkeypatch.setattr(ml, "config", lambda: ("http://local-inference.invalid", "open-test-model"))
    monkeypatch.setattr(ml, "call_model", lambda text: calls.append(text) or [])
    request = mutation(state, "analyze-once")
    first = client.post(f"{PREFIX}/analyze", json=request)
    assert first.status_code == 200
    assert client.post(f"{PREFIX}/analyze", json=request).json() == first.json()
    assert calls == [state["text"]]
    changed = client.post(f"{PREFIX}/analyze", json={**request, "expected_revision": first.json()["revision"]})
    assert_error(changed, 409, "request_conflict")
    assert len(calls) == 1


@pytest.mark.parametrize("change", [
    {"text": None}, {"text": ""}, {"text": {"value": "Молоток"}}, {"text": "м" * (C.MAX_TEXT + 1)},
    {"expected_revision": -1}, {"unexpected_field": True},
])
def test_invalid_parse_payload_returns_error_contract(client, change):
    request = {"request_id": "invalid-parse", "expected_revision": 0, "text": "Молоток", **change}
    error = assert_error(client.post(f"{PREFIX}/parse", json=request), 422, "invalid_input")
    assert not error.retryable


@pytest.mark.parametrize("value", [
    {"type": "number", "x": 500}, {"type": "number", "x": "NaN"},
    {"type": "number", "x": "1e9999999"}, {"type": "number", "x": "500", "unit_okei": "999"},
    {"type": "number", "x": "500", "unit_okei": "003", "unit_name": "мм"},
    {"type": "number", "x": "500", "unit_okei": "163", "unit_name": "кг"},
    {"type": "range"}, {"type": "range", "lo": "800", "hi": "400"},
    {"type": "text", "s": "500 г"},
])
def test_invalid_typed_answer_does_not_change_revision(client, value):
    state = confirm_family(client, parse(client))
    question = state["clarification"]
    assert question["accepts_value"]
    response = client.post(f"{PREFIX}/answer", json={
        **mutation(state, "invalid-value"), "question_id": question["id"], "value": value,
    })
    assert_error(response, 422, "invalid_input")
    assert client.get(f"{PREFIX}/sessions/{state['session_id']}").json() == state


def test_answer_requires_exactly_one_option_or_value(client):
    state = parse(client)
    question = state["clarification"]
    request = {**mutation(state, "invalid-answer"), "question_id": question["id"]}
    assert_error(client.post(f"{PREFIX}/answer", json=request), 422, "invalid_input")
    assert_error(client.post(f"{PREFIX}/answer", json={
        **request, "option_id": question["options"][0]["id"], "value": {"type": "number", "x": "500"},
    }), 422, "invalid_input")


def test_typed_answer_converts_units_and_final_requires_confirmed_family(client):
    provisional = parse(client)
    assert provisional["final"] is None
    state = confirm_family(client, provisional)
    question = state["clarification"]
    assert state["final"] is None and question["accepts_value"]
    assert question["value_unit_okei"] == "163"
    response = client.post(f"{PREFIX}/answer", json={
        **mutation(state, "typed-mass"), "question_id": question["id"],
        "value": {"type": "number", "x": "0.5", "unit_name": "кг"},
    })
    assert response.status_code == 200, response.text
    result = C.CardState.model_validate(response.json())
    assert {candidate.ref.code for candidate in result.candidates if candidate.status == "alive"} == {"hammer-1"}
    assert result.final and result.final.position_ref.code == "hammer-1" and result.final.conditional_on_family
    assert result.final.scope.coverage_status == "family_confirmed"
    assert result.final.scope.confirmation_evidence
    answer = next(assertion for assertion in result.assertions if assertion.origin == "user_answer")
    evidence = next(item for item in answer.evidence if isinstance(item, C.UserEvidence))
    assert evidence.question_id == question["id"] and evidence.revision == result.revision


def test_missing_session_and_wrong_position_snapshot_use_error_contract(client):
    assert_error(client.get(f"{PREFIX}/sessions/nonexistent"), 404, "session_expired")
    response = client.get(f"{PREFIX}/positions/hammer-1", params={"snapshot_id": "other", "version": 1})
    assert_error(response, 404, "not_found")


def test_disabled_model_keeps_fast_preview(client):
    state = parse(client, "Молоток, масса 500 г")
    error = assert_error(client.post(f"{PREFIX}/analyze", json=mutation(state, "disabled-model")), 503, "unavailable")
    assert not error.retryable
    assert client.get(f"{PREFIX}/sessions/{state['session_id']}").json() == state


def test_field_edit_and_undo_preserve_confirmed_family(client):
    state = confirm_family(client, parse(client, "Молоток, масса 500 г"))
    assert state["final"]["position_ref"]["code"] == "hammer-1"
    target = next(field for field in state["fields"] if field["label_ru"] == "Масса молотка")
    response = client.post(f"{PREFIX}/edit", json={
        **mutation(state, "edit-mass"),
        "patch": {"type": "set_field", "field_id": target["id"], "label_ru": target["label_ru"], "value": "700 г"},
    })
    assert response.status_code == 200, response.text
    edited = C.CardState.model_validate(response.json())
    assert edited.can_undo and edited.final.position_ref.code == "hammer-2"
    assertion = next(assertion for assertion in edited.assertions if assertion.origin == "user_edit")
    evidence = next(item for item in assertion.evidence if isinstance(item, C.UserEvidence))
    assert evidence.field_id == target["id"] and evidence.revision == edited.revision
    response = client.post(f"{PREFIX}/undo", json=mutation(response.json(), "undo-mass"))
    assert response.status_code == 200, response.text
    restored = C.CardState.model_validate(response.json())
    assert restored.revision == edited.revision + 1 and restored.final.position_ref.code == "hammer-1"
    assert restored.text == state["text"]
    assert not any(assertion.origin == "user_edit" for assertion in restored.assertions)


def test_span_edit_http_changes_only_selected_unicode_occurrence_and_replays(client):
    text = "🔧 Молоток; Молоток; масса упаковки 700 г"
    state = parse(client, text, "unicode-span")
    start = text.index("Молоток", text.index(";") + 1)
    request = {**mutation(state, "unicode-span-edit"), "patch": {
        "type": "replace_span", "snapshot_id": state["snapshot_id"], "base_revision": state["revision"],
        "span": {"start": start, "end": start + len("Молоток"), "text": "Молоток", "text_revision": state["revision"]},
        "replacement": "Молоток новый",
    }}
    response = client.post(f"{PREFIX}/edit", json=request)
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["text"] == "🔧 Молоток; Молоток новый; масса упаковки 700 г"
    C.CardState.model_validate(changed)
    assert client.post(f"{PREFIX}/edit", json=request).json() == changed
    restored = client.post(f"{PREFIX}/undo", json=mutation(changed, "unicode-span-undo"))
    assert restored.status_code == 200, restored.text
    assert restored.json()["text"] == text and restored.json()["revision"] == changed["revision"] + 1
    request.update(**mutation(restored.json(), "stale-unicode-span"))
    assert_error(client.post(f"{PREFIX}/edit", json=request), 409, "stale_revision")
    assert client.get(f"{PREFIX}/sessions/{state['session_id']}").json() == restored.json()


def test_model_failure_reports_partial_state_without_provider_details(client, monkeypatch):
    state = parse(client, "Молоток, масса 500 г")
    monkeypatch.setattr(ml, "config", lambda: ("http://local-inference.invalid", "open-test-model"))

    def failing_model(_):
        raise TimeoutError("private-provider-detail")

    monkeypatch.setattr(ml, "call_model", failing_model)
    response = client.post(f"{PREFIX}/analyze", json=mutation(state, "failed-analysis"))
    assert response.status_code == 200, response.text
    result = C.CardState.model_validate(response.json())
    assert result.status == "partial"
    assert any(diagnostic.kind == "model_unavailable" for diagnostic in result.diagnostics)
    assert any(isinstance(assertion.value, C.NumberValue) for assertion in result.assertions)
    assert "private-provider-detail" not in response.text


def test_empty_examples_and_invalid_filter_have_explicit_responses(client):
    response = client.get(f"{PREFIX}/examples", params={"domain": "tools", "n": 2})
    assert response.status_code == 200 and response.json() == []
    assert_error(client.get(f"{PREFIX}/examples", params={"domain": "unknown"}), 422, "invalid_input")
    assert_error(client.get(f"{PREFIX}/examples", params={"n": 31}), 422, "invalid_input")


def test_unavailable_snapshot_reports_not_ready_and_error(client, monkeypatch):
    monkeypatch.setattr(app.state, "pipeline", None)
    response = client.get(f"{PREFIX}/health")
    assert response.status_code == 200
    health = C.Health.model_validate(response.json())
    assert not health.ready and health.snapshot_id is None and health.counts == {}
    response = client.post(f"{PREFIX}/parse", json={"request_id": "offline", "expected_revision": 0, "text": "Молоток"})
    error = assert_error(response, 503, "unavailable")
    assert error.retryable
