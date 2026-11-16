"""Validate model proposals against original text, quantities, subjects and negation scope."""

import json
import re

from src.ktru import contracts as C
from src.ktru.engine import canonical, products
from src.ktru.engine.assertions import (
    local_relation,
    numeric_negated,
    property_relation_matches,
    subject_of,
    subject_ref,
)
from src.ktru.engine.text import (
    UNITS,
    _unit,
    clauses,
    content_stems,
    dec,
    identifier,
    norm,
    quantities,
    stem,
    to_base,
    word_has_label,
    words,
)

VERBAL_QUANTITIES = {"полкило": "0.5 кг", "полкилограмма": "0.5 кг", "полметра": "0.5 м"}
OPERATORS = {"eq", "neq", "gt", "ge", "lt", "le", "between", "approx"}


def _span(fact, text):
    quote = str(fact.get("quote") or "").strip()
    if not quote:
        raise ValueError("Нет цитаты")
    if "start" in fact or "end" in fact:
        start, end = fact.get("start"), fact.get("end")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError("Некорректные позиции цитаты")
        if norm(text[start:end]) != norm(quote):
            raise ValueError("Цитата не совпадает с указанным фрагментом")
        return start, end
    matches = list(re.finditer(re.escape(norm(quote)), norm(text)))
    if not matches:
        # «ширина 35 мм» for «шириной 35 мм»: the same words in another form, found only once.
        pattern = r"\s+".join(re.escape(stem(w)) + r"\w*" if w.isalpha() and len(w) > 3 else re.escape(w)
                              for w in re.findall(r"\w+|[^\w\s]", norm(quote)))
        matches = list(re.finditer(pattern, norm(text))) if pattern else []
    if len(matches) != 1:
        raise ValueError("Цитата не найдена или повторяется; нужны точные позиции")
    return matches[0].span()


def _numeric(fact, quote):
    raw_value = canonical.digits(str(fact.get("value") or "").strip())
    unit = norm(str(fact.get("unit") or "")).strip()
    unit = (_unit(canonical.digits(f"1 {unit}")[2:]) or "") if unit else unit  # «штука» → «шт»
    amount = dec(raw_value)
    if amount is None and not re.fullmatch(r"[\d.,\s–—-]+", raw_value):
        return None
    if amount is None and not quantities(raw_value):
        raise ValueError("Число или диапазон не распознаны")
    rewritten = canonical.digits(quote)
    for word, replacement in VERBAL_QUANTITIES.items():
        rewritten = re.sub(rf"\b{word}\b", replacement, rewritten, flags=re.IGNORECASE)
    source = quantities(rewritten)
    model = quantities(f"{raw_value} {unit}".strip())
    if len(model) != 1 or not source:
        raise ValueError("Числовое значение не подтверждено цитатой")
    proposed = model[0]
    explicit_operator = fact.get("operator")
    failure = "Число, единица или границы не совпадают с цитатой"
    for quantity in source:
        px, pdim = to_base(proposed.a, proposed.unit_okei)
        qx, qdim = to_base(quantity.a, quantity.unit_okei)
        phi = to_base(proposed.b, proposed.unit_okei)[0] if proposed.b is not None else None
        qhi = to_base(quantity.b, quantity.unit_okei)[0] if quantity.b is not None else None
        if (px, pdim, phi) != (qx, qdim, qhi):
            # «от 430 до 550 мм» read as two numbers: each bound of the quoted range stands for the range.
            if not (phi is None and qhi is not None and pdim == qdim and px in (qx, qhi)):
                continue
            proposed = quantity
        if numeric_negated(rewritten, quantity.start):
            failure = "Числовое отрицание требует уточнения"
            continue
        # Words before a number are a context, not a property name («АКБ 18 В» states voltage of the battery).
        # Only an explicit different property in that context contradicts the model («ширина 50» is not mass).
        if not property_relation_matches(str(fact["property"]), quantity.property_raw):
            failure = "Связь числа со свойством не подтверждена цитатой"
            continue
        if unit and unit not in UNITS:
            if unit not in norm(quote):
                continue
        if explicit_operator is not None and explicit_operator != quantity.op:
            failure = "Оператор не подтверждён цитатой"
            continue
        value = (C.RangeValue(lo=str(proposed.a), hi=str(proposed.b), unit_okei=proposed.unit_okei,
                             unit_name=unit or quantity.unit_raw) if proposed.b is not None else
                 C.NumberValue(x=str(proposed.a), unit_okei=proposed.unit_okei, unit_name=unit or quantity.unit_raw))
        left, right = next(((left, right) for left, right in clauses(rewritten)
                            if left <= quantity.start < right), (0, len(rewritten)))
        return value.model_dump(), quantity.op, rewritten[left:right]
    raise ValueError(failure)


def _negation(quote, value):
    """Check negation attached to the value; numerical bounds are handled separately."""
    value_tokens = content_stems(value)
    tokens = list(re.finditer(r"[А-Яа-яЁёA-Za-z]+", quote))
    matching = [index for index, token in enumerate(tokens)
                if any(word_has_label(stem(token.group()), label) for label in value_tokens)]
    if not matching:
        return None
    # «без стальной головки», «не стальная»; postposed «стальной головки нет».
    scopes = {any(norm(token.group()) in ("не", "без") for token in tokens[max(0, index - 2):index])
              or any(norm(token.group()) == "нет" for token in tokens[index + 1:index + 3])
              for index in matching}
    return next(iter(scopes)) if len(scopes) == 1 else None


def _presence_proof(prop, value, part, text, start, end):
    """Use original local ranges once; a colon keeps the property with its answer."""
    scopes = set()
    quote = text[start:end]
    for left, right in clauses(quote):
        cuts = list(re.finditer(r"\s+и\s+", quote[left:right], re.IGNORECASE))
        edges = [(left, left + cuts[0].start())] if cuts else [(left, right)]
        edges += [(left + cut.end(), left + cuts[index + 1].start() if index + 1 < len(cuts) else right)
                  for index, cut in enumerate(cuts)]
        for left_edge, right_edge in edges:
            lo, hi, _ = local_relation(text, start + left_edge, start + right_edge, presence=True)
            connector = re.search(r"\b(?:с|со)[^\S\r\n\v\f\x1c-\x1f\x85\u2028\u2029]+$", text[:lo], re.IGNORECASE)
            if connector:
                lo = connector.start()  # local_relation drops «с»; it proves presence here.
            scopes.add((lo, hi))
    proofs = [(found, text[lo:hi]) for lo, hi in sorted(scopes)
              if (found := canonical.presence(prop, value, text[lo:hi], part))]
    if len(proofs) > 1:
        raise ValueError("Нет единственной локальной связи для наличия свойства")
    return proofs[0] if proofs else None


def _validate(fact, text):
    prop = str(fact.get("property") or "").strip()
    value = str(fact.get("value") or "").strip()
    if not prop or not value:
        raise ValueError("Нет свойства или значения")
    if canonical.unknown_value(value):
        raise ValueError("Значение в описании не указано")
    if fact.get("operator", "eq") not in OPERATORS:
        raise ValueError("Неизвестный оператор")
    start, end = _span(fact, text)
    quote = text[start:end]
    cs, ce = next(((s, e) for s, e in clauses(text) if s <= start < e), (start, end))
    if (layout := products.layout(text)) and (segment := layout.scope(start, end)):
        cs, ce = segment  # several products: the local relation is the product's own segment («Брюки 48»)
    context = text[cs:ce]
    if len(clauses(quote)) == 1:
        context = local_relation(text, start, end)[2]
    numeric = _numeric(fact, quote)
    relation = (context if len(clauses(quote)) == 1 else numeric[2]) if numeric else quote
    status = "supported"
    if not numeric:
        # A property and its value must co-occur in one local relation, not merely somewhere in the same quote.
        parts = [piece for left, right in clauses(quote)
                 for piece in re.split(r"\s+и\s+", quote[left:right], flags=re.IGNORECASE)]
        expected_value, expected_property = content_stems(value), content_stems(prop)
        value_parts = [piece for piece in parts if expected_value and all(
            any(word_has_label(word, token) for word in content_stems(piece)) for token in expected_value)]
        linked = [piece for piece in value_parts if expected_property and all(
            any(word_has_label(word, token) for word in content_stems(piece)) for token in expected_property)]
        if len(linked) == 1:
            relation = linked[0]
        elif len(value_parts) == 1 and property_relation_matches(prop, value_parts[0]):
            # Implicit property: «сиденье фанера» names the material by its value alone. An explicit other
            # property in the same relation («покрытие металл») is not this one.
            relation = value_parts[0]
        else:
            relation = quote
            status = "ambiguous"
    part = str(fact.get("part") or "").strip()
    presence_proof = None
    if not numeric and canonical.is_presence(prop, value):
        presence_proof = _presence_proof(prop, value, part, text, start, end)
        if presence_proof:
            relation = presence_proof[1]
    owner_text = relation
    if presence_proof:
        predicate = next((match for match in re.finditer(r"\bв\s+", relation, re.IGNORECASE)
                          if subject_of(relation[match.end():]).kind == "set"), None)
        if predicate:
            owner_text = relation[:predicate.start()]  # The included object's container is a predicate, not its owner.
    subject = subject_of(owner_text)
    if not presence_proof and subject.kind == "item" and len(clauses(quote)) == 1 and quote == relation:
        subject = subject_of(context)
        relation = context
    if status == "supported" and canonical.property_class(prop) == "материал" \
            and canonical.material(value) == "сталь" and canonical.STAINLESS.search(relation):
        raise ValueError("Значение теряет уточнение материала в локальной связи")
    supplied = fact.get("subject")
    if supplied is not None and supplied not in ("item", "part", "package", "set", "unknown"):
        raise ValueError("Неизвестный объект свойства")
    if supplied in ("part", "package", "set") and part and subject.kind in ("item", "unknown"):
        # The part lexicon is small; a part the model names must itself be quoted in the same clause.
        scope = relation if presence_proof else context
        if not _quoted(part, context) and layout and (sentence := layout.parallel(start)):
            scope = text[sentence[0]:sentence[1]]  # «один с деревянной, второй со стеклопластиковой ручкой»
        if not _quoted(part, scope):
            raise ValueError("Часть товара не подтверждена цитатой")
        known = subject_of(part)
        subject = subject_ref(supplied, known.label if known.kind == supplied else norm(part))
    else:
        # The subject is read from the source relation; a model that labels it differently («высота сиденья» as
        # the item) does not overrule the text, and its verified value is kept with the text's subject.
        # A model «set» without a set word in the text is the batch of the item itself («две штуки вместе»).
        pass
    if subject.kind == "unknown":
        status = "ambiguous"
    negated = fact.get("negated", False)
    if type(negated) is not bool:
        raise ValueError("Отрицание должно быть логическим значением")
    if numeric:
        typed, operator, _ = numeric
        if negated:
            raise ValueError("Отрицательное числовое утверждение не поддержано источником")
        package = canonical.package_label(prop)
        if package and subject.kind == "item" and _quoted(package, context):
            subject = subject_ref("package", package)
        prop = canonical.numeric_property_name(prop) or canonical.property_from_unit(_dimension(typed)) or prop
        if canonical.dimension_doubt(prop, _dimension(typed)):
            status = "ambiguous"
    elif identifier(value):
        # Model/article numbers are proved character by character: «7O0» is not «700», «СО-10» is not «CO-10».
        if value not in quote:
            raise ValueError("Обозначение не совпадает с цитатой посимвольно")
        prop = canonical.property_name(prop) or prop
        typed, operator, negated, status = C.TextValue(s=value).model_dump(), "eq", False, "supported"
    elif presence_proof:
        # «с капюшоном»: the relation split drops the «с»; the verified quote keeps it.
        # Presence of a named object: the object comes from the claim, yes/no from the source words.
        (obj, present), relation = presence_proof
        known = subject_of(obj)
        if subject.kind == "item" and not _names_product(obj, text):
            subject = subject_ref("part", known.label if known.kind == "part" else obj)
        prop, negated, status = "наличие", False, "supported"
        typed, operator = C.BoolValue(v=present).model_dump(), "eq"
    else:
        expected = content_stems(value)
        observed = content_stems(quote)
        word = norm(value).strip()
        single = len(word) == 1 and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", norm(quote))  # size «L»
        named = bool(expected) and any(  # «кожа» is quoted as «кожаные», «синий» as «синяя»
            lookup(value) and lookup(value) == lookup(w) for lookup in (canonical.material, canonical.color)
            for w, _, _ in words(quote))
        if not single and not named and (
                not expected or not all(any(word_has_label(w, label) for w in observed) for label in expected)):
            raise ValueError("Значение не подтверждено цитатой")
        source_negated = _negation(relation, value)
        if source_negated is None:
            status = "ambiguous"
        elif negated != source_negated:
            raise ValueError("Отрицание не совпадает с цитатой")
        # Negation lives in polarity; «не сталь» is polarity=negative with operator eq, never a double negative.
        typed, operator = C.TextValue(s=value).model_dump(), "eq"
        if found := canonical.categorical(prop, value):
            # «каркас: сталь» names a part by the property slot; the part must be quoted in the clause.
            if canonical.property_class(prop) is None and subject.kind == "item" and _quoted(prop, context) \
                    and not _names_product(prop, text):
                known = subject_of(prop)
                subject = subject_ref("part", known.label if known.kind == "part" else norm(prop).strip())
            prop = found[0]
            typed = C.TextValue(s=found[1]).model_dump()  # canonical label; catalog binding decides enum
    if fact.get("uncertain") is True and status != "rejected":
        # The model may only lower confidence: a hedged or ordered value never becomes a confirmed fact.
        status = "ambiguous"
    return {**fact, "property": prop, "quote": quote, "start": start, "end": end,
            "typed_value": typed, "operator": operator, "negated": negated, "status": status,
            "subject_context": relation, "subject_ref": subject.model_dump()}


def _dimension(typed: dict) -> str | None:
    okei = typed.get("unit_okei")
    return next((dim for _, (code, dim, _) in UNITS.items() if code == okei), None) if okei else None


def _quoted(words: str, context: str) -> bool:
    observed = content_stems(context)
    stems = content_stems(words)
    return bool(stems) and all(any(word_has_label(w, t) for w in observed) for t in stems)


def _names_product(words: str, text: str) -> bool:
    label = canonical.product_label(text)
    return bool(label) and _quoted(words, label)


def ground(facts: list[dict], text: str) -> tuple[list[dict], list[dict]]:
    kept, rejected = [], []
    for fact in facts:
        try:
            kept.append(_validate(fact, text))
        except (ValueError, TypeError, KeyError) as error:
            rejected.append({"raw": json.dumps(fact, ensure_ascii=False)[:1000], "reason_ru": str(error)})
    return kept, rejected
