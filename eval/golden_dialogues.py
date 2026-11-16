"""Execute reproducible dialogue operations; retain every unsupported turn.

Saved requests are replayed byte-for-byte at the contract level. A cached response
is graded separately from the current server state. Browser behavior is unassessed.
"""

import re
import time
import uuid

import golden

from src.ktru import contracts as C
from src.ktru.service.errors import ServiceError

PREDICATES = {"assertion", "exact_cached_response", "current_server_state_equals",
              "cached_response_promoted_to_current_state", "subject", "active_subject", "final",
              "final_without_exhaustive_scope", "confirmed_family_from_prior_subject", "request_description",
              "invented_product", "conditional_assertion_preserved"}
FIXTURES = {"read_current_session_state", "client_response_revision_guard", "dynamic_current_field",
            "subject_name_edit_action", "assertion_subject_move_action", "assertion_modality_edit_action",
            "remove_field_value_action", "targeted_presence_correction_action"}
MISSING = "Operation requires its declared stateful fixture"


def _dump(card):
    return card.model_dump(mode="json") if card is not None else None


def _norm(text):
    return " ".join(str(text or "").casefold().replace("ё", "е").split())


def _label_match(aliases, label):
    normalized = _norm(label)
    return any(re.search(r"(?<!\w)" + re.escape(_norm(alias)) + r"(?!\w)", normalized) for alias in aliases)


def _subject_matches(selector, subject, current):
    kind = selector.get("subject_kind")
    if kind and (subject is None or subject.kind != kind):
        return False
    aliases, parents = selector.get("subject_label_aliases"), selector.get("parent_label_aliases")
    if not aliases and not parents:
        return True
    if subject is None:
        return False
    nodes = current.subjects if current is not None else []
    actual = next((node for node in nodes if node.id == subject.id), subject)
    if aliases and not _label_match(aliases, actual.label or ""):
        return False
    if parents:
        parent = next((node for node in nodes if node.id == actual.parent_subject_id), None)
        if parent is None or not _label_match(parents, parent.label or ""):
            return False
    return True


def _field_matches(selector, field, current):
    aliases = selector.get("field_label_aliases")
    if aliases and not _label_match(aliases, field.label_ru):
        return False
    return _subject_matches(selector, field.subject, current)


def _current_question(operation, current):
    """Return the declared question only when the live card already contains it."""
    selector = operation.get("question_selector")
    question = current.clarification if current is not None else None
    if question is None or not isinstance(selector, dict):
        return None
    kind = selector.get("kind")
    if kind and question.kind != kind:
        return None
    aliases = selector.get("char_name_aliases")
    if aliases and not (question.char_name and _label_match(aliases, question.char_name)):
        return None
    if not _subject_matches(selector, question.subject, current):
        return None
    count = selector.get("match_count")
    if count is not None and count != 1:
        return None
    return question


def _one(matches, what):
    if len(matches) == 1:
        return matches[0], None
    return None, f"Declared {what} is " + ("not present in" if not matches else "ambiguous in") + " the current card"


def _rename_patch(operation, current):
    selector, label = operation["subject_selector"], operation.get("new_label")
    if not isinstance(selector, dict) or not isinstance(selector.get("label"), str) or not isinstance(label, str) \
            or not label:
        return None, MISSING
    subject, reason = _one([node for node in current.subjects if node.kind == selector.get("kind")
                            and node.label and _label_match([selector["label"]], node.label)], "subject")
    return (C.RenameSubjectPatch(subject_id=subject.id, label=label), None) if subject else (None, reason)


def _modality(operation, assertion_id):
    changes = {key: operation[f"new_{key}"] for key in ("status", "polarity", "operator", "value")
               if f"new_{key}" in operation}
    if not changes:
        return None, MISSING
    return C.AssertionModalityPatch(assertion_id=assertion_id, **changes), None


def _assertion_patch(operation, current):
    selector = operation["assertion_selector"]
    if not isinstance(selector, dict) or selector.get("kind") != "assertion":
        return None, MISSING
    if "extra_fact" in operation:
        return None, "Operation adds a new fact together with the correction; no single edit supports it"
    state = _dump(current)
    assertion, reason = _one([a for a in state["assertions"] if a["status"] != "rejected"
                              and golden.match_fact(_fact(selector, False), a, state)], "assertion")
    if assertion is None:
        return None, reason
    target = operation.get("new_subject")
    if isinstance(target, dict):
        if target.get("kind") not in {"item", "part", "package", "set"} or not isinstance(target.get("label"), str):
            return None, MISSING
        return C.MoveAssertionPatch(assertion_id=assertion["id"], subject_kind=target["kind"],
                                    subject_label=target["label"]), None
    return _modality(operation, assertion["id"])


def _absent_item_field(selector, label, current):
    """CONSTRAINTS: nothing the user states is thrown away (KTRU field → Portal field → new field). A value for an
    item field the current card does not show yet is set by its declared label; the product picks the source.
    Part fields and ambiguous selectors stay blocked: a label alone cannot name their object."""
    if selector.get("subject_kind", "item") != "item" or not isinstance(label, str) or not label:
        return None
    if any(_field_matches(selector, field, current) for field in current.fields):
        return None
    return label


def _field_operation(operation, current):
    """A field edit without a saved patch: remove the value, set a yes/no presence, or restate its modality."""
    selector = operation["field_selector"]
    field, reason = _one([field for field in current.fields if _field_matches(selector, field, current)], "field")
    aliases = selector.get("field_label_aliases") or []
    absent = _absent_item_field(selector, aliases[0] if len(aliases) == 1 else None, current)
    if field is None and absent and isinstance(operation.get("value"), bool):
        return C.SetFieldPatch(label_ru=absent, value="Да" if operation["value"] else "Нет"), None
    if field is None:
        return None, reason
    if isinstance(operation.get("value"), bool):
        return C.SetFieldPatch(label_ru=field.label_ru, value="Да" if operation["value"] else "Нет",
                               field_id=field.id), None
    if any(key.startswith("new_") for key in operation):
        if len(field.assertion_ids) != 1:
            return None, "Declared field has no single reading to restate"
        return _modality(operation, field.assertion_ids[0])
    if str(operation.get("intent", "")).startswith("remove") or "remove_field_value_action" in operation.get(
            "fixture_requirements", []):
        if operation.get("also_unset_variant_confirmation"):
            return None, "Product variants are not part of the card"
        return C.RemoveFieldPatch(field_id=field.id), None
    return None, MISSING


def _edit_patch(operation, current):
    """Map a saved edit onto a contract patch, or explain why the live card cannot satisfy it."""
    if "subject_selector" in operation:
        return _rename_patch(operation, current)
    if "assertion_selector" in operation:
        return _assertion_patch(operation, current)
    if "patch" not in operation and isinstance(operation.get("field_selector"), dict):
        return _field_operation(operation, current)
    patch = operation.get("patch")
    if not isinstance(patch, dict):
        return None, "Operation requires its declared stateful fixture"
    if patch.get("type") == "replace_text":
        if isinstance(patch.get("text"), str) and patch["text"]:
            return C.ReplaceTextPatch(text=patch["text"]), None
        return None, "Operation requires its declared stateful fixture"
    if patch.get("type") != "set_field" or not isinstance(patch.get("value"), str) or not patch["value"]:
        return None, "Operation requires its declared stateful fixture"
    label = patch.get("label_ru")
    if not isinstance(label, str) or not label:
        return None, "Operation requires its declared stateful fixture"
    selector = operation.get("field_selector")
    if not isinstance(selector, dict):
        selector = {"field_label_aliases": [label]}
    matches = [field for field in (current.fields if current is not None else [])
               if _field_matches(selector, field, current)]
    if len(matches) == 1:
        return C.SetFieldPatch(label_ru=matches[0].label_ru, value=patch["value"], field_id=matches[0].id), None
    if_absent = selector.get("if_absent")
    if isinstance(if_absent, dict) and if_absent.get("add_unbound_field") is True:
        unbound = if_absent.get("label_ru")
        if isinstance(unbound, str) and unbound:
            return C.SetFieldPatch(label_ru=unbound, value=patch["value"]), None
        return None, "Declared unbound field label is missing"
    absent = _absent_item_field(selector, label, current)
    if absent:
        return C.SetFieldPatch(label_ru=absent, value=patch["value"]), None
    if not matches:
        return None, "Declared field is not present in the current card"
    return None, "Declared field selector is ambiguous in the current card"


def _unavailable(step, requests, responses, current):
    operation = step["operation"]
    action = operation.get("action")
    if action not in {"parse", "replay", "edit", "undo", "answer"}:
        return "Operation requires its declared stateful fixture"
    if action == "answer" and ("question_selector" in operation or "question_id_source" in operation):
        if _current_question(operation, current) is None:
            return "Declared question is not present in the current card"
    if set(step.get("fixture_requirements", [])) - FIXTURES:
        return "Declared fixture is not implemented"
    predicates = [*step.get("required_state", []), *step.get("forbidden_state", [])]
    if any(p.get("kind") not in PREDICATES for p in predicates):
        return "Declared semantic predicate is not implemented"
    # An edit may introduce the reviewed value, so its quote is anchored by the graded check after execution.
    if action in {"parse", "replay"}:
        source = operation.get("text", "") if action == "parse" else current.text if current else ""
        if any(p.get("kind") == "assertion" and (not isinstance(p.get("quote"), str) or not p["quote"] or p["quote"] not in source)
               for p in step.get("required_state", [])):
            return "Reviewed source quote fixture is unavailable"
    if action == "replay" and operation.get("saved_request") not in requests:
        return "Exact saved request is unavailable"
    aliases = [p.get("saved_response") for p in predicates if "saved_response" in p]
    aliases += [operation[k] for k in ("compare_response_to", "current_state_must_equal") if k in operation]
    if any(alias not in responses for alias in aliases):
        return "Saved response fixture is unavailable"
    if operation.get("session_id_source") == "current_card" and current is None:
        return "Current session is unavailable"
    if action in {"edit", "undo"} and current is None:
        return "Current session is unavailable"
    if action == "edit":
        _, reason = _edit_patch(operation, current)
        if reason:
            return reason
    if action == "answer":
        return "Operation requires its declared stateful fixture"
    return None


def _request(operation, requests, current, prefix):
    action = operation["action"]
    if action == "replay":
        request = requests[operation["saved_request"]].model_copy(deep=True)
        if not operation.get("same_payload", True):
            payload = {**request.model_dump(mode="json"), **operation.get("override", {})}
            request = C.ParseRequest.model_validate(payload)
        return request
    request_id = prefix + operation["request_id_alias"]
    session_source = operation.get("session_id_source")
    if action in {"edit", "undo"} and session_source in {None, "current_card"} and current is not None:
        session = current.session_id
    else:
        session = current.session_id if session_source == "current_card" else operation.get("session_id")
    revision_source = operation.get("revision_source")
    if action in {"edit", "undo"} and revision_source in {None, "current_card"} and current is not None:
        revision = current.revision
    else:
        revision = current.revision if revision_source == "current_card" else operation.get("expected_revision", 0)
    if action == "edit":
        patch, reason = _edit_patch(operation, current)
        if reason:
            raise ValueError(reason)
        return C.EditRequest(request_id=request_id, session_id=session, expected_revision=revision, patch=patch)
    if action == "undo":
        return C.UndoRequest(request_id=request_id, session_id=session, expected_revision=revision)
    return C.ParseRequest(request_id=request_id, session_id=session, expected_revision=revision, text=operation["text"])


def _dispatch(pipeline, request):
    if isinstance(request, C.EditRequest):
        return pipeline.edit(request)
    if isinstance(request, C.UndoRequest):
        return pipeline.undo(request)
    return pipeline.parse(request)


def _fact(predicate, require_grounding):
    subject = {"kind": predicate["subject_kind"]}
    for old, new in (("subject_label_aliases", "label"), ("parent_label_aliases", "parent_label")):
        if old in predicate:
            subject[new] = predicate[old]
    fact = {"property": predicate["property_aliases"], "subject": subject,
            "status": [predicate.get("status", "supported")],
            **{key: predicate[key] for key in ("value", "operator", "polarity") if key in predicate}}
    # A required edit predicate may omit the quote: the saved plan has no pre-edit source for the new value,
    # while a declared quote is still enforced against the post-execution card.
    if require_grounding and "quote" in predicate:
        fact = {**fact, "quote": predicate["quote"]}
    return fact


def _assertion_matches(predicate, current, require_grounding):
    if current is None:
        return False
    if require_grounding and "quote" in predicate and (not isinstance(predicate["quote"], str) or not predicate["quote"]):
        return False
    fact = _fact(predicate, require_grounding)
    return any(golden.match_fact(fact, assertion, current)
               for assertion in current.get("assertions", []))


def _subjects(current, kind, label):
    return [node for node in (current or {}).get("subjects", [])
            if node["kind"] == kind and node.get("label") and _label_match([label], node["label"])]


def _confirmed(card):
    scope = card.get("search_scope") or {}
    return bool((card.get("family") or {}).get("confirmed_by_text")
                or scope.get("coverage_status") in {"family_confirmed", "scope_exhaustive"})


def _semantic_matches(predicate, current, before):
    """Card-level predicates of the saved plans; each is read from the contract fields it names."""
    kind, current = predicate["kind"], current or {}
    if kind == "subject":
        return bool(_subjects(current, predicate["subject_kind"], predicate["label"]))
    if kind == "active_subject":
        ids = {node["id"] for kind_ in ("item", "part", "package", "set")
               for node in _subjects(current, kind_, predicate["label"])}
        return any(a["status"] != "rejected" and (a.get("subject") or {}).get("id") in ids
                   for a in current.get("assertions", []))
    if kind == "final":
        return current.get("final") is not None
    if kind == "final_without_exhaustive_scope":
        return current.get("final") is not None and (current.get("search_scope") or {}).get(
            "coverage_status") != "scope_exhaustive"
    if kind == "confirmed_family_from_prior_subject":
        prior = (before or {}).get("family")
        return bool(prior and current.get("family") and current["family"]["id"] == prior["id"] and _confirmed(current))
    if kind == "request_description":
        # No product is proposed and the card blocks on the missing product name.
        asks = any(d["kind"] == "out_of_scope" and d["severity"] == "blocker" for d in current.get("diagnostics", [])) \
            or (current.get("clarification") or {}).get("kind") == "family"
        return asks and not any(current.get(key) for key in ("family", "final", "suggestion"))
    if kind == "invented_product":
        # A proposed product, or a product name that the text does not contain.
        text = golden._stems(current.get("text", ""))
        named = [node["label"] for node in current.get("subjects", []) if node["kind"] == "item" and node.get("label")]
        return any(current.get(key) for key in ("family", "final", "suggestion")) or any(
            not golden._stems(label) <= text for label in named)
    # conditional_assertion_preserved: the conditional reading is kept, but not as a supported fact.
    fact = {"property": [predicate["property"]], "subject": {"kind": "part", "label": [predicate["part"]]},
            "status": ["ambiguous"], "value": {"type": "enum", "label": predicate["value"]}}
    return any(golden.match_fact(fact, a, current) for a in current.get("assertions", []))


def _predicate_matches(predicate, response, current, before, responses, require_grounding):
    kind = predicate["kind"]
    if kind == "assertion":
        return _assertion_matches(predicate, current, require_grounding)
    if kind not in {"exact_cached_response", "current_server_state_equals", "cached_response_promoted_to_current_state"}:
        return _semantic_matches(predicate, current, before)
    if kind == "exact_cached_response":
        return response == responses[predicate["saved_response"]]
    if kind == "current_server_state_equals":
        return current == responses[predicate["saved_response"]]
    return current == response and current != before


def _checks(step, response, current, before, responses, error):
    checked, failures = 1, []
    expected = step.get("expected_error")
    if expected:
        if not isinstance(error, ServiceError) or (error.status, error.body.code) != (expected["http_status"], expected["code"]):
            failures.append("expected error status/code differs")
    elif error:
        failures.append("unexpected request error")
    delta = step.get("revision_delta")
    if delta is not None:
        checked += 1
        old = before["revision"] if before else 0
        if current is None or current["revision"] - old != delta:
            failures.append("server revision delta differs")
    for key in ("required_state", "forbidden_state"):
        for index, predicate in enumerate(step.get(key, [])):
            checked += 1
            found = _predicate_matches(predicate, response, current, before, responses, key == "required_state")
            if found != (key == "required_state"):
                failures.append(f"{key}[{index}] differs")
    operation = step["operation"]
    for key, value in (("compare_response_to", response), ("current_state_must_equal", current)):
        if key in operation:
            checked += 1
            if value != responses[operation[key]]:
                failures.append(key + " differs")
    return {"status": "fail_checks" if failures else "pass_checks", "checked": checked, "failures": failures}


def run_scenario(plan, pipeline):
    """Return a row for every turn, including blocked successors of a missing fixture."""
    requests, responses, rows = {}, {}, []
    current, blocked = None, None
    prefix = uuid.uuid4().hex + "-"
    for step in plan["steps"]:
        common = {"case_id": step["case_id"], "group_id": plan["group_id"], "domain": plan["domain"],
                  "turn": step["turn"], "full_behavior_status": "unassessed", "stage": "dialogue_rules_only"}
        blocked = blocked or _unavailable(step, requests, responses, current)
        if blocked:
            rows.append({**common, "status": "fixture_missing", "checked": 0, "failures": [], "reason": blocked})
            continue
        started = time.perf_counter()
        before, response, error = _dump(current), None, None
        try:
            request = _request(step["operation"], requests, current, prefix)
            try:
                response = _dispatch(pipeline, request)
            except ServiceError as exc:
                error = exc
            session = response.session_id if response is not None else current.session_id if current else None
            current = pipeline.get(session) if session else None
            operation = step["operation"]
            if response is not None:
                if "save_exact_request_as" in operation:
                    requests = {**requests, operation["save_exact_request_as"]: request.model_copy(deep=True)}
                if "save_response_as" in operation:
                    responses = {**responses, operation["save_response_as"]: _dump(response)}
            result = _checks(step, _dump(response), _dump(current), before, responses, error)
            failures = [*result["failures"], *golden.graph_errors(_dump(current) or {})]
            accepted_text = request.text if isinstance(request, C.ParseRequest) else None
            if response is not None and accepted_text is not None and (response.text != accepted_text
                    or _dump(response).get("original_text", response.text) != accepted_text):
                failures.append("response original input changed")
            if response is not None and operation["action"] == "parse" and current.text != request.text:
                failures.append("current server input differs from accepted parse")
            if error and before != _dump(current):
                failures.append("rejected request changed server state")
            if failures:
                result = {**result, "status": "fail_checks", "failures": failures}
        except Exception as exc:
            blocked = "Previous request or fixture could not be reproduced"
            result = {"status": "runtime_error", "checked": 0, "failures": [f"{type(exc).__name__}: dialogue request failed"]}
        rows.append({**common, **result, "state": _dump(current), "response": _dump(response),
                     "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
    return rows
