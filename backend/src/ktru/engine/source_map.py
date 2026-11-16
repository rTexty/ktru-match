"""Original Unicode code-point mappings for layout and search-only spelling changes."""

import re
from collections.abc import Callable
from dataclasses import dataclass

from src.ktru import contracts as C
from src.ktru.engine.identity import _id
from src.ktru.engine.script import _GLUED, _SPACED, segment, transliterate
from src.ktru.engine.spelling import known_word
from src.ktru.engine.text import LAYOUT, fix_layout, norm, words

MAX_TYPO_DIAGNOSTICS = 16


@dataclass(frozen=True)
class SearchMapping:
    normalized_text: str
    search_text: str
    correction_map: list[C.SourceMapSegment]
    corrections: list[C.Correction]
    diagnostics: list[C.Diagnostic]


def validate_source_map(original: str, transformed: str, segments: list[C.SourceMapSegment]) -> None:
    """Changed spans are ordered; omitted gaps must contain the same source and target text."""
    source_end = target_end = 0
    for piece in segments:
        start, end = piece.original_start, piece.original_end
        target_start, end_target = piece.transformed_start, piece.transformed_end
        if not (source_end <= start < end <= len(original)
                and target_end <= target_start < end_target <= len(transformed)):
            raise ValueError("Source map has invalid or overlapping ranges")
        if original[source_end:start] != transformed[target_end:target_start]:
            raise ValueError("Source map does not preserve an unchanged gap")
        if piece.kind == "layout" and end - start != end_target - target_start:
            raise ValueError("Layout mapping changed the code-point length")
        source_end, target_end = end, end_target
    if original[source_end:] != transformed[target_end:]:
        raise ValueError("Source map does not preserve the final unchanged gap")


def _layout_segments(original: str, normalized: str, start: int, end: int, target_start: int):
    """Layout translation is one code point per source point; record only changed runs."""
    out = []
    cursor = start
    while cursor < end:
        if original[cursor] == normalized[cursor]:
            cursor += 1
            continue
        run_start = cursor
        while cursor < end and original[cursor] != normalized[cursor]:
            cursor += 1
        out.append(C.SourceMapSegment(original_start=run_start, original_end=cursor,
            transformed_start=target_start + run_start - start,
            transformed_end=target_start + cursor - start, kind="layout"))
    return out


def _whole_word(text: str, start: int, end: int) -> bool:
    def bound(char: str) -> bool:
        return char.isalnum() or char in "_-"
    return not (start and bound(text[start - 1])) and not (end < len(text) and bound(text[end]))


def _match_case(word: str, replacement: str) -> str:
    if word.isupper() and len(word) > 1:
        return replacement.upper()
    return replacement[:1].upper() + replacement[1:] if word[:1].isupper() else replacement


_INVISIBLE = "\u00ad\u200b\u200c\u200d\u2060\ufeff"
_BROKEN = re.compile(rf"[А-Яа-яЁё]+(?:[{_INVISIBLE}]+[А-Яа-яЁё]+)+")  # «Кур\u200bтка»: invisible characters in a word
_HYPHENATED = re.compile(r"([А-Яа-яЁё]{2,})-[ \t]*\r?\n[ \t]*([а-яё]{2,})")
_QUOTED = re.compile(r"«[^»]*»|\"[^\"]*\"|„[^“”]*[“”]")


def _readable(text: str, correct_word: Callable[[str], str | None]) -> bool:
    """Most words are real or fixable: «Cnjk lthtdzyysq» was the wrong layout, «Stol derevyannyy» is translit."""
    found = [word for word, _, _ in words(text) if len(word) >= 3 and word.isalpha()]
    readable = sum(known_word(norm(word)) or correct_word(word) is not None for word in found)
    return bool(found) and readable * 2 >= len(found)


def _mostly_latin(text: str) -> bool:
    """Russian typed in Latin letters: «Stol derevyannyy, shirina 120 sm». A brand next to Russian words is not."""
    found = [word for word, _, _ in words(text) if len(word) >= 3]
    latin = sum(word.isascii() for word in found)
    return latin > 0 and latin >= 3 * (len(found) - latin)


def _reads_as_translit(text: str, layout: str) -> bool:
    """«Drel akkumulyatornaya» is Latin transliteration, though every letter also maps to some Cyrillic one."""
    latin = [word for word, _, _ in words(text) if len(word) >= 3 and word.isascii()]
    if not latin or not _mostly_latin(text):
        return False
    return sum(transliterate(word) is not None for word in latin) > sum(
        known_word(norm(word)) for word, _, _ in words(layout) if len(word) >= 3)


def autocorrect(text: str, correct_word: Callable[[str], str | None],
                is_word: Callable[[str], bool] | None = None,
                is_common: Callable[[str], bool] | None = None) -> tuple[str, list[C.Correction]]:
    """Fix layout and misspellings before anything else reads the text; spans point into the corrected text.

    Quoted names («Секси») and real words stay as typed.
    """
    normalized = fix_layout(text) or text
    if normalized != text and (not _readable(normalized, correct_word) or _reads_as_translit(text, normalized)):
        normalized = text
    corrections: list[C.Correction] = []
    if normalized != text:
        corrections.append(C.Correction(kind="layout", original=text, replacement=normalized, target="text",
            reason_ru="Текст был набран в английской раскладке"))
    edits: list[tuple[int, int, str, str]] = []  # (start, end, replacement, reason) in the normalized text
    for match in _HYPHENATED.finditer(normalized):  # «дере-\nвянный»: a word carried over to the next line
        joined = match.group(1) + match.group(2)
        fixed = correct_word(joined)
        if known_word(norm(joined)) or fixed:
            edits.append((match.start(), match.end(), _match_case(joined, fixed) if fixed else joined,
                          "Соединено слово, перенесённое на новую строку"))
    for match in _BROKEN.finditer(normalized):
        joined = re.sub(f"[{_INVISIBLE}]", "", match.group(0))
        fixed = correct_word(joined)
        edits.append((match.start(), match.end(), _match_case(joined, fixed) if fixed else joined,
                      "Удалены невидимые символы внутри слова"))
    translit = _mostly_latin(normalized)

    def wrong_layout(word: str) -> str | None:
        """«rehnrf, размер 50»: one word typed in the English layout among Russian ones reads as a catalog word."""
        if translit or is_word is None or not word.isascii() or not word.isalpha() or len(word) < 4:
            return None
        fixed = norm(word.translate(LAYOUT))
        return _match_case(word, fixed) if is_word(fixed) else None

    layout_words = {word: fixed for word in dict.fromkeys(word for word, _, _ in words(normalized))
                    if (fixed := wrong_layout(word))}
    replacements = {word: layout_words.get(word) or (transliterate(word) if translit and word.isascii() else None)
                    or correct_word(word) for word in dict.fromkeys(word for word, _, _ in words(normalized))}
    quoted = [match.span() for match in _QUOTED.finditer(normalized)]
    if is_word is not None:
        for match in _SPACED.finditer(normalized):  # «Д р е л ь»: letters typed with spaces between them
            joined = match.group(0).replace(" ", "")
            pieces = [joined] if is_word(joined.lower()) else segment(joined, is_common or is_word)
            if pieces:
                edits.append((match.start(), match.end(), _match_case(joined, " ".join(pieces).lower()),
                              "Склеены буквы, разделённые пробелами"))
        for match in _GLUED.finditer(normalized):  # «курткасиняя»: words typed without spaces
            token = match.group(0)
            if any(a <= match.start() < b for a, b, _, _ in edits) or is_word(token.lower()) or correct_word(token):
                continue  # a typo of one word is not two words
            pieces = segment(match.group(0), is_common or is_word)
            if pieces and len(pieces) > 1:
                edits.append((match.start(), match.end(), " ".join(pieces), "Разделены слитно написанные слова"))
    for word, start, end in words(normalized):
        replacement = replacements[word]
        if not replacement or norm(replacement) == norm(word) or not _whole_word(normalized, start, end):
            continue
        if any(left < start < right for left, right in quoted) or any(a <= start < b for a, b, _, _ in edits):
            continue
        if known_word(norm(word)):  # A real word stays as typed; search may still use the catalog's spelling.
            continue
        edits.append((start, end, _match_case(word, replacement),
                      "Слово набрано в английской раскладке" if word in layout_words else "Исправлена опечатка"))
    pieces, cursor, length = [], 0, 0
    for start, end, replacement, reason in sorted(edits):
        gap = normalized[cursor:start]
        pieces += [gap, replacement]
        length += len(gap)
        corrections.append(C.Correction(kind="typo", original=normalized[start:end], replacement=replacement,
            target="text", reason_ru=reason,
            transformed_span=C.MappedTextSpan(start=length, end=length + len(replacement), text=replacement)))
        length += len(replacement)
        cursor = end
    pieces.append(normalized[cursor:])
    return "".join(pieces), corrections


def search_mapping(text: str, correct_word: Callable[[str], str | None], *, snapshot_id: str,
                   text_revision: int = 0) -> SearchMapping:
    """Compose layout and every typo occurrence without searching for repeated substrings."""
    normalized = fix_layout(text) or text
    if len(normalized) != len(text):
        raise ValueError("Layout correction changed the code-point length")
    pieces, segments, corrections, diagnostics = [], [], [], []
    if normalized != text:
        corrections.append(C.Correction(kind="layout", original=text, replacement=normalized,
            reason_ru="Текст набран в английской раскладке", target="normalized",
            original_span=C.Span(start=0, end=len(text), text=text, text_revision=text_revision),
            transformed_span=C.MappedTextSpan(start=0, end=len(normalized), text=normalized)))
        diagnostics.append(C.Diagnostic(id=_id("layout"), kind="layout", severity="info",
            message_ru=f"Исправлена раскладка: «{normalized}»", auto_applied=True,
            reason_ru="Почти все буквы латинские; исходный текст сохранён.",
            evidence=[C.TextEvidence(span=corrections[0].original_span)]))
    replacements = {word: correct_word(word) for word in dict.fromkeys(word for word, _, _ in words(normalized))}
    cursor = target_end = 0
    seen: set[str] = set()
    for word, start, end in words(normalized):
        replacement = replacements[word]
        if not replacement or replacement == word or not _whole_word(normalized, start, end):
            continue
        gap = normalized[cursor:start]
        pieces.append(gap)
        segments.extend(_layout_segments(text, normalized, cursor, start, target_end))
        target_end += len(gap)
        source = C.Span(start=start, end=end, text=text[start:end], text_revision=text_revision)
        target = C.MappedTextSpan(start=target_end, end=target_end + len(replacement), text=replacement)
        pieces.append(replacement)
        if source.text != replacement:
            segments.append(C.SourceMapSegment(original_start=start, original_end=end,
                transformed_start=target.start, transformed_end=target.end, kind="typo"))
        if word not in seen:
            corrections.append(C.Correction(kind="typo", original=source.text, replacement=replacement,
                reason_ru="Для поиска использован ближайший вариант; исходный текст сохранён.",
                original_span=source, transformed_span=target, target="search"))
            if len(seen) < MAX_TYPO_DIAGNOSTICS:
                diagnostics.append(C.Diagnostic(id=_id("typo", word), kind="typo", severity="info",
                    message_ru=f"Возможно, опечатка: «{word}» → «{replacement}»",
                    reason_ru="Для поиска использован ближайший вариант. Исходный текст сохранён.",
                    evidence=[C.TextEvidence(span=source)],
                    fixes=[C.Fix(id=_id("fix", word), label_ru=f"Заменить на «{replacement}»",
                        snapshot_id=snapshot_id, base_revision=text_revision,
                        patch=C.ReplaceSpanPatch(span=source, replacement=replacement,
                            snapshot_id=snapshot_id, base_revision=text_revision))]))
            seen.add(word)
        target_end += len(replacement)
        cursor = end
    pieces.append(normalized[cursor:])
    segments.extend(_layout_segments(text, normalized, cursor, len(text), target_end))
    search_text = "".join(pieces)
    validate_source_map(text, search_text, segments)
    return SearchMapping(normalized, search_text, segments, corrections, diagnostics)


def stamp_corrections(corrections: list[C.Correction], revision: int) -> list[C.Correction]:
    if any(correction.original_span and correction.original_span.text_revision not in (0, revision)
           for correction in corrections):
        raise ValueError("Correction refers to another text revision")
    return [correction.model_copy(update={"original_span": correction.original_span.model_copy(
        update={"text_revision": revision})}) if correction.original_span else correction
        for correction in corrections]


def stamp_fix(fix: C.Fix, snapshot_id: str, revision: int, *, unscoped: bool = False) -> C.Fix:
    allowed_revisions = (None, revision, 0) if unscoped else (None, revision)
    if fix.snapshot_id not in (None, snapshot_id) or fix.base_revision not in allowed_revisions:
        raise ValueError("Fix refers to another snapshot or revision")
    patch = fix.patch
    if isinstance(patch, C.ReplaceSpanPatch):
        allowed_spans = (0, revision) if unscoped else (revision,)
        if patch.snapshot_id != snapshot_id or patch.base_revision not in allowed_spans or patch.span.text_revision not in allowed_spans:
            raise ValueError("Span fix refers to another snapshot or revision")
        patch = patch.model_copy(update={"snapshot_id": snapshot_id, "base_revision": revision,
            "span": patch.span.model_copy(update={"text_revision": revision})})
    return fix.model_copy(update={"snapshot_id": snapshot_id, "base_revision": revision, "patch": patch})


def stamp_diagnostics(diagnostics: list[C.Diagnostic], snapshot_id: str, revision: int,
                      *, unscoped: bool = False) -> list[C.Diagnostic]:
    """Bind revision-free retrieval sources; later adapters must already use the current revision."""
    out = []
    for diagnostic in diagnostics:
        evidence = []
        for item in diagnostic.evidence:
            if isinstance(item, C.TextEvidence):
                if item.span.text_revision not in ((0, revision) if unscoped else (revision,)):
                    raise ValueError("Source span refers to another text revision")
                evidence.append(item.model_copy(update={"span": item.span.model_copy(update={"text_revision": revision})}))
            else:
                evidence.append(item)
        out.append(diagnostic.model_copy(update={"evidence": evidence,
            "fixes": [stamp_fix(fix, snapshot_id, revision, unscoped=unscoped) for fix in diagnostic.fixes]}))
    return out


def replace_span(text: str, patch: C.ReplaceSpanPatch, *, snapshot_id: str, revision: int) -> str:
    """Validate the fix against the current accepted source before replacing one exact span."""
    if patch.snapshot_id != snapshot_id or patch.base_revision != revision or patch.span.text_revision != revision:
        raise ValueError("Span edit has stale revision or snapshot")
    span = patch.span
    if not (0 <= span.start < span.end <= len(text)) or text[span.start:span.end] != span.text:
        raise ValueError("Span edit does not match the original text")
    result = text[:span.start] + patch.replacement + text[span.end:]
    if not result or len(result) > C.MAX_TEXT:
        raise ValueError("Span edit exceeds the accepted text length")
    return result
