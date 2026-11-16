"""Typing-noise repairs that change word boundaries or script: transliteration, spaced letters, glued words."""

import re
from collections.abc import Callable
from functools import cache

from src.ktru.engine.spelling import common_word

_MULTI = (("shch", "щ"), ("sch", "щ"), ("yo", "ё"), ("yu", "ю"), ("ya", "я"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"),
          ("ch", "ч"), ("sh", "ш"), ("ye", "е"))  # fmt: skip
# «-yy», «-iy», «-oy» spell adjective endings only at the end of a word («naya» is «ная», not «на»+«йа»).
_ENDINGS = (("yy", "ый"), ("iy", "ий"), ("ey", "ей"), ("ay", "ай"), ("oy", "ой"), ("uy", "уй"))
_SINGLE = {"a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е", "z": "з", "i": "и", "j": "й", "k": "к", "l": "л",
           "m": "м", "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "t": "т", "u": "у", "f": "ф", "h": "х", "c": "ц",
           "y": "ы", "q": "к", "w": "в", "x": "кс", "'": "ь"}  # fmt: skip
_VARIANTS = ({}, {"y": "й"}, {"y": "ь"}, {"e": "э"}, {"i": "ы"}, {"@soft": "ь"})  # "@soft": word-final ь («drel» → «дрель»)
_UNITS = {"mm": "мм", "sm": "см", "cm": "см", "kg": "кг", "gr": "г", "g": "г", "vt": "Вт", "v": "В", "ml": "мл", "l": "л"}


def _translit(word: str, variant: dict[str, str]) -> str:
    out, i, w = [], 0, word.lower()
    while i < len(w):
        ending = next((cyr for latin, cyr in _ENDINGS if i == len(w) - 2 and w.endswith(latin)), None)
        if ending:
            out.append(ending)
            break
        for latin, cyr in _MULTI:
            if w.startswith(latin, i):
                out.append(cyr)
                i += len(latin)
                break
        else:
            out.append(variant.get(w[i], _SINGLE.get(w[i], w[i])))
            i += 1
    if variant.get("@soft") and out and w[-1] in "lt":
        out.append("ь")
    return "".join(out)


def transliterate(word: str) -> str | None:
    """Russian reading of a word typed in Latin letters («derevyannyy» → «деревянный»), only if it is a real word."""
    if not word.isascii() or not word.isalpha():
        return None
    if word.lower() in _UNITS:
        return _UNITS[word.lower()]
    if len(word) < 3:
        return None
    for variant in _VARIANTS:
        candidate = _translit(word, variant)
        if common_word(candidate):
            return candidate[:1].upper() + candidate[1:] if word[:1].isupper() else candidate
    return None


_SPACED = re.compile(r"(?<![А-Яа-яЁё])[А-Яа-яЁё](?: [А-Яа-яЁё]){3,}(?![А-Яа-яЁё])")
_GLUED = re.compile(r"[А-Яа-яЁё]{9,}")


_SHORT_WORDS = {"без", "для", "над", "под", "при", "про", "или", "как", "все", "что", "это", "ещё", "уже"}


def segment(letters: str, is_word: Callable[[str], bool]) -> list[str] | None:
    """Fewest dictionary words that spell `letters` exactly, or None («курткасиняя» → куртка синяя).

    Pieces are words of four or more letters (or a short function word): a typo'd long word must not
    fall apart into «метали чес кий».
    """
    n, low = len(letters), letters.lower()

    @cache
    def best(start: int) -> tuple[str, ...] | None:
        if start == n:
            return ()
        options = []
        for end in range(min(n, start + 24), start + 2, -1):
            piece = low[start:end]
            if (len(piece) >= 4 or piece in _SHORT_WORDS) and is_word(piece) and (tail := best(end)) is not None:
                options.append((letters[start:end], *tail))
        return min(options, key=len) if options else None

    result = best(0)
    return list(result) if result and len(result) <= 6 else None
