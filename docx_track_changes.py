"""Turns "original paragraph + corrected text" into real Word Track Changes
(w:ins/w:del) injected directly into the paragraph's XML, using the
extraction/offset-map built by docx_extract.py.

Core algorithm:
1. Word-level diff (difflib) between original and corrected text.
2. Any diff opcode that overlaps a protected range (hyperlink text, tab/br
   characters) is reverted to "equal, original text" for its FULL span —
   never partially, to avoid ever mis-attributing corrected text into a
   protected element. This is a deliberately conservative simplification:
   a nearby real edit that happens to share an opcode with protected
   content is dropped rather than risking a corrupted hyperlink/tab.
3. Each opcode is further split at Item boundaries (run/hyperlink/protected
   -char boundaries), since even an "equal" span may cross a formatting
   change and must preserve each run's own formatting individually.
4. New XML elements are built per sub-range: plain runs for "equal",
   <w:del> for deleted original text, <w:ins> for inserted corrected text
   (using the rPr of the run(s) being replaced/preceded, per Word convention
   for "replace").
5. Anchors (images, field codes, etc.) are re-spliced back at their exact
   zero-width position, always untouched.
6. Only the specific w:r/w:hyperlink elements that were part of the
   extraction are removed and replaced — anything else that happens to be a
   direct child of the paragraph (bookmarks, proofErr, pre-existing
   w:ins/w:del) is left exactly where it was.
"""

from __future__ import annotations

import copy
import difflib
import re
from itertools import count
from typing import Iterator

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from docx_extract import ANCHOR, HYPERLINK, PROTECTED_CHAR, TEXT, Item, ParagraphExtraction

_TOKEN_RE = re.compile(r"\s+|\w+|[^\w\s]")


def make_id_counter(document) -> Iterator[int]:
    """A w:id counter guaranteed to start above any id already used by
    tracked changes present in the document (handles documents that already
    contain revisions)."""
    max_id = 0
    for el in document.element.body.iter():
        if el.tag in (qn("w:ins"), qn("w:del")):
            raw = el.get(qn("w:id"))
            if raw is not None:
                try:
                    max_id = max(max_id, int(raw))
                except ValueError:
                    pass
    return count(max_id + 1)


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _cumulative_offsets(tokens: list[str]) -> list[int]:
    offsets = [0]
    total = 0
    for t in tokens:
        total += len(t)
        offsets.append(total)
    return offsets


def _char_opcodes(orig: str, corr: str) -> list[tuple[str, int, int, int, int]]:
    orig_tokens = _tokenize(orig)
    corr_tokens = _tokenize(corr)
    orig_offsets = _cumulative_offsets(orig_tokens)
    corr_offsets = _cumulative_offsets(corr_tokens)

    sm = difflib.SequenceMatcher(None, orig_tokens, corr_tokens, autojunk=False)
    return [
        (tag, orig_offsets[i1], orig_offsets[i2], corr_offsets[j1], corr_offsets[j2])
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
    ]


def _overlaps_any(a1: int, a2: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a1 < p2 and p1 < a2 for p1, p2 in ranges)


def _clip_opcodes(opcodes, protected_ranges):
    """Revert any non-equal opcode that overlaps a protected range back to
    'equal, original text' for its whole span. See module docstring."""
    if not protected_ranges:
        return opcodes
    clipped = []
    for tag, a1, a2, b1, b2 in opcodes:
        if tag != "equal" and _overlaps_any(a1, a2, protected_ranges):
            clipped.append(("equal", a1, a2, a1, a2))
        else:
            clipped.append((tag, a1, a2, b1, b2))
    return clipped


def _suppress_respacing_opcodes(opcodes, orig: str, corr: str):
    """Revert any non-equal opcode that only resizes whitespace which was
    already present in the original (e.g. a double space collapsed to a
    single space) back to 'equal, original text'. The deleted/inserted
    content in these opcodes is itself whitespace, which renders as an
    invisible-looking tracked change in Word -- not worth flagging.

    A pure insertion into a gap that had no whitespace at all (e.g. adding
    the missing space between two run-together words) is left untouched:
    that's a real, visible fix, distinguishable here by a1 == a2 (nothing
    of the original is being touched)."""
    suppressed = []
    for tag, a1, a2, b1, b2 in opcodes:
        if tag != "equal" and a1 < a2 and orig[a1:a2].isspace():
            corr_span = corr[b1:b2]
            if corr_span == "" or corr_span.isspace():
                suppressed.append(("equal", a1, a2, a1, a2))
                continue
        suppressed.append((tag, a1, a2, b1, b2))
    return suppressed


def _items_overlapping(items: list[Item], start: int, end: int) -> list[Item]:
    return [it for it in items if it.kind != ANCHOR and it.start < end and start < it.end]


def _group_anchors(items: list[Item]) -> dict[int, list[Item]]:
    grouped: dict[int, list[Item]] = {}
    for it in items:
        if it.kind == ANCHOR:
            grouped.setdefault(it.start, []).append(it)
    return grouped


def _make_run(text: str, rpr) -> object:
    r = OxmlElement("w:r")
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    return r


def _make_del_run(text: str, rpr) -> object:
    r = OxmlElement("w:r")
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = OxmlElement("w:delText")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    return r


def _wrap_revision(tag: str, id_: int, author: str, date: str, run) -> object:
    el = OxmlElement(tag)
    el.set(qn("w:id"), str(id_))
    el.set(qn("w:author"), author)
    el.set(qn("w:date"), date)
    el.append(run)
    return el


def _make_protected_char_run(item: Item) -> object:
    r = OxmlElement("w:r")
    if item.rpr is not None:
        r.append(copy.deepcopy(item.rpr))
    r.append(copy.deepcopy(item.element))
    return r


def _make_anchor_run(item: Item) -> object:
    r = OxmlElement("w:r")
    if item.rpr is not None:
        r.append(copy.deepcopy(item.rpr))
    r.append(copy.deepcopy(item.element))
    return r


def _distinct_original_elements(items: list[Item]) -> list[object]:
    seen: set[int] = set()
    ordered: list[object] = []
    for it in items:
        if it.kind == HYPERLINK:
            el = it.element
        else:
            el = it.element.getparent()  # the containing w:r
        if el is None or id(el) in seen:
            continue
        seen.add(id(el))
        ordered.append(el)
    return ordered


def build_new_children(
    extraction: ParagraphExtraction,
    corrected_text: str,
    author: str,
    date: str,
    id_gen: Iterator[int],
) -> list[object]:
    opcodes = _char_opcodes(extraction.text, corrected_text)
    opcodes = _suppress_respacing_opcodes(opcodes, extraction.text, corrected_text)
    opcodes = _clip_opcodes(opcodes, extraction.protected_ranges)
    anchors_by_offset = _group_anchors(extraction.items)

    new_children: list[object] = []
    emitted_hyperlinks: set[int] = set()
    emitted_anchors: set[int] = set()
    last_rpr = None

    def emit_anchors_at(offset: int) -> None:
        for anchor_item in anchors_by_offset.get(offset, []):
            if id(anchor_item) in emitted_anchors:
                continue
            new_children.append(_make_anchor_run(anchor_item))
            emitted_anchors.add(id(anchor_item))

    for tag, a1, a2, b1, b2 in opcodes:
        emit_anchors_at(a1)

        if tag == "insert":
            text = corrected_text[b1:b2]
            new_children.append(_wrap_revision("w:ins", next(id_gen), author, date, _make_run(text, last_rpr)))
            continue

        sub_items = _items_overlapping(extraction.items, a1, a2)
        first_text_rpr = next((it.rpr for it in sub_items if it.kind != HYPERLINK), last_rpr)

        for it in sub_items:
            emit_anchors_at(it.start)

            if it.kind == HYPERLINK:
                if id(it.element) not in emitted_hyperlinks:
                    new_children.append(copy.deepcopy(it.element))
                    emitted_hyperlinks.add(id(it.element))
                continue

            s = max(a1, it.start)
            e = min(a2, it.end)
            piece_text = it.text[s - it.start : e - it.start]

            if tag == "equal":
                if it.kind == PROTECTED_CHAR:
                    new_children.append(_make_protected_char_run(it))
                else:
                    new_children.append(_make_run(piece_text, it.rpr))
            else:  # 'delete' or 'replace': original-side content is deleted
                new_children.append(
                    _wrap_revision("w:del", next(id_gen), author, date, _make_del_run(piece_text, it.rpr))
                )
            last_rpr = it.rpr

        if tag == "replace":
            text = corrected_text[b1:b2]
            new_children.append(
                _wrap_revision("w:ins", next(id_gen), author, date, _make_run(text, first_text_rpr))
            )
            last_rpr = first_text_rpr

    emit_anchors_at(len(extraction.text))
    return new_children


def apply_track_changes(
    paragraph: Paragraph,
    extraction: ParagraphExtraction,
    corrected_text: str,
    author: str,
    date: str,
    id_gen: Iterator[int],
) -> None:
    """Rebuild `paragraph` in place so the diff between extraction.text and
    corrected_text is expressed as tracked changes. No-op if the paragraph
    had no editable content or the text is unchanged.
    """
    if extraction.text == corrected_text or not extraction.items:
        return

    new_children = build_new_children(extraction, corrected_text, author, date, id_gen)

    original_elements = _distinct_original_elements(extraction.items)
    if not original_elements:
        return

    p_elem = paragraph._p
    insert_index = list(p_elem).index(original_elements[0])

    for el in original_elements:
        p_elem.remove(el)

    for offset, child in enumerate(new_children):
        p_elem.insert(insert_index + offset, child)


def changed_token_ratio(extraction: ParagraphExtraction, corrected_text: str) -> float:
    """Fraction of the paragraph's tokens touched by replace/insert/delete
    opcodes — used by the guardrail. Mirrors the same respacing suppression
    as build_new_children/_suppress_respacing_opcodes so the ratio reflects
    what will actually be tracked, not cosmetic whitespace resizing."""
    orig_tokens = _tokenize(extraction.text)
    if not orig_tokens:
        return 0.0
    corr_tokens = _tokenize(corrected_text)
    sm = difflib.SequenceMatcher(None, orig_tokens, corr_tokens, autojunk=False)
    changed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if i1 < i2 and "".join(orig_tokens[i1:i2]).isspace():
            corr_span = "".join(corr_tokens[j1:j2])
            if corr_span == "" or corr_span.isspace():
                continue
        changed += max(i2 - i1, j2 - j1)
    return changed / len(orig_tokens)
