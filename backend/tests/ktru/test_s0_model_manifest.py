"""Generation settings are explicit and reproducible, with no inferred immutable identity."""

import io
import json

import pytest

from src.ktru.ml import extract as ml
from src.ktru.service.stages import OpenModel


def test_default_request_retains_provider_reasoning_default(monkeypatch):
    monkeypatch.delenv("KTRU_LLM_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("KTRU_LLM_THINKING", raising=False)
    assert ml.request_parameters() == {"temperature": 0, "max_tokens": 4096,
                                      "response_format": {"type": "json_object"}}


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high"])
def test_explicit_reasoning_setting_reaches_transport_and_manifest(monkeypatch, effort):
    monkeypatch.setenv("KTRU_LLM_REASONING_EFFORT", effort)
    monkeypatch.setattr(ml, "config", lambda: ("http://model.invalid", "open-test-model"))
    requests = []

    def transport(request, timeout):
        requests.append(json.loads(request.data))
        content = json.dumps({"facts": [{"property": "Покрытие", "value": "водоотталкивающее",
                                        "quote": "покрытие водоотталкивающее"}]})
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    monkeypatch.setattr(ml.urllib.request, "urlopen", transport)
    result = OpenModel().extract("Ксилофон, покрытие водоотталкивающее", 1)
    assert len(requests) == 1 and requests[0]["reasoning_effort"] == effort
    assert requests[0]["max_tokens"] == 4096 and result.assertions
    manifest = result.model_manifest
    assert manifest.parameters["reasoning_effort"] == effort
    assert manifest.prompt_sha256 == ml.prompt_digest()
    assert manifest.revision is None and not manifest.identity_verified


def test_invalid_reasoning_setting_fails_before_transport(monkeypatch):
    monkeypatch.setenv("KTRU_LLM_REASONING_EFFORT", "invented")
    with pytest.raises(ValueError, match="KTRU_LLM_REASONING_EFFORT"):
        ml.request_parameters()


def test_api_key_reaches_chat_and_status_but_not_manifest(monkeypatch):
    monkeypatch.setenv("KTRU_LLM_URL", "http://gpu.invalid:8000")
    monkeypatch.setenv("KTRU_LLM_MODEL", "open-test-model")
    monkeypatch.setenv("KTRU_LLM_API_KEY", "test-key")
    seen = []

    def transport(request, timeout):
        seen.append((request.full_url, request.get_header("Authorization")))
        if request.full_url.endswith("/v1/models"):
            response = io.BytesIO(b"{}")
            response.status = 200
            return response
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"facts": []}'}}]}).encode())

    monkeypatch.setattr(ml.urllib.request, "urlopen", transport)
    monkeypatch.setitem(ml._status, "at", 0.0)
    assert ml.call_model("Стол") == []
    assert ml.status() == "available"
    assert seen == [("http://gpu.invalid:8000/v1/chat/completions", "Bearer test-key"),
                    ("http://gpu.invalid:8000/v1/models", "Bearer test-key")]
    assert "test-key" not in ml.manifest().model_dump_json() and "test-key" not in ml.endpoint_id()


def test_without_api_key_no_authorization_header(monkeypatch):
    monkeypatch.delenv("KTRU_LLM_API_KEY", raising=False)
    assert ml._headers() == {"Content-Type": "application/json"}


@pytest.mark.parametrize(("raw", "expected"), [(None, 4), ("16", 16)])
def test_concurrency_setting(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("KTRU_LLM_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("KTRU_LLM_CONCURRENCY", raw)
    assert ml.concurrency() == expected


@pytest.mark.parametrize("raw", ["0", "1000", "many"])
def test_invalid_concurrency_fails(monkeypatch, raw):
    monkeypatch.setenv("KTRU_LLM_CONCURRENCY", raw)
    with pytest.raises(ValueError):
        ml.concurrency()


@pytest.mark.parametrize(("raw", "enabled"), [("off", False), ("ON", True)])
def test_thinking_switch_reaches_the_chat_template(monkeypatch, raw, enabled):
    monkeypatch.setenv("KTRU_LLM_THINKING", raw)
    assert ml.request_parameters()["chat_template_kwargs"] == {"enable_thinking": enabled}


def test_invalid_thinking_switch_fails(monkeypatch):
    monkeypatch.setenv("KTRU_LLM_THINKING", "maybe")
    with pytest.raises(ValueError):
        ml.request_parameters()


def test_thinking_switch_is_recorded_flat_in_the_manifest(monkeypatch):
    monkeypatch.setenv("KTRU_LLM_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("KTRU_LLM_MODEL", "local-model")
    monkeypatch.setenv("KTRU_LLM_THINKING", "off")
    assert ml.manifest().parameters["enable_thinking"] is False
