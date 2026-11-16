"""Default adapters for the deterministic engine stage contracts."""

from __future__ import annotations

from collections.abc import Callable

from src.ktru import contracts as C
from src.ktru.catalog.reader import FamilyHit, Position
from src.ktru.engine.compatibility import resolve_family
from src.ktru.engine.identity import _id
from src.ktru.engine.plausibility import plausibility_diagnostics
from src.ktru.engine.questions import choose_question, question_budget_spent
from src.ktru.engine.readiness import build_diagnostics, evaluate_readiness
from src.ktru.engine.stage_types import BoundFacts, FieldResult, Inputs, Outcome, ResolutionData


class EngineQuestions:
    def choose(self, catalog: C.CatalogReader, inputs: Inputs, family: FamilyHit | None, hits: list[FamilyHit],
               confirmed: bool, facts: BoundFacts, resolution: ResolutionData, fields: FieldResult) -> C.QuestionResult:
        if family is not None:
            return choose_question(catalog, inputs, family, hits, confirmed, facts, resolution, fields)
        weak = [hit for hit in hits[:5] if hit.head_ok or hit.semantic_score is not None]
        if not weak or question_budget_spent(inputs):
            return C.QuestionResult()
        qid = _id("q", inputs.revision, "family", catalog.snapshot_id)
        pending = {f"{qid}-{index}": {"question_id": qid, "kind": "family", "family_id": hit.family_id}
                   for index, hit in enumerate(weak)}
        options = [C.ClarificationOption(id=f"{qid}-{index}", label_ru=f"{hit.name} ({len(hit.members)} поз.)",
                                        remaining_count=len(hit.members)) for index, hit in enumerate(weak)]
        clarification = C.Clarification(id=qid, kind="family", base_revision=inputs.revision,
            snapshot_id=catalog.snapshot_id, question_ru="Это один из этих товаров?",
            why_ru="Совпадение с каталогом слабое: подтвердите группу или уточните описание.",
            options=options, expected_remaining_worst=max(option.remaining_count for option in options))
        return C.QuestionResult(clarification=clarification, pending_options=pending)


class EngineDiagnostics:
    def build(self, catalog: C.CatalogReader, inputs: Inputs, work: str, family: FamilyHit | None,
              facts: BoundFacts, resolution: ResolutionData, fields: FieldResult) -> C.DiagnosticsResult:
        diagnostics = (build_diagnostics(catalog, inputs, work, family, facts, resolution, fields)
                       if family is not None else list(resolution.diagnostics))
        flagged = {i for d in diagnostics if d.kind == "implausible" for i in d.assertion_ids}
        numbers = [b.assertion for b in facts.all_bound if b.assertion.id not in flagged]
        diagnostics += plausibility_diagnostics(catalog, inputs.text, family, numbers, inputs.revision)
        return C.DiagnosticsResult(diagnostics=diagnostics)


class EngineReadiness:
    def evaluate(self, final: C.Decision | None, alive: list[Position], diagnostics: list[C.Diagnostic],
                 fields: list[C.CardField]) -> C.ReadinessResult:
        semantic_confirmation = any(d.kind == "out_of_scope" and d.severity == "warning" and not d.assertion_ids
                                    for d in diagnostics)
        if final is None and not alive and semantic_confirmation:
            return C.ReadinessResult(readiness="no_code",
                                     reasons_ru=["Подтвердите группу товара, чтобы проверить позиции КТРУ"])
        return evaluate_readiness(final, alive, diagnostics, fields)


class EngineResolver:
    def resolve(self, catalog: C.CatalogReader, inputs: Inputs, family: FamilyHit | None, facts: BoundFacts,
                confirmed: bool, confirm_reason: str, funnel: list[C.FunnelStage],
                diagnostics: list[C.Diagnostic]) -> ResolutionData:
        if family is None:
            return ResolutionData(C.ResolutionResult(), [], {}, {}, set(), set(), None, None,
                                  list(funnel), list(diagnostics))
        return resolve_family(catalog, inputs, family, facts, confirmed, confirm_reason, funnel, diagnostics)


class DeterministicCardAssembler:
    def __init__(self, *, candidate_resolver: C.Resolver | None = None, question_selector: C.QuestionSelector | None = None,
                 diagnostics: C.Diagnostics | None = None, readiness: C.Readiness | None = None,
                 resolve_function: Callable[..., Outcome] | None = None):
        self.candidate_resolver = candidate_resolver
        self.question_selector = question_selector
        self.diagnostics = diagnostics
        self.readiness = readiness
        self.resolve_function = resolve_function

    def resolve(self, catalog: C.CatalogReader, inputs: Inputs, session_id: str, request_id: str) -> Outcome:
        from src.ktru.engine.resolve import resolve  # noqa: PLC0415 - avoid the engine adapter import cycle.

        function = self.resolve_function or resolve
        if not any(stage is not None for stage in (self.candidate_resolver, self.question_selector, self.diagnostics, self.readiness)):
            return function(catalog, inputs, session_id, request_id)
        return function(catalog, inputs, session_id, request_id, resolver=self.candidate_resolver,
                        question_selector=self.question_selector,
                        diagnostics=self.diagnostics, readiness=self.readiness)


# Retain the former adapter name for existing in-process callers.
DeterministicResolver = DeterministicCardAssembler
