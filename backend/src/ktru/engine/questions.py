from __future__ import annotations

from collections import defaultdict

from src.ktru import contracts as C
from src.ktru.catalog.reader import Catalog, FamilyHit, Position
from src.ktru.catalog.validation import catalog_values
from src.ktru.engine import assertions as A
from src.ktru.engine.binding import _user_assertions
from src.ktru.engine.compatibility import alive_after, check
from src.ktru.engine.identity import _id
from src.ktru.engine.refs import _char_of, _cref, _pref
from src.ktru.engine.stage_types import BoundFacts, FieldResult, Inputs, ResolutionData
from src.ktru.engine.text import (
    OKEI,
    norm,
)
from src.ktru.engine.values import (
    char_okei,
    value_display,
)


# At most two clarification questions per description (TenderHack jury: «ideally none, one or two is fine»).
# Past the budget the card stays as it is: open fields are filled in place and the candidates stay listed.
MAX_QUESTIONS = 2


def question_budget_spent(inp: Inputs) -> bool:
    # A question the model answered from the description was never shown: it does not use the budget.
    return sum(not answer.get("inferred") for answer in inp.answers) >= MAX_QUESTIONS


def choose_question(cat: Catalog, inp: Inputs, family: FamilyHit, hits: list[FamilyHit],
                    confirmed: bool, facts: BoundFacts, resolution: ResolutionData,
                    field_result: FieldResult) -> C.QuestionResult:
    if question_budget_spent(inp):
        return C.QuestionResult()
    text, rev, members = inp.text, inp.revision, family.members
    fmap, display, by_char, dont_know = facts.fmap, facts.display, facts.by_char, facts.dont_know
    active, alive, conflicts = resolution.active, resolution.alive, resolution.conflicts
    unresolved, implausible = resolution.unresolved, resolution.implausible
    fields, pos, amb_fields = field_result.fields, field_result.pos, field_result.amb_fields
    pending: dict[str, dict] = {}
    # ---- clarification: conflict → family → distinguishing characteristic → required field -----------------------
    def make_q(kind: str, key: str | None, question: str, why: str, opts: list[tuple]) -> C.Clarification:
        qid = _id("q", rev, kind, key, cat.snapshot_id)
        options = []
        for j, (label, estimated_remaining, payload, dk) in enumerate(opts):
            remaining = estimated_remaining
            oid = f"{qid}-{j}"
            pending[oid] = {"question_id": qid, **payload}
            answer = None
            effects = []
            if payload["kind"] in ("char", "conflict"):
                bound, _ = _user_assertions(Inputs(text, rev + 1, answers=[pending[oid]]), fmap, display)
                if bound:
                    proposal = bound[0].assertion
                    answer = proposal.value
                    if bound[0].char_name:
                        trial = {**active, bound[0].char_name: [proposal]}
                        remaining = len(alive_after(members, trial)[0])
                    effects = [C.Binding(assertion_id=_id("ans", len(inp.answers), payload["char"]),
                        characteristic_ref=_cref(cat, member, characteristic), relation="exact",
                        subject_match="yes" if A.subject_matches(proposal, characteristic.name) else "unknown",
                        matched_value_codes=[str(v["value_code"]) for v in check(member, key, [proposal])[1]
                            if v.get("value_code")], rule="explicit_user_option",
                        evidence=[C.UserEvidence(type="user_answer", revision=rev + 1, question_id=qid,
                            option_id=oid, answer_text=label), C.CatalogEvidence(position_ref=_pref(cat, member),
                                characteristic_ref=_cref(cat, member, characteristic))])
                        for member in members if (characteristic := _char_of(member, key))
                        and catalog_values(characteristic) and _cref(cat, member, characteristic) is not None]
            options.append(C.ClarificationOption(id=oid, label_ru=label, remaining_count=remaining,
                is_dont_know=dk, answer=answer, effects=effects))
        worst = max((o.remaining_count for o in options if not o.is_dont_know), default=0)
        usable_chars = [ch for ch in fmap.get(key or "", []) if catalog_values(ch)]
        value_unit = char_okei(usable_chars) or next((o.answer.unit_okei for o in options
            if isinstance(o.answer, (C.NumberValue, C.RangeValue)) and o.answer.unit_okei), None)
        value_unit_name = OKEI[value_unit][2] if value_unit in OKEI else None
        return C.Clarification(
            id=qid, kind=kind, base_revision=rev, snapshot_id=cat.snapshot_id, question_ru=question, why_ru=why,
            char_name=display.get(key or "", key.removeprefix("~") if key else None), options=options,
            expected_remaining_worst=worst,
            subject=conflicts[key][0].subject if key in conflicts else A.subject_of(display.get(key or "", key or "")),
            accepts_value=any(isinstance(o.answer, (C.NumberValue, C.RangeValue)) for o in options),
            value_unit_okei=value_unit,
            value_unit_name=value_unit_name or next((v.get("okei_name") for ch in usable_chars for v in catalog_values(ch)
                if v.get("okei_name")), None) or next((o.answer.unit_name for o in options
                if isinstance(o.answer, (C.NumberValue, C.RangeValue)) and o.answer.unit_name), None),
        )  # fmt: skip

    clar: C.Clarification | None = None
    if conflicts:
        key = next((k for k in conflicts if not k.startswith("~")), next(iter(conflicts)))
        items = conflicts[key]
        label = display.get(key, items[0].property_raw)
        opts = [
            (f"Верно: {a.value_raw}", len(alive), {"kind": "conflict", "char": key, "assertion": a.model_dump()}, False)
            for a in items
        ]
        clar = make_q("conflict", key, f"Какое значение «{label}» верное?",
                      "В описании указаны несовместимые значения.", opts)  # fmt: skip
    elif not confirmed:
        opts = [
            (f"{h.name} ({len(h.members)} поз.)", len(h.members), {"kind": "family", "family_id": h.family_id}, False)
            for h in hits[:5]
            if h.head_ok or h.semantic_score is not None
        ]
        if opts:
            clar = make_q("family", None, "Какой это товар?",
                          "Подтвердите группу товара. Поиск предлагает варианты, но не доказывает полноту выбора.", opts)  # fmt: skip
    elif len(alive) > 1:
        best = None
        for key, chs in fmap.items():
            broad = key in by_char and any(isinstance(a.value, C.RangeValue) for a in by_char[key])
            # A stated label that still splits the survivors was general («Общего назначения» vs «...укороченные»).
            general = key in by_char and any(isinstance(a.value, C.EnumValue) for a in by_char[key])
            if (key in by_char and key not in unresolved and not broad and not general) or key in dont_know \
                    or key in implausible:
                continue
            groups: dict[str, list[Position]] = defaultdict(list)
            value_of: dict[str, dict] = {}
            for p in alive:
                ch = _char_of(p, key)
                values = catalog_values(ch)
                if not values:
                    groups["?"].append(p)
                    continue
                for v in values:
                    sig = value_display(v)
                    groups[sig].append(p)
                    value_of[sig] = v
            real = {k: v for k, v in groups.items() if k != "?"}
            if len(real) < 2 or any(len(v) == len(alive) for v in real.values()):
                continue
            unknown = groups.get("?", [])
            worst = max(len(v) + len(unknown) for v in real.values())
            score = (worst, not any(c.required for c in chs), len(real))
            if best is None or score < best[0]:
                best = (score, key, real, value_of, unknown)
        if best:
            _, key, real, value_of, unknown = best
            opts = [
                (sig, len(ps) + len(unknown), {"kind": "char", "char": key, "value": value_of[sig]}, False)
                for sig, ps in sorted(real.items(), key=lambda kv: -len(kv[1]))
            ]
            worst = max(o[1] for o in opts)
            opts.append(("Не знаю", len(alive), {"kind": "dont_know", "char": key}, True))
            clar = make_q("characteristic", key, f"{display[key]}?", "Выберите значение свойства.", opts)
            worst = clar.expected_remaining_worst
            why = (f"После ответа останется до {worst} из {len(alive)} подходящих позиций."
                   if worst < len(alive) else "Диапазоны могут пересекаться. Укажите точное значение, если оно известно.")
            clar = clar.model_copy(update={"why_ru": why})
    if clar is None and pos is not None:
        for f in fields:
            if f.status != "needs_answer" or not f.characteristic_ref:
                continue
            key = norm(f.label_ru).strip()
            if key in dont_know:
                continue
            ch = _char_of(pos, key)
            allowed = catalog_values(ch)
            values = [value for value in (amb_fields.get(key) or allowed) if value in allowed]
            if not values or len(values) > 15:
                continue
            opts = [(value_display(v), 1, {"kind": "char", "char": key, "value": v}, False) for v in values]
            opts.append(("Не знаю", 1, {"kind": "dont_know", "char": key}, True))
            clar = make_q("required", key, f"{f.label_ru}?",
                          "Нужно для заполнения карточки по выбранной позиции КТРУ.", opts)  # fmt: skip
            break

    return C.QuestionResult(clarification=clar, pending_options=pending)
