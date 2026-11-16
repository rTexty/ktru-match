"""Shared catalog-domain validation for reading, comparison, questions and card completion."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from pydantic import ValidationError

from src.ktru import contracts as C
from src.ktru.engine.text import OKEI

if TYPE_CHECKING:
    from src.ktru.catalog.reader import Char

MARKER = "[не найдено значение ID"


def value_defects(value: dict) -> list[str]:
    """Validate retained values, including legacy snapshots, without repairing source facts."""
    defects = list(value.get("defects") or [])
    if value.get("defect"):
        defects.append(value["defect"])
    kind = value.get("kind")
    if kind == "enum":
        label = value.get("label")
        if not isinstance(label, str) or not label.strip():
            defects.append("invalid_enum_label")
        elif MARKER in label:
            defects.append("missing_value_marker")
    elif kind not in ("enum", "number", "range"):
        defects.append("unrecognized_value")
    if kind == "range" and value.get("lo") is None and value.get("hi") is None:
        defects.append("missing_range_bounds")
    parsed = {}
    for key in (("x",) if kind == "number" else ("lo", "hi") if kind == "range" else ()):
        raw = value.get(key)
        if raw is None and kind == "range":
            continue
        try:
            number = Decimal(str(raw))
            if not number.is_finite() or abs(number.adjusted()) > 64 or abs(number.as_tuple().exponent) > 64:
                raise ValueError("Invalid catalog number")
            parsed[key] = number
        except (InvalidOperation, ValueError):
            defects.append("invalid_number" if kind == "number" else "invalid_range_bound")
    if "lo" in parsed and "hi" in parsed:
        if parsed["lo"] > parsed["hi"]:
            defects.append("reversed_range")
        elif parsed["lo"] == parsed["hi"] and not (
            value.get("lo_inclusive", True) and value.get("hi_inclusive", True)
        ):
            defects.append("empty_range")
    return list(dict.fromkeys(defects))


def value_type(values: list[dict]) -> str:
    kinds = {v.get("kind", "raw") for v in values}
    if not kinds or kinds == {"raw"}:
        return "unknown"
    if len(kinds) > 1:
        return "mixed"
    kind = next(iter(kinds))
    if kind == "enum" and all(
        isinstance(v.get("label"), str) and v["label"].strip().casefold() in ("да", "нет") for v in values
    ):
        return "bool"
    return kind if kind in ("enum", "number", "range", "text", "bool") else "unknown"


def catalog_values(characteristic: Char | None) -> list[dict]:
    """Return answerable catalog values only when the whole characteristic is usable."""
    if (characteristic is None or not characteristic.char_code or characteristic.defects
            or characteristic.kind_raw not in ("1", "2", "3") or not characteristic.allowed):
        return []
    domains, dimensions = set(), set()
    try:
        for value in characteristic.allowed:
            if value_defects(value):
                return []
            kind = value.get("kind")
            if kind == "enum":
                C.EnumValue(label=value.get("label"))
                domains.add("enum")
                continue
            unit = value.get("okei_code")
            if unit is not None and unit not in OKEI:
                return []
            domains.add("numeric")
            dimensions.add(OKEI[unit][0] if unit else None)
            common = {"unit_okei": unit, "unit_name": value.get("okei_name")}
            if kind == "number":
                C.NumberValue(x=value.get("x"), **common)
            elif kind == "range":
                C.RangeValue(lo=value.get("lo"), hi=value.get("hi"),
                    lo_inclusive=value.get("lo_inclusive", True), hi_inclusive=value.get("hi_inclusive", True), **common)
            else:
                return []
    except (TypeError, ValueError, ValidationError):
        return []
    if len(domains) != 1 or len(dimensions) > 1:
        return []
    return list(characteristic.allowed)
