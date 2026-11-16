"""Default extraction/search adapters. Every transition consumes typed stage results."""

import logging
import os
import threading
import time
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from src.ktru import contracts as C
from src.ktru.catalog.reader import Catalog, SearchResult
from src.ktru.catalog.vector import VectorIndex, default_index_dir
from src.ktru.engine import assertions as A
from src.ktru.engine.source_map import MAX_TYPO_DIAGNOSTICS as SOURCE_MAP_TYPO_LIMIT
from src.ktru.engine.source_map import search_mapping
from src.ktru.ml import extract as ml
from src.ktru.ml.classify import FamilyClassifier, configured_classifier
from src.ktru.ml.rerank import FamilyReranker, configured_reranker
from src.ktru.ml.semantic import SemanticFamilyIndex, configured_semantic

MAX_TYPO_DIAGNOSTICS = SOURCE_MAP_TYPO_LIMIT



class Rules:
    def extract(self, text: str, text_revision: int) -> C.ExtractionResult:
        started = time.perf_counter()
        return C.ExtractionResult(assertions=A.rule_assertions(text, text_revision),
                                  timings=C.Timings(parse=(time.perf_counter() - started) * 1000))


class OpenModel:
    def __init__(self, call: Callable[[str], list[dict]] | None = None):
        self.call = call

    def extract(self, text: str, text_revision: int) -> C.ExtractionResult:
        started = time.perf_counter()
        facts = (self.call or ml.call_model)(text)
        kept, rejected = ml.ground(facts, text)
        manifest = ml.manifest() if self.call is None else None
        return C.ExtractionResult(assertions=A.model_assertions(kept, text, text_revision),
            rejected_outputs=[C.RejectedOutput(**item) for item in rejected], model_manifest=manifest,
            status="analyzed", timings=C.Timings(extract=(time.perf_counter() - started) * 1000))


# Reciprocal-rank fusion constant, and when the semantic first choice alone may be proposed: similarity and its lead
# over the second family. Chosen on dev data (paraphrase dev, real listings): 12 of 15 such proposals right, against
# 54 of 141 at similarity 0.5 alone. Below that the nearest families only order the question's options.
FUSION_K = 5
SEMANTIC_PROPOSAL = 0.6
SEMANTIC_MARGIN = 0.04
# A description nearer by this much to a family outside the served groups is not one of ours: on dev, 31 of 33
# out-of-group products caught, 1 of 132 served products wrongly flagged.
OUTSIDE_MARGIN = 0.05


def _served(family) -> bool:
    return bool(family.members) and family.members[0].domain != "unknown"


def fuse_semantic(catalog, text: str, families: list, nearest: list[tuple[str, float]],
                  outside: tuple[str, float] | None = None) -> list:
    """Lexical order and the semantic nearest families fused by reciprocal rank. A family the text names by its
    product noun keeps precedence over one that only looks similar, unless that one is the confident first choice."""
    scores: dict[str, float] = defaultdict(float)
    for i, family in enumerate(families):
        scores[family.family_id] += 1 / (FUSION_K + i)
    for j, (family_id, _) in enumerate(nearest):
        scores[family_id] += 1 / (FUSION_K + j)
    known = {family.family_id: family for family in families}
    missing = [family_id for family_id, _ in nearest if family_id not in known]
    if missing:
        for hit in catalog.expand_matches(catalog.rank_matches(text, {family_id: 0.0 for family_id in missing})):
            known.setdefault(hit.family_id, hit)
    similarity = dict(nearest)
    first = nearest[0] if nearest else None
    confident = bool(first) and first[1] >= SEMANTIC_PROPOSAL and (
        len(nearest) < 2 or first[1] - nearest[1][1] >= SEMANTIC_MARGIN)
    beyond = outside[0] if outside and first and outside[1] - first[1] >= OUTSIDE_MARGIN else None
    fused = [replace(family, semantic_score=similarity.get(family.family_id),
                     semantic_pick=confident and not beyond and family.family_id == first[0], outside_group=beyond)
             for family in known.values()]
    fused.sort(key=lambda f: (_served(f), f.head_ok or f.semantic_pick, scores[f.family_id]), reverse=True)
    return fused[:10]


class LexicalRetriever:
    def __init__(self, catalog: C.CatalogReader, classifier: FamilyClassifier | None = None, *,
                 vector_unavailable: bool = False, reranker: FamilyReranker | None = None,
                 semantic: SemanticFamilyIndex | None = None):
        self.catalog = catalog
        self.classifier = classifier or configured_classifier()
        self.reranker = reranker or configured_reranker()
        self.semantic = semantic or configured_semantic()
        if self.semantic and hasattr(catalog, "family_documents"):
            self.semantic.warm(catalog.family_documents, catalog._family_in_scope)
        self._cache: OrderedDict[str, C.RetrievalResult] = OrderedDict()
        self._cache_lock = threading.RLock()
        self._vector_unavailable = vector_unavailable

    def _search(self, text: str):
        return self.catalog.search(text), [], [vector_diagnostic(text)] if self._vector_unavailable else []

    def retrieve(self, text: str, snapshot_id: str) -> C.RetrievalResult:
        if snapshot_id != self.catalog.snapshot_id:
            raise ValueError("Retrieval snapshot does not match session snapshot")
        started = time.perf_counter()
        with self._cache_lock:
            if text in self._cache:
                self._cache.move_to_end(text)
                return replace(self._cache[text], timings=C.Timings(retrieve=(time.perf_counter() - started) * 1000))
            result = self._retrieve(text, snapshot_id, started)
            if not any(d.kind == "model_unavailable" for d in result.diagnostics):
                self._cache[text] = result
                if len(self._cache) > 128:
                    self._cache.popitem(last=False)
            return result

    def _retrieve(self, text: str, snapshot_id: str, started: float) -> C.RetrievalResult:
        mapped = search_mapping(text, self.catalog.correct_word, snapshot_id=snapshot_id)
        result, manifests, search_diagnostics = self._search(mapped.search_text)
        families, examined = result
        diagnostics = [*mapped.diagnostics, *search_diagnostics]
        classifier_failed = bool(search_diagnostics)
        if self.classifier:
            manifests = [*manifests, self.classifier.manifest]
            try:
                families = self.classifier.rank(text, families)
            except (ValueError, RuntimeError, OSError):
                classifier_failed = True
                diagnostics.append(C.Diagnostic(id=A.identity("classifier", text), kind="model_unavailable",
                    severity="warning", message_ru="Классификатор семейств недоступен: показан лексический поиск",
                    reason_ru="Не удалось проверить выбор локального классификатора. Порядок семейств сохранён."))
        if self.semantic and hasattr(self.catalog, "family_documents"):
            manifests = [*manifests, self.semantic.manifest]
            try:
                nearest, outside = self.semantic.rank(mapped.search_text, self.catalog.family_documents,
                                                      self.catalog._family_in_scope)
                families = fuse_semantic(self.catalog, mapped.search_text, families, nearest, outside)
            except (ValueError, RuntimeError, OSError, KeyError):
                diagnostics.append(C.Diagnostic(id=A.identity("semantic", text), kind="model_unavailable",
                    severity="warning", message_ru="Смысловой поиск семейств недоступен: показан лексический поиск",
                    reason_ru="Модель эмбеддингов не ответила или индекс семейств ещё строится. Порядок семейств поиска сохранён."))
        if self.reranker:
            manifests = [*manifests, self.reranker.manifest]
            try:
                families = self.reranker.rank(text, families)
            except (ValueError, RuntimeError, OSError, KeyError):
                diagnostics.append(C.Diagnostic(id=A.identity("reranker", text), kind="model_unavailable",
                    severity="warning", message_ru="Переранжирование семейств недоступно: показан лексический порядок",
                    reason_ru="Модель переранжирования не ответила. Порядок семейств поиска сохранён."))
        row_truncated = getattr(result, "row_truncated", examined >= 400)
        family_truncated = getattr(result, "family_truncated", False)
        reasons = (["position_limit"] if row_truncated else []) + (["family_limit"] if family_truncated else [])
        scope = C.SearchScope(snapshot_id=snapshot_id, eligible_count=self.catalog.manifest["counts"]["final"],
            examined_count=examined, retained_count=sum(len(family.members) for family in families),
            family_ids=[family.family_id for family in families],
            family_membership_complete=all(family.membership_complete for family in families),
            retrieval_truncated=bool(reasons), coverage_status="unconfirmed", truncation_reasons=reasons,
            reason_ru="Поиск по каталогу предлагает семейства; требуется подтверждение пользователя")
        return C.RetrievalResult(families=families, scope=scope,
            timings=C.Timings(retrieve=(time.perf_counter() - started) * 1000),
            status="partial" if reasons or classifier_failed else "complete", normalized_text=mapped.normalized_text if mapped.normalized_text != text else None,
            search_text=mapped.search_text if mapped.search_text != text else None,
            correction_map=mapped.correction_map, corrections=mapped.corrections, diagnostics=diagnostics,
            model_manifests=manifests)


class HybridRetriever(LexicalRetriever):
    def __init__(self, catalog: Catalog, index: VectorIndex, classifier: FamilyClassifier | None = None,
                 reranker: FamilyReranker | None = None, semantic: SemanticFamilyIndex | None = None):
        super().__init__(catalog, classifier, reranker=reranker, semantic=semantic)
        self.index = index

    def _search(self, text: str):
        lexical, examined, truncated = self.catalog.search_matches(text)
        # A named product head already supplies strong lexical candidates. Avoid inference on this hot path.
        if any(hit.head_exact for hit in lexical):
            return SearchResult(self.catalog.expand_matches(lexical[:8]), examined, False,
                                truncated or len(lexical) > 8), [self.index.manifest], []
        try:
            dense = self.index.search(text)
        except (ValueError, RuntimeError, OSError):
            result = SearchResult(self.catalog.expand_matches(lexical[:8]), examined, False,
                                  truncated or len(lexical) > 8)
            return result, [self.index.manifest], [vector_diagnostic(text)]
        # Reciprocal-rank fusion; a weak word overlap must not swamp a semantic match.
        scores = {fid: 1 / (60 + rank) for rank, (fid, _score) in enumerate(dense, 1)}
        for rank, hit in enumerate(lexical, 1):
            scores[hit.family_id] = scores.get(hit.family_id, 0) + (1 if hit.head_ok else .15) / (60 + rank)
        matches = self.catalog.rank_matches(text, scores)
        # Preserve explicit full family names. Semantic similarity never changes lexical evidence.
        matches.sort(key=lambda hit: (hit.in_scope, hit.head_ok and hit.name_coverage >= 1,
                                     hit.query_coverage if hit.head_ok and hit.name_coverage >= 1 else 0,
                                     scores[hit.family_id]), reverse=True)
        dense_scores = dict(dense)
        families = [replace(hit, semantic_score=dense_scores.get(hit.family_id))
                    for hit in self.catalog.expand_matches(matches[:8])]
        examined = max(examined, self.index.metadata["final_positions"])
        result = SearchResult(families, examined, False, truncated or len(matches) > 8)
        return result, [self.index.manifest], []


def vector_diagnostic(text: str) -> C.Diagnostic:
    return C.Diagnostic(id=A.identity("vector", text), kind="model_unavailable", severity="warning",
        message_ru="Векторный поиск недоступен: показан лексический поиск",
        reason_ru="Не удалось загрузить или проверить локальный индекс КТРУ и embedding-модель.")


def configured_retriever(catalog: Catalog) -> LexicalRetriever:
    mode = os.environ.get("KTRU_SEARCH_MODE", "hybrid")
    if mode not in ("lexical", "hybrid"):
        raise ValueError("KTRU_SEARCH_MODE must be lexical or hybrid")
    if mode == "lexical":
        return LexicalRetriever(catalog)
    explicit = os.environ.get("KTRU_VECTOR_INDEX_DIR")
    if not explicit and not hasattr(catalog, "dir"):
        return LexicalRetriever(catalog)  # in-memory catalogs have no snapshot, so no index to verify against
    path = Path(explicit) if explicit else default_index_dir(catalog)
    if not (path / "manifest.json").is_file():
        return LexicalRetriever(catalog, vector_unavailable="KTRU_VECTOR_INDEX_DIR" in os.environ)
    try:
        index = VectorIndex(catalog, path, threads=int(os.environ.get("KTRU_EMBED_THREADS", "4")))
        return HybridRetriever(catalog, index)
    except (ImportError, ValueError, RuntimeError, OSError) as error:
        logging.getLogger(__name__).warning("Vector index unavailable (%s); using lexical search", type(error).__name__)
        return LexicalRetriever(catalog, vector_unavailable=True)


def combine(rules: C.ExtractionResult, model: C.ExtractionResult | None) -> C.ExtractionResult:
    if model is None:
        return rules.model_copy(update={"assertions": A.merge(rules.assertions)})
    return C.ExtractionResult(assertions=A.merge([*rules.assertions, *model.assertions]),
        rejected_outputs=model.rejected_outputs, model_manifest=model.model_manifest, status=model.status,
        timings=C.Timings(parse=rules.timings.parse, extract=model.timings.extract))
