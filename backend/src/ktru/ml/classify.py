"""Optional local GLiNER family ranking. A prediction never establishes catalog coverage."""

from __future__ import annotations

import hashlib
import math
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path

from src.ktru import contracts as C
from src.ktru.catalog.reader import FamilyHit

SCHEMA = '{"family": candidate_names + ["UNKNOWN"]}; plain source text; include_confidence=True'


class FamilyClassifier:
    def __init__(self, call: Callable, manifest: C.ModelManifest, *, cache_size: int = 128):
        if cache_size < 1:
            raise ValueError("Classifier cache must be positive")
        self.call, self.manifest, self.cache_size = call, manifest, cache_size
        self._cache: OrderedDict[tuple, tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_local(cls, path: Path) -> FamilyClassifier:
        """Load existing weights only. Hash the files used; never fetch a remote model."""
        required = [path / name for name in ("model.safetensors", "config.json", "tokenizer.json",
                                              "tokenizer_config.json", "encoder_config/config.json")]
        files = [path / "model.safetensors", *sorted(path.rglob("*.json"))]
        if not all(file.is_file() for file in required):
            raise ValueError("Classifier needs complete local model files")
        digest = hashlib.sha256()
        for file in files:
            digest.update(file.relative_to(path).as_posix().encode())
            with file.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        import torch  # noqa: PLC0415 - optional local inference dependency
        from gliner2 import AutoExtractor  # noqa: PLC0415

        torch.set_num_threads(4)
        model = AutoExtractor.from_pretrained(str(path), local_files_only=True).to(device="cpu", dtype=torch.float32).eval()
        manifest = C.ModelManifest(model="local/GLiNER-family-classifier", revision=digest.hexdigest(),
            runtime=f"gliner2/{version('gliner2')}", prompt_sha256=hashlib.sha256(SCHEMA.encode()).hexdigest(),
            parameters={"device": "cpu", "dtype": "float32", "threads": 4, "proposal_threshold": .5,
                        "cache_size": 128}, identity_verified=True)

        def classify(text, labels):
            with torch.inference_mode():
                return model.classify_text(text, {"family": labels}, include_confidence=True)

        return cls(classify, manifest)

    def rank(self, text: str, families: list[FamilyHit]) -> list[FamilyHit]:
        eligible = [family for family in families if family.head_ok]
        labels = tuple(dict.fromkeys(family.name for family in eligible)) + ("UNKNOWN",)
        if len(labels) < 2:
            return list(families)
        key = (text, labels)
        with self._lock:  # serialize inference and bound cache memory
            if key in self._cache:
                label, confidence = self._cache[key]
                self._cache.move_to_end(key)
            else:
                raw = self.call(text, list(labels))
                choice = raw.get("family") if isinstance(raw, dict) else None
                if not isinstance(choice, dict) or choice.get("label") not in labels:
                    raise ValueError("Invalid family classifier choice")
                confidence = choice.get("confidence")
                if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    raise ValueError("Invalid family classifier score")
                label = choice["label"]
                self._cache[key] = label, confidence
                if len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
        winners = [family for family in eligible if family.name == label]
        # Threshold is a conservative proposal rule, not a calibrated probability of correctness.
        if label == "UNKNOWN" or confidence < .5 or len(winners) != 1:
            return list(families)
        winner = winners[0]
        return [winner, *[family for family in families if family is not winner]]


def configured_classifier() -> FamilyClassifier | None:
    path = os.environ.get("KTRU_CLASSIFIER_PATH")
    return FamilyClassifier.from_local(Path(path)) if path else None
