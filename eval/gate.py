"""Labelled evaluation gate. No labels or resource evidence are inferred from predictions.

Expectations extend the case contract without changing smoke cases. Assessed labels are:
family_id, out_of_scope, final (versioned ref or null), alive_refs (versioned refs),
incomplete (questions population), facts (property_raw, value, operator, polarity,
subject_kind), bindings (property_raw, characteristic_ref), forbidden_fills
(property_raw or characteristic_ref), diagnostics (kind), unchanged_facts, extras.
Absent keys mean unassessed. Gold must be created and reviewed outside this runner.
"""

import hashlib
import json
import math
import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

DOMAINS = ("clothing", "furniture", "tools")
DEFINITIONS = {
    "retrieval.family_recall_at_3": "Cases with family_id: gold family in initial top 3 / labelled cases.",
    "retrieval.out_of_scope_precision": "Predicted out_of_scope on scope-labelled cases: correct / predictions.",
    "classification.final_code_accuracy": "All unique gold cases: exact final code and version / cases.",
    "classification.false_unique_rate_max": "Ambiguous or explicit null-final gold: finalized / cases.",
    "classification.alive_superset_recall": "Cases with nonempty alive_refs: every gold ref alive / cases.",
    "classification.questions_to_final_p50_max": "Median explicit answers to final on incomplete unique gold cases.",
    "extraction.value_unit_accuracy": "Expected typed facts: exact value, unit, operator, polarity / facts.",
    "extraction.binding_accuracy": "Versioned expected bindings: matching assertion bound to exact ref / bindings.",
    "extraction.unsupported_value_rate_max": "Returned supported assertions: invalid or absent evidence / assertions.",
    "extraction.forbidden_fill_rate_max": "Explicit forbidden fill opportunities: forbidden fill observed / labels.",
    "diagnostics.recall": "Expected diagnostic kinds: found / expected kinds.",
    "diagnostics.false_alarm_rate_max": "Explicit clean cases (diagnostics=[]): any diagnostic / cases.",
    "diagnostics.silent_change_rate_max": "Expected unchanged facts: changed without diagnostic or user evidence / facts.",
    "latency_ms.parse_p95": "Nearest-rank p95 measured parse wall time after successful warmup.",
    "latency_ms.analyze_p95": "Nearest-rank p95 measured analyze wall time after successful warmup.",
    "resources.peak_vram_gb_max": "Measured runtime peak VRAM in GB; requires external runtime evidence.",
    "completion_coverage": "Unique gold cases with any final / all unique gold cases.",
    "wrong_unique_rate": "Unique gold cases with wrong code or version final / all unique gold cases.",
    "fact_recall": "Expected facts recovered with typed value and subject / all expected facts.",
    "subject_accuracy": "Expected facts with subject_kind: correct recovered subject / labelled facts.",
    "extras_recall": "Explicit expected extras retained in non-KTRU card fields / expected extras.",
}


def load_thresholds(path):
    """Read the deliberately small nested numeric YAML subset used by thresholds.yaml.

    Reject unsupported syntax instead of silently skipping a human-owned threshold.
    """
    thresholds, section = {}, None
    for number, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"Threshold line {number}: expected key: numeric value")
        key, value = (part.strip() for part in line.split(":", 1))
        if not key or not all(c.isalnum() or c == "_" for c in key):
            raise ValueError(f"Threshold line {number}: invalid key")
        if not line[0].isspace() and not value:
            section = key
            continue
        if not line.startswith("  ") or line.startswith("   ") or not section:
            raise ValueError(f"Threshold line {number}: unsupported YAML structure")
        full_key = f"{section}.{key}"
        try:
            numeric = float(value)
        except ValueError as error:
            raise ValueError(f"Threshold line {number}: expected finite number") from error
        if full_key in thresholds or not math.isfinite(numeric) or numeric < 0:
            raise ValueError(f"Threshold line {number}: duplicate or invalid threshold")
        thresholds[full_key] = numeric
    if not thresholds:
        raise ValueError("Threshold file has no metrics")
    return thresholds


def load_cases(directory, split):
    if directory is None:
        return [], [], [f"{split}: case directory is not configured"]
    directory = Path(directory)
    files = sorted(directory.rglob("*.jsonl")) if directory.is_dir() else []
    cases, inputs, errors = [], [], []
    if not files:
        errors.append(f"{split}: no JSONL case files in {directory}")
    for path in files:
        raw = path.read_bytes()
        inputs.append({"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(), "split": split})
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeError:
            errors.append(f"{path}: not UTF-8")
            continue
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                case = json.loads(line)
                if not isinstance(case, dict):
                    raise ValueError("case must be an object")
                if case.get("split") != split:
                    errors.append(f"{path}:{number}: split must be {split}")
                    continue
                cases.append(case)
            except (ValueError, TypeError) as error:
                errors.append(f"{path}:{number}: {error}")
    if not cases:
        errors.append(f"{split}: no cases loaded")
    return cases, inputs, errors


def _ref(value):
    if not isinstance(value, dict):
        return None
    code, version = value.get("code"), value.get("version")
    if not isinstance(code, str) or not code or type(version) is not int or version < 1:
        return None
    return code, version


def _char_ref(value):
    if not isinstance(value, dict):
        return None
    ref = _ref({"code": value.get("position_code"), "version": value.get("position_version")})
    code = value.get("char_code")
    return (*ref, code) if ref and isinstance(code, str) and code else None


def validate_cases(cases):
    errors, seen_ids, groups = [], set(), {}
    for index, case in enumerate(cases):
        label = str(case.get("id", f"row-{index}"))
        for key in ("id", "split", "domain", "group", "family_group", "source", "text"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                errors.append(f"{label}: missing or invalid {key}")
        if case.get("split") not in ("dev", "holdout") or case.get("domain") not in DOMAINS:
            errors.append(f"{label}: invalid split or domain")
        if case.get("id") in seen_ids:
            errors.append(f"{label}: duplicate id")
        seen_ids.add(case.get("id"))
        for key in ("group", "family_group", "source", "source_hash"):
            value = case.get(key)
            if not isinstance(value, str) or not value:
                continue
            identity = key, value
            prior = groups.get(identity)
            if prior and prior != case.get("split"):
                errors.append(f"{label}: {key} {value!r} crosses splits")
            groups[identity] = case.get("split")
        if "source_hash" in case and (not isinstance(case["source_hash"], str)
                or len(case["source_hash"]) != 64
                or any(c not in "0123456789abcdef" for c in case["source_hash"])):
            errors.append(f"{label}: source_hash must be lowercase SHA-256")
        expect = case.get("expect")
        if not isinstance(case.get("answers"), list) or not isinstance(expect, dict):
            errors.append(f"{label}: answers must be a list and expect an object")
            continue
        for answer in case["answers"]:
            if not isinstance(answer, dict) or (("option" in answer) == ("value" in answer)):
                errors.append(f"{label}: answer needs exactly one option or typed value")
        supported_keys = {"family_id", "out_of_scope", "final", "alive_refs", "must_not_finalize", "incomplete",
                          "facts", "bindings", "forbidden_fills", "diagnostics", "unchanged_facts", "extras",
                          "readiness", "questions", "questions_max", "clarification"}
        errors += [f"{label}: unsupported expectation {key}" for key in expect if key not in supported_keys]
        if "readiness" in expect and expect["readiness"] not in ("ready", "needs_answers", "has_blockers", "no_code"):
            errors.append(f"{label}: invalid readiness label")
        for key in ("questions", "questions_max"):
            if key in expect and (type(expect[key]) is not int or expect[key] < 0):
                errors.append(f"{label}: {key} must be a nonnegative integer")
        if "clarification" in expect and expect["clarification"] is not None:
            if not isinstance(expect["clarification"], dict) or not expect["clarification"].get("kind"):
                errors.append(f"{label}: clarification needs kind or explicit null")
        if "final" in expect and expect["final"] is not None and _ref(expect["final"]) is None:
            errors.append(f"{label}: final requires code and version")
        if expect.get("must_not_finalize") and expect.get("final") is not None:
            errors.append(f"{label}: contradictory final labels")
        if "alive_refs" in expect and (not isinstance(expect["alive_refs"], list)
                or not expect["alive_refs"] or any(_ref(ref) is None for ref in expect["alive_refs"])):
            errors.append(f"{label}: alive_refs requires nonempty versioned refs")
        for key in ("facts", "bindings", "forbidden_fills", "diagnostics", "unchanged_facts", "extras"):
            if key in expect and (not isinstance(expect[key], list)
                    or any(not isinstance(item, dict) for item in expect[key])):
                errors.append(f"{label}: {key} must be a list of objects")
        for key in ("out_of_scope", "incomplete"):
            if key in expect and type(expect[key]) is not bool:
                errors.append(f"{label}: {key} must be boolean")
        if "family_id" in expect and (not isinstance(expect["family_id"], str) or not expect["family_id"]):
            errors.append(f"{label}: family_id must be nonempty text")
        for key in ("facts", "unchanged_facts", "extras"):
            for fact in expect.get(key, []) if isinstance(expect.get(key, []), list) else []:
                if not isinstance(fact, dict):
                    continue
                if not all(k in fact for k in ("property_raw", "value", "operator", "polarity", "subject_kind")):
                    errors.append(f"{label}: {key} needs property, typed value, operator, polarity and subject")
                if not isinstance(fact.get("value"), dict) or not fact["value"].get("type"):
                    errors.append(f"{label}: {key} needs a typed value")
                for name in ("x", "lo", "hi"):
                    value = (fact.get("value") or {}).get(name)
                    if value is not None and not isinstance(value, str):
                        errors.append(f"{label}: new numeric labels must be decimal strings")
        for fill in expect.get("forbidden_fills", []) if isinstance(expect.get("forbidden_fills", []), list) else []:
            if isinstance(fill, dict) and not fill.get("property_raw") and not _char_ref(fill.get("characteristic_ref")):
                errors.append(f"{label}: forbidden fill requires property_raw or versioned characteristic_ref")
        for binding in expect.get("bindings", []) if isinstance(expect.get("bindings", []), list) else []:
            if isinstance(binding, dict) and "property_raw" in binding and not _char_ref(binding.get("characteristic_ref")):
                errors.append(f"{label}: new binding requires versioned characteristic_ref")
        for diagnostic in expect.get("diagnostics", []) if isinstance(expect.get("diagnostics", []), list) else []:
            if isinstance(diagnostic, dict) and not isinstance(diagnostic.get("kind"), str):
                errors.append(f"{label}: diagnostic requires kind")
    return errors


def _utc(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0) else None


def _inference_errors(runtime, resource):
    inference = resource.get("inference") or {}
    if not isinstance(inference, dict):
        return ["inference evidence must be an object"]
    errors = []
    for key in ("host_id", "device_id", "linkage_evidence"):
        if not isinstance(inference.get(key), str) or not inference[key].strip():
            errors.append(f"inference {key} is missing")
    if inference.get("endpoint_id") != runtime.get("inference_endpoint_id"):
        errors.append("inference endpoint differs from configured endpoint")
    if inference.get("platform") != "linux" or inference.get("linkage_verified") is not True:
        errors.append("inference Linux host linkage is unverified")
    if not isinstance(resource.get("measurement_scope"), str) or not resource["measurement_scope"].strip():
        errors.append("inference measurement scope is missing")
    kind = inference.get("device_kind")
    if kind == "cpu":
        if inference.get("memory_kind") != "none" or resource.get("peak_vram_gb") != 0:
            errors.append("CPU inference requires no VRAM and measured peak zero")
    elif kind in ("cuda", "rocm"):
        if inference.get("memory_kind") != "dedicated_vram" or inference.get("shared_memory_deduplicated") is not True:
            errors.append("GPU inference requires dedicated VRAM measured without shared-allocation double counting")
    else:
        errors.append("inference device kind is unknown")
    return errors


def _observation_errors(resource, window):
    start, end = _utc(resource.get("observation_start")), _utc(resource.get("observation_end"))
    measured = _utc(resource.get("measured_at"))
    errors = []
    if not start or not end or not measured or start > end or measured < end:
        errors.append("resource observation requires ordered UTC start/end and measured_at after end")
    elif measured > datetime.now(UTC) + timedelta(seconds=5):
        errors.append("resource observation is future dated (clock tolerance is 5 seconds)")
    run_start, run_end = _utc((window or {}).get("start")), _utc((window or {}).get("end"))
    if not run_start or not run_end or run_start > run_end:
        errors.append("evaluated run window is missing or invalid")
    elif start and end and (start > run_start or end < run_end):
        errors.append("resource observation does not cover warmup through the last evaluated request")
    if not isinstance(resource.get("method"), str) or not resource["method"].strip():
        errors.append("resource measurement method is missing")
    peak = resource.get("peak_vram_gb")
    if type(peak) not in (int, float) or not math.isfinite(peak) or peak < 0:
        errors.append("resource measured peak VRAM is missing or invalid")
    return errors


def identity_errors(runtime, resource=None, window=None, require_resources=False):
    """Unknown machine/model identity cannot support acceptance or measured resource claims."""
    errors = []
    manifest = runtime.get("model_manifest") or {}
    if not runtime.get("runtime_id"):
        errors.append("runtime process identity is unverified")
    if runtime.get("platform") != "linux":
        errors.append("acceptance runtime must be Linux")
    if manifest.get("identity_verified") is not True or not manifest.get("revision") or not manifest.get("model"):
        errors.append("model revision identity is unverified")
    endpoint = runtime.get("inference_endpoint_id")
    if not isinstance(endpoint, str) or len(endpoint) != 64 or any(c not in "0123456789abcdef" for c in endpoint):
        errors.append("configured inference endpoint identity is missing or invalid")
    if resource:
        for key, expected in (("runtime_id", runtime.get("runtime_id")), ("platform", runtime.get("platform")),
                              ("snapshot_id", runtime.get("snapshot_id")), ("model_revision", manifest.get("revision"))):
            if not expected or resource.get(key) != expected:
                errors.append(f"resource evidence {key} differs from evaluated runtime")
        errors.extend(_inference_errors(runtime, resource))
        errors.extend(_observation_errors(resource, window))
    elif require_resources:
        errors.append("resource inference host/device/window evidence is missing")
    return errors


def apply_resource_evidence(report, runtime, resource):
    """Call only with evidence reloaded after evaluate; external linkage attestation is a trust boundary."""
    errors = identity_errors(runtime, resource, report.get("evaluation_window"), require_resources=True)
    report["errors"] = [*report.get("errors", []), *errors]
    name = "resources.peak_vram_gb_max"
    metric = {"value": resource["peak_vram_gb"] if not errors else None, "numerator": None,
              "denominator": int(not errors), "population": DEFINITIONS[name]}
    for split in report.get("splits", {}).values():
        for domain in split.get("domains", {}).values():
            domain["metrics"][name] = dict(metric)
    report["resource_evidence_note"] = "Inference host/device linkage is an external attestation; endpoint hashes alone do not prove it."
    if not errors:
        report["resource_evidence"] = {key: resource[key] for key in ("inference", "measurement_scope", "method",
                                                                   "observation_start", "observation_end", "measured_at")}


def _real_state(state, runtime, analyzed=False):
    if state.get("mode") != "real":
        raise ValueError("fake or unknown state cannot support acceptance")
    if runtime and state.get("snapshot_id") != runtime.get("snapshot_id"):
        raise ValueError("response snapshot identity differs from runtime")
    if runtime and runtime.get("contract_version") and state.get("contract_version") != runtime["contract_version"]:
        raise ValueError("response contract version differs from runtime")
    if runtime and analyzed:
        expected = runtime.get("model_manifest") or {}
        manifests = state.get("model_manifests", [])
        if not any(m.get("identity_verified") is True and m.get("revision") == expected.get("revision")
                   and m.get("model") == expected.get("model") and expected.get("revision") for m in manifests):
            raise ValueError("response model revision identity differs from runtime")


def execute_case(case, request, runtime=None):
    """Same parse -> analyze -> explicit-answer transitions for API and in-process adapters."""
    row = {"case": case, "initial": None, "state": None, "history": [], "error": None,
           "parse_ms": None, "analyze_ms": None, "questions": 0}
    try:
        start = time.perf_counter()
        state = request("parse", {"request_id": uuid.uuid4().hex, "expected_revision": 0, "text": case["text"]})
        _real_state(state, runtime)
        row["parse_ms"] = (time.perf_counter() - start) * 1000
        row["initial"] = state
        row["history"] = [state]
        start = time.perf_counter()
        state = request("analyze", {"session_id": state["session_id"], "request_id": uuid.uuid4().hex,
                                    "expected_revision": state["revision"]})
        _real_state(state, runtime, analyzed=True)
        row["analyze_ms"] = (time.perf_counter() - start) * 1000
        row["history"] = [*row["history"], state]
        for answer in case["answers"]:
            question = state.get("clarification")
            if not question:
                raise ValueError("expected answer has no current clarification")
            if "char_name" in answer and question.get("char_name") != answer["char_name"]:
                raise ValueError("answer characteristic differs from current clarification")
            body = {"session_id": state["session_id"], "request_id": uuid.uuid4().hex,
                    "expected_revision": state["revision"], "question_id": question["id"]}
            if "value" in answer:
                body["value"] = answer["value"]
            else:
                options = [option for option in question["options"]
                           if option.get("label_ru") == answer.get("option")]
                if len(options) != 1:
                    raise ValueError("answer does not select exactly one current option")
                body["option_id"] = options[0]["id"]
            state = request("answer", body)
            _real_state(state, runtime, analyzed=True)
            row["questions"] += 1
            row["history"] = [*row["history"], state]
        row["state"] = state
        if state.get("status") != "analyzed":
            row["error"] = "analysis did not complete successfully"
        expect = case["expect"]
        if "readiness" in expect and state.get("readiness") != expect["readiness"]:
            row["error"] = "readiness expectation failed"
        if "questions" in expect and row["questions"] != expect["questions"]:
            row["error"] = "question count expectation failed"
        if "questions_max" in expect and row["questions"] > expect["questions_max"]:
            row["error"] = "maximum question count expectation failed"
        if "clarification" in expect:
            gold_question, actual = expect["clarification"], state.get("clarification")
            if (gold_question is None and actual is not None) or (gold_question is not None
                    and (not actual or any(actual.get(k) != v for k, v in gold_question.items()))):
                row["error"] = "clarification expectation failed"
    except Exception as error:
        # No provider response or credential-bearing transport message is logged.
        row["error"] = f"{type(error).__name__}: case transition failed"
    return row


def _canonical(value):
    from src.ktru.engine.text import OKEI  # noqa: PLC0415
    if not isinstance(value, dict):
        return None
    kind = value.get("type")
    keys = {"number": ("x", "unit_okei"), "range": ("lo", "hi", "unit_okei"), "enum": ("label",),
            "bool": ("v",), "text": ("s",), "unparsed": ("s", "reason")}.get(kind)
    if keys is None:
        return None
    result = {"type": kind, **{key: value.get(key) for key in keys}}
    if kind in ("number", "range") and value.get("unit_okei") not in OKEI:
        unit = value.get("unit_name")
        result["unit_name"] = " ".join(unit.casefold().split()) if isinstance(unit, str) else unit
    if kind == "range":
        result.update(lo_inclusive=value.get("lo_inclusive", True), hi_inclusive=value.get("hi_inclusive", True))
    for key in ("x", "lo", "hi"):
        if result.get(key) is not None:
            try:
                decimal = Decimal(str(result[key]))
                if not decimal.is_finite():
                    return None
                # Decimal equality is exact and context-free; normalize() can round at the current precision.
                result[key] = decimal
            except InvalidOperation:
                return None
    return result


def _matches(gold, assertion, subject=False):
    keys = ("property_raw", "operator", "polarity") + (("subject_kind", "subject_label") if subject else ())
    if any(key in gold and gold[key] != assertion.get(key) for key in keys):
        return False
    if "value" in gold:
        value = _canonical(gold["value"])
        return value is not None and value == _canonical(assertion.get("value"))
    return "value_raw" in gold and gold["value_raw"] == assertion.get("value_raw")


def _supported(assertion, text):
    evidence = assertion.get("evidence", [])
    if assertion.get("span"):
        evidence = [*evidence, {"type": "text", "span": assertion["span"]}]
    for item in evidence:
        if item.get("type") == "text":
            span = item.get("span") or {}
            start, end = span.get("start"), span.get("end")
            if type(start) is int and type(end) is int and 0 <= start < end <= len(text):
                if span.get("text") == text[start:end]:
                    return True
        elif item.get("type") in ("user_answer", "user_edit") and type(item.get("revision")) is int:
            return True
        elif item.get("type") == "catalog" and _ref(item.get("position_ref")):
            return True
    return False


def _rate(numerator, denominator, name, unknown=False):
    return {"value": numerator / denominator if denominator and not unknown else None,
            "numerator": numerator, "denominator": denominator, "population": DEFINITIONS[name]}


def _percentile(values, percentile):
    return sorted(values)[max(0, math.ceil(len(values) * percentile) - 1)] if values else None


def score(rows, resource=None):
    counters = {name: [0, 0] for name in DEFINITIONS}
    unknown, questions = set(), []
    for row in rows:
        expect, state = row["case"]["expect"], row.get("state") or {}
        initial = row.get("initial") or {}
        failed = bool(row.get("error"))
        assertions = [a for a in state.get("assertions", []) if a.get("status", "supported") in ("supported", "ambiguous")]
        assertions = assertions if not failed else []
        final = _ref((state.get("final") or {}).get("position_ref")) if not failed else None
        gold = _ref(expect.get("final"))
        def count(name, correct, denominator=1):
            counters[name] = [counters[name][0] + int(correct), counters[name][1] + denominator]
        if expect.get("family_id"):
            count("retrieval.family_recall_at_3", not failed and expect["family_id"] in
                  [family["id"] for family in initial.get("families_top", [])[:3]])
        if "out_of_scope" in expect:
            predicted = any(d["kind"] == "out_of_scope" for d in state.get("diagnostics", []))
            if predicted:
                count("retrieval.out_of_scope_precision", not failed and expect["out_of_scope"])
            if failed:
                unknown.add("retrieval.out_of_scope_precision")
        if gold:
            count("classification.final_code_accuracy", final == gold)
            count("completion_coverage", final is not None)
            count("wrong_unique_rate", final is not None and final != gold)
            if expect.get("incomplete"):
                count("classification.questions_to_final_p50_max", 0)
                if final == gold:
                    questions.append(row["questions"])
                else:
                    unknown.add("classification.questions_to_final_p50_max")
        ambiguous = (("final" in expect and expect["final"] is None) or expect.get("must_not_finalize")
                     or len(expect.get("alive_refs", [])) > 1)
        if ambiguous:
            count("classification.false_unique_rate_max", final is not None)
            if failed:
                unknown.add("classification.false_unique_rate_max")
        if expect.get("alive_refs"):
            alive = {_ref(c["ref"]) for c in state.get("candidates", []) if c["status"] == "alive"}
            count("classification.alive_superset_recall", not failed and
                  {_ref(ref) for ref in expect["alive_refs"]} <= alive)
        for fact in expect.get("facts", []):
            exact = any(_matches(fact, assertion, subject=True) for assertion in assertions)
            count("fact_recall", exact)
            if "value" in fact:
                count("extraction.value_unit_accuracy", any(_matches(fact, a) for a in assertions))
            if "subject_kind" in fact:
                count("subject_accuracy", exact)
        for binding in expect.get("bindings", []):
            ref = _char_ref(binding.get("characteristic_ref"))
            if ref is None or not binding.get("property_raw"):
                continue  # Legacy smoke char_name labels do not assess versioned binding.
            ids = {a["id"] for a in assertions if a["property_raw"] == binding["property_raw"]}
            count("extraction.binding_accuracy", not failed and any(b["assertion_id"] in ids
                  and _char_ref(b.get("characteristic_ref")) == ref and b.get("subject_match") == "yes"
                  and b.get("relation") == "exact" for b in state.get("bindings", [])))
        for assertion in assertions:
            if assertion.get("status") == "rejected":
                continue
            count("extraction.unsupported_value_rate_max", not _supported(assertion, row["case"]["text"]))
        if failed:
            unknown.add("extraction.unsupported_value_rate_max")
        for fill in expect.get("forbidden_fills", []):
            seen = any(a.get("property_raw") == fill.get("property_raw") for a in assertions) if fill.get("property_raw") else False
            if fill.get("characteristic_ref"):
                seen = any(_char_ref(f.get("characteristic_ref")) == _char_ref(fill["characteristic_ref"])
                           and f.get("value") is not None for f in state.get("fields", []))
            count("extraction.forbidden_fill_rate_max", seen)
            if failed:
                unknown.add("extraction.forbidden_fill_rate_max")
        diagnostics = {d["kind"] for d in state.get("diagnostics", [])} if not failed else set()
        for diagnostic in expect.get("diagnostics", []):
            count("diagnostics.recall", diagnostic.get("kind") in diagnostics)
        if expect.get("diagnostics") == []:
            count("diagnostics.false_alarm_rate_max", bool(diagnostics))
            if failed:
                unknown.add("diagnostics.false_alarm_rate_max")
        for fact in expect.get("unchanged_facts", []):
            history = row.get("history", [])
            changed = False
            for before, after in zip(history, history[1:]):
                active_before = [a for a in before.get("assertions", []) if a.get("status", "supported") in ("supported", "ambiguous")]
                active_after = [a for a in after.get("assertions", []) if a.get("status", "supported") in ("supported", "ambiguous")]
                source_ids = {a["id"] for a in active_before if _matches(fact, a, subject=True)}
                was_present = bool(source_ids)
                still_present = any(_matches(fact, a, subject=True) for a in active_after)
                diagnosed = any(source_ids.intersection(d.get("assertion_ids", [])) for d in after.get("diagnostics", []))
                user_action = any(e.get("type") in ("user_answer", "user_edit")
                                  and e.get("revision", -1) > before.get("revision", -1)
                                  for a in active_after if a.get("property_raw") == fact.get("property_raw")
                                  and a.get("subject_kind") == fact.get("subject_kind")
                                  and a.get("subject_label") == fact.get("subject_label")
                                  for e in a.get("evidence", []))
                changed |= was_present and not still_present and not diagnosed and not user_action
            count("diagnostics.silent_change_rate_max", changed)
            if failed or len(history) < 2 or not any(_matches(fact, a, subject=True)
                    for a in history[0].get("assertions", [])):
                unknown.add("diagnostics.silent_change_rate_max")
        for extra in expect.get("extras", []):
            ids = {a["id"] for a in assertions if _matches(extra, a, subject=True)}
            count("extras_recall", any(f.get("source") in ("portal", "new")
                  and ids.intersection(f.get("assertion_ids", [])) for f in state.get("fields", [])))
    metrics = {name: _rate(*counts, name, unknown=name in unknown) for name, counts in counters.items()}
    name = "classification.questions_to_final_p50_max"
    metrics[name] = {**metrics[name], "value": statistics.median(questions) if questions and name not in unknown else None,
                     "numerator": None, "samples": questions}
    for action in ("parse", "analyze"):
        name = f"latency_ms.{action}_p95"
        values = [row[f"{action}_ms"] for row in rows if row.get(f"{action}_ms") is not None]
        metrics[name] = {"value": _percentile(values, .95) if len(values) == len(rows) else None,
                         "numerator": None, "denominator": len(rows), "population": DEFINITIONS[name]}
    name = "resources.peak_vram_gb_max"
    # A preloaded report cannot prove coverage of requests that have not happened yet.
    # apply_resource_evidence fills this metric after validating reloaded evidence against the run window.
    metrics[name] = {"value": None, "numerator": None, "denominator": 0, "population": DEFINITIONS[name]}
    return metrics


def check_report(report, thresholds):
    failures = list(report.get("errors", []))
    if not report.get("splits"):
        return [*failures, "no required splits present"]
    for split, result in report["splits"].items():
        total_key = f"min_cases.{split}_total"
        if total_key in thresholds and result["total"] < thresholds[total_key]:
            failures.append(f"{split}: cases {result['total']} < {thresholds[total_key]}")
        for domain in DOMAINS:
            if domain not in result.get("domains", {}):
                failures.append(f"{split}/{domain}: missing required domain")
                continue
            data = result["domains"][domain]
            base_key = "min_cases.dev_base_per_domain"
            if split == "dev" and base_key in thresholds and data.get("base_count", 0) < thresholds[base_key]:
                failures.append(f"{split}/{domain}: base cases {data.get('base_count', 0)} < {thresholds[base_key]}")
            for name, target in thresholds.items():
                if name.startswith("min_cases."):
                    if name not in ("min_cases.dev_total", "min_cases.holdout_total", base_key):
                        failures.append(f"unsupported threshold {name}")
                    continue
                metric = data.get("metrics", {}).get(name)
                value = metric and metric.get("value")
                if not metric or not metric.get("denominator") or value is None or not math.isfinite(value):
                    failures.append(f"{split}/{domain}: {name} unassessed or missing")
                elif value > target if name.endswith("_max") or name.startswith("latency_ms.") else value < target:
                    failures.append(f"{split}/{domain}: {name}={value} fails target {target}")
    return failures


def evaluate(cases, request, resource=None, runtime=None):
    """Warm the same adapter once, then retain every labelled row, including failures."""
    errors = validate_cases(cases)
    if errors:
        return {"splits": {}, "errors": errors, "warmup_ok": False, "cases": []}
    started = datetime.now(UTC).isoformat()
    warm = execute_case(cases[0], request, runtime) if cases and not errors else None
    warmup_ok = bool(warm and not warm["error"])
    if not warmup_ok:
        errors.append("warmup did not complete; warm latency is unassessed")
    rows = [execute_case(case, request, runtime) for case in cases] if not errors else []
    if not rows and cases:
        rows = [{"case": c, "state": None, "initial": None, "history": [], "error": "invalid dataset or warmup",
                 "parse_ms": None, "analyze_ms": None, "questions": 0} for c in cases]
    errors += [f"{r['case']['id']}: {r['error']}" for r in rows if r["error"]]
    ended = datetime.now(UTC).isoformat()
    splits = {}
    for split in sorted({c["split"] for c in cases}):
        selected = [r for r in rows if r["case"]["split"] == split]
        domains = {}
        for domain in DOMAINS:
            domain_rows = [r for r in selected if r["case"]["domain"] == domain]
            domains[domain] = {"total": len(domain_rows),
                               "base_count": len({r["case"]["group"] for r in domain_rows}),
                               "metrics": score(domain_rows, resource)}
        splits[split] = {"total": len(selected), "domains": domains}
    return {"splits": splits, "errors": errors, "warmup_ok": warmup_ok,
            "evaluation_window": {"start": started, "end": ended},
            "cases": [{"id": r["case"]["id"], "split": r["case"]["split"], "domain": r["case"]["domain"],
                       "error": r["error"], "parse_ms": r["parse_ms"], "analyze_ms": r["analyze_ms"],
                       "questions": r["questions"], "snapshot_id": (r["state"] or {}).get("snapshot_id")}
                      for r in rows]}
