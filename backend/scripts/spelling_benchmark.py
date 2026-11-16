"""Measure automatic spelling correction on the built catalog snapshot.

Three checks:
- synthetic misspellings of common catalog words, one error type at a time (fixed / wrong / missed);
- real golden descriptions (`eval/golden/cases.jsonl`): every word the corrector changes is listed;
- everyday correct words that must never change.

A synthetic typo that is itself a real Russian word («белки» → «белке») is reported separately: a word-by-word
corrector cannot know it is wrong, so it is left as typed.

    uv run --python 3.12 python scripts/spelling_benchmark.py [snapshot_dir]
"""

import json
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from src.ktru.catalog.reader import Catalog  # noqa: E402
from src.ktru.engine.source_map import autocorrect  # noqa: E402
from src.ktru.engine.spelling import known_word  # noqa: E402
from src.ktru.engine.text import stem  # noqa: E402

ROWS = ("йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю")
VOWEL_SWAP = {"о": "а", "а": "о", "е": "и", "и": "е", "я": "е"}
KINDS = ("adjacent key", "first letter", "dropped letter", "extra letter", "swapped letters", "vowel о/а е/и",
         "two errors")
EVERYDAY = ("красивый удобный прочный надежный складной детский ребенка кухни дачи гостиной спальни подарок "
            "компактный легкий тяжелый широкий узкий высокий низкий яркий матовый глянцевый мягкий жесткий "
            "водонепроницаемый беспроводной аккумуляторный портативный дорожный школьный ученический "
            "ноутбук смартфон планшет наушники колонка роутер принтер сканер шестигранных").split()


def _neighbours() -> dict[str, list[str]]:
    near = defaultdict(set)
    for row in ROWS:
        for a, b in zip(row, row[1:], strict=False):
            near[a].add(b)
            near[b].add(a)
    for upper, lower in zip(ROWS, ROWS[1:], strict=False):
        for i, char in enumerate(upper):
            for j in (i - 1, i):
                if 0 <= j < len(lower):
                    near[char].add(lower[j])
                    near[lower[j]].add(char)
    return {char: sorted(chars) for char, chars in near.items()}


NEAR = _neighbours()


def misspell(word: str, kind: str, rng: random.Random) -> str | None:
    w, i = list(word), rng.randrange(1, len(word) - 1)
    if kind == "adjacent key":
        w[i] = rng.choice(NEAR.get(w[i], [w[i]]))
    elif kind == "first letter":
        w[0] = rng.choice(NEAR.get(w[0], [w[0]]))
    elif kind == "dropped letter":
        del w[i]
    elif kind == "extra letter":
        w.insert(i, w[i])
    elif kind == "swapped letters":
        w[i], w[i + 1] = w[i + 1], w[i]
    elif kind == "vowel о/а е/и":
        spots = [k for k, char in enumerate(w) if k and char in VOWEL_SWAP]
        if not spots:
            return None
        k = rng.choice(spots)
        w[k] = VOWEL_SWAP[w[k]]
    elif kind == "two errors":
        j = rng.randrange(1, len(word) - 1)
        if abs(j - i) < 2:
            return None
        w[i] = rng.choice(NEAR.get(w[i], [w[i]]))
        del w[j]
    typo = "".join(w)
    return typo if typo != word else None


@dataclass
class Score:
    fixed: int = 0
    wrong: int = 0
    missed: int = 0
    real_word: int = 0  # the typo is itself a dictionary word; it stays as typed by design
    examples: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.fixed + self.wrong + self.missed

    def line(self, label: str) -> str:
        n = self.total or 1
        return (f"  {label:22s} fixed {self.fixed / n:6.1%}  wrong {self.wrong / n:5.1%}  missed {self.missed / n:5.1%}"
                f"  (n={self.total}, real-word typos left as typed: {self.real_word})")


def synthetic(catalog: Catalog, seed: int = 7) -> dict[tuple[str, str], Score]:
    rng = random.Random(seed)
    common = sorted(w for w, count in catalog._freq.items()
                    if count >= 30 and w.isalpha() and all("а" <= char <= "я" for char in w))
    groups = {"6+ letters": rng.sample([w for w in common if len(w) >= 6], 250),
              "4-5 letters": rng.sample([w for w in common if 4 <= len(w) <= 5], 120)}
    scores: dict[tuple[str, str], Score] = {}
    for group, sample in groups.items():
        for kind in KINDS:
            score = scores[group, kind] = Score()
            for word in sample:
                typo = misspell(word, kind, rng)
                if not typo or typo in catalog.vocab:
                    continue
                if known_word(typo):
                    score.real_word += 1
                    continue
                got = catalog.correct_word(typo)
                if got is None:
                    score.missed += 1
                elif stem(got) == stem(word):
                    score.fixed += 1
                else:
                    score.wrong += 1
                    if len(score.examples) < 5:
                        score.examples.append(f"{word}→{typo}→{got}")
    return scores


def golden_changes(catalog: Catalog) -> list[tuple[str, str, str, bool]]:
    """(case id, original, replacement, case is tagged as a typo case) for every change in golden texts."""
    out = []
    for line in (ROOT / "eval" / "golden" / "cases.jsonl").read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        tagged = any("typo" in tag or "опечат" in tag for tag in case.get("tags", []))
        for correction in autocorrect(case.get("text", ""), catalog.correct_word)[1]:
            out.append((case["case_id"], correction.original, correction.replacement, tagged))
    return out


def main() -> None:
    snapshot = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".ktru-build" / "snapshots" / "current"
    catalog = Catalog(snapshot)
    started = time.perf_counter()
    scores = synthetic(catalog)
    elapsed = time.perf_counter() - started
    total = Score()
    for (group, kind), score in scores.items():
        if kind == KINDS[0]:
            print(f"\nCatalog words, {group}")
        print(score.line(kind))
        for example in score.examples:
            print(f"      wrong: {example}")
        total.fixed, total.wrong, total.missed = (total.fixed + score.fixed, total.wrong + score.wrong,
                                                  total.missed + score.missed)
        total.real_word += score.real_word
    print("\n" + total.line("OVERALL"))
    checked = total.total + total.real_word
    print(f"  {checked} misspellings in {elapsed:.1f} s ({elapsed / checked * 1000:.2f} ms each, cache cold)")
    changes = golden_changes(catalog)
    print(f"\nGolden descriptions: {len(changes)} changes")
    for case_id, original, replacement, tagged in changes:
        print(f"  {case_id:12s} {original!r} → {replacement!r}" + ("" if tagged else "   (case not tagged as typo)"))
    changed = [(word, catalog.correct_word(word)) for word in EVERYDAY]
    print(f"\nEveryday words changed: {[pair for pair in changed if pair[1]]}")


if __name__ == "__main__":
    main()
