"""Secondary Portal fields require a named property or an explicit part/value relation."""

from src.ktru import contracts as C
from src.ktru.catalog.reader import Catalog, PortalChar
from src.ktru.engine import canonical
from src.ktru.engine.assertions import describes_root, identity, subject_of
from src.ktru.engine.text import content_stems, norm, quantities, word_has_label, words

NON_PRODUCT = ("поставк", "услуг", "требовани", "грузополучател", "вид продукци", "виды ")


def _same_subject(actual: C.SubjectRef, name: str) -> bool:
    expected = subject_of(name)
    # The card describes the root item; its product name is not part of the identity check.
    return actual.kind != "unknown" and actual.kind == expected.kind and describes_root(actual) and (
        actual.kind == "item" or actual.label == expected.label)


def _only_subject_words(name: str, actual: C.SubjectRef) -> bool:
    """Words of a Portal name besides its property word must name the fact's own part («спинки» of «спинка»)."""
    extra = [token for token in content_stems(name) if canonical.property_name(token) is None]
    if not extra:
        return True
    own = content_stems(actual.label or "") if actual.kind == "part" else []
    return bool(own) and all(any(word_has_label(token, label) or word_has_label(label, token) for label in own)
                             for token in extra)


def _valid_type(field: C.CardField, characteristic: PortalChar) -> bool:
    if isinstance(field.value, (C.NumberValue, C.RangeValue)):
        unit = field.value.unit_name
        return not characteristic.unit or norm(characteristic.unit) == norm(unit or "")
    if characteristic.allowed and isinstance(field.value, (C.TextValue, C.EnumValue)):
        source = content_stems(field.value.s if isinstance(field.value, C.TextValue) else field.value.label)
        return any(content_stems(label) and all(any(word_has_label(word, token) for word in source)
            for token in content_stems(label)) for label in characteristic.allowed)
    return not characteristic.allowed


def portalize(fields: list[C.CardField], cat: Catalog, domain: str, family_name: str, text: str) -> list[C.CardField]:
    if domain not in ("clothing", "furniture", "tools"):
        return fields
    category = cat.portal_category(domain, family_name, text)
    if not category:
        return fields
    characteristics = [p for p in cat.portal_chars(domain, category) if not norm(p.name).startswith(NON_PRODUCT)]
    result = []
    for field in fields:
        if field.source != "new":
            result.append(field)
            continue
        actual = field.subject or subject_of(field.display_ru if field.status == "unparsed" else field.label_ru)
        numeric = isinstance(field.value, (C.NumberValue, C.RangeValue))
        canonical_name = (canonical.numeric_property_name if numeric else canonical.property_name)(field.label_ru)
        named = [p for p in characteristics if (content_stems(p.name) == content_stems(field.label_ru)
                 or (canonical_name and canonical.property_name(p.name) == canonical_name
                     and _only_subject_words(p.name, actual)))  # «Вес» ≡ «масса»; «Высота спинки» ≡ высота of спинка
                 and _same_subject(actual, p.name) and _valid_type(field, p)]
        if field.status != "unparsed" and named:
            best = min(named, key=lambda p: (norm(p.name).strip() != norm(field.label_ru).strip(), len(p.name)))
            result.append(field.model_copy(update={"source": "portal", "label_ru": best.name,
                                                    "portal_category": category}))
            continue
        # Free clauses stay in the card. A derived field may be added only for one proven part/material relation.
        result.append(field)
        if field.status != "unparsed" or actual.kind != "part" or quantities(field.display_ru):
            continue
        observed = content_stems(field.display_ru)
        matches = []
        for characteristic in characteristics:
            if not norm(characteristic.name).startswith("материал") or not _same_subject(actual, characteristic.name):
                continue
            labels = [label for label in characteristic.allowed if content_stems(label)
                      and all(any(word_has_label(word, token) for word in observed)
                              for token in content_stems(label))]
            if labels:
                # Portal labels contain synonyms. Preserve the source wording instead of picking a canonical value.
                source_value = " ".join(word for word, _, _ in words(field.display_ru) if any(
                    len(tokens := content_stems(label)) == 1 and word_has_label(norm(word), tokens[0])
                    for label in labels))
                if source_value:
                    matches.append((characteristic, source_value))
        if len(matches) != 1:
            continue
        characteristic, label = matches[0]
        # Negated material needs a question; it must not become a positive Portal field.
        if "не" in norm(field.display_ru).split() or "без" in norm(field.display_ru).split():
            continue
        result.append(field.model_copy(update={"id": identity("portal", field.id, characteristic.name),
            "source": "portal", "label_ru": characteristic.name, "portal_category": category,
            "value": C.TextValue(s=label), "display_ru": label, "status": "from_text"}))
    return result
