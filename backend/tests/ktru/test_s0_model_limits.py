"""The model transport bounds untrusted output before grounding or session publication."""

import io
import json

import pytest

from src.ktru.ml import extract as ml

RESPONSE_LIMIT = 1024 * 1024
FACT_LIMIT = 128


class FakeResponse(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


@pytest.fixture
def model_transport(monkeypatch):
    monkeypatch.setattr(ml, "config", lambda: ("http://model.invalid", "open-test-model"))

    def install(data, *, size=None):
        body = json.dumps({"choices": [{"message": {"content": json.dumps(data)}}]}).encode()
        if size is not None:
            assert size >= len(body)
            body += b" " * (size - len(body))
        response = FakeResponse(body)
        monkeypatch.setattr(ml.urllib.request, "urlopen", lambda request, timeout: response)
        return response

    return install


def test_reads_one_bounded_response(model_transport):
    fact = {"property": "Масса", "value": "500", "unit": "г", "quote": "масса 500 г"}
    response = model_transport({"facts": [fact]})
    assert ml.call_model("Молоток, масса 500 г") == [fact]
    assert response.read_sizes == [RESPONSE_LIMIT + 1]


def test_exact_response_limit_remains_accepted(model_transport):
    response = model_transport({"facts": []}, size=RESPONSE_LIMIT)
    assert ml.call_model("Молоток") == []
    assert response.read_sizes == [RESPONSE_LIMIT + 1]


def test_oversized_response_is_rejected_before_json_parse(model_transport):
    response = model_transport({"facts": []}, size=RESPONSE_LIMIT + 1)
    with pytest.raises(ValueError):
        ml.call_model("Молоток")
    assert response.read_sizes == [RESPONSE_LIMIT + 1]


def test_exact_fact_limit_remains_accepted(model_transport):
    facts = [{"property": "Цвет", "value": "красный", "quote": "цвет красный"}] * FACT_LIMIT
    model_transport({"facts": facts})
    assert ml.call_model("Молоток, цвет красный") == facts


def test_excess_facts_are_rejected_without_truncating(model_transport):
    facts = [{"property": "Цвет", "value": "красный", "quote": "цвет красный"}] * (FACT_LIMIT + 1)
    model_transport({"facts": facts})
    with pytest.raises(ValueError):
        ml.call_model("Молоток, цвет красный")


@pytest.mark.parametrize("data", [
    [], None, {}, {"facts": None}, {"facts": "[]"}, {"facts": {"property": "Цвет"}},
    {"facts": [{"property": "Цвет", "value": "красный", "quote": "цвет красный"}, None]},
])
def test_malformed_fact_shape_is_rejected_wholesale(model_transport, data):
    model_transport(data)
    with pytest.raises(ValueError):
        ml.call_model("Молоток, цвет красный")
