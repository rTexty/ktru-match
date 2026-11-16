from __future__ import annotations

import re
from dataclasses import replace

from src.ktru import contracts as C
from src.ktru.catalog.reader import FamilyHit
from src.ktru.engine import assertions as A
from src.ktru.engine import canonical
from src.ktru.engine import overrides as O
from src.ktru.engine.adapters import EngineDiagnostics, EngineQuestions, EngineReadiness, EngineResolver
from src.ktru.engine.binding import _user_assertions, bind_facts
from src.ktru.engine.compatibility import check
from src.ktru.engine.fields import build_fields
from src.ktru.engine.identity import _id
from src.ktru.engine.questions import question_budget_spent
from src.ktru.engine.refs import _fam_summary
from src.ktru.engine.source_map import search_mapping, stamp_corrections, stamp_diagnostics, validate_source_map
from src.ktru.engine.stage_types import Bound, BoundFacts, FieldResult, Inputs, Outcome
from src.ktru.engine.text import content_stems, norm, stems_match
from src.ktru.engine.values import (
    Interval,
    _assertion_text,
    value_display,
)

__all__ = ["Inputs", "Outcome", "Interval", "value_display", "check", "resolve"]

def _text_domain(cat, text: str) -> str | None:
    """Domain of the product the text names when no catalog family carries it (secateurs, a spray gun)."""
    guess = getattr(cat, "guess_domain", None)
    return guess(text) if guess else None


def resolve(cat: C.CatalogReader, inp: Inputs, session_id: str, request_id: str, *,
            resolver: C.Resolver | None = None, question_selector: C.QuestionSelector | None = None, diagnostics: C.Diagnostics | None = None,
            readiness: C.Readiness | None = None) -> Outcome:  # noqa: C901
    resolver = resolver or EngineResolver()
    question_selector = question_selector or EngineQuestions()
    diagnostics = diagnostics or EngineDiagnostics()
    readiness = readiness or EngineReadiness()
    rev = inp.revision
    diags: list[C.Diagnostic] = []
    corrections: list[C.Correction] = []
    text = inp.text
    work = text
    if inp.retrieval is not None:
        work = inp.retrieval.normalized_text or text
        corrections = list(inp.retrieval.corrections)
        diags = stamp_diagnostics(inp.retrieval.diagnostics, cat.snapshot_id, rev, unscoped=True)
        search_text = inp.retrieval.search_text or work
        correction_map = list(inp.retrieval.correction_map)
        validate_source_map(text, search_text, correction_map)
        hits = inp.retrieval.families
        examined = inp.retrieval.scope.examined_count
        truncated = inp.retrieval.scope.retrieval_truncated
        truncation_reasons = inp.retrieval.scope.truncation_reasons
    else:
        mapped = search_mapping(text, cat.correct_word, snapshot_id=cat.snapshot_id, text_revision=rev)
        work = mapped.normalized_text
        corrections, diags = mapped.corrections, mapped.diagnostics
        search_text, correction_map = mapped.search_text, mapped.correction_map
        retrieval = cat.search(search_text)
        hits, examined = retrieval
        row_truncated = getattr(retrieval, "row_truncated", examined >= 400)
        family_truncated = getattr(retrieval, "family_truncated", False)
        truncated = row_truncated or family_truncated
        truncation_reasons = (["position_limit"] if row_truncated else []) + (["family_limit"] if family_truncated else [])
    corrections = stamp_corrections(corrections, rev)
    if inp.extraction is not None:
        independent = list(inp.extraction.assertions)
    else:
        independent = A.rule_assertions(text, rev)
        if inp.llm and inp.llm.get("facts"):
            independent = A.merge([*independent, *A.model_assertions(inp.llm["facts"], text, rev)])
    independent = _superseded(text, A.merge([_with_modality(text, a) for a in independent]))
    if inp.llm and inp.llm.get("error"):
        diags.append(C.Diagnostic(id=_id("llm", rev), kind="model_unavailable", severity="warning",
            message_ru="Модель извлечения недоступна: показан результат правил",
            reason_ru="Не удалось выполнить анализ открытой моделью. Повторите анализ позже."))
    fam_answer = next((a for a in reversed(inp.answers) if a["kind"] == "family"), None)
    family: FamilyHit | None = None
    confirmed = False
    confirm_reason = ""
    if fam_answer:
        template, fam_members = cat.family(fam_answer["family_id"])
        if fam_members:
            family = next((h for h in hits if h.family_id == fam_answer["family_id"]), None) or FamilyHit(
                fam_answer["family_id"], (template or fam_members[0]).name, template, fam_members, 0, 0, 0, True
            )
            confirmed, confirm_reason = True, "Семейство подтверждено пользователем"
    elif hits:
        top = hits[0]
        # Lexical rank proposes a family; so does the semantic index's confident first choice («шкаф для химикатов»).
        # Neither proves catalogue-wide uniqueness: the family stays a proposal until named or confirmed.
        if top.head_ok or getattr(top, "semantic_pick", False):
            family = top
            # A position-limited search may hide a more specific family: then the user confirms.
            named = None if "position_limit" in truncation_reasons else _named_family(
                hits, getattr(cat, "product_segment", lambda t: t)(text), text)
            if named is not None:
                family, confirmed = named, True
                confirm_reason = f"Семейство названо в описании полностью: «{named.name}»"
            elif getattr(top, "outside_group", None):
                family = None  # «Дрель хирургическая»: the whole catalog has a clearly nearer family outside the groups
    # The described product is named once, after the family is known: «стола» reads as the catalog's «стол».
    # A product name the user corrected wins over the name read from the text; several products name the root.
    renamed = O.renamed_item(inp.edits)
    product = renamed or A.root_label(text) or canonical.product_label(
        text, family.name if family else (hits[0].name if hits else None))
    independent = O.apply(A.label_product(independent, product), inp.edits)
    total_finals = cat.manifest["counts"]["final"]
    catalog_scope = C.FunnelScope(id=_id("scope", cat.snapshot_id, "catalog"), snapshot_id=cat.snapshot_id,
        kind="catalog", count=total_finals, membership_complete=True)
    funnel_scopes = [catalog_scope]
    query_evidence = [C.TextEvidence(span=C.Span(start=0, end=len(text), text=text, text_revision=rev))]
    funnel = [C.FunnelStage(label_ru="Конечных позиций КТРУ в снимке", count=total_finals, trigger="retrieval",
                           scope_id=catalog_scope.id, evidence=query_evidence)]
    if hits:
        retrieved_scope = C.FunnelScope(id=_id("scope", cat.snapshot_id, "retrieved", *sorted(h.family_id for h in hits)),
            snapshot_id=cat.snapshot_id, kind="retrieved_families", family_ids=[h.family_id for h in hits],
            count=sum(len(h.members) for h in hits), membership_complete=all(h.membership_complete for h in hits))
        funnel_scopes.append(retrieved_scope)
        funnel.append(
            C.FunnelStage(
                label_ru=f"В найденных семействах ({len(hits)})",
                count=sum(len(h.members) for h in hits),
                trigger="retrieval",
                scope_id=retrieved_scope.id,
                evidence=query_evidence,
            )
        )
    fam_id = family.family_id if family else None
    families_top = [_fam_summary(cat, h, confirmed and h.family_id == fam_id) for h in hits[:5]]
    state: dict = {
        "session_id": session_id,
        "revision": rev,
        "request_id": request_id,
        "snapshot_id": cat.snapshot_id,
        "status": inp.extraction.status if inp.extraction else ("analyzed" if inp.llm else "provisional"),
        "text": text,
        "normalized_text": work if work != text else None,
        "corrections": corrections,
        "search_text": search_text if search_text != text else None,
        "correction_map": correction_map,
        "funnel_scopes": funnel_scopes,
        "families_top": families_top,
        "answers_count": len(inp.answers),
        "model_manifests": ([inp.extraction.model_manifest] if inp.extraction and inp.extraction.model_manifest else [])
            + (inp.retrieval.model_manifests if inp.retrieval else []),
    }
    pending: dict[str, dict] = {}

    if family is None:
        user, _ = _user_assertions(inp, {}, {})
        user = [Bound(a, None) for a in O.apply([b.assertion for b in user], inp.edits)]
        edited_keys = {(A.property_key(b.assertion), b.assertion.subject.id) for b in user}
        superseded_open = [a.model_copy(update={"status": "rejected", "note_ru": "Заменено правкой пользователя"})
            for a in independent if a.status != "rejected" and (A.property_key(a), a.subject.id) in edited_keys]
        independent = [a for a in independent if a.status == "rejected" or (A.property_key(a), a.subject.id) not in edited_keys]
        independent.extend(b.assertion for b in user)
        # A reading the user removed constrains nothing; it stays visible as rejected.
        superseded_open += [a for a in independent if a.status == "rejected"]
        independent = [a for a in independent if a.status != "rejected"]
        semantic_proposals = any(hit.semantic_score is not None for hit in hits)
        outside = next((hit.outside_group for hit in hits[:1] if getattr(hit, "outside_group", None)), None)
        outside_name = cat.family_names().get(outside) if outside and hasattr(cat, "family_names") else None
        if outside_name:
            message, reason, severity = (
                "Товар, вероятно, не из групп «Одежда», «Мебель», «Инструменты»",
                f"Описание ближе всего к семейству КТРУ вне этих групп: «{outside_name}». Если товар всё же из "
                "этих групп, уточните его название или выберите группу.", "warning")
        elif semantic_proposals:
            message, reason, severity = ("Подтвердите предложенную группу КТРУ",
                "Найдены похожие по смыслу группы товара. Подтвердите группу или уточните описание.", "warning")
        else:
            message, reason, severity = ("Не удалось найти подходящую группу КТРУ",
                "Ни одно семейство каталога не совпадает с описанием достаточно полно. "
                "Это не доказывает, что кода нет: уточните название товара.", "blocker")
        diags.append(C.Diagnostic(id=_id("oos", rev), kind="out_of_scope", severity=severity,
                                  message_ru=message, reason_ru=reason))
        state.update(
            domain=_text_domain(cat, work),
            assertions=[*independent, *superseded_open],
            subjects=list({a.subject.id: a.subject for a in independent if a.subject}.values()),
            fields=[C.CardField(id=_id("f", a.id), label_ru=a.property_raw, source="new", value=a.value,
                display_ru=_assertion_text(a), assertion_ids=[a.id], subject=a.subject, evidence=a.evidence,
                evidence_ru=a.span.text if a.span else None,
                status="unparsed" if isinstance(a.value, C.UnparsedValue) else
                    ("confirmed" if a.origin != "text" else "from_text")) for a in A.visible_assertions(independent)],
            rejected_outputs=list(inp.extraction.rejected_outputs) if inp.extraction else [C.RejectedOutput(**r) for r in (inp.llm or {}).get("rejected", [])],
            status="partial" if inp.llm and inp.llm.get("error") else (inp.extraction.status if inp.extraction else ("analyzed" if inp.llm else "provisional")),
            diagnostics=diags,
            funnel=funnel,
            search_scope=C.SearchScope(
                snapshot_id=cat.snapshot_id, eligible_count=total_finals, examined_count=examined, retained_count=0,
                family_ids=[], family_membership_complete=True, retrieval_truncated=truncated, truncation_reasons=truncation_reasons,
                coverage_status="unconfirmed", reason_ru="Найдены похожие семейства; требуется подтверждение товара"
                    if semantic_proposals else "Подходящие семейства не найдены",
            ),
        )  # fmt: skip
        bound = [Bound(assertion, None) for assertion in independent]
        facts = BoundFacts({}, {}, bound, user, bound, {}, [], set())
        resolution = resolver.resolve(cat, inp, None, facts, False, "", funnel, diags)
        field_result = FieldResult(state["fields"], None, {})
        diags = diagnostics.build(cat, inp, work, None, facts, resolution, field_result).diagnostics
        question = question_selector.choose(cat, inp, None, hits, False, facts, resolution, field_result)
        ready = readiness.evaluate(resolution.final, resolution.alive, diags, field_result.fields)
        state.update(candidates=resolution.result.candidates, bindings=resolution.result.bindings,
                     conflicts=resolution.result.conflicts, final=resolution.final, suggestion=resolution.suggestion,
                     diagnostics=diags, clarification=question.clarification, readiness=ready.readiness,
                     readiness_reasons_ru=ready.reasons_ru)
        return _finish(cat, inp, state, question.pending_options, product)

    members = family.members
    funnel_scopes.append(C.FunnelScope(id=_id("scope", cat.snapshot_id, "family", family.family_id),
        snapshot_id=cat.snapshot_id, kind="family", family_ids=[family.family_id],
        count=len(members), membership_complete=family.membership_complete))
    facts = bind_facts(inp, work, family, independent)
    resolution = resolver.resolve(cat, inp, family, facts, confirmed, confirm_reason, funnel, diags)
    field_result = build_fields(cat, inp, work, family, facts, resolution)
    diags = diagnostics.build(cat, inp, work, family, facts, resolution, field_result).diagnostics
    question = question_selector.choose(cat, inp, family, hits, confirmed, facts, resolution, field_result)
    ready = readiness.evaluate(resolution.final, resolution.alive, diags, field_result.fields)
    pending = question.pending_options
    clar, readiness, reasons = question.clarification, ready.readiness, ready.reasons_ru
    if clar is None and resolution.final is None and len(resolution.alive) > 1 and question_budget_spent(inp):
        # The question budget is spent, but the buyer still gets a concrete next step (task: «предложить конкретные
        # шаги»): the characteristic that would have been asked next, with its catalog values.
        step = question_selector.choose(cat, replace(inp, answers=[]), family, hits, confirmed, facts, resolution,
                                        field_result).clarification
        if step is not None and step.kind == "characteristic":
            values = [o.label_ru for o in step.options if not o.is_dont_know]
            reasons = [*reasons, f"Чтобы определить один код, укажите в описании «{step.char_name}»: "
                       + "; ".join(values[:6]) + ("…" if len(values) > 6 else "")]
    extracted, user, superseded = facts.extracted, facts.user, facts.superseded
    alive, final, suggestion = resolution.alive, resolution.final, resolution.suggestion
    funnel, candidates, fields = resolution.funnel, resolution.result.candidates, field_result.fields
    replaced = {b.assertion.id for b in superseded}
    extracted = [Bound(b.assertion.model_copy(update={"note_ru": "Заменено ответом пользователя", "status": "rejected"})
                       if b.assertion.id in replaced else b.assertion, b.char_name, b.note) for b in extracted]
    state.update(
        rejected_outputs=list(inp.extraction.rejected_outputs) if inp.extraction else [C.RejectedOutput(**r) for r in (inp.llm or {}).get("rejected", [])],
        status="partial" if inp.llm and inp.llm.get("error") else (inp.extraction.status if inp.extraction else ("analyzed" if inp.llm else "provisional")),
        domain=members[0].domain if members[0].domain != "unknown" else _text_domain(cat, work) or "unknown",
        assertions=[b.assertion for b in extracted] + [b.assertion for b in user],
        family=_fam_summary(cat, family, confirmed),
        search_scope=C.SearchScope(
            snapshot_id=cat.snapshot_id,
            eligible_count=total_finals,
            examined_count=examined,
            retained_count=len(alive),
            family_ids=[family.family_id],
            family_membership_complete=True,
            retrieval_truncated=truncated, truncation_reasons=truncation_reasons,
            coverage_status="family_confirmed" if confirmed else "unconfirmed",
            reason_ru=(f"Поиск по названию «{renamed}», исправленному пользователем. " if renamed else "")
            + (confirm_reason or "Семейство выбрано по лучшему совпадению, но не подтверждено")
            + f". Проверены все {len(members)} конечных позиций семейства.",
        ),
        funnel=funnel,
        candidates=candidates,
        final=final,
        suggestion=suggestion,
        fields=fields,
        diagnostics=diags,
        clarification=clar,
        readiness=readiness,
        readiness_reasons_ru=reasons,
    )
    scope = state["search_scope"]
    evidence = [item for answer in user for item in answer.assertion.evidence]
    if fam_answer:
        evidence = [C.UserEvidence(type="user_answer", revision=fam_answer.get("revision", rev),
            question_id=fam_answer.get("question_id"), answer_text=family.name)]
    elif confirmed:
        start = norm(text).find(norm(family.name))
        if start >= 0:
            evidence = [C.TextEvidence(span=C.Span(start=start, end=start + len(family.name),
                text=text[start:start + len(family.name)], text_revision=rev))]
    scope = scope.model_copy(update={"family_membership_complete": family.membership_complete,
                                      "confirmation_evidence": evidence})
    state["search_scope"] = scope
    for name in ("final", "suggestion"):
        if state[name]:
            state[name] = state[name].model_copy(update={"scope": scope})
    assertions = state["assertions"]
    state["subjects"] = list({a.subject.id: a.subject for a in assertions if a.subject}.values())
    state["bindings"] = resolution.result.bindings
    state["conflicts"] = resolution.result.conflicts
    by_id = {a.id: a for a in assertions}
    state["fields"] = [field.model_copy(update={
        "subject": by_id[field.assertion_ids[0]].subject,
        "evidence": field.evidence or [item for ident in field.assertion_ids for item in by_id[ident].evidence],
    }) if field.assertion_ids and field.assertion_ids[0] in by_id else field for field in fields]
    return _finish(cat, inp, state, pending, product)


def _finish(cat: C.CatalogReader, inp: Inputs, state: dict, pending: dict[str, dict],
            product: str | None = None) -> Outcome:
    if product:
        # One item per description: every copy of it (assertions, fields, diagnostics, questions) carries its name.
        for key, value in list(state.items()):
            state[key] = _label_items(value, product)
    subjects = {assertion.subject.id: assertion.subject for assertion in state["assertions"] if assertion.subject}
    root = A.subject_of("").model_copy(update={"label": product})
    named = {subject.id: subject for subject in A.product_subjects(inp.text) or []}
    named[root.id] = root
    missing = [subject.parent_subject_id for subject in subjects.values() if subject.parent_subject_id not in subjects]
    while missing:
        # Parts can be the only stated facts; retain their structural parents (the item, a set) without a quote.
        parent = named.get(missing.pop())
        if parent is not None and parent.id not in subjects:
            subjects[parent.id] = parent
            missing.append(parent.parent_subject_id)
    state["subjects"] = list(subjects.values())
    state["diagnostics"] = stamp_diagnostics(state["diagnostics"], cat.snapshot_id, inp.revision)
    return Outcome(state, pending)


def _with_modality(text: str, assertion: C.Assertion) -> C.Assertion:
    """Hedged, alternative or ordered values stay visible but cannot act as confirmed facts."""
    if assertion.origin != "text" or assertion.span is None or isinstance(assertion.value, C.UnparsedValue):
        return assertion
    kind, reason = canonical.modality(text, assertion.span.start, assertion.span.end)
    if kind is None or assertion.status == "rejected":
        return assertion
    update: dict = {"status": "ambiguous", "note_ru": reason}
    if kind == "uncertain" and assertion.polarity == "positive":
        update["polarity"] = "uncertain"
    return assertion.model_copy(update=update)


def _superseded(text: str, assertions: list[C.Assertion]) -> list[C.Assertion]:
    """An explicit correction («исправляю … на 500 г») replaces earlier values of the same property and subject."""
    corrections = [a for a in assertions if a.origin == "text" and a.span is not None
                   and not isinstance(a.value, C.UnparsedValue) and canonical.correction_at(text, a.span.start)]
    out = []
    for assertion in assertions:
        later = next((c for c in corrections if c is not assertion and assertion.span is not None
                      and c.span.start > assertion.span.end and A.property_key(c) == A.property_key(assertion)
                      and c.subject and assertion.subject and c.subject.id == assertion.subject.id and c.value != assertion.value
                      and not canonical.correction_at(text, assertion.span.start)), None)
        out.append(assertion.model_copy(update={"status": "ambiguous", "note_ru": "Исправлено далее в описании"})
                   if later and assertion.status == "supported" else assertion)
    return out


def _label_items(value, label: str):
    if isinstance(value, C.SubjectRef):
        if value.kind == "item" and value.label is None:
            return value.model_copy(update={"label": label})
        if value.kind == "package" and value.label == A.PACKED:
            return value.model_copy(update={"label": A.packed_label(label)})
        return value
    if isinstance(value, list):
        return [_label_items(item, label) for item in value]
    if isinstance(value, C.Model):
        update = {}
        for name in type(value).model_fields:
            item = getattr(value, name)
            if isinstance(item, (C.Model, list)):
                new = _label_items(item, label)
                if new is not item:
                    update[name] = new
        if isinstance(value, C.Assertion) and value.subject_kind == "item" and value.subject_label is None:
            update["subject_label"] = label
        elif isinstance(value, C.Assertion) and value.subject_kind == "package" and value.subject_label == A.PACKED:
            update["subject_label"] = A.packed_label(label)
        return value.model_copy(update=update) if update else value
    return value


def _named_family(hits: list[FamilyHit], segment: str | None = None, text: str = "") -> FamilyHit | None:
    """The description names one family completely: every word of its name is present, and no other complete
    match is as specific. «Пальто зимнее детское из кожи» names the children's family, not «Пальто зимнее из кожи».
    The words must be in the product segment: «Стул ученический. Тип каркаса: деревянный» does not name «Стул на
    деревянном каркасе». Anything weaker stays a proposal and the user is asked."""
    def within(name_stems: list[str], stems: list[str]) -> bool:
        return all(any(stems_match(a, b) for b in stems) for a in name_stems)

    def phrase(name_stems: list[str]) -> bool:
        """The name as one phrase around its head noun in one clause («Обувь осенне-весенняя с верхом из кожи»)."""
        return any(stems_match(name_stems[0], b) and within(name_stems, stems[max(0, i - 2):i + len(name_stems) + 1])
                   for stems in clauses for i, b in enumerate(stems))

    said = content_stems(segment) if segment is not None else None
    # A property value that names the product class also counts: «Виды обуви: Обувь осенне-весенняя с верхом из кожи».
    clauses = [content_stems(piece) for piece in re.split(r"[.;:,\n]", text)] if text else []
    full = [h for h in hits if h.head_ok and h.membership_complete and h.name_coverage >= 1.0 and (
        said is None or within(content_stems(h.name), said) or phrase(content_stems(h.name)))]
    if not full:
        return None
    best = max(h.query_coverage for h in full)
    top = [h for h in full if h.query_coverage == best]
    return top[0] if len(top) == 1 else None
