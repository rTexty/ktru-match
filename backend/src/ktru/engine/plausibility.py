"""Absurd or unusual numbers: «молоток 50 литров», «куртка 300 кг», «дрель 5000 кг».

Evidence comes from the snapshot: real Portal listings of the same product noun (the `plausibility` table) and the
characteristics of the selected KTRU family. Two rules, both warnings only (CONSTRAINTS: unusual ≠ impossible):

- a dimension the product never states (volume of a hammer, voltage of a jacket) while it has many listings and
  no characteristic of the family uses it;
- a value far above what listings state (above 3× their 95th and 1.5× their 99th percentile).

Small values are not judged: listings often carry ×1000 unit slips («Вес 0.0015 кг» for a 1.5 kg drill)."""

from __future__ import annotations

from decimal import Decimal

from src.ktru import contracts as C
from src.ktru.catalog.reader import Catalog, FamilyHit
from src.ktru.engine import canonical
from src.ktru.engine.compatibility import diagnostic_context
from src.ktru.engine.identity import _id
from src.ktru.engine.text import OKEI, content_stems, dec, fmt, head_noun_stem, to_base

DIM_RU = {"volume": "объём", "voltage": "напряжение", "power": "мощность", "mass": "масса", "length": "размер"}
PROPS = (("масс", "масса"), ("вес", "масса"), ("длин", "длина"), ("ширин", "ширина"), ("высот", "высота"),
         ("глубин", "глубина"), ("диаметр", "диаметр"), ("толщин", "толщина"), ("объем", "объем"),
         ("емкост", "объем"), ("мощност", "мощность"), ("напряжен", "напряжение"))
ALWAYS_PRESENT = ("length", "mass")  # every physical product has a size and a weight
MIN_LISTINGS, MIN_VALUES, MIN_GROUP_VALUES = 20, 15, 30
_READABLE = {"mass": [("т", 10**6), ("кг", 1000), ("г", 1)], "length": [("м", 1000), ("мм", 1)],
             "volume": [("л", 1000), ("мл", 1)], "power": [("кВт", 1000), ("Вт", 1)], "voltage": [("В", 1)]}


def _human(x: Decimal, dim: str) -> str:
    """Base units back to a readable unit: 31000 g → «31 кг», 2400 mm → «2,4 м»."""
    for unit, factor in _READABLE.get(dim, []):
        if x >= factor or factor == 1:
            return f"{fmt((x / factor).quantize(Decimal('0.01')).normalize())} {unit}"
    return fmt(x)


def _ceiling(row: tuple) -> Decimal:
    """Largest unsurprising value: 3× the 95th percentile, or 1.5× the 99th with enough listings to trust it
    (one «куртка 228 т» typo must not hide «куртка 300 кг»)."""
    n, _q01, _q05, _q50, q95, q99 = row
    limit = Decimal(str(q95)) * 3
    return max(limit, Decimal(str(q99)) * Decimal("1.5")) if n >= 100 else limit


def _prop(property_raw: str) -> str | None:
    low = " ".join(content_stems(property_raw))
    return next((p for stem_, p in PROPS if stem_ in low), None)


def _family_dims(family: FamilyHit | None) -> set[str]:
    dims = set()
    for member in family.members if family else []:
        for ch in member.chars:
            for v in ch.allowed:
                if v.get("okei_code") in OKEI:
                    dims.add(OKEI[v["okei_code"]][0])
    return dims


def plausibility_diagnostics(cat: Catalog, text: str, family: FamilyHit | None, assertions: list[C.Assertion],
                             rev: int) -> list[C.Diagnostic]:
    norms_of = getattr(cat, "plausibility", None)
    if norms_of is None:
        return []
    heads = cat.text_heads(text)
    # The noun as written («ботинки») has its own listings even when search maps it to a class («Обувь …»).
    written = head_noun_stem(canonical.product_label(text) or text)
    options = [h for h in (written, heads[0][0] if heads else None, head_noun_stem(family.name) if family else None) if h]
    head = next((h for h in options if norms_of(h).get(("#", ""), (0,))[0] >= MIN_LISTINGS), options[0] if options else None)
    if not head:
        return []
    domain = (family.members[0].domain if family and family.members else None) or (heads[0][1] if heads else None)
    norms, group = norms_of(head), norms_of(f"@{domain}") if domain and domain != "unknown" else {}
    listings = norms.get(("#", ""), (0,))[0]
    label = canonical.product_label(text, family.name if family else None) or head
    dims_of_family = _family_dims(family)
    out: list[C.Diagnostic] = []
    # One warning per stated number, whichever reading carries it: «Молоток 1500 кг» is read both as the hammer's
    # mass and as an unnamed value; both readings end where the number does.
    quoted: set[int] = set()
    for a in assertions:
        value = a.value
        if not isinstance(value, C.NumberValue | C.RangeValue) or not value.unit_okei or value.unit_okei not in OKEI:
            continue
        if a.subject is not None and a.subject.kind in ("package", "part", "set"):
            continue
        x = dec(value.x if isinstance(value, C.NumberValue) else (value.lo or value.hi))
        if x is None or x <= 0 or (a.span and a.span.end in quoted):
            continue
        base, dim = to_base(x, value.unit_okei)
        if dim not in DIM_RU:
            continue
        context = diagnostic_context(cat, family.members if family else [], [a])
        if dim not in ALWAYS_PRESENT and dim not in dims_of_family and listings >= MIN_LISTINGS:
            seen = norms.get(("#", dim), (0,))[0]
            if seen <= max(2, listings * 0.003):
                out.append(C.Diagnostic(
                    id=_id("plaus-dim", a.id, rev), kind="implausible", severity="warning", assertion_ids=[a.id],
                    **context,
                    message_ru=f"Нехарактерная величина для товара «{label}»: {DIM_RU[dim]} ({a.value_raw})",
                    reason_ru=f"{DIM_RU[dim].capitalize()} указан(а) лишь в {seen} из {listings} предложений Портала "
                              f"с этим товаром и не входит в характеристики найденного семейства КТРУ. Проверьте "
                              "единицу измерения или название товара. Значение сохранено как есть."))
                quoted.add(a.span.end if a.span else -1)
                continue
        # The most lenient of the property's and the dimension's norms: «Напряжение 220 В» of a mains drill is
        # normal even when most listings are cordless drills with «Напряжение аккумулятора 18 В».
        prop = _prop(a.property_raw) or "*"
        candidates = [norms[key] for key in ((prop, dim), ("*", dim)) if key in norms and norms[key][0] >= MIN_VALUES]
        source = "этого товара"
        if not candidates:
            candidates = [group[key] for key in ((prop, dim), ("*", dim))
                          if key in group and group[key][0] >= MIN_GROUP_VALUES]
            source = "этой группы товаров"
        if not candidates:
            continue
        n, _q01, _q05, _q50, q95, q99 = max(candidates, key=_ceiling)
        limit = _ceiling((n, _q01, _q05, _q50, q95, q99))
        if base > limit:
            out.append(C.Diagnostic(
                id=_id("plaus-big", a.id, rev), kind="implausible", severity="warning", assertion_ids=[a.id],
                **context,
                message_ru=f"Необычно большое значение для «{label}»: {a.property_raw} {a.value_raw}",
                reason_ru=f"В 95% из {n} предложений Портала {source} — не больше {_human(Decimal(str(q95)), dim)}, "
                          f"максимум около {_human(Decimal(str(q99)), dim)}. Возможна ошибка в единице измерения "
                          "или в товаре. Значение сохранено как есть."))
            quoted.add(a.span.end if a.span else -1)
    return out
