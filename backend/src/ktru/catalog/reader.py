"""Read-only access to a published snapshot: positions, families, characteristics and lexical search."""

import bisect
import functools
import json
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from functools import lru_cache
from itertools import groupby
from pathlib import Path

from rapidfuzz import process as rf_process
from rapidfuzz.distance import OSA

from src.ktru import contracts as C
from src.ktru.catalog.validation import value_defects, value_type
from src.ktru.engine import canonical, products
from src.ktru.engine.spelling import (
    common_word,
    dictionary_neighbours,
    edit_cost,
    inflection_base,
    inflection_variant,
    known_word,
    noun_lemma,
    typo_cost,
)
from src.ktru.engine.text import (
    ALIASES,
    NOT_A_PRODUCT,
    OKEI,
    STOP,
    alias_of,
    content_stems,
    head_noun,
    head_noun_stem,
    is_adjective,
    lemma_stem,
    norm,
    quantities,
    stem,
    stems_match,
    with_aliases,
    words,
)

_CYRILLIC_WORD = re.compile(r"[а-яё]+(?:-[а-яё]+)*")
# Dimensions that tell product kinds apart; every product has a size, a mass and a count.
_SPECIFIC_DIMENSIONS = frozenset({"power", "voltage", "charge", "frequency", "rotation", "pressure", "temperature",
                                  "current", "energy", "sound", "volume", "volume_flow", "speed", "data", "force",
                                  "illuminance", "apparent_power", "torque", "resistance"})
_VOWELS = set("аеёиоуыэюя")
# Segment boundaries of a description; a decimal point or comma between digits is not one («10,8 В»).
_SEGMENT_SPLIT = re.compile(r"((?<!\d)[.,](?!\d)|[;:\n()!?])")


_METALS = {"металл", "сталь", "алюминий", "чугун", "латунь", "медь", "нержавеющая сталь", "титан", "бронза"}


def _materials(text: str) -> set[str]:
    """Coarse materials named in a text; any metal is «металл»."""
    found = {canonical.material(w) for w, _, _ in words(text)}
    return {"металл" if m in _METALS else m for m in found if m}


def _name_heads(name: str, stems: list[str]) -> list[str]:
    """Product nouns a family name lists: «Перчатки, рукавицы (варежки) бытового назначения» → перчатк, рукавиц,
    варежк. Each comma, semicolon or bracket piece adds its first noun; a leading adjective is not a product
    («Ударная установка» is an «установка», «Журнальный стол» a «стол»)."""
    first = head_noun_stem(re.split(r"[,;()]", name)[0])
    heads = [first] if first else stems[:1]
    noun = head_noun(re.split(r"[,;()]", name)[0])
    if noun and (lemma := lemma_stem(noun)) not in heads:
        heads.append(lemma)  # «Токарные резцы» is also «резец»
    for piece in re.split(r"[,;()]", name)[1:]:
        lead = piece.split()[:1]
        if lead and norm(lead[0]) in STOP:  # «…, с пневматическим приводом» qualifies, it does not list a product
            continue
        head = head_noun_stem(piece)
        if head and head not in heads and head not in NOT_A_PRODUCT:
            heads.append(head)
    return heads


def _segments(text: str) -> list[str]:
    """Text pieces in order, without the ones that name a property before a colon («Тип каркаса: деревянный»).
    A leading product phrase before a colon is kept: «набор ножей хозяйственных: ножницы, нож для сыра»."""
    parts = _SEGMENT_SPLIT.split(text)
    out = []
    for i, (seg, delim) in enumerate(zip(parts[::2], [*parts[1::2], ""], strict=False)):
        first = next(iter(words(seg)), None)
        header = delim == ":" and (i > 0 or first is None or canonical.property_name(first[0]) is not None
                                   or norm(first[0]) in _HEADER_WORDS)
        if seg.strip() and not header:
            out.append(seg)
    return out


_HEADER_WORDS = {"тип", "вид", "назначение", "модель", "артикул", "марка", "характеристики", "состав", "комплектация",
                 "наличие", "количество", "товар", "наименование", "описание", "параметр", "параметры"}


def _counted_noun(text: str, set_stem: str) -> str | None:
    """Stem of the noun after «набор»/«пара» when it counts a product («набор отверток», «пар рабочих перчаток»);
    None when a preposition or nothing follows the adjectives («набор демонстрационный по…», «комплект для…»)."""
    tokens = [norm(w) for w, _, _ in words(text)]
    for i, w in enumerate(tokens):
        if stem(w) == set_stem:
            for nxt in tokens[i + 1:i + 4]:  # «пар рабочих перчаток»: adjectives before the counted noun
                if nxt in STOP:
                    return None
                if not is_adjective(nxt):
                    return stem(nxt)
            return None
    return None


def _skeleton(word: str) -> str:
    """The word without doubled letters and ь/ъ: «стеллажа» and «стелажа», «карандашь» and «карандаш» match."""
    return re.sub(r"(.)\1+", r"\1", word.replace("ь", "").replace("ъ", ""))


@dataclass
class Char:
    char_code: str
    name: str
    kind_raw: str | None
    required: bool
    multi: bool
    allowed: list[dict]
    value_type: str = "unknown"
    defects: list[str] = field(default_factory=list)
    source: C.CatalogSource | None = None
    ref: C.CharacteristicRef | None = None
    type_raw: str | None = None


@dataclass
class Position:
    code: str
    version: int
    name: str
    okpd2_code: str | None
    okpd2_name: str | None
    status: str
    is_template: bool
    family_id: str
    domain: str
    supply_units: list[dict]
    chars: list[Char] = field(default_factory=list)
    ref: C.PositionRef | None = None
    parent_ref: C.PositionRef | None = None
    rubricators: list[dict] = field(default_factory=list)
    application_date_start: str | None = None
    application_date_end: str | None = None
    source: C.CatalogSource | None = None


@dataclass
class PortalChar:
    category: str
    name: str
    value_type: str | None
    allowed: list[str]
    unit: str | None


@dataclass
class FamilyHit:
    family_id: str
    name: str
    template: Position | None
    members: list[Position]  # ACTIVE non-template finals, complete membership
    bm25: float
    name_coverage: float  # share of the family-name content words present in the query
    query_coverage: float  # share of query content words present in the family name
    head_ok: bool
    membership_complete: bool = True
    semantic_score: float | None = None
    semantic_pick: bool = False  # the semantic index's confident first choice: may be proposed without a head match
    outside_group: str | None = None  # a family outside the served groups that the description is clearly nearer to


@dataclass(frozen=True)
class FamilyMatch:
    """Lightweight ranking data; full schemas are loaded only for retained families."""

    family_id: str
    name: str
    bm25: float
    name_coverage: float
    query_coverage: float
    head_ok: bool
    head_exact: bool
    in_scope: bool = False  # The family belongs to a domain the product serves (clothing, furniture, tools).


@dataclass(frozen=True)
class SearchResult:
    families: list[FamilyHit]
    examined: int
    row_truncated: bool
    family_truncated: bool

    def __iter__(self):
        yield self.families
        yield self.examined

    def __getitem__(self, index):
        return (self.families, self.examined)[index]


class Catalog:
    def __init__(self, snapshot_dir: Path):
        self.dir = snapshot_dir
        self._local = threading.local()
        self._families: dict[str, tuple[Position | None, list[Position]]] = {}
        self._family_names: dict[str, str] | None = None
        self._family_counts: dict[str, int] = {}
        self._family_in_scope: set[str] = set()
        self._family_domain: dict[str, str] = {}
        self._head_domains: dict[str, str] | None = None
        self._family_heads: set[str] = set()
        self._plausibility: dict[str, dict] = {}
        self._noun_families: dict[str, list[str]] | None = None
        self._family_dims: dict[str, frozenset[str]] = {}
        self._family_name_stems: dict[str, list[str]] = {}
        self._family_noun_heads: dict[str, list[str]] = {}
        self._portal: dict[tuple[str, str], list[PortalChar]] = {}
        self._portal_cats: dict[str, list[tuple[str, list[str]]]] = {}
        self._domain_stems: dict[str, frozenset[str]] = {}
        self.manifest = json.loads((snapshot_dir / "manifest.json").read_text())
        self.snapshot_id: str = self.manifest["snapshot_id"]
        db = self._db()
        self._metadata_tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        freq: Counter[str] = Counter()
        for (name,) in db.execute("SELECT name FROM positions WHERE status='ACTIVE'"):
            freq.update(norm(w) for w, _, _ in words(name) if len(w) > 3)
        for (name,) in db.execute("SELECT DISTINCT name FROM characteristics"):
            freq.update(norm(w) for w, _, _ in words(name) if len(w) > 3)
        labels = db.execute(
            "SELECT DISTINCT json_extract(j.value, '$.label') FROM characteristics, json_each(allowed) j "
            "WHERE json_extract(j.value, '$.kind') = 'enum'"
        )
        for (label,) in labels:
            freq.update(norm(w) for w, _, _ in words(label or "") if len(w) > 3)
        self.vocab = set(freq)
        self._freq = freq
        self._stem_freq: Counter[str] = Counter()
        for w, n in freq.items():
            self._stem_freq[stem(w)] += n

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(
                (self.dir / "catalog.sqlite").resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False
            )
            self._local.db = db
        return db

    # ---- positions ----------------------------------------------------------------------------------------------

    _COLS = ("code, version, name, okpd2_code, okpd2_name, status, is_template, family_id, domain, supply_units, "
             "parent_code, parent_version, application_date_start, application_date_end, source")

    def _row_to_pos(self, r) -> Position:
        metadata = None
        if "position_metadata" in self._metadata_tables:
            metadata = self._db().execute(
                "SELECT rubricators, source_meta FROM position_metadata WHERE position_code=? AND position_version=?",
                (r[0], r[1]),
            ).fetchone()
        source = json.loads(metadata[1]) if metadata and metadata[1] else {"xml": r[14]} if r[14] else None
        parent = None
        if r[10] and r[11] is not None:
            try:
                parent = C.PositionRef(snapshot_id=self.snapshot_id, code=r[10], version=int(r[11]))
            except (TypeError, ValueError):
                # Retain the position; an invalid parent version cannot become a different version.
                pass
        return Position(
            r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7], r[8], json.loads(r[9] or "[]"),
            ref=C.PositionRef(snapshot_id=self.snapshot_id, code=r[0], version=r[1]), parent_ref=parent,
            rubricators=json.loads(metadata[0] or "[]") if metadata else [],
            application_date_start=r[12], application_date_end=r[13],
            source=C.CatalogSource.model_validate(source) if source else None,
        )

    def chars(self, code: str, version: int) -> list[Char]:
        rows = self._db().execute(
            "SELECT char_code, name, kind_raw, required, multi, allowed, type_raw, actual FROM characteristics "
            "WHERE position_code=? AND position_version=? ORDER BY rowid",
            (code, version),
        )
        metadata = {}
        if "characteristic_metadata" in self._metadata_tables:
            metadata = {r[0]: r[1:] for r in self._db().execute(
                "SELECT characteristic_index,value_type,defects,source_meta FROM characteristic_metadata "
                "WHERE position_code=? AND position_version=?", (code, version)
            )}
        position_source = self._db().execute(
            "SELECT source FROM positions WHERE code=? AND version=?", (code, version)
        ).fetchone()
        result = []
        rows = list(rows)
        char_codes = Counter(r[0] for r in rows)
        for row_index, r in enumerate(rows):
            if not r[7]:
                continue
            identity_defects = (["missing_char_code"] if not r[0] else
                                ["duplicate_char_code"] if char_codes[r[0]] > 1 else [])
            ref = None if identity_defects else C.CharacteristicRef(snapshot_id=self.snapshot_id,
                                      position_code=code, position_version=version, char_code=r[0])
            allowed = []
            for value in json.loads(r[5] or "[]"):
                defects = value_defects(value)
                value_ref = C.AllowedValueRef(characteristic_ref=ref, value_code=value["value_code"]) \
                    if ref and value.get("value_code") else None
                allowed.append({**value, "ref": value_ref.model_dump() if value_ref else None, "defects": defects,
                                **({"defect": defects[0]} if defects else {})})
            meta = metadata.get(row_index)
            source = json.loads(meta[2]) if meta and meta[2] else (
                {"xml": position_source[0]} if position_source and position_source[0] else None
            )
            defects = list(dict.fromkeys([
                *(json.loads(meta[1] or "[]") if meta else []),
                *(d for value in allowed for d in value["defects"]),
                *(["unknown_kind"] if r[2] not in ("1", "2", "3") else []),
                *identity_defects,
            ]))
            result.append(Char(
                r[0], r[1], r[2], bool(r[3]), bool(r[4]), allowed,
                value_type=meta[0] if meta else value_type(allowed), defects=defects,
                source=C.CatalogSource.model_validate(source) if source else None, ref=ref, type_raw=r[6],
            ))
        return result

    def position(self, code: str, version: int | None = None) -> Position | None:
        q = f"SELECT {self._COLS} FROM positions WHERE code=?"
        args: tuple = (code,)
        if version is not None:
            q += " AND version=?"
            args = (code, version)
        r = self._db().execute(q + " ORDER BY version DESC LIMIT 1", args).fetchone()
        if not r:
            return None
        p = self._row_to_pos(r)
        p.chars = self.chars(p.code, p.version)
        return p

    def family(self, family_id: str) -> tuple[Position | None, list[Position]]:
        cached = self._families.get(family_id)
        if cached is not None:
            return cached
        rows = self._db().execute(
            f"SELECT {self._COLS} FROM positions WHERE family_id=? AND status='ACTIVE' ORDER BY code", (family_id,)
        )
        template, members = None, []
        for r in rows:
            p = self._row_to_pos(r)
            p.chars = self.chars(p.code, p.version)
            if p.is_template:
                template = p
            else:
                members.append(p)
        self._families[family_id] = (template, members)
        return template, members

    # ---- search -------------------------------------------------------------------------------------------------

    @functools.cached_property
    def _vocab_by_length(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = defaultdict(list)
        for w in self.vocab:
            if _CYRILLIC_WORD.fullmatch(w):
                out[len(w)].append(w)
        return out

    @functools.cached_property
    def _sorted_vocab(self) -> list[str]:
        return sorted(self.vocab)

    @functools.cached_property
    def _unseen_form_bases(self) -> frozenset[str]:
        """Bases of catalog words the dictionary lacks («шестигранной»): other endings may be real forms too."""
        bases = (inflection_base(w) for w in self.vocab if not known_word(w))
        return frozenset(base for base in bases if len(base) >= 7)  # Short bases («свет») are shared by many words.

    def is_truncation(self, w: str) -> bool:
        """«офисн», «подкл»: the start of a longer catalog word cut after a consonant, as in short descriptions."""
        n = norm(w)
        i = bisect.bisect_right(self._sorted_vocab, n)
        return len(n) >= 4 and n[-1] not in _VOWELS | {"й", "ь", "ъ"} and i < len(self._sorted_vocab) and self._sorted_vocab[i].startswith(n) and \
            len(self._sorted_vocab[i]) >= len(n) + 2

    @lru_cache(maxsize=4096)
    def has_word(self, w: str) -> bool:
        """A real word: in the Russian dictionary or common in the catalog (шуруповерт, бесщеточная)."""
        return common_word(w) or self._freq.get(norm(w), 0) >= 3

    def is_common(self, w: str) -> bool:
        """A word the catalog uses often: safe as a piece when glued text is split."""
        return self._freq.get(norm(w), 0) >= 20

    def correct_word(self, w: str) -> str | None:
        """Typo-tolerant lexical channel: an unknown spelling with a much more common catalog word nearby.

        Up to 1 edit when both words have at most 5 letters and 2 otherwise; a swap counts as one edit. Candidates are ranked by
        a typo cost where neighbouring keys, о/а-like vowels, н/нн and ь/ъ are cheap, then by catalog frequency.
        A real word (in the Russian dictionary or exactly in the catalog) only gets a doubled-letter or ь/ъ repair,
        since the catalog has its own spellings (e.g. «стелажа») and real words must not become other words.
        """
        n = norm(w)
        if len(n) < 3 or not _CYRILLIC_WORD.fullmatch(n) or any(n.startswith(slang) for slang in ALIASES):
            return None
        if (w.isupper() and len(n) <= 5) or self.is_truncation(n):
            return None  # Abbreviations («КТРУ») and shortened words («офисн») are written that way on purpose.
        squeezed = re.sub(r"(.)\1{2,}", r"\1", w.lower())  # Never three same letters: «куууртка», «ооочень».
        if squeezed != w.lower():
            pair = re.sub(r"(.)\1{2,}", r"\1\1", w.lower())  # «сериии» → «серии» before «куууртка» → «куртка»
            fixed = next((v for v in (pair, squeezed, re.sub(r"(.)\1+", r"\1", squeezed))
                          if self._freq.get(norm(v), 0) >= 5 or known_word(norm(v))), None)
            if fixed:
                return fixed
        own_word = self._freq.get(n, 0)
        if own_word >= 5:
            return None
        protected = own_word > 0 or known_word(n) or inflection_base(n) in self._unseen_form_bases
        if len(n) == 3 and (protected or w.isupper()):
            return None  # Three letters: only a non-word with a dropped letter («сул», «шкф»); never «ПВХ».
        sizes = (4,) if len(n) == 3 else range(len(n) - 2, len(n) + 3)
        pool = [c for size in sizes for c in self._vocab_by_length.get(size, ())]
        found = {cand: distance for cand, distance, _ in
                 rf_process.extract(n, pool, scorer=OSA.distance, score_cutoff=2, limit=None)}
        if not protected and len(n) > 3:  # Real words the catalog does not use («патрон», «тёплая») are fixes too.
            found.update(dict.fromkeys(dictionary_neighbours(n) - found.keys(), 1))
        best: tuple[float, int, int, str] | None = None  # catalog words
        fallback: tuple[float, int, int, str] | None = None  # words only the Russian dictionary knows
        for cand, distance in found.items():
            if cand == n or distance > (1 if max(len(n), len(cand)) <= 5 else 2):
                continue
            if len(n) == 3 and not set(n) & _VOWELS and not any(
                    cand[:i] + cand[i + 1:] == n and cand[i] in _VOWELS for i in range(len(cand))):
                continue  # A consonant cluster is a word typed without its vowel, or an abbreviation.
            freq = self._freq.get(cand, 0)
            cost = typo_cost(n, cand)
            if len(n) == 3 and not set(n) & _VOWELS and freq < 50:
                continue  # «стл» is «стол», but «вст» is an abbreviation, not the rare «вест».
            if (not freq and cost > 1) or (distance == 2 and len(n) <= 5 and cost > 1):
                continue  # A guess for a short word needs cheap edits: «пофиг» is neither «тофиг» nor «порфир».
            if protected:
                if _skeleton(cand) != _skeleton(n) or freq < max(10, 10 * own_word):
                    continue
            elif not known_word(cand) and (freq < 5 or self._stem_freq[stem(cand)] < 10
                                           or inflection_variant(n, cand)):
                # A catalog-only word must be common, and «шестигранных» may be a form of «шестигранной».
                continue
            key = (cost, distance, -freq, cand)  # Equal cost: one edit beats two, then the common word wins.
            if freq and (best is None or key < best):
                best = key
            elif not freq and (fallback is None or key < fallback):
                fallback = key
        # The catalog speaks the product language; a dictionary word must be clearly closer to replace it
        # (letter edits only: a catalog word with another first letter is still the likelier one).
        if fallback and (best is None or edit_cost(n, fallback[3]) <= edit_cost(n, best[3]) - 0.75):
            best = fallback
        return best[3] if best and best[0] <= 2 else None

    def family_names(self) -> dict[str, str]:
        if self._family_names is None:
            names, counts, scoped = {}, Counter(), set()
            rows = self._db().execute(
                "SELECT family_id, name, is_template, domain FROM positions WHERE status='ACTIVE' ORDER BY code, version"
            )
            for family_id, name, is_template, domain in rows:
                if is_template or family_id not in names:
                    names[family_id] = name
                if not is_template:
                    counts[family_id] += 1
                    if domain != "unknown":
                        scoped.add(family_id)
                        self._family_domain[family_id] = domain
            self._family_in_scope = scoped
            self._family_counts = dict(counts)
            active_names = {fid: name for fid, name in names.items() if fid in counts}
            self._family_name_stems = {fid: content_stems(name) for fid, name in active_names.items()}
            self._family_noun_heads = {fid: _name_heads(name, self._family_name_stems[fid])
                                       for fid, name in active_names.items()}
            self._family_names = active_names
        return self._family_names

    def search_text(self, text: str) -> str:
        """Search-only expansion: catalog names for everyday words, dictionary forms for «курток», «отверток»."""
        text = with_aliases(text)
        lemmas = []
        for w, _, _ in words(text):
            lemma = noun_lemma(norm(w)) if _CYRILLIC_WORD.fullmatch(norm(w)) else None
            if lemma and stem(lemma) != stem(w):
                lemmas.append(lemma)
        return f"{text} {' '.join(dict.fromkeys(lemmas))}" if lemmas else text

    def head_domains(self) -> dict[str, str]:
        """Product-noun stem → domain, from in-scope family names, Portal categories and real listings (СТЕ)."""
        if self._head_domains is None:
            self.family_names()
            votes: dict[str, Counter[str]] = {}
            named: set[str] = set()  # heads of catalog family names and Portal categories: products by definition

            def vote(head: str | None, domain: str, weight: int) -> None:
                if head:
                    votes.setdefault(head, Counter())[domain] += weight

            db = self._db()
            for family_id, domain in self._family_domain.items():
                for head in self._family_noun_heads[family_id]:
                    self._family_heads.add(head)
                    named.add(head)
                    vote(head, domain, 20)
            for domain, category in db.execute("SELECT DISTINCT domain, category FROM portal_chars"):
                head = head_noun_stem(category)
                named.add(head)
                vote(head, domain, 20)
            if "noun_class" in self._metadata_tables:
                for head, domain in db.execute("SELECT head, domain FROM noun_class"):
                    named.add(head)
                    vote(head, domain, 20)
            if "ste" in self._metadata_tables:
                for domain, name in db.execute("SELECT domain, name FROM ste"):
                    vote(head_noun_stem(name), domain, 1)
            self._head_domains = {}
            for head, counter in votes.items():
                if head in NOT_A_PRODUCT and head not in self._family_heads:
                    continue  # «набор» heads «Набор демонстрационный…»; «пара», «штука» never name a product
                domain, top = counter.most_common(1)[0]
                # A catalog class name is a product even when real listings use the word in another group too
                # («ложка» for shoes; «петля» for buttonholes); listings alone must agree on the group.
                if head in named or (top >= 20 and top >= 0.8 * sum(counter.values())):
                    self._head_domains[head] = domain
        return self._head_domains

    def text_heads(self, text: str) -> list[tuple[str, str]]:
        """(stem, domain) of the words that name a product of a served domain: the product segment's heads first
        («Ложка кулинарная. Материал: дерево» names a spoon, not a material), then the rest in text order."""
        found = []
        for segment in _segments(text):
            for head in self._segment_heads(segment):
                if head not in found:
                    found.append(head)
        found = found or self._segment_heads(text)
        if len(found) > 1 and found[0][0] in NOT_A_PRODUCT:
            # «Набор ножек для стола»: a set noun competes for the family but never decides the product's group.
            found = [found[1], found[0], *found[2:]]
        return found

    def _segment_heads(self, text: str) -> list[tuple[str, str]]:
        heads, found = self.head_domains(), []
        for w, _, _ in words(text):
            n = norm(w)
            if not re.fullmatch(r"[а-яё-]+", n):
                # «JPC75-BL-XXL Ninja Комбинезон»: article codes and Latin brands are not product nouns;
                # «Пассатижи-ZEBRA-200мм» keeps its leading Russian word.
                n = n.split("-")[0] if re.fullmatch(r"[а-яё]+", n.split("-")[0]) else ""
            if n in STOP or len(n) < 3:
                continue
            lemma = noun_lemma(n)
            # The catalog's own class name for an everyday word comes first («ботинки» → «Обувь …»).
            alias = alias_of(n) or (alias_of(lemma) if lemma else None)
            own = [stem(n)] + ([stem(lemma)] if lemma else [])
            if "-" in n:  # «Стойка-держатель», «Шкаф-купе»: the first part names the product
                first = n.split("-")[0]
                own += [stem(first)] + ([stem(x)] if (x := noun_lemma(first)) else [])
                alias = alias or alias_of(first)
            if alias and not any(c in self._family_heads for c in own):  # «ветровка» has its own family
                own = [head_noun_stem(alias) or content_stems(alias)[0], *own]
            candidates = own
            for c in candidates:
                if c in heads:
                    found.append((c, heads[c]))
                    break
        return found

    @functools.cached_property
    def family_documents(self) -> dict[str, str]:
        """«name. OKPD2 class. Характеристики: …» of every catalog family, for semantic retrieval."""
        names = self.family_names()
        db = self._db()
        okpd = dict(db.execute("SELECT family_id, okpd2_name FROM positions WHERE is_template=0"))
        chars: dict[str, list[str]] = defaultdict(list)
        for family_id, name in db.execute(
                "SELECT DISTINCT p.family_id, c.name FROM characteristics c JOIN positions p "
                "ON p.code = c.position_code AND p.version = c.position_version"):
            if len(chars[family_id]) < 12:
                chars[family_id].append(name)
        return {f: (f"{names[f]}. {okpd.get(f) or ''}. Характеристики: " + ", ".join(chars[f]))[:600] for f in names}

    def product_segment(self, text: str) -> str:
        """The text that names the product: everything before the first property name with a colon or the first
        number («Стул ученический. Тип каркаса» of «Стул ученический. Тип каркаса: деревянный»)."""
        searched = self.search_text(text)
        end = next((m.start() for m in re.finditer(r"[:\d]", searched)), len(searched))
        return searched[:end]

    def family_dimensions(self, family_id: str) -> frozenset[str]:
        """Physical dimensions the family's characteristics are measured in («power» for «Дрель ручная электрическая»)."""
        if family_id not in self._family_dims:
            dims = set()
            for (allowed,) in self._db().execute(
                    "SELECT c.allowed FROM characteristics c JOIN positions p ON p.code = c.position_code "
                    "AND p.version = c.position_version WHERE p.family_id = ?", (family_id,)):
                for value in json.loads(allowed or "[]"):
                    if value.get("okei_code") in OKEI:
                        dims.add(OKEI[value["okei_code"]][0])
            self._family_dims[family_id] = frozenset(dims)
        return self._family_dims[family_id]

    def noun_families(self, head: str) -> list[str]:
        """In-group families of the OKPD2 class Portal listings with this product noun are filed under."""
        if "noun_class" not in self._metadata_tables:
            return []
        if self._noun_families is None:
            self._noun_families = {h: json.loads(f) for h, f in
                                   self._db().execute("SELECT head, families FROM noun_class")}
        return self._noun_families.get(head, [])

    def plausibility(self, head: str) -> dict[tuple[str, str], tuple]:
        """Typical listing values of a product noun («@clothing» for a group): (prop, dim) → (n, q01…q99).

        prop «#» counts listings (dim "" = all of them, otherwise those stating that dimension)."""
        if "plausibility" not in self._metadata_tables:
            return {}
        if head not in self._plausibility:
            rows = self._db().execute("SELECT prop, dim, n, q01, q05, q50, q95, q99 FROM plausibility WHERE head=?",
                                      (head,)).fetchall()
            self._plausibility[head] = {(prop, dim): tuple(rest) for prop, dim, *rest in rows}
        return self._plausibility[head]

    def guess_domain(self, text: str) -> str | None:
        """Domain of the product the text names, even when the catalog has no family for it."""
        heads = self.text_heads(text)
        return heads[0][1] if heads else None

    def search_matches(self, text: str) -> tuple[list[FamilyMatch], int, bool]:
        """Rank up to 64 lexical families without loading their characteristics."""
        source_text = text
        text = self.search_text(text)
        terms = []
        for w in [p for w0, _, _ in words(text) for p in w0.split("-")]:
            if norm(w) in STOP or len(w) < 3:
                continue
            s = stem(w)
            terms.append(f'"{s[: max(4, len(s) - 1)] if len(s) > 5 else s}"*')
        if not terms:
            return [], 0, False
        q = " OR ".join(dict.fromkeys(terms))
        rows = (
            self._db()
            .execute(
                "WITH matched AS MATERIALIZED ("
                "SELECT p.family_id, bm25(positions_fts, 0, 0, 10.0, 2.0, 0.5) AS s FROM positions_fts "
                "JOIN positions p ON p.code = positions_fts.code AND p.version = positions_fts.version "
                "WHERE positions_fts MATCH ? AND p.status='ACTIVE' AND p.is_template=0) "
                "SELECT family_id, MIN(s), COUNT(*) FROM matched GROUP BY family_id ORDER BY MIN(s), family_id LIMIT 65",
                (q,),
            )
            .fetchall()
        )
        pool_truncated = len(rows) > 64
        rows = rows[:64]
        best = {fam: score for fam, score, _count in rows}
        # Rare words («акт») can fill the pool; every family named after the product noun must still be ranked.
        for stem_, _domain in self.text_heads(text)[:1]:
            for fam, score in self._db().execute(
                "WITH matched AS MATERIALIZED (SELECT p.family_id, bm25(positions_fts, 0, 0, 10.0, 2.0, 0.5) AS s "
                "FROM positions_fts JOIN positions p ON p.code = positions_fts.code AND p.version = positions_fts.version "
                "WHERE positions_fts MATCH ? AND p.status='ACTIVE' AND p.is_template=0) "
                "SELECT family_id, MIN(s) FROM matched GROUP BY family_id ORDER BY MIN(s), family_id LIMIT 600",
                (f'"{stem_}"*',),
            ):
                if any(stems_match(h, stem_) for h in self._family_noun_heads.get(fam, ())):
                    best.setdefault(fam, score)
            # No in-group family named by the noun: the families of the class its Portal listings are filed under.
            if not self._own_family(best, stem_):
                for fam in self.noun_families(stem_):
                    best.setdefault(fam, min(best.values(), default=-10.0))
        hits = self.rank_matches(source_text, {fam: -score for fam, score in best.items()})
        return hits, sum(count for _fam, _score, count in rows), pool_truncated

    def _own_family(self, families: dict, head: str) -> bool:
        """An in-group family whose name starts with the product noun is among these."""
        return any(fam in self._family_in_scope and any(stems_match(h, head) for h in self._family_noun_heads.get(fam, ()))
                   for fam in families)

    def rank_matches(self, text: str, scores: dict[str, float]) -> list[FamilyMatch]:
        text = self.search_text(text)
        names = self.family_names()
        q_stems = content_stems(text)
        # The product noun leads; a leading adjective («Зимняя куртка») passes the role to the next word.
        layout = products.layout(text)
        product_text = (layout.products[layout.root].label if layout and layout.root is not None else
                        canonical.product_label(text)) or text
        first = [w for w, _, _ in words(product_text) if norm(w) not in STOP]
        adjective = bool(first) and norm(first[0]).endswith(("ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие"))
        lead = first[:2] if adjective else first[:1]
        q_head = [x for w in lead for x in content_stems(w)]  # all parts of «Дрель-шуруповерт»
        q_head += [lemma_stem(w) for w in lead if re.fullmatch(r"[А-Яа-яЁё]+", w)]  # «резец» for «резцы»
        q_head += [head_noun_stem(v) or content_stems(v)[0] for w in lead if (v := alias_of(w))]
        hits = []
        for fam, score in scores.items():
            name = names.get(fam)
            if name is None:
                continue
            n_stems = self._family_name_stems[fam]
            name_cov = sum(1 for a in n_stems if any(stems_match(a, b) for b in q_stems)) / max(1, len(n_stems))
            query_cov = sum(1 for b in q_stems if any(stems_match(a, b) for a in n_stems)) / max(1, len(q_stems))
            # The head noun (first word of the family name) must be present for a confident family.
            # Russian descriptions lead with the product noun («Ножовка по дереву» is not «Деревья»).
            head_ok = any(stems_match(h, b) for h in self._family_noun_heads[fam] for b in q_head)
            hits.append(FamilyMatch(fam, name, score, name_cov, query_cov, head_ok,
                                    bool(n_stems) and n_stems[0] in q_head, fam in self._family_in_scope))
        # The first word that names a product of a served domain is the head, also when the text leads with a unit
        # or a container («5 пар перчаток», «В упаковке 10 рубашек», «Закажем два изделия: халат…»).
        found = self.text_heads(text)
        leading = found[:1]
        counted = None
        set_head = next((h for h in found[:2] if h[0] in NOT_A_PRODUCT), None)
        if set_head:
            # «Набор демонстрационный …», «комплект для ремонта …» name catalog classes; «набор отверток»,
            # «пара перчаток» count a product. The set noun heads a family only when no noun follows it or when the
            # family is named by the same two words («Набор ножей хозяйственных»); the counted noun competes.
            leading, counted = found[:2], _counted_noun(text, set_head[0])
        named = {h.family_id for stem_, _ in leading for h in hits
                 if any(stems_match(head, stem_) for head in self._family_noun_heads[h.family_id])
                 and (not set_head or stem_ != set_head[0] or counted is None
                      or (len(self._family_name_stems[h.family_id]) > 1
                          and stems_match(self._family_name_stems[h.family_id][1], counted)))}
        for stem_, _ in self.text_heads(text)[:1]:
            classed = self.noun_families(stem_)
            if len(classed) == 1 and not self._own_family({h.family_id: 0 for h in hits}, stem_):
                named |= {h.family_id for h in hits if h.family_id in classed}
        if named:
            hits = [replace(h, head_ok=h.family_id in named) for h in hits]
        # Name words found in the product segment count first: «Стул ученический. Тип каркаса: деревянный» is
        # «Стул ученический», not «Стул на деревянном каркасе» whose extra words sit in a property value.
        segment = next((s for s in _segments(text) if self._segment_heads(s)), text)
        s_stems = content_stems(segment)
        in_segment = {h.family_id: sum(1 for a in self._family_name_stems[h.family_id]
                                       if any(stems_match(a, b) for b in s_stems)) for h in hits}
        # More query words found in the family name next: «...для защиты от нефти» beats «...виброзащитная»,
        # whose shorter name scores higher coverage on fewer matched words. Coverage then breaks ties.
        # Only the Portal groups (clothing, furniture, tools) are served: a family of another group never outranks
        # theirs; with no head word among them no family is chosen and the card asks («Катетер» stays unresolved).
        # A family named for another material than the text states ranks after the others: «Шкаф металлический
        # для документов» is not «Шкаф деревянный для документов».
        stated = _materials(text)
        clash = {h.family_id: bool(stated) and bool(m := _materials(h.name)) and not (m & stated) for h in hits}
        # «Дрель 600 Вт» is the electric drill: at equal name match, a family whose schema measures the stated
        # dimension (power, voltage, capacity, pressure…) ranks before one that has no such characteristic.
        measured = {q.dimension for q in quantities(text)} & _SPECIFIC_DIMENSIONS
        fits = {h.family_id: len(measured & self.family_dimensions(h.family_id)) if measured and h.in_scope
                and h.head_ok else 0 for h in hits}
        hits.sort(key=lambda h: (h.in_scope, h.head_ok, not clash[h.family_id], in_segment[h.family_id],
                                 round(h.query_coverage * len(q_stems)), fits[h.family_id],
                                 h.name_coverage + 0.5 * h.query_coverage, h.bm25), reverse=True)
        return hits

    def expand_matches(self, matches: list[FamilyMatch]) -> list[FamilyHit]:
        hits = []
        for match in matches:
            template, members = self.family(match.family_id)
            if members:
                hits.append(FamilyHit(match.family_id, match.name, template, members, match.bm25,
                                      match.name_coverage, match.query_coverage, match.head_ok))
        return hits

    def search(self, text: str, limit_families: int = 8) -> SearchResult:
        hits, examined, truncated = self.search_matches(text)
        return SearchResult(self.expand_matches(hits[:limit_families]), examined, False,
                            truncated or len(hits) > limit_families)

    # ---- Portal (secondary schema) and real listings ---------------------------------------------------------------

    def portal_chars(self, domain: str, category: str) -> list[PortalChar]:
        key = (domain, category)
        if key not in self._portal:
            rows = self._db().execute(
                "SELECT name, value_type, allowed, unit FROM portal_chars WHERE domain=? AND category=?", key
            )
            self._portal[key] = [PortalChar(category, r[0], r[1], json.loads(r[2] or "[]"), r[3]) for r in rows]
        return self._portal[key]

    @lru_cache(maxsize=1)
    def _property_heads(self):
        """Prepare noun buckets once per immutable snapshot, including secondary Portal names."""
        self.family_names()
        families = [(head, fid) for fid, heads in self._family_noun_heads.items() for head in heads]
        portal = [(head_noun_stem(category) or "", name, allowed) for category, name, allowed in self._db().execute(
            "SELECT DISTINCT category,name,allowed FROM portal_chars")]
        return tuple({prefix: tuple(group) for prefix, group in groupby(sorted(rows, key=lambda row: row[0]),
                                                                     key=lambda row: row[0][:4])}
                     for rows in (families, portal))

    @lru_cache(maxsize=128)
    def product_property_phrases(self, product: str) -> frozenset[str]:
        """Characteristic names and complete dictionary values containing list separators."""
        self.family_names()
        head = head_noun_stem(alias_of(product) or product)
        if not head:
            return frozenset()
        family_heads, portal_heads = self._property_heads()
        families = sorted({fid for other, fid in family_heads.get(head[:4], ()) if stems_match(head, other)})
        rows = []
        if families:
            placeholders = ",".join("?" for _ in families)
            rows.extend(self._db().execute(
                "SELECT DISTINCT c.name,c.allowed FROM characteristics c JOIN positions p ON p.code=c.position_code "
                f"AND p.version=c.position_version WHERE p.status='ACTIVE' AND p.family_id IN ({placeholders})",
                families))
        rows.extend((name, allowed) for other, name, allowed in portal_heads.get(head[:4], ())
                    if stems_match(head, other))
        names = {name for name, _ in rows if name}
        for name, allowed in rows:
            for value in json.loads(allowed or "[]"):
                label = value if isinstance(value, str) else value.get("label")
                if name and isinstance(label, str) and re.search(r",|\s+и\s+", label, re.IGNORECASE):
                    names.add(f"{name} {label}")
        return frozenset(name for name in names if name)

    def domain_property_stems(self, domain: str) -> frozenset[str]:
        """Content stems of every characteristic name the domain uses in KTRU (ACTIVE positions) or the Portal."""
        if domain not in self._domain_stems:
            db = self._db()
            names = [r[0] for r in db.execute(
                "SELECT DISTINCT c.name FROM characteristics c JOIN positions p ON p.code = c.position_code "
                "AND p.version = c.position_version WHERE p.domain = ? AND p.status = 'ACTIVE'", (domain,))]
            names += [r[0] for r in db.execute("SELECT DISTINCT name FROM portal_chars WHERE domain = ?", (domain,))]
            self._domain_stems[domain] = frozenset(x for name in names for x in content_stems(name or ""))
        return self._domain_stems[domain]

    def portal_category(self, domain: str, family_name: str, text: str) -> str | None:
        """Portal category whose name best matches the KTRU family name (then the text). No match → None."""
        if domain not in self._portal_cats:
            rows = self._db().execute("SELECT DISTINCT category FROM portal_chars WHERE domain=?", (domain,))
            self._portal_cats[domain] = [(c, content_stems(c)) for (c,) in rows]
        f_stems, t_stems = content_stems(family_name), content_stems(text)
        best, best_score = None, 0.0
        for cat, c_stems in self._portal_cats[domain]:
            if not c_stems:
                continue
            head = any(stems_match(c_stems[0], f) for f in f_stems[:1])
            fam = sum(1 for c in c_stems if any(stems_match(c, f) for f in f_stems))
            txt = sum(1 for c in c_stems if any(stems_match(c, t) for t in t_stems))
            score = (2 * fam + txt) / len(c_stems) + (1 if head else 0)
            if head and score > best_score:
                best, best_score = cat, score
        return best

    def examples(self, domain: str, n: int) -> list[tuple[str, str, str]]:
        """Random real listings with characteristics: (ste_id, name, text)."""
        rows = (
            self._db()
            .execute(
                "SELECT ste_id, name, chars FROM ste WHERE domain=? AND chars != '' ORDER BY random() LIMIT ?",
                (domain, n),
            )
            .fetchall()
        )
        return [(sid, name, ste_text(name, chars)) for sid, name, chars in rows]


def ste_text(name: str, chars: str) -> str:
    """«Масса 10.20000 кг | Тип стойка» → «Масса 10,2 кг, тип стойка»; drops empty and zero placeholders."""
    parts = []
    for raw in chars.split("|"):
        p = raw.strip()
        if not p or "(не задано)" in p or re.search(r"\s0\.0+\s", p + " "):
            continue
        p = re.sub(r"(\d+)\.(\d*?)0+\b", lambda m: m.group(1) + ("," + m.group(2) if m.group(2) else ""), p)
        parts.append(p[0].lower() + p[1:] if parts else p)
    return f"{name.rstrip('. ')}. {', '.join(parts)}" if parts else name
