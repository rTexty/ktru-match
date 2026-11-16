"""Fail-closed evaluation checks; synthetic labels are test inputs, not acceptance evidence."""

import importlib.util
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.ktru import contracts as C

ROOT = Path(__file__).resolve().parents[3]
MODULE = Path(os.environ.get("KTRU_EVAL_MODULE", ROOT / "eval/gate.py")).resolve()
spec = importlib.util.spec_from_file_location("ktru_eval_gate", MODULE)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def case(case_id="case", split="dev", **expect):
    return {"id": case_id, "split": split, "domain": "tools", "group": case_id,
            "family_group": case_id, "source": case_id, "text": "Молоток", "answers": [], "expect": expect}


def state(final=None, **updates):
    return {"snapshot_id": "test", "mode": "real", "final": final, "families_top": [], "candidates": [], "assertions": [],
            "bindings": [], "fields": [], "diagnostics": [], **updates}


def row(c, result=None, error=None):
    return {"case": c, "state": result, "initial": result, "error": error,
            "parse_ms": 1, "analyze_ms": 2, "questions": 0, "history": [result] if result else []}


def metric(rows, name):
    return gate.score(rows)[name]


def test_omitted_final_is_not_a_negative_label():
    rows = [row(case(), state(final={"position_ref": {"code": "wrong", "version": 1}}))]
    assert metric(rows, "classification.final_code_accuracy")["denominator"] == 0
    assert metric(rows, "classification.false_unique_rate_max")["denominator"] == 0


def test_null_final_forbids_finalization():
    rows = [row(case(final=None), state(final={"position_ref": {"code": "wrong", "version": 1}}))]
    assert metric(rows, "classification.false_unique_rate_max")["value"] == 1


def test_final_requires_exact_code_and_version_and_counts_failures():
    gold = {"code": "hammer", "version": 2}
    rows = [row(case("a", final=gold), state(final={"position_ref": {**gold, "version": 1}})),
            row(case("b", final=gold), error="timeout")]
    result = metric(rows, "classification.final_code_accuracy")
    assert result["denominator"] == 2
    assert result["value"] == 0


def test_empty_denominator_cannot_pass_even_a_zero_maximum():
    report = {"splits": {"dev": {"total": 1, "domains": {"tools": {"metrics": gate.score([])}}}}}
    failures = gate.check_report(report, {"extraction.forbidden_fill_rate_max": 0})
    assert failures and any("unassessed" in x for x in failures)


def test_all_checks_splits_separately():
    report = {"splits": {"dev": {"total": 1, "domains": {"tools": {"metrics": {
        "classification.final_code_accuracy": {"value": 1, "denominator": 1}}}}},
        "holdout": {"total": 1, "domains": {"tools": {"metrics": {
            "classification.final_code_accuracy": {"value": 0, "denominator": 1}}}}}}}
    failures = gate.check_report(report, {"classification.final_code_accuracy": .92})
    assert any("holdout/tools" in x for x in failures)


@pytest.mark.parametrize("key", ["group", "family_group", "source", "source_hash"])
def test_linked_groups_cannot_cross_splits(key):
    left, right = case("a"), case("b", split="holdout")
    left[key] = right[key] = "shared"
    assert any(key in x for x in gate.validate_cases([left, right]))


def test_missing_new_case_metadata_fails():
    c = case()
    del c["family_group"]
    assert any("family_group" in x for x in gate.validate_cases([c]))


def test_alive_recall_uses_versioned_refs_and_failure_denominator():
    c = case(final=None, alive_refs=[{"code": "hammer", "version": 2}])
    result = state(candidates=[{"ref": {"code": "hammer", "version": 1}, "status": "alive"}])
    assert metric([row(c, result)], "classification.alive_superset_recall")["value"] == 0
    assert metric([row(c, error="timeout")], "classification.alive_superset_recall")["denominator"] == 1


def test_resource_measurement_is_absent_without_runtime_evidence():
    result = gate.score([row(case(), state())])
    assert result["resources.peak_vram_gb_max"]["value"] is None


def test_threshold_file_is_read_without_a_yaml_dependency():
    thresholds = gate.load_thresholds(ROOT / "eval/thresholds.yaml")
    assert thresholds["min_cases.dev_total"] == 240
    assert thresholds["classification.final_code_accuracy"] == .92


def test_unknown_threshold_is_a_failure():
    report = {"splits": {"dev": {"total": 1, "domains": {"tools": {"metrics": {}}}}}}
    assert gate.check_report(report, {"custom.not_a_metric": .9})


def test_input_files_have_content_hashes(tmp_path):
    p = tmp_path / "cases.jsonl"
    p.write_text(json.dumps(case()) + "\n")
    cases, inputs, errors = gate.load_cases(tmp_path, "dev")
    assert len(cases) == 1 and not errors
    assert len(inputs[0]["sha256"]) == 64


def test_missing_case_file_fails_closed(tmp_path):
    cases, inputs, errors = gate.load_cases(tmp_path, "holdout")
    assert not cases and not inputs and errors


def test_timeout_cannot_improve_error_rate_denominator():
    rows = [row(case(final=None), error="timeout")]
    result = metric(rows, "classification.false_unique_rate_max")
    assert result["denominator"] == 1 and result["value"] is None


def test_diagnostics_failures_remain_in_recall_denominator():
    rows = [row(case(diagnostics=[{"kind": "contradiction"}]), error="timeout")]
    result = metric(rows, "diagnostics.recall")
    assert result["denominator"] == 1 and result["value"] == 0


def test_no_gold_family_answer_is_inferred():
    calls = []
    def request(action, body):
        calls.append(action)
        return state(session_id="test", revision=len(calls), status="analyzed", clarification={
            "id": "question", "kind": "family", "options": [{"id": "one", "label_ru": "Молоток"}]})
    result = gate.execute_case(case(final={"code": "hammer", "version": 1}), request)
    assert calls == ["parse", "analyze"] and result["questions"] == 0


def test_invalid_expectation_cannot_create_a_passing_denominator():
    c = case(forbidden_fills=[{}])
    assert any("forbidden fill" in x for x in gate.validate_cases([c]))


def typed_fact():
    return {"property_raw": "Масса", "value": {"type": "number", "x": "500", "unit_okei": "163", "unit_name": "г"},
            "operator": "eq", "polarity": "positive", "subject_kind": "item"}


def test_quality_metrics_use_grounded_facts_and_versioned_bindings():
    fact = typed_fact()
    text = "Молоток, 500 г"
    ref = {"position_code": "hammer", "position_version": 1, "char_code": "mass"}
    assertion = {**fact, "id": "mass", "status": "supported", "evidence": [{"type": "text", "span": {
        "start": text.index("500"), "end": len(text), "text": "500 г"}}]}
    gold = {"code": "hammer", "version": 1}
    c = {**case(final=gold, family_id="hammer", incomplete=True, diagnostics=[], facts=[fact],
                bindings=[{"property_raw": "Масса", "characteristic_ref": ref}], extras=[fact],
                forbidden_fills=[{"property_raw": "Пол"}, {"characteristic_ref": {**ref, "char_code": "gender"}}],
                unchanged_facts=[fact]), "text": text}
    result = state(final={"position_ref": gold}, assertions=[assertion],
                   families_top=[{"id": "hammer"}], bindings=[{"assertion_id": "mass", "characteristic_ref": ref,
                                                              "subject_match": "yes", "relation": "exact"}],
                   fields=[{"source": "new", "assertion_ids": ["mass"]}])
    r = {**row(c, result), "questions": 2, "history": [result, result]}
    resource = {"peak_vram_gb": 4, "runtime_id": "fixture", "measured_at": "test", "method": "fixture"}
    values = gate.score([r], resource)
    for name in ("retrieval.family_recall_at_3", "classification.final_code_accuracy", "completion_coverage",
                 "extraction.binding_accuracy", "extraction.value_unit_accuracy", "fact_recall", "subject_accuracy",
                 "extras_recall"):
        assert values[name]["value"] == 1, name
    for name in ("wrong_unique_rate", "extraction.unsupported_value_rate_max", "extraction.forbidden_fill_rate_max",
                 "diagnostics.false_alarm_rate_max", "diagnostics.silent_change_rate_max"):
        assert values[name]["value"] == 0, name
    assert values["classification.questions_to_final_p50_max"]["value"] == 2
    assert values["resources.peak_vram_gb_max"]["value"] is None  # Raw evidence is assessed only after the timed run.


def test_wrong_fact_operator_subject_unit_and_binding_do_not_receive_credit():
    fact = typed_fact()
    ref = {"position_code": "hammer", "position_version": 1, "char_code": "mass"}
    c = case(facts=[fact], bindings=[{"property_raw": "Масса", "characteristic_ref": ref}],
             forbidden_fills=[{"property_raw": "Масса"}])
    wrong = {**fact, "id": "mass", "operator": "ge", "subject_kind": "package",
             "value": {**fact["value"], "unit_okei": "166"}}
    result = state(assertions=[wrong], bindings=[{"assertion_id": "mass",
                   "characteristic_ref": {**ref, "position_version": 2}, "subject_match": "yes", "relation": "exact"}])
    values = gate.score([row(c, result)])
    for name in ("extraction.value_unit_accuracy", "extraction.binding_accuracy", "fact_recall", "subject_accuracy"):
        assert values[name]["value"] == 0
    assert values["extraction.forbidden_fill_rate_max"]["value"] == 1
    assert values["extraction.unsupported_value_rate_max"]["value"] == 1


def test_scope_precision_counts_only_assessed_predictions():
    rows = [row(case("a", out_of_scope=True), state(diagnostics=[{"kind": "out_of_scope"}])),
            row(case("b", out_of_scope=False), state(diagnostics=[{"kind": "out_of_scope"}])),
            row(case("c", out_of_scope=True), state())]
    result = metric(rows, "retrieval.out_of_scope_precision")
    assert result["numerator"] == 1 and result["denominator"] == 2 and result["value"] == .5


def test_silent_change_detects_missing_fact_and_accepts_explicit_diagnostic():
    fact = typed_fact()
    c = case(unchanged_facts=[fact])
    before = state(assertions=[{**fact, "id": "a"}])
    after = state()
    r = {**row(c, after), "history": [before, after]}
    assert metric([r], "diagnostics.silent_change_rate_max")["value"] == 1
    explained = state(diagnostics=[{"kind": "unit_suspect", "assertion_ids": ["a"]}])
    assert metric([{**r, "history": [before, explained]}], "diagnostics.silent_change_rate_max")["value"] == 0


def test_equivalent_decimal_labels_match_and_invalid_decimal_does_not():
    left, right = typed_fact(), typed_fact()
    left["value"] = {**left["value"], "x": "500.00"}
    assert gate._matches(left, right)
    left["value"] = {**left["value"], "x": "invalid"}
    assert not gate._matches(left, right)


def test_user_and_catalog_evidence_and_bad_span_grounding():
    assert gate._supported({"evidence": [{"type": "user_answer", "revision": 2}]}, "")
    assert gate._supported({"evidence": [{"type": "catalog", "position_ref": {"code": "hammer", "version": 1}}]}, "")
    assert not gate._supported({"span": {"start": 0, "end": 6, "text": "wrong"}}, "hammer")


def test_runner_answers_only_live_question_options_and_typed_values():
    actions = []
    def request(action, body):
        actions.append((action, body))
        return state(session_id="test", revision=len(actions), status="analyzed", clarification={
            "id": "live-question", "options": [{"id": "live-option", "label_ru": "Да"}]})
    c = {**case(), "answers": [{"option": "Да"}, {"value": {"type": "number", "x": "500"}}]}
    result = gate.execute_case(c, request)
    assert result["error"] is None and result["questions"] == 2
    assert actions[2][1]["question_id"] == "live-question" and actions[2][1]["option_id"] == "live-option"
    assert actions[3][1]["value"] == {"type": "number", "x": "500"}


@pytest.mark.parametrize("options", [[], [{"id": "a", "label_ru": "Да"}, {"id": "b", "label_ru": "Да"}]])
def test_missing_or_duplicate_answer_option_records_failure(options):
    def request(action, body):
        return state(session_id="test", revision=1, status="analyzed", clarification={"id": "q", "options": options})
    c = {**case(), "answers": [{"option": "Да"}]}
    assert gate.execute_case(c, request)["error"]


def test_evaluate_warms_once_keeps_failure_rows_and_domain_bases():
    calls = []
    def request(action, body):
        calls.append(action)
        return state(session_id="test", revision=1, status="analyzed")
    result = gate.evaluate([case(final={"code": "hammer", "version": 1}, incomplete=True)], request)
    assert calls == ["parse", "analyze", "parse", "analyze"]
    assert result["warmup_ok"] and result["splits"]["dev"]["domains"]["tools"]["base_count"] == 1
    metric = result["splits"]["dev"]["domains"]["tools"]["metrics"]["classification.questions_to_final_p50_max"]
    assert metric["value"] is None and metric["denominator"] == 1
    def fail(action, body):
        raise TimeoutError()
    failed = gate.evaluate([case(final={"code": "hammer", "version": 1})], fail)
    assert failed["errors"] and failed["cases"][0]["error"]
    assert failed["splits"]["dev"]["domains"]["tools"]["metrics"]["classification.final_code_accuracy"]["denominator"] == 1


def test_invalid_dataset_stops_before_requests():
    c = case()
    del c["source"]
    def request(action, body):
        raise AssertionError("must not send")
    result = gate.evaluate([c], request)
    assert result["errors"] and not result["cases"]


def test_corrupt_case_lines_report_hash_and_errors(tmp_path):
    (tmp_path / "cases.jsonl").write_text("not-json\n[]\n" + json.dumps(case(split="holdout")) + "\n")
    rows, inputs, errors = gate.load_cases(tmp_path, "dev")
    assert not rows and inputs and len(errors) >= 3


def test_case_minimums_are_checked_per_domain_and_split():
    report = {"splits": {"dev": {"total": 1, "domains": {"tools": {"base_count": 1, "metrics": {}}}}}}
    failures = gate.check_report(report, {"min_cases.dev_total": 240, "min_cases.dev_base_per_domain": 20})
    assert any("cases 1" in x for x in failures) and any("base cases 1" in x for x in failures)


def runtime_identity():
    return {"runtime_id": "fixture-process", "platform": "linux", "snapshot_id": "test",
            "inference_endpoint_id": "a" * 64, "model_manifest": {
        "model": "fixture-open", "revision": "verified-digest", "identity_verified": True,
        "prompt_sha256": "fixture-prompt"}}


def test_fake_state_is_never_acceptance():
    def request(action, body):
        return state(mode="fake", session_id="test", revision=1, status="analyzed")
    assert gate.execute_case(case(), request)["error"]


def test_response_model_identity_must_match_runtime():
    def request(action, body):
        return state(session_id="test", revision=1, status="analyzed", model_manifests=[])
    assert gate.execute_case(case(), request, runtime_identity())["error"]


@pytest.mark.parametrize("key", ["runtime_id", "platform", "snapshot_id", "model_revision"])
def test_resource_report_must_match_actual_process_and_model(key):
    runtime = runtime_identity()
    window, resource = resource_proof()
    assert not gate.identity_errors(runtime, resource, window)
    resource[key] = "other"
    assert any(key in x for x in gate.identity_errors(runtime, resource, window))


def test_unverified_model_and_non_linux_runtime_cannot_pass():
    runtime = runtime_identity()
    runtime["platform"] = "darwin"
    runtime["model_manifest"] = {**runtime["model_manifest"], "identity_verified": False}
    assert len(gate.identity_errors(runtime)) == 2


def resource_proof():
    now = datetime.now(UTC)
    window = {"start": (now - timedelta(seconds=3)).isoformat(), "end": (now - timedelta(seconds=1)).isoformat()}
    evidence = {"runtime_id": "fixture-process", "platform": "linux", "snapshot_id": "test",
        "model_revision": "verified-digest", "peak_vram_gb": 4, "method": "fixture-observer",
        "measurement_scope": "all inference allocations on fixture GPU, counted once",
        "observation_start": (now - timedelta(seconds=4)).isoformat(), "observation_end": now.isoformat(),
        "measured_at": now.isoformat(), "inference": {"endpoint_id": "a" * 64, "host_id": "fixture-host",
            "platform": "linux", "device_kind": "cuda", "device_id": "GPU-fixture",
            "memory_kind": "dedicated_vram", "linkage_verified": True,
            "linkage_evidence": "fixture-observer host/process/device binding", "shared_memory_deduplicated": True}}
    return window, evidence


def test_linux_api_does_not_prove_linux_inference_host():
    window, evidence = resource_proof()
    evidence["inference"] = {**evidence["inference"], "platform": "darwin"}
    assert any("inference" in e for e in gate.identity_errors(runtime_identity(), evidence, window))


@pytest.mark.parametrize("key", ["host_id", "device_id", "memory_kind", "linkage_evidence", "linkage_verified",
                                  "shared_memory_deduplicated"])
def test_missing_host_device_linkage_cannot_pass(key):
    window, evidence = resource_proof()
    del evidence["inference"][key]
    assert gate.identity_errors(runtime_identity(), evidence, window)


def test_configured_inference_endpoint_is_linked_to_measurement():
    window, evidence = resource_proof()
    evidence["inference"] = {**evidence["inference"], "endpoint_id": "b" * 64}
    assert any("endpoint" in e for e in gate.identity_errors(runtime_identity(), evidence, window))


def test_measurement_must_cover_full_run_and_not_be_future_dated():
    window, evidence = resource_proof()
    evidence["observation_end"] = window["start"]
    assert gate.identity_errors(runtime_identity(), evidence, window)
    _, evidence = resource_proof()
    evidence["measured_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert gate.identity_errors(runtime_identity(), evidence, window)
    _, evidence = resource_proof()
    evidence["observation_start"] = "2026-10-10T00:00:00"  # UTC is required, not local naive time.
    assert gate.identity_errors(runtime_identity(), evidence, window)


def test_verified_observation_applies_resource_metric_after_run():
    window, evidence = resource_proof()
    report = {"evaluation_window": window, "errors": [], "splits": {"dev": {"domains": {
        "tools": {"metrics": gate.score([])}}}}}
    gate.apply_resource_evidence(report, runtime_identity(), evidence)
    assert not report["errors"]
    assert report["splits"]["dev"]["domains"]["tools"]["metrics"]["resources.peak_vram_gb_max"]["value"] == 4
    assert "attestation" in report["resource_evidence_note"]


def test_cpu_proof_requires_zero_vram_and_declared_device():
    window, evidence = resource_proof()
    evidence["inference"] = {**evidence["inference"], "device_kind": "cpu", "memory_kind": "none"}
    assert gate.identity_errors(runtime_identity(), evidence, window)
    evidence["peak_vram_gb"] = 0
    assert not gate.identity_errors(runtime_identity(), evidence, window)


@pytest.mark.parametrize("unit_code", [None, "unknown"])
def test_unknown_unit_names_are_not_collapsed(unit_code):
    gold = {"property_raw": "Length", "value": {"type": "number", "x": "1", "unit_okei": unit_code, "unit_name": "ft"}}
    actual = {**gold, "value": {**gold["value"], "unit_name": "yd"}}
    assert not gate._matches(gold, actual)


def test_exact_decimal_comparison_does_not_round_at_28_digits():
    gold = {"property_raw": "Length", "value": {"type": "number", "x": "1.00000000000000000000000000001"}}
    actual = {**gold, "value": {**gold["value"], "x": "1.00000000000000000000000000002"}}
    assert not gate._matches(gold, actual)
    actual["value"] = {**actual["value"], "x": "1.0000000000000000000000000000100"}
    assert gate._matches(gold, actual)


def test_rejected_original_fact_cannot_score_as_active_recovery():
    gold = typed_fact()
    c = case(facts=[gold], bindings=[{"property_raw": "Масса", "characteristic_ref": {
        "position_code": "hammer", "position_version": 1, "char_code": "mass"}}])
    rejected = {**gold, "id": "original", "status": "rejected"}
    active = {**gold, "id": "edited", "status": "supported", "value": {**gold["value"], "x": "600"}}
    result = state(assertions=[rejected, active], bindings=[{"assertion_id": "original", "subject_match": "yes",
        "relation": "exact", "characteristic_ref": c["expect"]["bindings"][0]["characteristic_ref"]}])
    values = gate.score([row(c, result)])
    assert values["extraction.value_unit_accuracy"]["value"] == 0
    assert values["fact_recall"]["value"] == 0
    assert values["extraction.binding_accuracy"]["value"] == 0


def test_unrelated_diagnostic_does_not_hide_silent_change():
    fact = typed_fact()
    c = case(unchanged_facts=[fact])
    before = state(assertions=[{**fact, "id": "mass", "status": "supported"}])
    after = state(diagnostics=[{"kind": "typo", "assertion_ids": ["unrelated"]}])
    assert metric([{**row(c, after), "history": [before, after]}], "diagnostics.silent_change_rate_max")["value"] == 1


def test_canonical_units_and_default_range_bounds_are_semantic():
    gold = {"property_raw": "Масса", "value": {"type": "number", "x": "500", "unit_okei": "163"}}
    actual = {**gold, "value": {**gold["value"], "unit_name": "грамм"}}
    assert gate._matches(gold, actual)
    gold["value"] = {"type": "range", "lo": "400", "hi": "600", "unit_okei": "163"}
    actual["value"] = {**gold["value"], "lo_inclusive": True, "hi_inclusive": True, "unit_name": "г"}
    assert gate._matches(gold, actual)
    actual["value"] = {**actual["value"], "hi_inclusive": False}
    assert not gate._matches(gold, actual)


def test_readiness_and_question_labels_are_checked():
    def request(action, body):
        return state(session_id="test", revision=1, status="analyzed", readiness="ready")
    assert gate.execute_case(case(readiness="needs_answers"), request)["error"]
    assert gate.execute_case(case(questions=1), request)["error"]
    assert gate.execute_case(case(clarification={"kind": "family"}), request)["error"]
    assert gate.validate_cases([case(unimplemented_label=True)])


def test_answer_does_not_apply_to_another_characteristic():
    def request(action, body):
        return state(session_id="test", revision=1, status="analyzed", clarification={
            "id": "q", "char_name": "Спинка", "options": [{"id": "yes", "label_ru": "Да"}]})
    c = {**case(), "answers": [{"char_name": "Сиденье", "option": "Да"}]}
    assert gate.execute_case(c, request)["error"]


def test_ambiguous_versioned_alive_gold_checks_false_unique():
    c = case(alive_refs=[{"code": "one", "version": 1}, {"code": "two", "version": 1}])
    result = state(final={"position_ref": {"code": "one", "version": 1}})
    assert metric([row(c, result)], "classification.false_unique_rate_max")["value"] == 1


@pytest.mark.parametrize("content", ["bad\n", "a:\n  b: nan\n", "a:\n  b: 1\n  b: 2\n"])
def test_invalid_threshold_file_fails_closed(tmp_path, content):
    path = tmp_path / "thresholds.yaml"
    path.write_text(content)
    with pytest.raises(ValueError):
        gate.load_thresholds(path)


def test_api_gate_exercises_transitions_and_checks_each_split(tmp_path):
    calls = []
    resource_path = tmp_path / "fixture-resource.json"
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append("health")
            _, observation = resource_proof()
            resource_path.write_text(json.dumps(observation))
            self.respond(C.Health(contract_version=C.CONTRACT_VERSION, snapshot_id="test", snapshot_date=None, ready=True,
                                  llm="available", runtime_id="fixture-process", platform="linux",
                                  inference_endpoint_id="a" * 64,
                                  model_manifest=C.ModelManifest(**runtime_identity()["model_manifest"])).model_dump(mode="json"))

        def do_POST(self):
            action = self.path.rsplit("/", 1)[-1]
            calls.append(action)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.respond(C.CardState(session_id="test", revision=1 if action == "parse" else 2,
                                     request_id=body["request_id"], snapshot_id="test", text="Молоток",
                                     readiness="ready", status="provisional" if action == "parse" else "analyzed",
                                     model_manifests=[] if action == "parse" else [C.ModelManifest(**runtime_identity()["model_manifest"])]
                                     ).model_dump(mode="json"))

        def respond(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    (tmp_path / "backend").symlink_to(ROOT / "backend", target_is_directory=True)
    (tmp_path / "eval/cases").mkdir(parents=True)
    (tmp_path / "holdout").mkdir()
    for split, directory in (("dev", tmp_path / "eval/cases"), ("holdout", tmp_path / "holdout")):
        rows = [{**case(f"{split}-{domain}", split, diagnostics=[]), "domain": domain} for domain in gate.DOMAINS]
        (directory / "cases.jsonl").write_text("".join(json.dumps(c) + "\n" for c in rows))
    threshold = tmp_path / "thresholds.yaml"
    threshold.write_text("diagnostics:\n  false_alarm_rate_max: 0\n")
    runner = Path(os.environ.get("KTRU_EVAL_RUNNER", ROOT / "eval/run.py")).resolve()
    script = (f"import sys; from pathlib import Path; sys.path.insert(0, {str(MODULE.parent)!r}); "
              f"exec(compile(Path({str(runner)!r}).read_text(), 'gate-cli', 'exec'), "
              f"{{'__file__': {str(tmp_path / 'eval/run.py')!r}, '__name__': '__main__'}})")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([sys.executable, "-c", script, "--split", "all", "--check", str(threshold),
                                 "--holdout", str(tmp_path / "holdout"), "--api",
                                 f"http://127.0.0.1:{server.server_port}", "--timeout", "2",
                                 "--resource-report", str(resource_path)],
                                capture_output=True, text=True, timeout=10, cwd=tmp_path, check=False)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert set(report["splits"]) == {"dev", "holdout"}
    assert report["runtime"]["snapshot_id"] == "test"
    assert report["splits"]["dev"]["total"] == report["splits"]["holdout"]["total"] == 3
    assert calls == ["health", *[action for _ in range(7) for action in ("parse", "analyze")], "health"]
