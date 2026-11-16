"""Cross-reference checks for published cards, independent of engine adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.ktru import contracts as C


def _span(card: C.CardState, span: C.Span) -> None:
    if span.text_revision != card.revision or span.end > len(card.text) or card.text[span.start:span.end] != span.text:
        raise ValueError("Source span does not match the original text and revision")


def _evidence(card: C.CardState, items: list[C.Evidence]) -> None:
    for item in items:
        if item.type == "text":
            _span(card, item.span)
        elif item.type == "catalog":
            position = item.position_ref
            characteristic = item.characteristic_ref
            if position.snapshot_id != card.snapshot_id:
                raise ValueError("Catalog evidence refers to another snapshot")
            if not any(candidate.ref == position for candidate in card.candidates) and not any(
                family.template_ref == position for family in [*card.families_top, *([card.family] if card.family else [])]):
                raise ValueError("Catalog evidence refers to an unknown position")
            if characteristic and (characteristic.snapshot_id != position.snapshot_id
                or characteristic.position_code != position.code or characteristic.position_version != position.version):
                raise ValueError("Characteristic evidence does not match its position")
            if item.value_ref and item.value_ref.characteristic_ref != characteristic:
                raise ValueError("Value evidence does not match its characteristic")
        elif item.revision > card.revision:
            raise ValueError("User evidence refers to a future revision")


def _maps(card: C.CardState) -> None:
    target = card.search_text if card.search_text is not None else card.text
    source_end = target_end = 0
    for segment in card.correction_map:
        if segment.original_start < source_end or segment.transformed_start < target_end:
            raise ValueError("Source map ranges overlap or are not ordered")
        if segment.original_end > len(card.text) or segment.transformed_end > len(target):
            raise ValueError("Source map range exceeds its text")
        if card.text[source_end:segment.original_start] != target[target_end:segment.transformed_start]:
            raise ValueError("Source map omits a changed range")
        if segment.kind == "layout" and (segment.original_end - segment.original_start
            != segment.transformed_end - segment.transformed_start):
            raise ValueError("Layout correction changed code-point length")
        source_end, target_end = segment.original_end, segment.transformed_end
    if card.text[source_end:] != target[target_end:]:
        raise ValueError("Source map omits a changed suffix")
    for correction in card.corrections:
        if correction.original_span:
            _span(card, correction.original_span)
        mapped = correction.transformed_span
        if mapped:
            corrected = {"normalized": card.normalized_text, "search": card.search_text,
                         "text": card.text}[correction.target]
            if corrected is None or mapped.end > len(corrected) or corrected[mapped.start:mapped.end] != mapped.text:
                raise ValueError("Correction span does not match its transformed text")
        if correction.original_span and correction.original_span.text != correction.original:
            raise ValueError("Correction source does not match its original span")
        if mapped and mapped.text != correction.replacement:
            raise ValueError("Correction replacement does not match its target span")


def _diagnostics(card: C.CardState) -> None:
    assertions = {assertion.id: assertion for assertion in card.assertions}
    subjects = {subject.id: subject for subject in card.subjects}
    for diagnostic in card.diagnostics:
        if any(ident not in assertions for ident in diagnostic.assertion_ids):
            raise ValueError("Diagnostic refers to an unknown assertion")
        for ident in diagnostic.assertion_ids:
            assertion = assertions[ident]
            if assertion.span:
                _span(card, assertion.span)
            _evidence(card, assertion.evidence)
            if assertion.subject:
                _evidence(card, assertion.subject.evidence)
        subject = diagnostic.subject
        if subject and (not _same_subject(subjects.get(subject.id), subject) or any(
            not _same_subject(assertions[ident].subject, subject) for ident in diagnostic.assertion_ids)):
            raise ValueError("Diagnostic subject does not match its assertions")
        if subject:
            _evidence(card, subject.evidence)
        _evidence(card, diagnostic.evidence)
        for ref in diagnostic.characteristic_refs:
            _characteristic(card, ref)
            if ref.snapshot_id != card.snapshot_id or not any(
                item.type == "catalog" and item.characteristic_ref == ref for item in diagnostic.evidence):
                raise ValueError("Diagnostic characteristic lacks matching catalog evidence")
        for fix in diagnostic.fixes:
            if fix.snapshot_id != card.snapshot_id or fix.base_revision != card.revision:
                raise ValueError("Fix does not match the card snapshot and revision")
            if fix.patch.type == "replace_span":
                _span(card, fix.patch.span)
                if fix.patch.snapshot_id != card.snapshot_id or fix.patch.base_revision != card.revision:
                    raise ValueError("Span fix does not match the card snapshot and revision")


def _same_subject(left: C.SubjectRef | None, right: C.SubjectRef) -> bool:
    return left is not None and (left.id, left.kind, left.label, left.parent_subject_id) == (
        right.id, right.kind, right.label, right.parent_subject_id)


def _assertions(card: C.CardState) -> set[str]:
    subjects = {subject.id: subject for subject in card.subjects}
    ids = {assertion.id for assertion in card.assertions}
    if len(ids) != len(card.assertions) or len(subjects) != len(card.subjects):
        raise ValueError("Assertion and subject IDs must be unique")
    for subject in card.subjects:
        _evidence(card, subject.evidence)
        if subject.parent_subject_id and subject.parent_subject_id not in subjects:
            raise ValueError("Subject refers to an unknown parent")
    for assertion in card.assertions:
        if assertion.span:
            _span(card, assertion.span)
        _evidence(card, assertion.evidence)
        if assertion.subject:
            _evidence(card, assertion.subject.evidence)
            if not _same_subject(subjects.get(assertion.subject.id), assertion.subject):
                raise ValueError("Assertion refers to an unknown subject")
    return ids


def _position(card: C.CardState, ref: C.PositionRef) -> None:
    if ref.snapshot_id != card.snapshot_id:
        raise ValueError("Position reference belongs to another snapshot")


def _characteristic(card: C.CardState, ref: C.CharacteristicRef | None) -> None:
    if ref and ref.snapshot_id != card.snapshot_id:
        raise ValueError("Characteristic reference belongs to another snapshot")
    if ref and not any((ref.position_code, ref.position_version) == (candidate.ref.code, candidate.ref.version)
                       for candidate in card.candidates):
        raise ValueError("Characteristic reference belongs to an unknown position")


def _references(card: C.CardState, ids: list[str], known: set[str]) -> None:
    if any(ident not in known for ident in ids):
        raise ValueError("Stage output refers to an unknown assertion")


def _fields_and_bindings(card: C.CardState, known: set[str]) -> None:
    subjects = {subject.id: subject for subject in card.subjects}
    for field in card.fields:
        _references(card, field.assertion_ids, known)
        _characteristic(card, field.characteristic_ref)
        _evidence(card, field.evidence)
        if field.subject:
            _evidence(card, field.subject.evidence)
            if not _same_subject(subjects.get(field.subject.id), field.subject):
                raise ValueError("Field refers to an unknown subject")
    for binding in card.bindings:
        _references(card, [binding.assertion_id], known)
        _characteristic(card, binding.characteristic_ref)
        _evidence(card, binding.evidence)
        for ref in binding.alternatives:
            _characteristic(card, ref)
        for ref in binding.matched_value_refs:
            _characteristic(card, ref.characteristic_ref)
            if binding.characteristic_ref and ref.characteristic_ref != binding.characteristic_ref:
                raise ValueError("Binding value does not match its characteristic")
    for conflict in card.conflicts:
        _references(card, conflict.assertion_ids, known)
        if conflict.subject and not _same_subject(subjects.get(conflict.subject.id), conflict.subject):
            raise ValueError("Conflict refers to an unknown subject")


def _candidates_and_decisions(card: C.CardState, known: set[str]) -> None:
    positions = set()
    for candidate in card.candidates:
        _position(card, candidate.ref)
        identity = (candidate.ref.code, candidate.ref.version)
        if identity in positions:
            raise ValueError("Candidate position references must be unique")
        positions.add(identity)
        for result in [*candidate.excluded_by, *candidate.supporting]:
            if result.position_ref != candidate.ref:
                raise ValueError("Compatibility result refers to another position")
            _references(card, result.assertion_ids, known)
            _characteristic(card, result.characteristic_ref)
            if result.characteristic_ref and (result.characteristic_ref.position_code,
                result.characteristic_ref.position_version) != (candidate.ref.code, candidate.ref.version):
                raise ValueError("Compatibility characteristic belongs to another position")
            _evidence(card, result.evidence)
    for decision in (card.final, card.suggestion):
        if decision:
            _position(card, decision.position_ref)
            if (decision.position_ref.code, decision.position_ref.version) not in positions:
                raise ValueError("Decision refers to an unknown candidate")
            candidate = next(item for item in card.candidates if item.ref == decision.position_ref)
            if candidate.status != "alive":
                raise ValueError("Decision refers to an excluded candidate")
            if decision.scope:
                _scope(card, decision.scope)
    for family in [*card.families_top, *([card.family] if card.family else [])]:
        if family.template_ref:
            _position(card, family.template_ref)
    if card.search_scope:
        _scope(card, card.search_scope)
    if card.final:
        final = card.final
        target = next(item for item in card.candidates if item.ref == final.position_ref)
        scope = final.scope
        if target.is_template or sum(item.status == "alive" for item in card.candidates) != 1:
            raise ValueError("Final decision requires one surviving non-template")
        if scope is None or not scope.family_membership_complete or scope.coverage_status == "unconfirmed":
            raise ValueError("Final decision requires a confirmed or exhaustive complete scope")


def _scope(card: C.CardState, scope: C.SearchScope) -> None:
    if scope.snapshot_id != card.snapshot_id:
        raise ValueError("Search scope belongs to another snapshot")
    _evidence(card, scope.confirmation_evidence)


def _funnel(card: C.CardState) -> None:
    scopes = {scope.id: scope for scope in card.funnel_scopes}
    if len(scopes) != len(card.funnel_scopes):
        raise ValueError("Funnel scope IDs must be unique")
    if any(scope.snapshot_id != card.snapshot_id for scope in card.funnel_scopes):
        raise ValueError("Funnel scope refers to another snapshot")
    for stage in card.funnel:
        scope = scopes.get(stage.scope_id)
        if scope is None or stage.count > scope.count:
            raise ValueError("Funnel count does not match its declared universe")
        _evidence(card, stage.evidence)


def validate_card(card: C.CardState) -> None:
    known = _assertions(card)
    _fields_and_bindings(card, known)
    _candidates_and_decisions(card, known)
    _maps(card)
    _diagnostics(card)
    _funnel(card)


def validate_extraction(result: C.ExtractionResult, text: str, revision: int) -> None:
    """Fresh extractor sources must use their requested input; only accepted cached facts may be rebased."""
    for assertion in result.assertions:
        spans = [assertion.span] if assertion.span else []
        evidence = [*assertion.evidence, *(assertion.subject.evidence if assertion.subject else [])]
        spans.extend(item.span for item in evidence if item.type == "text")
        for span in spans:
            if span.text_revision != revision or span.end > len(text) or text[span.start:span.end] != span.text:
                raise ValueError("Fresh extraction source does not match the requested text and revision")
