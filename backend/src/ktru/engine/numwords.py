"""Spelled-out Russian numerals → decimal strings with original character offsets.

``spans(text)`` tokenises the text into words and greedily parses numeral phrases:

* additive numerals merge from the highest rank to the lowest («сто двадцать пять» → 125), so two
  independent units never merge («семь пять» stays two spans);
* тысяча/миллион/миллиард multiply the part collected before them («две тысячи двадцать пять» → 2025);
* decimals use «запятая» («семь запятая пять» → 7.5) or «целых … десятых/сотых/тысячных»
  («две целых пять десятых» → 2.5);
* «полтора/полторы» → 1.5 (times a following тысяча/миллион), «половина/пол» → 0.5 as standalone words.

Matching works on a lowercased, ё→е copy, but every returned span points into the original text.
Pure stdlib, a single token scan; the token pattern has no nested quantifiers.
"""

import re
from decimal import Decimal
from typing import NamedTuple

__all__ = ["spans"]

_TOKEN_RE = re.compile(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*")
_TEN = Decimal(10)

_ADD = "add"
_MULT = "mult"
_ZERO = "zero"
_COMMA = "comma"
_WHOLE = "whole"
_SCALE = "scale"
_HALF = "half"
_ONE_HALF = "one_half"

# (value, merge rank, forms): a lower rank may follow a higher one inside one number,
# units 1 < teens 2 < tens 3 < hundreds 4.
_ADDITIVE_FORMS: tuple[tuple[int, int, str], ...] = (
    (1, 1, "один одна одно одного одной одному одним одном одну одни одних одними одною"),
    (2, 1, "два две двух двум двумя двое двоих двоим двоими"),
    (3, 1, "три трёх трех трём трем тремя"),
    (4, 1, "четыре четырёх четырех четырём четырем четырьмя"),
    (5, 1, "пять пяти пятью"),
    (6, 1, "шесть шести шестью"),
    (7, 1, "семь семи семью"),
    (8, 1, "восемь восьми восемью восьмью"),
    (9, 1, "девять девяти девятью"),
    (10, 2, "десять десяти десятью"),
    (11, 2, "одиннадцать одиннадцати одиннадцатью"),
    (12, 2, "двенадцать двенадцати двенадцатью"),
    (13, 2, "тринадцать тринадцати тринадцатью"),
    (14, 2, "четырнадцать четырнадцати четырнадцатью"),
    (15, 2, "пятнадцать пятнадцати пятнадцатью"),
    (16, 2, "шестнадцать шестнадцати шестнадцатью"),
    (17, 2, "семнадцать семнадцати семнадцатью"),
    (18, 2, "восемнадцать восемнадцати восемнадцатью"),
    (19, 2, "девятнадцать девятнадцати девятнадцатью"),
    (20, 3, "двадцать двадцати двадцатью"),
    (30, 3, "тридцать тридцати тридцатью"),
    (40, 3, "сорок сорока"),
    (50, 3, "пятьдесят пятидесяти пятьюдесятью"),
    (60, 3, "шестьдесят шестидесяти шестьюдесятью"),
    (70, 3, "семьдесят семидесяти семьюдесятью"),
    (80, 3, "восемьдесят восьмидесяти восемьюдесятью восьмьюдесятью"),
    (90, 3, "девяносто девяноста"),
    (100, 4, "сто ста стам стами стах"),
    (150, 4, "полтораста полутораста"),
    (200, 4, "двести двухсот двумстам двумястами двухстах"),
    (300, 4, "триста трёхсот трехсот трёмстам тремстам тремястами трёхстах трехстах"),
    (400, 4, "четыреста четырёхсот четырехсот четырёмстам четыремстам четырьмястами четырёхстах четырехстах"),
    (500, 4, "пятьсот пятисот пятистам пятьюстами пятистах"),
    (600, 4, "шестьсот шестисот шестистам шестьюстами шестистах"),
    (700, 4, "семьсот семисот семистам семьюстами семистах"),
    (800, 4, "восемьсот восьмисот восьмистам восемьюстами восьмьюстами восьмистах"),
    (900, 4, "девятьсот девятисот девятистам девятьюстами девятистах"),
)

# (value, order, forms): multipliers go in descending order, миллиард → миллион → тысяча.
_MULT_FORMS: tuple[tuple[int, int, str], ...] = (
    (1000, 1, "тысяча тысячи тысяче тысячу тысячей тысяч тысячам тысячами тысячах тысячью"),
    (
        10**6,
        2,
        "миллион миллиона миллиону миллионом миллионе миллионы миллионов миллионам миллионами миллионах",
    ),
    (
        10**9,
        3,
        "миллиард миллиарда миллиарду миллиардом миллиарде миллиарды миллиардов "
        "миллиардам миллиардами миллиардах",
    ),
)

_ZERO_FORMS = (
    "ноль нуль ноля нуля нолю нулю нолем нулем ноле нуле ноли нули нолей нулей "
    "нолям нулям нолями нулями нолях нулях"
)
_COMMA_FORMS = "запятая запятой запятую"
_WHOLE_FORMS = "целая целой целую целых целые целыми целое целого целому целым целом"
_SCALE_FORMS: tuple[tuple[int, str], ...] = (
    (10, "десятая десятой десятую десятых десятые десятыми"),
    (100, "сотая сотой сотую сотых сотые сотыми"),
    (1000, "тысячная тысячной тысячную тысячных тысячные тысячными"),
)
_HALF_FORMS = "половина половины половине половину половиной половиною половин пол"
_ONE_HALF_FORMS = "полтора полторы полутора полуторы"


class _Entry(NamedTuple):
    value: Decimal
    kind: str
    rank: int


class _Tok(NamedTuple):
    word: str
    start: int
    end: int
    joined: bool  # the gap after the previous token contains whitespace only


def _norm(word: str) -> str:
    return word.lower().replace("ё", "е")


def _build_lexicon() -> dict[str, _Entry]:
    lex: dict[str, _Entry] = {}

    def put(forms: str, value: Decimal, kind: str, rank: int = 0) -> None:
        entry = _Entry(value, kind, rank)
        for form in forms.split():
            lex[_norm(form)] = entry

    for value, rank, forms in _ADDITIVE_FORMS:
        put(forms, Decimal(value), _ADD, rank)
    for value, rank, forms in _MULT_FORMS:
        put(forms, Decimal(value), _MULT, rank)
    put(_ZERO_FORMS, Decimal(0), _ZERO)
    put(_COMMA_FORMS, Decimal(0), _COMMA)
    put(_WHOLE_FORMS, Decimal(0), _WHOLE)
    for value, forms in _SCALE_FORMS:
        put(forms, Decimal(value), _SCALE)
    put(_HALF_FORMS, Decimal("0.5"), _HALF)
    put(_ONE_HALF_FORMS, Decimal("1.5"), _ONE_HALF)
    return lex


_LEX = _build_lexicon()


def _tokenize(text: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    prev_end = 0
    for match in _TOKEN_RE.finditer(text):
        gap = text[prev_end : match.start()]
        tokens.append(_Tok(_norm(match.group(0)), match.start(), match.end(), not gap.strip()))
        prev_end = match.end()
    return tokens


def _lookup(tokens: list[_Tok], i: int) -> _Entry | None:
    return _LEX.get(tokens[i].word)


def _parse_additive(tokens: list[_Tok], i: int) -> tuple[Decimal, int] | None:
    """Longest run of additive numerals: «сто двадцать пять» → 125; stops before a second unit."""
    entry = _lookup(tokens, i)
    if entry is None or entry.kind != _ADD:
        return None
    value, last, j = entry.value, entry.rank, i + 1
    n = len(tokens)
    while j < n and tokens[j].joined:
        nxt = _lookup(tokens, j)
        if nxt is None or nxt.kind != _ADD or nxt.rank >= last:
            break
        value += nxt.value
        last = nxt.rank
        j += 1
    return value, j


def _parse_number(tokens: list[_Tok], i: int) -> tuple[Decimal, int] | None:
    """A number possibly with thousands/millions: «две тысячи сто двадцать пять» → 2125."""
    entry = _lookup(tokens, i)
    if entry is None:
        return None
    if entry.kind == _ZERO:
        return Decimal(0), i + 1
    if entry.kind not in (_ADD, _MULT):
        return None
    total = Decimal(0)
    sub: Decimal | None = None
    last_add = 0
    last_mult = 0
    j = i
    n = len(tokens)
    while j < n:
        if j > i and not tokens[j].joined:
            break
        e = _lookup(tokens, j)
        if e is None:
            break
        if e.kind == _ADD:
            if sub is None:
                sub, last_add = e.value, e.rank
            elif e.rank < last_add:
                sub += e.value
                last_add = e.rank
            else:
                break
        elif e.kind == _MULT:
            if last_mult and e.rank >= last_mult:
                break
            total += (sub if sub is not None else Decimal(1)) * e.value
            sub, last_add, last_mult = None, 0, e.rank
        else:
            break
        j += 1
    return total + (sub if sub is not None else Decimal(0)), j


def _parse_fraction(tokens: list[_Tok], i: int) -> tuple[Decimal, int, bool, int] | None:
    """The part after «запятая»/«целых»: (value, digit count, had leading zeros, next index)."""
    zeros = 0
    j = i
    n = len(tokens)
    while j < n and (j == i or tokens[j].joined):
        e = _lookup(tokens, j)
        if e is None or e.kind != _ZERO:
            break
        zeros += 1
        j += 1
    group = _parse_additive(tokens, j) if j < n else None
    if group is None:
        if zeros:
            return Decimal(0), zeros, True, j
        return None
    value, k = group
    return value, zeros + len(str(int(value))), bool(zeros), k


def _parse_phrase(tokens: list[_Tok], i: int) -> tuple[Decimal, int] | None:
    """A number, optionally followed by «запятая …», «целых … десятых» or a bare scale word."""
    parsed = _parse_number(tokens, i)
    if parsed is None:
        return None
    value, j = parsed
    n = len(tokens)
    if j >= n or not tokens[j].joined:
        return value, j
    nxt = _lookup(tokens, j)
    if nxt is None:
        return value, j
    if nxt.kind == _COMMA:
        frac = _parse_fraction(tokens, j + 1)
        if frac is None:
            return value, j
        frac_value, digits, _zeros, k = frac
        return value + frac_value / (_TEN**digits), k
    if nxt.kind == _WHOLE:
        frac = _parse_fraction(tokens, j + 1)
        if frac is not None:
            frac_value, digits, had_zeros, k = frac
            scale = _lookup(tokens, k) if k < n and tokens[k].joined else None
            if scale is not None and scale.kind == _SCALE:
                divisor = _TEN**digits if had_zeros else scale.value
                return value + frac_value / divisor, k + 1
        return value, j + 1  # «две целых» without a named scale is just 2
    if nxt.kind == _SCALE:
        return value / nxt.value, j + 1  # «пять сотых» → 0.05
    return value, j


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


def spans(text: str) -> list[tuple[int, int, str]]:
    """Return (start, end, decimal string) for every spelled-out number in ``text``.

    Offsets always refer to ``text``; matching ignores case and treats ё as е.
    """
    tokens = _tokenize(text)
    found: list[tuple[int, int, str]] = []
    i = 0
    n = len(tokens)
    while i < n:
        entry = _lookup(tokens, i)
        if entry is None:
            i += 1
            continue
        if entry.kind in (_ADD, _MULT, _ZERO):
            parsed = _parse_phrase(tokens, i)
            if parsed is not None:
                value, k = parsed
                found.append((tokens[i].start, tokens[k - 1].end, _fmt(value)))
                i = k
                continue
        elif entry.kind in (_HALF, _ONE_HALF):
            value, k = entry.value, i + 1
            if k < n and tokens[k].joined:
                nxt = _lookup(tokens, k)
                if nxt is not None and nxt.kind == _MULT:
                    value, k = value * nxt.value, k + 1
            found.append((tokens[i].start, tokens[k - 1].end, _fmt(value)))
            i = k
            continue
        i += 1
    return found
