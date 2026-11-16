from __future__ import annotations

from src.ktru import contracts as C
from src.ktru.catalog.reader import Catalog, FamilyHit
from src.ktru.catalog.validation import catalog_values
from src.ktru.engine import assertions as A
from src.ktru.engine.compatibility import check
from src.ktru.engine.identity import _id
from src.ktru.engine.portal import portalize
from src.ktru.engine.refs import _cref, _pref
from src.ktru.engine.stage_types import BoundFacts, FieldResult, Inputs, ResolutionData
from src.ktru.engine.text import (
    clauses,
    content_stems,
    norm,
    stems_match,
    words,
)
from src.ktru.engine.values import (
    _assertion_text,
    value_display,
)


def build_fields(cat: Catalog, inp: Inputs, work: str, family: FamilyHit,
                 facts: BoundFacts, resolution: ResolutionData) -> FieldResult:
    members = family.members
    display, by_char = facts.display, facts.by_char
    extracted, all_bound, user, dont_know = facts.extracted, facts.all_bound, facts.user, facts.dont_know
    final, suggestion, alive = resolution.final, resolution.suggestion, resolution.alive
    conflicts, implausible, unresolved = resolution.conflicts, resolution.implausible, resolution.unresolved
    # ---- card fields --------------------------------------------------------------------------------------------
    fields: list[C.CardField] = []
    chosen = final or suggestion
    # Fields from a position's own schema only when that position is the single survivor.
    pos = next((p for p in members if chosen and len(alive) == 1 and p.code == chosen.position_ref.code), None)
    covered: set[str] = set()
    amb_fields: dict[str, list[dict]] = {}
    if pos is not None:
        for ch in pos.chars:
            key = norm(ch.name).strip()
            allowed = catalog_values(ch)
            items = by_char.get(key, [])
            covered.add(key)
            cref = _cref(cat, pos, ch)
            base = {"id": _id("f", key), "label_ru": ch.name, "source": "ktru", "characteristic_ref": cref,
                    "required": ch.required, "assertion_ids": [a.id for a in items],
                    "evidence": [C.CatalogEvidence(position_ref=_pref(cat, pos), characteristic_ref=cref)]}  # fmt: skip
            ev = "; ".join(f"«{a.span.text}»" if a.span else f"ответ: {a.value_raw}" for a in items)
            if key in conflicts:
                fields.append(
                    C.CardField(
                        **base,
                        display_ru=" / ".join(a.value_raw for a in items),
                        evidence_ru="Противоречивые значения в тексте",
                        status="conflict",
                    )
                )
            elif not allowed:
                fields.append(C.CardField(
                    **base, display_ru=", ".join(a.value_raw for a in items) if items else "—",
                    evidence_ru="Значения каталога не проверены; автоматическое заполнение недоступно",
                    status="needs_answer",
                ))
            elif key in implausible or key in unresolved:
                fields.append(
                    C.CardField(
                        **base,
                        display_ru=", ".join(a.value_raw for a in items),
                        evidence_ru=f"{ev} — нет среди допустимых значений",
                        status="needs_answer",
                    )
                )
            elif items:
                _, matched = check(pos, key, items)
                user_given = any(a.origin != "text" for a in items)
                if len(matched) > 1 and ch.kind_raw != "1" and not user_given:
                    amb_fields[key] = matched
                    fields.append(C.CardField(
                        **base, display_ru=f"{', '.join(a.value_raw for a in items)} → одно из: "
                        + "; ".join(value_display(v) for v in matched),
                        evidence_ru=ev, status="needs_answer",
                    ))  # fmt: skip
                else:
                    shown = value_display(matched[0]) if matched else ", ".join(_assertion_text(a) for a in items)
                    raw = ", ".join(a.value_raw for a in items)
                    fields.append(C.CardField(
                        **base, value=items[0].value,
                        display_ru=shown if norm(shown) == norm(raw) else f"{shown} (из «{raw}»)",
                        evidence_ru=ev, status="confirmed" if user_given else "from_text",
                    ))  # fmt: skip
            elif len(allowed) == 1 and ch.kind_raw == "1":
                fields.append(
                    C.CardField(
                        **base,
                        display_ru=value_display(allowed[0]),
                        evidence_ru="Значение задано в позиции КТРУ",
                        status="from_catalog",
                    )
                )
            elif ch.required:
                fields.append(C.CardField(**base, display_ru="Не знаю" if key in dont_know else "—",
                                          evidence_ru="Обязательная характеристика: выберите значение",
                                          status="needs_answer"))  # fmt: skip
    # The proven catalog name covers its qualifiers too; source assertions keep their original wording.
    visible_ids = {assertion.id for assertion in A.visible_assertions([
        bound.assertion.model_copy(update={"property_raw": display[bound.char_name]})
        if bound.char_name else bound.assertion for bound in all_bound])}
    for b in all_bound:
        a = b.assertion
        if a.id not in visible_ids:
            continue
        if b.char_name and b.char_name in covered:
            continue
        ev = f"«{a.span.text}»" if a.span else ("ответ пользователя" if a.origin != "text" else None)
        if b.char_name and pos is None:
            fields.append(C.CardField(
                id=_id("f", b.char_name, a.id), label_ru=display.get(b.char_name, b.char_name), source="ktru",
                value=a.value, display_ru=_assertion_text(a), assertion_ids=[a.id], evidence_ru=ev,
                status="conflict" if b.char_name in conflicts else ("needs_answer" if b.char_name in unresolved
                    else ("confirmed" if a.origin != "text" else "from_text")),
            ))  # fmt: skip
        else:
            in_conflict = any(a in items for k, items in conflicts.items())
            label = a.property_raw[:1].upper() + a.property_raw[1:]
            if a.subject and a.subject.kind != "item" and a.subject.label:
                label = f"{label} ({a.subject.label})"
            note = b.note or ("Характеристики нет в выбранной позиции КТРУ" if b.char_name else None)
            fields.append(C.CardField(
                id=_id("f", "new", a.id), label_ru=label, source="new", value=a.value,
                display_ru=_assertion_text(a), assertion_ids=[a.id],
                evidence_ru="; ".join(x for x in (note, ev) if x),
                status="conflict" if in_conflict else ("unparsed" if isinstance(a.value, C.UnparsedValue)
                    else ("confirmed" if a.origin != "text" else "from_text")),
            ))  # fmt: skip
    # Nothing in the text is dropped: clauses without any recognised fact are kept as free fields.
    used = [(b.assertion.span.start, b.assertion.span.end) for b in extracted if b.assertion.span]
    fam_stems = content_stems(family.name)
    # Words that named a recognised fact («масса», «сиденье») are covered by that fact.
    fact_stems = [x for b in extracted for x in content_stems(b.assertion.property_raw)]
    for cs, ce in clauses(work):
        rest = [w for w, s, e in words(work[cs:ce]) if not any(us <= cs + s < ue for us, ue in used)]
        cst = content_stems(" ".join(rest))
        if not cst or all(any(stems_match(x, f) for f in fam_stems + fact_stems) for x in cst):
            continue
        has_fact = any(cs <= s < ce for s, _ in used)
        frag = (
            work[cs:ce].strip()
            if not has_fact
            else " ".join(
                w
                for w in rest
                if any(not any(stems_match(x, f) for f in fam_stems + fact_stems) for x in content_stems(w))
            )
        )
        fields.append(C.CardField(
            id=_id("f", "free", cs), label_ru="Дополнительно", source="new", value=C.TextValue(s=frag),
            display_ru=frag, evidence_ru="Фрагмент не сопоставлен с характеристиками КТРУ", status="unparsed",
        ))  # fmt: skip

    assertion_lookup = {b.assertion.id: b.assertion for b in all_bound}
    fields = [f.model_copy(update={"subject": assertion_lookup[f.assertion_ids[0]].subject,
        "evidence": [*f.evidence, *[item for ident in f.assertion_ids for item in assertion_lookup[ident].evidence]]})
        if f.assertion_ids else f for f in fields]
    fields = portalize(fields, cat, members[0].domain, family.name, work)
    user_ids = {bound.assertion.id for bound in user}
    edited_properties = {(A.property_key(bound.assertion), bound.assertion.subject.id) for bound in user}
    fields = [f for f in fields if not (f.subject and f.value and
        (A.property_key(C.Assertion(id="field", property_raw=f.label_ru, value_raw=f.display_ru,
            value=f.value, subject=f.subject, origin="text", extractor="rules")), f.subject.id) in edited_properties
        and not user_ids.intersection(f.assertion_ids))]

    return FieldResult(fields, pos, amb_fields)
