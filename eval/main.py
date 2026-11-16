"""Main-scenario eval: supplier description -> final KTRU code, with a simulated user who knows the product.

Gold comes from the catalog, never from the pipeline: a case names one ACTIVE final position; its expected
candidate set is every family member whose own allowed values accept every stated value (a missing
characteristic stays compatible). The simulated user answers each offered question with the gold position's
value, or «Не знаю». Two disjoint family samples: `dev` may guide fixes, `holdout` must not.

    uv run --project backend --python 3.12 python eval/main.py --split dev
    uv run --project backend --python 3.12 python eval/main.py --split holdout

This is catalog-derived synthetic validation, not an expert-labelled holdout of real supplier texts.
"""

import argparse
import json
import os
import random
import statistics
import sys
import time
import uuid
from collections import Counter
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from src.ktru import contracts as C  # noqa: E402
from src.ktru.catalog.reader import Catalog  # noqa: E402
from src.ktru.ml import extract as ml  # noqa: E402
from src.ktru.service.pipeline import Pipeline  # noqa: E402

DOMAINS = ("clothing", "furniture", "tools")
SPLIT_SEED = 20261010
SEEDS = {"dev": 11, "holdout": 77031}
PER_DOMAIN = 30
MAX_STEPS = 8
SHORT_UNITS = {"Сантиметр": "см", "Миллиметр": "мм", "Метр": "м", "Килограмм": "кг", "Грамм": "г",
               "Литр": "л", "Ватт": "Вт", "Киловатт": "кВт", "Вольт": "В", "Ампер-час": "А·ч",
               "Миллилитр": "мл", "Оборот в минуту": "об/мин"}
# A property no product of the domain has: the card must flag it, not accept it silently.
FOREIGN = {"clothing": "тяга двигателя 12 кН", "furniture": "тяга двигателя 5 кН", "tools": "фасон воротника: стойка"}


def usable(ch) -> bool:
    if ch.defects or len(ch.allowed) != 1:
        return False
    v = ch.allowed[0]
    if v.get("defects"):
        return False
    if v.get("kind") == "enum":
        return bool(v.get("label")) and len(v["label"]) <= 60
    return v.get("kind") in ("range", "number") and v.get("okei_name") in SHORT_UNITS


def bounds(v) -> tuple[Decimal | None, Decimal | None]:
    lo, hi = v.get("lo", v.get("x")), v.get("hi", v.get("x"))
    return (Decimal(lo) if lo not in (None, "") else None), (Decimal(hi) if hi not in (None, "") else None)


def stated(ch) -> tuple[str, dict]:
    """Phrase one characteristic value the way a supplier would and return the value it asserts."""
    v = ch.allowed[0]
    if v["kind"] == "enum":
        return f"{ch.name.lower()}: {v['label'].lower()}", {"kind": "enum", "label": v["label"]}
    lo, hi = bounds(v)
    lo, hi = lo if lo is not None else hi, hi if hi is not None else lo
    x = (lo + hi) / 2
    x = x.quantize(Decimal(1)) if hi - lo >= 2 else x.quantize(Decimal("0.01"))
    lo_ok = x > lo or v.get("lo_inclusive", True)
    hi_ok = x < hi or v.get("hi_inclusive", True)
    if not (lo <= x <= hi and lo_ok and hi_ok):
        x = lo
    return (f"{ch.name.lower()} {x.normalize():f} {SHORT_UNITS[v['okei_name']]}",
            {"kind": "number", "x": x, "okei": v.get("okei_code")})


def accepts(ch, value) -> bool:
    for v in ch.allowed:
        if value["kind"] == "enum" and v.get("kind") == "enum" and v.get("label", "").lower() == value["label"].lower():
            return True
        if value["kind"] == "number" and v.get("kind") in ("range", "number") and v.get("okei_code") == value["okei"]:
            lo, hi = bounds(v)
            if (lo is None or lo <= value["x"]) and (hi is None or value["x"] <= hi):
                return True
    return False


def families(cat: Catalog, domain: str) -> list[str]:
    rows = cat._db().execute("SELECT DISTINCT family_id FROM positions WHERE status='ACTIVE' AND is_template=0 "
                             "AND domain=? ORDER BY family_id", (domain,)).fetchall()
    return [r[0] for r in rows]


def build_cases(cat: Catalog, split: str) -> list[dict]:
    cases = []
    for domain in DOMAINS:
        order = random.Random(SPLIT_SEED).sample(families(cat, domain), len(families(cat, domain)))
        mine = order[0::2] if split == "dev" else order[1::2]  # disjoint family halves
        rng = random.Random(SEEDS[split])
        rng.shuffle(mine)
        taken = 0
        for family_id in mine:
            if taken >= PER_DOMAIN:
                break
            _, members = cat.family(family_id)
            if len(members) < 2:
                continue
            candidates = [p for p in members if sum(usable(c) for c in p.chars) >= 2]
            if not candidates:
                continue
            gold = rng.choice(candidates)
            chars = [c for c in gold.chars if usable(c)]
            rng.shuffle(chars)
            chars = chars[:4]
            facts = [stated(c) for c in chars]
            name = gold.name.split(",")[0].strip()
            for scenario, n, extra in (("full", len(facts), None), ("short", 1, None),
                                       ("foreign", len(facts), FOREIGN[domain])):
                text = ", ".join([name] + [phrase for phrase, _ in facts[:n]] + ([extra] if extra else []))
                values = {c.name: value for c, (_, value) in zip(chars[:n], facts[:n])}
                alive = sorted([p.code, p.version] for p in members if all(
                    accepts(ch, values[ch.name]) for ch in p.chars if ch.name in values and not ch.defects))
                cases.append({"case_id": f"{split}-{domain[:3]}-{taken:02d}-{scenario}", "domain": domain,
                              "scenario": scenario, "text": text, "family_id": family_id,
                              "gold": [gold.code, gold.version], "expected_alive": alive,
                              "expected_final": [gold.code, gold.version] if alive == [[gold.code, gold.version]]
                              else None, "foreign": extra})
            taken += 1
    return cases


def gold_char(cat: Catalog, gold: list, key: str):
    pos = cat.position(gold[0], gold[1])
    return next((ch for ch in (pos.chars if pos else []) if ch.name.lower().strip() == key.lower().strip()), None)


def choose(cat: Catalog, case: dict, question: C.Clarification, pending: dict) -> str | None:
    if question.kind == "family":
        return next((o.id for o in question.options if pending.get(o.id, {}).get("family_id") == case["family_id"]),
                    None)
    if question.kind == "conflict":
        return question.options[0].id if question.options else None
    for option in question.options:
        payload = pending.get(option.id, {})
        if payload.get("kind") != "char":
            continue
        ch = gold_char(cat, case["gold"], payload["char"])
        if ch and any(v.get("value_code") == payload["value"].get("value_code") for v in ch.allowed):
            return option.id
    return next((o.id for o in question.options if o.is_dont_know), None)


def run_case(pipeline: Pipeline, cat: Catalog, case: dict, with_model: bool = False) -> dict:
    row = {k: case[k] for k in ("case_id", "domain", "scenario", "text", "expected_final", "gold")}
    row.update(error=None, questions=0, question_kinds=[], final=None, initial_final=None, alive_superset=False,
               family_top3=False, foreign_flagged=None)
    try:
        started = time.perf_counter()
        state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=case["text"]))
        row["parse_ms"] = round((time.perf_counter() - started) * 1000, 1)
        if with_model:
            started = time.perf_counter()
            state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
                                                      expected_revision=state.revision))
            row["analyze_ms"] = round((time.perf_counter() - started) * 1000, 1)
            row["model_status"] = state.status  # "partial" = the model failed and rules answered alone
        row["family_top3"] = case["family_id"] in [f.id for f in state.families_top[:3]]
        row["initial_final"] = [state.final.position_ref.code, state.final.position_ref.version] if state.final else None
        alive = {(c.ref.code, c.ref.version) for c in state.candidates if c.status == "alive"}
        row["alive_superset"] = {tuple(x) for x in case["expected_alive"]} <= alive
        row["foreign_false_alarm"] = not case["foreign"] and any(
            d.kind == "implausible" and "не характерно" in d.message_ru for d in state.diagnostics)
        if case["foreign"]:
            word = case["foreign"].split()[0].lower()
            row["foreign_flagged"] = any(d.kind == "implausible" and word in (d.message_ru + " ".join(d.char_names)).lower()
                                         for d in state.diagnostics)
        for _ in range(MAX_STEPS):
            q = state.clarification
            if q is None or state.final:
                break
            option = choose(cat, case, q, pipeline._get(state.session_id).pending)
            if option is None:
                break
            row["questions"] += 1
            row["question_kinds"].append(q.kind)
            state = pipeline.answer(C.AnswerRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
                                                    expected_revision=state.revision, question_id=q.id,
                                                    option_id=option))
        row["final"] = [state.final.position_ref.code, state.final.position_ref.version] if state.final else None
        row["readiness"] = state.readiness
    except Exception as error:  # an error is a failed case, never a skipped one
        row["error"] = f"{type(error).__name__}: {error}"[:300]
    row["correct_initial"] = bool(case["expected_final"] and row["initial_final"] == case["expected_final"])
    row["correct_final"] = row["final"] == case["gold"]
    row["wrong_final"] = bool(row["final"] and row["final"] != case["gold"])
    return row


def metrics(rows: list[dict]) -> dict:
    n = len(rows)
    unique = [r for r in rows if r["expected_final"]]
    times = sorted(r["parse_ms"] for r in rows if "parse_ms" in r)
    foreign = [r for r in rows if r["foreign_flagged"] is not None]
    pct = lambda a, b: round(100 * a / b, 1) if b else None  # noqa: E731
    return {
        "cases": n,
        "correct_code_after_dialogue": f"{sum(r['correct_final'] for r in rows)}/{n}",
        "correct_code_after_dialogue_pct": pct(sum(r["correct_final"] for r in rows), n),
        "wrong_code": sum(r["wrong_final"] for r in rows),
        "code_from_text_alone": f"{sum(r['correct_initial'] for r in unique)}/{len(unique)}",
        "questions_mean": round(statistics.mean(r["questions"] for r in rows), 2) if rows else None,
        "zero_question_cases": sum(r["questions"] == 0 for r in rows),
        "questions_by_kind": dict(Counter(k for r in rows for k in r["question_kinds"])),
        "family_top3_pct": pct(sum(r["family_top3"] for r in rows), n),
        "gold_candidates_kept_pct": pct(sum(r["alive_superset"] for r in rows), n),
        "foreign_property_flagged": f"{sum(r['foreign_flagged'] for r in foreign)}/{len(foreign)}" if foreign else None,
        "foreign_false_alarms": sum(bool(r.get("foreign_false_alarm")) for r in rows),
        "parse_ms_p50": times[len(times) // 2] if times else None,
        "parse_ms_p95": times[max(0, int(len(times) * 0.95) - 1)] if times else None,
        "errors": sum(bool(r["error"]) for r in rows),
        "model_failures": sum(r.get("model_status") == "partial" for r in rows),
        "analyze_ms_p95": sorted(r["analyze_ms"] for r in rows if "analyze_ms" in r)[max(0, int(n * 0.95) - 1)]
        if any("analyze_ms" in r for r in rows) else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=SEEDS, default="dev")
    parser.add_argument("--snapshot", type=Path, default=Path(os.environ.get(
        "KTRU_SNAPSHOT_DIR", ROOT / ".ktru-build/snapshots-s04/current")))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--with-model", action="store_true",
                        help="also run /analyze with the configured open model (KTRU_LLM_URL, KTRU_LLM_MODEL)")
    parser.add_argument("--failures", type=int, default=0, help="print this many failed cases")
    a = parser.parse_args()
    cat = Catalog(a.snapshot)
    pipeline = Pipeline(cat)
    if a.with_model and ml.status() != "available":
        parser.error(f"model is {ml.status()}: start it and set KTRU_LLM_URL / KTRU_LLM_MODEL")
    rows = [run_case(pipeline, cat, case, a.with_model) for case in build_cases(cat, a.split)]
    report = {"split": a.split, "reader": "rules+model" if a.with_model else "rules", "snapshot_id": cat.snapshot_id, "independent_expert_holdout": False,
              "method": "catalog-derived gold; simulated user answers with gold values", "metrics": metrics(rows),
              "by_domain": {d: metrics([r for r in rows if r["domain"] == d]) for d in DOMAINS},
              "by_scenario": {s: metrics([r for r in rows if r["scenario"] == s]) for s in ("full", "short", "foreign")},
              "cases": rows}
    out = a.output or ROOT / f".ktru-build/eval/main-{a.split}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({k: report[k] for k in ("split", "reader", "metrics", "by_domain", "by_scenario")}, ensure_ascii=False,
                     indent=1))
    for r in [r for r in rows if not r["correct_final"]][: a.failures]:
        print(json.dumps({k: r[k] for k in ("case_id", "text", "gold", "final", "questions", "question_kinds", "error")},
                         ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
