"""Derive the OKPD2 classes of the three Portal groups from the organizers' data.

Each Portal category (Лист2 of Одежда/Мебель/Инструменты.xlsx) has a KPGZ code; the organizers' KPGZ dump
(pp-kpgz.zip) links a KPGZ code to an OKPD2 code. Classes named by at least three levels are kept: a broad class
(«31.0», «14.12») is already covered by the builder's prefixes. Writes src/ktru/catalog/portal_okpd2.json.

  uv run python scripts/portal_groups.py "../research/Казань 2026/Примеры выгрузок" \
      "../research/Данные с Тендерхаков/С апишки/pp-kpgz.zip"
"""

import hashlib
import json
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl

GROUPS = {"Одежда": "clothing", "Мебель": "furniture", "Инструменты": "tools"}
OUT = Path(__file__).resolve().parents[1] / "src/ktru/catalog/portal_okpd2.json"


def main(portal_dir: Path, kpgz_zip: Path) -> None:
    raw = zipfile.ZipFile(kpgz_zip).read("pp-kpgz.json")
    kpgz_okpd2 = {x["code"]: (x.get("okpd") or {}).get("code") for x in json.loads(raw)}
    votes: dict[str, Counter] = defaultdict(Counter)
    sources = [{"path": kpgz_zip.name, "sha256": hashlib.sha256(kpgz_zip.read_bytes()).hexdigest()}]
    for stem, group in GROUPS.items():
        path = portal_dir / f"{stem}.xlsx"
        sources.append({"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        seen = set()
        for row in openpyxl.load_workbook(path, read_only=True)["Лист2"].iter_rows(min_row=2, values_only=True):
            category, kpgz = row[0], row[1]
            okpd2 = kpgz_okpd2.get(kpgz) if kpgz else None
            if category and okpd2 and (category, kpgz) not in seen:
                seen.add((category, kpgz))
                votes[okpd2][group] += 1
    classes = {code: counter.most_common(1)[0][0] for code, counter in sorted(votes.items()) if code.count(".") >= 2}
    OUT.write_text(json.dumps({"sources": sources, "classes": classes}, ensure_ascii=False, indent=1) + "\n")
    print(OUT, Counter(classes.values()))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
