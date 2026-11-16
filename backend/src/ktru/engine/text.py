"""Deterministic Russian text utilities: layout repair, light stemming, quantity and unit parsing."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+(?:-[A-Za-zА-Яа-яЁё]+)*")

_EN = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./" + '~QWERTYUIOP{}ASDFGHJKL:"ZXCVBNM<>?'
_RU = "ёйцукенгшщзхъфывапролджэячсмитьбю." + "ЁЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,"
LAYOUT = str.maketrans(_EN, _RU)

STOP = {
    "и", "в", "во", "на", "с", "со", "из", "для", "по", "к", "от", "до", "не", "без", "а", "или", "при", "под", "над",
    "о", "об", "у", "за", "то", "же", "что", "это", "как", "самого", "самой", "самый", "типа", "тип", "вид",
}  # fmt: skip

_ENDINGS = sorted(
    """ыми ими ами ями ого его ому ему ой ей ий ый ая яя ое ее ые ие ом ем ах ях ов ев ам ям ую юю ою ею ью
    иями ость ости остью а я о е ы и у ю ь й""".split(),
    key=len,
    reverse=True,
)


def norm(s: str) -> str:
    return s.lower().replace("ё", "е")


_LOOKALIKE = str.maketrans("aceopxykmthb", "асеорхукмтнв")


def fold(s: str) -> str:
    """Same-length comparison form: lower case, ё → е, Latin look-alike letters as Cyrillic («o-образная»)."""
    return norm(s).translate(_LOOKALIKE)


# «Масштаб: 1:100»: a ratio is one value, not the numbers 1 and 100.
RATIO_RE = re.compile(r"(?<![\d.,:])\d+(?:[.,]\d+)?:\d+(?:[.,]\d+)?(?![\d.,:])")


def code_like(label: str) -> bool:
    """A value whose identity is its exact spelling: «L2», «A0», «O-образная», «x86», «Не более 250», «1:100»."""
    if RATIO_RE.fullmatch(label.strip()):
        return True
    return bool(re.search(r"[^\W\d_]", label)) and any(
        len(p) < 3 or any(ch.isdigit() or ch.isascii() for ch in p)
        for p in re.findall(r"[^\s-]+", label) if norm(p) not in STOP)


def stem(word: str) -> str:
    w = norm(word)
    for end in _ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= 3:
            return w[: -len(end)]
    return w


def words(s: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in WORD_RE.finditer(s)]


def content_stems(s: str) -> list[str]:
    """Stems of meaningful words; hyphenated compounds count as their parts («Дрель-шуруповерт»)."""
    parts = [p for w, _, _ in words(s) for p in w.split("-")]
    return [stem(p) for p in parts if norm(p) not in STOP and len(p) > 1]


# Measures, containers and generic nouns lead many descriptions («5 пар перчаток», «набор из 6 сверл», «два изделия:
# халат…») but are never the product. Stems come from the same stemmer the heads do.
NOT_A_PRODUCT = {stem(w) for w in (
    "пара пары пар штука штук шт упаковка коробка короб пачка мешок ящик набор комплект "
    "изделие изделия товар продукция позиция предмет вещь").split()}
_ADJECTIVE_ENDINGS = ("ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ого", "его", "ому", "ему", "ую", "юю",
                      "ых", "их", "ым", "им", "ыми", "ими")


@lru_cache(maxsize=65536)
def is_adjective(word: str) -> bool:
    """Adjective or participle by dictionary morphology («Токарные», «Защитный»); a noun ending like one stays a
    noun («лезвие», «покрытие», «оборудование»). Unknown words fall back to the ending."""
    from src.ktru.engine.spelling import _morph  # noqa: PLC0415 - spelling loads the dictionary lazily

    known = [p for p in _morph().parse(word) if p.is_known]
    if known:
        return known[0].tag.POS in ("ADJF", "ADJS", "PRTF", "PRTS")
    return len(word) > 4 and word.endswith(_ADJECTIVE_ENDINGS)


@lru_cache(maxsize=65536)
def lemma_stem(word: str) -> str:
    """Stem of the dictionary form, so «резцы» and «резец» (a fleeting vowel) share one key."""
    from src.ktru.engine.spelling import _morph  # noqa: PLC0415

    n = norm(word)
    nouns = [p for p in _morph().parse(n) if p.is_known and p.tag.POS == "NOUN"]
    return stem(nouns[0].normal_form) if nouns else stem(n)


def head_noun(name: str) -> str | None:
    """The first noun of a product name: «Навесной замок с дужкой» → «замок»."""
    for w, _, _ in words(name):
        n = norm(w)
        if n in STOP or len(n) < 3 or is_adjective(n):
            continue
        return n
    return None


def head_noun_stem(name: str) -> str | None:
    """Stem of the first noun of a product name: «Навесной замок с дужкой» → «замок»."""
    n = head_noun(name)
    stems_ = content_stems(n) if n else []
    return stems_[0] if stems_ else None


# Everyday names → the catalog's head noun (search only; the user's text is never rewritten).
# Keys are word prefixes; values are the words the catalog uses (KTRU names classes, not brands or slang:
# «Джемпер», «Обувь повседневная…», «Головные уборы», «Шлифмашина»).
ALIASES = {
    "бокорез": "кусачки",
    "пассатиж": "плоскогубцы",
    "ножовк": "пила ручная",
    "болгарк": "шлифмашина",
    "шлифовальн": "шлифмашина",
    "шуруповерт": "шуруповерт",
    "лобзик": "электролобзик",
    "паяльник": "электропаяльник",
    "толстовк": "джемпер",
    "худи": "толстовка",
    "свитер": "джемпер",
    "кардиган": "джемпер",
    "пуловер": "джемпер",
    "водолазк": "толстовка",
    "пуховик": "куртка",
    "ветровк": "куртка",
    "плащ": "одежда верхняя прочая",
    "дождевик": "одежда верхняя прочая",
    "ботинк": "обувь",
    "ботинок": "обувь",
    "полуботинк": "обувь",
    "туфл": "обувь",
    "сандал": "обувь",
    "мокасин": "обувь",
    "балетк": "обувь",
    "берц": "обувь",
    "бейсболк": "головные уборы",
    "кепк": "головные уборы",
    "панам": "головные уборы",
    "берет": "головные уборы",
    "рубашк": "сорочка",
    "майк": "белье нательное",
    "трус": "белье нательное",
    "тумбочк": "тумба",
    "столик": "стол",
    "тельник": "тельняшка",
    "тапк": "обувь",
    "тапочк": "обувь",
    "шлепанц": "обувь",
    "шлепк": "обувь",
    "сланц": "обувь",
    "валенк": "обувь",
    "галош": "обувь",
    "калош": "обувь",
    "шкафчик": "шкаф",
    # Furniture hardware sold by its own name; KTRU files it under «Фурнитура для мебели металлическая».
    "стяжк": "фурнитура для мебели",
    "шкант": "фурнитура для мебели",
    "полкодержател": "фурнитура для мебели",
    "зеркалодержател": "фурнитура для мебели",
    "стеклодержател": "фурнитура для мебели",
    "шпингалет": "фурнитура для мебели",
    "подстоль": "фурнитура для мебели",
    "рейлинг": "фурнитура для мебели",
    "уголок": "фурнитура для мебели",
    "муфт": "фурнитура для мебели",
    "заглушк": "фурнитура для мебели",
    "зацеп": "фурнитура для мебели",
    "стульчик": "стул",
    "полочк": "полка",
    "скамеечк": "скамейка",
    "табурет": "табурет",
    "аккум": "аккумулятор",
}
# Short words that are prefixes of unrelated words («поло» ↔ «половина»), so only the whole word counts.
WHOLE_WORD_ALIASES = {"поло": "футболка", "ушм": "шлифмашина"}


def alias_of(word: str) -> str | None:
    n = norm(word)
    return next((v for k, v in ALIASES.items() if n.startswith(k)), None) or WHOLE_WORD_ALIASES.get(n)


def with_aliases(text: str) -> str:
    extra = [v for p in text.replace("-", " ").split() if (v := alias_of(p))]
    return f"{text} {' '.join(dict.fromkeys(extra))}" if extra else text


# Three letters past a whole stem make the same word only as a diminutive or an adjective suffix:
# «шкаф» → «шкафчик», «дерево» → «деревянный», but «камера» is not «камертон».
_DIMINUTIVE = {"чик", "ечк", "очк", "янн", "енн", "анн", "овн", "евн", "ичн", "очн", "ечн", "ист", "ова", "ева"}


def stems_match(a: str, b: str) -> bool:
    """Two stems match if they share a long enough prefix (handles case/gender endings and сиденье/сидения)."""
    if a == b:
        return True
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    shorter, longer = min(len(a), len(b)), max(len(a), len(b))
    if longer - n > 3:  # «стол» is not «столешница», «штук» is not «штукатурный»
        return False
    if n == shorter and longer - n == 3 and (a if len(a) > len(b) else b)[n:] not in _DIMINUTIVE:
        return False
    # Long derivations share a long root: регулируемый / регулировка.
    return n >= max(4, shorter - 1) or (shorter >= 8 and n >= shorter - 3)


def word_has_label(text_stem: str, label_stem: str) -> bool:
    """Text word carries a catalog label: same word, or an adjective built on it (металлический ← «Металл»)."""
    return stems_match(text_stem, label_stem) or (len(label_stem) >= 5 and text_stem.startswith(label_stem))


# Latin letters that look like Cyrillic ones («Cтол» typed with a Latin C).
_LOOKALIKE = str.maketrans("AaBCcEeHKkMOoPpTXxy", "АаВСсЕеНКкМОоРрТХху")
_SCRIPTS = re.compile(r"[A-Za-zА-Яа-яЁё]+")


def fix_homoglyphs(text: str) -> str:
    """Latin lookalikes inside Cyrillic words become Cyrillic; the length never changes."""
    def fix(m: re.Match) -> str:
        token = m.group(0)
        latin = [c for c in token if c.isascii()]
        if not latin or len(latin) == len(token) or any(c not in "AaBCcEeHKkMOoPpTXxy" for c in latin):
            return token
        return token.translate(_LOOKALIKE)
    return _SCRIPTS.sub(fix, text)


def fix_layout(text: str) -> str | None:
    """Return the Cyrillic reading of text typed in the wrong (QWERTY) layout, or None if it is not that case.

    Latin lookalikes inside Cyrillic words are repaired in place when the layout itself is fine.
    """
    latin = sum(1 for c in text if "a" <= c.lower() <= "z")
    cyr = sum(1 for c in text if "а" <= c.lower() <= "я" or c in "ёЁ")
    if latin < 6 or cyr > latin * 0.2:
        fixed = fix_homoglyphs(text)
        return fixed if fixed != text else None
    # Keep model/article tokens (letters mixed with digits) as typed.
    out = []
    for tok in re.split(r"(\s+)", text):
        out.append(tok if any(ch.isdigit() for ch in tok) else tok.translate(LAYOUT))
    return "".join(out)


# ---- quantities ---------------------------------------------------------------------------------------------------

# unit token -> (okei code, dimension, factor to base unit of the dimension)
UNITS: dict[str, tuple[str, str, Decimal]] = {
    "мм": ("003", "length", Decimal(1)),
    "см": ("004", "length", Decimal(10)),
    "дм": ("005", "length", Decimal(100)),
    "м": ("006", "length", Decimal(1000)),
    "г": ("163", "mass", Decimal(1)),
    "гр": ("163", "mass", Decimal(1)),
    "грамм": ("163", "mass", Decimal(1)),
    "кг": ("166", "mass", Decimal(1000)),
    "т": ("168", "mass", Decimal(1000000)),
    "мл": ("111", "volume", Decimal(1)),
    "л": ("112", "volume", Decimal(1000)),
    "вт": ("212", "power", Decimal(1)),
    "квт": ("214", "power", Decimal(1000)),
    "в": ("222", "voltage", Decimal(1)),
    "%": ("744", "percent", Decimal(1)),
    "°c": ("280", "temperature", Decimal(1)),
    "шт": ("796", "piece_count", Decimal(1)),
    "а·ч": ("263", "charge", Decimal(1)),
    "ма·ч": ("263", "charge", Decimal("0.001")),
    "пар": ("715", "pair_count", Decimal(1)),
    "гц": ("290", "frequency", Decimal(1)),
    "кгц": ("291", "frequency", Decimal(1000)),
    "мгц": ("292", "frequency", Decimal(10**6)),
    "ггц": ("293", "frequency", Decimal(10**9)),
    "об/мин": ("331", "rotation", Decimal(1)),
    "кпа": ("297", "pressure", Decimal(1)),
    "мпа": ("298", "pressure", Decimal(1000)),
    "бар": ("309", "pressure", Decimal(100)),
    "°": ("2355", "angle", Decimal(1)),
    "дюйм": ("039", "length", Decimal("25.4")),
    "дб": ("3135", "sound", Decimal(1)),
    "вт·ч": ("243", "energy", Decimal(1)),
    "мм²": ("050", "area", Decimal(1)),
    "ом": ("274", "resistance", Decimal(1)),
    "а": ("260", "current", Decimal(1)),
    "кв": ("223", "voltage", Decimal(1000)),
    "н·м": (None, "torque", Decimal(1)),
    "г/м²": (None, "areal_density", Decimal(1)),
    "hrc": (None, "hardness", Decimal(1)),
    "мг": ("161", "mass", Decimal("0.001")),
    "км": ("008", "length", Decimal(10**6)),
    "нм": ("015", "length", Decimal("0.000001")),
    "пог. м": ("018", "length", Decimal(1000)),
    "км²": ("061", "area", Decimal(10**12)),
    "с": ("354", "time", Decimal(1)),
    "мин": ("355", "time", Decimal(60)),
    "ч": ("356", "time", Decimal(3600)),
    "сут": ("359", "time", Decimal(86400)),
    "мес": ("362", "time", Decimal(2629800)),
    "год": ("366", "time", Decimal(31557600)),
    "м³/ч": ("598", "volume_flow", Decimal(1)),
    "т/ч": ("534", "mass_flow", Decimal(1)),
    "м/с": ("328", "speed", Decimal(1)),
    "км/ч": ("333", "speed", Decimal(1000) / Decimal(3600)),
    "бит": ("254", "data", Decimal(1)),
    "мбайт": ("257", "data", Decimal(8 * 2**20)),
    "мб": ("257", "data", Decimal(8 * 2**20)),
    "гбайт": ("2553", "data", Decimal(8 * 2**30)),
    "гб": ("2553", "data", Decimal(8 * 2**30)),
    "бит/с": ("2541", "bitrate", Decimal(1)),
    "мбит/с": ("2545", "bitrate", Decimal(10**6)),
    "н": ("289", "force", Decimal(1)),
    "лк": ("283", "illuminance", Decimal(1)),
    "′": ("2356", "angle", Decimal(1) / Decimal(60)),
    "″": ("2357", "angle", Decimal(1) / Decimal(3600)),
    "л. с.": ("251", "power", Decimal("735.49875")),
    "л. с": ("251", "power", Decimal("735.49875")),
    "в·а": ("226", "apparent_power", Decimal(1)),
    "мбар": ("308", "pressure", Decimal("0.1")),
    "мм рт. ст.": ("338", "pressure", Decimal("0.133322")),
    "мм рт. ст": ("338", "pressure", Decimal("0.133322")),
    "кг/м³": ("316", "density", Decimal(1)),
}
OKEI: dict[str, tuple[str, Decimal, str]] = {
    "003": ("length", Decimal(1), "мм"),
    "004": ("length", Decimal(10), "см"),
    "005": ("length", Decimal(100), "дм"),
    "006": ("length", Decimal(1000), "м"),
    "163": ("mass", Decimal(1), "г"),
    "166": ("mass", Decimal(1000), "кг"),
    "168": ("mass", Decimal(1000000), "т"),
    "111": ("volume", Decimal(1), "мл"),
    "112": ("volume", Decimal(1000), "л"),
    "212": ("power", Decimal(1), "Вт"),
    "214": ("power", Decimal(1000), "кВт"),
    "222": ("voltage", Decimal(1), "В"),
    "744": ("percent", Decimal(1), "%"),
    "280": ("temperature", Decimal(1), "°C"),
    "263": ("charge", Decimal(1), "А·ч"),
    "290": ("frequency", Decimal(1), "Гц"),
    "291": ("frequency", Decimal(1000), "кГц"),
    "292": ("frequency", Decimal(10**6), "МГц"),
    "293": ("frequency", Decimal(10**9), "ГГц"),
    "331": ("rotation", Decimal(1), "об/мин"),
    "297": ("pressure", Decimal(1), "кПа"),
    "298": ("pressure", Decimal(1000), "МПа"),
    "309": ("pressure", Decimal(100), "бар"),
    "2355": ("angle", Decimal(1), "°"),
    "039": ("length", Decimal("25.4"), "дюйм"),
    "3135": ("sound", Decimal(1), "дБ"),
    "243": ("energy", Decimal(1), "Вт·ч"),
    "050": ("area", Decimal(1), "мм²"),
    "274": ("resistance", Decimal(1), "Ом"),
    "260": ("current", Decimal(1), "А"),
    "223": ("voltage", Decimal(1000), "кВ"),
    # Without these the catalog value domain is unverified and never narrows: 3744 in-group values are «Штука»
    # («Количество полок», «Количество выдвижных ящиков»).
    "796": ("piece_count", Decimal(1), "шт"),
    "715": ("pair_count", Decimal(1), "пар"),
    "161": ("mass", Decimal("0.001"), "мг"),
    "008": ("length", Decimal(10**6), "км"),
    "015": ("length", Decimal("0.000001"), "нм"),
    "018": ("length", Decimal(1000), "пог. м"),
    "061": ("area", Decimal(10**12), "км²"),
    "354": ("time", Decimal(1), "с"),
    "355": ("time", Decimal(60), "мин"),
    "356": ("time", Decimal(3600), "ч"),
    "359": ("time", Decimal(86400), "сут"),
    "362": ("time", Decimal(2629800), "мес"),  # 1/12 of a Julian year: 12 months equal 1 year
    "366": ("time", Decimal(31557600), "год"),
    "598": ("volume_flow", Decimal(1), "м³/ч"),
    "534": ("mass_flow", Decimal(1), "т/ч"),
    "328": ("speed", Decimal(1), "м/с"),
    "333": ("speed", Decimal(1000) / Decimal(3600), "км/ч"),
    "254": ("data", Decimal(1), "бит"),
    "257": ("data", Decimal(8 * 2**20), "Мбайт"),
    "2553": ("data", Decimal(8 * 2**30), "Гбайт"),
    "2541": ("bitrate", Decimal(1), "бит/с"),
    "2545": ("bitrate", Decimal(10**6), "Мбит/с"),
    "289": ("force", Decimal(1), "Н"),
    "283": ("illuminance", Decimal(1), "лк"),
    "2356": ("angle", Decimal(1) / Decimal(60), "′"),
    "2357": ("angle", Decimal(1) / Decimal(3600), "″"),
    "251": ("power", Decimal("735.49875"), "л. с."),
    "226": ("apparent_power", Decimal(1), "В·А"),
    "308": ("pressure", Decimal("0.1"), "мбар"),
    "338": ("pressure", Decimal("0.133322"), "мм рт. ст."),
    "316": ("density", Decimal(1), "кг/м³"),
}
# «массив дерева», «массажный», «весенний», «весы» only look like «масса»/«вес»: they never name the property.
NOT_PROPERTY_WORD = re.compile(r"масс(?:ив|аж|ов)|вес(?:н|ен|ел|л|ь|ы|ов|т)")
# «Количество полок: 4» states pieces without writing «шт»: a bare number is a count in the characteristic's unit.
COUNT_DIMENSIONS = frozenset({"piece_count", "pair_count"})

NUM = r"[+\-−]?(?:\d+(?:[.,]\d+)?[eE][+\-]?\d+|(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?!\d)|\d+)(?:[.,]\d+)?)"  # «1 000», «1e3»
_NB = r"(?<![А-Яа-яЁёA-Za-z])"  # operators and units are whole words
OPS = (
    rf"{_NB}(не\s+менее|не\s+более|не\s+ниже|не\s+выше|не\s+больше|не\s+меньше|более|менее|свыше|больше|меньше"
    r"|от|до|≥|<=|>=|≤|<|>)"
)
# Area/composite units (м², г/м2, л.с.) are not lengths/masses/volumes: the lookahead rejects them.
UNIT_WORDS = (("миллиметр", "мм"), ("сантиметр", "см"), ("дециметр", "дм"), ("метр", "м"), ("килограмм", "кг"),
              ("грамм", "г"), ("миллилитр", "мл"), ("литр", "л"), ("киловатт", "квт"), ("ватт", "вт"),
              ("вольт", "в"), ("процент", "%"), ("тонн", "т"))
# Composite units come first and keep their own dimension: «г/м²» is a fabric density, «Н·м» a torque, «об/мин» a speed.
# Ampere «А» and kilovolt «кВ» are matched in their own case only: «а» is a conjunction, «кв.» a square measure.
_COMPOSITE_UNITS = (r"(?-i:mAh|Ah|kW|W|V|Hz)|mm|cm|kg|rpm|hrc|г\s?/\s?(?:м²|м2|кв\.?\s?м)|об\s?/\s?мин|км\s?/\s?ч|м\s?/\s?с|м[³3]\s?/\s?ч|мбар|мг|км|гб|мб|н\s?[·*.]\s?м|вт\s?[·*.]\s?ч|ма\s?[·*.]?\s?ч|"
                    r"а\s?[·*.]?\s?ч|кгц|мгц|ггц|гц|кпа|мпа|бар|дюйм[а-яё]*|дб|мм²|мм2|ом")
UNIT_RE = (r"(" + _COMPOSITE_UNITS + r"|(?:" + "|".join(w for w, _ in UNIT_WORDS) + r")[а-яё]*|°\s?[cс]|°|ампер-час[а-я]*|шт|штук[аи]?|пар[аы]?|мм|см|дм|квт|кг|гр|грамм|мл|вт|(?-i:кВ)|м|г|т|л|в|(?-i:А)|%)"
           r"(?!\.?\s*с\.)\.?(?![А-Яа-яЁёA-Za-z/²³\d])")


_LATIN_UNITS = {"v": "в", "w": "вт", "kw": "квт", "ah": "а·ч", "mah": "ма·ч", "mm": "мм", "cm": "см", "kg": "кг",
                "hz": "гц", "rpm": "об/мин", "hrc": "hrc"}


def _unit(raw: str | None) -> str | None:
    """Unit token as an abbreviation: «килограмма» → «кг»."""
    u = (raw or "").rstrip(".").lower()
    if not u:
        return None
    squeezed = re.sub(r"[\s·*.]", "", u)
    if u.startswith("°"):
        return "°c" if squeezed in ("°c", "°с") else "°"
    if u.startswith("шт"):
        return "шт"
    if squeezed in ("ач", "ампер-час") or u.startswith(("а·", "а ·", "ампер-час")):
        return "а·ч"
    if squeezed == "мач":
        return "ма·ч"
    if squeezed == "втч":
        return "вт·ч"
    if squeezed == "нм" and squeezed != u:
        return "н·м"
    if u.startswith("г") and "/" in u:
        return "г/м²"
    if u.startswith("об"):
        return "об/мин"
    if u.startswith("дюйм") or u in ('"', "″"):
        return "дюйм"
    if squeezed in ("мм²", "мм2"):
        return "мм²"
    if u.startswith("пар"):
        return "пар"
    if squeezed in ("км/ч", "м/с", "м³/ч", "м3/ч"):
        return squeezed.replace("3", "³")
    if squeezed in _LATIN_UNITS:  # «18V», «2.0Ah», «750W»: unit symbols typed in Latin
        return _LATIN_UNITS[squeezed]
    return next((abbr for word, abbr in UNIT_WORDS if u.startswith(word)), u)
QTY_RE = re.compile(
    rf"(?:{OPS}\s*)?(?P<a>{NUM})\s*(?:(?P<unit_a>{UNIT_RE})\s*)?"
    rf"(?:(?P<sep>[-–—]|до|…|\.\.\.)\s*(?P<b>{NUM}))?\s*(?P<unit>{UNIT_RE})?",
    re.IGNORECASE,
)
DIM_RE = re.compile(
    rf"(?P<a>{NUM})\s*(?:[xх×*]|на(?=\s))\s*(?P<b>{NUM})(?:\s*(?:[xх×*]|на(?=\s))\s*(?P<c>{NUM}))?\s*(?P<unit>{UNIT_RE})?",
    re.IGNORECASE,
)
# «[0,5; 0,7] кг», «(0,5; 0,7] кг»: square brackets include the bound, round ones exclude it.
BRACKET_RE = re.compile(
    rf"(?P<o>[\[(])\s*(?P<a>{NUM})\s*[;…]\s*(?P<b>{NUM})\s*(?P<c>[\])])\s*(?P<unit>{UNIT_RE})?", re.IGNORECASE
)
# «20 ± 1 мм», «20 мм ± 1%», «20 мм плюс-минус 1».
TOL_RE = re.compile(
    rf"(?P<a>{NUM})\s*(?P<unit_a>{UNIT_RE})?\s*(?:±|\+/-|\+-|плюс[\s-]*минус)\s*(?P<d>{NUM})\s*(?P<unit>{UNIT_RE})?",
    re.IGNORECASE,
)
DIM_NAMES = ("длина", "ширина", "высота")
# «Ш1200 В750 Г600 мм», «Ш800хГ450хВ1800»: furniture sizes named by a letter. Two or more together, so a lone
# «В750» (a model name) stays text.
_LETTER_ITEM = r"[ШВГДшвгд]\.?\s?\d+(?:[.,]\d+)?"
LETTER_DIM_RE = re.compile(rf"(?<![А-Яа-яЁёA-Za-z]){_LETTER_ITEM}(?:\s*[xх×*,]?\s*{_LETTER_ITEM}){{1,3}}"
                           rf"(?:\s*(?P<unit>{UNIT_RE}))?")
FRACTION_RE = re.compile(r"(?<![\d/.,])(?P<a>\d{1,2})\s?/\s?(?P<b>[1-9]\d?)(?![\d/])\s*(?P<unit>дюйм[а-яё]*|[\"″])")
LETTER_ITEM_RE = re.compile(r"([ШВГДшвгд])\.?\s?(\d+(?:[.,]\d+)?)")
LETTER_NAMES = {"ш": "ширина", "в": "высота", "г": "глубина", "д": "длина"}
OP_MAP = {
    "не менее": "ge", "не ниже": "ge", "не меньше": "ge", "от": "ge", ">=": "ge", "≥": "ge",
    "не более": "le", "не выше": "le", "не больше": "le", "до": "le", "<=": "le", "≤": "le",
    "более": "gt", "больше": "gt", "свыше": "gt", ">": "gt",
    "менее": "lt", "меньше": "lt", "<": "lt",
}  # fmt: skip


IDENT_WORDS = {"гост", "ту", "тс", "тр", "iso", "en", "din", "арт", "артикул", "модель", "код", "серия", "сто", "снип"}
IDENT_PREFIXES = ("модел", "артикул", "арт", "код", "сери", "гост")
_INVISIBLE = "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
_TOKEN = re.compile(r"[^\s,;:()«»\"]+")


def identifier(token: str) -> bool:
    """«A12B», «СО-10», «AB-48», «7O0»: letters and digits in one token that is not a number with its unit."""
    t = token.strip(".")
    if not (re.search(r"\d", t) and re.search(r"[A-Za-zА-Яа-яЁё]", t)):
        return False
    letter, invisible = r"[A-Za-zА-Яа-яЁё]", f"[{_INVISIBLE}]*"
    # Letters touch digits («A12B», «у01»), or a code hyphen joins them («AB-48», «СО-10», «D-7»).
    # «р.48-50» and «№241» are abbreviations before a number, not identifiers.
    if not (re.search(rf"{letter}{invisible}\d|\d{invisible}{letter}", t)
            or re.fullmatch(rf"(?:[A-Za-z]{invisible})+-\d[\w-]*|[А-ЯЁ]{{2,}}-\d[\w-]*", t)):
        return False
    if re.fullmatch(rf"{NUM}(?:[xх×*]{NUM})+(?:{UNIT_RE})?", t, re.IGNORECASE):
        return False  # «50х60х70см» is a set of dimensions
    return not re.fullmatch(rf"{NUM}\s*{UNIT_RE}", t, re.IGNORECASE) and not re.fullmatch(NUM, t)


def dec(s: str | None) -> Decimal | None:
    if s is None:
        return None
    try:
        d = Decimal(re.sub(r"[ \u00a0\u202f]", "", s).replace(",", ".").strip())
    except InvalidOperation:
        return None
    if not d.is_finite() or len(s) > 100 or abs(d.adjusted()) > 64 or abs(int(d.as_tuple().exponent)) > 64:
        return None
    return d


@dataclass
class Quantity:
    start: int
    end: int
    raw: str
    op: str  # eq | ge | le | gt | lt | between
    a: Decimal
    b: Decimal | None
    unit_raw: str | None
    unit_okei: str | None
    dimension: str | None
    property_raw: str
    lo_inclusive: bool = True
    hi_inclusive: bool = True
    doubt: str | None = None  # why the reading is uncertain (kept, never silently fixed)


CLAUSE_SPLIT = re.compile(r"[,;:\n]|\.(?!\d)")


SENTENCE_SPLIT = re.compile(r"\n+|(?<=[.!?…])\s+")


def sentences(text: str) -> list[tuple[int, int]]:
    """Sentence bounds without the final punctuation; «0.5» and «1,25» never split a sentence."""
    out, start = [], 0
    for m in [*SENTENCE_SPLIT.finditer(text), None]:
        end = m.start() if m else len(text)
        piece = text[start:end]
        stripped = piece.rstrip().rstrip(".!?…").rstrip()
        lead = len(piece) - len(piece.lstrip())
        if stripped.strip():
            out.append((start + lead, start + len(stripped)))
        start = m.end() if m else len(text)
    return out


def clauses(text: str) -> list[tuple[int, int]]:
    out, start = [], 0
    depth = [0] * (len(text) + 1)
    for i, ch in enumerate(text):
        depth[i + 1] = max(0, depth[i] + (ch in "[(") - (ch in "])"))
    for m in CLAUSE_SPLIT.finditer(text):
        # A decimal comma (0,5) and an interval «[0,5; 0,7]» do not split a clause.
        i = m.start()
        if m.group(0) == "," and i > 0 and text[i - 1].isdigit() and text[i + 1 : i + 2].isdigit():
            continue
        if depth[i] > 0:
            continue
        out.append((start, i))
        start = m.end()
    out.append((start, len(text)))
    return [(s, e) for s, e in out if text[s:e].strip()]


_HEADER_START = re.compile(r"[;\n]|\.(?!\d)")


def _header_before(text: str, cs: int) -> str:
    """«Высота: 1300 мм», «Максимальное постоянное напряжение, U: 480»: the name before the colon owns the value."""
    head = text[:cs].rstrip()
    if not head.endswith(":"):
        return ""
    starts = [m.end() for m in _HEADER_START.finditer(head[:-1])]
    return " ".join(w for w, _, _ in words(head[starts[-1] if starts else 0:-1])[-10:])


def _qty(text, cs, start, end, op, a, b, unit_raw, prop) -> "Quantity":
    u = UNITS.get(unit_raw) if unit_raw else None
    return Quantity(
        start=cs + start,
        end=cs + end,
        raw=text[cs + start : cs + end].strip(),
        op=op,
        a=a,
        b=b,
        unit_raw=unit_raw,
        unit_okei=u[0] if u else None,
        dimension=u[1] if u else None,
        property_raw=prop,
    )


SIZE_ORDER_DOUBT = "Порядок трёх размеров не указан"
_SIZE_NAMES = {"длин": "длина", "ширин": "ширина", "высот": "высота", "глубин": "глубина"}


def _align_sizes(found: list[Quantity]) -> list[Quantity]:
    """«габариты 1200x750x450 мм … высота 750 мм, ширина 1200 мм»: sizes named elsewhere in the text give each
    number of an unordered triple its name. Unnamed numbers keep the order doubt."""
    def base(q: Quantity) -> Decimal | None:
        unit = UNITS.get(q.unit_raw) if q.unit_raw else None
        return q.a * unit[2] if unit and unit[1] == "length" else None

    named: dict[Decimal, set[str]] = {}
    for q in found:
        if q.doubt or q.op != "eq" or base(q) is None:
            continue
        last = norm(q.property_raw).split()[-1:] or [""]
        name = next((v for k, v in _SIZE_NAMES.items() if last[0].startswith(k)), None)
        if name:
            named.setdefault(base(q), set()).add(name)
    used: set[str] = set()
    for q in found:
        if q.doubt != SIZE_ORDER_DOUBT or base(q) is None:
            continue
        names = named.get(base(q), set()) - used
        if len(names) == 1:
            q.property_raw, q.doubt = names.pop(), None
            used.add(q.property_raw)
    return found


def quantities(text: str) -> list[Quantity]:
    """Quantities with offsets in the original text; spelled numbers («пять килограммов») count too."""
    ratios = [(m.start(), m.end()) for m in RATIO_RE.finditer(text)]
    found = [q for q in _align_sizes(_spelled_quantities(text)) if not any(s <= q.start < e for s, e in ratios)]
    for q in found:
        # «столешница 20 мм толщиной»: the property word may follow the number.
        after = _PROPERTY_AFTER.match(text, q.end)
        if after and not _PROPERTY_WORD.search(norm(q.property_raw or "")):
            q.property_raw = f"{q.property_raw} {after.group(1)}".strip()
        # «питается от 220 В», «работает от сети 12 В»: the source of supply, not a lower bound.
        if q.op == "ge" and norm(text[q.start:q.start + 2]) == "от" and _SUPPLIED.search(norm(text[:q.start])):
            q.op = "eq"
    return found


_PROPERTY_AFTER = re.compile(r"\s*(толщиной|шириной|высотой|длиной|глубиной|диаметром|весом|массой|объ[её]мом|"
                             r"мощностью)(?![А-Яа-яЁё])", re.IGNORECASE)
_PROPERTY_WORD = re.compile(r"(?<![а-яё])(?:толщин|ширин|высот|длин|глубин|диаметр|вес|масс|объ[её]м|мощност)")
_SUPPLIED = re.compile(r"(?:пита\w*|работа\w*|подключа\w*|заряжа\w*)\s*$")


def _spelled_quantities(text: str) -> list[Quantity]:
    from src.ktru.engine import numwords  # noqa: PLC0415 - numwords imports nothing from here at load time

    spelled = numwords.spans(text)
    if not spelled:
        return _quantities(text)
    rewritten, segments, last, shift = [], [], 0, 0
    for start, end, number in spelled:
        rewritten.append(text[last:start])
        digits = number.replace(".", ",")
        segments.append((start + shift, start + shift + len(digits), start, end))
        shift += len(digits) - (end - start)
        rewritten.append(digits)
        last = end
    rewritten.append(text[last:])
    work = "".join(rewritten)

    def back(pos: int, closing: bool) -> int:
        delta = 0
        for new_start, new_end, old_start, old_end in segments:
            if new_start <= pos < new_end or (closing and new_start < pos <= new_end):
                return old_end if closing else old_start
            if pos >= new_end:
                delta = old_end - new_end
        return pos + delta

    adjacent = {i for i, (a, b) in enumerate(zip(spelled, spelled[1:], strict=False)) if not text[a[1]:b[0]].strip()}
    out = []
    for q in _quantities(work):
        q.start, q.end = back(q.start, False), back(q.end, True)
        q.raw = text[q.start:q.end].strip()
        if any(spelled[i][0] <= q.start < spelled[i + 1][1] for i in adjacent):
            q.doubt = "Два числа словами подряд: значение неоднозначно"
        out.append(q)
    return out


def _quantities(text: str) -> list[Quantity]:
    found = []
    for cs, ce in clauses(text):
        clause = text[cs:ce]
        taken: list[tuple[int, int]] = []
        start_count = len(found)
        # «1/2 дюйма», «3/4"»: a fraction is one number, not «1» and «2».
        for m in FRACTION_RE.finditer(clause):
            unit_raw = _unit(m.group("unit"))
            x = Decimal(m.group("a")) / Decimal(m.group("b"))
            prop = " ".join(w for w, _, _ in words(clause[: m.start()])[-4:])
            found.append(_qty(text, cs, m.start(), m.end(), "eq", x.quantize(Decimal("0.001")).normalize(), None,
                              unit_raw, prop))
            taken.append((m.start(), m.end()))
        for m in LETTER_DIM_RE.finditer(clause):
            unit_raw = _unit(m.group("unit"))
            items = list(LETTER_ITEM_RE.finditer(clause, m.start(), m.end()))
            for i, item in enumerate(items):
                if (x := dec(item.group(2))) is not None:
                    end = m.end() if i == len(items) - 1 else item.end()
                    found.append(_qty(text, cs, item.start(), end, "eq", x, None, unit_raw,
                                      LETTER_NAMES[item.group(1).lower()]))
            taken.append((m.start(), m.end()))
        for t in _TOKEN.finditer(clause):
            if identifier(t.group()):
                taken.append((t.start(), t.end()))  # identifiers are text, never quantities
        for m in BRACKET_RE.finditer(clause):
            a, b = dec(m.group("a")), dec(m.group("b"))
            if a is None or b is None:
                continue
            prop = " ".join(w for w, _, _ in words(clause[: m.start()])[-4:])
            q = _qty(text, cs, m.start(), m.end(), "between", min(a, b), max(a, b), _unit(m.group("unit")), prop)
            q.lo_inclusive, q.hi_inclusive = m.group("o") == "[", m.group("c") == "]"
            q.doubt = "Границы диапазона указаны в обратном порядке" if b < a else None
            found.append(q)
            taken.append((m.start(), m.end()))
        # «20 ± 1 мм» is the closed range [19; 21]; «20 мм ± 1%» is relative to the nominal value.
        for m in TOL_RE.finditer(clause):
            a, d = dec(m.group("a")), dec(m.group("d"))
            if a is None or d is None or (m.start() > 0 and clause[m.start() - 1].isalpha()):
                continue
            unit_a = _unit(m.group("unit_a"))
            unit_d = _unit(m.group("unit"))
            delta = abs(a) * d / 100 if unit_d == "%" else d
            unit_raw = unit_a or (unit_d if unit_d != "%" else None)
            prop = " ".join(w for w, _, _ in words(clause[: m.start()])[-4:])
            found.append(_qty(text, cs, m.start(), m.end(), "between", a - delta, a + delta, unit_raw, prop))
            taken.append((m.start(), m.end()))
        # «50х60х70 см», «120 на 60 см»: length × width (× height) sharing one unit.
        for m in DIM_RE.finditer(clause):
            if m.start() > 0 and clause[m.start() - 1].isalpha() or any(ts <= m.start() < te for ts, te in taken):
                continue
            unit_raw = _unit(m.group("unit"))
            prop = " ".join(w for w, _, _ in words(clause[: m.start()])[-3:])
            last = "c" if m.group("c") else "b"
            for i, g in enumerate(("a", "b", "c"), 1):
                x = dec(m.group(g)) if m.group(g) else None
                if x is not None:
                    label = f"{prop} {DIM_NAMES[i - 1]}".strip()
                    # Each size quotes its own number; the shared unit closes the last one.
                    end = m.end() if g == last else m.end(g)
                    found.append(_qty(text, cs, m.start(g), end, "eq", x, None, unit_raw, label))
                    if m.group("c"):
                        # Three sizes have several conventions (Д×Ш×В, Ш×Г×В): the order is a reading, not a fact.
                        found[-1].doubt = SIZE_ORDER_DOUBT
            taken.append((m.start(), m.end()))
        for m in QTY_RE.finditer(clause):
            a = dec(m.group("a").replace("−", "-"))
            if a is None or any(ts <= m.start("a") < te for ts, te in taken):
                continue
            s, e = m.start("a"), m.end()
            token = re.match(r"[+\-−]?\d[\d.,]*", clause[s:]).group().rstrip(".,")
            if len(re.findall(r"[.,]", token)) >= 2:
                taken.append((s, s + len(token)))  # «1,2,5 кг» is not a number: keep the text, extract nothing
                continue
            doubt = "Запятая может отделять тысячи" if re.fullmatch(r"\d{1,3},\d{3}", token) else None
            # Digits glued to letters are model/article numbers (A4, 3M), not quantities.
            if s > 0 and clause[s - 1].isalpha():
                continue
            if clause[e - 1 : e].isdigit() and clause[e : e + 1].isalpha():
                continue
            unit_raw = _unit(m.group("unit") or m.group("unit_a"))
            unit_a = _unit(m.group("unit_a"))
            if unit_a and m.group("unit") and UNITS.get(unit_a, ("", ""))[1] != UNITS.get(unit_raw, ("", ""))[1]:
                continue  # «5 кг - 10 см»: not one range
            # «2020 г.» is a year, not grams.
            if unit_raw == "г" and re.fullmatch(r"(19|20)\d\d", m.group("a")):
                continue
            op = OP_MAP.get(re.sub(r"\s+", " ", (m.group(1) or "").lower()), "eq")
            if re.search(r"(?<![а-яё])минус\s*$", clause[: m.start("a")].lower()) and a > 0:
                a = -a  # «минус 5 отвёрток» keeps its sign; the validator decides whether it is possible
            b = dec(m.group("b").replace("−", "-")) if m.group("b") else None
            if b is not None:
                op = "between"
                if unit_a and unit_raw and unit_a != unit_raw:
                    a = a * UNITS[unit_a][2] / UNITS[unit_raw][2]
                if unit_a and not m.group("unit") and m.group("unit_a"):
                    unit_raw = unit_a
                if b < a:
                    a, b = b, a
                    doubt = "Границы диапазона указаны в обратном порядке"
            prop_words = [w for w, _, _ in words(clause[: m.start()])]
            # Document/article identifiers are not quantities: «ГОСТ 12.4.131-83», «ТР ТС 019/2011», «арт. 4364».
            if prop_words and norm(prop_words[-1]).startswith(IDENT_PREFIXES) and not unit_raw:
                continue
            prop = " ".join(prop_words[-4:]) if prop_words else _header_before(text, cs)
            found.append(_qty(text, cs, m.start(), e, op, a, b, unit_raw, prop))
            found[-1].doubt = doubt
        _merge_bounds(text, found, start_count)
    return found


def _merge_bounds(text: str, found: list[Quantity], start: int) -> None:
    """«не меньше 90 см и не больше 100 см»: a lower and an upper bound of one clause form one range."""
    clause = found[start:]
    lows = [q for q in clause if q.op in ("ge", "gt")]
    highs = [q for q in clause if q.op in ("le", "lt")]
    if len(lows) != 1 or len(highs) != 1:
        return
    lo, hi = lows[0], highs[0]
    unit_raw = lo.unit_raw or hi.unit_raw
    if (lo.dimension and hi.dimension and lo.dimension != hi.dimension) or lo.start > hi.start:
        return
    a, b = lo.a, hi.a
    if lo.unit_raw and hi.unit_raw and lo.unit_raw != hi.unit_raw:
        a = a * UNITS[lo.unit_raw][2] / UNITS[hi.unit_raw][2]
        unit_raw = hi.unit_raw
    if a > b or (a == b and not (lo.op == "ge" and hi.op == "le")):
        return  # contradictory or empty bounds stay separate for diagnostics
    merged = _qty(text, 0, lo.start, hi.end, "between", a, b, unit_raw, lo.property_raw)
    merged.lo_inclusive, merged.hi_inclusive = lo.op == "ge", hi.op == "le"
    found[start:] = [q for q in clause if q is not lo and q is not hi] + [merged]


def to_base(x: Decimal, okei: str | None) -> tuple[Decimal, str | None]:
    if okei and okei in OKEI:
        dim, factor, _ = OKEI[okei]
        return x * factor, dim
    return x, None


def fmt(x: Decimal) -> str:
    s = format(x.normalize(), "f")
    return s.replace(".", ",")
