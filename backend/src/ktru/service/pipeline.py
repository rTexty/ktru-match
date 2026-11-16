"""Session pipeline: parse/answer/edit/undo with atomic revision CAS and request-id idempotency.

Sessions live in memory in ONE API process (demo scope). Restart or eviction returns session_expired.
"""

import dataclasses
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable

from src.ktru import contracts as C
from src.ktru.catalog.reader import Catalog
from src.ktru.catalog.validation import catalog_values
from src.ktru.contract_validation import validate_extraction
from src.ktru.engine import overrides
from src.ktru.engine.adapters import DeterministicCardAssembler
from src.ktru.engine.resolve import Inputs, resolve
from src.ktru.engine.source_map import autocorrect, replace_span
from src.ktru.engine.text import OKEI, UNITS, norm
from src.ktru.engine.values import value_display
from src.ktru.ml import choice
from src.ktru.ml import extract as ml
from src.ktru.service.errors import ServiceError, _err
from src.ktru.service.session_store import InMemorySessionStore, Session, Snapshot
from src.ktru.service.stages import OpenModel, Rules, combine, configured_retriever

MAX_SESSIONS = 500
MAX_HISTORY = 50
MAX_CACHED_REQUESTS = 20
MAX_CACHED_STATE_BYTES = 128 * 1024  # Per session; also bounded by MAX_SESSIONS.
MAX_INPUTS = 100  # answers + edits per description
INFERRED_CALLS = 3  # short model calls that may answer questions from the description's own words
MAX_INFERRED_VALUES = 40  # longer catalog value lists are not offered to the model
MAX_ANALYSES = ml.concurrency()  # Bound model resource use independently of session CAS (KTRU_LLM_CONCURRENCY).

__all__ = ["Pipeline", "ServiceError", "Session"]


def _resolve_current(*args, **kwargs):
    """Keep the legacy module-level resolver hook for existing in-process integrations."""
    return resolve(*args, **kwargs)


def _rebase(result: C.ExtractionResult, revision: int) -> C.ExtractionResult:
    """Re-scope cached facts to the recomputed card without changing their original text."""
    def evidence(items):
        return [item.model_copy(update={"span": item.span.model_copy(update={"text_revision": revision})})
                if isinstance(item, C.TextEvidence) else item for item in items]

    assertions = [assertion.model_copy(update={
        "span": assertion.span.model_copy(update={"text_revision": revision}) if assertion.span else None,
        "evidence": evidence(assertion.evidence),
        "subject": assertion.subject.model_copy(update={"evidence": evidence(assertion.subject.evidence)})
            if assertion.subject else None,
    }) for assertion in result.assertions]
    return result.model_copy(update={"assertions": assertions})


def _override(x: Session, p) -> dict:
    """Record a correction of a reading the user saw on the current card; a missing target is a stale card."""
    state = x.state

    def stale(what: str):
        return _err(409, "stale_revision", f"{what} устарело. Обновите карточку.", revision=x.revision)

    if isinstance(p, C.RemoveFieldPatch):
        target = next((f for f in (state.fields if state else []) if f.id == p.field_id), None)
        if target is None:
            raise stale("Поле")
        if not target.assertion_ids:
            raise _err(422, "invalid_input", "У поля нет значения из описания или ответа.")
        return {"kind": "remove", "field_id": p.field_id, "label_ru": target.label_ru,
                "assertion_ids": list(target.assertion_ids)}
    if isinstance(p, C.RenameSubjectPatch):
        if not any(subject.id == p.subject_id for subject in (state.subjects if state else [])):
            raise stale("Объект")
        return {"kind": "rename", "subject_id": p.subject_id, "label": p.label.strip()}
    target = next((a for a in (state.assertions if state else []) if a.id == p.assertion_id), None)
    if target is None or target.status == "rejected":
        raise stale("Утверждение")
    if isinstance(p, C.MoveAssertionPatch):
        return {"kind": "move", "assertion_id": p.assertion_id, "subject_kind": p.subject_kind,
                "subject_label": p.subject_label.strip()}
    return {"kind": "modality", "assertion_id": p.assertion_id, "status": p.status, "polarity": p.polarity,
            "operator": p.operator, "value": p.value.model_dump() if p.value else None}


def _remember_corrections(x: Session, text: str, fixes: list[C.Correction]) -> None:
    """Keep automatic fixes per accepted text, so undo and reverting one word keep the others visible."""
    x.corrections.pop(text, None)
    if fixes:
        x.corrections[text] = fixes
    while len(x.corrections) > MAX_HISTORY + 1:
        x.corrections.pop(next(iter(x.corrections)))


def _after_span_edit(fixes: list[C.Correction], patch: C.ReplaceSpanPatch) -> list[C.Correction]:
    """Drop fixes the edit touched (e.g. the user reverted one) and shift the ones after it."""
    start, end = patch.span.start, patch.span.end
    delta = len(patch.replacement) - (end - start)
    kept = []
    for fix in fixes:
        span = fix.transformed_span
        if span is None or span.end <= start:
            kept.append(fix)
        elif span.start >= end:
            kept.append(fix.model_copy(update={"transformed_span": span.model_copy(
                update={"start": span.start + delta, "end": span.end + delta})}))
    return kept


class Pipeline:
    def __init__(self, catalog: Catalog, *, extractor: C.FactExtractor | None = None,
                 retriever: C.Retriever | None = None, rule_extractor: C.RuleExtractor | None = None,
                 resolver: C.CardAssembler | None = None, candidate_resolver: C.Resolver | None = None,
                 question_selector: C.QuestionSelector | None = None,
                 diagnostics: C.Diagnostics | None = None, readiness: C.Readiness | None = None,
                 session_store: C.SessionStore | None = None,
                 chooser: Callable[[str, str, list[str]], tuple[str, int, int] | None] | None = None):
        self.catalog = catalog
        self.chooser = chooser or choice.choose
        self.extractor = extractor or OpenModel()
        self.retriever = retriever or configured_retriever(catalog)
        self.rule_extractor = rule_extractor or Rules()
        self.runtime_id = uuid.uuid4().hex
        self.resolver = resolver or DeterministicCardAssembler(candidate_resolver=candidate_resolver,
            question_selector=question_selector,
            diagnostics=diagnostics, readiness=readiness, resolve_function=_resolve_current)
        self.session_store = session_store or InMemorySessionStore(max_sessions=lambda: MAX_SESSIONS,
            max_cached_requests=lambda: MAX_CACHED_REQUESTS, max_cached_state_bytes=lambda: MAX_CACHED_STATE_BYTES)
        # Compatibility views for existing one-process callers and tests.
        self._sessions = getattr(self.session_store, "_sessions", OrderedDict())
        self._created = getattr(self.session_store, "_created", OrderedDict())
        self._lock = getattr(self.session_store, "_lock", threading.RLock())
        self._inference_slots = threading.BoundedSemaphore(MAX_ANALYSES)

    # ---- session store ------------------------------------------------------------------------------------------

    @property
    def runtime_metadata(self) -> dict:
        manifest = ml.manifest()
        return {"runtime_id": self.runtime_id, "platform": sys.platform,
                "inference_endpoint_id": ml.endpoint_id(),
                "model_manifest": manifest.model_dump() if manifest else None}

    def _get(self, session_id: str) -> Session:
        return self.session_store.read(session_id)

    def _new(self) -> Session:
        return self.session_store.create(self.catalog.snapshot_id)

    def get(self, session_id: str) -> C.CardState:
        s = self._get(session_id)
        with s.lock:
            if s.state is None:
                raise _err(404, "session_expired", "Сессия пуста.")
            return s.state.model_copy(deep=True)

    # ---- transitions --------------------------------------------------------------------------------------------

    def _run(self, s: Session, request_id: str, expected: int, payload: dict, mutate: Callable[[Session], bool],
             *, extract_ms: float = 0, started_at: float | None = None):
        """CAS on revision, apply mutate (returns whether the old inputs go onto the undo stack), recompute."""
        def transition(s: Session) -> C.CardState:
            prev: Snapshot = (s.text, list(s.answers), list(s.edits))
            push = mutate(s)
            transition_started = time.perf_counter()
            llm = s.llm if s.llm and s.llm.get("text") == s.text else None
            rules = self.rule_extractor.extract(s.text, s.revision + 1)
            model = llm.get("extraction") if llm else None
            if model:
                model = _rebase(model, s.revision + 1)
            extraction = combine(rules, model)
            retrieval = self.retriever.retrieve(s.text, s.snapshot_id)
            if retrieval.scope.snapshot_id != s.snapshot_id:
                raise ValueError("Retrieval result changed the session snapshot")
            renamed = overrides.renamed_item(s.edits)
            if renamed:  # The user's name of the product decides the families; the text keeps its own spans.
                by_name = self.retriever.retrieve(renamed, s.snapshot_id)
                if by_name.scope.snapshot_id != s.snapshot_id:
                    raise ValueError("Retrieval result changed the session snapshot")
                retrieval = dataclasses.replace(retrieval, families=by_name.families, scope=by_name.scope,
                                                status=by_name.status)
            inputs = Inputs(s.text, s.revision + 1, s.answers, s.edits, llm,
                            extraction=extraction, retrieval=retrieval)
            t0 = time.perf_counter()
            out = self.resolver.resolve(self.catalog, inputs, s.id, request_id)
            if push and prev[0]:
                s.history = [*s.history, prev][-MAX_HISTORY:]
            now = time.perf_counter()
            resolution_ms = round((now - t0) * 1000, 1)
            total = round((now - (started_at if started_at is not None else transition_started)) * 1000, 1)
            out.state["timings_ms"] = C.Timings(parse=rules.timings.parse, retrieve=retrieval.timings.retrieve,
                extract=round(extract_ms, 1), resolve=resolution_ms, total=total)
            out.state["can_undo"] = bool(s.history)
            if s.corrections.get(s.text):
                out.state["corrections"] = [*s.corrections[s.text], *out.state.get("corrections", [])]
            out.state["revision"] = s.revision + 1
            state = C.CardState.model_validate(out.state)
            s.pending = out.pending_options
            return state

        return self.session_store.cas(s, request_id, payload, expected, transition)

    def parse(self, req: C.ParseRequest) -> C.CardState:
        # Correction comes first: rules, search and the model all read the corrected text.
        corrected, fixes = autocorrect(req.text, self.catalog.correct_word, getattr(self.catalog, "has_word", None),
                                    getattr(self.catalog, "is_common", None))
        if len(corrected) > C.MAX_TEXT:
            corrected, fixes = req.text, []

        def mutate(x: Session) -> bool:
            if x.text in (req.text, corrected):  # Resubmitting a reverted correction keeps the user's spelling.
                return False
            x.text, x.answers, x.edits = corrected, [], []
            _remember_corrections(x, corrected, fixes)
            return True

        if req.session_id:
            s = self._get(req.session_id)
        else:
            if req.expected_revision != 0:
                raise _err(409, "stale_revision", "Новая сессия начинается с ревизии 0.", revision=0)
            s = self.session_store.get_or_create(req.request_id, self.catalog.snapshot_id)

        return self._run(s, req.request_id, req.expected_revision, req.model_dump(), mutate)

    def analyze(self, req: C.AnalyzeRequest, extractor=None) -> C.CardState:
        """Run the open model on the accepted text, outside the session lock; publish only if the revision is still
        the one the request was based on (a late model answer never overwrites newer state)."""
        s = self._get(req.session_id)
        cached = self.session_store.replay(s, req.request_id, req.model_dump(), req.expected_revision)
        if cached is not None:
            return cached
        stale_revision = None
        with s.lock:
            if req.expected_revision != s.revision:
                stale_revision = s.revision
            else:
                if req.expected_revision in s.analyzing or not self._inference_slots.acquire(blocking=False):
                    raise _err(503, "analysis_busy", "Модель занята. Повторите анализ позже.", retryable=True,
                               revision=s.revision, request_id=req.request_id)
                s.analyzing = s.analyzing | {req.expected_revision}
                text = s.text
        if stale_revision is not None:
            # An identical analysis may have published after the first replay check.
            cached = self.session_store.replay(s, req.request_id, req.model_dump(), req.expected_revision)
            if cached is not None:
                return cached
            raise _err(409, "stale_revision", "Данные изменились.", retryable=True, revision=stale_revision)
        started_at = time.perf_counter()
        try:
            try:
                extracted = (OpenModel(extractor) if extractor else self.extractor).extract(
                    text, req.expected_revision + 1)
                validate_extraction(extracted, text, req.expected_revision + 1)
                extracted = extracted.model_copy(deep=True)
                result = {"text": text, "extraction": extracted, "error": None}
            except Exception as e:  # Keep rules when the model fails; never expose provider errors.
                extracted = C.ExtractionResult(status="partial")
                result = {"text": text, "extraction": extracted, "error": f"{type(e).__name__}: {e}"}

            def mutate(x: Session) -> bool:
                if x.text != text:
                    raise _err(409, "stale_revision", "Текст изменился во время анализа.", revision=x.revision)
                x.llm = result
                return False

            extract_ms = round((time.perf_counter() - started_at) * 1000, 1)
            if extracted.timings.extract:
                extract_ms = extracted.timings.extract
            state = self._run(s, req.request_id, req.expected_revision, req.model_dump(), mutate,
                              extract_ms=extract_ms, started_at=started_at)
            if extractor is None and result["error"] is None:
                state = self._answer_from_text(s, req.request_id, text, state)
            return state
        finally:
            with s.lock:
                s.analyzing = s.analyzing - {req.expected_revision}
            self._inference_slots.release()

    def _answer_from_text(self, s: Session, request_id: str, text: str, state: C.CardState) -> C.CardState:
        """Questions the description already answers by implication are answered by the model: «мужская» →
        «Половой признак: Мужской», «размер 122» of a child's robe → «Рост: 122 см». The value carries the quoted
        words as evidence and stays editable. Bounded: at most INFERRED_CALLS short calls; any failure, or no
        verbatim quote, leaves the question or the empty field as it was."""
        tried: set[str] = set()
        for step in range(INFERRED_CALLS):
            target = self._open_characteristic(s, state, tried)
            if target is None:
                break
            name, options = target
            tried.add(norm(name))
            try:
                chosen = self.chooser(text, name, list(options))
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                break
            if chosen is None:
                continue
            label, start, end = chosen
            answer = {**options[label], "inferred": {"start": start, "end": end}}

            def mutate(x: Session, answer=answer) -> bool:
                if x.text != text:
                    raise _err(409, "stale_revision", "Текст изменился во время анализа.", revision=x.revision)
                if len(x.answers) + len(x.edits) >= MAX_INPUTS:
                    raise _err(409, "too_many_inputs", "Слишком много ответов.", revision=x.revision)
                x.answers = [*x.answers, {**answer, "revision": x.revision + 1}]
                return False

            try:
                state = self._run(s, f"{request_id}:infer-{step}", state.revision,
                                  {"inferred": label, "characteristic": name}, mutate)
            except ServiceError:
                break  # the user moved on meanwhile: their state wins
        return state

    def _open_characteristic(self, s: Session, state: C.CardState,
                             tried: set[str]) -> tuple[str, dict[str, dict]] | None:
        """The characteristic the card asks about, else a required one every remaining position has and the text
        does not state; with its catalog values as answer payloads. Never the product or a conflict question."""
        question = state.clarification
        if question is not None and question.kind not in ("characteristic", "required"):
            return None
        if question is not None and question.char_name and norm(question.char_name) not in tried:
            with s.lock:
                pending = dict(s.pending)
            options = {o.label_ru: pending[o.id] for o in question.options if not o.is_dont_know and o.id in pending}
            if len(options) >= 2:
                return question.char_name, options
        alive = [self.catalog.position(c.ref.code, c.ref.version) for c in state.candidates if c.status == "alive"]
        alive = [p for p in alive if p is not None]
        stated = {norm(f.label_ru) for f in state.fields if f.status in ("from_text", "confirmed")}
        stated |= {norm(a.property_raw) for a in state.assertions if a.status != "rejected"}
        for ch in alive[0].chars if alive else []:
            name = norm(ch.name)
            if not ch.required or name in tried or name in stated:
                continue
            values: dict[str, dict] = {}
            for p in alive:
                same = next((c for c in p.chars if norm(c.name) == name), None)
                if same is None:
                    break
                values.update((value_display(v), v) for v in catalog_values(same))
            else:
                if 2 <= len(values) <= MAX_INFERRED_VALUES:
                    key = name.strip()
                    return ch.name, {label: {"kind": "char", "char": key, "value": v} for label, v in values.items()}
        return None

    def answer(self, req: C.AnswerRequest) -> C.CardState:
        s = self._get(req.session_id)

        def mutate(x: Session) -> bool:
            question = x.state.clarification if x.state else None
            if question is None or question.id != req.question_id:
                raise _err(409, "stale_revision", "Вопрос устарел. Обновите карточку.", revision=x.revision)
            if req.value is not None:
                if not question.accepts_value or not isinstance(req.value, (C.NumberValue, C.RangeValue)):
                    raise _err(422, "invalid_input", "Для этого вопроса выберите предложенный вариант.")
                value = req.value
                if isinstance(value, C.RangeValue) and value.lo is None and value.hi is None:
                    raise _err(422, "invalid_input", "Укажите число или границы диапазона.")
                named = UNITS.get(norm(value.unit_name).strip().rstrip(".")) if value.unit_name else None
                if value.unit_name and named is None:
                    raise _err(422, "invalid_input", "Единица измерения не распознана.")
                unit = value.unit_okei or (named[0] if named else question.value_unit_okei)
                if unit and unit not in OKEI:
                    raise _err(422, "invalid_input", "Код единицы измерения не распознан.")
                if named and named[0] != unit:
                    raise _err(422, "invalid_input", "Название и код единицы измерения не совпадают.")
                expected_unit = question.value_unit_okei
                if expected_unit and (not unit or OKEI[unit][0] != OKEI[expected_unit][0]):
                    raise _err(422, "invalid_input", "Единица не подходит к этому свойству.")
                raw = {**value.model_dump(exclude={"type", "unit_okei", "unit_name"}), "kind": value.type,
                       "okei_code": unit, "okei_name": OKEI[unit][2] if unit else None}
                opt = {"question_id": question.id, "kind": "char", "char": norm(question.char_name).strip(),
                       "value": raw, "subject": question.subject.model_dump() if question.subject else None}
            else:
                assert req.option_id is not None  # AnswerRequest validates exactly one answer representation.
                selected_option = x.pending.get(req.option_id)
                if selected_option is None or selected_option["question_id"] != req.question_id:
                    raise _err(409, "stale_revision", "Вариант устарел. Обновите карточку.", revision=x.revision)
                opt = selected_option
            if len(x.answers) + len(x.edits) >= MAX_INPUTS:
                raise _err(422, "invalid_input", "Слишком много ответов; начните описание заново.")
            x.answers = [*x.answers, {**opt, "revision": x.revision + 1, "option_id": req.option_id}]
            return True

        return self._run(s, req.request_id, req.expected_revision, req.model_dump(), mutate)

    def edit(self, req: C.EditRequest) -> C.CardState:
        s = self._get(req.session_id)

        def mutate(x: Session) -> bool:
            p = req.patch
            if isinstance(p, C.ReplaceSpanPatch):
                try:
                    changed = replace_span(x.text, p, snapshot_id=x.snapshot_id, revision=x.revision)
                except ValueError as error:
                    raise _err(409, "stale_revision", "Исправление устарело. Обновите карточку.",
                               revision=x.revision) from error
                if changed == x.text:
                    return False
                _remember_corrections(x, changed, _after_span_edit(x.corrections.get(x.text, []), p))
                x.text, x.answers, x.edits = changed, [], []
            elif isinstance(p, C.ReplaceTextPatch):
                if p.text == x.text:
                    return False
                x.text, x.answers, x.edits = p.text, [], []
            elif isinstance(p, C.SetFieldPatch):
                if len(x.answers) + len(x.edits) >= MAX_INPUTS:
                    raise _err(422, "invalid_input", "Слишком много правок; начните описание заново.")
                target = next((f for f in (x.state.fields if x.state else []) if f.id == p.field_id), None)
                if p.field_id and target is None:
                    raise _err(409, "stale_revision", "Поле устарело. Обновите карточку.", revision=x.revision)
                x.edits = [*x.edits, {"label_ru": target.label_ru if target else p.label_ru, "value": p.value,
                    "field_id": p.field_id, "revision": x.revision + 1,
                    "subject": target.subject.model_dump() if target and target.subject else None}]
            else:
                if len(x.answers) + len(x.edits) >= MAX_INPUTS:
                    raise _err(422, "invalid_input", "Слишком много правок; начните описание заново.")
                x.edits = [*x.edits, {**_override(x, p), "revision": x.revision + 1}]
            return True

        return self._run(s, req.request_id, req.expected_revision, req.model_dump(), mutate)

    def undo(self, req: C.UndoRequest) -> C.CardState:
        s = self._get(req.session_id)

        def mutate(x: Session) -> bool:
            if not x.history:
                raise _err(409, "invalid_input", "Нечего отменять.", revision=x.revision)
            *rest, last = x.history
            x.history = rest
            x.text, x.answers, x.edits = last
            return False  # undo creates a new revision but is not itself undoable

        return self._run(s, req.request_id, req.expected_revision, req.model_dump(), mutate)
