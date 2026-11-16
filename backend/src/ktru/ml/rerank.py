"""Optional cross-encoder reranking of candidate families (open model, e.g. Qwen3-Reranker-0.6B under vLLM).

Only families whose name holds the product noun of the text and that belong to a served group compete; their
places in the list are reordered by the reranker score, every other family keeps its place, and nothing is
removed. A reranker order is a proposal: family confirmation and catalog coverage are decided as before.
Measured on 1247 real Portal listings (OKPD2 class labels): +38 right families over lexical order.

Config: KTRU_RERANK_URL (server base URL with /v1/rerank), KTRU_RERANK_MODEL; KTRU_RERANK_API_KEY or, if unset,
KTRU_LLM_API_KEY. Unset URL → disabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from urllib.parse import urlparse

from src.ktru import contracts as C
from src.ktru.catalog.reader import FamilyHit
from src.ktru.ml.extract import PROPRIETARY_HOSTS

PREFIX = ("<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct "
          "provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n")
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
INSTRUCT = ("Given a product description from a Russian public procurement listing, judge whether the KTRU catalog "
            "class describes this product")
QUERY_CHARS = 400  # the served reranker reads 1024 tokens: query and one family must fit together


def _document(family: FamilyHit) -> str:
    member = family.template or (family.members[0] if family.members else None)
    okpd2 = member.okpd2_name if member and member.okpd2_name else ""
    return f"{family.name}. Класс ОКПД2: {okpd2}"


class FamilyReranker:
    def __init__(self, call: Callable[[str, list[str]], list[float]], manifest: C.ModelManifest, *,
                 cache_size: int = 256):
        self.call, self.manifest, self.cache_size = call, manifest, cache_size
        self._cache: OrderedDict[tuple, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def rank(self, text: str, families: list[FamilyHit]) -> list[FamilyHit]:
        slots = [i for i, family in enumerate(families) if family.head_ok and family.members
                 and family.members[0].domain != "unknown"]
        if len(slots) < 2:
            return list(families)
        query = text[:QUERY_CHARS]
        documents = [_document(families[i]) for i in slots]
        key = (query, tuple(documents))
        with self._lock:
            scores = self._cache.get(key)
            if scores is not None:
                self._cache.move_to_end(key)
        if scores is None:
            scores = self.call(query, documents)
            if len(scores) != len(documents) or any(not isinstance(s, (int, float)) for s in scores):
                raise ValueError("Invalid reranker scores")
            with self._lock:
                self._cache[key] = scores
                if len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
        ordered = [families[i] for _, i in sorted(zip(scores, slots, strict=True), key=lambda pair: -pair[0])]
        out = list(families)
        for slot, family in zip(slots, ordered, strict=True):
            out[slot] = family
        return out


def configured_reranker() -> FamilyReranker | None:
    url = os.environ.get("KTRU_RERANK_URL", "").rstrip("/")
    if not url:
        return None
    model = os.environ.get("KTRU_RERANK_MODEL", "")
    if not model:
        raise ValueError("KTRU_RERANK_MODEL is required with KTRU_RERANK_URL")
    host = urlparse(url).hostname or ""
    if any(h in host for h in PROPRIETARY_HOSTS):
        raise ValueError(f"proprietary model host is not allowed in the product: {host}")
    key = os.environ.get("KTRU_RERANK_API_KEY") or os.environ.get("KTRU_LLM_API_KEY", "")
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key.strip()}"} if key else {})}
    timeout = float(os.environ.get("KTRU_RERANK_TIMEOUT", "5"))

    def call(query: str, documents: list[str]) -> list[float]:
        body = {"model": model, "query": f"{PREFIX}<Instruct>: {INSTRUCT}\n<Query>: {query}\n",
                "documents": [f"<Document>: {d}{SUFFIX}" for d in documents]}
        request = urllib.request.Request(url + "/v1/rerank", json.dumps(body).encode(), headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            results = json.loads(response.read(1024 * 1024))["results"]
        return [float(r["relevance_score"]) for r in sorted(results, key=lambda r: r["index"])]

    template = PREFIX + INSTRUCT + SUFFIX
    manifest = C.ModelManifest(model=model, runtime="openai-compatible-rerank",
                               prompt_sha256=hashlib.sha256(template.encode()).hexdigest(),
                               parameters={"query_chars": QUERY_CHARS, "scope": "head-matching group families"})
    return FamilyReranker(call, manifest)
