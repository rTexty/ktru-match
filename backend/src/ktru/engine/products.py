"""Several products in one description: which products are named, how they nest, and who owns a statement.

A description that names one product has no layout: the single root item describes it. A layout names two or more:

- enumerations «Стул 4шт и молаток 2шт», «Куртка синяя — 2 штуки; стул чёрный — 3 штуки», «2 — дрель; набор — 1»;
- tables pasted from a spreadsheet with a «Товар» column, one product per row;
- instances «два стула: один синий, второй зеленый»: a batch item and numbered instances under it;
- sets «Один набор: мини-стул 4шт и мини-молоток 2шт», «Брюки 48 и куртка 50 упакованы в один костюм»,
  with membership stated later: «Свёрла входят в набор», «в наборе отвёртки».

The first top-level item is the root: the catalog family and the card describe it. A statement belongs to the
product whose segment contains it, else to the product named nearest to it in its clause («на молотке …»), else to
the root (with no root item, it stays on the described item). Pure text: subjects are built from a layout in `assertions.assign`.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import groupby, islice

from src.ktru.engine import canonical, numwords
from src.ktru.engine.text import alias_of, clauses, content_stems, head_noun_stem, norm, sentences, stems_match

_W = r"[A-Za-zА-Яа-яЁё]+(?:-[A-Za-zА-Яа-яЁё]+)*"
_ADJ = r"[А-Яа-яЁё]+(?:ый|ий|ой|ая|яя|ое|ее|ые|ие|ых|их|ого|его|ую|юю)"
_CARDINAL = r"один|одна|одно|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|оба|обе"
_NUM = rf"\d+|{_CARDINAL}"
_DASH = r"[—–:-]"
_COUNT_UNIT = r"шт\b\.?|штук[а-яё]*|пар[аы]?\b"
_LEAD = re.compile(r"(?:поставляем|поставка|нужно|нужны|нужен|нужна|закупаем|требуется|требуются)\s+", re.IGNORECASE)
SET_NOUNS = ("набор", "комплект", "костюм")
SET_COUNTED = ("набор", "комплект")  # counted in комплекты, not in штуки
_SET = r"(?:набор|комплект|костюм)[а-яё]*"

ENTRY = re.compile(rf"(?P<noun>{_W})(?:\s+{_ADJ})*\s*(?P<dash>{_DASH}\s*)?(?P<num>{_NUM})(?:\s*(?P<unit>{_COUNT_UNIT}))?",
                   re.IGNORECASE)
ENTRY_NUM_FIRST = re.compile(rf"(?P<num>{_NUM})\s*{_DASH}\s*(?P<noun>{_W})", re.IGNORECASE)
MEMBER = re.compile(rf"(?P<noun>{_W})(?:\s+{_ADJ})*", re.IGNORECASE)
INSTANCES = re.compile(
    rf"(?:{_LEAD.pattern})?(?P<num>{_NUM})\s+(?:{_ADJ}\s+)*(?P<noun>{_W})(?:\s+для\s+{_W})?\s*:\s*(?P<rest>.+)",
    re.IGNORECASE | re.DOTALL)
INSTANCE = re.compile(r"(?P<mark>один|одна|одно|два|две|три|первый|первая|первое|второй|вторая|второе|третий|третья|"
                      r"другой|другая|другое|остальные)(?![а-яё])", re.IGNORECASE)
SET_HEAD = re.compile(rf"(?:(?P<num>\d+|один|одна|одно)\s+)?(?P<name>(?:{_ADJ}\s+)*(?P<noun>{_SET})(?:\s+{_ADJ})*)"
                      r"\s*:\s*(?P<rest>.+)", re.IGNORECASE | re.DOTALL)
SET_TAIL = re.compile(rf"(?P<rest>.+?)\s+(?:упакован[а-яё]*|входят|входит|собран[а-яё]*|объединен[а-яё]*)\s+в\s+"
                      rf"(?P<num>один|одну|одно|1)\s+(?P<noun>{_SET})", re.IGNORECASE | re.DOTALL)
COUNTED = re.compile(rf"(?<![\w-])(?P<num>{_NUM})\s+(?:{_ADJ}\s+)*(?P<noun>{_W})", re.IGNORECASE)
IN_SET = re.compile(rf"(?P<noun>{_W})\s+(?:входят|входит)\s+в\s+(?:состав\s+)?(?P<set>{_SET})", re.IGNORECASE)
SET_HAS = re.compile(rf"в\s+(?P<set>наборе|комплекте)\s+(?P<noun>{_W})", re.IGNORECASE)
TABLE_HEADS = {"товар", "наименование", "название", "позиция", "предмет"}


@dataclass
class Count:
    """A number that counts a product: «Стул 4шт», «два стула», «набор — 1», «две дрели отдельно»."""
    start: int
    end: int
    value: str
    pieces: bool    # in штуки («два стула»), not a bare number («2 — дрель»)
    mention: tuple[int, int]  # the counted words: evidence of the count


@dataclass
class Product:
    kind: str                       # item | set | part (a member of a set)
    label: str
    parent: int | None = None       # index of the containing product (set or batch item)
    scopes: tuple[tuple[int, int], ...] = ()  # text this product owns
    counts: list[Count] = field(default_factory=list)
    instance: bool = False
    noun: str = ""                  # head noun the text refers to it by («набор» of «учебный набор»)

    def __post_init__(self):
        self.noun = self.noun or self.label.split()[0]


@dataclass
class Layout:
    text: str
    products: list[Product]
    root: int | None

    def owner(self, start: int, end: int, numeric: bool = False) -> int | None:
        """Product owning text[start:end]: its segment, else the nearest product named in its clause, else the root.

        None: no root item («Костюм женский: куртка, брюки»); the statement stays on the described item."""
        start, end = self.trim(start, end)
        inside = [(e - s, i) for i, p in enumerate(self.products) for s, e in p.scopes if s <= start and end <= e]
        if not inside and numeric:
            # A rule quantity spans from its clause start; the number at its end decides.
            inside = [(e - s, i) for i, p in enumerate(self.products) for s, e in p.scopes if s < end <= e]
        found = min(inside)[1] if inside else None
        cs, ce = next(((s, e) for s, e in clauses(self.text) if s <= start < e), (start, end))
        if found is None and end <= ce:
            named = [(min(abs(ws - start), abs(we - end)), i) for i, ws, we in self.references(cs, ce)]
            found = min(named)[1] if named else None
        if found is None or (self.root is None and self.products[found].parent is None):
            return self.root  # a named set alone is the described item itself; only its members move
        return found

    def trim(self, start: int, end: int) -> tuple[int, int]:
        while start < end and self.text[start].isspace():
            start += 1
        while end > start and self.text[end - 1].isspace():
            end -= 1
        return start, end

    def scope(self, start: int, end: int) -> tuple[int, int] | None:
        """The product segment holding text[start:end] («Брюки 48» in «Брюки 48 и куртка 50 упакованы …»)."""
        start, end = self.trim(start, end)
        inside = [(e - s, s, e) for p in self.products for s, e in p.scopes if s <= start and end <= e]
        return min(inside)[1:] if inside else None

    def references(self, start: int, end: int) -> list[tuple[int, int, int]]:
        out = []
        for m in re.finditer(_W, self.text[start:end]):
            index = self.find(m.group())
            if index is not None:
                out.append((index, start + m.start(), start + m.end()))
        return out

    def find(self, word: str) -> int | None:
        return next((i for i, p in enumerate(self.products) if not p.instance and same_noun(word, p.noun)), None)

    def parallel(self, start: int) -> tuple[int, int] | None:
        """Sentence of instances («один с деревянной, второй со стеклопластиковой ручкой»): one shared noun."""
        if not any(p.instance and any(s <= start < e for s, e in p.scopes) for p in self.products):
            return None
        return next(((s, e) for s, e in sentences(self.text) if s <= start < e), None)


def same_noun(word: str, noun: str) -> bool:
    """«стуле» names «стул», «мини-стульев» names «мини-стул», a typo «молотке» names «молаток»."""
    left, right = norm(word).split("-"), norm(noun).split("-")
    if len(left) != len(right) or left[:-1] != right[:-1]:
        return False
    a, b = left[-1], right[-1]
    if min(len(a), len(b)) < 3:
        return a == b
    n = min(4, len(b))
    return a[:n] == b[:n] or (len(a) >= 5 and len(b) >= 5 and a[:2] == b[:2] and _distance(a[:5], b[:5]) <= 1)


def _distance(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1]


def _value(raw: str) -> str | None:
    if raw.isdigit():
        return str(int(raw))
    found = numwords.spans(raw)
    return found[0][2] if len(found) == 1 and found[0][:2] == (0, len(raw)) else None


def lemma(noun: str, numeral: str | None = None) -> str:
    """Nominative of a noun counted by 2–4 («два стула» → «стул», «два молотка» → «молоток», «две дрели» → «дрель»)."""
    *head, word = norm(noun).split("-")
    number = norm(numeral or "")
    if number in ("два", "три", "четыре", "оба", "2", "3", "4") and len(word) > 3 and word.endswith(("а", "я")):
        word = word[:-1] + ("ь" if word.endswith("я") else "")
        if re.search(r"[бвгджзклмнпрстфхцчшщ][кн]$", word):
            word = word[:-1] + "о" + word[-1]  # fleeting vowel: молотк → молоток, ботинк → ботинок
    elif number in ("две", "обе", "2", "3", "4") and len(word) > 3 and word.endswith(("ы", "и")):
        word = word[:-1] + ("а" if word.endswith("ы") or word[-2] in "кгхжшчщ" else "ь")
    return "-".join([*head, word])


def _product_word(word: str) -> bool:
    from src.ktru.engine.assertions import PARTS  # noqa: PLC0415 - assertions builds subjects from layouts

    low = norm(word)
    if low.startswith(SET_NOUNS):
        return True
    return (len(low) > 2 and all(len(piece) > 1 for piece in low.split("-")) and low not in canonical._NOT_PRODUCT and not canonical.property_name(low)
            and not canonical.color(low) and not canonical.material(low) and not canonical.unknown_value(low)
            and not any(low.startswith(prefix) for prefix in PARTS) and not low.isdigit())


def _entries(text: str, start: int, end: int, bare: bool = False) -> list[tuple[re.Match, int]] | None:
    """Split text[start:end] into «noun count» entries; None unless every piece is one."""
    piece = text[start:end]
    separators = r";" if ";" in piece else r",|\s+и\s+"
    out, last = [], 0
    for m in [*re.finditer(separators, piece), None]:
        raw = piece[last:m.start() if m else len(piece)]
        lead = len(raw) - len(raw.lstrip())
        stripped = raw.strip().rstrip(".!?").rstrip()
        if not out and (verb := _LEAD.match(stripped)):
            lead, stripped = lead + verb.end(), stripped[verb.end():]
        found = ENTRY.fullmatch(stripped) or ENTRY_NUM_FIRST.fullmatch(stripped) or (
            MEMBER.fullmatch(stripped) if bare else None)
        # «Микроскоп цифровой, монокулярный»: an adjective after a comma qualifies the product, it is not another one.
        if not found or not _product_word(found.group("noun")) or re.fullmatch(_ADJ, found.group("noun"), re.IGNORECASE):
            return None
        out.append((found, start + last + lead))
        last = m.end() if m else len(piece)
    return out


def _add_entries(products: list[Product], found: list[tuple[re.Match, int]], kind: str, parent: int | None) -> None:
    for m, offset in found:
        noun = m.group("noun")
        is_set = kind == "item" and norm(noun).startswith(SET_NOUNS)
        scope = (offset, offset + m.end())
        product = Product(kind="set" if is_set else kind, label=lemma(noun), parent=None if is_set else parent,
                          scopes=(scope,))
        # «Брюки 48» states a size; a count needs штуки, a dash («набор — 1») or a leading number («2 — дрель»).
        groups = m.groupdict()
        if groups.get("num") and (groups.get("unit") or groups.get("dash", True)) and _value(groups["num"]):
            product.counts.append(Count(offset + m.start("num"), offset + m.end("num"), _value(groups["num"]),
                                        bool(groups.get("unit")), scope))
        products.append(product)


def _table(text: str) -> list[Product]:
    lines = [(m.start(), m.end()) for m in re.finditer(r"[^\n]+", text)]
    if len(lines) < 3 or "\t" not in text[lines[0][0]:lines[0][1]]:
        return []
    head = [norm(cell).strip() for cell in text[lines[0][0]:lines[0][1]].split("\t")]
    if head[0] not in TABLE_HEADS:
        return []
    count_column = next((i for i, cell in enumerate(head) if cell.startswith(("количеств", "кол-во"))), None)
    products = []
    for start, end in lines[1:]:
        cells, offset, spans = text[start:end].split("\t"), start, []
        for cell in cells:
            spans.append((offset, offset + len(cell)))
            offset += len(cell) + 1
        word = re.match(_W, cells[0].strip())
        if not word or not _product_word(word.group()):
            return []
        product = Product(kind="item", label=norm(cells[0].strip()), scopes=((start, end),))
        if count_column is not None and count_column < len(cells) and (value := _value(cells[count_column].strip())):
            product.counts.append(Count(*spans[count_column], value, False, (start, end)))
        products.append(product)
    return products


def _instances(text: str, start: int, end: int, m: re.Match, products: list[Product]) -> bool:
    rest = start + m.start("rest")
    pieces, last = [], rest
    for sep in [*re.finditer(r"[,;]|\s+и\s+", text[rest:end]), None]:
        stop = rest + sep.start() if sep else end
        if text[last:stop].strip():
            pieces.append((last + len(text[last:stop]) - len(text[last:stop].lstrip()), stop))
        last = rest + sep.end() if sep else end
    marks = [INSTANCE.match(text[s:e]) for s, e in pieces]
    if len(pieces) < 2 or not all(marks):
        return False
    label = lemma(m.group("noun"), m.group("num"))
    group = Product(kind="item", label=label, scopes=((start, rest),))
    if value := _value(m.group("num")):
        group.counts.append(Count(start + m.start("num"), start + m.end("num"), value,
                                  not label.startswith(SET_COUNTED), (start + m.start("num"), start + m.end("noun"))))
    products.append(group)
    index = len(products) - 1
    for number, ((s, e), mark) in enumerate(zip(pieces, marks, strict=True), 1):
        item = Product(kind="item", label=f"{label} {number}", parent=index, scopes=((s, e),),
                       instance=True)
        if value := _value(mark.group("mark")):
            item.counts.append(Count(s, s + mark.end(), value, True, (s, e)))
        products.append(item)
    return True


def _sentence(text: str, start: int, end: int, products: list[Product]) -> None:
    piece = text[start:end]
    if (m := SET_HEAD.fullmatch(piece)) and (found := _entries(text, start + m.start("rest"), end, bare=True)):
        name = " ".join(norm(word) for word in m.group("name").split())
        product = Product(kind="set", label=name, scopes=((start, start + m.end("name")),),
                          noun=lemma(m.group("noun")))
        if m.group("num") and (value := _value(m.group("num"))):
            product.counts.append(Count(start + m.start("num"), start + m.end("num"), value, False,
                                        (start + m.start("num"), start + m.end("name"))))
        products.append(product)
        _add_entries(products, found, "part", len(products) - 1)
        return
    if (m := SET_TAIL.fullmatch(piece)) and (found := _entries(text, start, start + m.end("rest"), bare=True)):
        tail = (start + m.end("rest") + 1, end)
        products.append(Product(kind="set", label=lemma(m.group("noun")), scopes=(tail,), counts=[
            Count(start + m.start("num"), start + m.end("num"), _value(m.group("num")) or "1",
                  not norm(m.group("noun")).startswith(SET_COUNTED), tail)]))
        _add_entries(products, found, "part", len(products) - 1)
        return
    if (m := INSTANCES.fullmatch(piece)) and _product_word(m.group("noun")) and _instances(text, start, end, m, products):
        return
    found = _entries(text, start, end)
    if not found:
        bare = _entries(text, start, end, bare=True)
        found = bare if bare and all("num" not in match.groupdict() for match, _ in bare) else None
    if found and len(found) >= 2 and len({lemma(m.group("noun")) for m, _ in found}) >= 2:
        _add_entries(products, found, "item", None)


def _membership(text: str, products: list[Product]) -> None:
    layout = Layout(text, products, None)
    for m in IN_SET.finditer(text):
        member, owner = layout.find(m.group("noun")), layout.find(m.group("set"))
        if member is not None and owner is not None and products[owner].kind == "set" and member != owner:
            products[member].kind, products[member].parent = "part", owner
    for m in SET_HAS.finditer(text):
        owner = layout.find(m.group("set"))
        if owner is not None and products[owner].kind == "set" and layout.find(m.group("noun")) is None \
                and _product_word(m.group("noun")):
            products.append(Product(kind="part", label=norm(m.group("noun")), parent=owner))
    counted = {(c.start, c.end) for p in products for c in p.counts}
    for m in COUNTED.finditer(text):
        # «две дрели отдельно»: a number before a product's name counts that product.
        index, value = layout.find(m.group("noun")), _value(m.group("num"))
        if index is not None and value and (m.start("num"), m.end("num")) not in counted:
            pieces = not products[index].label.startswith(SET_COUNTED)
            products[index].counts.append(Count(m.start("num"), m.end("num"), value, pieces, (m.start(), m.end())))


# Category nouns a description may lead with before naming the product kind.
_CATEGORIES = ("обув", "одежд", "спецодежд", "мебел", "инструмент", "издели", "товар")


@lru_cache(maxsize=256)
def layout(text: str) -> Layout | None:
    """Products named in the text, or None when it describes one product (the single root item)."""
    products = _table(text)
    if not products:
        for start, end in sentences(text):
            _sentence(text, start, end, products)
    if not products:
        return None
    _membership(text, products)
    if sum(p.parent is None for p in products) < 2 and all(p.parent is None for p in products):
        return None
    tops = [p for p in products if p.parent is None]
    if (len(tops) == 2 and all(p.kind == "item" and not p.counts for p in tops)
            and norm(tops[0].noun).startswith(_CATEGORIES) and len(products) == 2):
        return None  # «Обувь специальная диэлектрическая, боты клееные»: the boots are that footwear
    root = next((i for i, p in enumerate(products) if p.kind == "item" and p.parent is None), None)
    return Layout(text, products, root)


def split_descriptions(text: str, product_names: Iterable[str] = (),
                       property_names: Callable[[str], Iterable[str]] | None = None) -> list[str]:
    """Independent descriptions for separate cards; keep parts and explicit sets together."""
    # Spreadsheet headers describe the cells, not another product; keep the column names with their values.
    lines = [line for line in text.splitlines() if line.strip()]
    headers = [cell.strip() for cell in lines[0].split("\t")] if lines else []
    name_column = next((i for i, name in enumerate(headers) if norm(name).split()[:1]
                        and norm(name).split()[0] in TABLE_HEADS), None)
    if len(headers) > 1 and name_column is not None and len(lines) > 1:
        descriptions = []
        for line in lines[1:]:
            cells = [cell.strip() for cell in line.split("\t")]
            if len(cells) <= name_column or not cells[name_column]:
                return [text]  # malformed rows stay visible instead of losing their contents
            description = [cells[name_column]]
            for i, value in enumerate(cells):
                label = headers[i] if i < len(headers) else ""
                if value and i != name_column and norm(label) not in {"№", "номер", "п/п", "№ п/п"}:
                    description.append(f"{label}: {value}" if label else value)
            descriptions.append("; ".join(description))
        return descriptions

    found = layout(text)
    if found and any(p.parent is not None for p in found.products) and (IN_SET.search(text) or SET_HAS.search(text)):
        return [text]  # membership stated later must not lose its set when separate cards are made
    # ponytail: list markers define item boundaries even for products absent from the catalog.
    marker_pattern = r"[ \t]*(?:\d+[.)]|[-•*])[ \t]+"
    markers = list(re.finditer(rf"(?m)(?:^|(?<=;)){marker_pattern}", text))
    numbered = [marker for marker in markers if marker.group().lstrip()[:1].isdigit()]
    if len(numbered) > 1:
        markers = numbered  # bullets inside numbered items describe their characteristics
    protected = [(start, end) for start, end in sentences(text)
                 if (local := layout(text[start:end])) and any(p.parent is not None for p in local.products)]
    rows: list[list[str]] = []
    heads: list[str] = []
    product_names = tuple(product_names)
    known_heads = {head for name in product_names if (head := head_noun_stem(name))}
    heads_by_prefix = {prefix: tuple(group) for prefix, group in groupby(sorted(known_heads), key=lambda head: head[:4])}
    known_phrases = {" ".join(re.findall(_W, norm(name))) for name in product_names}
    phrase_widths = {len(name.split()) for name in known_phrases}
    found_nouns = {norm(product.noun) for product in found.products} if found else set()

    @lru_cache(maxsize=1024)
    def named(noun: str) -> bool:
        noun_heads = content_stems(noun) + content_stems(alias_of(noun) or "")
        return (any(stems_match(word, name) for word in noun_heads for name in heads_by_prefix.get(word[:4], ()))
                or norm(noun) in found_nouns
                or bool(found and any(same_noun(noun, p.noun) for p in found.products)))

    @lru_cache(maxsize=128)
    def properties(product: str) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(parts) for name in property_names(product) if (
            parts := re.findall(_W, norm(re.sub(r"\([^)]*\)", "", name))))) if property_names else ()

    current = 0
    last, previous_separator = 0, ""
    separators = [m for m in re.finditer(r";|\n+|(?<=[.!?])\s+|,\s+|\s+и\s+", text, re.IGNORECASE)
                  if not any(start <= m.start() < end for start, end in protected)
                  and not any(marker.start() <= m.start() < marker.end() for marker in markers)]
    if len(markers) > 1:
        boundaries = {marker.start() for marker in markers}
        separators = [separator for separator in separators if separator.end() in boundaries]
    for separator in [*separators, None]:
        raw = text[last:separator.start() if separator else len(text)]
        piece = raw.strip()
        if not piece:
            last = separator.end() if separator else len(text)
            previous_separator = separator.group() if separator else ""
            continue
        marked = re.match(marker_pattern, piece)
        if marked:
            piece = piece[marked.end():]
        head = _LEAD.sub("", piece, count=1)
        head = re.sub(rf"^(?:{_NUM})(?=\s|{_DASH})\s*(?:{_DASH}\s*)?", "", head, flags=re.IGNORECASE)
        words = re.findall(_W, head)
        while words and re.fullmatch(_ADJ, words[0], re.IGNORECASE):
            words.pop(0)
        noun = words[0] if words else ""
        named_product = named(noun)
        phrase = re.findall(_W, norm(head))
        local_properties = [parts for parts in properties(heads[current]) if phrase and parts[0] == phrase[0]] \
            if rows and property_names and not marked else []
        if separator and re.fullmatch(r",\s+|\s+и\s+", separator.group(), re.IGNORECASE) and local_properties:
            # Catalog labels and dictionary values can contain commas: inspect the complete wording before cutting it.
            ahead = list(islice(re.finditer(_W, norm(text[last:])), max(map(len, local_properties))))
            if any([word.group() for word in ahead[:len(parts)]] == list(parts)
                   and last + ahead[len(parts) - 1].end() > separator.start() for parts in local_properties):
                continue
        last = separator.end() if separator else len(text)
        product_size = max((i for i in phrase_widths if i <= len(phrase)
                            if " ".join(phrase[:i]) in known_phrases), default=0)
        property_size = max((len(parts) if tuple(phrase[:len(parts)]) == parts else 1
                             for parts in local_properties if tuple(phrase[:len(parts)]) == parts
                             or (len(phrase) > 1 and phrase[1] in {"да", "нет"})), default=0)
        describes_property = property_size > 0 and property_size >= product_size
        continues = bool(not marked and rows and same_noun(noun, heads[current]) and re.match(
            rf"{_W}\s+(?:{_ADJ}\s+)*(?:оснащ|предназнач|оборудован|имеет|должен|должна|может|содержит)",
            head, re.IGNORECASE))
        member = bool(rows and norm(heads[current]).startswith(SET_NOUNS)
                      and (previous_separator.lstrip().startswith(",") or norm(previous_separator).strip() == "и"))
        first_product = not rows and not piece.endswith(":")
        if (noun and norm(noun) not in canonical._PREPOSITIONS
                and (_product_word(noun) or (marked and named_product))
                and (named_product or marked or first_product or not known_heads)
                and not continues and not member and not describes_property):
            if len(rows) == 1 and not heads[0]:
                heads[0] = noun  # introductory text belongs to the first actual product
            else:
                current = len(rows)
                heads.append(noun)
                rows.append([])
        elif rows:
            owners = {i for word in re.findall(_W, piece) for i, name in enumerate(heads) if same_noun(word, name)}
            if len(owners) == 1:
                current = owners.pop()
        if not rows:
            heads.append("")
            rows.append([])
        rows[current].append(piece)
        previous_separator = separator.group() if separator else ""
    return ["; ".join(row) for row in rows] if len(rows) > 1 else [text]
