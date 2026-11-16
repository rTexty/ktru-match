"""Real dependency boundaries preserve session and transition semantics."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from src.ktru import contracts as C
from src.ktru.service.pipeline import Pipeline, ServiceError
from tests.ktru import test_s0_stages


@pytest.fixture
def stage_catalog(tmp_path):
    return test_s0_stages.stage_catalog.__wrapped__(tmp_path)


def request(text="Молоток, масса молотка 500 г", request_id="first"):
    return C.ParseRequest(request_id=request_id, expected_revision=0, text=text)


def test_extractor_retained_output_cannot_change_accepted_cached_facts(stage_catalog):
    class RetainingExtractor:
        saved = None

        def extract(self, text, text_revision):
            quote = "покрытие синее"
            start = text.index(quote)
            self.saved = C.ExtractionResult(status="analyzed", assertions=[C.Assertion(id="retained-fact",
                property_raw="Покрытие", value_raw="синее", value=C.EnumValue(label="синее"), origin="text",
                extractor="llm", span=C.Span(start=start, end=start + len(quote), text=quote,
                    text_revision=text_revision))])
            return self.saved

    extractor = RetainingExtractor()
    pipeline = Pipeline(stage_catalog, extractor=extractor)
    original = "Молоток, масса молотка 500 г, покрытие синее"
    state = pipeline.parse(request(original))
    analyzed = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id="retained-analysis",
        expected_revision=state.revision))
    extractor.saved.assertions[0].value.label = "красное"
    refreshed = pipeline.parse(C.ParseRequest(session_id=state.session_id, request_id="retained-recompute",
        expected_revision=analyzed.revision, text=original))
    accepted = next(item for item in refreshed.assertions if item.id == "retained-fact")
    assert accepted.value.label == "синее" and accepted.value_raw == "синее"


def dependencies():
    from src.ktru.engine.adapters import DeterministicResolver, EngineDiagnostics, EngineQuestions, EngineReadiness
    from src.ktru.service.session_store import InMemorySessionStore

    calls = []

    class Questions(EngineQuestions):
        def choose(self, *args):
            calls.append(("questions", args[1].revision, args[2] is None))
            return super().choose(*args)

    class Diagnostics(EngineDiagnostics):
        def build(self, *args):
            calls.append(("diagnostics", args[1].revision, args[3] is None))
            result = super().build(*args)
            marker = C.Diagnostic(id="injected", kind="typo", severity="info", message_ru="Проверка адаптера",
                reason_ru="Тест", fixes=[C.Fix(id="adapter-fix", label_ru="Исправить", patch={"type": "replace_text", "text": "Молоток"})])
            return result.model_copy(update={"diagnostics": [*result.diagnostics, marker]})

    class Readiness(EngineReadiness):
        def evaluate(self, *args):
            calls.append(("readiness", None, None))
            result = super().evaluate(*args)
            return result.model_copy(update={"reasons_ru": [*result.reasons_ru, "Проверка адаптера"]})

    class Resolver(DeterministicResolver):
        def resolve(self, *args):
            calls.append(("resolver", args[1].revision, None))
            return super().resolve(*args)

    return calls, Resolver(question_selector=Questions(), diagnostics=Diagnostics(), readiness=Readiness()), InMemorySessionStore


@pytest.mark.parametrize("text,unknown", [("Молоток, масса молотка 500 г", False), ("Ксилофон, масса упаковки 500 г", True)])
def test_real_adapters_run_for_known_and_unknown_families(stage_catalog, text, unknown):
    calls, resolver, Store = dependencies()
    store = Store()
    pipeline = Pipeline(stage_catalog, resolver=resolver, session_store=store)
    req = request(text)
    state = pipeline.parse(req)
    assert calls == [("resolver", 1, None), ("diagnostics", 1, unknown), ("questions", 1, unknown), ("readiness", None, None)]
    marker = next(item for item in state.diagnostics if item.id == "injected")
    assert marker.fixes[0].snapshot_id == state.snapshot_id and marker.fixes[0].base_revision == 1
    assert "Проверка адаптера" in state.readiness_reasons_ru
    assert pipeline.parse(req) == state and len(calls) == 4
    edited = pipeline.edit(C.EditRequest(session_id=state.session_id, request_id="edit", expected_revision=1,
        patch=C.ReplaceTextPatch(text="Молоток, масса молотка 700 г")))
    undone = pipeline.undo(C.UndoRequest(session_id=state.session_id, request_id="undo", expected_revision=2))
    assert (edited.revision, undone.revision, undone.text) == (2, 3, text)
    assert len(calls) == 12 and store.read(state.session_id).snapshot_id == stage_catalog.snapshot_id


def test_injected_store_keeps_lru_and_request_byte_limits(stage_catalog):
    _, resolver, Store = dependencies()
    store = Store(max_sessions=2, max_cached_state_bytes=1)
    pipeline = Pipeline(stage_catalog, resolver=resolver, session_store=store)
    first = pipeline.parse(request(request_id="first"))
    second = pipeline.parse(request(request_id="second"))
    assert store.read(first.session_id).requests == {}
    assert pipeline.get(first.session_id) == first
    third = pipeline.parse(request(request_id="third"))
    assert pipeline.get(first.session_id) == first and pipeline.get(third.session_id) == third
    for operation in (lambda: pipeline.get(second.session_id), lambda: pipeline.parse(request(request_id="second"))):
        with pytest.raises(ServiceError) as error:
            operation()
        assert error.value.status == 404 and error.value.body.code == "session_expired"
    with pytest.raises(ServiceError) as error:
        pipeline.parse(C.ParseRequest(session_id=first.session_id, request_id="first", expected_revision=0, text=first.text))
    assert error.value.body.code == "stale_revision"


def test_injected_store_serializes_replay_and_rolls_back_failed_transition(stage_catalog):
    calls, resolver, Store = dependencies()
    store = Store()
    pipeline = Pipeline(stage_catalog, resolver=resolver, session_store=store)
    barrier = threading.Barrier(2)
    req = request(request_id="parallel")

    def run():
        barrier.wait(timeout=3)
        return pipeline.parse(req)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        states = [future.result(timeout=5) for future in futures]
    assert states[0] == states[1] and len(calls) == 4 and len(pipeline._sessions) == 1
    session = store.read(states[0].session_id)

    def fail(candidate):
        candidate.text = "Поврежденный текст"
        candidate.answers.append({"kind": "dont_know"})
        candidate.history.append(("changed", [], []))
        raise ValueError("abort")

    with pytest.raises(ValueError, match="abort"):
        store.cas(session, "failure", {"value": 1}, 1, fail)
    assert session.text == req.text and session.revision == 1
    assert session.answers == [] and session.history == [] and pipeline.get(session.id) == states[0]
    with pytest.raises(ServiceError) as error:
        pipeline.parse(request(text="Молоток", request_id="parallel"))
    assert error.value.body.code == "request_conflict"


def test_model_runs_outside_store_cas_and_cannot_publish_stale_state(stage_catalog):
    _, resolver, Store = dependencies()
    started, release = threading.Event(), threading.Event()
    store = Store()

    class Model:
        def extract(self, text, revision):
            started.set()
            assert release.wait(timeout=3)
            return C.ExtractionResult(status="analyzed")

    pipeline = Pipeline(stage_catalog, resolver=resolver, session_store=store, extractor=Model())
    state = pipeline.parse(request())
    req = C.AnalyzeRequest(session_id=state.session_id, request_id="analyze", expected_revision=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pipeline.analyze, req)
        try:
            assert started.wait(timeout=3)
            edited = pipeline.edit(C.EditRequest(session_id=state.session_id, request_id="edit", expected_revision=1,
                patch=C.ReplaceTextPatch(text="Молоток, масса молотка 700 г")))
        finally:
            release.set()
        with pytest.raises(ServiceError) as error:
            future.result(timeout=5)
    assert error.value.body.code == "stale_revision"
    assert pipeline.get(state.session_id) == edited and edited.revision == 2
    assert store.read(state.session_id).analyzing == set()


def test_pipeline_injects_stage_adapters_on_answer_analyze_and_replay(stage_catalog):
    calls, resolver, Store = dependencies()

    class Model:
        def extract(self, text, revision):
            return C.ExtractionResult(status="analyzed")

    pipeline = Pipeline(stage_catalog, question_selector=resolver.question_selector,
        diagnostics=resolver.diagnostics, readiness=resolver.readiness, extractor=Model(), session_store=Store())
    state = pipeline.parse(request("Молоток"))  # named family: the first question is a characteristic
    question = state.clarification
    answer = C.AnswerRequest(session_id=state.session_id, request_id="answer", expected_revision=1,
        question_id=question.id, option_id=question.options[0].id)
    answered = pipeline.answer(answer)
    analyzed = C.AnalyzeRequest(session_id=state.session_id, request_id="analyzed", expected_revision=2)
    final = pipeline.analyze(analyzed)
    assert final.revision == 3 and final.final is not None
    assert len(calls) == 9
    assert pipeline.answer(answer) == answered and pipeline.analyze(analyzed) == final
    assert len(calls) == 9


def test_store_evicted_replay_checks_revision_and_snapshot_is_pinned(stage_catalog):
    _, resolver, Store = dependencies()
    store = Store(max_cached_requests=1)
    pipeline = Pipeline(stage_catalog, resolver=resolver, session_store=store)
    state = pipeline.parse(request())
    edit = C.EditRequest(session_id=state.session_id, request_id="new", expected_revision=1,
        patch=C.ReplaceTextPatch(text="Молоток, масса молотка 700 г"))
    current = pipeline.edit(edit)
    session = store.read(state.session_id)
    assert list(session.requests) == ["new"] and pipeline.edit(edit) == current
    with pytest.raises(ServiceError) as error:
        store.replay(session, "first", request().model_dump(), 0)
    assert error.value.body.code == "stale_revision"
    with pytest.raises(ValueError, match="pinned snapshot"):
        store.cas(session, "wrong-snapshot", {}, 2,
            lambda _: current.model_copy(update={"revision": 3, "snapshot_id": "another-snapshot"}))
    assert session.snapshot_id == stage_catalog.snapshot_id and session.revision == 2


@pytest.mark.parametrize("text,has_candidates,has_conflicts", [
    ("Молоток, масса молотка 500 г", True, False),
    ("Молоток, масса молотка 500 г, масса молотка 700 г", True, True),
    ("Ксилофон, масса упаковки 500 г", False, False),
])
def test_candidate_resolver_is_a_real_typed_boundary(stage_catalog, text, has_candidates, has_conflicts):
    from src.ktru.engine.adapters import EngineResolver

    observed = []

    class CandidateResolver(EngineResolver):
        def resolve(self, *args):
            result = super().resolve(*args)
            result = replace(result, result=result.result.model_copy(update={
                "bindings": [binding.model_copy(update={"rule": "recorded_adapter_binding"})
                             for binding in result.result.bindings]}))
            observed.append(result)
            assert isinstance(result.result, C.ResolutionResult)
            return result

    pipeline = Pipeline(stage_catalog, candidate_resolver=CandidateResolver())
    req = request(text)
    state = pipeline.parse(req)
    assert len(observed) == 1
    result = observed[0].result
    assert bool(result.candidates) == has_candidates and bool(result.conflicts) == has_conflicts
    assert state.candidates == result.candidates and state.bindings == result.bindings
    assert state.conflicts == result.conflicts
    if has_candidates:
        assert result.bindings
        assert bool(result.compatibility) is not has_conflicts
    assert pipeline.parse(req) == state and len(observed) == 1


def test_returned_cards_cannot_mutate_store_current_state_or_replay(stage_catalog):
    class Model:
        def extract(self, text, revision):
            return C.ExtractionResult(status="analyzed")

    pipeline = Pipeline(stage_catalog, extractor=Model())
    req = request()
    parsed = pipeline.parse(req)
    baseline = parsed.model_copy(deep=True)
    session_id = baseline.session_id

    def damage(state):
        state.revision = 999
        state.text = "Изменение вне перехода"
        state.search_scope.snapshot_id = "another-snapshot"
        state.fields[0].label_ru = "Измененное поле"
        state.assertions[0].property_raw = "Измененное свойство"

    damage(parsed)
    assert pipeline.get(session_id) == baseline and pipeline.parse(req) == baseline
    replay = pipeline.parse(req)
    damage(replay)
    assert pipeline.parse(req) == baseline
    current = pipeline.get(session_id)
    damage(current)
    assert pipeline.get(session_id) == baseline
    analyze = C.AnalyzeRequest(session_id=session_id, request_id="model-copy", expected_revision=1)
    analyzed = pipeline.analyze(analyze)
    analyzed_baseline = analyzed.model_copy(deep=True)
    damage(analyzed)
    assert pipeline.get(session_id) == analyzed_baseline
    assert pipeline.analyze(analyze) == analyzed_baseline
    assert pipeline.parse(req) == baseline


@pytest.mark.parametrize("text", ["Молоток, масса молотка 500 г", "Ксилофон, масса упаковки 500 г"])
def test_invalid_injected_diagnostic_revision_is_rejected_without_publication(stage_catalog, text):
    from src.ktru.engine.adapters import EngineDiagnostics

    class Diagnostics(EngineDiagnostics):
        enabled = True

        def build(self, catalog, inputs, *args):
            result = super().build(catalog, inputs, *args)
            if not self.enabled:
                return result
            quote = inputs.text[:8]
            invalid = C.Diagnostic(id="invalid-adapter-revision", kind="ambiguous", severity="warning",
                message_ru="Проверка адаптера", reason_ru="Тест", evidence=[C.TextEvidence(span=C.Span(
                    start=0, end=len(quote), text=quote, text_revision=999))])
            return result.model_copy(update={"diagnostics": [*result.diagnostics, invalid]})

    diagnostics = Diagnostics()
    pipeline = Pipeline(stage_catalog, diagnostics=diagnostics)
    req = request(text)
    with pytest.raises(ValueError, match="Source span"):
        pipeline.parse(req)
    session_id = pipeline._created[req.request_id]
    session = pipeline.session_store.read(session_id)
    assert session.revision == 0 and session.text == "" and not session.requests and not session.history
    with pytest.raises(ServiceError) as error:
        pipeline.get(session_id)
    assert error.value.status == 404 and error.value.body.code == "session_expired"
    diagnostics.enabled = False
    accepted = pipeline.parse(req)
    assert accepted.session_id == session_id and accepted.revision == 1
    assert pipeline.get(session_id) == accepted


def test_fresh_model_future_revision_is_rejected_and_rules_remain(stage_catalog):
    text = "Молоток, масса молотка 500 г, покрытие водоотталкивающее"

    class Model:
        def extract(self, accepted_text, revision):
            quote = "покрытие водоотталкивающее"
            start = accepted_text.index(quote)
            span = C.Span(start=start, end=start + len(quote), text=quote, text_revision=999)
            assertion = C.Assertion(id="future-model", property_raw="Покрытие", value_raw="водоотталкивающее",
                value=C.EnumValue(label="водоотталкивающее"), origin="text", extractor="llm", span=span,
                evidence=[C.TextEvidence(span=span, extractor="llm")])
            return C.ExtractionResult(assertions=[assertion], status="analyzed")

    pipeline = Pipeline(stage_catalog, extractor=Model())
    parsed = pipeline.parse(request(text))
    result = pipeline.analyze(C.AnalyzeRequest(session_id=parsed.session_id, request_id="invalid-model",
        expected_revision=parsed.revision))
    assert result.status == "partial"
    assert any(diagnostic.kind == "model_unavailable" for diagnostic in result.diagnostics)
    assert not any(assertion.id == "future-model" for assertion in result.assertions)
    assert any(isinstance(assertion.value, C.NumberValue) and assertion.value.x == "500"
               and assertion.extractor == "rules" for assertion in result.assertions)
    for assertion in result.assertions:
        if assertion.span is not None:
            assert assertion.span.text_revision == result.revision
            assert result.text[assertion.span.start:assertion.span.end] == assertion.span.text
        for evidence in assertion.evidence:
            if isinstance(evidence, C.TextEvidence):
                assert evidence.span.text_revision == result.revision


def test_duplicate_analyze_returns_replay_when_first_check_precedes_other_publish(stage_catalog):
    from src.ktru.service.session_store import InMemorySessionStore

    checked, release = threading.Event(), threading.Event()
    calls = []

    class Store(InMemorySessionStore):
        def replay(self, *args):
            result = super().replay(*args)
            if result is None and threading.current_thread().name.startswith("delayed-analysis"):
                checked.set()
                assert release.wait(timeout=3)
            return result

    class Model:
        def extract(self, text, revision):
            calls.append((text, revision))
            return C.ExtractionResult(status="analyzed")

    pipeline = Pipeline(stage_catalog, session_store=Store(), extractor=Model())
    state = pipeline.parse(request())
    req = C.AnalyzeRequest(session_id=state.session_id, request_id="same-analysis", expected_revision=1)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="delayed-analysis") as pool:
        delayed = pool.submit(pipeline.analyze, req)
        try:
            assert checked.wait(timeout=3)
            completed = pipeline.analyze(req)
        finally:
            release.set()
        replayed = delayed.result(timeout=5)
    assert completed == replayed and completed.revision == 2
    assert len(calls) == 1 and pipeline.get(state.session_id) == completed
