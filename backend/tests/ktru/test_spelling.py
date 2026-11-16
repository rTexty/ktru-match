"""Automatic spelling correction: word candidates (catalog and Russian dictionary) and whole-text rules."""

import importlib.util
import os
from collections import Counter
from pathlib import Path

import pytest

from src.ktru.catalog.reader import Catalog
from src.ktru.engine.source_map import autocorrect
from src.ktru.engine.spelling import typo_cost
from src.ktru.engine.text import stem

ROOT = Path(__file__).resolve().parents[3]
SNAP = Path(os.environ.get("KTRU_SNAPSHOT_DIR", ROOT / ".ktru-build" / "snapshots" / "current"))


def words_catalog(frequencies):
    catalog = Catalog.__new__(Catalog)
    catalog.vocab = set(frequencies)
    catalog._freq = Counter(frequencies)
    catalog._stem_freq = Counter()
    for word, count in frequencies.items():
        catalog._stem_freq[stem(word)] += count
    return catalog


def test_common_typing_mistakes_cost_less_than_other_letters():
    assert typo_cost("лакет", "пакет") > typo_cost("пакит", "пакет")  # first letter; о/а-like vowel
    assert typo_cost("пакер", "пакет") < typo_cost("пакен", "пакет")  # р is next to т on the keyboard
    assert typo_cost("рапирой", "рапирой") == 0


@pytest.mark.parametrize(("typed", "vocab", "expected"), [
    ("леревянный", {"деревянный": 80}, "деревянный"),  # wrong first letter
    ("стлу", {"стул": 80, "стол": 80}, "стул"),  # swapped letters in a short word
    ("сул", {"стул": 80}, "стул"),  # three letters with a dropped one
    ("стл", {"стол": 80}, "стол"),  # typed without the vowel
    ("бумаго", {"бумага": 80}, "бумага"),  # misspelt ending of a dictionary word
    ("карандашь", {"карандаш": 80}, "карандаш"),  # needless soft sign
    ("Куууртка", {"куртка": 80}, "куртка"),
    ("сериии", {"серии": 80, "сери": 80}, "серии"),
    ("потрон", {"патрона": 80}, "патрон"),  # the dictionary word is much closer than the catalog form
])
def test_misspelling_is_corrected(typed, vocab, expected):
    assert words_catalog(vocab).correct_word(typed) == expected


@pytest.mark.parametrize(("typed", "vocab"), [
    ("КТРУ", {"кира": 80}),  # abbreviation
    ("вст", {"вест": 10}),  # «высота» in a short description, not a rare word
    ("офисн", {"офисный": 80, "офисе": 80}),  # a shortened word
    ("шестигранных", {"шестигранной": 80, "шестигранник": 80}),  # an unseen form of a catalog word
    ("пофиг", {"порфир": 80}),  # two expensive edits on a short word are a guess
    ("стеллажа", {"стелажа": 80}),  # a real word is not rewritten as another word
])
def test_word_is_left_as_typed(typed, vocab):
    catalog = words_catalog(vocab)
    fixed, corrections = autocorrect(typed, catalog.correct_word)
    assert fixed == typed and not corrections


def test_dictionary_word_does_not_replace_a_close_catalog_word():
    assert words_catalog({"предсердия": 80}).correct_word("предсердя") == "предсердия"


def test_text_rules_join_hyphenation_drop_invisible_characters_and_skip_quotes():
    catalog = words_catalog({"деревянный": 80, "куртка": 80, "стол": 80})
    text = "Стол дере-\nвянный, кур\u200bтка «Леревянный»"
    fixed, corrections = autocorrect(text, catalog.correct_word)
    assert fixed == "Стол деревянный, куртка «Леревянный»"
    assert [c.original for c in corrections] == ["дере-\nвянный", "кур\u200bтка"]
    assert all(fixed[c.transformed_span.start:c.transformed_span.end] == c.replacement for c in corrections)


def test_wrong_layout_and_translit_are_fixed():
    catalog = words_catalog({"стол": 80, "деревянный": 80})
    assert autocorrect("Cnjk lthtdzyysq", catalog.correct_word)[0] == "Стол деревянный"
    assert autocorrect("Stol derevyannyy, shirina 120 sm.", catalog.correct_word)[0] == \
        "Стол деревянный, ширина 120 см."


def test_brand_next_to_russian_words_is_not_transliterated():
    catalog = words_catalog({"молоток": 80})
    assert autocorrect("Молоток 500 г, Hammer Pro", catalog.correct_word)[0] == "Молоток 500 г, Hammer Pro"
    assert autocorrect("Makita DF333", catalog.correct_word)[0] == "Makita DF333"


def test_latin_lookalikes_inside_russian_words_are_cyrillic():
    catalog = words_catalog({"стол": 80, "высота": 80})
    fixed, fixes = autocorrect("Cтол, выcота 75 cм.", catalog.correct_word)
    assert fixed == "Стол, высота 75 см." and len(fixed) == len("Cтол, выcота 75 cм.")
    assert fixes[0].kind == "layout"


def test_spaced_and_glued_letters_become_words():
    known = {"дрель", "куртка", "синяя", "размер", "без", "капюшона"}
    catalog = words_catalog({w: 80 for w in known})
    is_word = lambda w: w in known  # noqa: E731
    assert autocorrect("Д р е л ь", catalog.correct_word, is_word)[0] == "Дрель"
    assert autocorrect("Курткасиняяразмер 48", catalog.correct_word, is_word)[0] == "Куртка синяя размер 48"
    assert autocorrect("безкапюшона", catalog.correct_word, is_word)[0] == "без капюшона"
    assert autocorrect("шуруповертгигант", catalog.correct_word, is_word)[0] == "шуруповертгигант"  # not all words


@pytest.mark.skipif(not (SNAP / "manifest.json").exists(), reason="snapshot not built")
def test_spelling_quality_on_the_catalog_snapshot():
    """The bar: >95% of misspellings fixed, few wrong fixes, no correct word in real descriptions changed."""
    spec = importlib.util.spec_from_file_location("spelling_benchmark", ROOT / "backend/scripts/spelling_benchmark.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    catalog = Catalog(SNAP)
    scores = bench.synthetic(catalog).values()
    total = sum(score.total for score in scores)
    assert sum(score.fixed for score in scores) / total > 0.95
    assert sum(score.wrong for score in scores) / total < 0.04
    changes = {(original, replacement) for _, original, replacement, _ in bench.golden_changes(catalog)}
    assert changes == {
        ("Стлу", "Стул"), ("металический", "металлический"), ("сидене", "сиденье"),
        ("подлокотнков", "подлокотников"), ("розмер", "размер"), ("подклатка", "подкладка"),
        ("полиэстэр", "полиэстер"), ("Дерль", "Дрель"), ("ударнная", "ударная"), ("мощьность", "мощность"),
        ("потрон", "патрон"), ("Стл", "Стол"), ("Куууртка", "Куртка"), ("ооочень", "очень"),
        ("тёёплая", "теплая"), ("Cnjk lthtdzyysq.", "Стол деревянныйю"), ("деревянныйю", "деревянный"),
        ("Кур\u200bтка", "Куртка"), ("раз\u200bмер", "размер"), ("дере-\nвянный", "деревянный"),
        ("промокаит", "промокает"), ("Малаток", "Молоток"), ("дрэль", "дрель"),
        ("Cтол, выcота 75 cм.", "Стол, высота 75 см."), ("Stol", "Стол"), ("derevyannyy", "деревянный"),
        ("shirina", "ширина"), ("sm", "см"), ("Klyuch", "Ключ"), ("modeli", "модели"), ("dlina", "длина"),
        ("mm", "мм"),
    }
    assert not [word for word in bench.EVERYDAY if catalog.correct_word(word)]
