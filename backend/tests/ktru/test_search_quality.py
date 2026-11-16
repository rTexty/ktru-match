"""Search quality on the real catalog snapshot: everyday product words reach the catalog's own class names."""

import os
import re
from pathlib import Path

import pytest

from src.ktru.catalog.reader import Catalog
from src.ktru.service.stages import LexicalRetriever

ROOT = Path(__file__).resolve().parents[3]
SNAP = Path(os.environ.get("KTRU_SNAPSHOT_DIR", ROOT / ".ktru-build" / "snapshots" / "current"))
pytestmark = pytest.mark.skipif(not (SNAP / "manifest.json").exists(), reason="snapshot not built")

# (description, regex for an acceptable family name among the first three). KTRU names classes, not everyday words.
CASES = [
    ("Свитер мужской, шерсть", r"^Джемпер"),
    ("Нужно 48 курток размера 50", r"^Куртка"),
    ("Нужны 5 пар рабочих перчаток.", r"^Перчатки"),
    ("В упаковке 10 рубашек; масса одной 200 г", r"^Сорочка"),
    ("Ботинки кожаные, размер 42, закупаем 30 пар.", r"^Обувь"),
    ("Бейсболка синяя, регулировка сзади", r"^Головные уборы"),
    ("Ветровка женская", r"^Ветровка"),
    ("Кресло гостевое", r"^Кресло офисное"),
    ("Дрель аккумуляторная 18 В, один аккумулятор", r"^Дрель ручная электрическая"),
    ("УШМ сетевая, диаметр диска 125 мм", r"^Шлифмашина"),
    ("Болгарка 125 мм", r"^Шлифмашина"),
    ("Вчера стол был синий, сегодня в акте указан красный", r"^Стол"),
    ("Закажем два изделия: халат белый размер 50 и фартук чёрный", r"^(Халат|Фартук)"),
    ("Паяльник электрический 60 Вт", r"^Электропаяльник"),
]


@pytest.fixture(scope="module")
def retriever():
    catalog = Catalog(SNAP)
    return catalog, LexicalRetriever(catalog)


@pytest.mark.parametrize(("text", "pattern"), CASES)
def test_everyday_description_reaches_the_catalog_class(retriever, text, pattern):
    catalog, search = retriever
    names = [family.name for family in search.retrieve(text, catalog.snapshot_id).families[:3]]
    assert any(re.search(pattern, name) for name in names), names


def test_served_domain_beats_a_medical_family_with_the_same_head(retriever):
    catalog, search = retriever
    top = search.retrieve("Дрель аккумуляторная", catalog.snapshot_id).families[0]
    assert top.members[0].domain == "tools"


@pytest.mark.parametrize(("text", "domain"), [
    ("Бейсболка синяя", "clothing"),
    ("Стол складной 150 см", "furniture"),
    ("Отвертка длиной 150 мм", "tools"),
    ("Непонятная штука без названия", None),
])
def test_domain_comes_from_the_product_noun(retriever, text, domain):
    catalog, _ = retriever
    assert catalog.guess_domain(text) == domain


def test_units_and_containers_are_not_the_product(retriever):
    catalog, _ = retriever
    assert catalog.guess_domain("Три пары ботинок") == "clothing"
    assert catalog.text_heads("В упаковке 10 рубашек")[0][1] == "clothing"


@pytest.mark.parametrize(("text", "fixed"), [
    ("Стелаж металический, 5 полок.", "Стеллаж металлический, 5 полок."),
    ("Рулетка измерительня 5 м.", "Рулетка измерительная 5 м."),
    ("Диван трёхместный, цвет серый", "Диван трёхместный, цвет серый"),  # two dictionary words, but one real word
    ("Молоток фуфлопластовый, масса 500 г.", "Молоток фуфлопластовый, масса 500 г."),
    ("Столдеревянныйширина 120 см", "Стол деревянный ширина 120 см"),
])
def test_long_words_are_not_split_into_pieces(retriever, text, fixed):
    from src.ktru.engine.source_map import autocorrect

    catalog, _ = retriever
    assert autocorrect(text, catalog.correct_word, catalog.has_word, catalog.is_common)[0] == fixed
