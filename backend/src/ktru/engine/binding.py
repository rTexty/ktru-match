from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal

from src.ktru import contracts as C
from src.ktru.catalog.reader import Char, FamilyHit, Position
from src.ktru.engine import assertions as A
from src.ktru.engine import canonical
from src.ktru.engine import overrides as O
from src.ktru.engine.identity import _id
from src.ktru.engine.spelling import _morph, counted_phrase
from src.ktru.engine.stage_types import Bound, BoundFacts, Inputs
from src.ktru.engine.text import (
    COUNT_DIMENSIONS,
    STOP,
    _header_before,
    clauses,
    code_like,
    content_stems,
    fold,
    head_noun_stem,
    norm,
    quantities,
    stem,
    stems_match,
    word_has_label,
    words,
)
from src.ktru.engine.values import (
    ANTONYMS,
    GENERIC_CHAR_WORDS,
    NO,
    PACKAGE_WORDS,
    YES,
    _dim,
    char_okei,
    label_match,
    value_display,
)


def _span(text: str, s: int, e: int, rev: int) -> C.Span:
    return C.Span(start=s, end=e, text=text[s:e], text_revision=rev)



def _clause_of(text: str, pos: int) -> tuple[int, int]:
    for s, e in clauses(text):
        if s <= pos <= e:
            return s, e
    return 0, len(text)



_LEADING_NUMBER = re.compile(r"\s*(\d+(?:[.,]\d+)?)(?:\s*\(.*\))?\s*")
def _numbered(chs: list[Char]) -> dict[Decimal, list[str]] | None:
    """Enum labels that are all numbers («1», «3 (IV климатический пояс)») by their number; None otherwise."""
    labels = {v.get("label") or "" for ch in chs for v in ch.allowed if v.get("kind") == "enum"}
    heads = {label: _LEADING_NUMBER.fullmatch(label) for label in labels}
    if not labels or not all(heads.values()):
        return None
    out: dict[Decimal, list[str]] = defaultdict(list)
    for label, m in heads.items():
        out[Decimal(m.group(1).replace(",", "."))].append(label)
    return out


QUALIFIERS = ("максимальн", "минимальн", "номинальн", "количеств", "общ", "значени")


def _states_property(raw: str, family_name: str) -> bool:
    """«Молоток 1500 кг», «Нужно 1 000 стульев»: the product's own noun or a non-noun before a number names no
    property. Then the unit names it («масса»), or the value stays an unnamed «значение»."""
    first = next((w for w in re.findall(r"[А-Яа-яЁё]+", raw) if norm(w) not in STOP), None)
    if first is None:
        return False
    known = [p for p in _morph().parse(norm(first)) if p.is_known]
    if known and known[0].tag.POS not in ("NOUN", "ADJF", "PRTF"):
        return False
    return not (family_name and stems_match(stem(norm(first)), head_noun_stem(family_name) or ""))


def _parts(token: tuple) -> list[str]:
    """«крыльчато-флюгерный» carries both stems of the label «Крыльчато-флюгерный»."""
    pieces = token[3].split("-")
    return [token[0], *(stem(p) for p in pieces)] if len(pieces) > 1 else [token[0]]


def extract(
    text: str, rev: int, family_chars: dict[str, list[Char]], display: dict[str, str], family_name: str = ""
) -> list[Bound]:
    out: list[Bound] = []
    t_stems = [(stem(w), s, e, w) for w, s, e in words(text)]

    # 1. Quantities (numbers with units and operators).
    dims: dict[str, str | None] = {}
    for name, chs in family_chars.items():
        if any(v.get("kind") in ("number", "range") for ch in chs for v in ch.allowed):
            dims[name] = _dim(char_okei(chs))
    # «Класс одежды…: 3» names the catalog label «3» or «3 (IV климатический пояс)»: the number is that label.
    numbered = {name: by_number for name, chs in family_chars.items()
                if name not in dims and (by_number := _numbered(chs)) is not None}
    for q in quantities(text):
        prop_stems = content_stems(q.property_raw)
        is_package = any(s.startswith(PACKAGE_WORDS) for s in prop_stems)
        value: C.NumberValue | C.RangeValue
        if q.op == "between":
            value = C.RangeValue(lo=str(q.a), hi=str(q.b), unit_okei=q.unit_okei, unit_name=q.unit_raw,
                                 lo_inclusive=q.lo_inclusive, hi_inclusive=q.hi_inclusive)
        else:
            value = C.NumberValue(x=str(q.a), unit_okei=q.unit_okei, unit_name=q.unit_raw)
        stated = q.property_raw if q.property_raw and _states_property(q.property_raw, family_name) else None
        property_raw = (canonical.numeric_property_name(q.property_raw) or stated
                        or canonical.property_from_unit(q.dimension) or "значение")  # «19 кг» states a mass
        if A.subject_of(q.property_raw).kind == "item":
            property_raw = A.qualified(property_raw, q.property_raw, family_name)
        # «весит 1 литр»: a unit that does not fit the property is a doubt, as in the independent rule pass.
        doubt = q.doubt or canonical.dimension_doubt(property_raw, q.dimension)
        a = C.Assertion(
            id=_id("q", q.start, q.raw),
            subject_kind="package" if is_package else "item",
            property_raw=property_raw,
            value_raw=q.raw,
            unit_raw=q.unit_raw,
            value=value,
            operator=q.op,  # type: ignore[arg-type]
            origin="text",
            extractor="rules",
            span=_span(text, q.start, q.end, rev),
            # «1200х750х450 мм» without names: which size is the height is a reading, never a filter.
            status="ambiguous" if doubt else "supported",
            note_ru=doubt,
        )
        if is_package:
            out.append(Bound(a, None, "Относится к упаковке, а не к самому товару"))
            continue
        def score(prop_stems: list[str], q=q) -> list[tuple]:
            scored = []
            for name, dim in [*dims.items(), *((n, None) for n in numbered)]:
                if name in numbered and (q.dimension or q.op != "eq"):
                    continue
                if q.dimension and dim and dim != q.dimension:
                    continue
                if q.dimension is None and dim not in (None, "percent", *COUNT_DIMENSIONS):
                    continue  # a unitless number binds only to a unitless characteristic
                c_stems = content_stems(display[name])
                overlap = sum(1 for s in c_stems if any(stems_match(s, p) for p in prop_stems))
                # Tie-break: «Масса молотка» (the item itself) beats «Масса бойка» (a part).
                f_stems = content_stems(family_name)
                item = sum(1 for s in c_stems if any(stems_match(s, f) for f in f_stems))
                # A part named in the characteristic but absent from the text («головки») is not the subject.
                # Qualifiers («Максимальное количество фотометрических тестов в час») name no part when two words
                # already agree.
                extra = sum(1 for s in c_stems if not any(stems_match(s, x) for x in prop_stems + f_stems)
                            and not (overlap >= 2 and s.startswith(QUALIFIERS)))
                # ...and a part named in the text but absent from the characteristic («масса бойка») is not this one.
                extra_prop = sum(1 for x in prop_stems if not any(stems_match(x, y) for y in c_stems + f_stems))
                if overlap and (extra or extra_prop):
                    continue
                scored.append((overlap, item, name, dim))
            scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
            return scored

        def unique(scored: list[tuple]) -> bool:
            return bool(scored) and scored[0][0] > 0 and sum(1 for s in scored if s[:2] == scored[0][:2]) == 1

        scored = score(prop_stems)
        # «с 4 полками», «три выдвижных ящика»: the counted noun follows the number.
        if q.op == "eq" and q.dimension in (None, *COUNT_DIMENSIONS):
            following = re.findall(r"[а-яё]+", norm(re.split(r"[.,;:\n(\d]", text[q.end:], maxsplit=1)[0]))[:3]
            for n in range(1, len(following) + 1):
                counted = " ".join(following[:n])
                alt = score(content_stems("количество " + counted))
                if unique(alt) and alt[0][0] > 1 and (not unique(scored) or alt[0][0] > scored[0][0]):
                    scored = alt
                    a = a.model_copy(update={"property_raw": f"количество {counted_phrase(following[:n]) or counted}"})
                    break
            else:
                if property_raw == "значение" and (phrase := counted_phrase(following)):
                    a = a.model_copy(update={"property_raw": f"количество {phrase}"})
        top = [s for s in scored if scored and s[:2] == scored[0][:2]]
        if scored and scored[0][0] > 0 and len(top) == 1 and scored[0][2] in numbered:
            labels = numbered[scored[0][2]].get(q.a, [])
            if len(labels) == 1:
                a = a.model_copy(update={"value": C.EnumValue(label=labels[0]), "property_raw": display[scored[0][2]]})
            out.append(Bound(a, scored[0][2] if len(labels) == 1 else None))
        elif scored and scored[0][0] > 0 and len(top) == 1:
            out.append(Bound(a, scored[0][2]))
        elif q.dimension and not prop_stems and len(scored) == 1:
            out.append(Bound(a, scored[0][2], "Привязано по единице измерения"))
        else:
            out.append(Bound(a, None))

    # 2. Yes/no and enum characteristics by labels and names in the text.
    # The leading words that repeat the family name name the family, they do not tell its members apart
    # («Резцы для дорожной, горной и строительной техники» is not «Виды резцов: Дорожные, Горные…»).
    f_name_stems = content_stems(family_name)
    naming: set[int] = set()
    first_break = next((m.start() for m in re.finditer(r"[.;:\n]", text)), len(text))
    for i, (st, s, _e, w) in enumerate(t_stems):
        if s >= first_break:
            break  # «…(дрон). Тип: дрон самолетного типа»: the value after the name is not the name
        if norm(w) in STOP:
            continue
        if not any(stems_match(st, f) for f in f_name_stems):
            break
        naming.add(i)
    # Words of a property name before its colon are not values («Вид ткани: твил» does not say «Материал: ткань»).
    # The colon of a ratio («1:100») does not end a name.
    headers = [(m.start(), m.end()) for m in re.finditer(r"[^.;:\n]*(?:(?<=[^\W\d_]):|:(?!\d))", text)
               if re.search(r"[^\W\d_]", m.group())]
    occurrences: list[tuple[int, int, int]] = []  # every family label found in the text: (start, end, word count)
    for name, chs in family_chars.items():
        labels = {v["label"] for ch in chs for v in ch.allowed if v.get("kind") == "enum" and v.get("label")}
        if not labels:
            continue
        name_stems = [s for s in content_stems(display[name]) if s != "наличи"]
        if {norm(x) for x in labels} <= {YES, NO}:
            out.extend(_bind_bool(text, rev, name, display[name], name_stems, t_stems))
            continue
        specific = [x for x in name_stems if x not in GENERIC_CHAR_WORDS]
        matched: list[tuple[int, int, Bound]] = []  # (start, label length, binding)
        for label in sorted(labels):
            l_stems = content_stems(label)
            # «O-образная», «L2», «x86», «1:100», «Не более 250»: codes, digits and short parts match as the phrase.
            # A bare number («50») is a size or count: it binds only through its property name.
            exact = code_like(label)
            if not l_stems and not exact:
                continue
            # «Без поверки», «С поверкой»: the leading preposition is part of the value.
            lead = norm(label.split()[0]) if label.split() and not exact else ""
            lead = lead if lead in ("без", "не", "с", "со") else ""
            phrase = re.compile(r"(?<![\w-])" + re.escape(fold(label.strip())) + r"(?![\w-])") if exact else None
            candidates = []
            if phrase is not None:
                for m in phrase.finditer(fold(text)):
                    idx = next((i for i, x in enumerate(t_stems) if x[1] >= m.start()), len(t_stems))
                    candidates.append((idx, m.start(), m.end(), text[m.start():m.end()]))
            else:
                for idx, (st, s, e, w) in enumerate(t_stems):
                    if not word_has_label(st, l_stems[0]):
                        continue
                    if len(l_stems) > 1:
                        window = [x for x in t_stems if s <= x[1] <= e + 40]
                        if not all(any(stems_match(ls, p) for x in window for p in _parts(x)) for ls in l_stems):
                            continue
                        # The binding quotes the whole value («секция шкафа»), so «Шкаф» inside it is not another one.
                        e_label = max([e, *(x[2] for x in window
                                            if any(stems_match(ls, p) for ls in l_stems[1:] for p in _parts(x)))])
                    else:
                        e_label = e
                    candidates.append((idx, s, e_label, text[s:e_label]))
            occurrences.extend((s, e, len(l_stems)) for _, s, e, _ in candidates if _clause_of(text, s)[1] >= e)
            found = []
            for idx, s, e, w in candidates:
                if idx in naming or any(hs <= s < he for hs, he in headers):
                    continue
                if lead and (idx == 0 or norm(t_stems[idx - 1][3]) not in ({lead} | ({"с", "со"} if lead in ("с", "со") else set()))):
                    continue
                owners = {
                    n
                    for n, c2 in family_chars.items()
                    for ch in c2
                    for v in ch.allowed
                    if v.get("kind") == "enum" and label_match(v.get("label", ""), label)
                }
                cs, ce = _clause_of(text, s)
                # Split independent conjunctions before matching a catalog property's relation.
                local_start = cs
                local_end = ce
                for separator in re.finditer(r"\s+и\s+", text[cs:ce]):
                    if cs + separator.end() <= s:
                        local_start = cs + separator.end()
                    elif cs + separator.start() >= e:
                        local_end = cs + separator.start()
                        break
                if not A.property_relation_matches(display[name], text[local_start:local_end]):
                    continue
                near = [x[0] for x in t_stems[max(0, idx - 2) : idx + 3] if cs <= x[1] < ce]
                near += content_stems(_header_before(text, cs))  # «Материал верха: натуральная кожа»
                subject_here = bool(specific) and all(any(stems_match(x, c) for c in near) for x in specific)
                # A label shared by several characteristics (metal seat vs metal back) needs its subject nearby.
                if len(owners) > 1 and not subject_here:
                    continue
                found.append((idx, s, e, w))
            for idx, s, e, w in found:
                # «без подлокотников», «не металл», «без мягкого сиденья»: negation within two words before.
                before = [norm(x[3]) for x in t_stems[max(0, idx - 2) : idx] if _clause_of(text, x[1])[0] <= s]
                if lead:  # the negation word belongs to the value; only a second one negates it («не без …»)
                    before = before[:-1]
                neg = "не" in before or "без" in before
                matched.append((s, len(l_stems),
                    Bound(
                        C.Assertion(
                            id=_id("e", name, s, label),
                            property_raw=display[name],
                            value_raw=w,
                            value=C.EnumValue(label=label),
                            polarity="negative" if neg else "positive",
                            operator="eq",
                            origin="text",
                            extractor="rules",
                            span=_span(text, s, e, rev),
                        ),
                        name,
                    )
                ))
        # «сапоги с удлиненными голенищами» is that label, not also the shorter label «Сапоги» inside the same words.
        out.extend(b for s, n, b in matched if not any(
            s2 <= s and b.assertion.span.end <= b2.assertion.span.end and n2 > n for s2, n2, b2 in matched))
    # The same holds across characteristics: «искусственная кожа» (a label of «Материал подошвы») does not also say
    # «Кожа» of «Материал подкладки», even where the longer label is not bound for lack of its property name.
    out = [b for b in out if not (isinstance(b.assertion.value, C.EnumValue) and b.assertion.span and any(
        s2 <= b.assertion.span.start and b.assertion.span.end <= e2 and n2 > len(content_stems(b.assertion.value.label))
        for s2, e2, n2 in occurrences))]

    # 3. Colours with no catalog characteristic stay as new card fields.
    used = [(b.assertion.span.start, b.assertion.span.end) for b in out if b.assertion.span]
    if not any("цвет" in n for n in family_chars):
        for st, s, e, w in t_stems:
            if canonical.color(w) and not any(us <= s < ue for us, ue in used):
                out.append(
                    Bound(
                        C.Assertion(
                            id=_id("c", s),
                            property_raw="Цвет",
                            value_raw=w,
                            value=C.TextValue(s=w),
                            origin="text",
                            extractor="rules",
                            span=_span(text, s, e, rev),
                        ),
                        None,
                    )
                )
    return out



_BOOL_ANSWER = re.compile(r"(?:[^\S\r\n\v\f\x1c-\x1f\x85\u2028\u2029]*[:—–-][^\S\r\n\v\f\x1c-\x1f\x85\u2028\u2029]*|"
                          r"[^\S\r\n\v\f\x1c-\x1f\x85\u2028\u2029]+)(да|нет|есть|имеется|отсутствует)(?![а-яё])", re.IGNORECASE)


def _bind_bool(text, rev, name, display_name, name_stems, t_stems) -> list[Bound]:
    """«Наличие мягкого сидения»: each mention of the noun with its nearest modifier, antonym or negation.

    Every mention is returned, so «с подлокотниками и без подлокотников» becomes a visible conflict.
    """
    out: list[Bound] = []
    if not name_stems:
        return out
    noun, adjs = name_stems[-1], name_stems[:-1]
    for i, (st, s, e, _w) in enumerate(t_stems):
        if not stems_match(st, noun):
            continue
        cs, ce = _clause_of(text, s)

        def same_clause(j: int) -> bool:
            return 0 <= j < len(t_stems) and cs <= t_stems[j][1] < ce

        near = sorted((abs(j - i), j) for j in range(i - 2, i + 3) if j != i and same_clause(j))
        value: bool | None = None
        mod_j = None
        for _, j in near:
            w_st = t_stems[j][0]
            if w_st.startswith("отсутств") and j > i:
                value, mod_j = False, j
            elif adjs and stems_match(adjs[-1], w_st):
                value, mod_j = True, j
            elif adjs and any(w_st.startswith(ant) for a in adjs for ant in ANTONYMS.get(a, [])):
                value, mod_j = False, j
            if value is not None:
                break
        if value is None and not adjs:
            value = True
        # «без (мягкого) сиденья» negates the noun group; «не мягкое» negates the modifier.
        group_start = min(i, mod_j) if mod_j is not None and mod_j < i else i
        before = [norm(t_stems[j][3]) for j in range(group_start - 2, group_start) if same_clause(j)]
        if before[-2:] == ["не", "без"]:
            value, mod_j = True, (mod_j if mod_j is not None and mod_j < i else group_start - 1)  # «не без …»
        elif "без" in before:
            value, mod_j = False, (mod_j if mod_j is not None and mod_j < i else group_start - 1)
        elif value is not None and before[-1:] == ["не"]:
            value = not value
        if value is None:
            continue
        lo = min([s] + ([t_stems[mod_j][1]] if mod_j is not None and same_clause(mod_j) else []))
        hi = max([e] + ([t_stems[mod_j][2]] if mod_j is not None and same_clause(mod_j) else []))
        if "без" in before or before[-1:] == ["не"]:
            lo = min(lo, t_stems[group_start - (2 if before[0:1] == ["без"] and len(before) == 2 else 1)][1])
        # «натуральная кожа: нет», «мягкая спинка — да»: an explicit answer after the name decides the value.
        answer = _BOOL_ANSWER.match(text, hi)
        if answer:
            value, hi = answer.group(1).lower() in ("да", "есть", "имеется"), answer.end()
        out.append(
            Bound(
                C.Assertion(
                    id=_id("b", name, s),
                    property_raw=display_name,
                    value_raw=text[lo:hi],
                    value=C.BoolValue(v=value),
                    origin="text",
                    extractor="rules",
                    span=_span(text, lo, hi, rev),
                ),
                name,
            )
        )
    return out



def _bind_open(assertions: list[C.Assertion], fmap, display, family_name: str = "") -> list[Bound]:
    out = []
    f_stems = content_stems(family_name)
    for raw_assertion in assertions:
        assertion = raw_assertion
        names = A.matching_names(assertion, list(display.values()))
        key = norm(names[0]).strip() if len(names) == 1 else None
        value = assertion.value
        if key and isinstance(value, C.TextValue) and (v_stems := content_stems(value.s)) and all(
                any(stems_match(v, f) for f in f_stems) for v in v_stems):
            key = None  # «тип: шейкер-инкубатор» repeats the family name; it is not a value of «Тип»
        if (key and isinstance(value, C.NumberValue) and not value.unit_okei and assertion.operator == "eq"
                and (by_number := _numbered(fmap[key])) is not None):
            labels = by_number.get(Decimal(value.x), [])
            if len(labels) == 1:
                value = C.EnumValue(label=labels[0])
                assertion = assertion.model_copy(update={"value": value})
        if key and isinstance(value, (C.NumberValue, C.RangeValue)):
            expected_unit = char_okei(fmap[key])
            if (expected_unit and not value.unit_okei and _dim(expected_unit) not in COUNT_DIMENSIONS) or (
                expected_unit and value.unit_okei and _dim(expected_unit) != _dim(value.unit_okei)
            ):
                key = None
        if key and isinstance(value, C.TextValue):
            labels = {v["label"] for ch in fmap[key] for v in ch.allowed if v.get("kind") == "enum"}
            matched = [label for label in labels if label_match(value.s, label) or (
                canonical.property_class(assertion.property_raw) == "материал" and canonical.STAINLESS.fullmatch(value.s)
                and canonical.material(value.s) == canonical.material(label))]
            if len(matched) == 1:
                value = C.BoolValue(v=norm(matched[0]) == YES) if norm(matched[0]) in (YES, NO) else C.EnumValue(label=matched[0])
                assertion = assertion.model_copy(update={"value": value})
        out.append(Bound(assertion, key))
    return out



def _merge_bound(bound: list[Bound], fmap: dict[str, list[Char]] | None = None) -> list[Bound]:
    merged = A.merge([item.assertion for item in bound])
    out = []
    for assertion in merged:
        originals = [item for item in bound if A.semantic_key(A.enrich(item.assertion)) == A.semantic_key(assertion)]
        keys = {item.char_name for item in originals if item.char_name}
        if len(keys) > 1 and fmap and isinstance(assertion.value, C.EnumValue):
            # «тип: электронный» (model) and «Вид: Электронный» (catalog label): the characteristic that lists the
            # value is the one meant.
            listing = {k for k in keys if any(label_match(v.get("label", ""), assertion.value.label)
                                              for ch in fmap.get(k, []) for v in ch.allowed if v.get("kind") == "enum")}
            keys = listing if len(listing) == 1 else keys
        key = next(iter(keys)) if len(keys) == 1 else None
        kept = assertion
        if key and fmap and isinstance(assertion.value, C.EnumValue) and not any(
                label_match(v.get("label", ""), assertion.value.label)
                for ch in fmap.get(key, []) for v in ch.allowed if v.get("kind") == "enum"):
            # «с верхом из кожи» merged with «Кожа» of «Материал подкладки»: that label is not one of «Материал
            # верха», so the free-text reading stays.
            text_form = next((item.assertion for item in originals if isinstance(item.assertion.value, C.TextValue)),
                             None)
            if text_form is not None:
                kept = A.enrich(text_form).model_copy(
                    update={"evidence": assertion.evidence, "extractor": assertion.extractor})
        out.append(Bound(kept, key, next((item.note for item in originals if item.note), None)))
    return _drop_nested(out)


def _drop_nested(bound: list[Bound]) -> list[Bound]:
    """«искусственная кожа» (rules) and «кожа» (model) read from the same words for one characteristic: the longer
    value is meant, the shorter one is a part of its wording, not a second value. Free text «с верхом из кожи» next
    to the catalog value «Материал верха: натуральная кожа» says nothing more than that value."""
    def stems(b: Bound) -> set[str]:
        value = b.assertion.value
        if isinstance(value, C.EnumValue):
            return set(content_stems(value.label))
        return set(content_stems(value.s)) if isinstance(value, C.TextValue) else set()

    def implied(b: Bound, other: Bound) -> bool:
        a, o, mine = b.assertion, other.assertion, stems(b)
        if other is b or other.char_name != b.char_name or o.polarity != a.polarity:
            return False
        if not isinstance(o.value, C.EnumValue) or not mine:
            return False
        if isinstance(a.value, C.TextValue):
            return all(any(stems_match(m, t) for t in stems(other)) for m in mine)  # «трикотаж» ⊂ «Трикотажное полотно»
        return bool(a.span and o.span) and mine < stems(other) and o.span.start < a.span.end \
            and a.span.start < o.span.end and stems(other) <= set(content_stems(a.span.text))

    return [b for b in bound if not (b.char_name and any(implied(b, other) for other in bound))]



def family_char_map(members: list[Position]) -> tuple[dict[str, list[Char]], dict[str, str]]:
    m: dict[str, list[Char]] = defaultdict(list)
    display: dict[str, str] = {}
    for p in members:
        for ch in p.chars:
            k = norm(ch.name).strip()
            m[k].append(ch)
            display.setdefault(k, ch.name)
    return m, display



def _current_subject(subject: C.SubjectRef | None, inp: Inputs) -> C.SubjectRef | None:
    """Retained user subjects refer to the same unchanged original text."""
    if subject is None:
        return None
    evidence = []
    for item in subject.evidence:
        if isinstance(item, C.TextEvidence):
            span = item.span
            if span.end > len(inp.text) or inp.text[span.start:span.end] != span.text:
                raise ValueError("Retained subject evidence does not match the original text")
            if span.text_revision > inp.revision:
                raise ValueError("Retained subject evidence refers to a future revision")
            evidence.append(item.model_copy(update={"span": span.model_copy(update={"text_revision": inp.revision})}))
        else:
            evidence.append(item)
    return subject.model_copy(update={"evidence": evidence})


def _user_assertions(inp: Inputs, fmap: dict[str, list[Char]], display: dict[str, str]) -> tuple[list[Bound], set]:
    user: list[Bound] = []
    dont_know: set[str] = set()
    for i, ans in enumerate(inp.answers):
        if ans["kind"] == "dont_know":
            dont_know.add(ans["char"])
        elif ans["kind"] == "char":
            v = ans["value"]
            val: C.EnumValue | C.BoolValue | C.NumberValue | C.RangeValue
            if v.get("kind") == "enum":
                lab = v["label"]
                val = C.BoolValue(v=norm(lab) == YES) if norm(lab) in (YES, NO) else C.EnumValue(label=lab)
            elif v.get("kind") == "number":
                val = C.NumberValue(x=v["x"], unit_okei=v.get("okei_code"), unit_name=v.get("okei_name"))
            else:
                val = C.RangeValue(
                    lo=v.get("lo"), hi=v.get("hi"), lo_inclusive=v.get("lo_inclusive", True),
                    hi_inclusive=v.get("hi_inclusive", True), unit_okei=v.get("okei_code"),
                    unit_name=v.get("okei_name"),
                )  # fmt: skip
            a = C.Assertion(
                id=_id("ans", i, ans["char"]),
                property_raw=display.get(ans["char"], ans["char"]),
                value_raw=value_display(v),
                value=val,
                operator="between" if isinstance(val, C.RangeValue) else "eq",
                origin="user_answer",
                extractor="none",
                subject=C.SubjectRef.model_validate(ans["subject"]) if ans.get("subject") else None,
                unit_raw=val.unit_name if isinstance(val, (C.NumberValue, C.RangeValue)) else None,
                evidence=[C.UserEvidence(type="user_answer", revision=ans.get("revision", inp.revision),
                    question_id=ans.get("question_id"), option_id=ans.get("option_id"), answer_text=value_display(v))],
            )
            if inferred := ans.get("inferred"):
                # The model answered the question from the description's words: a reading of the text with its
                # quote, not something the user said.
                start, end = inferred["start"], inferred["end"]
                span = C.Span(start=start, end=end, text=inp.text[start:end], text_revision=inp.revision)
                a = a.model_copy(update={"id": _id("inf", i, ans["char"]), "origin": "text", "extractor": "llm",
                    "span": span, "evidence": [C.TextEvidence(span=span, extractor="llm")],
                    "note_ru": f"Модель вывела «{value_display(v)}» из слов «{span.text}»"})
            user.append(Bound(A.enrich(a), ans["char"] if ans["char"] in fmap else None))
        elif ans["kind"] == "conflict":
            a = C.Assertion.model_validate({**ans["assertion"], "id": _id("res", i), "origin": "user_answer"})
            a = a.model_copy(update={"evidence": [C.UserEvidence(type="user_answer",
                revision=ans.get("revision", inp.revision), question_id=ans.get("question_id"),
                answer_text=a.value_raw)], "span": None})
            user.append(Bound(A.enrich(a), ans["char"] if ans["char"] in fmap else None))
    for i, ed in enumerate(inp.edits):
        if ed.get("kind") in O.KINDS:
            continue  # Corrections of existing readings, applied by overrides.apply.
        key = norm(ed["label_ru"]).strip()
        q = quantities(ed["value"])
        val2: C.NumberValue | C.RangeValue | C.EnumValue | C.TextValue | C.BoolValue
        operator = "eq"
        if len(q) == 1 and not ed["value"][:q[0].start].strip() and not ed["value"][q[0].end:].strip():
            quantity = q[0]
            operator = quantity.op
            val2 = (C.RangeValue(lo=str(quantity.a), hi=str(quantity.b), unit_okei=quantity.unit_okei,
                                unit_name=quantity.unit_raw) if quantity.b is not None else
                    C.NumberValue(x=str(quantity.a), unit_okei=quantity.unit_okei, unit_name=quantity.unit_raw))
        elif key in fmap:
            labels = [v["label"] for ch in fmap[key] for v in ch.allowed if v.get("kind") == "enum"]
            lab = next((x for x in labels if norm(x) == norm(ed["value"]).strip()), None)
            val2 = C.EnumValue(label=lab) if lab else C.TextValue(s=ed["value"])
        else:
            val2 = C.TextValue(s=ed["value"])
        if isinstance(val2, C.TextValue) and norm(val2.s).strip() in (YES, NO):
            val2 = C.BoolValue(v=norm(val2.s).strip() == YES)  # An explicit «да»/«нет» is a fact in any field.
        if isinstance(val2, C.EnumValue) and norm(val2.label) in (YES, NO):
            val2 = C.BoolValue(v=norm(val2.label) == YES)
        a = C.Assertion(
            id=_id("edit", i, key),
            property_raw=ed["label_ru"],
            value_raw=ed["value"],
            value=val2,
            operator=operator,
            unit_raw=val2.unit_name if isinstance(val2, (C.NumberValue, C.RangeValue)) else None,
            origin="user_edit",
            extractor="none",
            # «Наличие спинки» is a characteristic of the product (KTRU naming), not of the back itself.
            subject=C.SubjectRef.model_validate(ed["subject"]) if ed.get("subject") else
                (A.subject_ref("item", None) if key.startswith("наличие") else None),
            evidence=[C.UserEvidence(type="user_edit", revision=ed.get("revision", inp.revision),
                field_id=ed.get("field_id"), answer_text=ed["value"])],
        )
        user.append(Bound(A.enrich(a), key if key in fmap else None))
    latest = {}
    for bound in user:
        assertion = bound.assertion.model_copy(update={"subject": _current_subject(bound.assertion.subject, inp)})
        latest[(bound.char_name or A.property_key(assertion), assertion.subject.id)] = Bound(
            assertion, bound.char_name, bound.note)
    return list(latest.values()), dont_know



def _overridden(bound: list[Bound], inp: Inputs, display: dict[str, str]) -> list[Bound]:
    """Replay the user's corrections; a moved or restated reading is bound to the characteristic again."""
    changed = O.apply([b.assertion for b in bound], inp.edits)
    out = []
    for b, assertion in zip(bound, changed, strict=True):
        key = b.char_name
        if assertion.subject != b.assertion.subject:
            names = A.matching_names(assertion, list(display.values()))
            key = norm(names[0]).strip() if len(names) == 1 else None
        out.append(Bound(assertion, key, b.note))
    return _merge_bound(out) if changed != [b.assertion for b in bound] else out


def bind_facts(inp: Inputs, work: str, family: FamilyHit, independent: list[C.Assertion]) -> BoundFacts:
    text, rev, members = inp.text, inp.revision, family.members
    fmap, display = family_char_map(members)
    schema_proposals = extract(work, rev, fmap, display, family.name)
    # Schema rules only add interpretations. Open assertions are never filtered by a family schema.
    grounded_rules = []
    for proposal in schema_proposals:
        assertion = proposal.assertion
        if assertion.span:
            start, end = _clause_of(work, assertion.span.start)
            focused = work[assertion.span.start:assertion.span.end]
            if isinstance(assertion.value, C.EnumValue) and label_match(focused, assertion.value.label):
                # The words are the catalog value itself («Тип изделия: ручка»), not a part the fact is about.
                clause = work[start:end]
                cut = assertion.span.start - start
                context = clause[:cut] + " " * len(focused) + clause[cut + len(focused):]
            else:
                context = focused if A.subject_of(focused).kind != "item" else work[start:end]
            original = assertion.span.model_copy(update={"text": text[assertion.span.start:assertion.span.end]})
            assertion = assertion.model_copy(update={"span": original})
            assertion = A.enrich(assertion, context)
            if isinstance(assertion.value, (C.NumberValue, C.RangeValue)) and A.numeric_negated(work, original.start):
                assertion = assertion.model_copy(update={"status": "ambiguous", "polarity": "negative"})
        else:
            assertion = A.enrich(assertion)
        assertion = A.assign(text, [A.contextualize(text, assertion)])[0]
        key = proposal.char_name if proposal.char_name and A.subject_matches(assertion, proposal.char_name) else None
        if key is None and assertion.subject and assertion.subject.kind == "part" and assertion.subject.label:
            if A.subject_of(assertion.property_raw).kind == "item":
                assertion = assertion.model_copy(update={
                    "property_raw": f"{assertion.property_raw} ({assertion.subject.label})"})
        grounded_rules.append(Bound(assertion, key, proposal.note))
    schema_proposals = grounded_rules
    extracted = _overridden(_merge_bound([*_bind_open(independent, fmap, display, family.name), *schema_proposals], fmap), inp,
                            display)
    user, dont_know = _user_assertions(inp, fmap, display)
    user = _overridden([Bound(A.enrich(b.assertion), b.char_name, b.note) for b in user], inp, display)

    # User answers and edits supersede text facts on the same characteristic.
    user_keys = {(b.char_name or "~" + A.property_key(b.assertion), b.assertion.subject.id) for b in user}
    superseded = [b for b in extracted if b.assertion.status != "rejected" and
        (b.char_name or "~" + A.property_key(b.assertion), b.assertion.subject.id) in user_keys]
    # A reading the user removed constrains nothing; it stays visible as rejected.
    all_bound = [b for b in extracted if b not in superseded and b.assertion.status != "rejected"] + [
        b for b in user if b.assertion.status != "rejected"]
    by_char: dict[str, list[C.Assertion]] = defaultdict(list)
    for b in all_bound:
        if b.char_name:
            by_char[b.char_name].append(b.assertion)

    return BoundFacts(fmap, display, extracted, user, all_bound, by_char, superseded, dont_know)
