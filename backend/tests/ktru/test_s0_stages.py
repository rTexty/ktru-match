"""S0 stage regressions use a small real catalog and no external model service."""

import json
import sqlite3
import uuid
from dataclasses import replace

import pytest

from src.ktru import contracts as C
from src.ktru.catalog.build import DDL
from src.ktru.catalog.reader import Catalog
from src.ktru.engine import assertions as A
from src.ktru.engine.resolve import check
from src.ktru.service.pipeline import Pipeline, ServiceError


@pytest.fixture
def stage_catalog(tmp_path):
    with sqlite3.connect(tmp_path / "catalog.sqlite") as db:
        db.executescript(DDL)
        for index, (lo, hi) in enumerate(((400, 600), (600, 800)), 1):
            code = f"hammer-{index}"
            db.execute(
                "INSERT INTO positions (code,version,name,status,is_template,family_id,domain,supply_units) "
                "VALUES (?,1,'Молоток','ACTIVE',0,'hammer','tools','[]')", (code,),
            )
            allowed = [{"kind": "range", "lo": str(lo), "hi": str(hi), "hi_inclusive": False,
                        "okei_code": "163", "okei_name": "г", "value_code": f"v{index}"}]
            db.execute("INSERT INTO characteristics VALUES (?,1,'mass','Масса молотка',NULL,'2',1,0,1,?)",
                       (code, json.dumps(allowed)))
            db.execute("INSERT INTO positions_fts VALUES (?,1,'молоток',NULL,'масса молотка')", (code,))
    (tmp_path / "manifest.json").write_text(json.dumps({"snapshot_id": "test-stages", "counts": {"final": 2}}))
    return Catalog(tmp_path)


def parse(pipeline, text):
    return pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text))


def analyze(pipeline, state, facts):
    return pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
        expected_revision=state.revision), extractor=lambda _: facts)


@pytest.mark.parametrize("bounds", [
    {"lo": "800", "hi": "400"},
    {"lo": "500", "hi": "500", "hi_inclusive": False},
    {"lo": "invalid", "hi": "400"},
    {"lo": "400", "hi": "invalid"},
    {"lo": "NaN", "hi": "400"},
    {"lo": "400", "hi": "Infinity"},
])
def test_malformed_catalog_range_is_unknown_never_a_contradiction(stage_catalog, bounds):
    position = stage_catalog.position("hammer-1")
    characteristic = replace(position.chars[0], allowed=[{"kind": "range", "okei_code": "163", **bounds}])
    position = replace(position, chars=[characteristic])
    assertion = C.Assertion(id="mass", property_raw="Масса молотка", value_raw="500 г",
        value=C.NumberValue(x="500", unit_okei="163"), origin="text", extractor="rules")
    result, matched = check(position, "масса молотка", [assertion])
    assert result == "unknown"
    assert matched == []


def test_exclusion_carries_rule_input_quote_and_catalog_reference(stage_catalog):
    text = "Молоток, масса молотка 500 г"
    state = parse(Pipeline(stage_catalog), text)
    excluded = next(candidate for candidate in state.candidates if candidate.ref.code == "hammer-2")
    assert excluded.status == "excluded" and excluded.excluded_by
    by_id = {assertion.id: assertion for assertion in state.assertions}
    for result in excluded.excluded_by:
        assert result.result == "contradicts" and result.rule
        assert result.characteristic_ref.position_code == excluded.ref.code
        assert result.assertion_ids and all(by_id[ident].status == "supported" for ident in result.assertion_ids)
        quotes = [item for item in result.evidence if isinstance(item, C.TextEvidence)]
        catalog = [item for item in result.evidence if isinstance(item, C.CatalogEvidence)]
        assert quotes and catalog
        assert any(item.position_ref == excluded.ref and item.characteristic_ref == result.characteristic_ref
                   for item in catalog)
        for item in quotes:
            span = item.span
            assert text[span.start:span.end] == span.text and span.text_revision == state.revision
            assert any(item in by_id[ident].evidence for ident in result.assertion_ids)


def test_binding_preserves_snapshot_scoped_allowed_value_refs(stage_catalog):
    state = parse(Pipeline(stage_catalog), "Молоток, масса молотка 500 г")
    binding = next(binding for binding in state.bindings if binding.characteristic_ref.position_code == "hammer-1")
    expected = C.AllowedValueRef(characteristic_ref=binding.characteristic_ref, value_code="v1")
    assert binding.matched_value_refs == [expected]
    assert binding.matched_value_codes == ["v1"]
    serialized = binding.model_dump(mode="json")
    assert C.Binding.model_validate(serialized).matched_value_refs == [expected]


def test_model_extra_stays_source_backed_when_family_is_unknown(stage_catalog):
    pipeline = Pipeline(stage_catalog)
    text = "Ксилофон, покрытие водоотталкивающее, масса упаковки 700 г"
    state = analyze(pipeline, parse(pipeline, text), [
        {"property": "Покрытие", "value": "водоотталкивающее", "quote": "покрытие водоотталкивающее"},
        {"property": "Цвет", "value": "красный", "quote": "красный"},
    ])
    assert state.family is None and state.final is None and state.suggestion is None
    extra = next(assertion for assertion in state.assertions if assertion.property_raw == "Покрытие")
    field = next(field for field in state.fields if extra.id in field.assertion_ids)
    assert field.source == "new" and field.subject == extra.subject
    assert field.evidence and all(item in extra.evidence for item in field.evidence)
    for item in field.evidence:
        if isinstance(item, C.TextEvidence):
            assert text[item.span.start:item.span.end] == item.span.text
    package = next(assertion for assertion in state.assertions if assertion.subject.kind == "package")
    assert package.value.x == "700" and package.evidence
    assert state.rejected_outputs and not any(assertion.property_raw == "Цвет" for assertion in state.assertions)


def test_free_clause_keeps_text_evidence_and_subject(stage_catalog):
    text = "Молоток, индивидуальная гравировка логотипа"
    state = parse(Pipeline(stage_catalog), text)
    extra = next(field for field in state.fields if "гравиров" in field.display_ru)
    assert extra.source == "new" and extra.subject
    quotes = [item for item in extra.evidence if isinstance(item, C.TextEvidence)]
    assert quotes
    assert all(text[item.span.start:item.span.end] == item.span.text for item in quotes)
    assert all(item.span.text_revision == state.revision for item in quotes)


@pytest.mark.parametrize("product", ["Молоток", "Ксилофон"])
def test_additional_clauses_are_not_repeated_as_a_whole_sentence(stage_catalog, product):
    text = f"{product}, индивидуальная гравировка логотипа, гарантия производителя"
    state = parse(Pipeline(stage_catalog), text)
    extras = [f for f in state.fields if f.label_ru == "Дополнительно"]
    assert [f.display_ru for f in extras] == [product, "индивидуальная гравировка логотипа",
                                           "гарантия производителя"]
    assert all(f.assertion_ids and f.subject and f.evidence for f in extras)
    quotes = [e.span.text for f in extras for e in f.evidence if isinstance(e, C.TextEvidence)]
    assert all(f.display_ru in quotes for f in extras)


def test_analyze_reuses_retrieval_and_replay_runs_no_stages(stage_catalog, monkeypatch):
    pipeline = Pipeline(stage_catalog)
    state = parse(pipeline, "Молоток, масса молотка 500 г")
    queries, model_calls = [], []
    original_search = stage_catalog.search

    def search(query, *args, **kwargs):
        queries.append(query)
        return original_search(query, *args, **kwargs)

    def extractor(text):
        model_calls.append(text)
        return []

    monkeypatch.setattr(stage_catalog, "search", search)
    request = C.AnalyzeRequest(session_id=state.session_id, request_id="single-stage-run",
        expected_revision=state.revision)
    result = pipeline.analyze(request, extractor=extractor)
    assert not queries and len(model_calls) == 1
    assert pipeline.analyze(request, extractor=extractor) == result
    assert not queries and len(model_calls) == 1


class RecordingRules:
    def __init__(self):
        self.calls = []

    def extract(self, text, text_revision):
        self.calls.append((text, text_revision))
        return C.ExtractionResult(assertions=A.rule_assertions(text, text_revision), status="provisional")


class RecordingExtractor:
    def __init__(self, advance=None):
        self.calls = []
        self.advance = advance or (lambda _: None)

    def extract(self, text, text_revision):
        self.calls.append((text, text_revision))
        self.advance(.05)
        quote = "покрытие водоотталкивающее"
        start = text.index(quote)
        span = C.Span(start=start, end=start + len(quote), text=quote, text_revision=text_revision)
        assertion = C.Assertion(id="open-coating", property_raw="Покрытие", value_raw="водоотталкивающее",
            value=C.EnumValue(label="водоотталкивающее"), origin="text", extractor="llm", span=span,
            evidence=[C.TextEvidence(span=span, extractor="llm")])
        return C.ExtractionResult(assertions=[assertion], status="analyzed", timings=C.Timings(extract=50))


class RecordingRetriever:
    def __init__(self, catalog, advance=None):
        self.catalog = catalog
        self.calls = []
        self.advance = advance or (lambda _: None)

    def retrieve(self, text, snapshot_id):
        self.calls.append((text, snapshot_id))
        self.advance(.02)
        search = self.catalog.search(text)
        scope = C.SearchScope(snapshot_id=snapshot_id, eligible_count=2, examined_count=search.examined,
            retained_count=sum(len(family.members) for family in search.families),
            family_ids=[family.family_id for family in search.families], family_membership_complete=True,
            retrieval_truncated=False, coverage_status="unconfirmed", reason_ru="Тестовый поиск")
        return C.RetrievalResult(families=search.families, scope=scope, timings=C.Timings(retrieve=20), status="complete")


def test_pipeline_uses_injected_stages_and_keeps_open_assertions(stage_catalog):
    rules, extractor, retriever = RecordingRules(), RecordingExtractor(), RecordingRetriever(stage_catalog)
    pipeline = Pipeline(stage_catalog, rule_extractor=rules, extractor=extractor, retriever=retriever)
    text = "Молоток, масса молотка 500 г, покрытие водоотталкивающее"
    state = parse(pipeline, text)
    assert rules.calls == [(text, 1)] and not extractor.calls
    assert retriever.calls == [(text, stage_catalog.snapshot_id)]
    request = C.AnalyzeRequest(session_id=state.session_id, request_id="typed-stage",
        expected_revision=state.revision)
    result = pipeline.analyze(request)
    assert len(rules.calls) == len(retriever.calls) == 2 and len(extractor.calls) == 1
    assert all(call[0] == text for call in rules.calls + extractor.calls)
    extra = next(assertion for assertion in result.assertions if assertion.property_raw == "Покрытие")
    field = next(field for field in result.fields if extra.id in field.assertion_ids)
    assert extra.extractor == "llm" and extra.evidence
    assert field.source == "new" and field.evidence
    assert pipeline.analyze(request) == result
    assert len(rules.calls) == len(retriever.calls) == 2 and len(extractor.calls) == 1


def test_injected_stage_timings_keep_retrieval_and_extraction_separate(stage_catalog):
    pipeline = Pipeline(stage_catalog, rule_extractor=RecordingRules(),
        extractor=RecordingExtractor(), retriever=RecordingRetriever(stage_catalog))
    state = parse(pipeline, "Молоток, покрытие водоотталкивающее")
    result = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id="stage-timings",
        expected_revision=state.revision))
    assert result.timings_ms.extract == 50
    assert result.timings_ms.retrieve == 20
    assert all(value >= 0 for value in result.timings_ms.model_dump().values())


@pytest.mark.parametrize("allowed,kind", [
    ([{"kind": "range", "lo": "600", "hi": "800", "okei_code": "163"},
      {"kind": "range", "lo": "800", "hi": "400", "okei_code": "163"}], "2"),
    ([{"kind": "range", "lo": "600", "hi": "800", "okei_code": "163"}], "unknown-kind"),
])
def test_schema_defect_keeps_candidate_alive_and_explains_unknown(stage_catalog, allowed, kind):
    with sqlite3.connect(stage_catalog.dir / "catalog.sqlite") as db:
        db.execute("UPDATE characteristics SET allowed=?,kind_raw=? WHERE position_code='hammer-2'",
                   (json.dumps(allowed), kind))
    state = parse(Pipeline(stage_catalog), "Молоток, масса молотка 500 г")
    candidate = next(candidate for candidate in state.candidates if candidate.ref.code == "hammer-2")
    assert candidate.status == "alive" and not candidate.excluded_by
    assert any(diagnostic.kind == "schema_defect" for diagnostic in state.diagnostics)
    assert state.final is None


def test_model_manifest_is_preserved_with_source_backed_fact(stage_catalog):
    manifest = C.ModelManifest(model="open-test-model", revision="sha256:local-test", runtime="local-test",
        prompt_sha256="a" * 64, parameters={"temperature": 0}, identity_verified=True)

    class WithManifest(RecordingExtractor):
        def extract(self, text, text_revision):
            result = super().extract(text, text_revision)
            return result.model_copy(update={"model_manifest": manifest})

    pipeline = Pipeline(stage_catalog, extractor=WithManifest(), retriever=RecordingRetriever(stage_catalog))
    state = parse(pipeline, "Ксилофон, покрытие водоотталкивающее")
    result = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id="manifest-stage",
        expected_revision=state.revision))
    assert result.model_manifests == [manifest]
    assert result.model_manifests[0].identity_verified
    extra = next(assertion for assertion in result.assertions if assertion.property_raw == "Покрытие")
    assert extra.evidence and result.family is None and result.final is None


def test_injected_rule_fact_without_subject_survives_unknown_family(stage_catalog):
    text = "Ксилофон, покрытие водоотталкивающее"
    quote = "покрытие водоотталкивающее"

    class NoSubjectRules:
        def extract(self, text, text_revision):
            start = text.index(quote)
            span = C.Span(start=start, end=start + len(quote), text=quote, text_revision=text_revision)
            fact = C.Assertion(id="without-subject", property_raw="Покрытие", value_raw="водоотталкивающее",
                value=C.EnumValue(label="водоотталкивающее"), origin="text", extractor="rules", subject=None,
                span=span, evidence=[C.TextEvidence(span=span)])
            return C.ExtractionResult(assertions=[fact])

    state = parse(Pipeline(stage_catalog, rule_extractor=NoSubjectRules()), text)
    assert state.family is None and state.final is None
    fact = next(fact for fact in state.assertions if fact.id == "without-subject")
    field = next(field for field in state.fields if fact.id in field.assertion_ids)
    assert fact.value_raw == "водоотталкивающее" and field.source == "new"
    assert field.evidence == fact.evidence and field.evidence


@pytest.mark.parametrize("polarity,operator", [("uncertain", "eq"), ("positive", "approx")])
def test_supported_uncertain_fact_cannot_exclude(stage_catalog, polarity, operator):
    position = stage_catalog.position("hammer-2")
    assertion = C.Assertion(id="uncertain-mass", property_raw="Масса молотка", value_raw="500 г",
        value=C.NumberValue(x="500", unit_okei="163"), origin="text", extractor="rules",
        status="supported", polarity=polarity, operator=operator)
    result, matched = check(position, "масса молотка", [assertion])
    assert result == "unknown" and matched == []


@pytest.mark.parametrize("okei", ["999", None])
def test_unknown_catalog_numeric_unit_cannot_exclude_measured_fact(stage_catalog, okei):
    position = stage_catalog.position("hammer-2")
    allowed = [{"kind": "range", "lo": "600", "hi": "800", "okei_code": okei}]
    position = replace(position, chars=[replace(position.chars[0], allowed=allowed)])
    assertion = C.Assertion(id="measured-mass", property_raw="Масса молотка", value_raw="500 г",
        value=C.NumberValue(x="500", unit_okei="163"), origin="text", extractor="rules")
    result, matched = check(position, "масса молотка", [assertion])
    assert result == "unknown" and matched == []


@pytest.mark.parametrize("value", [C.NumberValue(x="500", unit_okei="163"), C.EnumValue(label="Стальной")])
def test_mixed_catalog_enum_numeric_domain_cannot_exclude(stage_catalog, value):
    position = stage_catalog.position("hammer-2")
    allowed = [{"kind": "enum", "label": "Деревянный"},
               {"kind": "range", "lo": "600", "hi": "800", "okei_code": "163"}]
    position = replace(position, chars=[replace(position.chars[0], allowed=allowed)])
    assertion = C.Assertion(id="mixed-domain", property_raw="Масса молотка", value_raw="500 г",
        value=value, origin="text", extractor="rules")
    result, matched = check(position, "масса молотка", [assertion])
    assert result == "unknown" and matched == []


def test_repeated_typo_is_corrected_in_text_before_parsing(stage_catalog, monkeypatch):
    text = " ".join(["молаток"] * 500)
    corrected = " ".join(["молоток"] * 500)
    monkeypatch.setattr(stage_catalog, "correct_word", lambda word: "молоток" if word == "молаток" else None)
    pipeline = Pipeline(stage_catalog)
    state = parse(pipeline, text)
    assert state.text == corrected and state.search_text is None
    assert not [diagnostic for diagnostic in state.diagnostics if diagnostic.kind == "typo"]
    assert pipeline.get(state.session_id) == state
    assert len(state.corrections) == 500
    assert all(c.target == "text" and c.original == "молаток" and c.transformed_span
               and corrected[c.transformed_span.start:c.transformed_span.end] == "молоток" for c in state.corrections)
    spans = [assertion.span for assertion in state.assertions if assertion.span]
    for entity in [*state.assertions, *state.fields, *state.subjects]:
        spans.extend(evidence.span for evidence in entity.evidence if isinstance(evidence, C.TextEvidence))
    assert spans
    assert all(corrected[span.start:span.end] == span.text and span.text_revision == state.revision for span in spans)
    # Reverting one correction keeps the user's spelling; resubmitting that text does not re-correct it.
    first = state.corrections[0].transformed_span
    reverted = pipeline.edit(C.EditRequest(session_id=state.session_id, request_id="revert-typo",
        expected_revision=state.revision, patch=C.ReplaceSpanPatch(
            span=C.Span(start=first.start, end=first.end, text=first.text, text_revision=state.revision),
            replacement="молаток", snapshot_id=state.snapshot_id, base_revision=state.revision)))
    assert reverted.text == "молаток" + corrected[len("молоток"):]
    assert len([c for c in reverted.corrections if c.target == "text"]) == 499  # The other fixes stay.
    again = pipeline.parse(C.ParseRequest(session_id=reverted.session_id, request_id="resubmit",
        expected_revision=reverted.revision, text=reverted.text))
    assert again.text == reverted.text


def test_many_unique_typos_limit_diagnostics_but_correct_entire_search(stage_catalog, monkeypatch):
    from src.ktru.service.stages import LexicalRetriever

    tokens = [f"ошибка{chr(1072 + index)}" for index in range(26)]  # Cyrillic: Latin lookalikes are repaired
    text = " ".join(tokens)
    queries = []
    original_search = stage_catalog.search

    def search(query):
        queries.append(query)
        return original_search(query)

    monkeypatch.setattr(stage_catalog, "correct_word", lambda word: "молоток" if word in tokens else None)
    monkeypatch.setattr(stage_catalog, "search", search)
    result = LexicalRetriever(stage_catalog).retrieve(text, stage_catalog.snapshot_id)
    typos = [diagnostic for diagnostic in result.diagnostics if diagnostic.kind == "typo"]
    assert 0 < len(typos) <= 16
    assert len({diagnostic.id for diagnostic in typos}) == len(typos)
    assert queries == [" ".join(["молоток"] * len(tokens))]
    for diagnostic in typos:
        assert len(diagnostic.fixes) == 1
        patch = diagnostic.fixes[0].patch
        assert isinstance(patch, C.ReplaceSpanPatch)
        assert text[patch.span.start:patch.span.end] == patch.span.text
        assert patch.replacement == "молоток"
        changed = text[:patch.span.start] + patch.replacement + text[patch.span.end:]
        assert changed.split().count("молоток") == 1
        assert all(token in changed.split() for token in tokens if token != patch.span.text)


def test_replay_cache_byte_budget_evicts_old_state_but_preserves_latest_replay(stage_catalog, monkeypatch):
    from src.ktru.service import pipeline as service

    pipeline = Pipeline(stage_catalog)
    state = parse(pipeline, "Молоток, масса молотка 500 г")
    budget = len(state.model_dump_json().encode()) * 3 // 2
    monkeypatch.setattr(service, "MAX_CACHED_STATE_BYTES", budget)
    requests = []
    for index in range(4):
        request = C.ParseRequest(session_id=state.session_id, request_id=f"cache-transition-{index}",
            expected_revision=state.revision, text=state.text)
        requests.append(request)
        state = pipeline.parse(request)
    cached = pipeline._get(state.session_id).requests
    assert sum(len(result.model_dump_json().encode()) for _, result in cached.values()) <= budget
    assert requests[0].request_id not in cached and requests[-1].request_id in cached
    assert pipeline.parse(requests[-1]) == state
    with pytest.raises(ServiceError) as expired_replay:
        pipeline.parse(requests[0])
    assert expired_replay.value.body.code == "stale_revision"


def test_oversized_state_is_published_without_retention_in_replay_cache(stage_catalog, monkeypatch):
    from src.ktru.service import pipeline as service

    monkeypatch.setattr(service, "MAX_CACHED_STATE_BYTES", 1)
    pipeline = Pipeline(stage_catalog)
    state = parse(pipeline, "Молоток, масса молотка 500 г")
    assert pipeline.get(state.session_id) == state and state.revision == 1
    assert not pipeline._get(state.session_id).requests
