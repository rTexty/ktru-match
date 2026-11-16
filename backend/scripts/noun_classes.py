"""Product noun → OKPD2 class, from public Portal cards (zakupki.mos.ru) of the three groups.

Input: JSONL of fetched cards with fields domain, head (first noun stem of the listing name), okpd2. A noun keeps
its majority class when at least two of its cards agree (or its only card names one). Writes
src/ktru/catalog/noun_okpd2.json, which the snapshot builder joins to KTRU families of the same class: a
listing word without a KTRU family of its own («перфоратор») then reaches the class family the Portal files it
under («Инструменты ручные электрические…»).

  uv run python scripts/noun_classes.py ../.ktru-build/quality/bench/head_raw.jsonl
"""

import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src/ktru/catalog/noun_okpd2.json"


def main(path: Path) -> None:
    votes: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for line in path.open():
        card = json.loads(line)
        if card.get("okpd2") and card.get("head"):
            votes[(card["domain"], card["head"])][card["okpd2"]] += 1
    nouns: dict[str, dict] = {}
    for (domain, head), counter in sorted(votes.items()):
        code, agree = counter.most_common(1)[0]
        cards = sum(counter.values())
        if agree >= 2 or cards == 1:
            best = nouns.get(head)
            if best is None or agree > best["agree"]:
                nouns[head] = {"okpd2": code, "domain": domain, "cards": cards, "agree": agree}
    source = {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "origin": "public cards https://old.zakupki.mos.ru/api/Cssp/Sku/GetEntity"}
    OUT.write_text(json.dumps({"source": source, "nouns": nouns}, ensure_ascii=False, indent=1) + "\n")
    print(OUT, len(nouns), Counter(n["domain"] for n in nouns.values()))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
