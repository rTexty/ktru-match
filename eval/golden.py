"""Frozen public behavior validation. This module never infers labels from output.

Only explicit checks receive pass_checks. Natural-language requirements, dialogue
fixtures and exact catalog accuracy are reported separately, never implied by it.
"""

import csv
import hashlib
import json
import re
import time
import uuid
from collections import Counter
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from src.ktru import contracts as C

ROOT = Path(__file__).resolve().parents[1]
BANK = ROOT / "research-library/09-user-input-corpus/2026-10-10/cases"
DATA = ROOT / "eval/golden"
HEADER = ("case_id", "group_id", "domain", "tags", "priority", "precondition", "input_json",
          "expected_action", "must", "forbidden", "oracle_status")
VALUE = TypeAdapter(C.Value)
OPERATORS = {"eq", "neq", "gt", "ge", "lt", "le", "between", "approx"}
POLARITIES = {"positive", "negative", "uncertain"}
KINDS = {"item", "part", "package", "set", "unknown"}
ARTIFACTS = ("selection.json", "dialogues.json", "curation-audit.json")
# Evaluation conversion table is independent of the product quantity parser.
UNITS = {"003": ("length", "1"), "004": ("length", "10"), "005": ("length", "100"), "006": ("length", "1000"),
         "163": ("mass", "1"), "166": ("mass", "1000"), "168": ("mass", "1000000"),
         "111": ("volume", "1"), "112": ("volume", "1000"),
         "212": ("power", "1"), "214": ("power", "1000"), "222": ("voltage", "1"), "744": ("percent", "1"),
         "715": ("pair_count", "1"), "796": ("piece_count", "1"), "280": ("temperature", "1"),
         "263": ("charge", "1"), "290": ("frequency", "1"), "291": ("frequency", "1000"), "331": ("rotation", "1"),
         "297": ("pressure", "1"), "298": ("pressure", "1000"), "309": ("pressure", "100"), "2355": ("angle", "1"),
         "039": ("length", "25.4"), "3135": ("sound", "1"), "260": ("current", "1")}
UNIT_NAMES = {"мм": "003", "см": "004", "дм": "005", "м": "006", "г": "163", "гр": "163",
              "грамм": "163", "кг": "166", "т": "168", "мл": "111", "л": "112", "вт": "212",
              "квт": "214", "в": "222", "%": "744", "пара": "715", "пары": "715", "пар": "715",
              "шт": "796", "шт.": "796", "штука": "796", "штуки": "796", "штук": "796", "°c": "280", "°с": "280",
              "а·ч": "263", "ач": "263", "гц": "290", "об/мин": "331", "бар": "309", "кпа": "297", "мпа": "298",
              "°": "2355", "дюйм": "039", "дб": "3135"}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load_bank(directory=BANK):
    rows, seen = [], set()
    for path in sorted(Path(directory).glob("*.tsv")):
        raw = path.read_bytes()
        reader = csv.DictReader(raw.decode("utf-8").splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE)
        if tuple(reader.fieldnames or ()) != HEADER:
            raise ValueError(f"Invalid TSV header: {path.name}")
        for line, source in enumerate(reader, 2):
            if set(source) != set(HEADER) or any(value is None for value in source.values()):
                raise ValueError(f"Invalid TSV row: {path.name}:{line}")
            text = json.loads(source["input_json"])
            if not isinstance(text, str) or source["case_id"] in seen:
                raise ValueError(f"Invalid or duplicate case: {path.name}:{line}")
            seen.add(source["case_id"])
            canonical = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            rows.append({**source, "tags": source["tags"].split(";"), "text": text,
                         "source_file": path.name, "source_hash": digest(raw), "row_hash": digest(canonical.encode())})
    if not rows:
        raise ValueError("Source bank has no cases")
    return rows


def select(rows, groups, case_ids=None):
    if not isinstance(groups, dict) or any(not isinstance(p, str) or not isinstance(v, list)
                                          or any(not isinstance(n, str) for n in v) for p, v in groups.items()):
        raise ValueError("Selection must map prefixes to group lists")
    available = {row["group_id"] for row in rows}
    chosen = set()
    for prefix, names in groups.items():
        for name in names:
            if name not in available or not name.startswith(prefix + "-G") or name in chosen:
                raise ValueError(f"Unknown, duplicate or invalid group: {name}")
            chosen.add(name)
    selected = [deepcopy(row) for row in rows if row["group_id"] in chosen]
    if case_ids is not None:
        if (not isinstance(case_ids, list) or not case_ids
                or any(not isinstance(case_id, str) for case_id in case_ids)
                or len(case_ids) != len(set(case_ids))):
            raise ValueError("Compact selection needs unique explicit case IDs")
        wanted = set(case_ids)
        if wanted - {row["case_id"] for row in selected}:
            raise ValueError("Compact case IDs are outside selected source groups")
        selected = [row for row in selected if row["case_id"] in wanted]
        if {row["group_id"] for row in selected} != chosen:
            raise ValueError("Compact selection contains unused groups")
    return selected


def _aliases(value):
    return isinstance(value, list) and bool(value) and all(isinstance(s, str) and s.strip() for s in value)


def _fact_errors(fact, text, required):
    if not isinstance(fact, dict):
        return ["fact must be an object"]
    errors = []
    allowed = {"property", "value", "value_aliases", "value_types", "operator", "polarity", "status", "subject", "quote"}
    if set(fact) - allowed:
        errors.append("unknown fact keys")
    if not _aliases(fact.get("property")):
        errors.append("property needs explicit aliases")
    if (required or "operator" in fact) and (not isinstance(fact.get("operator"), str) or fact["operator"] not in OPERATORS):
        errors.append("invalid operator")
    if not isinstance(fact.get("polarity"), str) or fact["polarity"] not in POLARITIES:
        errors.append("invalid operator or polarity")
    status = fact.get("status")
    if not _aliases(status) or set(status) - {"supported", "ambiguous"}:
        errors.append("invalid expected status")
    subject = fact.get("subject")
    if not isinstance(subject, dict) or not isinstance(subject.get("kind"), str) or subject["kind"] not in KINDS:
        errors.append("invalid subject")
    else:
        if set(subject) - {"kind", "label", "parent_label"}:
            errors.append("unknown subject keys")
        for key in ("label", "parent_label"):
            if key in subject and not _aliases(subject[key]):
                errors.append(f"invalid subject {key}")
    if "value" in fact:
        try:
            VALUE.validate_python(fact["value"], strict=True)
        except (ValidationError, TypeError):
            errors.append("invalid typed value")
    elif required:
        errors.append("required fact needs typed value")
    if "value_aliases" in fact and (not _aliases(fact["value_aliases"])
            or not isinstance(fact.get("value"), dict) or fact["value"].get("type") not in {"enum", "text"}):
        errors.append("value aliases need enum or text")
    if "value_types" in fact and (not _aliases(fact["value_types"])
            or set(fact["value_types"]) - {"number", "range", "enum", "bool", "text", "unparsed"}):
        errors.append("invalid value type filter")
    quote = fact.get("quote")
    if required or quote is not None:
        if not isinstance(quote, str) or not quote or quote not in text:
            errors.append("quote must occur in original input")
    return errors


def _expected_errors(expected, text):
    if not isinstance(expected, dict):
        return ["expected must be an object"]
    errors = []
    allowed = {"required", "forbidden", "preserve_quotes", "no_final", "no_assertions", "clarification_required", "domain"}
    if set(expected) - allowed:
        errors.append("unknown expected keys")
    if "domain" in expected and (not isinstance(expected["domain"], str)
            or expected["domain"] not in {"clothing", "furniture", "tools"}):
        errors.append("invalid expected domain")
    for key in ("required", "forbidden"):
        facts = expected.get(key, [])
        if not isinstance(facts, list):
            errors.append(f"{key} must be a list")
            continue
        for fact in facts:
            errors.extend(_fact_errors(fact, text, key == "required"))
    for key in ("no_final", "no_assertions", "clarification_required"):
        if key in expected and type(expected[key]) is not bool:
            errors.append(f"{key} must be boolean")
    quotes = expected.get("preserve_quotes", [])
    if not isinstance(quotes, list) or any(not isinstance(q, str) or not q or q not in text for q in quotes):
        errors.append("preserve_quotes must contain original fragments")
    return errors


def validate(dataset, manifest, bank_rows):
    errors = []
    if not isinstance(manifest, dict) or not isinstance(dataset, list) or any(not isinstance(r, dict) for r in dataset):
        return ["manifest must be an object and cases must be a list of objects"]
    if not dataset:
        errors.append("Golden Set must not be empty")
    compact = manifest.get("schema_version") == "judging-golden-2"
    if manifest.get("schema_version") not in {"adversarial-golden-1", "judging-golden-2"}:
        errors.append("unsupported Golden Set schema")
    if manifest.get("exposure") != "public_synthetic_validation":
        errors.append("public bank cannot be claimed as independent holdout")
    sources = {r["source_file"]: r["source_hash"] for r in bank_rows}
    if manifest.get("sources") != sources:
        errors.append("complete source bank hashes differ from manifest")
    try:
        if compact and not isinstance(manifest.get("case_ids"), list):
            raise ValueError("Compact manifest needs explicit case IDs")
        selected = select(bank_rows, manifest["groups"], manifest.get("case_ids") if compact else None)
    except (ValueError, KeyError, TypeError) as error:
        return [*errors, str(error)]
    source = {row["case_id"]: row for row in selected}
    ids = [row.get("case_id") for row in dataset]
    if any(not isinstance(i, str) for i in ids):
        return [*errors, "case IDs must be strings"]
    if len(ids) != len(set(ids)) or set(ids) != set(source):
        errors.append("case IDs duplicate, missing or outside declared selection")
    dialogue = sum(row.get("case_id", "").startswith("DLG-") for row in dataset)
    if compact:
        single_groups = Counter(row.get("group_id") for row in dataset if not row.get("case_id", "").startswith("DLG-"))
        if any(count > 1 for count in single_groups.values()):
            errors.append("judging-golden-2 keeps one standalone input per source group")
    for key, actual in (("total", len(dataset)), ("single", len(dataset) - dialogue), ("dialogue", dialogue)):
        if type(manifest.get(key)) is not int or manifest[key] != actual:
            errors.append(f"{key} count differs from manifest")
    for row in dataset:
        label = row.get("case_id", "unknown")
        original = source.get(label)
        if original is None:
            continue
        for key in original:
            if key == "oracle_status":  # Reviewed annotation status is stored separately.
                continue
            if row.get(key) != original[key]:
                errors.append(f"{label}: source {key} differs")
        if manifest.get("sources", {}).get(original["source_file"]) != original["source_hash"]:
            errors.append(f"{label}: source file hash differs from manifest")
        errors.extend(f"{label}: {e}" for e in _expected_errors(row.get("expected"), original["text"]))
    for group in manifest["groups"].get("DLG", []):
        members = [row for row in dataset if row.get("group_id") == group]
        turns = [next((t for t in row["tags"] if t.startswith("turn_")), None) for row in members]
        if turns != [f"turn_{i}" for i in range(1, 6)]:
            errors.append(f"{group}: dialogue needs all five ordered turns")
    return errors


def _norm(text):
    return " ".join(str(text or "").casefold().replace("ё", "е").split())


def _label_match(aliases, label):
    normalized = _norm(label)
    return any(re.search(r"(?<!\w)" + re.escape(_norm(alias)) + r"(?!\w)", normalized) for alias in aliases)


def _quantity(value):
    code = value.get("unit_okei") or UNIT_NAMES.get(_norm(value.get("unit_name")))
    if code in UNITS:
        dimension, factor = UNITS[code]
        identity = ("known", dimension)
    else:
        identity, factor = ("unknown", code, _norm(value.get("unit_name"))), "1"
    with localcontext() as context:
        context.prec = 200
        numbers = tuple(Decimal(value[key]) * Decimal(factor) if value.get(key) is not None else None
                        for key in (("x",) if value["type"] == "number" else ("lo", "hi")))
    return identity, numbers


def _value_match(fact, value, equivalent=False):
    if "value_types" in fact and (not isinstance(value, dict) or value.get("type") not in fact["value_types"]):
        return False
    expected = fact.get("value")
    if expected is None:
        return True
    try:
        left = VALUE.validate_python(expected, strict=True).model_dump()
        right = VALUE.validate_python(value, strict=True).model_dump()
    except (ValidationError, TypeError):
        return False
    kind = left["type"]
    if kind != right["type"]:
        if equivalent and {kind, right["type"]} <= {"enum", "text"}:
            # E1: an unbound categorical value is text in the product and an enum label in gold.
            expected_label, actual = left.get("label", left.get("s")), right.get("label", right.get("s"))
            return _norm(actual) in {_norm(v) for v in [expected_label, *fact.get("value_aliases", [])]}
        return False
    if kind in {"number", "range"}:
        return _quantity(left) == _quantity(right) and (kind != "range" or all(
            left[key] == right[key] for key in ("lo_inclusive", "hi_inclusive")))
    key = {"enum": "label", "text": "s", "unparsed": "s", "bool": "v"}[kind]
    if kind == "bool":
        return left[key] == right[key]
    # Identifier-like text is deliberately exact; enum labels permit case variation.
    if kind in {"text", "unparsed"}:
        return right[key] in [left[key], *fact.get("value_aliases", [])]
    return _norm(right[key]) in {_norm(v) for v in [left[key], *fact.get("value_aliases", [])]}


def _source_match(quote, assertion, state, equivalent=False):
    if quote is None:
        return True
    if assertion.get("origin") != "text":
        return False
    text = state.get("original_text", state.get("text", ""))
    evidence = [*assertion.get("evidence", [])]
    if assertion.get("span"):
        evidence.append({"type": "text", "span": assertion["span"]})
    for item in evidence:
        span = item.get("span") or {}
        start, end = span.get("start"), span.get("end")
        if item.get("type") != "text" or type(start) is not int or type(end) is not int:
            continue
        if not 0 <= start < end <= len(text) or span.get("text") != text[start:end]:
            continue
        revision = state.get("text_revision", state.get("revision"))
        if revision is not None and span.get("text_revision") != revision:
            continue
        if quote in text[start:end]:
            return True
        if equivalent and _same_statement(quote, text, start, end):
            return True
    return False


def _same_statement(quote, text, start, end):
    """E4: the evidence overlaps the gold quote and both lie in one sentence (no . ! ? or line break between)."""
    for m in re.finditer(re.escape(quote), text):
        lo, hi = min(m.start(), start), max(m.end(), end)
        if m.start() < end and start < m.end() and not re.search(r"[.!?\n]\s", text[lo:hi].rstrip(".!? ")):
            return True
    return False


def _stems(text):
    return {w[:5] for w in re.findall(r"[a-zа-я]+", _norm(text)) if len(w) >= 3}


def _presence_equivalent(fact, assertion, state):
    """E2: «Наличие капюшона» of the item equals «наличие» of the part «капюшон» (same yes/no, same owner)."""
    value, actual = fact.get("value") or {}, assertion.get("value") or {}
    if value.get("type") != "bool" or actual.get("type") != "bool" or actual.get("v") != value.get("v"):
        return False
    if _norm(assertion.get("property_raw")) != "наличие":
        return False
    subjects = {s["id"]: s for s in state.get("subjects", [])}
    obj = subjects.get((assertion.get("subject") or {}).get("id")) or {}
    obj_stems = _stems(obj.get("label") or "")
    target = fact["subject"]
    named = set().union(*(_stems(name) for name in [*fact["property"], *target.get("label", [])]))
    if not obj_stems or not obj_stems <= named:
        return False
    parent = subjects.get(obj.get("parent_subject_id")) or {}
    owners = [obj.get("label") or "", parent.get("label") or ""]
    expected = [*target.get("label", []), *target.get("parent_label", [])]
    return not expected or any(_stems(o) & _stems(e) for o in owners for e in expected if o and e)


def match_fact(fact, assertion, state, equivalent=False):
    if equivalent and _presence_equivalent(fact, assertion, state):
        return all(assertion.get(k) == fact[k] for k in ("operator", "polarity") if k in fact) \
            and assertion.get("status") in fact["status"] and _source_match(fact.get("quote"), assertion, state, True)
    if _norm(assertion.get("property_raw")) not in {_norm(p) for p in fact["property"]}:
        return False
    for key in ("operator", "polarity"):
        if key in fact and assertion.get(key) != fact[key]:
            return False
    if assertion.get("status") not in fact["status"] or not _value_match(fact, assertion.get("value"), equivalent):
        return False
    subject = assertion.get("subject") or {}
    target = fact["subject"]
    if assertion.get("subject_kind") != target["kind"] or subject.get("kind", target["kind"]) != target["kind"]:
        return False
    subjects = {s["id"]: s for s in state.get("subjects", [])}
    actual = subjects.get(subject.get("id"))
    if not actual or actual.get("kind") != subject.get("kind"):
        return False
    if any(subject.get(key) is not None and subject.get(key) != actual.get(key)
           for key in ("label", "parent_subject_id")):
        return False
    visited, ancestor = set(), actual
    while ancestor is not None:
        if ancestor["id"] in visited:
            return False
        visited.add(ancestor["id"])
        parent_id = ancestor.get("parent_subject_id")
        if parent_id is not None and parent_id not in subjects:
            return False
        ancestor = subjects.get(parent_id)
    label = actual.get("label") or assertion.get("subject_label")
    if "label" in target and not _label_match(target["label"], label):
        return False
    if "parent_label" in target:
        parent = subjects.get(actual.get("parent_subject_id"))
        if not parent or parent.get("kind") not in KINDS:
            return False
        if not _label_match(target["parent_label"], parent.get("label")):
            return False
    return _source_match(fact.get("quote"), assertion, state, equivalent)


def _retained(quote, state, equivalent=False):
    # Original text alone is not extraction. The fragment must reach an observation/card field.
    if equivalent:
        # E3: sentence punctuation and spacing around a fragment do not change what was kept.
        quote = " ".join(quote.split()).strip(" .,;:!?")
    for item in [*state.get("assertions", []), *state.get("fields", [])]:
        if item.get("status") == "rejected":
            continue
        raw = [item.get("value_raw"), item.get("property_raw"), (item.get("value") or {}).get("s")]
        if equivalent:
            raw = [" ".join(v.split()) for v in raw if isinstance(v, str)]
        if any(isinstance(v, str) and quote in v for v in raw):
            return True
    return False


def graph_errors(state):
    """Invalid identity must fail grading, including forbidden-only checks."""
    nodes = state.get("subjects", [])
    if not isinstance(nodes, list) or any(not isinstance(s, dict) or not isinstance(s.get("id"), str) for s in nodes):
        return ["invalid subject registry"]
    subjects = {s["id"]: s for s in nodes}
    if len(subjects) != len(nodes):
        return ["duplicate subject identities"]
    for subject in nodes:
        visited, ancestor = set(), subject
        while ancestor is not None:
            if ancestor["id"] in visited:
                return ["cyclic subject graph"]
            visited.add(ancestor["id"])
            parent = ancestor.get("parent_subject_id")
            if parent is not None and parent not in subjects:
                return ["dangling subject parent"]
            ancestor = subjects.get(parent)
    assertions = state.get("assertions", [])
    if not isinstance(assertions, list):
        return ["invalid assertions"]
    for assertion in assertions:
        if not isinstance(assertion, dict):
            return ["invalid assertion"]
        ref = assertion.get("subject") or {}
        actual = subjects.get(ref.get("id"))
        if not actual or assertion.get("subject_kind") != actual.get("kind") or ref.get("kind") != actual.get("kind"):
            return ["unresolved or conflicting assertion subject"]
        if any(ref.get(key) is not None and ref.get(key) != actual.get(key) for key in ("label", "parent_subject_id")):
            return ["conflicting inline assertion subject"]
    return []


def grade(case, state, error=None, equivalent=False):
    """equivalent=True applies the documented E1–E3 equivalences; the default is the strict official grade."""
    if error or state is None:
        return {"status": "runtime_error", "failures": [error or "state is missing"], "checked": 0}
    if "text" in case and (state.get("text") != case["text"]
            or state.get("original_text", state.get("text")) != case["text"]):
        return {"status": "runtime_error", "failures": ["original input changed"], "checked": 0}
    invalid_graph = graph_errors(state)
    if invalid_graph:
        return {"status": "runtime_error", "failures": invalid_graph, "checked": 0}
    expected, failures, checked = case["expected"], [], 0
    assertions = state.get("assertions", [])
    if "domain" in expected:
        checked += 1
        if state.get("domain") != expected["domain"]:
            failures.append("domain")
    for key in ("required", "forbidden"):
        for index, fact in enumerate(expected.get(key, [])):
            checked += 1
            # An unsupported claim is still forbidden. Missing evidence cannot hide it.
            predicate = fact if key == "required" else {k: v for k, v in fact.items() if k != "quote"}
            found = any(match_fact(predicate, a, state, equivalent) for a in assertions)
            if found != (key == "required"):
                failures.append(f"{key}[{index}]: " + ("missing fact" if key == "required" else "forbidden fact"))
    for quote in expected.get("preserve_quotes", []):
        checked += 1
        if not _retained(quote, state, equivalent):
            failures.append(f"fragment not retained: {quote}")
    for key, violation in (("no_final", state.get("final") is not None),
                           ("no_assertions", any(a.get("status") != "rejected" and (a.get("value") or {}).get("type") != "unparsed"
                                                 for a in assertions)),
                           ("clarification_required", state.get("clarification") is None)):
        if expected.get(key):
            checked += 1
            if violation:
                failures.append(key)
    return {"status": "fail_checks" if failures else "pass_checks" if checked else "unassessed",
            "failures": failures, "checked": checked}


def load_dataset(directory=DATA, bank_directory=BANK):
    directory = Path(directory)
    try:
        raw = (directory / "cases.jsonl").read_bytes()
        manifest_raw = (directory / "manifest.json").read_bytes()
        manifest = json.loads(manifest_raw)
        dataset = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        bank = load_bank(bank_directory)
        artifacts = {name: (directory / name).read_bytes() for name in ARTIFACTS}
        selection, dialogues = (json.loads(artifacts[name]) for name in ("selection.json", "dialogues.json"))
        json.loads(artifacts["curation-audit.json"])
    except (OSError, ValueError, UnicodeError):
        return [], {}, ["Frozen files or source bank are missing, malformed or unreadable"]
    errors = validate(dataset, manifest, bank)
    if not isinstance(manifest, dict):
        return [], {}, errors
    if manifest.get("schema_version") == "adversarial-golden-1" and any(
            manifest.get(key) != value for key, value in (("total", 300), ("single", 240), ("dialogue", 60))):
        errors.append("adversarial-golden-1 release requires 300 records: 240 single and 60 dialogue turns")
    if digest(raw) != manifest.get("case_sha256"):
        errors.append("frozen case file hash differs")
    hashes = manifest.get("artifact_sha256")
    if not isinstance(hashes, dict) or set(hashes) != set(ARTIFACTS):
        errors.append("frozen artifacts must use the declared fixed whitelist")
        hashes = hashes if isinstance(hashes, dict) else {}
    for name in ARTIFACTS:
        if digest(artifacts[name]) != hashes.get(name):
            errors.append(f"frozen {name} hash differs")
    if not isinstance(selection, dict) or selection.get("groups", selection) != manifest.get("groups"):
        errors.append("selection groups differ from manifest")
    if manifest.get("schema_version") == "judging-golden-2":
        if isinstance(selection, dict) and selection.get("case_ids") != manifest.get("case_ids"):
            errors.append("selection case IDs differ from manifest")
    errors.extend(_dialogue_errors(dialogues, dataset))
    # Carry the exact parsed plan bytes already checked above into execution.
    return dataset, {**manifest, "manifest_sha256": digest(manifest_raw), "validated_dialogues": dialogues}, errors


def _dialogue_errors(dialogues, dataset):
    if not isinstance(dialogues, dict) or not isinstance(dialogues.get("scenarios"), list):
        return ["dialogue plans need a scenarios list"]
    source, records = {}, {}
    for row in dataset:
        if isinstance(row, dict) and str(row.get("case_id", "")).startswith("DLG-"):
            source.setdefault(row.get("group_id"), []).append(row["case_id"])
            records[row["case_id"]] = row
    errors, seen = [], set()
    for plan in dialogues["scenarios"]:
        if not isinstance(plan, dict) or not isinstance(plan.get("group_id"), str):
            errors.append("invalid dialogue plan")
            continue
        group = plan["group_id"]
        if group in seen or group not in source or plan.get("case_ids") != source.get(group):
            errors.append(f"{group}: duplicate, missing or mismatched dialogue IDs")
        seen.add(group)
        steps = plan.get("steps")
        if not isinstance(steps, list) or len(steps) != 5:
            errors.append(f"{group}: five planned steps required")
            continue
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or not isinstance(step.get("operation"), dict):
                errors.append(f"{group}: invalid operation")
                continue
            members = source.get(group, [])
            action = step["operation"].get("action")
            if (index >= len(members) or step.get("case_id") != members[index] or step.get("turn") != index + 1
                    or not isinstance(action, str) or action not in {"parse", "edit", "answer", "undo", "replay"}):
                errors.append(f"{group}: invalid ordered operation")
                continue
            original = records[step["case_id"]]
            for key, field in (("raw_input", "text"), ("source_precondition", "precondition"),
                               ("source_must", "must"), ("source_forbidden", "forbidden")):
                if step.get(key) != original.get(field):
                    errors.append(f"{group}: dialogue source {key} differs")
            operation = step["operation"]
            if not isinstance(operation.get("request_id_alias"), str) or not operation["request_id_alias"]:
                errors.append(f"{group}: request alias missing")
            if action == "parse" and operation.get("text") != original["text"]:
                errors.append(f"{group}: parse text differs from source")
            if action == "replay" and (not isinstance(operation.get("saved_request"), str)
                    or type(operation.get("same_payload")) is not bool
                    or ("override" in operation and not isinstance(operation["override"], dict))):
                errors.append(f"{group}: invalid exact replay operation")
    if seen != set(source):
        errors.append("dialogue plans do not cover all selected groups")
    return errors


def evaluate(dataset, pipeline, with_model=False, dialogues=None):
    """Run source-only inputs. Stateful scenarios are kept as scenarios, not new descriptions."""
    if not dataset:
        return {"mode": "golden", "total": 0, "passed": False, "errors": ["Golden Set must not be empty"],
                "statuses": {}, "cases": [], "checked": 0, "full_behavior_assessed": 0,
                "independent_acceptance": False}
    dialogue_rows = {}
    if dialogues:
        from golden_dialogues import run_scenario  # noqa: PLC0415 - avoids a module cycle with the shared matcher
        for plan in dialogues["scenarios"]:
            for row in run_scenario(plan, pipeline):
                dialogue_rows[row["case_id"]] = row
    rows = []
    for case in dataset:
        common = {"case_id": case["case_id"], "group_id": case["group_id"], "domain": case["domain"],
                  "tags": case["tags"], "full_behavior_status": "unassessed",
                  "stage": "parse+analyze" if with_model else "parse_rules_only"}
        if case.get("annotation_status") == "needs_adjudication":
            rows.append({**common, "stage": "not_executed", "status": "needs_adjudication", "checked": 0, "failures": []})
            continue
        if case["case_id"] in dialogue_rows:
            rows.append({**common, **dialogue_rows[case["case_id"]]})
            continue
        combo = None
        if case["case_id"].startswith("COMBO-"):
            from golden_combos import run_combo  # noqa: PLC0415 - avoids a module cycle with the shared grader
            combo = run_combo(case, pipeline, with_model)
        if combo is not None:
            rows.append({**common, **combo})
            continue
        if case["case_id"].startswith("DLG-") or case["precondition"] != "-":
            rows.append({**common, "stage": "not_executed", "status": "fixture_missing", "checked": 0, "failures": [],
                         "reason": "Requires its declared fixture; never substitute a fresh standalone session"})
            continue
        started, state, error = time.perf_counter(), None, None
        try:
            state = pipeline.parse(C.ParseRequest(request_id=uuid.uuid4().hex, expected_revision=0, text=case["text"]))
            if with_model:
                state = pipeline.analyze(C.AnalyzeRequest(session_id=state.session_id, request_id=uuid.uuid4().hex,
                                                         expected_revision=state.revision))
                if state.status != "analyzed" or any(d.kind == "model_unavailable" for d in state.diagnostics):
                    error = "model extraction unavailable or incomplete"
            state = state.model_dump(mode="json")
        except Exception as exc:
            error = f"{type(exc).__name__}: request failed"  # Never echo provider/URL/credential payloads.
        rows.append({**common, **grade(case, state, error), "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                     "state": state, "precondition_status": "not_reproduced" if case["precondition"] != "-" else "none"})
    statuses = dict(Counter(row["status"] for row in rows))
    by_section = {}
    for prefix in sorted({r["case_id"].split("-")[0] for r in rows}):
        subset = [r for r in rows if r["case_id"].startswith(prefix + "-")]
        by_section[prefix] = {"total": len(subset), "statuses": dict(Counter(r["status"] for r in subset))}
    return {"mode": "golden", "stage": "parse+analyze" if with_model else "parse_rules_only", "total": len(rows),
            "dialogue_stage": "dialogue_rules_only" if dialogues else "not_executed",
            "statuses": statuses, "by_section": by_section, "checked": sum(r["checked"] for r in rows),
            "full_behavior_assessed": 0, "exact_code_accuracy": None, "independent_acceptance": False,
            "evidence_note": "pass_checks covers explicit anchors only. Missing fixtures and manual clauses are unassessed.",
            "snapshot_id": pipeline.catalog.snapshot_id, "cases": rows}
