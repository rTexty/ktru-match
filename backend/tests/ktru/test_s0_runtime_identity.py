import io
import json

import pytest

from src.ktru.ml import extract as ml


def test_ollama_manifest_records_observed_digest_and_runtime(monkeypatch):
    monkeypatch.setattr(ml, "config", lambda: ("http://127.0.0.1:11434", "gemma4:e4b"))
    digest = "a" * 64

    def request(url, timeout):
        if url.endswith("/api/tags"):
            data = {"models": [{"name": "gemma4:e4b", "digest": digest}]}
        else:
            data = {"version": "test-runtime"}
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(ml.urllib.request, "urlopen", request)
    result = ml.manifest()
    assert result.identity_verified and result.revision == digest
    assert result.runtime == "ollama/test-runtime"
    assert result.model == "gemma4:e4b" and "http" not in result.model_dump_json()


def test_tag_without_digest_is_not_verified(monkeypatch):
    monkeypatch.setattr(ml, "config", lambda: ("http://127.0.0.1:11434", "gemma4:e4b"))
    monkeypatch.setattr(ml.urllib.request, "urlopen",
                        lambda *args, **kwargs: io.BytesIO(b'{"models":[{"name":"gemma4:e4b"}]}'))
    result = ml.manifest()
    assert result.revision is None and not result.identity_verified


def test_generic_provider_manifest_retains_unverified_identity(monkeypatch):
    monkeypatch.setattr(ml, "config", lambda: ("http://model.invalid:8080", "open-model-tag"))
    monkeypatch.setattr(ml.urllib.request, "urlopen", lambda *args, **kwargs: None)
    result = ml.manifest()
    assert result.model == "open-model-tag" and not result.identity_verified and result.revision is None


@pytest.mark.parametrize("data", [{"models": ["bad"]}, ["bad"], {"models": None}])
def test_malformed_native_metadata_keeps_unverified_manifest(monkeypatch, data):
    monkeypatch.setattr(ml, "config", lambda: ("http://127.0.0.1:11434", "gemma4:e4b"))
    monkeypatch.setattr(ml.urllib.request, "urlopen",
                        lambda *args, **kwargs: io.BytesIO(json.dumps(data).encode()))
    result = ml.manifest()
    assert not result.identity_verified and result.revision is None


def test_endpoint_identity_excludes_credentials_and_changes_with_transport(monkeypatch):
    monkeypatch.setattr(ml, "config", lambda: ("HTTP://user:secret@MODEL.invalid:80/base/?token=secret", "open"))
    first = ml.endpoint_id()
    monkeypatch.setattr(ml, "config", lambda: ("http://model.invalid/base", "open"))
    assert first == ml.endpoint_id() and len(first) == 64
    assert "secret" not in first
    monkeypatch.setattr(ml, "config", lambda: ("http://another.invalid/base", "open"))
    assert first != ml.endpoint_id()
    monkeypatch.setattr(ml, "config", lambda: None)
    assert ml.endpoint_id() is None
