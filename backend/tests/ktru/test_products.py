"""Several products in one description: layouts, subjects and statement owners. Pure rules: no catalog, no model."""

import pytest

from src.ktru import contracts as C
from src.ktru.engine import products
from src.ktru.engine.assertions import (
    ROOT_ID,
    describes_root,
    model_assertions,
    presence_assertions,
    product_subjects,
    root_label,
    rule_assertions,
    subject_matches,
)
from src.ktru.ml import grounding


def _shape(text: str):
    layout = products.layout(text)
    assert layout is not None
    return [(p.kind, p.label, None if p.parent is None else layout.products[p.parent].label,
             [c.value for c in p.counts]) for p in layout.products]


def _facts(found: list[C.Assertion], prop: str) -> list[tuple]:
    return sorted(((a.value.x, a.subject.kind, a.subject.label) for a in found if a.property_raw == prop),
                  key=lambda fact: (fact[0], fact[2] or ""))


@pytest.mark.parametrize("text", [
    "Молоток слесарный 500 г, рукоятка деревянная.",
    "Стул офисный, 4 шт, черный",
    "Ширина 50, высота 40",
    "Брюки 48, рост 170",
    "Купальник раздельный 152, р-р 38. Вид основного сырья Полиэстер",
    "Куртка синяя 2 шт",
])
def test_one_product_has_no_layout(text):
    assert products.layout(text) is None


@pytest.mark.parametrize(("text", "expected"), [
    ("молоток и гвозди", ["молоток", "гвозди"]),
    ("Молоток массой 500 г и гвозди длиной 50 мм", ["Молоток массой 500 г", "гвозди длиной 50 мм"]),
    ("Куртка, размер 48; Стул чёрный — 3 штуки", ["Куртка; размер 48", "Стул чёрный — 3 штуки"]),
    ("Стул 4шт и молаток 2шт. На стуле нет подлокотников.",
     ["Стул 4шт; На стуле нет подлокотников.", "молаток 2шт."]),
    ("Стул офисный, мягкое сиденье и жёсткая спинка", ["Стул офисный, мягкое сиденье и жёсткая спинка"]),
    ("Молоток, масса 500 г", ["Молоток, масса 500 г"]),
    ("Брюки 48 и куртка 50 упакованы в один костюм.", ["Брюки 48 и куртка 50 упакованы в один костюм."]),
    ("Один набор: молоток и гвозди", ["Один набор: молоток и гвозди"]),
    ("Молоток, бренд Зубр, артикул 123", ["Молоток, бренд Зубр, артикул 123"]),
    ("Купальник раздельный 152, р-р 38. Вид основного сырья Полиэстер",
     ["Купальник раздельный 152, р-р 38. Вид основного сырья Полиэстер"]),
    ("Стол письменный, гарантия 24 месяца, страна производства Россия",
     ["Стол письменный, гарантия 24 месяца, страна производства Россия"]),
])
def test_split_independent_cards_keeps_properties_and_sets(text, expected):
    names = ["Молоток", "Гвозди", "Стул", "Стол", "Куртка", "Халат", "Купальник"]
    assert products.split_descriptions(text, names) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("1. Стул офисный, чёрный\n2. Навесной замок, ширина 50 мм\n3. Лобзик электрический, 2 шт",
     ["Стул офисный, чёрный", "Навесной замок, ширина 50 мм", "Лобзик электрический, 2 шт"]),
    ("Набор отверток из 6 предметов, 2 шт; Стул офисный, 10 шт; Стол письменный, 5 шт",
     ["Набор отверток из 6 предметов; 2 шт", "Стул офисный; 10 шт", "Стол письменный; 5 шт"]),
    ("Два стула: один синий, второй зеленый. Стол письменный, ширина 1200 мм. Молоток, масса 500 г.",
     ["Два стула: один синий, второй зеленый.", "Стол письменный; ширина 1200 мм.", "Молоток; масса 500 г."]),
    ("Один набор: молоток и гвозди. Стул офисный, 2 шт",
     ["Один набор: молоток и гвозди.", "Стул офисный; 2 шт"]),
    ("№\tНаименование товара\tКоличество\tЦвет\n1\tСтул офисный\t10\tсиний\n2\tСтол письменный\t5\t",
     ["Стул офисный; Количество: 10; Цвет: синий", "Стол письменный; Количество: 5"]),
    ("Нужно приобрести следующие товары:\nСтул офисный\nМолоток слесарный",
     ["Нужно приобрести следующие товары:; Стул офисный", "Молоток слесарный"]),
    ("Стул офисный. Стул оснащен мягкой спинкой. Стул предназначен для кабинета.",
     ["Стул офисный. Стул оснащен мягкой спинкой. Стул предназначен для кабинета."]),
    ("Стул офисный. Стул офисный оснащен мягкой спинкой. Стул офисный предназначен для кабинета.",
     ["Стул офисный. Стул офисный оснащен мягкой спинкой. Стул офисный предназначен для кабинета."]),
    ("Стул синий; Стул зеленый", ["Стул синий", "Стул зеленый"]),
    ("1. Ксилофон, 1 шт\n2. Стул, 2 шт\n3. Бубен, 3 шт",
     ["Ксилофон, 1 шт", "Стул, 2 шт", "Бубен, 3 шт"]),
    ("2 — дрель; набор — 1; 5 — свёрла. Свёрла входят в набор, две дрели отдельно.",
     ["2 — дрель; набор — 1; 5 — свёрла. Свёрла входят в набор, две дрели отдельно."]),
    ("1. Молоток, 2 шт\n2. Ручка шариковая, 10 шт\n3. Бумага офисная, 5 пачек\n4. Блокнот, 20 шт",
     ["Молоток, 2 шт", "Ручка шариковая, 10 шт", "Бумага офисная, 5 пачек", "Блокнот, 20 шт"]),
])
def test_split_lists_tables_and_local_sets(text, expected):
    names = ["Стул", "Стол", "Молоток", "Гвозди", "Набор", "Навесной замок", "Электролобзик",
             "Ручка", "Бумага", "Блокнот"]
    assert products.split_descriptions(text, names) == expected


def test_enumeration_names_each_product_with_its_count():
    assert _shape("Стул 4шт и молаток 2шт. На стуле нет подлокотников.") == [
        ("item", "стул", None, ["4"]), ("item", "молаток", None, ["2"])]
    assert _shape("Куртка синяя — 2 штуки; стул чёрный — 3 штуки.") == [
        ("item", "куртка", None, ["2"]), ("item", "стул", None, ["3"])]


def test_table_rows_are_products():
    assert _shape("Товар\tКоличество\tЦвет\nКуртка\t2\t\nСтул\t3\tчёрный") == [
        ("item", "куртка", None, ["2"]), ("item", "стул", None, ["3"])]


def test_instances_hang_under_the_batch():
    assert _shape("Поставляем два стула: один синий, второй зеленый.") == [
        ("item", "стул", None, ["2"]), ("item", "стул 1", "стул", ["1"]), ("item", "стул 2", "стул", [])]
    assert _shape("Три отдельных ботинка для витрины: один размера 40 и два размера 42.")[0] == (
        "item", "ботинок", None, ["3"])


def test_sets_hold_their_members():
    assert _shape("Брюки 48 и куртка 50 упакованы в один костюм.") == [
        ("set", "костюм", None, ["1"]), ("part", "брюки", "костюм", []), ("part", "куртка", "костюм", [])]
    assert _shape("Один учебный набор: мини-стул 4шт и мини-молоток 2шт.") == [
        ("set", "учебный набор", None, ["1"]), ("part", "мини-стул", "учебный набор", ["4"]),
        ("part", "мини-молоток", "учебный набор", ["2"])]


def test_membership_stated_later_moves_a_product_into_the_set():
    inside = _shape("2 — дрель; набор — 1; 5 — свёрла. Свёрла входят в набор, две дрели отдельно.")
    assert inside == [("item", "дрель", None, ["2", "2"]), ("set", "набор", None, ["1"]),
                      ("part", "сверла", "набор", ["5"])]
    apart = _shape("2 — дрель; набор — 1; 5 — свёрла. Свёрла отдельно, в наборе отвёртки.")
    assert apart[2] == ("item", "сверла", None, ["5"]) and apart[3] == ("part", "отвертки", "набор", [])


@pytest.mark.parametrize(("noun", "numeral", "expected"), [
    ("стула", "два", "стул"), ("молотка", "Два", "молоток"), ("ботинка", "три", "ботинок"),
    ("дрели", "две", "дрель"), ("куртки", "две", "куртка"), ("мини-стул", None, "мини-стул"),
])
def test_lemma_of_a_counted_noun(noun, numeral, expected):
    assert products.lemma(noun, numeral) == expected


@pytest.mark.parametrize(("word", "noun", "same"), [
    ("стуле", "стул", True), ("молотке", "молаток", True), ("мини-стульев", "мини-стул", True),
    ("мини-стульев", "мини-молоток", False), ("стола", "стул", False), ("наборе", "набор", True),
])
def test_references_to_a_product(word, noun, same):
    assert products.same_noun(word, noun) is same


def test_counts_and_statements_belong_to_their_products():
    text = "Стул 4шт и молаток 2шт. На стуле нет подлокотников, на молотке не знаю материал ручки."
    found = rule_assertions(text, 1)
    assert _facts(found, "количество") == [("2", "item", "молаток"), ("4", "item", None)]
    assert root_label(text) == "стул"
    absent = next(a for a in found if a.property_raw == "наличие")
    assert absent.subject.label == "подлокотников" and absent.subject.parent_subject_id == ROOT_ID
    handle = next(a for a in found if a.value_raw.startswith("на молотке"))
    hammer = next(a.subject for a in found if a.subject.label == "молаток")
    assert handle.subject.kind == "part" and handle.subject.parent_subject_id == hammer.id


def test_same_count_of_two_products_stays_two_facts():
    assert _facts(rule_assertions("Стул 2шт и стол 2шт", 1), "количество") == [
        ("2", "item", None), ("2", "item", "стол")]


def test_counted_noun_gets_pieces_and_its_words_as_evidence():
    text = "Два молотка: один с деревянной, второй со стеклопластиковой ручкой."
    count = next(a for a in rule_assertions(text, 1) if a.property_raw == "количество" and a.value.x == "2")
    assert count.subject.id == ROOT_ID and count.value.unit_name == "шт"
    assert any(e.span.text == "Два молотка" for e in count.evidence)


def test_instance_part_by_ellipsis_from_the_model():
    text = "Два молотка: один с деревянной, второй со стеклопластиковой ручкой."
    kept, rejected = grounding.ground([
        {"property": "материал", "value": "деревянной", "subject": "part", "part": "ручка", "quote": "деревянной"},
        {"property": "материал", "value": "стеклопластиковой", "subject": "part", "part": "ручка",
         "quote": "стеклопластиковой"}], text)
    assert not rejected
    found = model_assertions(kept, text, 1)
    handles = {a.value.s: a.subject for a in found}
    owners = {s.id: s.label for s in product_subjects(text)}
    assert {s.label for s in handles.values()} == {"рукоятка"}
    assert owners[handles["дерево"].parent_subject_id] == "молоток 1"
    assert owners[handles["стеклопластик"].parent_subject_id] == "молоток 2"


def test_set_members_from_the_model():
    text = "Брюки 48 и куртка 50 упакованы в один костюм."
    kept, rejected = grounding.ground([
        {"property": "размер", "value": "48", "subject": "part", "part": "брюки", "quote": "Брюки 48"},
        {"property": "размер", "value": "50", "subject": "part", "part": "куртка", "quote": "куртка 50"}], text)
    assert not rejected
    sizes = {a.value.x: a.subject for a in model_assertions(kept, text, 1)}
    suit = next(a for a in rule_assertions(text, 1) if a.subject.kind == "set" and a.property_raw == "количество")
    assert (sizes["48"].label, sizes["50"].label) == ("брюки", "куртка")
    assert sizes["48"].parent_subject_id == sizes["50"].parent_subject_id == suit.subject.id
    assert (suit.subject.label, suit.value.x, suit.value.unit_name) == ("костюм", "1", "шт")


def test_model_batch_set_is_the_item():
    kept, rejected = grounding.ground([{"property": "количество", "value": "две", "unit": "штука", "subject": "set",
                                        "quote": "Две штуки вместе"}], "Поставляем стулья. Две штуки вместе.")
    assert not rejected and kept[0]["subject_ref"]["kind"] == "item"


def test_only_the_root_product_binds_to_the_catalog():
    found = rule_assertions("Куртка синяя — 2 штуки; стул чёрный — 3 штуки.", 1)
    jacket = next(a for a in found if a.property_raw == "количество" and a.subject.id == ROOT_ID)
    chair = next(a for a in found if a.property_raw == "количество" and a.subject.id != ROOT_ID)
    assert describes_root(jacket.subject) and not describes_root(chair.subject)
    assert subject_matches(jacket, "Количество") and not subject_matches(chair, "Количество")


def test_named_set_alone_keeps_its_facts_on_the_described_item():
    text = "Костюм женский: куртка, брюки. Назначение Женский. Брюки фиолетового цвета"
    assert root_label(text) == "костюм женский"
    found = rule_assertions(text, 1)
    purpose = next(a for a in found if a.value_raw == "Назначение Женский")
    trousers = next(a for a in found if a.value_raw == "Брюки фиолетового цвета")
    assert purpose.subject.id == ROOT_ID
    assert trousers.subject.kind == "part" and trousers.subject.label == "брюки"


def test_negation_before_a_noun_is_not_the_absence_of_the_place():
    found = presence_assertions("На стуле нет подлокотников. Подлокотников у мини-стульев нет.", 1)
    assert sorted(a.subject.label for a in found) == ["подлокотников", "подлокотников"]
