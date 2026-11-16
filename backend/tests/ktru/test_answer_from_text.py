"""A question the description already answers by implication is answered by the model, with a verbatim quote."""

import os
import uuid
from pathlib import Path

import pytest

from src.ktru import contracts as C
from src.ktru.catalog.reader import Catalog
from src.ktru.service.pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[3]
SNAP = Path(os.environ.get("KTRU_SNAPSHOT_DIR", ROOT / ".ktru-build" / "snapshots" / "current"))
pytestmark = pytest.mark.skipif(not (SNAP / "manifest.json").exists(), reason="snapshot not built")


class NoFacts:
    def extract(self, text, revision):
        return C.ExtractionResult(status="analyzed")


@pytest.fixture(scope="module")
def catalog():
    return Catalog(SNAP)


def run(catalog, text, chooser):
    pipeline = Pipeline(catalog, extractor=NoFacts(), chooser=chooser)
    state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))
    return state, pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
                                                    expected_revision=state.revision))


def test_an_implied_value_answers_the_question_and_shows_its_quote(catalog):
    text = "Стол письменный на металлокаркасе 1200x600 мм"
    asked = []

    def chooser(description, characteristic, labels):
        asked.append((characteristic, labels))
        quote = "на металлокаркасе"
        return ("Металлический", description.index(quote), description.index(quote) + len(quote)) \
            if characteristic == "Тип каркаса" else None

    before, after = run(catalog, text, chooser)
    assert before.clarification and before.clarification.char_name == "Тип каркаса"
    assert asked[0] == ("Тип каркаса", ["Деревянный", "Металлический"])
    inferred = [a for a in after.assertions if a.property_raw == "Тип каркаса"]
    assert inferred and inferred[0].span.text == "на металлокаркасе" and "вывела" in inferred[0].note_ru
    assert inferred[0].origin == "text" and inferred[0].extractor == "llm"  # a reading of the text, not the user's
    assert sum(c.status == "alive" for c in after.candidates) < sum(c.status == "alive" for c in before.candidates)
    assert any(stage.label_ru.startswith("Из описания: Тип каркаса") for stage in after.funnel)


def test_a_required_value_implied_by_the_text_is_filled_while_the_open_question_stays(catalog):
    text = "Халат детский хлопковый, размер 122"
    asked = []

    def chooser(description, characteristic, labels):
        asked.append(characteristic)
        if characteristic != "Рост" or "122 см" not in labels:
            return None  # the text says nothing about the child's sex
        return "122 см", description.index("размер 122"), description.index("размер 122") + len("размер 122")

    _, after = run(catalog, text, chooser)
    assert asked[:2] == ["Половой признак", "Рост"]
    assert after.clarification and after.clarification.char_name == "Половой признак"  # still the user's call
    height = [a for a in after.assertions if a.property_raw == "Рост"]
    assert height and height[0].span.text == "размер 122" and height[0].value_raw == "122 см"


def test_no_choice_or_a_failing_model_keeps_the_question(catalog):
    def failing(*_):
        raise TimeoutError("slow model")

    for chooser in (lambda *_: None, failing):
        before, after = run(catalog, "Стол письменный 1200x600 мм, ЛДСП, бук", chooser)
        assert after.clarification and after.clarification.char_name == before.clarification.char_name
