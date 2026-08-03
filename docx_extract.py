"""Per-paragraph text extraction for the track-changes pipeline.

Walks each paragraph's runs/hyperlinks in document order and builds a flat
list of Items describing exactly how the paragraph's plain-text stream maps
back to its source XML elements. This is the foundation docx_track_changes.py
uses to rebuild the paragraph with w:ins/w:del injected, without ever losing
track of images, hyperlinks, tabs, or per-run formatting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from docx.document import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.hyperlink import Hyperlink
from docx.text.paragraph import Paragraph
from docx.text.run import Run

# Child elements of a run that represent exactly one placeholder character in
# the plain-text stream. Any diff opcode touching one of these is forced back
# to "keep original" — we never let the LLM add/remove tabs or line breaks.
_PROTECTED_CHAR_TAGS = {
    qn("w:tab"): "\t",
    qn("w:br"): "\n",
    qn("w:cr"): "\n",
}

# Kinds of Item.
TEXT = "text"
PROTECTED_CHAR = "protected_char"
HYPERLINK = "hyperlink"
ANCHOR = "anchor"

PROTECTED_KINDS = {PROTECTED_CHAR, HYPERLINK}


@dataclass
class Item:
    start: int
    end: int
    kind: str
    element: object  # the specific child node: w:t/w:tab/w:br/w:cr (text/protected_char),
    # the anchor node itself (anchor), or the w:hyperlink element (hyperlink)
    rpr: object = None  # w:rPr of the containing run, if any (text/protected_char/anchor)
    text: str = ""


@dataclass
class ParagraphExtraction:
    paragraph: Paragraph
    text: str
    items: list[Item] = field(default_factory=list)

    @property
    def protected_ranges(self) -> list[tuple[int, int]]:
        return [(it.start, it.end) for it in self.items if it.kind in PROTECTED_KINDS]


def _run_child_items(run_element, start_offset: int) -> tuple[list[Item], int]:
    items: list[Item] = []
    offset = start_offset
    rpr = run_element.find(qn("w:rPr"))
    for child in run_element:
        tag = child.tag
        if tag == qn("w:rPr"):
            continue
        if tag == qn("w:t"):
            text = child.text or ""
            if text:
                items.append(Item(offset, offset + len(text), TEXT, child, rpr, text))
                offset += len(text)
        elif tag in _PROTECTED_CHAR_TAGS:
            ch = _PROTECTED_CHAR_TAGS[tag]
            items.append(Item(offset, offset + 1, PROTECTED_CHAR, child, rpr, ch))
            offset += 1
        else:
            # Anything else (w:drawing, field codes, footnote/endnote refs,
            # w:noBreakHyphen, w:softHyphen, w:sym, pre-existing w:proofErr,
            # comment ranges, etc.) is zero-width and must round-trip
            # untouched at its exact position — never sent to the LLM.
            items.append(Item(offset, offset, ANCHOR, child, rpr, ""))
    return items, offset


def extract_paragraph(paragraph: Paragraph) -> ParagraphExtraction:
    items: list[Item] = []
    offset = 0
    for content in paragraph.iter_inner_content():
        if isinstance(content, Hyperlink):
            text = content.text or ""
            if text:
                items.append(Item(offset, offset + len(text), HYPERLINK, content._hyperlink, None, text))
                offset += len(text)
            # An empty hyperlink (no visible text) contributes nothing and is
            # dropped here; on reconstruction any paragraph with zero items
            # simply has no edits applied, which is correct.
        elif isinstance(content, Run):
            run_items, offset = _run_child_items(content._r, offset)
            items.extend(run_items)
    text = "".join(it.text for it in items)
    return ParagraphExtraction(paragraph=paragraph, text=text, items=items)


def iter_all_paragraphs(container) -> Iterator[Paragraph]:
    """Yield every paragraph in a Document/Cell, recursing into tables
    (including nested tables) so the same per-paragraph pipeline applies
    uniformly to body text and table content.
    """
    if isinstance(container, Document):
        parts = [container.paragraphs, container.tables]
    else:  # a _Cell
        parts = [container.paragraphs, container.tables]

    for paragraph in parts[0]:
        yield paragraph
    for table in parts[1]:
        yield from iter_all_paragraphs_in_table(table)


def iter_all_paragraphs_in_table(table: Table) -> Iterator[Paragraph]:
    for row in table.rows:
        for cell in row.cells:
            yield from iter_all_paragraphs(cell)
