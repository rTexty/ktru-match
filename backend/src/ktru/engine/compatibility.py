from __future__ import annotations

from collections import defaultdict

from src.ktru import contracts as C
from src.ktru.catalog.reader import Catalog, FamilyHit, Position
from src.ktru.catalog.validation import catalog_values, value_defects
from src.ktru.engine import assertions as A
from src.ktru.engine.identity import _id
from src.ktru.engine.refs import _char_of, _cref, _pref
from src.ktru.engine.stage_types import BoundFacts, Inputs, ResolutionData
from src.ktru.engine.text import (
    UNITS,
    norm,
)
from src.ktru.engine.values import (
    _assertion_text,
    assertion_interval,
    char_okei,
    label_match,
    value_display,
    value_matches,
)


def diagnostic_context(cat: Catalog, members: list[Position], assertions: list[C.Assertion],
                       key: str | None = None) -> dict:
    """Retain every source; assign a subject only when all observations share it."""
    subjects = [assertion.subject for assertion in assertions]
    subject = subjects[-1] if subjects and subjects[-1] is not None and all(
        item is not None and item.id == subjects[-1].id and item.kind == subjects[-1].kind
        and item.label == subjects[-1].label and item.parent_subject_id == subjects[-1].parent_subject_id
        for item in subjects) else None
    evidence = [item for assertion in assertions for item in assertion.evidence]
    evidence.extend(item for scoped in subjects if scoped for item in scoped.evidence)
    refs = []
    for member in members:
        characteristic = _char_of(member, key) if key is not None else None
        if characteristic is None:
            continue
        ref = _cref(cat, member, characteristic)
        if ref is not None and ref not in refs:
            refs.append(ref)
        evidence.append(C.CatalogEvidence(position_ref=_pref(cat, member), characteristic_ref=ref))
    return {"subject": subject, "characteristic_refs": refs,
            "evidence": list({item.model_dump_json(): item for item in evidence}.values())}


def _constraint_evidence(cat: Catalog, members: list[Position], constraints: dict[str, list[C.Assertion]]) -> list:
    evidence = [item for key, assertions in constraints.items()
                for item in diagnostic_context(cat, members, assertions, key)["evidence"]]
    return list({item.model_dump_json(): item for item in evidence}.values())


def check(p: Position, key: str, assertions: list[C.Assertion]) -> tuple[str, list[dict]]:
    """compatible | contradicts | unknown for one candidate characteristic, plus matched allowed values."""
    ch = _char_of(p, key)
    if not catalog_values(ch):
        return "unknown", []
    okei = char_okei([ch])
    matched: list[dict] = []
    for a in assertions:
        if (a.status != "supported" or a.polarity == "uncertain" or a.operator == "approx"
                or not A.subject_matches(A.enrich(a), ch.name)):
            return "unknown", []
        comparable = [(v, r) for v in ch.allowed if (r := value_matches(a, v, okei)) is not None]
        if not comparable:
            return "unknown", []
        hits = [v for v, r in comparable if r]
        if a.polarity == "negative":
            # «не металл» contradicts only when every allowed value is the negated one.
            if len(hits) == len(comparable):
                return "contradicts", []
            continue
        if not hits:
            return "contradicts", []
        matched = [v for v in matched if v in hits] or hits
    return "compatible", matched



def _mutually_conflict(items: list[C.Assertion], okei: str | None, multi: bool = False) -> bool:
    """Incompatible text facts on one property. Multi-choice characteristics («состав: полиэфир, хлопок») hold
    several labels without conflict."""
    texts = [a for a in items if a.origin == "text"]
    for i, a in enumerate(texts):
        for b in texts[i + 1 :]:
            ia, ib = assertion_interval(a, okei), assertion_interval(b, okei)
            if ia and ib and not ia.overlaps(ib):
                return True
            if isinstance(a.value, C.EnumValue) and isinstance(b.value, C.EnumValue):
                same = label_match(a.value.label, b.value.label)
                if (not same and not multi and a.polarity == b.polarity == "positive") or (
                    same and a.polarity != b.polarity
                ):
                    return True
            if isinstance(a.value, C.BoolValue) and isinstance(b.value, C.BoolValue) and a.value.v != b.value.v:
                return True
    return False



def alive_after(members: list[Position], constraints: dict[str, list[C.Assertion]]) -> tuple[list[Position], dict[str, list]]:
    excl: dict[str, list] = defaultdict(list)
    for p in members:
        for key, items in constraints.items():
            if check(p, key, items)[0] == "contradicts":
                excl[p.code].append((key, items))
    return [p for p in members if p.code not in excl], excl



def resolve_family(
    cat: Catalog, inp: Inputs, family: FamilyHit, facts: BoundFacts,
    confirmed: bool, confirm_reason: str, funnel: list[C.FunnelStage], diags: list[C.Diagnostic],
) -> ResolutionData:
    rev, members = inp.revision, family.members
    fmap, display = facts.fmap, facts.display
    all_bound, by_char, user = facts.all_bound, facts.by_char, facts.user
    funnel, diags = list(funnel), list(diags)
    for member in members:
        for characteristic in member.chars:
            defects = [*characteristic.defects, *[defect for value in characteristic.allowed
                       for defect in value_defects(value)]]
            if characteristic.kind_raw not in ("1", "2", "3"):
                defects.append("unknown_kind")
            if characteristic.allowed and not catalog_values(characteristic) and not defects:
                defects.append("unverified_value_domain")
            if defects:
                diags.append(C.Diagnostic(id=_id("schema", member.code, member.version, characteristic.char_code),
                    kind="schema_defect", severity="warning", char_names=[characteristic.name],
                    characteristic_refs=[ref] if (ref := _cref(cat, member, characteristic)) else [],
                    evidence=[C.CatalogEvidence(position_ref=_pref(cat, member), characteristic_ref=ref)],
                    message_ru=f"Дефект каталога: «{characteristic.name}» в позиции {member.code}",
                    reason_ru="Свойство не используется для исключения: " + ", ".join(sorted(set(defects)))))
    # Conflicts suspend a constraint until the user resolves it.
    conflicts: dict[str, list[C.Assertion]] = {}
    for key, items in by_char.items():
        if _mutually_conflict(items, char_okei(fmap.get(key, [])), any(ch.multi for ch in fmap.get(key, []))):
            conflicts[key] = items
    unbound_nums: dict[tuple, list[C.Assertion]] = defaultdict(list)
    for b in all_bound:
        if b.char_name is None and isinstance(b.assertion.value, (C.NumberValue, C.RangeValue)):
            dim = UNITS[b.assertion.unit_raw][1] if b.assertion.unit_raw in UNITS else None
            unbound_nums[(b.assertion.subject_kind, b.assertion.subject_label, A.property_key(b.assertion), dim)].append(b.assertion)
    for items in unbound_nums.values():
        # Unbound numbers conflict only when both carry units (unitless «48-50 / 170-176» is size + height).
        if len(items) > 1 and all(a.unit_raw for a in items) and _mutually_conflict(items, None):
            conflicts["~" + norm(items[0].property_raw)] = items
    for key, items in conflicts.items():
        label = display.get(key, items[0].property_raw)
        diags.append(
            C.Diagnostic(
                id=_id("conf", key, rev),
                kind="contradiction",
                severity="blocker",
                assertion_ids=[a.id for a in items],
                **diagnostic_context(cat, members, items, key),
                char_names=[label],
                message_ru=f"Противоречие: «{label}» = " + " и ".join(f"«{a.value_raw}»" for a in items),
                reason_ru="Источники в тексте расходятся. Ни один не выбран автоматически; "
                "свойство не используется для отбора, пока вы не уточните.",
            )
        )

    active = {k: v for k, v in by_char.items() if k not in conflicts}
    # A numeric fact that contradicts every member is suspicious (unit error), not a reason to empty the family.
    implausible: set[str] = set()
    for key, items in list(active.items()):
        if any(isinstance(a.value, (C.NumberValue, C.RangeValue)) for a in items) and not alive_after(members, {key: items})[0]:
            implausible.add(key)
            active.pop(key)
    unresolved = {key for key, items in active.items()
        if all(check(member, key, items)[0] == "unknown" for member in members)}
    for key in unresolved:
        diags.append(C.Diagnostic(id=_id("invalid-value", key, rev), kind="value_not_allowed", severity="blocker",
            assertion_ids=[a.id for a in by_char[key]], char_names=[display.get(key, key)],
            **diagnostic_context(cat, members, by_char[key], key),
            message_ru=f"Значение «{display.get(key, key)}» не сопоставлено с каталогом",
            reason_ru="Исходное значение сохранено. Оно не исключает позиции; выберите допустимое значение."))
    text_only = {k: [a for a in v if a.origin == "text"] for k, v in active.items()}
    text_only = {k: v for k, v in text_only.items() if v}
    scope_id = _id("scope", cat.snapshot_id, "family", family.family_id)
    family_evidence = [C.CatalogEvidence(position_ref=_pref(cat, member)) for member in members]
    if inp.text:
        family_evidence.append(C.TextEvidence(span=C.Span(start=0, end=len(inp.text), text=inp.text,
                                                         text_revision=rev)))
    if confirmed:
        family_evidence.extend(C.UserEvidence(type="user_answer", revision=answer.get("revision", rev),
            question_id=answer.get("question_id"), option_id=answer.get("option_id"), answer_text=family.name)
            for answer in inp.answers if answer.get("kind") == "family"
            and answer.get("family_id") == family.family_id)
    funnel.append(C.FunnelStage(label_ru=f"Семейство «{family.name}»", count=len(members), trigger="retrieval",
                               scope_id=scope_id, evidence=family_evidence))
    if text_only:
        funnel.append(
            C.FunnelStage(label_ru="После свойств из текста", count=len(alive_after(members, text_only)[0]),
                          trigger="assertion", scope_id=scope_id,
                          evidence=_constraint_evidence(cat, members, text_only))
        )
    staged = dict(text_only)
    for b in user:
        if b.char_name and b.char_name in active:
            staged[b.char_name] = active[b.char_name]
            inferred = b.assertion.origin == "text"  # the model answered from the description's own words
            funnel.append(
                C.FunnelStage(
                    label_ru=f"{'Из описания' if inferred else 'Ответ'}: {display.get(b.char_name, b.char_name)} = "
                             f"{b.assertion.value_raw}",
                    count=len(alive_after(members, staged)[0]),
                    trigger="assertion" if inferred else "answer" if b.assertion.origin == "user_answer" else "edit",
                    scope_id=scope_id, evidence=_constraint_evidence(cat, members, staged),
                )
            )
    alive, excl = alive_after(members, active)

    candidates = []
    for p in members:
        excluded_by = []
        for key, items in excl.get(p.code, []):
            ch = _char_of(p, key)
            excluded_by.append(
                C.CompatResult(
                    position_ref=_pref(cat, p),
                    characteristic_ref=_cref(cat, p, ch) if ch else None,
                    char_name=display.get(key, key),
                    assertion_ids=[a.id for a in items],
                    result="contradicts",
                    rule="scoped_typed_value_contradiction",
                    evidence=[*[evidence for assertion in items for evidence in assertion.evidence],
                              C.CatalogEvidence(position_ref=_pref(cat, p),
                                                characteristic_ref=_cref(cat, p, ch) if ch else None)],
                    reason_ru=f"«{display.get(key, key)}»: указано {', '.join(_assertion_text(a) for a in items)}, "
                    f"в позиции — {', '.join(value_display(v) for v in (ch.allowed if ch else [])) or 'нет значений'}",
                )
            )
        supporting = []
        for key, items in active.items():
            if check(p, key, items)[0] == "compatible":
                ch = _char_of(p, key)
                supporting.append(
                    C.CompatResult(
                        position_ref=_pref(cat, p),
                        characteristic_ref=_cref(cat, p, ch) if ch else None,
                        char_name=display.get(key, key),
                        assertion_ids=[a.id for a in items],
                        result="compatible",
                        rule="scoped_typed_value_compatible",
                        evidence=[*[evidence for assertion in items for evidence in assertion.evidence],
                                  C.CatalogEvidence(position_ref=_pref(cat, p),
                                                    characteristic_ref=_cref(cat, p, ch) if ch else None)],
                        reason_ru=f"«{display.get(key, key)}»: {', '.join(_assertion_text(a) for a in items)}",
                    )
                )
        candidates.append(
            C.Candidate(
                ref=_pref(cat, p),
                name=p.name,
                is_template=False,
                status="excluded" if p.code in excl else "alive",
                excluded_by=excluded_by,
                supporting=supporting,
            )
        )

    final = suggestion = None
    if len(alive) == 1 and family.membership_complete:
        basis = [confirm_reason] if confirm_reason else []
        basis.append(
            f"Исключены по свойствам {len(members) - 1} из {len(members)} позиций семейства"
            if len(members) > 1
            else "В семействе одна конечная позиция"
        )
        basis.append(_actuality(alive[0]))
        decision = C.Decision(
            position_ref=_pref(cat, alive[0]), name=alive[0].name, basis_ru=basis, conditional_on_family=True
        )
        if confirmed:
            final = decision
        elif family.name_coverage >= 0.66:
            suggestion = decision
    elif alive and not confirmed and family.name_coverage >= 0.66:
        suggestion = C.Decision(
            position_ref=_pref(cat, alive[0]),
            name=alive[0].name,
            basis_ru=["Лучшее совпадение по тексту; семейство не подтверждено"],
            conditional_on_family=True,
        )

    bindings = [C.Binding(assertion_id=b.assertion.id,
        characteristic_ref=_cref(cat, p, ch), relation="exact" if b.assertion.status == "supported" else "ambiguous",
        subject_match="yes" if A.subject_matches(b.assertion, ch.name) else "unknown",
        matched_value_codes=[str(v["value_code"]) for v in check(p, b.char_name, [b.assertion])[1] if v.get("value_code")],
        matched_value_refs=[C.AllowedValueRef.model_validate(v["ref"])
                            for v in check(p, b.char_name, [b.assertion])[1] if v.get("ref")],
        rule="property_subject_and_typed_value", evidence=b.assertion.evidence)
        for b in all_bound if b.char_name for p in members if (ch := _char_of(p, b.char_name))]
    groups = [C.ConflictGroup(id=_id("conflict", key, rev), subject=diagnostic_context(cat, [], items)["subject"],
        property_raw=items[0].property_raw, assertion_ids=[a.id for a in items]) for key, items in conflicts.items()]
    result = C.ResolutionResult(candidates=candidates, bindings=bindings, conflicts=groups,
        compatibility=[item for candidate in candidates for item in [*candidate.excluded_by, *candidate.supporting]])
    return ResolutionData(result, alive, active, conflicts, implausible, unresolved, final, suggestion, funnel, diags)


def _actuality(p: Position) -> str:
    """Status, version and validity of the chosen position, with its required characteristics."""
    period = (f", применяется с {p.application_date_start[:10]}" if p.application_date_start else "") + (
        f" по {p.application_date_end[:10]}" if p.application_date_end else "")
    required = sum(1 for ch in p.chars if ch.required)
    status = "действующая (ACTIVE)" if p.status == "ACTIVE" else p.status
    return (f"Позиция {status}, версия {p.version}{period}; обязательных характеристик: {required}, "
            f"необязательных: {len(p.chars) - required}")
