from __future__ import annotations

import re
from decimal import Decimal

from src.ktru import contracts as C
from src.ktru.catalog.reader import Catalog, FamilyHit, Position
from src.ktru.catalog.validation import catalog_values
from src.ktru.engine.compatibility import alive_after, diagnostic_context
from src.ktru.engine.identity import _id
from src.ktru.engine.refs import _char_of, _cref, _pref
from src.ktru.engine.stage_types import BoundFacts, FieldResult, Inputs, ResolutionData
from src.ktru.engine.text import (
    UNITS,
    content_stems,
    dec,
    fmt,
    norm,
    quantities,
    to_base,
)
from src.ktru.engine.values import (
    PLAUSIBLE_MAX_MM,
    value_display,
)


def build_diagnostics(cat: Catalog, inp: Inputs, work: str, family: FamilyHit,
                      facts: BoundFacts, resolution: ResolutionData, fields: FieldResult) -> list[C.Diagnostic]:
    rev, members = inp.revision, family.members
    by_char, display, all_bound = facts.by_char, facts.display, facts.all_bound
    implausible, amb_fields = resolution.implausible, fields.amb_fields
    diags = list(resolution.diagnostics)
    if fields.pos is not None:
        for characteristic in fields.pos.chars:
            if characteristic.required and not catalog_values(characteristic):
                diags.append(C.Diagnostic(
                    id=_id("quarantine", fields.pos.code, fields.pos.version, characteristic.char_code,
                           characteristic.name, rev),
                    kind="schema_defect", severity="blocker", char_names=[characteristic.name],
                    characteristic_refs=[ref] if (ref := _cref(cat, fields.pos, characteristic)) else [],
                    evidence=[C.CatalogEvidence(position_ref=_pref(cat, fields.pos), characteristic_ref=ref)],
                    message_ru=f"Обязательная характеристика «{characteristic.name}» не проверена",
                    reason_ru="Дефект каталога не позволяет предложить допустимые значения или заполнить поле. "
                              "Исходные данные сохранены; карточка не готова.",
                ))
    # ---- diagnostics ----------------------------------------------------------------------------------------------
    for key in implausible:
        items = by_char[key]
        fixes = []
        a0 = items[0]
        if a0.span and isinstance(a0.value, C.NumberValue) and a0.unit_raw in UNITS:
            x = dec(a0.value.x) or Decimal(0)
            dim = UNITS[a0.unit_raw][1]
            for unit in ("мм", "см", "дм", "м", "г", "кг"):
                if unit == a0.unit_raw or UNITS[unit][1] != dim:
                    continue
                trial = a0.model_copy(update={"value": C.NumberValue(x=a0.value.x, unit_okei=UNITS[unit][0])})
                if alive_after(members, {key: [trial]})[0]:
                    quantity = next((q for q in quantities(inp.text) if q.end == a0.span.end and q.a == x
                                     and q.unit_raw == a0.unit_raw), None)
                    if quantity is None:
                        continue
                    replacement = re.sub(rf"{re.escape(a0.unit_raw)}\.?\s*$", unit, quantity.raw)
                    span = C.Span(start=quantity.start, end=quantity.end, text=quantity.raw, text_revision=rev)
                    fixes.append(C.Fix(id=_id("fx", key, unit), label_ru=f"Заменить на «{fmt(x)} {unit}»",
                        snapshot_id=cat.snapshot_id, base_revision=rev,
                        patch=C.ReplaceSpanPatch(span=span, replacement=replacement,
                                                 snapshot_id=cat.snapshot_id, base_revision=rev)))
        allowed = sorted({value_display(v) for p in members if (ch := _char_of(p, key)) for v in ch.allowed})
        diags.append(
            C.Diagnostic(
                id=_id("impl", key, rev),
                kind="implausible",
                severity="warning",
                assertion_ids=[x.id for x in items],
                **diagnostic_context(cat, members, items, key),
                char_names=[display.get(key, key)],
                message_ru=f"Необычное значение: «{display.get(key, key)}» = {', '.join(x.value_raw for x in items)}",
                reason_ru="Значение не входит в допустимые ни для одной позиции семейства ("
                + "; ".join(allowed[:6])
                + "). Оно сохранено как есть и не используется для отбора.",
                fixes=fixes,
            )
        )
    for b in all_bound:
        a = b.assertion
        if b.char_name in implausible or not isinstance(a.value, C.NumberValue) or not a.value.unit_okei:
            continue
        base, dim = to_base(dec(a.value.x) or Decimal(0), a.value.unit_okei)
        for pref, mx in PLAUSIBLE_MAX_MM.items():
            if dim == "length" and base > mx and any(s.startswith(pref) for s in content_stems(a.property_raw)):
                diags.append(C.Diagnostic(
                    id=_id("pl", a.id), kind="implausible", severity="warning", assertion_ids=[a.id],
                    **diagnostic_context(cat, members, [a], b.char_name),
                    message_ru=f"Подозрительно большое значение: {a.property_raw} {a.value_raw}",
                    reason_ru=f"Правило: {pref}… обычно не больше {mx} мм. Значение не изменено.",
                ))  # fmt: skip
    for key, matched in amb_fields.items():
        diags.append(
            C.Diagnostic(
                id=_id("amb", key, rev),
                kind="ambiguous",
                severity="warning",
                assertion_ids=[assertion.id for assertion in by_char.get(key, [])],
                **diagnostic_context(cat, [fields.pos] if fields.pos else members, by_char.get(key, []), key),
                char_names=[display.get(key, key)],
                message_ru=f"«{display.get(key, key)}»: значение попадает в несколько вариантов каталога",
                reason_ru="Варианты: " + "; ".join(value_display(v) for v in matched) + ". Нужно выбрать один.",
            )
        )
    known = [*(inp.extraction.assertions if inp.extraction else []), *(b.assertion for b in facts.all_bound)]
    spans = {a.id: a.span for a in known if a.span}
    diags += foreign_properties(cat, family, fields.fields, inp.text, spans, rev)
    return diags


def _same_root(a: str, b: str) -> bool:
    """Stricter than stems_match: «фасон» must not pass as «фасовка» when deciding whether a property exists."""
    return a == b or (min(len(a), len(b)) >= 5 and (a.startswith(b) or b.startswith(a)) and abs(len(a) - len(b)) <= 2)


_NAME_BEFORE_COLON = re.compile(r"\s*:\s*\S")
# Commercial and physical properties any product may state, even when its KTRU schema does not list them.
GENERAL_PROPERTY_STEMS = ("гарант", "срок", "стран", "производител", "модел", "артикул", "бренд", "марк", "количеств",
                          "комплект", "упаковк", "цвет", "масс", "вес", "размер", "габарит", "длин", "ширин", "высот",
                          "глубин", "толщин", "диаметр", "объем", "материал", "тип", "назначени", "наличи", "сертифик",
                          "стандарт", "гост", "вид", "форм", "покрыт",
                          "значен")  # «Значение» names an unnamed number («3 ящика», «1200 мм»), not a property


def foreign_properties(cat: Catalog, family: FamilyHit, fields: list[C.CardField], text: str,
                       spans: dict[str, C.Span], rev: int) -> list[C.Diagnostic]:
    """A stated property that no KTRU position or Portal schema of the domain uses («тяга двигателя» for a table).

    Absence from this family's schema alone is not enough to accept an extra property: the property must at
    least exist somewhere in the product domain. The value is kept; the card warns that it may not belong."""
    stems_of = getattr(cat, "domain_property_stems", None)
    domain = family.members[0].domain if family.members else None
    if stems_of is None or not domain or domain == "unknown":
        return []
    vocabulary = stems_of(domain)
    out = []
    for field in fields:
        label = field.label_ru.split("(")[0].strip()
        if field.source != "new" or field.status not in ("from_text", "confirmed", "unparsed"):
            continue
        if norm(label) == "дополнительно":
            # An unparsed fragment directly before a colon names a property: «фасон воротника: стойка».
            span = spans.get(field.assertion_ids[0]) if len(field.assertion_ids) == 1 else None
            if span is None or len(span.text) > 40 or not _NAME_BEFORE_COLON.match(text, span.end):
                continue
            label = span.text.strip()
        stems = content_stems(label)
        if not stems or any(s.startswith(g) for s in stems for g in GENERAL_PROPERTY_STEMS):
            continue
        if any(_same_root(stems[0], v) for v in vocabulary):
            continue
        out.append(C.Diagnostic(
            id=_id("foreign", field.id, rev), kind="implausible", severity="warning",
            assertion_ids=list(field.assertion_ids), char_names=[label], subject=field.subject,
            evidence=list(field.evidence),
            message_ru=f"Свойство «{label}» не характерно для товара «{family.name}»",
            reason_ru="Такого свойства нет ни у одной позиции КТРУ этой товарной области и в схеме Портала. "
                      "Значение сохранено, но не используется для подбора. Проверьте, относится ли оно к товару.",
        ))
    return out


def evaluate_readiness(final: C.Decision | None, alive: list[Position],
                       diags: list[C.Diagnostic], fields: list[C.CardField]) -> C.ReadinessResult:
    # ---- readiness ----------------------------------------------------------------------------------------------
    reasons: list[str] = []
    blockers = [d for d in diags if d.severity == "blocker"]
    # Required fields, and stated values that could not be encoded unambiguously, both need an answer.
    missing = [
        f.label_ru for f in fields
        if (f.status in ("needs_answer", "conflict") and (f.required or f.assertion_ids))
        or (f.required and (f.status == "unparsed" or (f.source == "ktru" and f.characteristic_ref is None)))
    ]
    if final is None:
        readiness = "no_code" if not alive else "needs_answers"
        if not alive:
            reasons.append("Ни одна позиция семейства не подходит")
        elif len(alive) > 1:
            reasons.append(f"Подходят {len(alive)} позиций: нужны уточнения")
        else:
            reasons.append("Семейство не подтверждено: это предположение")
    elif blockers:
        readiness = "has_blockers"
    elif missing:
        readiness = "needs_answers"
    else:
        readiness = "ready"
    reasons += [d.message_ru for d in blockers]
    if final is not None and missing:
        reasons.append("Не заполнены обязательные характеристики: " + ", ".join(missing))
    if readiness == "ready":
        reasons.append("Код выбран, обязательные характеристики заполнены")

    return C.ReadinessResult(readiness=readiness, reasons_ru=reasons)
