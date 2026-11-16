"""Catalog provenance and defects on small XML and legacy immutable snapshots."""

import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

import pytest

from src.ktru import contracts as C
from src.ktru.catalog.build import NS, build, parse_value
from src.ktru.catalog.reader import Catalog, Char, Position
from src.ktru.catalog.validation import catalog_values, value_defects
from src.ktru.engine.compatibility import alive_after, check
from src.ktru.engine.text import stem


def _value(body):
    return ET.fromstring(f'<value xmlns="{NS["o"]}"><valueCode>v1</valueCode>{body}</value>')


@pytest.mark.parametrize(
    ("body", "defect"),
    [
        ("<rangeSet><valueRange><min>8</min><max>4</max></valueRange></rangeSet>", "reversed_range"),
        ("<rangeSet><valueRange><min>4</min><max>4</max><maxMathNotation>less</maxMathNotation>"
         "</valueRange></rangeSet>", "empty_range"),
        ("<valueSet><concreteValue>NaN</concreteValue></valueSet>", "invalid_number"),
        ("<rangeSet><valueRange><min>4</min><minMathNotation>mystery</minMathNotation>"
         "</valueRange></rangeSet>", "unknown_bound_operator"),
        ("<unrecognized>retain this</unrecognized>", "unrecognized_value"),
        ("<rangeSet><valueRange/></rangeSet>", "missing_range_bounds"),
    ],
)
def test_invalid_values_are_retained_with_defects(body, defect):
    value = parse_value(_value(body))
    assert defect in value["defects"]
    assert value["defect"]
    assert "retain this" in value["raw_xml"] if defect == "unrecognized_value" else value["raw_xml"]


def test_unbounded_range_is_not_zero_or_defective():
    value = parse_value(_value("<rangeSet><valueRange><max>8</max></valueRange></rangeSet>"))
    assert value["lo"] is None
    assert value["hi"] == "8"
    assert not value["defects"]


def _position(version, kind="2"):
    return f"""<position><data><code>child</code><version>{version}</version><name>Молоток</name>
    <status>ACTIVE</status><isTemplate>false</isTemplate><OKPD2><code>25.73</code></OKPD2>
    <applicationDateStart>2026-01-01</applicationDateStart><applicationDateEnd>2027-01-01</applicationDateEnd>
    <parentPositionInfo><code>parent</code><version>12</version></parentPositionInfo>
    <rubricators><rubricatorInfo><code>tools</code><name>Инструменты</name></rubricatorInfo></rubricators>
    <characteristics><characteristic><code>mass</code><name>Масса</name><kind>{kind}</kind><type>2</type>
    <values><value><valueCode>same-value</valueCode><valueSet><concreteValue>500</concreteValue></valueSet>
    <OKEI><code>163</code><name>Грамм</name></OKEI></value></values></characteristic></characteristics>
    </data></position>"""


def test_build_and_reader_preserve_scoped_sources_and_parent_version(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "nsiKTRUNew_all_actual_20261003000000_001.xml"
    source.write_text(f'<root xmlns="{NS["o"]}">{_position(10)}{_position(11, "unknown")}</root>')
    manifest = build(raw, tmp_path / "snapshots")
    catalog = Catalog(tmp_path / "snapshots" / f'snapshot-{manifest["snapshot_id"]}')
    first, second = catalog.position("child", 10), catalog.position("child", 11)
    assert first.source.xml == source.name
    assert first.source.element_index == 0
    assert second.source.element_index == 1
    assert first.ref.snapshot_id == catalog.snapshot_id
    assert first.parent_ref.code == "parent"
    assert first.parent_ref.version == 12
    assert first.application_date_start == "2026-01-01"
    assert first.application_date_end == "2027-01-01"
    assert first.rubricators == [{"code": "tools", "name": "Инструменты"}]
    ch = first.chars[0]
    assert ch.source.characteristic_index == 0
    assert ch.value_type == "number"
    assert ch.ref.position_version == 10
    assert ch.allowed[0]["ref"]["characteristic_ref"]["position_version"] == 10
    assert ch.allowed[0]["source"]["value_index"] == 0
    assert second.chars[0].allowed[0]["ref"] != ch.allowed[0]["ref"]
    assert "unknown_kind" in second.chars[0].defects
    assert manifest["schema_version"] != "s0-mvp-1"


def test_legacy_snapshot_is_read_without_mutation(tmp_path):
    db_path = tmp_path / "catalog.sqlite"
    db = sqlite3.connect(db_path)
    db.executescript("""
    CREATE TABLE positions(code TEXT,version INTEGER,name TEXT,okpd2_code TEXT,okpd2_name TEXT,status TEXT,
    is_template INTEGER,parent_code TEXT,parent_version TEXT,family_id TEXT,domain TEXT,supply_units TEXT,
    application_date_start TEXT,application_date_end TEXT,source TEXT);
    CREATE TABLE characteristics(position_code TEXT,position_version INTEGER,char_code TEXT,name TEXT,
    type_raw TEXT,kind_raw TEXT,required INTEGER,multi INTEGER,actual INTEGER,allowed TEXT);
    INSERT INTO positions VALUES('old',2,'Молоток',NULL,NULL,'ACTIVE',0,'parent','12','parent','tools',
    '[]','2026-01-01',NULL,'old.xml');
    """)
    allowed = [{"kind": "range", "lo": "8", "hi": "4", "value_code": "broken"}]
    db.execute("INSERT INTO characteristics VALUES('old',2,'mass','Масса','2','2',0,0,1,?)",
               (json.dumps(allowed),))
    db.commit()
    db.close()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"snapshot_id": "legacy", "schema_version": "s0-mvp-1"}))
    before = (hashlib.sha256(db_path.read_bytes()).hexdigest(), manifest_path.read_bytes())
    position = Catalog(tmp_path).position("old")
    assert position.source.xml == "old.xml"
    assert position.source.element_index is None
    assert position.parent_ref.version == 12
    assert "reversed_range" in position.chars[0].defects
    assert "reversed_range" in position.chars[0].allowed[0]["defects"]
    assert before == (hashlib.sha256(db_path.read_bytes()).hexdigest(), manifest_path.read_bytes())


def test_existing_dataclass_positional_constructors_stay_compatible():
    characteristic = Char("mass", "Масса", "2", False, False, [])
    position = Position("old", 2, "Молоток", None, None, "ACTIVE", False, "old", "tools", [], [characteristic])
    assert position.chars == [characteristic]
    assert characteristic.value_type == "unknown"
    assert characteristic.defects == []


def _catalog_with_char_codes(tmp_path, char_codes):
    raw = tmp_path / "raw"
    raw.mkdir()
    characteristic_rows = []
    for index, char_code in enumerate(char_codes):
        code_xml = f"<code>{char_code}</code>" if char_code is not None else ""
        characteristic_rows.append(
            f"<characteristic>{code_xml}<name>Свойство {index}</name><kind>2</kind><type>1</type>"
            f"<values><value><valueCode>same-value</valueCode><qualityDescription>Значение {index}"
            "</qualityDescription></value></values></characteristic>"
        )
    source = raw / "nsiKTRUNew_all_actual_20261003000000_001.xml"
    source.write_text(
        f'<root xmlns="{NS["o"]}"><position><data><code>child</code><version>10</version>'
        "<name>Молоток</name><status>ACTIVE</status><isTemplate>false</isTemplate><characteristics>"
        + "".join(characteristic_rows) + "</characteristics></data></position></root>"
    )
    manifest = build(raw, tmp_path / "snapshots")
    return Catalog(tmp_path / "snapshots" / f'snapshot-{manifest["snapshot_id"]}')


@pytest.mark.parametrize(
    ("char_codes", "defect", "affected"),
    [((None, "mass"), "missing_char_code", [0]), (("mass", "mass"), "duplicate_char_code", [0, 1])],
    ids=["missing", "duplicate"],
)
def test_invalid_characteristic_identity_is_reported(tmp_path, char_codes, defect, affected):
    chars = _catalog_with_char_codes(tmp_path, char_codes).position("child", 10).chars
    assert len(chars) == 2
    for index in affected:
        assert defect in chars[index].defects


@pytest.mark.parametrize("char_codes", [(None, "mass"), ("mass", "mass")], ids=["missing", "duplicate"])
def test_invalid_characteristic_rows_keep_own_xml_source(tmp_path, char_codes):
    chars = _catalog_with_char_codes(tmp_path, char_codes).position("child", 10).chars
    assert [ch.source.characteristic_index for ch in chars] == [0, 1]
    for index, ch in enumerate(chars):
        assert ch.source.element_index == 0
        assert ch.allowed[0]["source"]["characteristic_index"] == index
        assert ch.allowed[0]["source"]["value_index"] == 0


@pytest.mark.parametrize(
    ("char_codes", "affected"),
    [((None, "mass"), [0]), (("mass", "mass"), [0, 1])],
    ids=["missing", "duplicate"],
)
def test_invalid_characteristic_identity_does_not_create_refs(tmp_path, char_codes, affected):
    chars = _catalog_with_char_codes(tmp_path, char_codes).position("child", 10).chars
    for index in affected:
        assert chars[index].ref is None
        assert all(value.get("ref") is None for value in chars[index].allowed)
    if char_codes[0] is None:
        assert chars[1].ref.char_code == "mass"
        assert chars[1].allowed[0]["ref"]["characteristic_ref"]["char_code"] == "mass"


@pytest.mark.parametrize("label", ["", " \t\n ", None, 42, [], {}])
def test_invalid_enum_label_is_a_catalog_defect(label):
    assert "invalid_enum_label" in value_defects({"kind": "enum", "label": label})


def _material_assertion():
    return C.Assertion(id="material", property_raw="Материал", value_raw="Металл",
                       value=C.EnumValue(label="Металл"), origin="text", extractor="rules")


def test_xml_empty_enum_retains_raw_source_and_cannot_exclude(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "nsiKTRUNew_all_actual_20261003000000_001.xml"
    position_xml = _position(10).replace("<name>Масса</name>", "<name>Материал</name>")
    position_xml = position_xml.replace("<valueSet><concreteValue>500</concreteValue></valueSet>",
                                        "<qualityDescription> \t </qualityDescription>")
    source.write_text(f'<root xmlns="{NS["o"]}">{position_xml}</root>')
    manifest = build(raw, tmp_path / "snapshots")
    position = Catalog(tmp_path / "snapshots" / f'snapshot-{manifest["snapshot_id"]}').position("child", 10)
    characteristic = position.chars[0]
    value = characteristic.allowed[0]
    assert value["label"] == ""
    assert "invalid_enum_label" in value["defects"]
    assert "qualityDescription" in value["raw_xml"]
    assert value["source"]["xml"] == source.name
    assert value["source"]["characteristic_index"] == 0
    assert characteristic.source.xml == source.name
    assert "invalid_enum_label" in characteristic.defects
    assert not catalog_values(characteristic)
    assert check(position, "материал", [_material_assertion()])[0] == "unknown"
    alive, excluded = alive_after([position], {"материал": [_material_assertion()]})
    assert alive == [position]
    assert not excluded


def test_legacy_empty_enum_is_retained_and_cannot_exclude(tmp_path):
    db_path = tmp_path / "catalog.sqlite"
    db = sqlite3.connect(db_path)
    db.executescript("""
    CREATE TABLE positions(code TEXT,version INTEGER,name TEXT,okpd2_code TEXT,okpd2_name TEXT,status TEXT,
    is_template INTEGER,parent_code TEXT,parent_version TEXT,family_id TEXT,domain TEXT,supply_units TEXT,
    application_date_start TEXT,application_date_end TEXT,source TEXT);
    CREATE TABLE characteristics(position_code TEXT,position_version INTEGER,char_code TEXT,name TEXT,
    type_raw TEXT,kind_raw TEXT,required INTEGER,multi INTEGER,actual INTEGER,allowed TEXT);
    INSERT INTO positions VALUES('old',2,'Молоток',NULL,NULL,'ACTIVE',0,NULL,NULL,'old','tools',
    '[]','2026-01-01',NULL,'old.xml');
    """)
    allowed = [{"kind": "enum", "label": " \t ", "value_code": "empty"}]
    db.execute("INSERT INTO characteristics VALUES('old',2,'material','Материал','1','2',0,0,1,?)",
               (json.dumps(allowed),))
    db.commit()
    db.close()
    (tmp_path / "manifest.json").write_text(json.dumps({"snapshot_id": "legacy", "schema_version": "s0-mvp-1"}))
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    position = Catalog(tmp_path).position("old")
    characteristic = position.chars[0]
    assert characteristic.allowed[0]["label"] == " \t "
    assert "invalid_enum_label" in characteristic.defects
    assert characteristic.source.xml == "old.xml"
    assert check(position, "материал", [_material_assertion()])[0] == "unknown"
    alive, excluded = alive_after([position], {"материал": [_material_assertion()]})
    assert alive == [position]
    assert not excluded
    assert before == hashlib.sha256(db_path.read_bytes()).hexdigest()


def test_runtime_empty_enum_does_not_exclude_without_reader_validation():
    characteristic = Char("material", "Материал", "2", False, False, [{"kind": "enum", "label": " "}])
    position = Position("old", 2, "Молоток", None, None, "ACTIVE", False, "old", "tools", [], [characteristic])
    assert check(position, "материал", [_material_assertion()])[0] == "unknown"
    alive, excluded = alive_after([position], {"материал": [_material_assertion()]})
    assert alive == [position]
    assert not excluded


def _word_catalog(frequencies):
    """Supply controlled catalog word evidence without a published snapshot."""
    catalog = Catalog.__new__(Catalog)
    catalog.vocab = set(frequencies)
    catalog._freq = Counter(frequencies)
    catalog._stem_freq = Counter()
    catalog._vocab_by_first = defaultdict(list)
    for word, count in frequencies.items():
        catalog._stem_freq[stem(word)] += count
        catalog._vocab_by_first[word[0]].append(word)
    return catalog


@pytest.mark.parametrize("word", ["молотка", "Молотка"])
def test_rare_known_catalog_word_is_not_changed_to_different_word(word):
    catalog = _word_catalog({"молотка": 2, "молока": 70})
    catalog._stem_freq[stem("молотка")] = 3
    assert catalog.correct_word(word) is None


@pytest.mark.parametrize(
    ("word", "target"), [("стелажа", "стеллажа"), ("стеллажа", "стелажа")],
)
def test_rare_known_catalog_word_can_repair_repeated_letter(word, target):
    catalog = _word_catalog({word: 2, target: 70})
    assert catalog.correct_word(word) == target


def test_unknown_catalog_word_keeps_typo_correction():
    catalog = _word_catalog({"молоток": 70})
    assert catalog._stem_freq[stem("молаток")] == 0
    assert catalog.correct_word("молаток") == "молоток"


def test_rare_known_catalog_stem_protects_another_word_form():
    catalog = _word_catalog({"молотка": 2, "молоки": 70})
    assert "молотки" not in catalog.vocab
    assert catalog._stem_freq[stem("молотки")] == 2
    assert catalog.correct_word("молотки") is None
