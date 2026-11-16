"""S0 contract: HTTP models, independent assertions, evidence and candidate bindings.

Field names are English snake_case, UI text Russian. See source-of-truth/03-architecture/SCHEMA.md.
Numbers travel as decimal strings to avoid float precision loss.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.ktru.contract_validation import validate_card

if TYPE_CHECKING:
    from src.ktru.catalog.reader import FamilyHit, PortalChar, Position, SearchResult
    from src.ktru.engine.stage_types import BoundFacts, FieldResult, Inputs, Outcome, ResolutionData
    from src.ktru.service.session_store import Session

CONTRACT_VERSION = "s0-4"
MAX_TEXT = 4000
MAX_SPLIT_TEXT = 40_000

Source = Literal["ktru", "portal", "new"]
Operator = Literal["eq", "neq", "gt", "ge", "lt", "le", "between", "approx"]
Polarity = Literal["positive", "negative", "uncertain"]
Origin = Literal["text", "user_answer", "user_edit", "catalog"]
Extractor = Literal["rules", "llm", "both", "none"]
SubjectKind = Literal["item", "part", "package", "set", "unknown"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PositionRef(Model):
    snapshot_id: str
    code: str
    version: int = Field(ge=1)


class PortalPrice(Model):
    amount: str
    currency: Literal["RUB"] = "RUB"
    kind: Literal["median", "reference"]
    unit: str | None = None


class PortalCode(Model):
    code: str
    name: str | None = None


class ProductPhoto(Model):
    ste_id: int
    name: str
    source_url: str
    image_url: str | None
    relation: Literal["portal_ktru", "okpd2_candidate"]
    price: PortalPrice | None = None
    offers_count: int = Field(default=0, ge=0)
    fetched_at: str
    # Why the card belongs to the chosen code: the Portal names the KTRU code itself, the same OKPD2 code, or a
    # broader/narrower OKPD2 code. The card's OKPD2 and its Portal classifier (КПГЗ) entry are shown as evidence.
    match: Literal["ktru", "okpd2", "okpd2_related"] = "okpd2"
    okpd2: PortalCode | None = None
    kpgz: PortalCode | None = None


class CharacteristicRef(Model):
    snapshot_id: str
    position_code: str
    position_version: int = Field(ge=1)
    char_code: str


class AllowedValueRef(Model):
    characteristic_ref: CharacteristicRef
    value_code: str


class CatalogSource(Model):
    xml: str
    element_index: int | None = None
    archive: str | None = None
    characteristic_index: int | None = None
    value_index: int | None = None


class Span(Model):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str
    text_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def exact_length(self):
        if self.end < self.start or self.end - self.start != len(self.text):
            raise ValueError("Span must use exact Unicode code-point offsets")
        return self


class MappedTextSpan(Model):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def exact_length(self):
        if self.end < self.start or self.end - self.start != len(self.text):
            raise ValueError("Mapped span must use exact Unicode code-point offsets")
        return self


class SourceMapSegment(Model):
    """Changed ranges from original text to search text; unchanged gaps are implicit."""

    original_start: int = Field(ge=0)
    original_end: int = Field(ge=0)
    transformed_start: int = Field(ge=0)
    transformed_end: int = Field(ge=0)
    kind: Literal["layout", "typo"]

    @model_validator(mode="after")
    def ordered(self):
        if self.original_end <= self.original_start or self.transformed_end <= self.transformed_start:
            raise ValueError("Source map ranges must be ordered")
        return self


class TextEvidence(Model):
    type: Literal["text"] = "text"
    span: Span
    extractor: Extractor = "rules"


class UserEvidence(Model):
    type: Literal["user_answer", "user_edit"]
    revision: int
    question_id: str | None = None
    option_id: str | None = None
    field_id: str | None = None
    answer_text: str | None = None


class CatalogEvidence(Model):
    type: Literal["catalog"] = "catalog"
    position_ref: PositionRef
    characteristic_ref: CharacteristicRef | None = None
    value_ref: AllowedValueRef | None = None


Evidence = Annotated[TextEvidence | UserEvidence | CatalogEvidence, Field(discriminator="type")]


class SubjectRef(Model):
    id: str
    kind: SubjectKind
    label: str | None = None
    parent_subject_id: str | None = None
    evidence: list[Evidence] = []


def finite_decimal(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Expected a decimal string") from error
    if not parsed.is_finite():
        raise ValueError("Decimal must be finite")
    if abs(parsed.adjusted()) > 64 or abs(int(parsed.as_tuple().exponent)) > 64:
        raise ValueError("Decimal magnitude or scale is too large")
    return value


# ---- values -------------------------------------------------------------------------------------------------------


class EnumValue(Model):
    type: Literal["enum"] = "enum"
    label: str


class NumberValue(Model):
    type: Literal["number"] = "number"
    x: str = Field(max_length=100)
    unit_okei: str | None = Field(default=None, max_length=10)
    unit_name: str | None = Field(default=None, max_length=60)

    _finite = field_validator("x")(finite_decimal)


class RangeValue(Model):
    type: Literal["range"] = "range"
    lo: str | None = Field(default=None, max_length=100)
    lo_inclusive: bool = True
    hi: str | None = Field(default=None, max_length=100)
    hi_inclusive: bool = True
    unit_okei: str | None = Field(default=None, max_length=10)
    unit_name: str | None = Field(default=None, max_length=60)

    _finite = field_validator("lo", "hi")(finite_decimal)

    @model_validator(mode="after")
    def ordered(self):
        if self.lo is not None and self.hi is not None:
            lo, hi = Decimal(self.lo), Decimal(self.hi)
            if lo > hi or (lo == hi and not (self.lo_inclusive and self.hi_inclusive)):
                raise ValueError("Range is reversed or empty")
        return self


class BoolValue(Model):
    type: Literal["bool"] = "bool"
    v: bool


class TextValue(Model):
    type: Literal["text"] = "text"
    s: str


class UnparsedValue(Model):
    type: Literal["unparsed"] = "unparsed"
    s: str
    reason: str


Value = Annotated[EnumValue | NumberValue | RangeValue | BoolValue | TextValue | UnparsedValue, Field(discriminator="type")]


# ---- facts --------------------------------------------------------------------------------------------------------


class Assertion(Model):
    id: str
    subject_kind: SubjectKind = "item"
    subject_label: str | None = None
    subject: SubjectRef | None = None
    property_raw: str
    value_raw: str
    unit_raw: str | None = None
    value: Value
    operator: Operator = "eq"
    polarity: Polarity = "positive"
    origin: Origin
    extractor: Extractor
    span: Span | None = None
    evidence: list[Evidence] = []
    status: Literal["supported", "ambiguous", "rejected"] = "supported"
    note_ru: str | None = None


class Binding(Model):
    assertion_id: str
    characteristic_ref: CharacteristicRef | None = None
    relation: Literal["exact", "generalized", "ambiguous", "none"]
    subject_match: Literal["yes", "no", "unknown"]
    matched_value_codes: list[str] = []
    matched_value_refs: list[AllowedValueRef] = []
    alternatives: list[CharacteristicRef] = []
    rule: str
    evidence: list[Evidence] = []


class ConflictGroup(Model):
    id: str
    subject: SubjectRef | None = None
    property_raw: str
    assertion_ids: list[str]


class CompatResult(Model):
    position_ref: PositionRef
    characteristic_ref: CharacteristicRef | None = None
    char_name: str | None = None
    assertion_ids: list[str] = []
    result: Literal["compatible", "contradicts", "unknown"]
    rule: str = "unspecified"
    evidence: list[Evidence] = []
    reason_ru: str


class Candidate(Model):
    ref: PositionRef
    name: str
    is_template: bool
    status: Literal["alive", "excluded"]
    excluded_by: list[CompatResult] = []
    supporting: list[CompatResult] = []
    missing_required: list[str] = []
    retrieval_score: float | None = None


class FamilySummary(Model):
    id: str
    name: str
    template_ref: PositionRef | None = None
    member_count: int
    membership_complete: bool = True
    score: float
    confirmed_by_text: bool = False


class SearchScope(Model):
    snapshot_id: str
    eligible_count: int = Field(ge=0)
    examined_count: int = Field(ge=0)
    retained_count: int = Field(ge=0)
    family_ids: list[str]
    family_membership_complete: bool
    retrieval_truncated: bool
    coverage_status: Literal["unconfirmed", "family_confirmed", "scope_exhaustive"]
    reason_ru: str
    truncation_reasons: list[str] = []
    confirmation_evidence: list[Evidence] = []


class FunnelScope(Model):
    id: str = Field(min_length=1)
    snapshot_id: str
    kind: Literal["catalog", "retrieved_families", "family"]
    family_ids: list[str] = []
    count: int = Field(ge=0)
    membership_complete: bool


class FunnelStage(Model):
    label_ru: str
    count: int = Field(ge=0)
    trigger: Literal["retrieval", "assertion", "answer", "edit"]
    scope_id: str = Field(min_length=1)
    evidence: list[Evidence] = []


class Decision(Model):
    position_ref: PositionRef
    name: str
    basis_ru: list[str]
    conditional_on_family: bool
    scope: SearchScope | None = None


class SetFieldPatch(Model):
    type: Literal["set_field"] = "set_field"
    label_ru: str = Field(min_length=1, max_length=200)
    value: str = Field(max_length=500)
    field_id: str | None = None


class ReplaceTextPatch(Model):
    type: Literal["replace_text"] = "replace_text"
    text: str = Field(min_length=1, max_length=MAX_TEXT)


class ReplaceSpanPatch(Model):
    type: Literal["replace_span"] = "replace_span"
    span: Span
    replacement: str = Field(max_length=MAX_TEXT)
    snapshot_id: str
    base_revision: int = Field(ge=0)


class RemoveFieldPatch(Model):
    """The value of a card field becomes unknown again. Removal is not a negative fact."""

    type: Literal["remove_field"] = "remove_field"
    field_id: str = Field(min_length=1)


class MoveAssertionPatch(Model):
    """The user states which object a reading belongs to («1,8 кг — масса кейса»)."""

    type: Literal["move_assertion"] = "move_assertion"
    assertion_id: str = Field(min_length=1)
    subject_kind: Literal["item", "part", "package", "set"]
    subject_label: str = Field(min_length=1, max_length=100)


class AssertionModalityPatch(Model):
    """The user weakens or restates how sure a reading is; unset fields keep the current reading."""

    type: Literal["set_modality"] = "set_modality"
    assertion_id: str = Field(min_length=1)
    status: Literal["supported", "ambiguous"] | None = None
    polarity: Polarity | None = None
    operator: Operator | None = None
    value: Value | None = None

    @model_validator(mode="after")
    def changes_something(self):
        if all(item is None for item in (self.status, self.polarity, self.operator, self.value)):
            raise ValueError("Modality edit changes nothing")
        if isinstance(self.value, UnparsedValue):
            raise ValueError("Modality edit needs a typed value")
        return self


class RenameSubjectPatch(Model):
    """The user names the object differently («это табурет»); a renamed item is searched again."""

    type: Literal["rename_subject"] = "rename_subject"
    subject_id: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=100)


EditPatch = Annotated[SetFieldPatch | ReplaceTextPatch | ReplaceSpanPatch | RemoveFieldPatch | MoveAssertionPatch
                      | AssertionModalityPatch | RenameSubjectPatch, Field(discriminator="type")]


class Fix(Model):
    id: str
    label_ru: str
    patch: EditPatch
    snapshot_id: str | None = None
    base_revision: int | None = None


class Diagnostic(Model):
    id: str
    kind: Literal[
        "typo",
        "layout",
        "unit_suspect",
        "value_not_allowed",
        "contradiction",
        "implausible",
        "missing_required",
        "out_of_scope",
        "schema_defect",
        "ambiguous",
        "model_unavailable",
    ]
    severity: Literal["blocker", "warning", "info"]
    assertion_ids: list[str] = []
    char_names: list[str] = []
    subject: SubjectRef | None = None
    characteristic_refs: list[CharacteristicRef] = []
    evidence: list[Evidence] = []
    message_ru: str
    reason_ru: str
    fixes: list[Fix] = []
    auto_applied: bool = False


class ClarificationOption(Model):
    id: str
    label_ru: str
    remaining_count: int
    is_dont_know: bool = False
    answer: Value | None = None
    effects: list[Binding] = []


class Clarification(Model):
    id: str
    kind: Literal["family", "characteristic", "conflict", "required"]
    base_revision: int
    snapshot_id: str
    question_ru: str
    why_ru: str
    char_name: str | None = None
    options: list[ClarificationOption]
    expected_remaining_worst: int
    accepts_value: bool = False
    value_unit_okei: str | None = None
    value_unit_name: str | None = None
    subject: SubjectRef | None = None


class CardField(Model):
    id: str
    label_ru: str
    source: Source
    characteristic_ref: CharacteristicRef | None = None
    value: Value | None = None
    display_ru: str
    assertion_ids: list[str] = []
    evidence_ru: str | None = None
    status: Literal["confirmed", "from_text", "from_catalog", "needs_answer", "conflict", "unparsed"]
    required: bool = False
    portal_category: str | None = None  # Portal category whose schema has this field (source == "portal")
    subject: SubjectRef | None = None
    evidence: list[Evidence] = []


class RejectedOutput(Model):
    """A model proposal that failed validation: kept for transparency, never used as a product fact."""

    raw: str
    reason_ru: str


class Correction(Model):
    kind: Literal["layout", "typo", "unit"]
    original: str
    replacement: str
    reason_ru: str
    original_span: Span | None = None
    transformed_span: MappedTextSpan | None = None
    target: Literal["normalized", "search", "text"] = "search"  # "text": applied to the accepted text itself


class Timings(Model):
    parse: float = 0
    retrieve: float = 0
    extract: float = 0
    resolve: float = 0
    total: float = 0


class ModelManifest(Model):
    model: str
    revision: str | None = None
    runtime: str = "openai-compatible"
    prompt_sha256: str
    parameters: dict[str, str | int | float | bool | None] = {}
    identity_verified: bool = False


class ExtractionResult(Model):
    assertions: list[Assertion] = []
    rejected_outputs: list[RejectedOutput] = []
    model_manifest: ModelManifest | None = None
    status: Literal["provisional", "analyzed", "partial"] = "provisional"
    timings: Timings = Timings()


@dataclass(frozen=True)
class RetrievalResult:
    """Runtime search boundary; full catalog members stay outside the HTTP schema."""

    families: list[FamilyHit]
    scope: SearchScope
    timings: Timings = field(default_factory=Timings)
    status: Literal["complete", "partial"] = "complete"
    normalized_text: str | None = None
    search_text: str | None = None
    correction_map: list[SourceMapSegment] = field(default_factory=list)
    corrections: list[Correction] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    model_manifests: list[ModelManifest] = field(default_factory=list)


class ResolutionResult(Model):
    candidates: list[Candidate] = []
    bindings: list[Binding] = []
    compatibility: list[CompatResult] = []
    conflicts: list[ConflictGroup] = []


class QuestionResult(Model):
    clarification: Clarification | None = None
    pending_options: dict[str, dict] = {}


class ReadinessResult(Model):
    readiness: Literal["ready", "needs_answers", "has_blockers", "no_code"]
    reasons_ru: list[str] = []


class DiagnosticsResult(Model):
    diagnostics: list[Diagnostic] = []


class CardState(Model):
    contract_version: str = CONTRACT_VERSION
    session_id: str
    revision: int
    request_id: str
    snapshot_id: str
    mode: Literal["fake", "real"] = "real"
    status: Literal["provisional", "analyzed", "partial"]
    text: str
    normalized_text: str | None = None
    search_text: str | None = None
    correction_map: list[SourceMapSegment] = []
    corrections: list[Correction] = []
    domain: str | None = None
    assertions: list[Assertion] = []
    subjects: list[SubjectRef] = []
    bindings: list[Binding] = []
    conflicts: list[ConflictGroup] = []
    family: FamilySummary | None = None
    families_top: list[FamilySummary] = []
    search_scope: SearchScope | None = None
    funnel: list[FunnelStage] = []
    funnel_scopes: list[FunnelScope] = []
    candidates: list[Candidate] = []
    suggestion: Decision | None = None
    final: Decision | None = None
    fields: list[CardField] = []
    diagnostics: list[Diagnostic] = []
    rejected_outputs: list[RejectedOutput] = []
    clarification: Clarification | None = None
    readiness: Literal["ready", "needs_answers", "has_blockers", "no_code"]
    readiness_reasons_ru: list[str] = []
    answers_count: int = 0
    can_undo: bool = False
    timings_ms: Timings = Timings()
    model_manifests: list[ModelManifest] = []

    @model_validator(mode="after")
    def consistent_references(self):
        validate_card(self)
        return self


# ---- requests -----------------------------------------------------------------------------------------------------

class SplitRequest(Model):
    text: str = Field(min_length=1, max_length=MAX_SPLIT_TEXT)


class ParseRequest(Model):
    session_id: str | None = None
    request_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=MAX_TEXT)


class AnalyzeRequest(Model):
    session_id: str
    request_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)


class AnswerRequest(Model):
    session_id: str
    request_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    question_id: str
    option_id: str | None = None
    value: Value | None = None

    @model_validator(mode="after")
    def one_answer(self):
        if (self.option_id is None) == (self.value is None):
            raise ValueError("Provide one option or typed value")
        return self


class EditRequest(Model):
    session_id: str
    request_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    patch: EditPatch


class UndoRequest(Model):
    session_id: str
    request_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)


class ErrorBody(Model):
    code: Literal["stale_revision", "session_expired", "invalid_input", "request_conflict", "unavailable", "not_found",
                  "analysis_busy"]
    message_ru: str
    retryable: bool
    current_revision: int | None = None
    request_id: str | None = None


class ErrorResponse(Model):
    error: ErrorBody


class Health(Model):
    contract_version: str
    snapshot_id: str | None
    snapshot_date: str | None
    counts: dict[str, int] = {}
    ready: bool
    llm: Literal["disabled", "available", "unavailable"]
    llm_concurrency: int | None = None  # Parallel /analyze calls the API admits (KTRU_LLM_CONCURRENCY)
    runtime_id: str | None = None
    inference_endpoint_id: str | None = None
    platform: str | None = None
    model_manifest: ModelManifest | None = None


class PositionCharacteristic(Model):
    char_code: str | None
    name: str
    required: bool
    kind_raw: str | None
    values_ru: list[str]
    ref: CharacteristicRef | None = None
    value_refs: list[AllowedValueRef] = []
    value_type: str = "unknown"
    defects: list[str] = []
    source: CatalogSource | None = None


class PositionDetail(Model):
    ref: PositionRef
    name: str
    okpd2_code: str | None
    okpd2_name: str | None
    is_template: bool
    status: str
    family_id: str
    supply_units: list[dict]
    characteristics: list[PositionCharacteristic]
    parent_ref: PositionRef | None = None
    rubricators: list[dict] = []
    application_date_start: str | None = None
    application_date_end: str | None = None
    source: CatalogSource | None = None


class Example(Model):
    """A real product listing (СТЕ) from the organizers' Portal export, used as live demo input."""

    ste_id: str
    domain: Literal["clothing", "furniture", "tools"]
    name: str
    text: str


class CatalogReader(Protocol):
    snapshot_id: str
    manifest: dict

    def search(self, text: str, limit_families: int = 8) -> SearchResult: ...
    def family(self, family_id: str) -> tuple[Position | None, list[Position]]: ...
    def position(self, code: str, version: int | None = None) -> Position | None: ...
    def correct_word(self, word: str) -> str | None: ...
    def portal_category(self, domain: str, family_name: str, text: str) -> str | None: ...
    def portal_chars(self, domain: str, category: str) -> list[PortalChar]: ...


class RuleExtractor(Protocol):
    def extract(self, text: str, text_revision: int) -> ExtractionResult: ...


class FactExtractor(Protocol):
    def extract(self, text: str, text_revision: int) -> ExtractionResult: ...


class Retriever(Protocol):
    def retrieve(self, text: str, snapshot_id: str) -> RetrievalResult: ...


class CardAssembler(Protocol):
    def resolve(self, catalog: CatalogReader, inputs: Inputs, session_id: str, request_id: str) -> Outcome: ...


class Resolver(Protocol):
    def resolve(self, catalog: CatalogReader, inputs: Inputs, family: FamilyHit | None, facts: BoundFacts,
                confirmed: bool, confirmation_reason: str, funnel: list[FunnelStage],
                diagnostics: list[Diagnostic]) -> ResolutionData: ...


class QuestionSelector(Protocol):
    def choose(self, catalog: CatalogReader, inputs: Inputs, family: FamilyHit | None, hits: list[FamilyHit],
               confirmed: bool, facts: BoundFacts, resolution: ResolutionData, fields: FieldResult) -> QuestionResult: ...


class Diagnostics(Protocol):
    def build(self, catalog: CatalogReader, inputs: Inputs, work: str, family: FamilyHit | None,
              facts: BoundFacts, resolution: ResolutionData, fields: FieldResult) -> DiagnosticsResult: ...


class Readiness(Protocol):
    def evaluate(self, final: Decision | None, alive: list[Position], diagnostics: list[Diagnostic],
                 fields: list[CardField]) -> ReadinessResult: ...


class SessionStore(Protocol):
    def create(self, snapshot_id: str) -> Session: ...
    def read(self, session_id: str) -> Session: ...
    def get_or_create(self, request_id: str, snapshot_id: str) -> Session: ...
    def replay(self, session: Session, request_id: str, payload: dict,
               expected_revision: int) -> CardState | None: ...
    def cas(self, session: Session, request_id: str, payload: dict, expected_revision: int,
            transition: Callable[[Session], CardState]) -> CardState: ...


class Pipeline(Protocol):
    def parse(self, request: ParseRequest) -> CardState: ...
    def analyze(self, request: AnalyzeRequest) -> CardState: ...
    def answer(self, request: AnswerRequest) -> CardState: ...
    def edit(self, request: EditRequest) -> CardState: ...
    def undo(self, request: UndoRequest) -> CardState: ...
    def get(self, session_id: str) -> CardState: ...
