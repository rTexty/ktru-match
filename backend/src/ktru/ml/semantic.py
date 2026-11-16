"""Optional semantic family retrieval with an open embedding model (e.g. Qwen3-Embedding-0.6B under vLLM).

Lexical search misses synonyms and paraphrases («шкаф для химикатов» → «Шкаф для хранения химических реактивов»,
«электростеплер» → «Степлер строительный электрический»). Each family served by the product is embedded once as
«name. OKPD2 class. Характеристики: …»; a description is embedded with a retrieval instruction, and the nearest
families are fused with the lexical order by reciprocal rank (see `service.stages`). A semantic pick is a proposal:
it never confirms a family and never establishes catalog coverage.

Measured offline (11.10.2026): real Portal listings 493 → 654 right families of 1247; held-out paraphrase test
55 → 60 of 68; KTRU-rendered descriptions unchanged.

Config: KTRU_EMBED_URL (server base URL with /v1/embeddings), KTRU_EMBED_MODEL; KTRU_EMBED_API_KEY or, if unset,
KTRU_LLM_API_KEY; KTRU_EMBED_TIMEOUT (seconds, default 5). Unset URL → disabled.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from src.ktru import contracts as C
from src.ktru.ml.extract import PROPRIETARY_HOSTS

INSTRUCT = ("Instruct: Given a product description from a Russian public procurement request, retrieve the catalog "
            "class of the product\nQuery: ")
QUERY_CHARS = 500
DOCUMENT_FORMAT = "name. okpd2_name. Характеристики: up to 12 characteristic names; 600 chars"
BATCH = 32
ATTEMPTS = 3


class SemanticFamilyIndex:
    """Every catalog family is embedded, not only the served ones: a description nearer to a family outside the
    served groups («Дрель хирургическая») is recognised as such instead of being mapped onto the nearest served one."""

    def __init__(self, call: Callable[[list[str]], list[list[float]]], manifest: C.ModelManifest, *,
                 cache_size: int = 256, cache_dir: Path | None = None):
        self.call, self.manifest, self.cache_size = call, manifest, cache_size
        self.cache_dir = cache_dir
        self._families: list[str] | None = None
        self._matrix: np.ndarray | None = None
        self._served: np.ndarray | None = None
        self._lock = threading.Lock()  # guards the one-time index build
        self._cache_lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[list[tuple[str, float]], tuple[str, float] | None]] = OrderedDict()

    def _vectors(self, texts: list[str]) -> np.ndarray:
        chunks = [texts[i:i + BATCH] for i in range(0, len(texts), BATCH)]

        def call(chunk: list[str]) -> list[list[float]]:
            for attempt in range(ATTEMPTS):  # one slow batch of 449 must not cost the whole index
                try:
                    return self.call(chunk)
                except OSError:
                    if attempt == ATTEMPTS - 1 or len(chunk) == 1:
                        raise
            return []

        with ThreadPoolExecutor(4) as pool:
            parts = list(pool.map(call, chunks))
        if any(len(part) != len(chunk) for part, chunk in zip(parts, chunks, strict=True)):
            raise ValueError("Invalid embedding response")
        matrix = np.asarray([row for part in parts for row in part], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.where(norms == 0, 1, norms)

    def _index(self, documents: dict[str, str], served: set[str], *,
               wait: bool = True) -> tuple[list[str], np.ndarray, np.ndarray]:
        if self._matrix is not None:
            return self._families, self._matrix, self._served
        if not wait:
            # A request never builds or waits for the index (minutes on a cold start): search stays lexical while
            # a background build runs, and a failed build is retried in the background by the next request.
            if not self._lock.locked():
                self.warm(documents, served)
            raise RuntimeError("Semantic family index is still being built")
        self._lock.acquire()
        try:
            if self._matrix is None:
                families = sorted(documents)
                digest = hashlib.sha256(json.dumps([self.manifest.model, self.manifest.prompt_sha256,
                                                    [documents[f] for f in families]]).encode()).hexdigest()[:24]
                path = self.cache_dir / f"families-{digest}.npy" if self.cache_dir else None
                if path is not None and path.is_file():
                    matrix = np.load(path)
                else:
                    matrix = self._vectors([documents[f] for f in families])
                    if path is not None:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        np.save(path, matrix)
                self._families, self._served = families, np.array([f in served for f in families])
                self._matrix = matrix  # set last: requests read it without the lock
            return self._families, self._matrix, self._served
        finally:
            self._lock.release()

    def warm(self, documents: dict[str, str], served: set[str]) -> None:
        """Build (or load) the family vectors off the request path; a failure is retried by the next request."""
        def build():
            try:
                self._index(documents, served)
            except (ValueError, RuntimeError, OSError):
                logging.getLogger(__name__).warning("Semantic family index not ready; the next request retries it")
        threading.Thread(target=build, name="semantic-index", daemon=True).start()

    def rank(self, text: str, documents: dict[str, str], served: set[str],
             k: int = 10) -> tuple[list[tuple[str, float]], tuple[str, float] | None]:
        """The k served families nearest to the description, and the nearest family outside the served groups."""
        query = INSTRUCT + text[:QUERY_CHARS]
        with self._cache_lock:
            cached = self._cache.get(query)
            if cached is not None:
                self._cache.move_to_end(query)
                return cached
        families, matrix, mask = self._index(documents, served, wait=False)
        scores = matrix @ self._vectors([query])[0]
        inside = np.where(mask, scores, -np.inf)
        order = np.argsort(-inside)[:k]
        nearest = [(families[i], float(scores[i])) for i in order if mask[i]]
        outside_scores = np.where(mask, -np.inf, scores)
        j = int(np.argmax(outside_scores))
        outside = (families[j], float(scores[j])) if not mask[j] else None
        result = (nearest, outside)
        with self._cache_lock:
            self._cache[query] = result
            if len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return result


def configured_semantic() -> SemanticFamilyIndex | None:
    url = os.environ.get("KTRU_EMBED_URL", "").rstrip("/")
    if not url:
        return None
    model = os.environ.get("KTRU_EMBED_MODEL", "")
    if not model:
        raise ValueError("KTRU_EMBED_MODEL is required with KTRU_EMBED_URL")
    host = urlparse(url).hostname or ""
    if any(h in host for h in PROPRIETARY_HOSTS):
        raise ValueError(f"proprietary model host is not allowed in the product: {host}")
    key = os.environ.get("KTRU_EMBED_API_KEY") or os.environ.get("KTRU_LLM_API_KEY", "")
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key.strip()}"} if key else {})}
    timeout = float(os.environ.get("KTRU_EMBED_TIMEOUT", "5"))

    def call(texts: list[str]) -> list[list[float]]:
        request = urllib.request.Request(url + "/v1/embeddings", json.dumps({"model": model, "input": texts}).encode(),
                                         headers)
        # A request embeds one query and keeps the short timeout; index batches run in the background.
        with urllib.request.urlopen(request, timeout=timeout if len(texts) == 1 else max(timeout, 120)) as response:
            data = json.loads(response.read(64 * 1024 * 1024))["data"]
        return [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]

    manifest = C.ModelManifest(model=model, runtime="openai-compatible-embeddings",
                               prompt_sha256=hashlib.sha256((INSTRUCT + DOCUMENT_FORMAT).encode()).hexdigest(),
                               parameters={"query_chars": QUERY_CHARS, "scope": "families of the served groups"})
    cache_dir = Path(os.environ.get("KTRU_SEMANTIC_CACHE") or Path(tempfile.gettempdir()) / "ktru-semantic")
    return SemanticFamilyIndex(call, manifest, cache_dir=cache_dir)
