"""Immutable full-catalog family vectors, exact cosine search and an offline builder."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

from src.ktru.catalog.reader import Catalog
from src.ktru.ml.embed import DIMENSIONS, MODEL_FILES, PREPROCESSING_SHA256, E5Encoder, file_hash, prepare_model

INDEX_VERSION = 1


def default_index_dir(catalog: Catalog) -> Path:
    build = os.environ.get("KTRU_BUILD_DIR")
    root = Path(build) if build else catalog.dir.resolve().parents[1]
    return root / "vector" / catalog.snapshot_id / "current"


def family_documents(catalog: Catalog) -> list[dict]:
    names = catalog.family_names()
    categories: dict[str, set[str]] = defaultdict(set)
    characteristics: dict[str, set[str]] = defaultdict(set)
    db = catalog._db()
    for fid, name in db.execute(
        "SELECT DISTINCT family_id, okpd2_name FROM positions WHERE status='ACTIVE' AND is_template=0"
    ):
        if name:
            categories[fid].add(name)
    for fid, name in db.execute(
        "SELECT DISTINCT p.family_id, c.name FROM positions p JOIN characteristics c "
        "ON c.position_code=p.code AND c.position_version=p.version "
        "WHERE p.status='ACTIVE' AND p.is_template=0 AND c.actual=1"
    ):
        if name:
            characteristics[fid].add(name)
    return [{"family_id": fid, "name": name, "member_count": catalog._family_counts[fid],
             "text": f"{name}. Категория: {'; '.join(sorted(categories[fid]))}. "
                     f"Характеристики: {'; '.join(sorted(characteristics[fid])[:12])}"}
            for fid, name in sorted(names.items())]


def build_index(catalog: Catalog, destination: Path, model_dir: Path, *, threads: int = 4) -> Path:
    """Publish a self-contained index atomically; the SQLite snapshot remains read-only."""
    import numpy as np  # noqa: PLC0415

    prepare_model(model_dir)
    encoder = E5Encoder(model_dir, threads=threads)
    documents = family_documents(catalog)
    if not documents:
        raise ValueError("Cannot index an empty catalog")
    snapshot_hash = file_hash(catalog.dir / "catalog.sqlite")
    docs_json = json.dumps(documents, ensure_ascii=False, sort_keys=True)
    identity = hashlib.sha256((snapshot_hash + docs_json + json.dumps(encoder.metadata, sort_keys=True)
                               + PREPROCESSING_SHA256).encode()).hexdigest()[:20]
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"index-{identity}"
    if not (target / "manifest.json").is_file():
        staging = Path(tempfile.mkdtemp(prefix=".vector-", dir=destination))
        try:
            vectors = np.empty((len(documents), DIMENSIONS), dtype=np.float32)
            for start in range(0, len(documents), 64):
                batch = documents[start:start + 64]
                vectors[start:start + len(batch)] = encoder.encode([row["text"] for row in batch])
                if start % 1024 == 0:
                    print(f"Embedding families: {start + len(batch)}/{len(documents)}", flush=True)
            np.save(staging / "vectors.npy", vectors, allow_pickle=False)
            (staging / "families.json").write_text(docs_json)
            (staging / "model").mkdir()
            for name in (*MODEL_FILES, "model.json"):
                shutil.copyfile(model_dir / name, staging / "model" / name)
            manifest = {"index_version": INDEX_VERSION, "index_id": identity,
                "snapshot_id": catalog.snapshot_id, "snapshot_sha256": snapshot_hash,
                "preprocessing_sha256": PREPROCESSING_SHA256, "dimensions": DIMENSIONS,
                "families": len(documents), "final_positions": sum(row["member_count"] for row in documents),
                "model": encoder.metadata, "model_manifest": encoder.manifest.model_dump(),
                "files_sha256": {name: file_hash(staging / name) for name in ("vectors.npy", "families.json")}}
            (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            staging.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    link = destination / (".current-" + identity)
    link.symlink_to(target.name, target_is_directory=True)
    link.replace(destination / "current")
    return destination / "current"


class VectorIndex:
    def __init__(self, catalog: Catalog, path: Path, *, threads: int = 4):
        import numpy as np  # noqa: PLC0415

        self.metadata = json.loads((path / "manifest.json").read_text())
        m = self.metadata
        if m.get("index_version") != INDEX_VERSION or m.get("preprocessing_sha256") != PREPROCESSING_SHA256:
            raise ValueError("Unsupported vector index preprocessing")
        if m.get("snapshot_id") != catalog.snapshot_id or m.get("snapshot_sha256") != file_hash(catalog.dir / "catalog.sqlite"):
            raise ValueError("Vector index belongs to a different catalog snapshot")
        if {name: file_hash(path / name) for name in ("vectors.npy", "families.json")} != m.get("files_sha256"):
            raise ValueError("Vector index files have changed")
        self.documents = json.loads((path / "families.json").read_text())
        if len(self.documents) != m.get("families") or {r["family_id"] for r in self.documents} != set(catalog.family_names()):
            raise ValueError("Vector index does not cover the complete family catalog")
        self.vectors = np.load(path / "vectors.npy", mmap_mode="r", allow_pickle=False)
        if self.vectors.shape != (len(self.documents), DIMENSIONS) or not np.isfinite(self.vectors).all():
            raise ValueError("Invalid vector index shape or values")
        self.encoder = E5Encoder(path / "model", threads=threads)
        if self.encoder.metadata != m.get("model"):
            raise ValueError("Query encoder differs from the indexed model")
        self.manifest = self.encoder.manifest.model_copy(update={"parameters": {
            **self.encoder.manifest.parameters, "index_id": m["index_id"], "snapshot_id": catalog.snapshot_id,
            "indexed_families": len(self.documents), "indexed_positions": m["final_positions"]}})

    def search(self, text: str, limit: int = 32) -> list[tuple[str, float]]:
        import numpy as np  # noqa: PLC0415

        if limit < 1:
            return []
        vector = self.encoder.encode([text], query=True)[0]
        # For 14k x 384, a flat exact scan is small and avoids ANN recall loss or another service.
        scores = np.einsum("ij,j->i", self.vectors, vector, optimize=False)
        best = np.argsort(-scores, kind="stable")[:limit]
        return [(self.documents[i]["family_id"], float(scores[i])) for i in best]
