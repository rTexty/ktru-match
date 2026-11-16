"""Capture the browser demo's cards from the API using the synthetic catalog."""

import argparse
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from src.ktru import contracts as C  # noqa: E402
from src.ktru.catalog.build import build  # noqa: E402

DESCRIPTION = "Молоток"
DEMO_CODES = {"500": "DEMO-HAMMER-500", "700": "DEMO-HAMMER-700"}


class Scenario(C.Model):
    description: str
    card: C.CardState
    answers: dict[str, C.CardState]


class Demo(C.Model):
    scenarios: list[Scenario]


def parse(client: TestClient, description: str, request_id: str) -> C.CardState:
    response = client.post(
        "/api/ktru/parse",
        json={
            "request_id": request_id,
            "expected_revision": 0,
            "text": description,
        },
    )
    return C.CardState.model_validate(response.raise_for_status().json())


def public_card(card: C.CardState, session_id: str) -> C.CardState:
    # Replace the generated session ID and machine-dependent timings for repeatable builds.
    return card.model_copy(update={"session_id": session_id, "timings_ms": C.Timings()})


def verify_code(card: C.CardState, expected: str | None) -> None:
    actual = card.final.position_ref.code if card.final else None
    if actual != expected:
        raise RuntimeError(f"Demo request {card.request_id} returned {actual!r}; expected {expected!r}.")


def verify_options(question: C.Clarification | None) -> C.Clarification:
    if question is None or question.kind != "characteristic":
        raise RuntimeError("The demo description must ask for a characteristic.")
    numeric_values = {option.answer.x for option in question.options if isinstance(option.answer, C.NumberValue)}
    unknown_count = sum(option.is_dont_know for option in question.options)
    if (
        numeric_values != DEMO_CODES.keys()
        or unknown_count != 1
        or len(question.options) != len(DEMO_CODES) + 1
        or len({option.id for option in question.options}) != len(question.options)
    ):
        raise RuntimeError("The demo clarification must offer both masses and an unknown answer.")
    return question


def capture_answer(client: TestClient, question: C.Clarification, option: C.ClarificationOption) -> C.CardState:
    # Each answer runs on its own session at the same initial question.
    branch = parse(client, DESCRIPTION, f"demo-branch-{option.id}")
    if branch.clarification != question:
        raise RuntimeError("The initial demo clarification changed between sessions.")
    response = client.post(
        "/api/ktru/answer",
        json={
            "session_id": branch.session_id,
            "request_id": f"demo-answer-{option.id}",
            "expected_revision": branch.revision,
            "question_id": question.id,
            "option_id": option.id,
        },
    )
    card = C.CardState.model_validate(response.raise_for_status().json())
    expected = DEMO_CODES[option.answer.x] if isinstance(option.answer, C.NumberValue) else None
    verify_code(card, expected)
    if not card.can_undo:
        raise RuntimeError("The demo must allow each clarification answer to be undone.")
    return public_card(card, "demo-clarification")


def capture(client: TestClient) -> Demo:
    initial = parse(client, DESCRIPTION, "demo-parse")
    verify_code(initial, None)
    question = verify_options(initial.clarification)
    answers = {option.id: capture_answer(client, question, option) for option in question.options}
    scenarios = [Scenario(description=DESCRIPTION, card=public_card(initial, "demo-clarification"), answers=answers)]
    for mass, expected in DEMO_CODES.items():
        description = f"{DESCRIPTION}, масса {mass} г"
        card = parse(client, description, f"demo-parse-{mass}")
        verify_code(card, expected)
        scenarios.append(Scenario(description=description, card=public_card(card, f"demo-{mass}"), answers={}))
    return Demo(scenarios=scenarios)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    output = parser.parse_args().output

    with TemporaryDirectory() as directory:
        snapshot_root = Path(directory)
        build(ROOT / "demo", snapshot_root)
        env = {name: value for name, value in os.environ.items() if not name.startswith("KTRU_")}
        env.update(KTRU_SNAPSHOT_DIR=str(snapshot_root / "current"), KTRU_SEARCH_MODE="lexical")
        with patch.dict(os.environ, env, clear=True):
            from src.api.ktru.app import app  # noqa: PLC0415

            with TestClient(app) as client:
                demo = capture(client)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(demo.model_dump_json(indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
