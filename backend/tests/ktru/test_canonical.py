"""Focused deterministic-extraction tests: canonical vocabulary, quantities, assertions and grounding.

Pure rules only: no catalog, no snapshot, no model. Every expectation pins the current behaviour.
"""

from decimal import Decimal

import pytest

from src.ktru import contracts as C
from src.ktru.engine import canonical
from src.ktru.engine.assertions import _counts_items, label_product, packed_label, rule_assertions, subject_of
from src.ktru.engine.text import _unit, identifier, quantities
from src.ktru.ml import grounding


def _one_quantity(text: str):
    found = quantities(text)
    assert len(found) == 1, [(q.raw, str(q.a), str(q.b), q.op) for q in found]
    return found[0]


# ---- canonical: presence, materials, colours ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prop", "value", "relation", "expected"),
    [
        ("подлокотники", "нет", "Стул без подлокотников", ("подлокотники", False)),
        ("аккумулятор", "в комплекте", "аккумулятор в комплекте", ("аккумулятор", True)),
        ("подлокотники", "нет", "Стул с подлокотниками", ("подлокотники", True)),
        ("подлокотники", "нет", "Стул", None),
    ],
)
def test_presence_reads_yes_no_from_the_relation(prop, value, relation, expected):
    assert canonical.presence(prop, value, relation) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("деревянной", "дерево"),
        ("деревянный", "дерево"),
        ("хлопковый", "хлопок"),
        ("стальной", "сталь"),
        ("фанерный", "фанера"),
    ],
)
def test_material_canonicalises_adjectives(value, expected):
    assert canonical.material(value) == expected


@pytest.mark.parametrize(("value", "expected"), [("синяя", "синий"), ("зелёный", "зеленый")])
def test_color_canonicalises_adjectives(value, expected):
    assert canonical.color(value) == expected


@pytest.mark.parametrize(
    ("prop", "value", "expected"),
    [
        ("цвет", "синий", ("цвет", "синий")),
        ("окрас", "красный", ("цвет", "красный")),
        ("материал", "деревянный", ("материал", "дерево")),
        ("что-то непонятное", "дерево", ("материал", "дерево")),
    ],
)
def test_categorical_picks_property_and_label(prop, value, expected):
    assert canonical.categorical(prop, value) == expected


# ---- canonical: property names, packages, unknowns, modality, labels, digits -----------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Сам молоток весит", "масса"),
        ("брутто с коробкой", "масса"),
        ("power", "мощность"),
        ("длина 200 мм", "длина"),
        ("нетто", "масса"),
        ("Weight", "масса"),
    ],
)
def test_property_name(raw, expected):
    assert canonical.property_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("с коробкой", "коробка"), ("в упаковке", "упаковка"), ("брутто", "упаковка"), ("тара", "тара"), ("Стол", None)],
)
def test_package_label(raw, expected):
    assert canonical.package_label(raw) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("неизвестен", True),
        ("неизвестная", True),
        ("не указано", True),
        ("нет данных", True),
        ("-", True),
        ("дерево", False),
    ],
)
def test_unknown_value(value, expected):
    assert canonical.unknown_value(value) is expected


@pytest.mark.parametrize(
    ("prop", "dimension", "expected"),
    [
        ("масса", "volume", True),
        ("ширина", "mass", True),
        ("масса", "mass", False),
        ("мощность", "percent", False),
        ("материал", "length", False),
    ],
)
def test_dimension_doubt_flags_mismatched_units(prop, dimension, expected):
    assert (canonical.dimension_doubt(prop, dimension) is not None) is expected


@pytest.mark.parametrize(
    ("text", "phrase", "expected"),
    [
        ("Стул, наверное, деревянный", "деревянный", "uncertain"),
        ("Возможно, стул деревянный", "деревянный", "uncertain"),
        ("В заказе стоит 800 мм.", "800", "order"),
        ("Стол деревянный", "деревянный", None),
    ],
)
def test_modality(text, phrase, expected):
    start = text.index(phrase)
    polarity, _ = canonical.modality(text, start, start + len(phrase))
    assert polarity == expected


@pytest.mark.parametrize(
    ("text", "family", "expected"),
    [
        ("Нужно 1 000 стульев.", "Стул", "стул"),
        ("Высота стола не меньше 90 см", "Стол письменный", "стол"),
        ('{"name":"Дрель","power":"600 Вт"}', None, "дрель"),
        ("Стул без подлокотников", None, "стул"),
        ("Толщина столешницы 20 мм", None, "стол"),
        ("Дрель", None, "дрель"),
    ],
)
def test_product_label(text, family, expected):
    assert canonical.product_label(text, family) == expected


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ("семьдесят пять сантиметров", ("75", "см")),
        ("полтора метра", ("1,5", "м")),
        ("девятьсот граммов", ("900", "г")),
    ],
)
def test_digits_spells_numbers_and_units(text, parts):
    result = canonical.digits(text)
    assert all(part in result for part in parts)


# ---- text: identifiers and quantities --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("A12B", True),
        ("СО-10", True),
        ("7O0", True),
        ("18В", False),
        ("50х60х70", False),
        ("700", False),
        ("мм", False),
    ],
)
def test_identifier(token, expected):
    assert identifier(token) is expected


@pytest.mark.parametrize(
    ("text", "lo", "hi", "lo_inclusive", "hi_inclusive"),
    [
        ("[0,5; 0,7) кг", "0.5", "0.7", True, False),
        ("[0,5; 0,7] кг", "0.5", "0.7", True, True),
        ("(0,5; 0,7] кг", "0.5", "0.7", False, True),
        ("(0,5; 0,7) кг", "0.5", "0.7", False, False),
    ],
)
def test_bracket_interval(text, lo, hi, lo_inclusive, hi_inclusive):
    quantity = _one_quantity(text)
    assert (quantity.op, quantity.a, quantity.b) == ("between", Decimal(lo), Decimal(hi))
    assert (quantity.lo_inclusive, quantity.hi_inclusive) == (lo_inclusive, hi_inclusive)
    assert quantity.unit_raw == "кг"
    assert quantity.doubt is None


@pytest.mark.parametrize(
    ("text", "lo", "hi"),
    [("20 ± 1 мм", "19", "21"), ("20 мм ± 1%", "19.8", "20.2")],
)
def test_tolerance_becomes_a_range(text, lo, hi):
    quantity = _one_quantity(text)
    assert (quantity.op, quantity.a, quantity.b) == ("between", Decimal(lo), Decimal(hi))
    assert quantity.unit_raw == "мм"


@pytest.mark.parametrize(
    ("text", "values", "doubted"),
    [("120 на 60 см", ["120", "60"], 0), ("120 × 60 × 75 см", ["120", "60", "75"], 3)],
)
def test_dimension_products(text, values, doubted):
    found = quantities(text)
    assert [q.a for q in found] == [Decimal(value) for value in values]
    assert [q.unit_raw for q in found] == ["см"] * len(values)
    assert sum(bool(q.doubt) for q in found) == doubted
    if doubted:
        assert all("Порядок" in q.doubt for q in found)


def test_reversed_range_keeps_doubt():
    quantity = _one_quantity("от 80 см до 60 см")
    assert (quantity.op, quantity.a, quantity.b) == ("between", Decimal(60), Decimal(80))
    assert quantity.doubt and "обратном порядке" in quantity.doubt


def test_malformed_number_is_not_a_quantity():
    assert quantities("1,2,5 кг") == []


@pytest.mark.parametrize(
    ("text", "value", "doubted"),
    [("1,000 стульев", "1.000", True), ("1 000 стульев", "1000", False)],
)
def test_thousands_separator(text, value, doubted):
    quantity = _one_quantity(text)
    assert quantity.a == Decimal(value)
    assert bool(quantity.doubt) is doubted
    if doubted:
        assert "тысячи" in quantity.doubt


@pytest.mark.parametrize(
    ("text", "value", "unit"),
    [("минус 5", "-5", None), ("1e3 г", "1000", "г")],
)
def test_signed_and_exponent_numbers(text, value, unit):
    quantity = _one_quantity(text)
    assert quantity.a == Decimal(value)
    assert quantity.unit_raw == unit


def test_lower_and_upper_bounds_merge():
    quantity = _one_quantity("не меньше 90 см и не больше 100 см")
    assert (quantity.op, quantity.a, quantity.b) == ("between", Decimal(90), Decimal(100))
    assert quantity.lo_inclusive and quantity.hi_inclusive
    assert quantity.doubt is None


@pytest.mark.parametrize(
    ("text", "op", "value"),
    [("не менее 90 см", "ge", "90"), ("не более 100 см", "le", "100")],
)
def test_single_bound_operator(text, op, value):
    quantity = _one_quantity(text)
    assert (quantity.op, quantity.a, quantity.b) == (op, Decimal(value), None)


def test_identifier_is_not_a_quantity():
    found = quantities("модели A12B, длина 200 мм")
    assert len(found) == 1
    assert found[0].a == Decimal(200)
    assert "длина" in found[0].property_raw


@pytest.mark.parametrize(
    ("text", "value", "unit", "dimension"),
    [("18В", "18", "в", "voltage"), ("500г", "500", "г", "mass")],
)
def test_glued_unit_is_still_a_quantity(text, value, unit, dimension):
    quantity = _one_quantity(text)
    assert (quantity.a, quantity.unit_raw, quantity.dimension) == (Decimal(value), unit, dimension)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("килограмма", "кг"),
        ("сантиметров", "см"),
        ("мм.", "мм"),
        ("°C", "°c"),
        ("шт.", "шт"),
        ("пар", "пар"),
        ("м", "м"),
        (None, None),
        ("", None),
    ],
)
def test_unit_abbreviation(raw, expected):
    assert _unit(raw) == expected


# ---- assertions: rule_assertions, subjects, labels -------------------------------------------------------------------


def test_rule_assertions_reads_model_identifier_and_length():
    found = rule_assertions("Ключ модели A12B, длина 200 мм", 1)
    texts = [(a.property_raw, a.value.s) for a in found if isinstance(a.value, C.TextValue)]
    numbers = [(a.property_raw, a.value.x, a.status) for a in found if isinstance(a.value, C.NumberValue)]
    assert texts == [("модель", "A12B")]
    assert numbers == [("длина", "200", "supported")]


def test_rule_assertions_separates_net_from_gross():
    found = rule_assertions("Молоток, масса нетто 500 г, масса брутто 700 г", 1)
    numbers = [a for a in found if isinstance(a.value, C.NumberValue)]
    assert {(a.subject_kind, a.value.x) for a in numbers} == {("item", "500"), ("package", "700")}
    assert all(a.note_ru is None for a in numbers)


@pytest.mark.parametrize(
    ("context", "kind", "label"),
    [
        ("сиденье фанера", "part", "сиденье"),
        ("каркас стальной", "part", "каркас"),
        ("масса упаковки", "package", "упаковка"),
        ("масса брутто 700 г", "package", "товар в упаковке"),
        ("масса молотка 500 г", "item", None),
        ("комплект", "set", "набор"),
        ("подлокотники", "item", None),
    ],
)
def test_subject_of(context, kind, label):
    subject = subject_of(context)
    assert (subject.kind, subject.label) == (kind, label)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 000 стульев", True),
        ("1,000 стульев", True),
        ("4шт", True),
        ("20 пар", True),
        ("500 г", False),
        ("120 см", False),
        ("5 кг", False),
    ],
)
def test_counts_items(text, expected):
    assert _counts_items(text, _one_quantity(text)) is expected


@pytest.mark.parametrize(
    ("product", "expected"),
    [("Молоток", "Молоток в упаковке"), ("Стол письменный", "Стол в упаковке")],
)
def test_packed_label(product, expected):
    assert packed_label(product) == expected


def test_label_product_names_item_and_package():
    found = rule_assertions("Молоток, масса нетто 500 г, масса брутто 700 г", 1)
    labeled = label_product(found, "молоток")
    assert all(a.subject_label == "молоток" for a in labeled if a.subject_kind == "item")
    assert all(a.subject_label == "молоток в упаковке" for a in labeled if a.subject_kind == "package")


# ---- grounding: quotes are proved, doubt is preserved -----------------------------------------------------------------


def test_ground_keeps_quoted_part_material():
    facts = [{"property": "материал", "value": "фанера", "subject": "part", "part": "сиденье",
              "quote": "сиденье фанера"}]
    kept, rejected = grounding.ground(facts, "Стул: сиденье фанера")
    assert not rejected
    assert len(kept) == 1
    assert kept[0]["status"] == "supported"
    assert kept[0]["subject_ref"]["kind"] == "part"
    assert kept[0]["subject_ref"]["label"] == "сиденье"
    assert kept[0]["typed_value"] == {"type": "text", "s": "фанера"}


@pytest.mark.parametrize(
    ("fact", "text", "reason"),
    [
        ({"property": "модель", "value": "700", "quote": "модели 7O0"}, "Дрель модели 7O0", "не подтверждено"),
        ({"property": "цвет", "value": "неизвестен", "quote": "Цвет неизвестен"}, "Цвет неизвестен", "не указано"),
    ],
)
def test_ground_rejects_ungrounded_values(fact, text, reason):
    kept, rejected = grounding.ground([fact], text)
    assert not kept
    assert len(rejected) == 1
    assert reason in rejected[0]["reason_ru"]


def test_ground_uncertain_fact_stays_ambiguous():
    facts = [{"property": "материал", "value": "дерево", "quote": "деревянный", "uncertain": True}]
    kept, rejected = grounding.ground(facts, "Стул, наверное, деревянный")
    assert not rejected
    assert [fact["status"] for fact in kept] == ["ambiguous"]
