"""Spelling helpers: a Russian-dictionary guard and an edit distance where common typing mistakes are cheap."""

from functools import cache, lru_cache

import pymorphy3

_ROWS = ("йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю")
# Unstressed vowels people mix up are the cheapest mistake; voiced/voiceless consonants and neighbouring keys next.
_VOWEL_ALIKE = {frozenset(pair) for pair in ("оа", "еи", "её", "ея", "иы", "юу")}
_SOUND_ALIKE = {frozenset(pair) for pair in ("зс", "дт", "бп", "вф", "гк", "жш")}
_SILENT = set("ьъ")


def _neighbours() -> set[frozenset[str]]:
    pairs = set()
    for row in _ROWS:
        pairs |= {frozenset(pair) for pair in zip(row, row[1:], strict=False)}
    for upper, lower in zip(_ROWS, _ROWS[1:], strict=False):
        for i, char in enumerate(upper):
            pairs |= {frozenset((char, lower[j])) for j in (i - 1, i) if 0 <= j < len(lower)}
    return pairs


_KEYBOARD = _neighbours()


def _substitution(a: str, b: str) -> float:
    if a == b:
        return 0.0
    pair = frozenset((a, b))
    if pair in _VOWEL_ALIKE:
        return 0.4
    return 0.5 if pair in _SOUND_ALIKE or pair in _KEYBOARD else 1.0


def _indel(word: str, i: int) -> float:
    """Deleting/inserting word[i]: a doubled letter (н/нн) or a soft/hard sign is a cheap mistake."""
    char = word[i]
    if char in _SILENT or (i and word[i - 1] == char) or (i + 1 < len(word) and word[i + 1] == char):
        return 0.5
    return 1.0


@lru_cache(maxsize=65536)
def typo_cost(typed: str, word: str) -> float:
    """Weighted optimal-string-alignment distance from what was typed to a dictionary word.

    The first letter is rarely the mistyped one, so a different first letter costs half an edit more.
    """
    first = 0.5 if typed[:1] != word[:1] else 0.0
    return first + edit_cost(typed, word)


@lru_cache(maxsize=65536)
def edit_cost(typed: str, word: str) -> float:
    """Letter edits only, without the first-letter penalty."""
    n, m = len(typed), len(word)
    row = [0.0] * (m + 1)
    for j in range(1, m + 1):
        row[j] = row[j - 1] + _indel(word, j - 1)
    prev2: list[float] = []
    for i in range(1, n + 1):
        cur = [row[0] + _indel(typed, i - 1)] + [0.0] * m
        for j in range(1, m + 1):
            cur[j] = min(row[j] + _indel(typed, i - 1), cur[j - 1] + _indel(word, j - 1),
                         row[j - 1] + _substitution(typed[i - 1], word[j - 1]))
            if i > 1 and j > 1 and typed[i - 1] == word[j - 2] and typed[i - 2] == word[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 0.5)
        prev2, row = row, cur
    return row[m]


_ALPHABET = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
# Endings of nouns and adjectives: words that differ only here may be two forms of one word.
_INFLECTIONS = sorted(set("""ыми ими ами ями ого его ому ему ой ей ий ый ая яя ое ее ые ие ом ем ах ях ов ев ам ям ую юю
    ою ею ью ым им ых их а я о е ы и у ю ь й""".split()), key=len, reverse=True)


def inflection_base(word: str) -> str:
    return next((word[:-len(end)] for end in _INFLECTIONS if word.endswith(end) and len(word) - len(end) >= 3), word)


def inflection_variant(a: str, b: str) -> bool:
    """«шестигранных» and «шестигранной» differ only in an inflection ending."""
    return inflection_base(a) == inflection_base(b)


def dictionary_neighbours(word: str) -> set[str]:
    """Real Russian words one edit (insert, delete, replace or swap) away; about 1 ms for a 6-letter word."""
    out = set()
    for i in range(len(word) + 1):
        if i < len(word):
            out.add(word[:i] + word[i + 1:])
        if i < len(word) - 1:
            out.add(word[:i] + word[i + 1] + word[i] + word[i + 2:])
        for char in _ALPHABET:
            out.add(word[:i] + char + word[i:])
            if i < len(word):
                out.add(word[:i] + char + word[i + 1:])
    out.discard(word)
    return {candidate for candidate in out if len(candidate) >= 3 and common_word(candidate)}


_PROPER = {"Name", "Surn", "Patr", "Geox", "Orgn", "Trad", "Abbr", "Init"}


def common_word(word: str) -> bool:
    """A dictionary word that is not only a name, place or abbreviation («самсунг» is not «Самсун»)."""
    return known_word(word) and any(parse.is_known and not _PROPER & parse.tag.grammemes
                                    for parse in _morph().parse(word))


@cache
def _morph() -> pymorphy3.MorphAnalyzer:
    return pymorphy3.MorphAnalyzer()  # Loaded on first use: about half a second and 15 MB.


@lru_cache(maxsize=65536)
def known_word(word: str) -> bool:
    """A real Russian word form (any inflection), so it must not be rewritten as a typo."""
    return _morph().word_is_known(word)


@lru_cache(maxsize=65536)
def noun_lemma(word: str) -> str | None:
    """Dictionary form of a noun whose stem differs from the word's («курток» → «куртка»), else None."""
    if len(word) < 4:
        return None
    for parse in _morph().parse(word)[:2]:
        if parse.tag.POS == "NOUN" and parse.score >= 0.3 and parse.normal_form != word:
            return parse.normal_form
    return None


def counted_phrase(words: list[str]) -> str | None:
    """The counted things in the genitive plural, as a characteristic names them: «выдвижных ящика» → «выдвижных
    ящиков», «полками» → «полок». Adjectives are kept up to the first noun; None without a noun."""
    out = []
    for word in words[:3]:
        parse = next((p for p in _morph().parse(word) if p.tag.POS in ("NOUN", "ADJF")), None)
        if parse is None:
            return None
        form = parse.inflect({"gent", "plur"})
        out.append(form.word if form else word)
        if parse.tag.POS == "NOUN":
            return " ".join(out)
    return None
