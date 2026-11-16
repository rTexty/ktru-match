"""Compare family recall and first/uncached/cached search latency on the existing development gold."""

import argparse
import hashlib
import json
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ktru.catalog.reader import Catalog  # noqa: E402
from src.ktru.service.stages import configured_retriever  # noqa: E402


def latency(values):
    ordered = sorted(values)
    return {"samples": len(values), "p50_ms": statistics.median(ordered),
            "p95_ms": ordered[int(.95 * (len(ordered) - 1))], "p99_ms": ordered[int(.99 * (len(ordered) - 1))]}


def main():
    root = Path(__file__).resolve().parents[2]
    fixtures = root / "research-library/14-s0-combined/2026-10-10"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=fixtures / "retrieval-gold.json")
    parser.add_argument("--texts", type=Path, default=fixtures / "selected.json")
    parser.add_argument("--snapshot", type=Path, default=root / ".ktru-build/snapshots/current")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path, default=root / ".ktru-build/vector/benchmark.json")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    texts = {r["case_id"]: r["text"] for r in json.loads(args.texts.read_text())}
    cases = [dict(g, text=texts[g["case_id"]]) for g in json.loads(args.gold.read_text()) if g["eligible"]]
    if not cases:
        parser.error("No eligible family-recall cases")
    started = time.perf_counter()
    catalog = Catalog(args.snapshot)
    retriever = configured_retriever(catalog)
    startup = (time.perf_counter() - started) * 1000
    rows = []
    for case in cases:
        started = time.perf_counter()
        result = retriever.retrieve(case["text"], catalog.snapshot_id)
        rows.append({"case_id": case["case_id"], "acceptable_family_ids": case["acceptable_family_ids"],
                     "families": [f.family_id for f in result.families], "names": [f.name for f in result.families],
                     "first_ms": (time.perf_counter() - started) * 1000, "uncached_ms": [], "cached_ms": []})
    for case, row in zip(cases, rows, strict=True):
        for _ in range(args.repeats):
            retriever._cache.clear()  # Deliberately measure inference/SQL separately from result-cache hits.
            started = time.perf_counter()
            retriever.retrieve(case["text"], catalog.snapshot_id)
            row["uncached_ms"].append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            retriever.retrieve(case["text"], catalog.snapshot_id)
            row["cached_ms"].append((time.perf_counter() - started) * 1000)
    metrics = {"cases": len(rows), "startup_ms": startup,
               "recall": {str(k): sum(bool(set(row["acceptable_family_ids"]) & set(row["families"][:k]))
                                      for row in rows) / len(rows) for k in (1, 3, 5, 8)},
               "first_query": latency([row["first_ms"] for row in rows]),
               "uncached_query": latency([v for row in rows for v in row["uncached_ms"]]),
               "cached_query": latency([v for row in rows for v in row["cached_ms"]])}
    report = {"snapshot_id": catalog.snapshot_id, "retriever": type(retriever).__name__,
              "platform": platform.platform(), "python": platform.python_version(),
              "evaluation": "Existing development gold, reused for tuning; family-class recall only, not final-code accuracy or holdout",
              "fixture_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (args.gold, args.texts)},
              "metrics": metrics, "rows": rows}
    if args.baseline:
        baseline = json.loads(args.baseline.read_text())
        if baseline["snapshot_id"] != catalog.snapshot_id or {r["case_id"] for r in baseline["rows"]} != {r["case_id"] for r in rows}:
            raise ValueError("Baseline snapshot or cases do not match")
        report["baseline"] = baseline["metrics"]
        report["baseline_sha256"] = hashlib.sha256(args.baseline.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(metrics, indent=2))
    print(args.output)


if __name__ == "__main__":
    main()
