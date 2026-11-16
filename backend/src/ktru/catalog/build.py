"""Build an immutable SQLite + FTS5 snapshot from actual KTRU XML files.

Usage: python -m src.ktru.catalog.build <xml_dir> <out_dir> [--portal-dir DIR]
Only `*_actual_*` files are read; `not-actual` files are ignored (CONSTRAINTS.md).
--portal-dir: the organizers' Portal exports (Одежда/Мебель/Инструменты.xlsx). Лист2 gives Portal category schemas
(secondary to KTRU), Лист1 gives real product listings (СТЕ) used as live demo inputs.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from src.ktru.catalog.validation import value_defects, value_type
from src.ktru.engine.text import NOT_A_PRODUCT, content_stems, head_noun_stem

NS = {"o": "http://zakupki.gov.ru/oos/types/1"}
SCHEMA_VERSION = "s0-mvp-3"
MARKER = "[не найдено значение ID"

LO_OPS = {"greater": False, "greaterOrEqual": True}
HI_OPS = {"less": False, "lessOrEqual": True}


def _t(el, path: str) -> str | None:
    found = el.find(path, NS)
    return found.text.strip() if found is not None and found.text else None


def domain_of(okpd2: str | None) -> str:
    if not okpd2:
        return "unknown"
    if okpd2.startswith(("14.", "15.2")):
        return "clothing"
    if okpd2.startswith("31."):
        return "furniture"
    if okpd2.startswith(("25.7", "28.24", "26.51")):
        return "tools"
    return "unknown"


def parse_value(v) -> dict:
    out: dict = {"value_code": _t(v, "o:valueCode"), "defects": []}
    okei = v.find("o:OKEI", NS)
    if okei is not None:
        out["okei_code"] = _t(okei, "o:code")
        out["okei_name"] = _t(okei, "o:name")
    label = _t(v, "o:qualityDescription")
    if label is not None:
        out["kind"] = "enum"
        out["label"] = label
        if MARKER in label:
            out["defect"] = "missing_value_marker"
    concrete = _t(v, "o:valueSet/o:concreteValue")
    rng = v.find("o:rangeSet/o:valueRange", NS)
    if label is None and concrete is not None:
        out["kind"] = "number"
        out["x"] = concrete
    elif label is None and rng is not None:
        out["kind"] = "range"
        out["lo"] = _t(rng, "o:min")
        out["hi"] = _t(rng, "o:max")
        out["lo_inclusive"] = LO_OPS.get(_t(rng, "o:minMathNotation") or "", True)
        out["hi_inclusive"] = HI_OPS.get(_t(rng, "o:maxMathNotation") or "", True)
        for key, ops in (("min", LO_OPS), ("max", HI_OPS)):
            operator = _t(rng, f"o:{key}MathNotation")
            if operator is not None and operator not in ops:
                out["defects"].append("unknown_bound_operator")
    elif label is None:
        out["kind"] = "raw"
    out["defects"] = value_defects(out)
    if out["defects"]:
        out["defect"] = out["defects"][0]
        out["raw_xml"] = ET.tostring(v, encoding="unicode")
    return out


def parse_position(pos, source: str, element_index: int | None = None) -> tuple[dict, list[dict]] | None:
    data = pos.find("o:data", NS)
    if data is None:
        return None
    code = _t(data, "o:code")
    version = _t(data, "o:version")
    if not code or not version:
        return None
    okeis = [{"code": _t(o, "o:code"), "name": _t(o, "o:name")} for o in data.findall("o:OKEIs/o:OKEI", NS)]
    okpd2 = _t(data, "o:OKPD2/o:code")
    row = {
        "code": code,
        "version": int(version),
        "name": _t(data, "o:name") or "",
        "okpd2_code": okpd2,
        "okpd2_name": _t(data, "o:OKPD2/o:name"),
        "status": _t(data, "o:status") or "",
        "is_template": (_t(data, "o:isTemplate") or "false") == "true",
        "parent_code": _t(data, "o:parentPositionInfo/o:code"),
        "parent_version": _t(data, "o:parentPositionInfo/o:version"),
        "domain": domain_of(okpd2),
        "supply_units": json.dumps(okeis, ensure_ascii=False),
        "application_date_start": _t(data, "o:applicationDateStart"),
        "application_date_end": _t(data, "o:applicationDateEnd"),
        "source": source,
        "source_meta": json.dumps({"xml": source, "element_index": element_index}, ensure_ascii=False),
        "rubricators": json.dumps([
            {"code": _t(r, "o:code"), "name": _t(r, "o:name")}
            for r in data.findall("o:rubricators/o:rubricatorInfo", NS)
        ], ensure_ascii=False),
    }
    chars = []
    characteristics = data.findall("o:characteristics/o:characteristic", NS)
    char_codes = Counter(_t(ch, "o:code") for ch in characteristics)
    for index, ch in enumerate(characteristics):
        char_source = {"xml": source, "element_index": element_index, "characteristic_index": index}
        values = [
            {**parse_value(v), "source": {**char_source, "value_index": value_index}}
            for value_index, v in enumerate(ch.findall("o:values/o:value", NS))
        ]
        defects = list(dict.fromkeys(d for v in values for d in v["defects"]))
        if _t(ch, "o:kind") not in ("1", "2", "3"):
            defects.append("unknown_kind")
        char_code = _t(ch, "o:code")
        if not char_code:
            defects.append("missing_char_code")
        elif char_codes[char_code] > 1:
            defects.append("duplicate_char_code")
        chars.append(
            {
                "position_code": code,
                "position_version": int(version),
                "char_code": _t(ch, "o:code"),
                "name": _t(ch, "o:name") or "",
                "type_raw": _t(ch, "o:type"),
                "kind_raw": _t(ch, "o:kind"),
                "required": (_t(ch, "o:isRequired") or "false") == "true",
                "multi": _t(ch, "o:choiceType") == "2",
                "actual": (_t(ch, "o:actual") or "true") == "true",
                "allowed": json.dumps(values, ensure_ascii=False),
                "value_type": value_type(values),
                "defects": json.dumps(defects),
                "source_meta": json.dumps(char_source, ensure_ascii=False),
                "characteristic_index": index,
            }
        )
    return row, chars


DDL = """
CREATE TABLE positions (
  code TEXT, version INTEGER, name TEXT, okpd2_code TEXT, okpd2_name TEXT, status TEXT,
  is_template INTEGER, parent_code TEXT, parent_version TEXT, family_id TEXT, domain TEXT,
  supply_units TEXT, application_date_start TEXT, application_date_end TEXT, source TEXT,
  PRIMARY KEY (code, version));
CREATE TABLE characteristics (
  position_code TEXT, position_version INTEGER, char_code TEXT, name TEXT, type_raw TEXT, kind_raw TEXT,
  required INTEGER, multi INTEGER, actual INTEGER, allowed TEXT);
CREATE INDEX ix_char_pos ON characteristics(position_code, position_version);
CREATE TABLE position_metadata (
  position_code TEXT, position_version INTEGER, rubricators TEXT, source_meta TEXT,
  PRIMARY KEY (position_code, position_version));
CREATE TABLE characteristic_metadata (
  position_code TEXT, position_version INTEGER, char_code TEXT, value_type TEXT, defects TEXT, source_meta TEXT,
  characteristic_index INTEGER,
  PRIMARY KEY (position_code, position_version, characteristic_index));
CREATE VIRTUAL TABLE positions_fts USING fts5(code UNINDEXED, version UNINDEXED, name, okpd2_name, chars,
  tokenize = "unicode61 remove_diacritics 2");
CREATE TABLE manifest (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE portal_chars (
  domain TEXT, category TEXT, kpgz_code TEXT, name TEXT, role TEXT, value_type TEXT, allowed TEXT, unit TEXT);
CREATE INDEX ix_portal_cat ON portal_chars(domain, category);
CREATE TABLE ste (ste_id TEXT, domain TEXT, name TEXT, chars TEXT);
CREATE INDEX ix_ste_domain ON ste(domain);
CREATE TABLE plausibility (head TEXT, prop TEXT, dim TEXT, n INTEGER, q01 REAL, q05 REAL, q50 REAL, q95 REAL,
  q99 REAL);
CREATE INDEX ix_plausibility_head ON plausibility(head);
CREATE TABLE noun_class (head TEXT PRIMARY KEY, domain TEXT, okpd2 TEXT, families TEXT);
"""


def _char_text(allowed_json: str) -> str:
    return " ".join(v.get("label", "") for v in json.loads(allowed_json) if v.get("kind") == "enum")


PORTAL_FILES = {"Одежда": "clothing", "Мебель": "furniture", "Инструменты": "tools"}


def build_portal(db: sqlite3.Connection, portal_dir: Path, quarantine: dict[str, int]) -> list[dict]:
    """Portal category schemas (Лист2) and real listings (Лист1). Rows with ID markers or broken structure are
    quarantined: a marker is never an allowed value (CONSTRAINTS.md)."""
    import openpyxl  # noqa: PLC0415 - only needed when Portal exports are supplied

    sources = []
    for stem, domain in PORTAL_FILES.items():
        path = portal_dir / f"{stem}.xlsx"
        if not path.exists():
            continue
        sources.append({"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        wb = openpyxl.load_workbook(path, read_only=True)
        rows = []
        for r in wb["Лист2"].iter_rows(min_row=2, values_only=True):
            category, kpgz, _kname, name, role, vtype, values, unit = (list(r) + [None] * 8)[:8]
            if not category or not name or MARKER in str(category) or MARKER in str(name) or not vtype:
                quarantine["portal_structure"] = quarantine.get("portal_structure", 0) + 1
                continue
            allowed = [v.strip() for v in str(values or "").split(";") if v.strip()]
            clean = [v for v in allowed if MARKER not in v and "не найдено значение" not in v]
            if len(clean) != len(allowed):
                quarantine["portal_marker_values"] = (
                    quarantine.get("portal_marker_values", 0) + len(allowed) - len(clean)
                )
            rows.append((domain, category, kpgz, name, role, vtype, json.dumps(clean, ensure_ascii=False), unit))
        db.executemany("INSERT INTO portal_chars VALUES (?,?,?,?,?,?,?,?)", rows)
        ste = []
        for r in wb["Лист1"].iter_rows(min_row=2, values_only=True):
            ste_id, name, _model, _vendor, _country, chars = (list(r) + [None] * 6)[:6]
            if ste_id and name:
                ste.append((str(ste_id), domain, str(name).strip(), str(chars or "").strip()))
        db.executemany("INSERT INTO ste VALUES (?,?,?,?)", ste)
        wb.close()
    return sources


GROUP_CLASSES = Path(__file__).with_name("portal_okpd2.json")


def widen_groups(db: sqlite3.Connection) -> dict[str, int]:
    """The Portal groups name more OKPD2 classes than the base prefixes (backpacks, respirators, safes, abrasives).

    `portal_okpd2.json` maps each Portal category to its OKPD2 class (organizers' KPGZ links). Such a class often
    is a catch-all («прочие»), so a position joins a group only when the group speaks of it: its product noun and
    at least 3/4 of its name words occur in the group's Portal categories or in at least three of its real
    listings (a Petri dish under a plastics class or a pile driver under «прочие машины» stays out).
    Subcontracting services are never a product of a group."""
    classes = json.loads(GROUP_CLASSES.read_text())["classes"]
    vocab: dict[str, Counter] = {}
    for domain, category in db.execute("SELECT DISTINCT domain, category FROM portal_chars"):
        vocab.setdefault(domain, Counter()).update({s: 3 for s in set(content_stems(category))})
    for domain, name in db.execute("SELECT domain, name FROM ste"):
        vocab.setdefault(domain, Counter()).update(set(content_stems(name)))
    widened: Counter = Counter()
    rows = db.execute("SELECT code, version, name, okpd2_code FROM positions WHERE domain='unknown'").fetchall()
    for code, version, name, okpd2 in rows:
        match = max((c for c in classes if okpd2 and (okpd2 == c or okpd2.startswith(c + "."))), key=len, default=None)
        if not match:
            continue
        spoken = vocab.get(classes[match], Counter())
        head = head_noun_stem(name or "")
        stems = [s for s in content_stems((name or "").split("(")[0]) if len(s) > 2]
        coverage = sum(spoken[s] >= 3 for s in stems) / max(1, len(stems))
        if head and head not in NOT_A_PRODUCT and spoken[head] >= 3 and coverage >= 0.75:
            db.execute("UPDATE positions SET domain=? WHERE code=? AND version=?", (classes[match], code, version))
            widened[classes[match]] += 1
    services = db.execute("UPDATE positions SET domain='unknown' WHERE domain!='unknown' AND name LIKE 'Услуги%'")
    return {**{f"widened_{k}": v for k, v in widened.items()}, "services_out_of_groups": services.rowcount}


# Listing units → (dimension, factor to the base unit of text.UNITS: mm, g, ml, W, V).
_LISTING_UNITS = {"мм": ("length", 1), "см": ("length", 10), "м": ("length", 1000), "г": ("mass", 1),
                  "кг": ("mass", 1000), "т": ("mass", 1_000_000), "мл": ("volume", 1), "л": ("volume", 1000),
                  "дм3": ("volume", 1000), "вт": ("power", 1), "квт": ("power", 1000), "в": ("voltage", 1)}
_LISTING_PROPS = (("масс", "масса"), ("вес", "масса"), ("длин", "длина"), ("ширин", "ширина"), ("высот", "высота"),
                  ("глубин", "глубина"), ("диаметр", "диаметр"), ("толщин", "толщина"), ("объем", "объем"),
                  ("емкост", "объем"), ("мощност", "мощность"), ("напряжен", "напряжение"))
_LISTING_SEGMENT = re.compile(r"^(?P<name>.+?)\s+(?P<num>\d+(?:[.,]\d+)?)\s*(?P<unit>[^\d|]{0,12})$")
_PACKAGE = re.compile(r"упаков|короб|тар[аы]|транспорт|брутто|паллет", re.IGNORECASE)


def _quantiles(values: list[float]) -> tuple[float, ...]:
    values = sorted(values)
    return tuple(values[min(len(values) - 1, int(q * len(values)))] for q in (0.01, 0.05, 0.5, 0.95, 0.99))


def build_plausibility(db: sqlite3.Connection) -> int:
    """Typical values per product noun from real Portal listings (СТЕ): the base for «unusual value» warnings.

    For each product noun (first noun of the listing name) and property (масса, высота, …, or «*» for any property
    of that dimension) the table keeps the 1/5/50/95/99 % quantiles in base units. A row with prop «#» counts
    the listings of that noun, so an absent dimension («литры» for a hammer) is evidence too. Package sizes, zero
    placeholders and unparsed values are skipped."""
    values: dict[tuple[str, str, str], list[float]] = {}
    seen_dims: dict[tuple[str, str], int] = Counter()
    listings: Counter = Counter()
    for domain, name, chars in db.execute("SELECT domain, name, chars FROM ste"):
        noun = head_noun_stem(name or "")
        if not noun or noun in NOT_A_PRODUCT:
            continue
        # The product noun, and «@group» for a noun with too few listings of its own.
        for head in (noun, f"@{domain}"):
            listings[head] += 1
        dims = set()
        for segment in (chars or "").split(" | "):
            m = _LISTING_SEGMENT.match(segment.strip())
            if not m or _PACKAGE.search(m.group("name")):
                continue
            unit = (m.group("unit") or "").strip().lower().split(";")[0].strip().rstrip(".")
            if unit not in _LISTING_UNITS:
                continue
            dim, factor = _LISTING_UNITS[unit]
            x = float(m.group("num").replace(",", ".")) * factor
            if x <= 0:
                continue
            low = m.group("name").lower().replace("ё", "е")
            prop = next((p for stem_, p in _LISTING_PROPS if stem_ in low), None)
            for head in (noun, f"@{domain}"):
                for key in ((head, prop, dim), (head, "*", dim)) if prop else ((head, "*", dim),):
                    values.setdefault(key, []).append(x)
            dims.add(dim)
        for dim in dims:
            for head in (noun, f"@{domain}"):
                seen_dims[(head, dim)] += 1
    rows = [(head, prop, dim, len(v), *_quantiles(v)) for (head, prop, dim), v in values.items() if len(v) >= 5]
    rows += [(head, "#", dim, n, None, None, None, None, None) for (head, dim), n in seen_dims.items()]
    rows += [(head, "#", "", n, None, None, None, None, None) for head, n in listings.items() if n >= 5]
    db.executemany("INSERT INTO plausibility VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


NOUN_CLASSES = Path(__file__).with_name("noun_okpd2.json")


def build_noun_classes(db: sqlite3.Connection) -> int:
    """Listing nouns → in-group KTRU families of the OKPD2 class the Portal files them under (noun_okpd2.json).

    «Перфоратор» has no KTRU family of its own; its Portal cards are filed under 28.24.1, whose KTRU family is
    «Инструменты ручные электрические…». Only families of exactly that class are linked."""
    nouns = json.loads(NOUN_CLASSES.read_text())["nouns"]
    by_class: dict[str, set[str]] = {}
    for family, okpd2 in db.execute(
            "SELECT family_id, okpd2_code FROM positions WHERE status='ACTIVE' AND is_template=0 AND domain!='unknown'"):
        by_class.setdefault(okpd2, set()).add(family)
    rows = [(head, n["domain"], n["okpd2"], json.dumps(sorted(by_class[n["okpd2"]])))
            for head, n in nouns.items() if by_class.get(n["okpd2"])]
    db.executemany("INSERT INTO noun_class VALUES (?,?,?,?)", rows)
    return len(rows)


def build(xml_dir: Path, out_dir: Path, portal_dir: Path | None = None) -> dict:
    files = sorted(p for p in xml_dir.glob("*.xml") if "_actual_" in p.name and "not-actual" not in p.name)
    if not files:
        raise SystemExit(f"no actual XML files in {xml_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=out_dir, prefix=".staging-"))
    db = sqlite3.connect(tmp / "catalog.sqlite")
    db.executescript(DDL)
    db.create_function("char_text", 1, _char_text)
    counts = {"source_records": 0, "positions_active": 0, "final": 0, "templates": 0, "characteristics": 0}
    quarantine: dict[str, int] = {}
    sources = []
    tag = f"{{{NS['o']}}}position"
    for f in files:
        sources.append({"path": f.name, "sha256": hashlib.sha256(f.read_bytes()).hexdigest()})
        element_index = -1
        for _, el in ET.iterparse(f, events=("end",)):
            if el.tag != tag:
                continue
            element_index += 1
            parsed = parse_position(el, f.name, element_index)
            el.clear()
            if not parsed:
                continue
            row, chars = parsed
            counts["source_records"] += 1
            # Several versions of a code may appear; keep the newest one per code+version pair.
            db.execute(
                "INSERT OR REPLACE INTO positions VALUES (:code,:version,:name,:okpd2_code,:okpd2_name,:status,"
                ":is_template,:parent_code,:parent_version,NULL,:domain,:supply_units,:application_date_start,"
                ":application_date_end,:source)",
                row,
            )
            db.execute(
                "INSERT OR REPLACE INTO position_metadata VALUES (:code,:version,:rubricators,:source_meta)", row
            )
            db.execute(
                "DELETE FROM characteristics WHERE position_code=? AND position_version=?",
                (row["code"], row["version"]),
            )
            db.execute(
                "DELETE FROM characteristic_metadata WHERE position_code=? AND position_version=?",
                (row["code"], row["version"]),
            )
            for c in chars:
                for v in json.loads(c["allowed"]):
                    for defect in v.get("defects", []):
                        quarantine[defect] = quarantine.get(defect, 0) + 1
                if "unknown_kind" in json.loads(c["defects"]):
                    quarantine["unknown_kind"] = quarantine.get("unknown_kind", 0) + 1
            db.executemany(
                "INSERT INTO characteristics VALUES (:position_code,:position_version,:char_code,:name,:type_raw,"
                ":kind_raw,:required,:multi,:actual,:allowed)",
                chars,
            )
            db.executemany(
                "INSERT OR REPLACE INTO characteristic_metadata VALUES (:position_code,:position_version,"
                ":char_code,:value_type,:defects,:source_meta,:characteristic_index)", chars
            )
    # Family: a template groups its children; a non-template without a template parent is a singleton family.
    db.execute(
        "UPDATE positions SET family_id = CASE WHEN is_template=1 THEN code ELSE COALESCE(parent_code, code) END"
    )
    db.execute("CREATE INDEX ix_pos_family ON positions(family_id)")
    db.execute("CREATE INDEX ix_pos_code ON positions(code)")
    db.execute(
        "INSERT INTO positions_fts SELECT p.code, p.version, p.name, COALESCE(p.okpd2_name,''), "
        "COALESCE((SELECT group_concat(c.name || ' ' || char_text(c.allowed), ' ') FROM characteristics c "
        "WHERE c.position_code=p.code AND c.position_version=p.version), '') FROM positions p WHERE p.status='ACTIVE'"
    )
    counts["positions_active"] = db.execute("SELECT count(*) FROM positions WHERE status='ACTIVE'").fetchone()[0]
    counts["templates"] = db.execute(
        "SELECT count(*) FROM positions WHERE status='ACTIVE' AND is_template=1"
    ).fetchone()[0]
    counts["final"] = counts["positions_active"] - counts["templates"]
    counts["characteristics"] = db.execute("SELECT count(*) FROM characteristics").fetchone()[0]
    if portal_dir is not None:
        sources += build_portal(db, portal_dir, quarantine)
        sources.append({"path": GROUP_CLASSES.name, "sha256": hashlib.sha256(GROUP_CLASSES.read_bytes()).hexdigest()})
        counts.update(widen_groups(db))
        counts["plausibility_rows"] = build_plausibility(db)
        if NOUN_CLASSES.exists():
            sources.append({"path": NOUN_CLASSES.name,
                            "sha256": hashlib.sha256(NOUN_CLASSES.read_bytes()).hexdigest()})
            counts["noun_classes"] = build_noun_classes(db)
    counts["portal_chars"] = db.execute("SELECT count(*) FROM portal_chars").fetchone()[0]
    counts["portal_categories"] = db.execute("SELECT count(DISTINCT domain || category) FROM portal_chars").fetchone()[
        0
    ]
    counts["ste"] = db.execute("SELECT count(*) FROM ste").fetchone()[0]
    snapshot_id = hashlib.sha256((SCHEMA_VERSION + "".join(s["sha256"] for s in sources)).encode()).hexdigest()[:16]
    manifest = {
        "snapshot_id": snapshot_id,
        "schema_version": SCHEMA_VERSION,
        "built_at": datetime.now(UTC).isoformat(),
        "snapshot_date": files[0].name.split("_actual_")[1][:8],
        "source_files": sources,
        "counts": counts,
        "quarantine_summary": quarantine,
    }
    db.execute("INSERT INTO manifest VALUES ('manifest', ?)", (json.dumps(manifest, ensure_ascii=False),))
    db.commit()
    db.close()
    (tmp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    final = out_dir / f"snapshot-{snapshot_id}"
    if final.exists():
        raise SystemExit(f"snapshot already published: {final}")
    os.rename(tmp, final)
    current = out_dir / "current"
    if current.is_symlink() or current.exists():
        current.unlink()
    current.symlink_to(final.name)
    return manifest


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build a KTRU snapshot")
    ap.add_argument("xml_dir", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--portal-dir", type=Path, default=None)
    a = ap.parse_args()
    m = build(a.xml_dir, a.out_dir, a.portal_dir)
    print(json.dumps({k: m[k] for k in ("snapshot_id", "counts", "quarantine_summary")}, ensure_ascii=False))
