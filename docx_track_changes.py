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
from docx.text.font import Font
from docx.text.paragraph import Paragraph

from docx_extract import ANCHOR, HYPERLINK, PROTECTED_CHAR, TEXT, Item, ParagraphExtraction

_TOKEN_RE = re.compile(r"\s+|\w+|[^\w\s]")

# XML 1.0 forbids most C0/C1 control characters in text content (NULL bytes
# included) -- lxml raises ValueError the moment one is assigned to a w:t.
# Word itself never produces these, but a model's corrected_text occasionally
# does (e.g. a stray control character slipping out of a JSON response), and
# it's invisible/meaningless content either way, so dropping it is safe.
_XML_VALID_RANGES = (
    "\x09\x0a\x0d"
    "\x20-\uD7FF"
    "\uE000-\uFFFD"
    "\U00010000-\U0010FFFF"
)
_INVALID_XML_CHAR_RE = re.compile(f"[^{_XML_VALID_RANGES}]")


def sanitize_for_xml(text: str) -> str:
    return _INVALID_XML_CHAR_RE.sub("", text)


# Gemini has no way to express italics in a plain-text field, so it marks a
# span that needs italic formatting (e.g. a book/poem title) by wrapping it
# in a single pair of asterisks, and an uncommon non-English word in a pair
# of tildes (italic at its first use in the manuscript only, decided by
# main.py). See strip_italic_markers(). The tilde form must start with a
# letter so "~50 to ~60" in the text is never taken for a marker.
_ITALIC_MARKER_RE = re.compile(r"\*([^*\n]+)\*|~([^\W\d_][^~\n]*?)~")


def make_id_counter(document) -> Iterator[int]:
    """A w:id counter guaranteed to start above any id already used by
    tracked changes or comments present in the document (handles documents
    that already contain revisions). Review comments take their ids from it
    too, so no annotation id is ever shared."""
    max_id = 0
    for el in document.element.body.iter():
        if el.tag in (qn("w:ins"), qn("w:del"), qn("w:rPrChange"), qn("w:pPrChange"), qn("w:commentRangeStart")):
            raw = el.get(qn("w:id"))
            if raw is not None:
                try:
                    max_id = max(max_id, int(raw))
                except ValueError:
                    pass
    return count(max_id + 1)


def strip_italic_markers(text: str) -> tuple[str, list[tuple[int, int]], list[tuple[int, int]]]:
    """Remove Gemini's italic markers from `text` and return the clean text,
    the character ranges (offsets into the clean text) marked with asterisks
    (always italic), and those marked with tildes (non-English words, italic
    only at their first use in the manuscript). Keeps the markers themselves
    from ever reaching the document as literal characters."""
    if "*" not in text and "~" not in text:
        return text, [], []

    clean_parts: list[str] = []
    spans: list[tuple[int, int]] = []
    foreign_spans: list[tuple[int, int]] = []
    pos = 0
    out_len = 0
    for m in _ITALIC_MARKER_RE.finditer(text):
        clean_parts.append(text[pos:m.start()])
        out_len += m.start() - pos
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        clean_parts.append(inner)
        (spans if m.group(1) is not None else foreign_spans).append((out_len, out_len + len(inner)))
        out_len += len(inner)
        pos = m.end()
    clean_parts.append(text[pos:])
    return "".join(clean_parts), spans, foreign_spans


def _split_italic_segments(
    text: str, offset: int, italic_spans: list[tuple[int, int]]
) -> list[tuple[str, bool]]:
    """Split `text`, which occupies [offset, offset + len(text)) in the final
    corrected text, into (segment, is_italic) pieces per italic_spans."""
    if not italic_spans:
        return [(text, False)]

    segments: list[tuple[str, bool]] = []
    pos = 0
    n = len(text)
    for start, end in italic_spans:
        s = max(start - offset, pos)
        e = min(end - offset, n)
        if s >= e:
            continue
        if s > pos:
            segments.append((text[pos:s], False))
        segments.append((text[s:e], True))
        pos = e
        if pos >= n:
            break
    if pos < n:
        segments.append((text[pos:], False))
    return segments or [(text, False)]


def rpr_has_italic(rpr) -> bool:
    if rpr is None:
        return False
    i = rpr.find(qn("w:i"))
    if i is None:
        return False
    val = i.get(qn("w:val"))
    return val not in ("0", "false", "off")


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
    already present in the original (e.g. one space widened to two, or a
    space swapped for a non-breaking one) back to 'equal, original text'. The deleted/inserted
    content in these opcodes is itself whitespace, which renders as an
    invisible-looking tracked change in Word -- not worth flagging.

    A pure insertion into a gap that had no whitespace at all (e.g. adding
    the missing space between two run-together words) is left untouched:
    that's a real, visible fix, distinguishable here by a1 == a2 (nothing
    of the original is being touched). So is removing a space outright
    ("Morals —teachers" -> "Morals—teachers", "word ." -> "word."): the
    two sides now touch, which is visible, and suppressing it used to drop
    the fix silently.

    The exception is a run of plain spaces collapsed to one (a double space
    between sentences): it stays a tracked deletion of the extra spaces,
    because the client counts a left-in double space as a miss."""
    suppressed = []
    for tag, a1, a2, b1, b2 in opcodes:
        if tag != "equal" and a1 < a2 and orig[a1:a2].isspace():
            corr_span = corr[b1:b2]
            if corr_span == " " and set(orig[a1:a2]) == {" "}:
                suppressed.append(("equal", a1, a1 + 1, b1, b2))
                suppressed.append(("delete", a1 + 1, a2, b2, b2))
                continue
            if corr_span.isspace():
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


def _make_run(text: str, rpr, italic: bool = False) -> object:
    r = OxmlElement("w:r")
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    if italic:
        Font(r, None).italic = True
    return r


def _make_run_with_tracked_italic(text: str, rpr, id_: int, author: str, date: str, italic: bool = True) -> object:
    """A run whose text is unchanged but italics were just added to it (or
    removed, with italic=False), tracked via w:rPrChange (Word's mechanism
    for a reviewable run-property change) rather than wrapped in w:ins,
    since the text content itself isn't an insertion."""
    r = OxmlElement("w:r")
    new_rpr = copy.deepcopy(rpr) if rpr is not None else OxmlElement("w:rPr")
    r.append(new_rpr)

    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)

    if italic:
        Font(r, None).italic = True  # inserts <w:i/> at its schema-ordered slot
    else:
        for tag in ("w:i", "w:iCs"):
            for el in new_rpr.findall(qn(tag)):
                new_rpr.remove(el)

    change = OxmlElement("w:rPrChange")
    change.set(qn("w:id"), str(id_))
    change.set(qn("w:author"), author)
    change.set(qn("w:date"), date)
    # w:rPrChange requires exactly one w:rPr child recording the prior
    # properties (schema minOccurs=1), even when there were none.
    change.append(copy.deepcopy(rpr) if rpr is not None else OxmlElement("w:rPr"))
    new_rpr.append(change)  # w:rPrChange must be the last child of w:rPr

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
    italic_spans: list[tuple[int, int]] = (),
    roman_spans: list[tuple[int, int]] = (),
) -> list[object]:
    """roman_spans are ranges of the ORIGINAL text whose italics are removed
    (as a tracked formatting change) where that text is unchanged."""
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
            for seg_text, seg_italic in _split_italic_segments(text, b1, italic_spans):
                new_children.append(
                    _wrap_revision("w:ins", next(id_gen), author, date, _make_run(seg_text, last_rpr, seg_italic))
                )
            continue

        sub_items = _items_overlapping(extraction.items, a1, a2)
        first_text_rpr = next((it.rpr for it in sub_items if it.kind != HYPERLINK), last_rpr)
        shift = b1 - a1  # constant offset from original- to corrected-text coordinates within this opcode

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
                    roman_start = s
                    for roman_text, roman in _split_italic_segments(piece_text, s, roman_spans):
                        seg_start, roman_start = roman_start, roman_start + len(roman_text)
                        if roman and rpr_has_italic(it.rpr):
                            new_children.append(
                                _make_run_with_tracked_italic(roman_text, it.rpr, next(id_gen), author, date, italic=False)
                            )
                            continue
                        for seg_text, seg_italic in _split_italic_segments(roman_text, seg_start + shift, italic_spans):
                            if seg_italic and not rpr_has_italic(it.rpr):
                                new_children.append(
                                    _make_run_with_tracked_italic(seg_text, it.rpr, next(id_gen), author, date)
                                )
                            else:
                                new_children.append(_make_run(seg_text, it.rpr))
            else:  # 'delete' or 'replace': original-side content is deleted
                new_children.append(
                    _wrap_revision("w:del", next(id_gen), author, date, _make_del_run(piece_text, it.rpr))
                )
            last_rpr = it.rpr

        if tag == "replace":
            text = corrected_text[b1:b2]
            for seg_text, seg_italic in _split_italic_segments(text, b1, italic_spans):
                new_children.append(
                    _wrap_revision(
                        "w:ins", next(id_gen), author, date, _make_run(seg_text, first_text_rpr, seg_italic)
                    )
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
    italic_spans: list[tuple[int, int]] = (),
    roman_spans: list[tuple[int, int]] = (),
) -> None:
    """Rebuild `paragraph` in place so the diff between extraction.text and
    corrected_text is expressed as tracked changes. No-op if the paragraph
    had no editable content, or the text is unchanged and there are no
    italic-only formatting changes to apply either.
    """
    if (extraction.text == corrected_text and not italic_spans and not roman_spans) or not extraction.items:
        return

    new_children = build_new_children(extraction, corrected_text, author, date, id_gen, italic_spans, roman_spans)

    original_elements = _distinct_original_elements(extraction.items)
    if not original_elements:
        return

    p_elem = paragraph._p
    insert_index = list(p_elem).index(original_elements[0])

    for el in original_elements:
        p_elem.remove(el)

    for offset, child in enumerate(new_children):
        p_elem.insert(insert_index + offset, child)


_WORD_RE = re.compile(r"\w+")


def token_count(text: str) -> int:
    """Number of words changed_token_ratio divides by -- used by the
    guardrail's short-paragraph floor (config.MIN_TOKENS_FOR_CHANGE_RATIO_GUARDRAIL):
    a ratio computed against a denominator this small is dominated by even
    one legitimate small edit, so it isn't a meaningful signal below it."""
    return len(_WORD_RE.findall(text))


def changed_token_ratio(extraction: ParagraphExtraction, corrected_text: str) -> float:
    """Fraction of the paragraph's WORDS replaced, inserted or deleted -- used
    by the guardrail. Words only: this used to diff the same tokens as the
    tracked changes (words, whitespace, punctuation), but the whitespace
    between words nearly always aligns as "equal", so even a paragraph with
    every word replaced scored only ~0.5. Counting words makes the threshold
    mean what it says ("60% of the words changed"), and punctuation-only or
    spacing-only fixes (e.g. an ellipsis expanded to " . . .") count as 0."""
    orig_words = _WORD_RE.findall(extraction.text)
    if not orig_words:
        return 0.0
    corr_words = _WORD_RE.findall(corrected_text)
    sm = difflib.SequenceMatcher(None, orig_words, corr_words, autojunk=False)
    changed = sum(
        max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"
    )
    return changed / len(orig_words)


# Review comments: an issue Gemini flagged but was not confident enough to
# correct (a tense shift, a sentence fragment, a wrong word) becomes a Word
# comment anchored on the words it is about, added after the paragraph's
# tracked changes are in place.

def _visible_text(el) -> str:
    """The text `el` shows once its tracked changes are accepted: deleted
    text left out, a tab or break counted as one character as in
    extraction."""
    if el.tag == qn("w:del"):
        return ""
    if el.tag == qn("w:t"):
        return el.text or ""
    if el.tag in (qn("w:tab"), qn("w:br"), qn("w:cr")):
        return "\t"
    return "".join(_visible_text(child) for child in el)


def _split_plain_run(run, k: int) -> None:
    """Split a run holding one w:t (and nothing else but its rPr) at text
    offset k, so a comment can start or end mid-run."""
    t = run.find(qn("w:t"))
    tail = copy.deepcopy(run)
    tail_t = tail.find(qn("w:t"))
    text = t.text or ""
    t.text, tail_t.text = text[:k], text[k:]
    for el in (t, tail_t):
        el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    run.addnext(tail)


def _is_plain_run(el) -> bool:
    return el.tag == qn("w:r") and [c.tag for c in el if c.tag != qn("w:rPr")] == [qn("w:t")]


def add_review_comment(document, paragraph: Paragraph, quote: str, text: str, author: str, comment_id: int) -> None:
    """Add a Word comment on the first occurrence of `quote` in the
    paragraph as it reads after its tracked changes, or on the whole
    paragraph when the quote can't be found (the comment then names it)."""
    p = paragraph._p

    def content_children() -> list[tuple[object, int, int]]:
        spans, pos = [], 0
        for child in p:
            n = len(_visible_text(child)) if child.tag != qn("w:pPr") else 0
            if n:
                spans.append((child, pos, pos + n))
            pos += n
        return spans

    children = content_children()
    if not children:
        return
    visible = "".join(_visible_text(c) for c, _, _ in children)
    start = visible.find(quote) if quote else -1
    if start == -1:
        start, end = 0, len(visible)
        if quote:
            text = f"{text} (about: “{quote}”)"
    else:
        end = start + len(quote)
        for offset in (start, end):
            for child, a, b in content_children():
                if a < offset < b and _is_plain_run(child):
                    _split_plain_run(child, offset - a)
                    break
        children = content_children()

    covered = [c for c, a, b in children if a < end and b > start]
    comment = document.comments.add_comment(text=text, author=author, initials="")
    comment._comment_elm.set(qn("w:id"), str(comment_id))
    covered[0].addprevious(OxmlElement("w:commentRangeStart", attrs={qn("w:id"): str(comment_id)}))
    reference = OxmlElement("w:r")
    reference.append(OxmlElement("w:commentReference", attrs={qn("w:id"): str(comment_id)}))
    covered[-1].addnext(reference)
    covered[-1].addnext(OxmlElement("w:commentRangeEnd", attrs={qn("w:id"): str(comment_id)}))
