"""Build the local open-model vector index for the current immutable KTRU snapshot."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ktru.catalog.reader import Catalog  # noqa: E402
from src.ktru.catalog.vector import build_index, default_index_dir  # noqa: E402
from src.ktru.ml.embed import REVISION  # noqa: E402


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=Path(os.environ.get(
        "KTRU_SNAPSHOT_DIR", root / ".ktru-build/snapshots/current")))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    catalog = Catalog(args.snapshot)
    destination = args.output or default_index_dir(catalog).parent
    model = args.model_dir or destination.parent / "models" / REVISION
    print(build_index(catalog, destination, model, threads=args.threads), flush=True)


if __name__ == "__main__":
    main()
