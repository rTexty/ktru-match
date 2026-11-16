from __future__ import annotations

from src.ktru import contracts as C
from src.ktru.catalog.reader import Catalog, Char, FamilyHit, Position
from src.ktru.engine.text import (
    norm,
)


def _char_of(p: Position, key: str) -> Char | None:
    for ch in p.chars:
        if norm(ch.name).strip() == key:
            return ch
    return None



def _pref(cat: Catalog, p: Position) -> C.PositionRef:
    return C.PositionRef(snapshot_id=cat.snapshot_id, code=p.code, version=p.version)



def _cref(cat: Catalog, p: Position, ch: Char) -> C.CharacteristicRef | None:
    if not ch.char_code or any(defect in ch.defects for defect in ("missing_char_code", "duplicate_char_code")):
        return None
    return C.CharacteristicRef(
        snapshot_id=cat.snapshot_id, position_code=p.code, position_version=p.version, char_code=ch.char_code
    )



def _fam_summary(cat: Catalog, h: FamilyHit, confirmed: bool) -> C.FamilySummary:
    return C.FamilySummary(
        id=h.family_id,
        name=h.name,
        template_ref=_pref(cat, h.template) if h.template else None,
        member_count=len(h.members),
        membership_complete=h.membership_complete,
        score=round(h.name_coverage + 0.5 * h.query_coverage, 3),
        confirmed_by_text=confirmed,
    )
