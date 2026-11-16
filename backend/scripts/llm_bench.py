"""Check a configured open-model endpoint: reachability, valid facts and latency at a given concurrency.

Uses the same transport as the API (KTRU_LLM_URL, KTRU_LLM_MODEL, optional KTRU_LLM_API_KEY). The key and the
endpoint address are never printed. Example:
    uv run python scripts/llm_bench.py --n 64 --concurrency 16
"""

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ktru.ml import extract as ml  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def texts(n: int) -> list[str]:
    rows = [json.loads(line) for line in (ROOT / "eval/golden/cases.jsonl").open(encoding="utf-8")]
    found = [row["text"] for row in rows if isinstance(row.get("text"), str) and row["text"].strip()]
    return [found[i % len(found)] for i in range(n)]


def one(text: str) -> tuple[float, int, str | None]:
    started = time.perf_counter()
    try:
        facts = ml.call_model(text)
        kept, _ = ml.ground(facts, text)
        return time.perf_counter() - started, len(kept), None
    except Exception as error:  # noqa: BLE001 - report the failure kind, never the provider message
        return time.perf_counter() - started, 0, type(error).__name__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=32, help="requests to send")
    parser.add_argument("--concurrency", type=int, default=ml.concurrency())
    args = parser.parse_args()
    if ml.config() is None:
        sys.exit("Set KTRU_LLM_URL and KTRU_LLM_MODEL first.")
    print(f"model={ml.config()[1]} endpoint_id={ml.endpoint_id()[:12]} status={ml.status()}")
    one("Стол письменный, ЛДСП, 1200х600 мм")  # warm-up: loads the prompt prefix into the server cache
    started = time.perf_counter()
    with ThreadPoolExecutor(args.concurrency) as pool:
        results = list(pool.map(one, texts(args.n)))
    wall = time.perf_counter() - started
    seconds = sorted(r[0] for r in results)
    errors = [r[2] for r in results if r[2]]
    print(f"{args.n} requests, concurrency {args.concurrency}: {wall:.1f} s total, "
          f"{args.n / wall:.2f} rows/s")
    print(f"latency p50 {statistics.median(seconds):.2f} s, p95 {seconds[int(.95 * (len(seconds) - 1))]:.2f} s, "
          f"max {seconds[-1]:.2f} s; grounded facts per row p50 {statistics.median(r[1] for r in results)}")
    if errors:
        print(f"errors: {len(errors)} ({', '.join(sorted(set(errors)))})")
        sys.exit(1)


if __name__ == "__main__":
    main()
