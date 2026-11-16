from __future__ import annotations

from dataclasses import dataclass, field

from src.ktru import contracts as C
from src.ktru.catalog.reader import Char, Position


@dataclass
class Bound:
    assertion: C.Assertion
    char_name: str | None  # normalized family-level characteristic name
    note: str | None = None



@dataclass(frozen=True)
class Inputs:
    text: str
    revision: int
    answers: list[dict] = field(default_factory=list)  # {"kind": family|char|dont_know|conflict, ...}
    edits: list[dict] = field(default_factory=list)  # {"label_ru", "value"}
    llm: dict | None = None  # Legacy model envelope retained for direct callers.
    extraction: C.ExtractionResult | None = None
    retrieval: C.RetrievalResult | None = None



@dataclass
class Outcome:
    state: dict
    pending_options: dict[str, dict]



@dataclass(frozen=True)
class BoundFacts:
    fmap: dict[str, list[Char]]
    display: dict[str, str]
    extracted: list[Bound]
    user: list[Bound]
    all_bound: list[Bound]
    by_char: dict[str, list[C.Assertion]]
    superseded: list[Bound]
    dont_know: set[str]


@dataclass(frozen=True)
class ResolutionData:
    result: C.ResolutionResult
    alive: list[Position]
    active: dict[str, list[C.Assertion]]
    conflicts: dict[str, list[C.Assertion]]
    implausible: set[str]
    unresolved: set[str]
    final: C.Decision | None
    suggestion: C.Decision | None
    funnel: list[C.FunnelStage]
    diagnostics: list[C.Diagnostic]


@dataclass(frozen=True)
class FieldResult:
    fields: list[C.CardField]
    pos: Position | None
    amb_fields: dict[str, list[dict]]
