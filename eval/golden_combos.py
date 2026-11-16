"""Execute COMBO records whose precondition is a reproducible fixture; leave the others fixture_missing.

Two fixture kinds exist:
- request replay (COMBO-097/098): the same request ID is sent again to the evaluated pipeline;
- catalog context (FIXTURE_CATALOGS): the text is parsed against a small synthetic catalog that has exactly the
  declared search context (a candidate value, a schema without a field, near-duplicate fields, ...).
Fixture runs use the same stages as ordinary single records (parse, plus analyze with the evaluated model when
requested). Browser behavior and the manual clauses of a record stay unassessed.
"""

import json
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

import golden

from src.ktru import contracts as C
from src.ktru.catalog.build import DDL
from src.ktru.catalog.reader import Catalog
from src.ktru.service.errors import ServiceError
from src.ktru.service.pipeline import Pipeline

# COMBO-098 declares only that the original payload was "about a blue drill"; this is that synthetic payload.
BLUE_PAYLOAD = "Дрель синяя."


def _enum(*labels):
    return [{"kind": "enum", "label": label, "value_code": f"v{index}"} for index, label in enumerate(labels, 1)]


def _grams(lo, hi):
    return [{"kind": "range", "lo": str(lo), "hi": str(hi), "okei_code": "163", "okei_name": "г", "value_code": "v1"}]


# Each fixture states the precondition it reproduces. positions: (code, name, chars); chars: (name, allowed).
_HAMMERS = {
    "family": "hammer", "domain": "tools",
    "positions": [
        ("hammer-800", "Молоток", [("Материал рукоятки", _enum("Дерево", "Фибергласс")), ("Масса", _grams(800, 800))]),
        ("hammer-500", "Молоток", [("Материал рукоятки", _enum("Дерево", "Фибергласс")), ("Масса", _grams(500, 500))]),
    ],
}
_CHAIRS = {
    "family": "chair", "domain": "furniture",
    "positions": [(f"chair-{index}", "Стул", [("Материал", _enum(material))])
                  for index, material in enumerate(("Металл", "Дерево", "Пластик"), 1)],
}
_GLOVES = {
    "family": "gloves", "domain": "clothing",
    "positions": [(f"gloves-{size}", "Перчатки", [("Цвет", _enum("Синий", "Белый")), ("Размер", _enum(size))])
                  for size in ("L", "M")],
}
FIXTURE_CATALOGS = {
    "COMBO-007": {**_HAMMERS, "precondition": "candidate hammer-800 has a fixed mass 800 g; the text has no mass"},
    "COMBO-008": {**_HAMMERS, "precondition": "candidate hammer-800 has a fixed mass 800 g; scope is not confirmed"},
    "COMBO-013": {
        "family": "drill", "domain": "tools",
        "positions": [("drill-1", "Дрель", [("Наличие кейса", _enum("Да", "Нет"))])],
        "ste": [("D-7-Plus", "Дрель D-7 Plus", "Модель: D-7 Plus; Кейс: да; Запасной аккумулятор: да")],
        "precondition": "a similar listing D-7 Plus has a case and a spare battery; the modification is not checked",
    },
    "COMBO-015": {**_CHAIRS, "precondition": "three chair candidates differ by material; one is metal"},
    "COMBO-016": {**_CHAIRS, "precondition": "three chair candidates differ by material; one is metal"},
    "COMBO-019": {**_GLOVES, "precondition": "the glove schema lists colour and size, not aroma"},
    "COMBO-020": {
        **_GLOVES, "ste": [("gloves-mint", "Перчатки ароматизированные", "Цвет: синий; Размер: L; Аромат: мята")],
        "precondition": "the glove schema lists colour and size; a neighbouring listing is mint-scented",
    },
    "COMBO-087": {
        "family": "sofa", "domain": "furniture",
        "positions": [(f"sofa-{index}", "Диван", [("Материал каркаса", _enum("Дерево", "Металл")),
                                                  ("Материал обивки", _enum(upholstery)),
                                                  ("Материал наполнителя", _enum("Пенополиуретан"))])
                      for index, upholstery in enumerate(("Полиэстер", "Кожа"), 1)],
        "precondition": "the sofa schema has several close material fields (frame, upholstery, filling)",
    },
}


def build_catalog(spec, directory):
    """Write a minimal snapshot (same DDL as the real build) and return its reader."""
    directory = Path(directory)
    db = sqlite3.connect(directory / "catalog.sqlite")
    db.executescript(DDL)
    for code, name, chars in spec["positions"]:
        db.execute("INSERT INTO positions (code,version,name,status,is_template,family_id,domain,supply_units) "
                   "VALUES (?,1,?,'ACTIVE',0,?,?,'[]')", (code, name, spec["family"], spec["domain"]))
        for index, (char_name, allowed) in enumerate(chars, 1):
            db.execute("INSERT INTO characteristics VALUES (?,1,?,?,NULL,'2',1,0,1,?)",
                       (code, f"c{index}", char_name, json.dumps(allowed, ensure_ascii=False)))
        db.execute("INSERT INTO positions_fts VALUES (?,1,?,NULL,?)",
                   (code, name.casefold(), " ".join(char_name.casefold() for char_name, _ in chars)))
    for ste_id, name, chars in spec.get("ste", []):
        db.execute("INSERT INTO ste VALUES (?,?,?,?)", (ste_id, spec["domain"], name, chars))
    db.commit()
    db.close()
    snapshot_id = "fixture-" + spec["family"]
    (directory / "manifest.json").write_text(json.dumps({"snapshot_id": snapshot_id,
                                                         "counts": {"final": len(spec["positions"])}}))
    return Catalog(directory)


def _parse(pipeline, text, request_id=None, session_id=None, revision=0):
    return pipeline.parse(C.ParseRequest(request_id=request_id or uuid.uuid4().hex, session_id=session_id,
                                         expected_revision=revision, text=text))


def _describe(pipeline, text, with_model):
    """Same stages as an ordinary single record: parse, then analyze with the evaluated model when requested.
    Returns (requests in order, their responses, model error or None)."""
    parse = C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text)
    state = pipeline.parse(parse)
    requests, responses, error = [parse], [state], None
    if with_model:
        analyze = C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
                                   expected_revision=state.revision)
        state = pipeline.analyze(analyze)
        requests, responses = [parse, analyze], [*responses, state]
        if state.status != "analyzed" or any(d.kind == "model_unavailable" for d in state.diagnostics):
            error = "model extraction unavailable or incomplete"
    return requests, responses, error


def _dump(card):
    return card.model_dump(mode="json")


def _dispatch(pipeline, request):
    return pipeline.analyze(request) if isinstance(request, C.AnalyzeRequest) else pipeline.parse(request)


def _same_request_replay(case, pipeline, with_model):
    """COMBO-097: identical retries return the cached responses and neither re-run analysis nor add a revision."""
    requests, responses, error = _describe(pipeline, case["text"], with_model)
    session_id = responses[0].session_id
    before = _dump(pipeline.get(session_id))
    replayed = [_dispatch(pipeline, request.model_copy(deep=True)) for request in requests]
    after = _dump(pipeline.get(session_id))
    checks = (("exact_cached_response differs", [_dump(r) for r in replayed] == [_dump(r) for r in responses]),
              ("replay changed server state", after == before),
              ("server revision delta differs", after["revision"] == before["revision"]))
    return after, len(checks), [name for name, ok in checks if not ok], error


def _changed_payload_replay(case, pipeline, with_model):
    """COMBO-098: the same request ID with another payload is a 409 conflict; a new request ID is accepted.
    Only request bookkeeping is checked here, so no model stage runs."""
    request_id = uuid.uuid4().hex
    first = _dump(_parse(pipeline, BLUE_PAYLOAD, request_id))
    before = _dump(pipeline.get(first["session_id"]))
    error = None
    try:
        _parse(pipeline, case["text"], request_id)
    except ServiceError as exc:
        error = exc
    after = _dump(pipeline.get(first["session_id"]))
    cached = _dump(_parse(pipeline, BLUE_PAYLOAD, request_id))
    corrected = _dump(_parse(pipeline, case["text"], session_id=first["session_id"], revision=after["revision"]))
    checks = (
        ("expected error status/code differs",
         error is not None and (error.status, error.body.code) == (409, "request_conflict")),
        ("rejected request changed server state", after == before),
        ("exact_cached_response differs", cached == first),
        ("later correction was not accepted",
         corrected["revision"] == after["revision"] + 1 and corrected["text"] == case["text"]),
    )
    return corrected, len(checks), [name for name, ok in checks if not ok], None


def _catalog_context(case, pipeline, with_model):
    """Parse the record against a catalog that has exactly its declared search context."""
    with tempfile.TemporaryDirectory() as directory:
        fixture = Pipeline(build_catalog(FIXTURE_CATALOGS[case["case_id"]], directory), extractor=pipeline.extractor)
        _, responses, error = _describe(fixture, case["text"], with_model)
    return _dump(responses[-1]), 0, [], error


REPLAYS = {"COMBO-097": _same_request_replay, "COMBO-098": _changed_payload_replay}


def run_combo(case, pipeline, with_model=False):
    """Return a graded row for a COMBO record with a reproducible fixture, else None."""
    case_id = case["case_id"]
    if case_id in REPLAYS:
        run, fixture = REPLAYS[case_id], "request_replay"
    elif case_id in FIXTURE_CATALOGS:
        run, fixture = _catalog_context, FIXTURE_CATALOGS[case_id]["precondition"]
    else:
        return None
    started, state = time.perf_counter(), None
    stage = "parse+analyze" if with_model and case_id != "COMBO-098" else "parse_rules_only"
    try:
        state, extra, extra_failures, error = run(case, pipeline, with_model)
        result = golden.grade(case, state, error)
    except Exception as exc:
        result = {"status": "runtime_error", "failures": [f"{type(exc).__name__}: fixture request failed"], "checked": 0}
    if result["status"] != "runtime_error":
        failures = [*result["failures"], *extra_failures]
        checked = result["checked"] + extra
        result = {"status": "fail_checks" if failures else "pass_checks" if checked else "unassessed",
                  "failures": failures, "checked": checked}
    return {**result, "stage": stage, "fixture": fixture, "state": state, "precondition_status": "fixture",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
