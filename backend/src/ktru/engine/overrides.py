"""User corrections of readings: remove a value, move it to another object, restate its modality, rename an object.

They are stored as session edits (so revision CAS and undo apply) and replayed on every recomputation over the
assertions read from the unchanged text. Matching uses the assertion and subject IDs shown on the card the user
edited; recomputation from the same text and facts reproduces those IDs. Original evidence is kept and the user
edit is added as evidence, so an overridden reading still shows where it came from.
"""

from __future__ import annotations

from pydantic import TypeAdapter

from src.ktru import contracts as C
from src.ktru.engine import assertions as A

KINDS = ("remove", "move", "modality", "rename")
_VALUE = TypeAdapter(C.Value)


def overrides(edits: list[dict]) -> list[dict]:
    return [edit for edit in edits if edit.get("kind") in KINDS]


def renamed_item(edits: list[dict]) -> str | None:
    """The latest user name of the described product, if the user renamed it."""
    item_id = A.subject_ref("item", None).id
    return next((edit["label"] for edit in reversed(overrides(edits))
                 if edit["kind"] == "rename" and edit["subject_id"] == item_id), None)


def _evidence(assertion: C.Assertion, edit: dict) -> list:
    item = C.UserEvidence(type="user_edit", revision=edit["revision"], answer_text=edit.get("note"))
    return list(assertion.evidence) if item in assertion.evidence else [*assertion.evidence, item]


def _older(assertion: C.Assertion, revision: int) -> bool:
    """A removal covers text readings and user inputs given before it, never a later answer or edit."""
    given = [item.revision for item in assertion.evidence if isinstance(item, C.UserEvidence)]
    return assertion.origin == "text" or not given or max(given) < revision


def _key(assertion: C.Assertion) -> tuple:
    return A.property_key(assertion), assertion.subject.id if assertion.subject else None


def _subject(kind: str, label: str) -> C.SubjectRef:
    return A.subject_ref(kind, None if kind == "item" else label).model_copy(update={"label": label})


def apply(assertions: list[C.Assertion], edits: list[dict]) -> list[C.Assertion]:
    """Apply the user's corrections in order. Idempotent: applying twice gives the same assertions."""
    out = list(assertions)
    for edit in overrides(edits):
        kind = edit["kind"]
        if kind == "remove":
            ids = set(edit["assertion_ids"])
            keys = {_key(a) for a in out if a.id in ids}
            out = [a.model_copy(update={"status": "rejected", "note_ru": "Значение удалено пользователем",
                                        "evidence": _evidence(a, edit)})
                   if a.status != "rejected" and _older(a, edit["revision"]) and (a.id in ids or _key(a) in keys)
                   else a for a in out]
        elif kind == "move":
            subject = _subject(edit["subject_kind"], edit["subject_label"])
            out = [a.model_copy(update={"subject": subject, "subject_kind": subject.kind,
                                        "subject_label": subject.label, "evidence": _evidence(a, edit)})
                   if a.id == edit["assertion_id"] else a for a in out]
        elif kind == "modality":
            changes = {key: edit[key] for key in ("status", "polarity", "operator") if edit.get(key) is not None}
            if edit.get("value") is not None:
                changes["value"] = _VALUE.validate_python(edit["value"])
            out = [a.model_copy(update={**changes, "note_ru": "Уверенность изменена пользователем",
                                        "evidence": _evidence(a, edit)})
                   if a.id == edit["assertion_id"] else a for a in out]
        else:
            renamed: dict[str, C.SubjectRef] = {}
            for a in out:
                if a.subject and a.subject.id == edit["subject_id"] and a.subject.id not in renamed:
                    subject = (a.subject.model_copy(update={"label": edit["label"]}) if a.subject.kind == "item"
                               else _subject(a.subject.kind, edit["label"]))
                    renamed[a.subject.id] = subject.model_copy(update={"evidence": a.subject.evidence})
            out = [a.model_copy(update={"subject": renamed[a.subject.id], "subject_label": edit["label"]})
                   if a.subject and a.subject.id in renamed else a for a in out]
    return out
