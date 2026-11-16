"""Accuracy regressions: independent facts, ownership, and family recall."""

import json
import sqlite3
import uuid

import pytest

from src.ktru import contracts as C
from src.ktru.catalog.build import DDL
from src.ktru.catalog.reader import Catalog
from src.ktru.engine import assertions as A
from src.ktru.ml import extract as ml
from src.ktru.ml.classify import FamilyClassifier
from src.ktru.service.pipeline import Pipeline
from src.ktru.service.stages import LexicalRetriever


def categorical(text):
    return [a for a in A.rule_assertions(text, 1) if a.property_raw in ("материал", "цвет")]


def test_independent_materials_keep_each_part():
    facts = categorical("Стул: сиденье фанера, спинка пластик, каркас сталь.")
    assert {(a.subject_label, a.value.s) for a in facts} == {
        ("сиденье", "фанера"), ("спинка", "пластик"), ("каркас", "сталь"),
    }
    assert all(a.status == "supported" for a in facts)


def test_conjoined_colors_keep_separate_owners():
    facts = categorical("Пальто синее и воротник белый и пуговицы черные.")
    assert {(a.subject_kind, a.subject_label, a.value.s) for a in facts} == {
        ("item", None, "синий"), ("part", "воротник", "белый"), ("part", "пуговицы", "черный"),
    }


def test_colon_header_is_the_owner():
    facts = categorical("Стул. Сиденье: фанера. Спинка: пластик.")
    assert {(a.subject_label, a.value.s) for a in facts} == {("сиденье", "фанера"), ("спинка", "пластик")}


@pytest.mark.parametrize("text", [
    "Стул желательно белый, фактический цвет неизвестен.",
    "Стул раньше белый, сейчас цвет неизвестен.",
    "Стул синий или зеленый, выбор не сделан.",
    'Стул с надписью "белый".',
])
def test_non_factual_colors_do_not_become_supported(text):
    facts = categorical(text)
    assert facts
    assert all(a.status != "supported" for a in facts)


def test_postposed_package_owner_is_shared_by_rules_and_model():
    text = "Молоток. Масса 700 г относится к пустой коробке, масса молотка неизвестна."
    facts = A.rule_assertions(text, 1)
    mass = [a for a in facts if isinstance(a.value, C.NumberValue)]
    assert len(mass) == 1 and mass[0].subject_kind == "package"
    kept, rejected = ml.ground([{"property": "масса", "value": "700", "unit": "г", "subject": "package",
                               "quote": "Масса 700 г относится к пустой коробке"}], text)
    assert not rejected and kept[0]["subject_ref"]["kind"] == "package"


def test_model_local_relation_does_not_mix_two_part_materials():
    text = "Стул, сиденье фанера и спинка пластик."
    kept, rejected = ml.ground([{"property": "материал", "value": "фанера", "subject": "part", "part": "сиденье",
                               "quote": "сиденье фанера"}], text)
    assert not rejected and kept[0]["subject_ref"]["label"] == "сиденье"


@pytest.mark.parametrize("text", ["Стул. Серийный номер A123.", "Стул произведен в Беларуси.",
    "Перчатки. Ленточный шов.", "Стул производства Синтез.", "Ножовка по дереву.",
    "Сверло для металла.", "Стул, цвет дерева.", "Дрель, цвет металлический.", "Куртка, материал синий.",
    "Стул для обработки твердого дерева.", "Пила для мягкого металла.", "Сверло для резки дерева."])
def test_unrelated_words_do_not_create_supported_material_or_color(text):
    assert not [a for a in categorical(text) if a.status == "supported"]


def test_shared_material_negation():
    facts = categorical("Стул без шпона и МДФ.")
    assert facts and all(a.polarity == "negative" for a in facts)


def test_two_product_colors_have_distinct_subjects():
    facts = categorical("Стул белый и стол черный.")
    assert len(facts) == 2 and facts[0].subject.id != facts[1].subject.id


def test_mass_before_package_conjunction_belongs_to_item():
    facts = [a for a in A.rule_assertions("Молоток 500 г и коробка 700 г.", 1)
             if isinstance(a.value, C.NumberValue)]
    assert [(a.value.x, a.subject_kind) for a in facts] == [("500", "item"), ("700", "package")]


def test_color_before_absent_part_belongs_to_item():
    facts = categorical("Пальто черное без белого воротника.")
    assert [(a.value.s, a.subject_kind) for a in facts] == [("черный", "item"), ("белый", "part")]


@pytest.mark.parametrize("text", ["Стул без подлокотников белый.", "Стул не складной белый."])
def test_absent_feature_does_not_negate_color(text):
    assert all(a.polarity == "positive" for a in categorical(text))


def test_absent_conjoined_parts_do_not_create_positive_colors():
    facts = categorical("Пальто без белого воротника и черных пуговиц.")
    assert not [a for a in facts if a.status == "supported" and a.polarity == "positive"]


def test_common_material_adjectives_are_preserved():
    facts = categorical("Стул, сиденье фанерное, спинка пластиковая.")
    assert {(a.subject_label, a.value.s) for a in facts} == {("сиденье", "фанера"), ("спинка", "пластик")}


def test_historical_model_color_uses_shared_factuality_check():
    text = "Стул раньше белый, сейчас цвет неизвестен."
    kept, _ = ml.ground([{"property": "цвет", "value": "белый", "subject": "item", "quote": "белый"}], text)
    facts = A.model_assertions(kept, text, 1)
    assert facts and all(a.status == "ambiguous" for a in facts)


def test_compound_material_is_one_value():
    facts = categorical("Ложка. Материал: Сталь нержавеющая.")
    assert len(facts) == 1 and facts[0].value.s == "нержавеющая сталь"


def test_model_cannot_drop_local_stainless_qualifier():
    text = "Ложка. Материал: нержавеющая сталь."
    kept, rejected = ml.ground([{"property": "материал", "value": "сталь", "quote": "сталь"}], text)
    assert not kept and rejected


def test_other_part_stainless_does_not_reject_plain_steel():
    text = "Стул. Каркас: сталь, сиденье: нержавеющая сталь."
    kept, rejected = ml.ground([{"property": "материал", "value": "сталь", "subject": "part",
                               "part": "каркас", "quote": "Каркас: сталь, сиденье: нержавеющая сталь"}], text)
    assert kept and not rejected
    assert kept[0]["status"] == "ambiguous"


def test_gross_mass_keeps_packed_product_owner():
    facts = [a for a in A.rule_assertions("Стул: масса нетто 5 кг, брутто с коробкой 6 кг.", 1)
             if isinstance(a.value, C.NumberValue)]
    assert facts[1].subject_label == A.PACKED


def test_bare_product_does_not_turn_feature_count_into_another_product():
    assert A.product_subjects("Стул, подлокотников 0.") is None


@pytest.mark.parametrize("text", ["Стул не из дерева.", "Стул не из дерева и металла, а из пластика."])
def test_material_preposition_keeps_negation(text):
    facts = categorical(text)
    assert all(a.polarity == "negative" for a in facts if a.value.s in ("дерево", "металл"))


@pytest.mark.parametrize("text", ["Сверло для резки заготовок из металла.", "Сверло для изготовления изделий из дерева."])
def test_workpiece_material_does_not_become_product_material(text):
    assert not [a for a in categorical(text) if a.status == "supported"]


@pytest.mark.parametrize("text", ["Молоток. Стальной головки нет.", "Стул. Белый цвет отсутствует."])
def test_postposed_absence_cannot_make_positive_value(text):
    assert not [a for a in categorical(text) if a.status == "supported" and a.polarity == "positive"]


@pytest.mark.parametrize("text", ["Стул белый и подлокотников нет.", "Стул белый и держателя нет.",
    "Стул белый подлокотников нет.", "Стул белый подлокотники отсутствуют."])
def test_another_object_absence_does_not_negate_item_color(text):
    facts = categorical(text)
    assert not [a for a in facts if a.status == "supported" and a.polarity == "negative"]


def test_headwear_color_belongs_to_the_item():
    facts = categorical("Головной убор черный.")
    assert len(facts) == 1 and facts[0].subject_kind == "item"


def test_materials_on_product_part_and_package_are_separate():
    facts = categorical("Футболка из полиэтилена в хлопковом мешочке.")
    assert [(a.value.s, a.subject_kind) for a in facts] == [("полиэтилен", "item"), ("хлопок", "package")]
    facts = categorical("Стальная голова молотка на деревянной рукояти с резиновым хватом.")
    assert facts[0].subject_label == "головка"


@pytest.mark.parametrize("text", [
    "Стул рядом со столом. Он деревянный. Продается стул.",
    "Стул рядом со столом. Стол деревянный. Продается только стул, его материал неизвестен.",
    "Один артикул стула, варианты исполнения: зеленый / синий. В заказ идет один вариант.",
    "Не надо додумывать металл.",
    "Плащ из материала с названием Облачный хлопок. Это торговое название, не доказательство хлопкового волокна.",
    "Вчера стол был синий, сегодня в акте указан красный. Перекрашивали или ошибка акта — не знаю.",
])
def test_unresolved_references_and_mentions_cannot_narrow_catalog(text):
    assert not [a for a in categorical(text) if a.status == "supported" and a.polarity == "positive"]


@pytest.fixture
def crowded_catalog(tmp_path):
    db = sqlite3.connect(tmp_path / "catalog.sqlite")
    db.executescript(DDL)
    for i in range(451):
        family = "large" if i < 450 else "small"
        code = f"chair-{i}"
        db.execute("INSERT INTO positions (code,version,name,status,is_template,family_id,domain,supply_units) "
                   "VALUES (?,1,'Стул','ACTIVE',0,?,'furniture','[]')", (code, family))
        db.execute("INSERT INTO positions_fts VALUES (?,1,'стул',NULL,'')", (code,))
    db.commit()
    db.close()
    (tmp_path / "manifest.json").write_text(json.dumps({"snapshot_id": "crowded", "counts": {"final": 451}}))
    return Catalog(tmp_path)


def test_large_family_does_not_hide_another_family(crowded_catalog):
    result = crowded_catalog.search("Стул")
    assert {f.family_id for f in result.families} == {"large", "small"}
    assert sum(len(f.members) for f in result.families) == 451


def test_leading_request_word_does_not_hide_product(crowded_catalog):
    state = Pipeline(crowded_catalog).parse(C.ParseRequest(request_id=uuid.uuid4().hex,
        expected_revision=0, text="Требуется стул, цвет белый"))
    assert state.family is not None
    assert state.final is None and state.search_scope.coverage_status == "unconfirmed"


def test_classifier_manifest_reaches_card_without_confirming_scope(crowded_catalog):
    manifest = C.ModelManifest(model="test", prompt_sha256="test")
    model = FamilyClassifier(lambda text, labels: {"family": {"label": "Стул", "confidence": .9}}, manifest)
    p = Pipeline(crowded_catalog, retriever=LexicalRetriever(crowded_catalog, model))
    state = p.parse(C.ParseRequest(request_id="ranked", expected_revision=0, text="Стул"))
    assert state.model_manifests == [manifest]
    assert state.final is None and state.search_scope.coverage_status == "unconfirmed"


def test_classifier_failure_keeps_families_and_reports_diagnostic(crowded_catalog):
    def fail(text, labels):
        raise RuntimeError("local inference failed")
    model = FamilyClassifier(fail, C.ModelManifest(model="test", prompt_sha256="test"))
    r = LexicalRetriever(crowded_catalog, model).retrieve("Стул", crowded_catalog.snapshot_id)
    assert {f.family_id for f in r.families} == {"large", "small"}
    assert any(d.kind == "model_unavailable" for d in r.diagnostics)
