#!/usr/bin/env python3
"""Evaluation runner (in-process, real snapshot).

  python3 eval/run.py seeds            # 17 labelled smoke cases (dev; the rules were tuned on them — not holdout)
  python3 eval/run.py ste --n 200      # real Portal listings (СТЕ) per domain: behaviour/coverage, NO gold codes

СТЕ have no KTRU gold in the organizers' data, so the ste mode measures coverage and behaviour only
(family found/confirmed, final, readiness, diagnostics, field sources, latency) — never accuracy.
Results are written to $KTRU_BUILD_DIR/eval/ (default .ktru-build/eval/).
"""

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
import urllib.request
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import gate

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import golden  # noqa: E402

from src.ktru import contracts as C  # noqa: E402
from src.ktru.catalog.reader import Catalog, ste_text  # noqa: E402
from src.ktru.engine.text import norm  # noqa: E402
from src.ktru.service.pipeline import Pipeline  # noqa: E402

BUILD = Path(os.environ.get("KTRU_BUILD_DIR", ROOT / ".ktru-build"))
SNAP = Path(os.environ.get("KTRU_SNAPSHOT_DIR", BUILD / "snapshots" / "current"))


def parse(p: Pipeline, text: str) -> tuple[C.CardState, float]:
    t = time.perf_counter()
    st = p.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=text[: C.MAX_TEXT]))
    return st, (time.perf_counter() - t) * 1000


def run_seeds(p: Pipeline) -> dict:
    cases = [json.loads(x) for x in (ROOT / "eval/cases/seed.jsonl").read_text().splitlines() if x.strip()]
    rows = []
    for c in cases:
        st, ms = parse(p, c["text"])
        gold = (c["expect"].get("final") or {}).get("code") or next(iter(c["expect"].get("alive_codes_superset", [])), None)
        family_answers = 0
        if gold and st.clarification and st.clarification.kind == "family":
            family_id = p.catalog.position(gold).family_id
            index = next((i for i, family in enumerate(st.families_top) if family.id == family_id), None)
            if index is not None:
                question = st.clarification
                st = p.answer(C.AnswerRequest(session_id=st.session_id, request_id=uuid.uuid4().hex,
                    expected_revision=st.revision, question_id=question.id, option_id=question.options[index].id))
                family_answers = 1
        for a in c["answers"]:
            q = st.clarification
            opt = q and next((o for o in q.options if norm(o.label_ru) == norm(a["option"])), None)
            if not opt:
                break
            req = C.AnswerRequest(
                session_id=st.session_id,
                request_id=uuid.uuid4().hex,
                expected_revision=st.revision,
                question_id=q.id,
                option_id=opt.id,
            )
            st = p.answer(req)
        e, fails = c["expect"], []
        final = st.final.position_ref.code if st.final else None
        if "final" in e and final != (e["final"]["code"] if e["final"] else None):
            fails.append(f"final={final}")
        if e.get("must_not_finalize") and final:
            fails.append("finalized")
        if "readiness" in e and st.readiness != e["readiness"]:
            fails.append(f"readiness={st.readiness}")
        kinds = {d.kind for d in st.diagnostics}
        fails += [f"no {d['kind']}" for d in e.get("diagnostics", []) if d["kind"] not in kinds]
        rows.append({"id": c["id"], "ok": not fails, "fails": fails, "ms": round(ms, 1), "explicit_gold_family_answers": family_answers})
    return {"mode": "seeds", "passed": sum(r["ok"] for r in rows), "total": len(rows), "cases": rows}


def run_ste(p: Pipeline, n: int) -> dict:
    db = p.catalog._db()
    out: dict = {"mode": "ste", "n_per_domain": n, "note": "behaviour/coverage only: СТЕ have no KTRU gold"}
    out["domains"] = {}
    for domain in ("clothing", "furniture", "tools"):
        rows = db.execute(
            "SELECT ste_id, name, chars FROM ste WHERE domain=? AND chars != '' ORDER BY ste_id", (domain,)
        ).fetchall()
        sample = rows[:: max(1, len(rows) // n)][:n]  # deterministic: every k-th listing
        c: Counter = Counter()
        lat: list[float] = []
        sources: Counter = Counter()
        diags: Counter = Counter()
        examples = []
        for sid, name, chars in sample:
            st, ms = parse(p, ste_text(name, chars))
            lat.append(ms)
            c["family_found"] += st.family is not None
            c["family_confirmed"] += bool(st.family and st.family.confirmed_by_text)
            c["final"] += st.final is not None
            c["suggestion"] += st.suggestion is not None
            c["question"] += st.clarification is not None
            c[f"readiness_{st.readiness}"] += 1
            sources.update(f.source for f in st.fields)
            c["~unparsed"] += sum(f.status == "unparsed" for f in st.fields)
            diags.update(d.kind for d in st.diagnostics)
            if len(examples) < 8:
                examples.append(
                    {
                        "ste_id": sid,
                        "name": name[:80],
                        "family": st.family and st.family.name,
                        "final": st.final and st.final.position_ref.code,
                        "readiness": st.readiness,
                    }
                )
        k = len(sample)
        out["domains"][domain] = {
            "n": k,
            "rates": {key: round(v / k, 3) for key, v in sorted(c.items()) if not key.startswith("~")},
            "fields_by_source": dict(sources),
            "unparsed_fields_per_item": round(c["~unparsed"] / k, 2),
            "diagnostics": dict(diags),
            "latency_ms": {"p50": round(statistics.median(lat), 1), "p95": round(sorted(lat)[int(0.95 * (k - 1))], 1)},
            "examples": examples,
        }
    return out


def run_gate(a) -> dict:
    requested = ("dev", "holdout") if a.split == "all" else (a.split,)
    cases, inputs, errors = [], [], []
    for split in requested:
        directory = ROOT / "eval/cases" if split == "dev" else a.holdout or os.environ.get("KTRU_HOLDOUT_DIR")
        loaded, hashed, failures = gate.load_cases(directory, split)
        cases.extend(loaded)
        inputs.extend(hashed)
        errors.extend(failures)
    errors.extend(gate.validate_cases(cases))
    thresholds = gate.load_thresholds(a.check)
    threshold_raw = Path(a.check).read_bytes()
    inputs.append({"path": str(Path(a.check).resolve()), "sha256": hashlib.sha256(threshold_raw).hexdigest(),
                   "role": "thresholds"})
    runtime = {"transport": "api" if a.api else "in-process", "snapshot_id": None,
               "contract_version": C.CONTRACT_VERSION}
    if errors:
        result = {"splits": {s: {"total": 0, "domains": {}} for s in requested}, "cases": [], "errors": errors,
                  "warmup_ok": False, "mode": "gate", "split": a.split, "inputs": inputs, "runtime": runtime}
        result["failures"] = gate.check_report(result, thresholds)
        result["passed"] = False
        return result
    if a.api:
        url = urlsplit(a.api)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("API URL must be HTTP(S), without credentials, query or fragment")
        base = a.api.rstrip("/")
        if not base.endswith("/api/ktru"):
            base += "/api/ktru"
        def request(action, body):
            req = urllib.request.Request(base + "/" + action,
                                         json.dumps(body).encode() if body is not None else None,
                                         {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=a.timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("API response exceeds size limit")
            data = json.loads(raw)
            model = C.Health if action == "health" else C.CardState
            return model.model_validate(data).model_dump(mode="json")
        health = request("health", None)
        runtime.update(snapshot_id=health["snapshot_id"], contract_version=health["contract_version"], llm=health["llm"],
                       runtime_id=health.get("runtime_id"), platform=health.get("platform"),
                       inference_endpoint_id=health.get("inference_endpoint_id"), model_manifest=health.get("model_manifest"))
        if not health["ready"] or health["contract_version"] != C.CONTRACT_VERSION or health["llm"] != "available":
            errors.append("API runtime is not ready, extraction unavailable, or contract version differs")
    else:
        p = Pipeline(Catalog(SNAP))
        runtime["snapshot_id"] = p.catalog.snapshot_id
        runtime.update(p.runtime_metadata)
        models = {"parse": C.ParseRequest, "analyze": C.AnalyzeRequest, "answer": C.AnswerRequest}
        def request(action, body):
            return getattr(p, action)(models[action].model_validate(body)).model_dump(mode="json")
    if not runtime["snapshot_id"]:
        errors.append("runtime snapshot_id is missing")
    identity_errors = gate.identity_errors(runtime)
    errors.extend(identity_errors)
    result = gate.evaluate(cases, request, runtime=runtime) if not errors else {
        "splits": {}, "cases": [], "errors": [], "warmup_ok": False}
    if result["cases"]:
        ending = request("health", None) if a.api else {**p.runtime_metadata, "snapshot_id": p.catalog.snapshot_id}
        for key in ("runtime_id", "platform", "snapshot_id", "inference_endpoint_id"):
            if ending.get(key) != runtime.get(key):
                errors.append(f"runtime {key} changed during evaluation")
        if (ending.get("model_manifest") or {}) != (runtime.get("model_manifest") or {}):
            errors.append("runtime model manifest changed during evaluation")
    # A measurer can finish its observation during the run. Hash the report actually assessed.
    resource = None
    if a.resource_report:
        raw = Path(a.resource_report).read_bytes()
        resource = json.loads(raw)
        if not isinstance(resource, dict):
            raise ValueError("Resource report must be an object")
        inputs.append({"path": str(Path(a.resource_report).resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
                       "role": "runtime_resources"})
    gate.apply_resource_evidence(result, runtime, resource)
    for split in requested:
        if split not in result["splits"]:
            result["splits"][split] = {"total": 0, "domains": {}}
    result["errors"] = [*errors, *result["errors"]]
    snapshots = {row["snapshot_id"] for row in result["cases"] if row["snapshot_id"]}
    if snapshots and snapshots != {runtime["snapshot_id"]}:
        result["errors"].append("runtime snapshot changed during evaluation")
    result.update(mode="gate", split=a.split, inputs=inputs, runtime=runtime,
                  evidence_note="Dev is validation. Holdout labels are supplied externally; path names do not prove blindness.")
    result["failures"] = gate.check_report(result, thresholds)
    result["passed"] = not result["failures"]
    return result


def run_golden(a) -> dict:
    cases, manifest, errors = golden.load_dataset(a.golden_dir)
    common = {"mode": "golden", "dataset_sha256": manifest.get("case_sha256"), "errors": errors,
              "dataset_manifest_sha256": manifest.get("manifest_sha256"),
              "artifact_sha256": manifest.get("artifact_sha256"), "source_bank_sha256": manifest.get("sources"),
              "exposure": manifest.get("exposure"), "independent_acceptance": False}
    if errors or a.validate_only:
        return {**common, "validation_only": True, "total": len(cases), "passed": not errors,
                "single": manifest.get("single"), "dialogue": manifest.get("dialogue"), "cases": []}
    dialogues = manifest["validated_dialogues"]
    result = golden.evaluate(cases, Pipeline(Catalog(SNAP)), with_model=a.with_model, dialogues=dialogues)
    # This command cannot pass full behavior while any manual clause or fixture remains unassessed.
    return {**result, **common, "passed": result["total"] > 0 and result["full_behavior_assessed"] == result["total"]
            and not any(k != "pass_checks" and v for k, v in result["statuses"].items())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["seeds", "ste", "golden"], nargs="?")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--split", choices=["dev", "holdout", "all"])
    ap.add_argument("--check", metavar="THRESHOLDS")
    ap.add_argument("--holdout", type=Path)
    ap.add_argument("--api", metavar="URL")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--resource-report", type=Path,
                    help="External inference host/device/endpoint identity, peak VRAM and UTC observation covering this run")
    ap.add_argument("--golden-dir", type=Path, default=golden.DATA)
    ap.add_argument("--validate-only", action="store_true", help="Validate frozen Golden Set without product requests")
    ap.add_argument("--with-model", action="store_true", help="Golden Set: analyze after parse using the configured open model")
    a = ap.parse_args()
    if a.mode and (a.split or a.check or a.api or a.holdout or a.resource_report):
        ap.error("seeds/ste are smoke commands; use --split and --check for the gate")
    if not a.mode and (not a.split or not a.check):
        ap.error("provide seeds/ste or both --split and --check")
    if (a.validate_only or a.with_model or a.golden_dir != golden.DATA) and a.mode != "golden":
        ap.error("Golden Set options require golden mode")
    if a.validate_only and a.with_model:
        ap.error("--validate-only does not execute the model")
    if not 0 < a.timeout <= 120:
        ap.error("--timeout must be > 0 and <= 120 seconds")
    if a.mode == "ste" and a.n < 1:
        ap.error("--n must be positive")
    t = time.perf_counter()
    if a.mode == "golden":
        try:
            res = run_golden(a)
        except Exception as error:
            res = {"mode": "golden", "passed": False, "errors": [f"{type(error).__name__}: gold input or runtime unavailable"]}
    elif a.mode:
        p = Pipeline(Catalog(SNAP))
        res = run_seeds(p) if a.mode == "seeds" else run_ste(p, a.n)
        res.update(snapshot_id=p.catalog.snapshot_id)
    else:
        try:
            res = run_gate(a)
        except Exception as error:
            res = {"mode": "gate", "split": a.split, "passed": False,
                   "failures": [f"{type(error).__name__}: gate input or runtime unavailable"]}
    res["run_s"] = round(time.perf_counter() - t, 1)
    (BUILD / "eval").mkdir(parents=True, exist_ok=True)
    path = BUILD / "eval" / f"{a.mode or 'gate-' + a.split}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=2))
    if a.mode == "ste":
        summary = {d: {k: v for k, v in r.items() if k != "examples"} for d, r in res["domains"].items()}
    else:
        summary = {k: v for k, v in res.items() if k != "cases"}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"→ {path}", file=sys.stderr)
    if a.mode == "seeds" and res["passed"] != res["total"]:
        sys.exit(1)
    if a.mode == "golden" and not res["passed"]:
        sys.exit(1)
    if not a.mode and not res["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
