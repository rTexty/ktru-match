#!/usr/bin/env python3
"""Replay recorded model facts through the current pipeline and grade them on the Golden Set.

Offline, deterministic, free: no model call. Model outputs come from a previous live run
(`case-results.jsonl` with `provider_attempts[].facts`). Grounding, rules, retrieval, resolve and the
grader are the current code. A replay measures changes after the model; prompt changes need a live run.

  uv run --project backend python eval/replay.py .ktru-build/deepseek-golden/case-results.jsonl
  uv run --project backend python eval/replay.py RUN.jsonl --compare .ktru-build/quality/replay-PREV.jsonl
"""

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "eval")]

import golden  # noqa: E402

from src.ktru.catalog.reader import Catalog  # noqa: E402
from src.ktru.service.pipeline import Pipeline  # noqa: E402
from src.ktru.service.stages import OpenModel  # noqa: E402

BUILD = Path(os.environ.get("KTRU_BUILD_DIR", ROOT / ".ktru-build"))


def snapshot_for(recorded: Path) -> Path:
    """KTRU_SNAPSHOT_DIR, else the local snapshot the recorded run used (same id), else snapshots/current."""
    if os.environ.get("KTRU_SNAPSHOT_DIR"):
        return Path(os.environ["KTRU_SNAPSHOT_DIR"])
    for line in recorded.read_text().splitlines():
        snapshot_id = (json.loads(line).get("state") or {}).get("snapshot_id")
        if snapshot_id:
            found = sorted(BUILD.glob(f"snapshots*/snapshot-{snapshot_id}"))
            if found:
                return found[0]
            break
    return BUILD / "snapshots" / "current"


class Recorded:
    """Text → recorded facts. An incomplete or missing recording fails like the live provider did."""

    def __init__(self, paths: list[Path]):
        self.facts: dict[str, list[dict] | None] = {}
        lines = [line for path in paths for line in path.read_text().splitlines()]
        for line in lines:
            row = json.loads(line)
            for attempt in row.get("provider_attempts") or []:
                done = attempt.get("complete") and attempt.get("finish_reason") == "stop"
                if done or attempt["text_sha256"] not in self.facts:  # a later complete answer wins
                    self.facts[attempt["text_sha256"]] = attempt.get("facts") if done else None

    def __call__(self, text: str) -> list[dict]:
        facts = self.facts.get(hashlib.sha256(text.encode()).hexdigest())
        if facts is None:
            raise RuntimeError("no complete recorded model response")
        return facts


def _dump(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else str(value)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("recorded", type=Path, nargs="+", help="case-results.jsonl of live model runs (later files fill gaps)")
    ap.add_argument("--compare", type=Path, help="previous replay jsonl to diff against")
    ap.add_argument("--only", nargs="*", help="case ids or section prefixes (e.g. SUBJ NUM-001)")
    a = ap.parse_args()
    cases, manifest, errors = golden.load_dataset()
    if errors:
        sys.exit(f"invalid Golden Set: {errors[:3]}")
    if a.only:
        cases = [c for c in cases if any(c["case_id"] == o or c["case_id"].startswith(o + "-") for o in a.only)]
    pipeline = Pipeline(Catalog(snapshot_for(a.recorded[0])), extractor=OpenModel(Recorded(a.recorded)))
    started = time.perf_counter()
    result = golden.evaluate(cases, pipeline, with_model=True, dialogues=manifest["validated_dialogues"])
    rows = result["cases"]
    out = BUILD / "quality" / f"replay-{time.strftime('%Y%m%dT%H%M%S')}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False, default=_dump) + "\n" for r in rows))
    by_id = {c["case_id"]: c for c in cases}
    # Second score: the same states graded with the documented equivalences E1–E3 (eval/golden.py).
    equivalent = Counter()
    for r in rows:
        if r.get("status") in ("pass_checks", "fail_checks") and r.get("stage") == "parse+analyze":
            r["status_equivalent"] = golden.grade(by_id[r["case_id"]], r["state"], equivalent=True)["status"]
        else:
            r["status_equivalent"] = r.get("status")
        equivalent[r["status_equivalent"]] += 1
    result["statuses_equivalent"] = dict(equivalent)
    failures = Counter(f.split(":")[0].split("[")[0] for r in rows for f in r.get("failures", []))
    print(json.dumps({"snapshot": pipeline.catalog.snapshot_id, "total": len(rows), "statuses": result["statuses"], "statuses_equivalent": result["statuses_equivalent"],
                      "by_section": {k: v["statuses"] for k, v in result["by_section"].items()},
                      "failure_kinds": dict(failures.most_common()), "run_s": round(time.perf_counter() - started, 1)},
                     ensure_ascii=False, indent=1))
    if a.compare:
        before = {json.loads(line)["case_id"]: json.loads(line) for line in a.compare.read_text().splitlines()}
        gained = [r["case_id"] for r in rows if r["status"] == "pass_checks"
                  and before.get(r["case_id"], {}).get("status") != "pass_checks"]
        lost = [r["case_id"] for r in rows if r["status"] != "pass_checks"
                and before.get(r["case_id"], {}).get("status") == "pass_checks"]
        print("gained:", gained)
        print("lost:", lost)
    print(f"→ {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
