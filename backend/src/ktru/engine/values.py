from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.ktru import contracts as C
from src.ktru.catalog.reader import Char
from src.ktru.catalog.validation import value_defects
from src.ktru.engine.text import (
    COUNT_DIMENSIONS,
    OKEI,
    code_like,
    content_stems,
    dec,
    fmt,
    fold,
    norm,
    stems_match,
    to_base,
)

YES, NO = "да", "нет"
ANTONYMS = {"мягк": ["жестк", "тверд"], "жестк": ["мягк"]}
GENERIC_CHAR_WORDS = {"тип", "вид", "наличи", "материал", "форм", "цвет", "назначени", "исполнени", "конструкци"}
COLOURS = [
    "черн", "бел", "сер", "красн", "син", "зелен", "желт", "коричнев", "бежев", "голуб", "розов", "оранжев",
    "фиолетов", "бордов", "серебрист",
]  # fmt: skip
PACKAGE_WORDS = ("упаков", "коробк", "короб", "тар")
PLAUSIBLE_MAX_MM = {"высот": 3000, "ширин": 6000, "длин": 10000, "глубин": 3000}


@dataclass
class Interval:
    lo: Decimal | None
    lo_inc: bool
    hi: Decimal | None
    hi_inc: bool
    dim: str | None

    def overlaps(self, o: Interval) -> bool:
        if self.dim and o.dim and self.dim != o.dim:
            return False
        lo, lo_inc = _max_lo(self, o)
        hi, hi_inc = _min_hi(self, o)
        if lo is None or hi is None:
            return True
        return lo < hi or (lo == hi and lo_inc and hi_inc)



def _max_lo(a: Interval, b: Interval) -> tuple[Decimal | None, bool]:
    if a.lo is None:
        return b.lo, b.lo_inc
    if b.lo is None or a.lo > b.lo:
        return a.lo, a.lo_inc
    if b.lo > a.lo:
        return b.lo, b.lo_inc
    return a.lo, a.lo_inc and b.lo_inc



def _min_hi(a: Interval, b: Interval) -> tuple[Decimal | None, bool]:
    if a.hi is None:
        return b.hi, b.hi_inc
    if b.hi is None or a.hi < b.hi:
        return a.hi, a.hi_inc
    if b.hi < a.hi:
        return b.hi, b.hi_inc
    return a.hi, a.hi_inc and b.hi_inc



def _dim(okei: str | None) -> str | None:
    return OKEI[okei][0] if okei in OKEI else None



def allowed_interval(v: dict) -> Interval | None:
    if value_defects(v):
        return None
    okei = v.get("okei_code")
    if v.get("kind") == "number":
        x = dec(v.get("x"))
        if x is None:
            return None
        bx, dim = to_base(x, okei)
        return Interval(bx, True, bx, True, dim)
    if v.get("kind") == "range":
        lo, hi = dec(v.get("lo")), dec(v.get("hi"))
        blo = to_base(lo, okei)[0] if lo is not None else None
        bhi = to_base(hi, okei)[0] if hi is not None else None
        return Interval(blo, v.get("lo_inclusive", True), bhi, v.get("hi_inclusive", True), _dim(okei))
    return None



def value_display(v: dict) -> str:
    short = OKEI[v["okei_code"]][2] if v.get("okei_code") in OKEI else (v.get("okei_name") or "")
    if v.get("kind") == "enum":
        return v.get("label", "")
    if v.get("kind") == "number":
        return f"{fmt(dec(v['x']) or Decimal(0))} {short}".strip()
    if v.get("kind") == "range":
        lo, hi = dec(v.get("lo")), dec(v.get("hi"))
        parts = []
        if lo is not None:
            parts.append(f"{'≥' if v.get('lo_inclusive', True) else '>'} {fmt(lo)}")
        if hi is not None:
            parts.append(f"{'≤' if v.get('hi_inclusive', True) else '<'} {fmt(hi)}")
        return (" и ".join(parts) + f" {short}").strip()
    return "?"



def assertion_interval(a: C.Assertion, char_okei: str | None) -> Interval | None:
    v = a.value
    if (isinstance(v, (C.NumberValue, C.RangeValue)) and a.origin == "text" and char_okei and not v.unit_okei
            and _dim(char_okei) not in COUNT_DIMENSIONS):
        return None  # The text has not stated a unit; only an explicit field answer may supply it.
    if isinstance(v, C.NumberValue):
        x = dec(v.x)
        if x is None:
            return None
        bx, dim = to_base(x, v.unit_okei or char_okei)
        op = a.operator
        if op in ("ge", "gt"):
            return Interval(bx, op == "ge", None, True, dim)
        if op in ("le", "lt"):
            return Interval(None, True, bx, op == "le", dim)
        return Interval(bx, True, bx, True, dim)
    if isinstance(v, C.RangeValue):
        okei = v.unit_okei or char_okei
        lo = to_base(dec(v.lo), okei)[0] if v.lo is not None else None
        hi = to_base(dec(v.hi), okei)[0] if v.hi is not None else None
        return Interval(lo, v.lo_inclusive, hi, v.hi_inclusive, _dim(okei))
    return None



def label_match(label: str, text_label: str, allow_specific: bool = False) -> bool:
    """Allowed label vs a label from the text. allow_specific: «Металл хромированный» is also a «Металл»."""
    if code_like(label) or code_like(text_label):
        # «L2» is not «L5», «O-образная» is not «U-образная»: codes compare by their exact spelling.
        fa, fb = fold(label).strip(), fold(text_label).strip()
        return fa == fb or (allow_specific and fa.startswith(fb + " "))
    a, b = content_stems(label), content_stems(text_label)
    if not a and not b:  # «3»: a label of digits only is its spelling
        return bool(fold(label).strip()) and fold(label).strip() == fold(text_label).strip()
    if not a or not b or (len(a) != len(b) and not (allow_specific and len(a) > len(b))):
        return False
    return all(stems_match(x, y) for x, y in zip(a, b, strict=False))



def value_matches(a: C.Assertion, v: dict, char_okei: str | None) -> bool | None:
    """True/False if the assertion is compatible with allowed value v; None if not comparable."""
    if value_defects(v):
        return None
    if isinstance(a.value, C.EnumValue):
        if v.get("kind") != "enum":
            return None
        # Text may name a general label («Металл» covers «Металл хромированный»); a picked catalog option is exact.
        return label_match(v.get("label", ""), a.value.label, allow_specific=a.origin == "text")
    if isinstance(a.value, C.BoolValue):
        if v.get("kind") != "enum" or norm(v.get("label", "")) not in (YES, NO):
            return None
        return (norm(v["label"]) == YES) == a.value.v
    ai = assertion_interval(a, char_okei)
    vi = allowed_interval(v)
    if ai is None or vi is None or ai.dim != vi.dim:
        return None
    return ai.overlaps(vi)



def char_okei(chars: list[Char]) -> str | None:
    for ch in chars:
        for v in ch.allowed:
            if v.get("okei_code"):
                return v["okei_code"]
    return None



def _assertion_text(a: C.Assertion) -> str:
    v = a.value
    if isinstance(v, C.BoolValue):
        return "Да" if v.v else "Нет"
    if isinstance(v, C.EnumValue):
        return ("не " if a.polarity == "negative" else "") + v.label
    if isinstance(v, C.NumberValue):
        ops = {"ge": "≥ ", "gt": "> ", "le": "≤ ", "lt": "< "}
        return f"{ops.get(a.operator, '')}{fmt(dec(v.x) or Decimal(0))} {v.unit_name or ''}".strip()
    if isinstance(v, C.RangeValue):
        lo = fmt(d) if (d := dec(v.lo)) is not None else "…"
        hi = fmt(d) if (d := dec(v.hi)) is not None else "…"
        return f"{lo}–{hi} {v.unit_name or ''}".strip()
    if isinstance(v, C.TextValue):
        # «стальной головки нет»: a negative text fact must never read as the positive value.
        return ("не " if a.polarity == "negative" else "") + v.s
    if isinstance(v, C.UnparsedValue):
        return v.s
    return "?"
