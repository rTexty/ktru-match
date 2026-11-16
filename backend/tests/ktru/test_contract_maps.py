"""Correction maps and checked span edits use original Unicode code-point offsets."""

import uuid

import pytest

from src.ktru import contracts as C
from src.ktru.service.pipeline import Pipeline, ServiceError
from src.ktru.service.stages import LexicalRetriever


class EmptyCatalog:
    snapshot_id = "map-test"
    manifest = {"counts": {"final": 0}}

    def __init__(self, corrections=None):
        self.corrections = corrections or {}
        self.queries = []

    def correct_word(self, word):
        return self.corrections.get(word)

    def search(self, text):
        self.queries.append(text)
        return [], 0


def parse(pipeline, text):
    return pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))


def span_edit(state, start, end, replacement, **changes):
    patch = {"type": "replace_span", "span": {"start": start, "end": end,
        "text": state.text[start:end], "text_revision": state.revision}, "replacement": replacement,
        "snapshot_id": state.snapshot_id, "base_revision": state.revision}
    patch.update(changes)
    return C.EditRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision, patch=patch)


def test_span_edit_changes_only_selected_repetition_and_keeps_emoji():
    pipeline = Pipeline(EmptyCatalog())
    original = "🔧 молаток; молаток; масса упаковки 700 г"
    state = parse(pipeline, original)
    start = original.index("молаток", original.index(";") + 1)
    request = span_edit(state, start, start + len("молаток"), "молоток новый")
    changed = pipeline.edit(request)
    assert changed.text == "🔧 молаток; молоток новый; масса упаковки 700 г"
    assert changed.revision == state.revision + 1
    assert pipeline.edit(request) == changed
    undone = pipeline.undo(C.UndoRequest(session_id=state.session_id, request_id="undo-span",
        expected_revision=changed.revision))
    assert undone.text == original and undone.revision == changed.revision + 1


@pytest.mark.parametrize("changed", [
    {"snapshot_id": "another-snapshot"},
    {"base_revision": 0},
    {"span": {"start": 2, "end": 9, "text": "молоток", "text_revision": 1}},
    {"span": {"start": 2, "end": 9, "text": "молаток", "text_revision": 0}},
    {"span": {"start": 3, "end": 10, "text": "молаток", "text_revision": 1}},
])
def test_span_edit_rejects_invalid_preconditions_without_state_change(changed):
    pipeline = Pipeline(EmptyCatalog())
    state = parse(pipeline, "🔧 молаток и масса 700 г")
    request = span_edit(state, 2, 9, "молоток", **changed)
    with pytest.raises(ServiceError) as error:
        pipeline.edit(request)
    assert error.value.body.code in {"invalid_input", "stale_revision"}
    assert pipeline.get(state.session_id) == state


def test_whole_text_replacement_remains_a_supported_migration():
    pipeline = Pipeline(EmptyCatalog())
    state = parse(pipeline, "Старая строка")
    changed = pipeline.edit(C.EditRequest(session_id=state.session_id, request_id="whole-text",
        expected_revision=state.revision, patch=C.ReplaceTextPatch(text="Новая строка")))
    assert changed.text == "Новая строка"


def test_retrieval_preserves_original_and_maps_all_unequal_replacements():
    text = "🔧 молаток; дрельк; молаток; масса упаковки 700 г"
    catalog = EmptyCatalog({"молаток": "молоточек", "дрельк": "дрель"})
    result = LexicalRetriever(catalog).retrieve(text, catalog.snapshot_id)
    assert catalog.queries == ["🔧 молоточек; дрель; молоточек; масса упаковки 700 г"]
    assert result.search_text == catalog.queries[0]
    assert [(entry.original_start, entry.original_end, entry.transformed_start, entry.transformed_end, entry.kind)
        for entry in result.correction_map] == [(2, 9, 2, 11, "typo"), (11, 17, 13, 18, "typo"),
                                               (19, 26, 20, 29, "typo")]
    for correction in result.corrections:
        source, target = correction.original_span, correction.transformed_span
        assert text[source.start:source.end] == source.text == correction.original
        assert result.search_text[target.start:target.end] == target.text == correction.replacement


def test_layout_and_typos_are_applied_to_the_text_before_parsing():
    catalog = EmptyCatalog({"молаток": "молоточек"})
    pipeline = Pipeline(catalog)
    state = parse(pipeline, "🔧 vjkfnjr vjkfnjr")
    assert state.text == "🔧 молоточек молоточек"
    assert state.normalized_text is None and state.search_text is None and not state.correction_map
    assert [(c.kind, c.original, c.replacement) for c in state.corrections] == [
        ("layout", "🔧 vjkfnjr vjkfnjr", "🔧 молаток молаток"),
        ("typo", "молаток", "молоточек"), ("typo", "молаток", "молоточек")]
    assert [(c.transformed_span.start, c.transformed_span.end) for c in state.corrections[1:]] == [(2, 11), (12, 21)]
    assert not [diagnostic for diagnostic in state.diagnostics if diagnostic.kind in ("typo", "layout")]
    for assertion in state.assertions:
        if assertion.span:
            assert state.text[assertion.span.start:assertion.span.end] == assertion.span.text


def test_autocorrection_keeps_the_typed_case():
    catalog = EmptyCatalog()
    catalog.correct_word = lambda word: "молоток" if word.lower() == "молаток" else None  # Like Catalog: by norm().
    state = parse(Pipeline(catalog), "Молаток МОЛАТОК молаток")
    assert state.text == "Молоток МОЛОТОК молоток"


def test_diagnostic_cap_does_not_limit_corrected_search_words():
    tokens = [f"ошибка{chr(1072 + index)}" for index in range(26)]  # Cyrillic: Latin lookalikes are repaired
    catalog = EmptyCatalog(dict.fromkeys(tokens, "молоток"))
    result = LexicalRetriever(catalog).retrieve(" ".join(tokens), catalog.snapshot_id)
    assert catalog.queries == [" ".join(["молоток"] * len(tokens))]
    assert len([diagnostic for diagnostic in result.diagnostics if diagnostic.kind == "typo"]) == 16
    assert len(result.correction_map) >= len(tokens)


def test_layout_and_typos_keep_model_identifiers_unchanged():
    catalog = EmptyCatalog({"молаток": "молоточек", "AB": "молоток"})
    result = LexicalRetriever(catalog).retrieve("vjkfnjr AB123 🔧 vjkfnjr", catalog.snapshot_id)
    assert result.normalized_text == "молаток AB123 🔧 молаток"
    assert result.search_text == "молоточек AB123 🔧 молоточек"
    assert [(entry.original_start, entry.original_end, entry.transformed_start, entry.transformed_end)
        for entry in result.correction_map] == [(0, 7, 0, 9), (16, 23, 18, 27)]


def test_repeated_typo_is_corrected_everywhere_and_one_occurrence_can_be_reverted():
    pipeline = Pipeline(EmptyCatalog({"молаток": "молоток"}))
    corrected = " ".join(["молоток"] * 500)
    state = parse(pipeline, " ".join(["молаток"] * 500))
    assert state.text == corrected and len(state.corrections) == 500
    assert pipeline.get(state.session_id) == state
    assert not pipeline._get(state.session_id).requests
    reverted = pipeline.edit(C.EditRequest(session_id=state.session_id, request_id="one-of-many",
        expected_revision=state.revision, patch=span_edit(state, 0, 7, "молаток").patch))
    assert reverted.text == "молаток" + corrected[len("молоток"):]
    assert reverted.text.count("молаток") == 1


@pytest.mark.parametrize("source,target,segments", [
    ("🔧 молаток", "🔧 молоток", []),
    ("🔧 молаток", "🔧 молоток", [{"original_start": 2, "original_end": 9,
        "transformed_start": 3, "transformed_end": 9, "kind": "typo"}]),
    ("молаток", "молоточек", [{"original_start": 0, "original_end": 7,
        "transformed_start": 0, "transformed_end": 9, "kind": "layout"}]),
])
def test_source_map_validation_rejects_missing_changes_and_invalid_layout(source, target, segments):
    from src.ktru.engine.source_map import validate_source_map

    with pytest.raises(ValueError):
        validate_source_map(source, target, [C.SourceMapSegment(**entry) for entry in segments])


def test_reverting_one_autocorrection_keeps_the_others_and_undo_restores_them():
    pipeline = Pipeline(EmptyCatalog({"леревянный": "деревянный", "карандош": "карандаш"}))
    state = parse(pipeline, "стол леревянный, карандош")
    assert state.text == "стол деревянный, карандаш"
    wood, pencil = state.corrections
    reverted = pipeline.edit(span_edit(state, wood.transformed_span.start, wood.transformed_span.end, "леревянный"))
    assert reverted.text == "стол леревянный, карандаш"
    kept = [c for c in reverted.corrections if c.target == "text"]
    assert [(c.original, c.transformed_span.start) for c in kept] == [("карандош", pencil.transformed_span.start)]
    undone = pipeline.undo(C.UndoRequest(session_id=reverted.session_id, request_id="undo-revert",
        expected_revision=reverted.revision))
    assert undone.text == state.text
    assert [c.original for c in undone.corrections if c.target == "text"] == ["леревянный", "карандош"]
