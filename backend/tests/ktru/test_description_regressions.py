"""Single-product descriptions keep their properties through splitting and model analysis."""

import json
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.ktru.app import app
from src.ktru import contracts as C
from src.ktru.catalog.build import DDL
from src.ktru.catalog.reader import Catalog
from src.ktru.engine import assertions as A
from src.ktru.engine.products import split_descriptions
from src.ktru.service.pipeline import Pipeline
from tests.ktru.test_s0_stages import stage_catalog  # noqa: F401

WENGER = (
    "Рюкзак WENGER универсальный, светло-серый, светоотражающие элементы, 22 л, 33х15х45 см. "
    "Боковые карманы Да, вентилируемая спинка Да, вентилируемые лямки Да, внутренний органайзер Да, "
    "высота 450 мм, кол-во отделений 2 шт, количество карманов 4 шт, масса 660 г, материал полиэстер, "
    "наличие ручки для переноса Да, объем 22 Л; ДМ3, отверстие для наушников Нет, "
    "перегородка в основном отделении Нет, пол унисекс, регулируемые лямки Да, "
    "ремни для утягивания основного отделения Нет, узор Нет, фронтальный карман Да, цвет светло-серый"
)
BACKPACK_PROPERTIES = [
    "Светоотражающие элементы", "Перегородка в основном отделении", "Пол",
    "Ремни для утягивания основного отделения", "Ремни",
]


@pytest.fixture
def description_catalog(tmp_path):
    with sqlite3.connect(tmp_path / "catalog.sqlite") as db:
        db.executescript(DDL)
        for index, name in enumerate([
            "Рюкзак туристический", "Элементы химические", "Перегородка имплантируемая",
            "Интерактивный пол", "Ремни безопасности", "Стул офисный",
            "Крестовина", "Дрель ударная", "Кейс для инструмента", "Диван прямой", "Секция диванная",
        ]):
            db.execute(
                "INSERT INTO positions (code,version,name,status,is_template,family_id,domain,supply_units) "
                "VALUES (?,1,?,'ACTIVE',0,?,'unknown','[]')", (f"product-{index}", name, f"family-{index}"),
            )
        for name in BACKPACK_PROPERTIES:
            db.execute("INSERT INTO portal_chars VALUES ('unknown','Рюкзаки туристические',NULL,?,NULL,"
                       "'text','[]',NULL)", (name,))
        for category, name in [("Стулья офисные", "Крестовина (пятилучие)"),
                               ("Дрели ударные", "Кейс в комплекте")]:
            db.execute("INSERT INTO portal_chars VALUES ('unknown',?,NULL,?,NULL,'text','[]',NULL)",
                       (category, name))
        for name, allowed in [("Виды диванов, секций диванных", ["Диваны"]),
                              ("Виды мебели общего назначения", ["Диваны, секции диванные"])]:
            db.execute("INSERT INTO portal_chars VALUES ('unknown','Диваны',NULL,?,NULL,'text',?,NULL)",
                       (name, json.dumps(allowed, ensure_ascii=False)))
    (tmp_path / "manifest.json").write_text(json.dumps({"snapshot_id": "description-tests", "counts": {"final": 6}}))
    return Catalog(tmp_path)


@pytest.fixture
def sofa_catalog(description_catalog):
    schema = {
        "Количество посадочных мест": [{"kind": "range", "lo": "1", "hi": "10", "okei_code": "796"}],
        "Вид материала обивки": [{"kind": "enum", "label": "Ткань мебельная"}],
        "Наличие механизма раскладывания": [{"kind": "enum", "label": value} for value in ("Да", "Нет")],
    }
    with sqlite3.connect(description_catalog.dir / "catalog.sqlite") as db:
        for name, allowed in schema.items():
            db.execute("INSERT INTO characteristics (position_code,position_version,char_code,name,kind_raw,required,actual,allowed) "
                       "VALUES ('product-9',1,?,?, '1',1,1,?)", (name, name, json.dumps(allowed)))
        db.execute("INSERT INTO positions_fts VALUES ('product-9',1,'диван прямой',NULL,NULL)")
    return description_catalog


@pytest.mark.parametrize("suffix", ["", " с индивидуальной гравировкой"])
def test_bound_catalog_names_cover_additional_fragments_without_losing_unknown_words(sofa_catalog, suffix):
    text = ("Диван прямой, количество посадочных мест 3 шт, "
            "вид материала обивки Ткань мебельная" + suffix)
    pipeline = Pipeline(sofa_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "Материал обивки", "value": "Ткань мебельная",
             "quote": "вид материала обивки Ткань мебельная"},
        ])
    assert state.family is not None
    assert any(field.label_ru == "Количество посадочных мест" and field.value for field in state.fields)
    assert any(field.label_ru == "Вид материала обивки" and field.value for field in state.fields)
    extras = [field.display_ru for field in state.fields if field.label_ru.startswith("Дополнительно")]
    assert "количество посадочных мест 3 шт" not in extras
    if suffix:
        assert any("индивидуальной гравировкой" in value for value in extras)
    else:
        assert "вид материала обивки Ткань мебельная" not in extras
    assert any(a.value_raw == "количество посадочных мест 3 шт" for a in state.assertions)


@pytest.mark.parametrize("answer, expected", [("Нет", False), ("Да", True), (": Нет", False), ("— Да", True),
                                            (":\u00a0Нет", False), (":\u202fНет", False)])
def test_catalog_boolean_uses_explicit_postposed_answer(sofa_catalog, answer, expected):
    text = f"Диван прямой, наличие механизма раскладывания {answer}"
    state = Pipeline(sofa_catalog).parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))
    field = next(field for field in state.fields if field.label_ru == "Наличие механизма раскладывания")
    assert field.value == C.BoolValue(v=expected)
    assert any(isinstance(evidence, C.TextEvidence) and answer in evidence.span.text for evidence in field.evidence)


@pytest.mark.parametrize("answer", ["Нет данных", "Нет информации", "Нет сведений"])
@pytest.mark.parametrize("with_model", [False, True])
def test_unknown_presence_is_not_a_boolean_fact(sofa_catalog, answer, with_model):
    quote = "наличие механизма раскладывания " + answer
    text = "Диван прямой, " + quote
    pipeline = Pipeline(sofa_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))
    if with_model:
        state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
            expected_revision=state.revision), extractor=lambda _: [
                {"property": "Наличие механизма раскладывания", "value": "Нет", "quote": quote},
            ])
    field = next(field for field in state.fields if field.label_ru == "Наличие механизма раскладывания")
    assert field.value is None and field.status == "needs_answer"
    assert not any(isinstance(field.value, C.BoolValue) for field in state.fields)
    assert any(answer in field.display_ru for field in state.fields)


@pytest.mark.parametrize("value, expected", [("Да", True), ("true", True), ("Нет", False), ("false", False)])
@pytest.mark.parametrize("separator", [", ", "; ", "\n", " и "])
@pytest.mark.parametrize("order", ["last", "first", "after_color", "after_package"])
def test_unknown_presence_in_wide_model_quote_does_not_change_another_property(sofa_catalog, value, expected,
                                                                            separator, order):
    known = "наличие подлокотников " + ("Да" if expected else "Нет")
    unknown = "наличие механизма раскладывания Нет данных"
    quote = separator.join([unknown, known] if order == "first" else [known, unknown])
    if order == "after_color":
        quote = "цвет синий" + separator + quote
    elif order == "after_package":
        quote = "масса коробки 10 г" + separator + quote
    pipeline = Pipeline(sofa_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0,
                                          text="Диван прямой, " + quote))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "Наличие подлокотников", "value": value, "quote": quote},
        ])
    assert not state.rejected_outputs
    assert any(assertion.extractor in ("llm", "both") and assertion.value == C.BoolValue(v=expected)
               and assertion.subject.label == "подлокотников" for assertion in state.assertions)
    mechanism = next(field for field in state.fields if field.label_ru == "Наличие механизма раскладывания")
    assert mechanism.value is None and mechanism.status == "needs_answer"


@pytest.mark.parametrize("answer", ["Нет данных", "Нет информации", "Нет сведений"])
def test_narrow_model_answer_keeps_the_unknown_source_relation(sofa_catalog, answer):
    pipeline = Pipeline(sofa_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0,
        text="Диван прямой, наличие механизма раскладывания: " + answer))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "Наличие механизма раскладывания", "value": "Нет", "quote": "Нет"},
        ])
    mechanism = next(field for field in state.fields if field.label_ru == "Наличие механизма раскладывания")
    assert mechanism.value is None and mechanism.status == "needs_answer"
    assert not any(isinstance(field.value, C.BoolValue) for field in state.fields)
    assert any(answer in field.display_ru for field in state.fields)


@pytest.mark.parametrize("value, expected", [("Да", True), ("true", True), ("Нет", False), ("false", False)])
@pytest.mark.parametrize("separator", [", ", "; ", " и "])
def test_model_presence_keeps_a_colon_answer_and_an_adjacent_unknown(sofa_catalog, value, expected, separator):
    quote = ("наличие подлокотников: " + ("Да" if expected else "Нет") + separator
             + "наличие механизма раскладывания: Нет данных")
    pipeline = Pipeline(sofa_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0,
                                          text="Диван прямой, " + quote))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "Наличие подлокотников", "value": value, "quote": quote},
        ])
    assert not state.rejected_outputs
    assert any(assertion.extractor in ("llm", "both") and assertion.value == C.BoolValue(v=expected)
               and assertion.subject.label == "подлокотников" for assertion in state.assertions)
    mechanism = next(field for field in state.fields if field.label_ru == "Наличие механизма раскладывания")
    assert mechanism.value is None and mechanism.status == "needs_answer"


@pytest.mark.parametrize("quote, text", [
    ("подлокотников Да", "Диван прямой, наличие подлокотников Да"),
    ("с подлокотниками", "Стул с подлокотниками"),
])
@pytest.mark.parametrize("value", ["Да", "true"])
def test_nested_model_quote_proves_the_same_presence_once(sofa_catalog, quote, text, value):
    pipeline = Pipeline(sofa_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "Наличие подлокотников", "value": value, "quote": quote},
        ])
    assert not state.rejected_outputs
    assert any(assertion.extractor in ("llm", "both") and assertion.value == C.BoolValue(v=True)
               and assertion.subject.kind == "part" for assertion in state.assertions)


@pytest.mark.parametrize("answer", ["Нет данных", "Нет информации", "Нет сведений"])
def test_partial_model_presence_quote_keeps_its_unknown_tail(sofa_catalog, answer):
    pipeline = Pipeline(sofa_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0,
        text="Диван прямой, наличие подлокотников " + answer))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "Наличие подлокотников", "value": "Нет", "quote": "подлокотников Нет"},
        ])
    assert not any(assertion.extractor in ("llm", "both") and isinstance(assertion.value, C.BoolValue)
                   and assertion.status == "supported" for assertion in state.assertions)
    assert any(answer in field.display_ru for field in state.fields)


@pytest.mark.parametrize("quote, expected", [
    ("аккумулятор в комплекте", True), ("аккумулятор входит в комплект", True),
    ("аккумулятор включён в комплект", True), ("аккумулятор входит в набор", True),
    ("аккумулятор входит в состав комплекта", True), ("аккумулятор включён в состав набора", True),
    ("аккумулятор не входит в набор", False), ("аккумулятор не входит в состав комплекта", False),
])
@pytest.mark.parametrize("fact", [
    {"property": "наличие", "value": "аккумулятор"},
    {"property": "наличие", "value": "есть", "part": "аккумулятор"},
    {"property": "наличие аккумулятора", "value": "true"},
])
def test_model_presence_keeps_the_included_object_with_its_predicate(sofa_catalog, quote, expected, fact):
    pipeline = Pipeline(sofa_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0,
                                          text="Дрель, " + quote))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [{**fact, "quote": quote}])
    assert not state.rejected_outputs
    assert any(assertion.extractor in ("llm", "both") and assertion.value == C.BoolValue(v=expected)
               and assertion.subject.kind == "part" and assertion.subject.label == "аккумулятор"
               for assertion in state.assertions)


def test_rules_keep_the_included_object_as_owner():
    assertions = A.rule_assertions("Дрель, аккумулятор в комплекте", 1)
    presence = [assertion for assertion in assertions if isinstance(assertion.value, C.BoolValue)
                and "аккумулятор" in assertion.value_raw]
    assert presence
    assert all(assertion.subject.kind == "part" and assertion.subject.label == "аккумулятор"
               for assertion in presence)


@pytest.mark.parametrize("description, property_raw", [("пластиковый", "материал"), ("синий", "цвет")])
def test_inclusion_predicate_does_not_move_other_facts_to_the_set(description, property_raw):
    assertions = A.rule_assertions(f"Дрель, {description} аккумулятор в комплекте", 1)
    facts = [assertion for assertion in assertions if assertion.property_raw == property_raw]
    assert facts
    assert all(assertion.subject.kind == "part" and assertion.subject.label == "аккумулятор" for assertion in facts)


@pytest.mark.parametrize("prefix, owner", [("Упаковка", "package"), ("Набор инструментов", "set")])
def test_model_inclusion_keeps_the_explicit_outer_owner(sofa_catalog, prefix, owner):
    quote = prefix + ": аккумулятор в комплекте"
    pipeline = Pipeline(sofa_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0,
                                          text="Дрель, " + quote))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "наличие", "value": "аккумулятор", "quote": quote},
        ])
    assert not state.rejected_outputs
    assert any(assertion.extractor in ("llm", "both") and assertion.value == C.BoolValue(v=True)
               and assertion.subject.kind == owner for assertion in state.assertions)


@pytest.mark.parametrize("quote, owner, label", [
    ("аккумулятор прилагается в коробке", "package", "коробка"),
    ("аккумулятор включён в упаковку", "package", "дрель в упаковке"),
    ("аккумулятор входит в корпус", "unknown", None),
])
def test_model_presence_keeps_a_postposed_container(sofa_catalog, quote, owner, label):
    pipeline = Pipeline(sofa_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0,
                                          text="Дрель, " + quote))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "наличие", "value": "аккумулятор", "quote": quote},
        ])
    assert not state.rejected_outputs
    assert any(assertion.extractor in ("llm", "both") and assertion.value == C.BoolValue(v=True)
               and assertion.subject.kind == owner and assertion.subject.label == label
               and assertion.status == ("ambiguous" if owner == "unknown" else "supported")
               and assertion.span.text == quote
               for assertion in state.assertions)


@pytest.mark.parametrize("answer", ["Нет данных", "Нет информации", "Нет сведений"])
@pytest.mark.parametrize("relation, quote", [
    ("наличие аккумулятора в коробке", "аккумулятора"),
    ("аккумулятор прилагается в коробке", "аккумулятор"),
])
def test_narrow_model_presence_keeps_the_unknown_container_answer(sofa_catalog, answer, relation, quote):
    pipeline = Pipeline(sofa_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0,
                                          text=f"Дрель, {relation} {answer}"))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "наличие", "value": "аккумулятор", "quote": quote},
        ])
    assert not state.rejected_outputs
    assertion = next(a for a in state.assertions if a.extractor in ("llm", "both") and a.span.text == quote)
    assert assertion.value == C.UnparsedValue(s=answer, reason="Значение не указано")
    assert assertion.status == "ambiguous" and assertion.polarity == "uncertain"
    assert any(isinstance(evidence, C.TextEvidence) and evidence.span.text == answer for evidence in assertion.evidence)


def test_model_presence_rejects_two_distinct_local_proofs(sofa_catalog):
    quote = "наличие подлокотников Да; наличие подлокотников Нет"
    pipeline = Pipeline(sofa_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0,
                                          text="Диван прямой, " + quote))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "Наличие подлокотников", "value": "true", "quote": quote},
        ])
    assert len(state.rejected_outputs) == 1
    assert state.rejected_outputs[0].reason_ru == "Нет единственной локальной связи для наличия свойства"
    assert not any(assertion.extractor in ("llm", "both") for assertion in state.assertions)


@pytest.mark.parametrize("separator", [", ", "; ", "\n", "\r\n", "\v", "\f", "\u0085", "\u2028", "\u2029"])
def test_boolean_does_not_take_answer_from_next_characteristic(sofa_catalog, separator):
    text = "Диван прямой, наличие механизма раскладывания" + separator + "Нет подлокотников"
    state = Pipeline(sofa_catalog).parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))
    field = next(field for field in state.fields if field.label_ru == "Наличие механизма раскладывания")
    assert field.value == C.BoolValue(v=True)


@pytest.mark.parametrize("suffix, expected_count", [
    ("", 1), ("; Стул офисный, 2 шт", 2), ("; Ремни безопасности", 2),
    ("\n2. Перегородка имплантируемая", 2),
])
def test_http_split_keeps_backpack_properties_and_real_other_products(description_catalog, monkeypatch, suffix,
                                                                    expected_count):
    monkeypatch.setenv("KTRU_SNAPSHOT_DIR", str(description_catalog.dir))
    with TestClient(app) as client:
        text = WENGER + suffix
        response = client.post("/api/ktru/split", json={"text": text})
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == expected_count
    if not suffix:
        assert rows == [WENGER]
    else:
        assert "цвет светло-серый" in rows[0] and "светоотражающие элементы" in rows[0]
        assert rows[-1].startswith(suffix.strip(";\n").removeprefix("2.").strip().split(",")[0])


@pytest.mark.parametrize("product", ["Молоток", "Рюкзак WENGER универсальный"])
def test_analyze_does_not_repeat_additional_fragments_as_a_model_group(stage_catalog, product):  # noqa: F811
    text = f"{product}, серый, индивидуальная гравировка логотипа"
    pipeline = Pipeline(stage_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))
    group = f"{product}, серый"
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "Дополнительно", "value": group, "quote": group},
        ])
    extras = [field for field in state.fields if field.label_ru == "Дополнительно"]
    assert [field.display_ru for field in extras] == [product, "индивидуальная гравировка логотипа"]
    assert all(field.assertion_ids and field.subject and field.evidence for field in extras)
    assert all(text[item.span.start:item.span.end] == item.span.text
               for field in extras for item in field.evidence if isinstance(item, C.TextEvidence))


def test_recognized_backpack_facts_are_not_repeated_in_additional(stage_catalog):  # noqa: F811
    state = Pipeline(stage_catalog).parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=WENGER))
    extras = [field.display_ru for field in state.fields if field.label_ru == "Дополнительно"]
    assert extras.count("светло-серый") == 1
    assert not {"цвет светло-серый", "масса 660 г", "материал полиэстер", "22 л",
                "высота 450 мм", "объем 22 Л"} & set(extras)
    assert "светоотражающие элементы" in extras
    assert "перегородка в основном отделении Нет" in extras
    assert any(assertion.value_raw == "цвет светло-серый" for assertion in state.assertions)


@pytest.mark.parametrize("text, unknown", [
    ("Молоток, материал сталь, цвет сталь", "цвет сталь"),
    ("Рюкзак, цвет серый, материал серый", "материал серый"),
    ("Рюкзак, светло-серый, цвет светло-серый, материал светло-серый", "материал светло-серый"),
])
def test_additional_keeps_distinct_unknown_property(text, unknown):
    visible = A.visible_assertions(A.rule_assertions(text, 1))
    assert any(assertion.property_raw == "Дополнительно" and assertion.value_raw == unknown for assertion in visible)


@pytest.mark.parametrize("change", [
    {"status": "ambiguous", "polarity": "uncertain"},
    {"status": "ambiguous"},
    {"operator": "neq"},
    {"subject": C.SubjectRef(id="other", kind="item", label="Стул")},
])
def test_additional_keeps_different_reading_or_owner(change):
    original = A.rule_assertions("индивидуальная гравировка логотипа", 1)[0]
    proposal = original.model_copy(update={"id": "other-reading", "extractor": "llm", **change})
    assert {assertion.id for assertion in A.visible_assertions([original, proposal])} == {original.id, proposal.id}


@pytest.mark.parametrize("text", [
    "Стул офисный, черный, спинка сетчатая, сиденье мягкое, подлокотники Да, колеса Да, "
    "механизм качания Да, крестовина пластиковая, высота 900 мм, нагрузка 120 кг. "
    "Стул оснащен поясничной поддержкой.",
    "Дрель ударная, мощность 750 Вт, патрон быстрозажимной 13 мм, аккумулятор Нет, "
    "подсветка Да, дополнительная рукоятка Да, кейс Да, масса 2 кг.",
])
def test_shortened_catalog_property_keeps_one_product(description_catalog, text):
    assert split_descriptions(text, description_catalog.family_names().values(),
                              description_catalog.product_property_phrases) == [text]


def test_explicit_list_keeps_delivery_notes_with_each_product():
    note = "К поставке прилагается инструкция по эксплуатации и перечень поставляемых принадлежностей. "
    items = [f"{name}, {note * 15}маркер-{index}"
             for index, name in enumerate(["Молоток", "Стул", "Замок"], 1)]
    text = "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1))
    assert C.MAX_TEXT < len(text) < C.MAX_SPLIT_TEXT
    names = ["Молоток", "Стул", "Замок", "Инструкция", "Перечень"]
    rows = split_descriptions(text, names)
    assert len(rows) == len(items)
    assert all(f"маркер-{index}" in row and "перечень поставляемых принадлежностей" in row
               for index, row in enumerate(rows, 1))


def test_numbered_products_keep_bullet_characteristics():
    text = "1. Дрель ударная\n- Кейс Да\n- Аккумулятор Нет\n2. Стул офисный"
    names = ["Дрель ударная", "Кейс для инструмента", "Аккумулятор", "Стул офисный"]
    rows = split_descriptions(text, names, lambda _: ["Кейс в комплекте", "Аккумулятор"])
    assert rows == ["Дрель ударная\n- Кейс Да\n- Аккумулятор Нет", "Стул офисный"]


@pytest.mark.parametrize("suffix, count", [("", 1), ("; Секция диванная, высота 900 мм", 2),
                                         (", Секция диванная, высота 900 мм", 2)])
def test_commas_inside_catalog_properties_and_values_are_not_products(description_catalog, suffix, count):
    text = ("Диван прямой, виды диванов, секций диванных Диваны, "
            "виды мебели общего назначения Диваны, секции диванные, высота 850 мм")
    rows = split_descriptions(text + suffix, description_catalog.family_names().values(),
                              description_catalog.product_property_phrases)
    assert len(rows) == count
    assert "виды диванов, секций диванных Диваны" in rows[0]
    assert "виды мебели общего назначения Диваны, секции диванные" in rows[0]
    if suffix:
        assert rows[-1].startswith("Секция диванная")
    else:
        assert rows == [text]


def test_compound_dictionary_value_from_ktru_keeps_its_product(description_catalog):
    with sqlite3.connect(description_catalog.dir / "catalog.sqlite") as db:
        db.execute("INSERT INTO characteristics (position_code,position_version,char_code,name,allowed) "
                   "VALUES ('product-9',1,'construction','Конструкция',?)",
                   (json.dumps([{"kind": "enum", "label": "Диваны, секции диванные"}], ensure_ascii=False),))
    text = "Диван прямой, конструкция Диваны, секции диванные; Секция диванная, высота 900 мм"
    rows = split_descriptions(text, description_catalog.family_names().values(),
                              description_catalog.product_property_phrases)
    assert len(rows) == 2
    assert "конструкция Диваны, секции диванные" in rows[0]
    assert rows[1].startswith("Секция диванная")


def test_property_index_accepts_nullable_duplicate_portal_values(description_catalog):
    with sqlite3.connect(description_catalog.dir / "catalog.sqlite") as db:
        db.execute("INSERT INTO portal_chars VALUES ('unknown','Диваны',NULL,"
                   "'Виды мебели общего назначения',NULL,'text',NULL,NULL)")
    phrases = description_catalog.product_property_phrases("Диван")
    assert "Виды мебели общего назначения" in phrases
    assert "Виды мебели общего назначения Диваны, секции диванные" in phrases


def test_supported_presence_is_not_repeated_as_an_unknown_field(stage_catalog):  # noqa: F811
    pipeline = Pipeline(stage_catalog)
    text = "Рюкзак, карманы Да"
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "Наличие карманов", "value": "Да", "quote": "карманы Да"},
        ])
    assert "карманы Да" not in [f.display_ru for f in state.fields if f.label_ru == "Дополнительно"]
    assert any(a.value_raw == "карманы Да" for a in state.assertions)


def test_wide_numeric_evidence_does_not_hide_unknown_qualifiers():
    text = "Молоток, масса 500 г с индивидуальной гравировкой"
    assertions = A.rule_assertions(text, 1)
    number = next(a for a in assertions if isinstance(a.value, C.NumberValue))
    fragment = next(a for a in assertions if a.value_raw.startswith("масса 500"))
    number = number.model_copy(update={"evidence": [*number.evidence,
        C.TextEvidence(span=fragment.span, extractor="llm")]})
    visible = A.visible_assertions([number, fragment])
    assert fragment.id in {a.id for a in visible}


def test_wide_presence_evidence_does_not_hide_unknown_qualifiers():
    text = "Рюкзак, Боковые карманы Да с индивидуальной гравировкой"
    fragment = next(a for a in A.rule_assertions(text, 1) if a.value_raw.startswith("Боковые"))
    span = fragment.span.model_copy(update={"end": fragment.span.start + len("Боковые карманы Да"),
                                          "text": "Боковые карманы Да"})
    presence = fragment.model_copy(update={"id": "presence", "property_raw": "наличие", "value_raw": "Да",
        "value": C.BoolValue(v=True), "span": span, "extractor": "llm",
        "evidence": [C.TextEvidence(span=fragment.span, extractor="llm")]})
    assert fragment.id in {a.id for a in A.visible_assertions([presence, fragment])}


@pytest.mark.parametrize("quote", ["Боковые карманы Да", "Боковые карманы Да с индивидуальной гравировкой"])
def test_model_presence_quote_preserves_unknown_qualifier(stage_catalog, quote):  # noqa: F811
    text = "Рюкзак, Боковые карманы Да с индивидуальной гравировкой"
    pipeline = Pipeline(stage_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "Наличие боковых карманов", "value": "Да", "quote": quote},
        ])
    assert not state.rejected_outputs
    assert any("индивидуальной гравировкой" in f.display_ru for f in state.fields if f.label_ru == "Дополнительно")


def test_model_presence_keeps_an_unknown_adjective(stage_catalog):  # noqa: F811
    text = "Рюкзак, Водонепроницаемые карманы Да"
    pipeline = Pipeline(stage_catalog)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))
    state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: [
            {"property": "Наличие карманов", "value": "Да", "quote": "Водонепроницаемые карманы Да"},
        ])
    assert not state.rejected_outputs
    assert any("Водонепроницаемые" in f.display_ru for f in state.fields if f.label_ru == "Дополнительно")
