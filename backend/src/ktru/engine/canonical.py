"""Canonical fact vocabulary: presence, materials, colours, spelled numbers and product names.

The model proposes facts in free form («подлокотники: нет», «каркас: сталь», «деревянной ручкой»).
These helpers map such proposals to one canonical shape; the caller still proves every value from the quote.
"""

import re

from src.ktru.engine import numwords
from src.ktru.engine.spelling import noun_lemma
from src.ktru.engine.text import (
    COUNT_DIMENSIONS,
    NOT_PROPERTY_WORD,
    content_stems,
    norm,
    quantities,
    stem,
    word_has_label,
)

# ---- presence ------------------------------------------------------------------------------------------------------

PRESENCE_PROPERTIES = ("наличи", "комплект", "комплектаци", "комплектност", "в комплект", "входит", "состав поставк")
_ABSENT = re.compile(r"(?<![а-яё])(без|нет|отсутству\w*|не\s+входит|не\s+входят|не\s+включ\w*|не\s+име\w*|"
                     r"не\s+предусмотр\w*|не\s+прилага\w*|не\s+комплект\w*)(?![а-яё])")
_PRESENT = re.compile(r"(?<![а-яё])(есть|имеется|имеются|входит|входят|в\s+комплекте|включ\w*|прилага\w*|"
                      r"присутству\w*|с|со|да)(?![а-яё])")
_PRESENCE_VALUES = {"нет", "есть", "да", "без", "имеется", "имеются", "отсутствует", "отсутствуют", "входит",
                    "входят", "не входит", "не входят", "в комплекте", "присутствует", "true", "false"}


def is_presence(prop: str, value: str) -> bool:
    p, v = norm(prop).strip(), norm(value).strip()
    return v in _PRESENCE_VALUES or any(p.startswith(x) for x in PRESENCE_PROPERTIES)


def presence(prop: str, value: str, relation: str, part: str = "") -> tuple[str, bool] | None:
    """(object, present) for a presence claim. The object comes from the claim (or its named part); presence comes from the text."""
    p, v = norm(prop).strip(), norm(value).strip()
    generic = any(p.startswith(x) for x in PRESENCE_PROPERTIES)
    obj = v if generic and v not in _PRESENCE_VALUES else p
    obj = re.sub(r"^(наличие|наличия|в комплекте|комплектация)(\s+|$)", "", obj).strip()  # «наличие» alone names nothing
    if (not obj or obj in _PRESENCE_VALUES) and part:
        obj = norm(part).strip()  # «наличие: есть» + part «капюшон»
    if not obj or obj in _PRESENCE_VALUES or not content_stems(obj):
        return None
    observed = content_stems(relation)
    if not all(any(word_has_label(w, t) for w in observed) for t in content_stems(obj)):
        return None
    low = norm(relation)
    if _ABSENT.search(low):
        return obj, False
    if _PRESENT.search(low):
        return obj, True
    return None


# ---- materials and colours -----------------------------------------------------------------------------------------

# Adjective/noun stem → canonical material noun (nominative, as catalog values are written).
MATERIALS = {
    "дерев": "дерево", "древесин": "древесина", "сталь": "сталь", "стальн": "сталь", "нержаве": "нержавеющая сталь",
    "металл": "металл", "металлическ": "металл", "чугун": "чугун", "алюмини": "алюминий", "медн": "медь", "мед": "медь",
    "латун": "латунь", "титан": "титан", "пластик": "пластик", "пластмасс": "пластмасса", "резин": "резина",
    "прорезинен": "резина", "стеклопластик": "стеклопластик", "стекл": "стекло", "фанер": "фанера", "дуб": "дуб",
    "сосн": "сосна", "бук": "бук", "берез": "береза", "массив": "массив", "мдф": "МДФ", "лдсп": "ЛДСП", "дсп": "ДСП",
    "шпон": "шпон", "хлоп": "хлопок", "хлопк": "хлопок", "хлопков": "хлопок", "хлопчатобумажн": "хлопок",
    "лен": "лен", "льнян": "лен", "шерст": "шерсть", "шерстян": "шерсть", "шелк": "шелк", "шелков": "шелк",
    "полиэстер": "полиэстер", "полиэфир": "полиэстер", "нейлон": "нейлон", "полиамид": "полиамид",
    "хлопок": "хлопок", "мехов": "мех", "деним": "деним", "джинсов": "деним", "брезент": "брезент",
    "алюминий": "алюминий", "велюр": "велюр", "синтепон": "синтепон", "пухов": "пух",
    "холлофайбер": "холлофайбер", "кашемир": "кашемир",
    "акрил": "акрил", "вискоз": "вискоза", "эластан": "эластан", "спандекс": "эластан", "флис": "флис",
    "кож": "кожа", "кожан": "кожа", "экокож": "экокожа", "замш": "замша", "трикотаж": "трикотаж",
    "синтетик": "синтетика", "синтетическ": "синтетика", "текстил": "текстиль", "ткан": "ткань",
    "картон": "картон", "картонн": "картон", "бумаг": "бумага", "бумажн": "бумага", "полиэтилен": "полиэтилен",
    "полипропилен": "полипропилен", "пвх": "ПВХ", "поливинилхлорид": "ПВХ", "полиуретан": "полиуретан",
    "пенополиуретан": "пенополиуретан", "поролон": "поролон", "ротанг": "ротанг", "бамбук": "бамбук",
    "фибергласс": "стеклопластик", "углепластик": "углепластик", "камен": "камень", "мрамор": "мрамор",
}
# Colour stem → masculine nominative.
COLORS = {
    "син": "синий", "голуб": "голубой", "зелен": "зеленый", "черн": "черный", "бел": "белый", "красн": "красный",
    "сер": "серый", "желт": "желтый", "оранжев": "оранжевый", "фиолетов": "фиолетовый", "розов": "розовый",
    "коричнев": "коричневый", "бежев": "бежевый", "бордов": "бордовый", "хаки": "хаки", "серебрист": "серебристый",
    "золотист": "золотистый", "бирюзов": "бирюзовый", "сирен": "сиреневый", "темно-син": "темно-синий",
}
_MATERIAL_PROPS = ("материал", "состав", "сырь", "из чего", "ткан", "покрыти")
_COLOR_PROPS = ("цвет", "окрас", "расцвет")


def _lookup(table: dict[str, str], value: str) -> str | None:
    """Single-concept value only: «синий» or «хлопковый», not «синий и зелёный»."""
    words = [w for w in content_stems(value)]
    if len(words) != 1:
        whole = norm(value).strip()
        return table.get(whole) if whole in table else None
    w = words[0]
    # A value word is its stem plus a short ending («деревянный» → «дерев»): «синтепон» is not «син(ий)».
    best = max((k for k in table if (w.startswith(k) and len(w) - len(k) <= 4) or stem(k) == w), key=len, default=None)
    return table[best] if best else None


STAINLESS = re.compile(r"(?:нержавеющ(?:ая|ей|ую)\s+стал(?:ь|и|ью)|стал(?:ь|и|ью)\s+нержавеющ(?:ая|ей|ую))", re.IGNORECASE)


def material(value: str) -> str | None:
    if STAINLESS.fullmatch(norm(value).strip()):
        return "нержавеющая сталь"
    return _lookup(MATERIALS, value)


def color(value: str) -> str | None:
    return _lookup(COLORS, value)


def property_class(prop: str) -> str | None:
    p = norm(prop)
    if any(p.startswith(x) or f" {x}" in p for x in _MATERIAL_PROPS):
        return "материал"
    if any(p.startswith(x) for x in _COLOR_PROPS):
        return "цвет"
    return None


def categorical(prop: str, value: str) -> tuple[str, str] | None:
    """(canonical property, canonical label) for a material or colour claim, else None."""
    cls = property_class(prop)
    if cls == "цвет" or (cls is None and color(value) and not material(value)):
        label = color(value)
        return ("цвет", label) if label else None
    if cls == "материал" or (cls is None and material(value)):
        label = material(value)
        return ("материал", label) if label else None
    return None


# ---- property names ------------------------------------------------------------------------------------------------

# Word stem → canonical property. Order matters: the more specific stem first.
PROPERTY_WORDS = (
    ("брутто", "масса"), ("нетто", "масса"), ("весит", "масса"), ("весят", "масса"), ("вес", "масса"),
    ("масс", "масса"), ("ширин", "ширина"), ("высот", "высота"), ("длин", "длина"), ("толщин", "толщина"),
    ("глубин", "глубина"), ("диаметр", "диаметр"), ("мощност", "мощность"), ("мощн", "мощность"),
    ("напряжени", "напряжение"), ("емкост", "емкость"), ("объем", "объем"), ("размер", "размер"),
    ("рост", "рост"), ("температур", "температура"), ("плотност", "плотность"), ("количеств", "количество"),
    ("кол-в", "количество"), ("модел", "модель"), ("артикул", "артикул"), ("цвет", "цвет"),
    ("материал", "материал"), ("длительност", "длительность"), ("срок", "срок"),
    ("выдержива", "нагрузка"), ("нагрузк", "нагрузка"),
)
_PROPERTY_RE = re.compile(r"(?<![а-яё])(" + "|".join(re.escape(s) for s, _ in PROPERTY_WORDS) + r")[а-яё-]*")
PACKAGES = {"упаков": "упаковка", "короб": "коробка", "тар": "тара", "футляр": "футляр", "пачк": "пачка",
            "пакет": "пакет", "мешоч": "мешочек", "кейс": "кейс", "брутт": "упаковка"}


ENGLISH_PROPERTIES = {"power": "мощность", "weight": "масса", "mass": "масса", "width": "ширина", "height": "высота",
                      "length": "длина", "size": "размер", "color": "цвет", "colour": "цвет", "material": "материал",
                      "model": "модель", "voltage": "напряжение", "capacity": "емкость", "quantity": "количество",
                      "qty": "количество", "count": "количество", "thickness": "толщина", "depth": "глубина"}


def property_name(raw: str) -> str | None:
    """Canonical property named in a context; the word closest to the value (the last one) wins."""
    found = [m for m in _PROPERTY_RE.finditer(norm(raw)) if not NOT_PROPERTY_WORD.match(m.group(0))]
    if not found:
        english = [ENGLISH_PROPERTIES[w] for w in re.findall(r"[a-z]+", norm(raw)) if w in ENGLISH_PROPERTIES]
        return english[-1] if english else None
    word = found[-1].group(1)
    return next(name for stem_, name in PROPERTY_WORDS if stem_ == word)


TEXT_PROPERTIES = {"цвет", "материал", "модель", "артикул"}


def numeric_property_name(raw: str) -> str | None:
    """Property for a number: «цвет 760» or «материал 210» are codes or densities, never colour or material."""
    name = property_name(raw)
    return None if name in TEXT_PROPERTIES else name


def package_label(raw: str) -> str | None:
    for word in re.findall(r"[а-яё]+", norm(raw)):
        for prefix, label in PACKAGES.items():
            if word.startswith(prefix) and not (prefix == "тар" and not re.fullmatch(r"тар[аеуыо]й?", word)):
                return label
    return None


UNIT_PROPERTIES = {"voltage": "напряжение", "power": "мощность", "mass": "масса", "temperature": "температура",
                   "volume": "объем", "charge": "емкость", "frequency": "частота", "rotation": "частота вращения",
                   "pressure": "давление", "torque": "крутящий момент", "current": "сила тока",
                   "areal_density": "плотность", "sound": "уровень шума", "energy": "энергия",
                   "resistance": "сопротивление", "hardness": "твердость"}


ROUND_PARTS = ("диск", "круг", "колес", "труб", "шланг", "черен", "сверл")
GARMENTS = ("брюк", "куртк", "рубашк", "халат", "костюм", "плать", "юбк", "футболк", "свитер", "джемпер", "толстовк",
            "жилет", "пальто", "шорт", "комбинезон", "ботин", "сапог", "туфл", "кроссовк", "полуботин", "кед", "блуз",
            "сорочк", "водолазк", "фартук")


def unnamed_property(context: str, dimension: str | None, x, part: str | None) -> str | None:
    """A number with no property word: «рукава 60 см» is a length, «диск 190 мм» a diameter, «брюки 48» a size."""
    words = re.findall(r"[а-яё]+", norm(context))
    if dimension == "length" and part:
        return "диаметр" if norm(part).startswith(ROUND_PARTS) else "длина"
    if dimension is None and words and words[0].startswith(GARMENTS) and len(words) <= 3 and 26 <= x <= 80:
        return "размер"
    return None


def property_from_unit(dimension: str | None) -> str | None:
    """«питание 12 В», «600 Вт»: an unnamed value whose unit names exactly one property."""
    return UNIT_PROPERTIES.get(dimension or "")


DIMENSIONS = {"масса": "mass", "нагрузка": "mass", "ширина": "length", "высота": "length", "длина": "length", "толщина": "length",
              "глубина": "length", "диаметр": "length", "мощность": "power", "напряжение": "voltage", "объем": "volume"}


def dimension_doubt(prop: str, dimension: str | None) -> str | None:
    """«Молоток весит 1 литр»: a unit of another quantity is kept, but never trusted as the property."""
    name = property_name(prop) or ""
    expected = DIMENSIONS.get(name)
    if expected and dimension and dimension not in (expected, "percent"):
        return "Единица не соответствует свойству"
    if name == "размер" and dimension not in (None, "length", "percent", *COUNT_DIMENSIONS):
        return "Единица не соответствует свойству"  # «размер 1 литр»
    return None


_UNKNOWN = re.compile(r"^(неизвест\w*|не\s+указан\w*|не\s+знаю|нет\s+данных|не\s+определ\w*|не\s+уточн\w*|"
                      r"уточняется|\?+|н/д|-+|—)$")


def unknown_value(value: str) -> bool:
    """«цвет неизвестен» states that the value is unknown, not a value."""
    return bool(_UNKNOWN.match(norm(value).strip()))


_CORRECTION = re.compile(r"(?<![а-яё])(исправля\w*|исправлен\w*|исправ\w*|поправка|правильно|вернее|точнее|"
                         r"опечатка|ошиб\w*\s+в)(?![а-яё])")


def correction_at(text: str, start: int) -> bool:
    """«Исправляю массу головки на 500 г»: the sentence holding the value corrects an earlier one."""
    low = norm(text)
    s_start = max((m.end() for m in re.finditer(r"[.!?\n]\s*", low[:start])), default=0)
    s_end = next((m.start() for m in re.finditer(r"[.!?\n]", low[start:])), len(low) - start) + start
    return bool(_CORRECTION.search(low, s_start, s_end))


# ---- modality ------------------------------------------------------------------------------------------------------

_HEDGE = re.compile(r"(?<![а-яё])(наверн\w*|возможно|может\s+быть|а\s+может|кажется|вроде\w*|"
                    r"предположительно|не\s+уверен\w*|скорее\s+всего|либо|или)(?![а-яё])")
_META = re.compile(r"(?<![а-яё])(не\s+знаю|неизвестн\w*|не\s+уточн\w*|надо\s+уточнить|уточнить|уточняется|"
                   r"под\s+вопросом|не\s+измеряли|не\s+проверя\w*|не\s+провер\w*)(?![а-яё])")
_ORDER = re.compile(r"(?<![а-яё])(в\s+заказе|по\s+заказу|в\s+заявке|заказано|в\s+техзадании)(?![а-яё])")
_SENTENCE = re.compile(r"[.!?\n;]+")
_CLAUSE = re.compile(r"[,:—–]+|\s-\s")


def _segments(text: str) -> list[tuple[int, int, list[tuple[int, int]]]]:
    out, start = [], 0
    for m in [*_SENTENCE.finditer(text), None]:
        end = m.start() if m else len(text)
        if text[start:end].strip():
            parts, cs = [], start
            for c in [*_CLAUSE.finditer(text, start, end), None]:
                ce = c.start() if c else end
                if text[cs:ce].strip():
                    parts.append((cs, ce))
                cs = c.end() if c else end
            out.append((start, end, parts))
        start = m.end() if m else end
    return out


def modality(text: str, start: int, end: int) -> tuple[str | None, str | None]:
    """(polarity, reason) for a fact at [start, end): «uncertain» under hedges/alternatives, None otherwise.

    A hedge inside the fact's clause applies to it; a clause that is only a hedge («наверное», «границу не
    уточнили») applies to its neighbours in the same sentence. A value written in an order is not a measurement.
    """
    low = norm(text)
    for s_start, s_end, parts in _segments(low):
        if not s_start <= start < max(s_end, s_start + 1):
            continue
        if _ORDER.search(low, s_start, s_end):
            return "order", "Значение из заказа, а не измерение товара"
        own = next((i for i, (cs, ce) in enumerate(parts) if cs <= start < ce), None)
        if own is None:
            return None, None
        cs, ce = parts[own]
        if _HEDGE.search(low, cs, ce) or (_META.search(low, cs, ce) and not re.search(r"\d", low[start:end])):
            return "uncertain", "Высказано с сомнением или как вариант"
        for neighbour in (own - 1, own + 1):
            if 0 <= neighbour < len(parts):
                ns, ne = parts[neighbour]
                clause = low[ns:ne]
                if (_HEDGE.search(clause) or _META.search(clause)) and not re.search(r"\d", clause) \
                        and len(clause.split()) <= 5 and not property_name(clause) and not color(clause.strip()) \
                        and not material(clause.strip()):
                    return "uncertain", "Высказано с сомнением"
        return None, None
    return None, None


# ---- spelled numbers -----------------------------------------------------------------------------------------------

UNIT_WORDS = (
    (r"миллиметр\w*", "мм"), (r"сантиметр\w*", "см"), (r"дециметр\w*", "дм"), (r"километр\w*", "км"),
    (r"метр\w*", "м"), (r"килограмм\w*", "кг"), (r"кило(?![а-яё])", "кг"), (r"грамм\w*", "г"),
    (r"тонн\w*", "т"), (r"миллилитр\w*", "мл"), (r"литр\w*", "л"), (r"киловатт\w*", "квт"), (r"ватт\w*", "вт"),
    (r"вольт\w*", "в"), (r"процент\w*", "%"),
)


def digits(text: str) -> str:
    """Spell numbers and unit words as digits and abbreviations. For verification only: offsets change."""
    out, last = [], 0
    for start, end, number in numwords.spans(text):
        out.append(text[last:start])
        out.append(number.replace(".", ","))
        last = end
    out.append(text[last:])
    rewritten = "".join(out)
    for pattern, unit in UNIT_WORDS:
        rewritten = re.sub(rf"(?<![А-Яа-яЁё]){pattern}", unit, rewritten, flags=re.IGNORECASE)
    return rewritten


# ---- product name --------------------------------------------------------------------------------------------------

_NOT_PRODUCT = {"товар", "изделие", "наименование", "поставка", "поставляем", "продаем", "нужен", "нужна", "нужно",
                "нужны", "заказать", "купить", "купим", "закупить", "закупка", "поставить", "требуются", "продается",
                "продаётся", "нетто", "брутто", "минус", "плюс", "около", "примерно", "всего", "итого",
                "требуется", "закупка", "описание", "характеристики", "количество", "цвет", "размер", "масса", "вес",
                "материал", "высота", "ширина", "длина", "толщина", "мощность", "напряжение", "набор", "комплект"}


_WORD = r"[A-Za-zА-Яа-яЁё\u200b-\u200f\u2060\ufeff]+(?:-[A-Za-zА-Яа-яЁё\u200b-\u200f\u2060\ufeff]+)*"
# A part named alone is a part of its usual product («Толщина столешницы 20 мм» describes a table).
_LEADING_PARTS = ("сиден", "спинк", "столешниц", "ножк", "рукоят", "ручк", "головк", "подкладк", "подошв", "каркас",
                  "обивк", "рукав", "воротник", "молни", "лезви")
PART_OWNERS = {"столешниц": "стол", "сиден": "стул", "спинк": "стул", "подлокотник": "стул", "бо": "молоток"}


_PREPOSITIONS = {"без", "с", "со", "для", "из", "на", "в", "во", "под", "от", "к", "ко", "по", "у", "при", "о", "об",
                 "из-под", "через", "над", "перед", "между", "около", "вместо", "кроме", "либо", "или", "и",
                 "не", "ни", "меньше", "больше", "менее", "более", "свыше", "примерно", "это", "весит", "весом",
                 "массой", "шириной", "высотой", "длиной", "толщиной"}
_JSON_NAMES = ("name", "товар", "наименование", "название", "title", "product", "item")


def _json_name(text: str) -> str | None:
    import json  # noqa: PLC0415 - only for descriptions pasted as JSON

    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict):
        return next((str(v) for k, v in data.items() if norm(str(k)) in _JSON_NAMES and isinstance(v, str)), None)
    return None


def product_label(text: str, family_name: str | None = None) -> str | None:
    """Head noun phrase of the description: words before the first number, punctuation or preposition.

    Verbs and property words are skipped; «Нужно 1 000 стульев» names the product after the number.
    When the catalog family has the same head word, its nominative form is used («стола» → «стол»).
    Invisible characters stay as typed, so the label matches the source text exactly."""
    stripped = text.strip()
    if stripped.startswith("{"):
        stripped = _json_name(stripped) or stripped
    head = re.split(r"[,;:.!?\n\t(\"«]|\d", stripped, maxsplit=1)[0]
    words = re.findall(_WORD, head)
    while len(words) > 1 and not re.fullmatch(r"[А-Яа-яЁё-]+", words[0]):
        words = words[1:]  # «JPC75-BL-XXL Ninja Комбинезон»: leading article codes and Latin brands
    words = words[:4]
    led_by_property = bool(words) and property_name(words[0]) is not None
    while words and norm(words[0]) in _NOT_PRODUCT:
        words = words[1:]
    cut = next((i for i, w in enumerate(words) if i > 0 and norm(w) in _PREPOSITIONS), None)
    words = words[:cut] if cut else words
    if len(words) >= 2 and norm(words[0]).startswith(_LEADING_PARTS) and (owner := noun_lemma(norm(words[1]))):
        return owner  # «Сиденье стула красное» describes a chair; the seat is its part
    if (len(words) >= 3 and norm(words[0]).endswith(("ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие"))
            and not norm(words[0]).startswith(("сменн", "запасн")) and norm(words[1]).startswith((*_LEADING_PARTS, "голов"))
            and (owner := noun_lemma(norm(words[2])))):
        return owner  # «Стальная голова молотка» describes a hammer; «Сменная головка молотка» sells the head
    if len(words) == 1 and led_by_property:
        owner = next((v for k, v in PART_OWNERS.items() if norm(words[0]).startswith(k) and len(k) > 2), None)
        if owner:
            return owner
    if words and family_name:
        family_head = norm(re.findall(_WORD, family_name)[0]) if re.findall(_WORD, family_name) else ""
        first = norm(words[0])
        if len(family_head) >= 3 and first != family_head and first[:4] == family_head[:4]:
            words = [family_head, *words[1:]]
    if not words:
        # «50 литров молотка» names the product after the number and its unit, not the unit itself.
        sentence = re.split(r"[.!?\n]", stripped, maxsplit=1)[0]
        stated = next(iter(quantities(sentence)), None)
        after = (re.match(rf"\s*({_WORD})", sentence[stated.end:]) if stated is not None else None) \
            or re.search(rf"\d[\d\s.,]*\s+({_WORD})", sentence)
        words = [after.group(1)] if after else []
        if words and family_name and re.findall(_WORD, family_name):
            family_head = norm(re.findall(_WORD, family_name)[0])
            words = [family_head] if norm(words[0])[:4] == family_head[:4] else words
    if len(words) == 1 and led_by_property:
        # «Толщина столешницы 20 мм» describes a table; «Столешница из дуба» sells the tabletop itself.
        owner = next((v for k, v in PART_OWNERS.items() if norm(words[0]).startswith(k) and len(k) > 2), None)
        if owner:
            return owner
    return norm(" ".join(words)) if words else None
