"""Small open multilingual embeddings: pinned INT8 ONNX, local CPU inference only."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from importlib.metadata import version
from pathlib import Path

from src.ktru import contracts as C

MODEL = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
MODEL_FILE = "model_qint8_avx512_vnni.onnx"
MODEL_SHA256 = "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88"
MODEL_FILES = ("config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", MODEL_FILE)
DIMENSIONS = 384
MAX_TOKENS = 128
PREPROCESSING = "e5-v1; query:/passage: prefixes; attention-mean pooling; L2; 128 tokens; family-name+okpd2+chars-v1"
PREPROCESSING_SHA256 = hashlib.sha256(PREPROCESSING.encode()).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_model(path: Path) -> dict:
    """Build-time download from a pinned public repository. Never called by the API."""
    path.mkdir(parents=True, exist_ok=True)
    for name in MODEL_FILES:
        target = path / name
        if not target.is_file():
            from huggingface_hub import hf_hub_download  # noqa: PLC0415 - build-time dependency

            downloaded = hf_hub_download(MODEL, f"onnx/{name}", revision=REVISION,
                                         cache_dir=path / ".download", token=False)
            shutil.copyfile(downloaded, target)
    hashes = {name: file_hash(path / name) for name in MODEL_FILES}
    if hashes[MODEL_FILE] != MODEL_SHA256:
        raise ValueError("Embedding weights differ from the pinned public model")
    metadata = {"model": MODEL, "revision": REVISION, "license": "MIT", "files_sha256": hashes}
    if (path / "model.json").is_file() and json.loads((path / "model.json").read_text()) != metadata:
        raise ValueError("Cached embedding model files have changed")
    (path / "model.json").write_text(json.dumps(metadata, indent=2))
    return metadata


class E5Encoder:
    def __init__(self, path: Path, *, threads: int = 4):
        if threads < 1:
            raise ValueError("Embedding threads must be positive")
        metadata = json.loads((path / "model.json").read_text())
        if metadata.get("model") != MODEL or metadata.get("revision") != REVISION:
            raise ValueError("Embedding model identity differs from the pinned model")
        hashes = {name: file_hash(path / name) for name in MODEL_FILES}
        if hashes != metadata.get("files_sha256") or hashes[MODEL_FILE] != MODEL_SHA256:
            raise ValueError("Embedding model files have changed")

        from fastembed import TextEmbedding  # noqa: PLC0415 - load the runtime only when an index exists
        from fastembed.common.model_description import ModelSource, PoolingType  # noqa: PLC0415

        local_name = "local/ktru-multilingual-e5-small-int8"
        if not any(m["model"] == local_name for m in TextEmbedding.list_supported_models()):
            TextEmbedding.add_custom_model(model=local_name, pooling=PoolingType.MEAN, normalization=True,
                sources=ModelSource(hf=MODEL), dim=DIMENSIONS, model_file=MODEL_FILE)
        try:
            self.model = TextEmbedding(model_name=local_name, specific_model_path=str(path.resolve()),
                local_files_only=True, threads=threads, providers=["CPUExecutionProvider"], cuda=False)
        except Exception as error:  # ONNX backend errors do not share one exception base beyond Exception.
            raise RuntimeError("Could not load the local embedding runtime") from error
        self.model.model.tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self._lock = threading.Lock()
        self.metadata = metadata
        self.manifest = C.ModelManifest(model=MODEL, revision=REVISION,
            runtime=f"fastembed/{version('fastembed')};onnxruntime/{version('onnxruntime')}",
            prompt_sha256=PREPROCESSING_SHA256, identity_verified=True,
            parameters={"device": "cpu", "dtype": "int8", "threads": threads, "dimensions": DIMENSIONS,
                        "max_tokens": MAX_TOKENS, "weights_sha256": hashes[MODEL_FILE]})

    def encode(self, texts: list[str], *, query: bool = False):
        import numpy as np  # noqa: PLC0415

        prefix = "query: " if query else "passage: "
        with self._lock:
            try:
                vectors = np.asarray(list(self.model.embed([prefix + text for text in texts], batch_size=32)),
                                     dtype=np.float32)
            except Exception as error:
                raise RuntimeError("Local embedding inference failed") from error
        if vectors.shape != (len(texts), DIMENSIONS) or not np.isfinite(vectors).all():
            raise ValueError("Invalid embedding output")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if (norms <= 0).any():
            raise ValueError("Empty embedding output")
        return vectors / norms
