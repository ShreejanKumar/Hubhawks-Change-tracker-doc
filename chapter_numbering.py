"""Chapter heading numbers checked against the table of contents.

A manuscript's chapter numbers are a whole-document property that no single
Gemini chunk can see, so they're fixed here in code. The table of contents is
the authority: each body heading whose title matches a TOC entry must carry
that entry's number. A heading's number is either typed text ("3. HOW TO…")
or Word's automatic list numbering (w:numPr). Authors who paste chapters in
from separate files often end up with each chapter heading in its own list,
each restarting at whatever number it had in the source file, which is how
two consecutive chapters both came out as "3." (client review, Oct 2026).

Typed numbers are returned as text fixes, so they go through the normal
diff and become w:del/w:ins. List numbers can't be expressed as text, so
the heading is moved onto a list of its own that starts at the right number,
recorded as a w:pPrChange (a tracked formatting change Word can reject).

Without a usable TOC (one numbered 1, 2, 3… in order) nothing is changed:
there's no reliable way to tell which of two conflicting numbers is right.
"""

from __future__ import annotations

import copy
import difflib
import random
import re
from dataclasses import dataclass

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

# "3. TITLE……… 12", "3) Title\t12", "3 TITLE"; the leaders and page number are dropped.
_TOC_ENTRY_RE = re.compile(r"^\s*(\d{1,3})\s*[.):]?\s+(\S.*)$")
_LEADERS_RE = re.compile(r"(?:[.…·_\-]{2,}|\t).*$")
_TYPED_NUMBER_RE = re.compile(r"^(\s*(?:chapter\s+)?)(\d{1,3})(?=\s*[.):]|\s)", re.IGNORECASE)
# A chapter heading is short; anything longer is a sentence that merely
# mentions a chapter title.
_MAX_HEADING_CHARS = 120
_TITLE_MATCH_RATIO = 0.8
_MIN_TOC_ENTRIES = 3


@dataclass
class TocEntry:
    number: int
    title: str


@dataclass
class ChapterFix:
    paragraph_id: int
    current: int
    expected: int
    typed: bool  # True: fix the text; False: fix the list numbering


def _normalise_title(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _strip_leading_number(text: str) -> str:
    return re.sub(r"^\s*(?:chapter\s+)?\d{1,3}\s*[.):]?\s*", "", text, flags=re.IGNORECASE)


def find_toc(texts: list[str]) -> tuple[list[TocEntry], int, int] | None:
    """The first run of numbered entries 1, 2, 3… with dot leaders or tabs
    (a title too long for one line may continue on the next paragraph).
    Returns the entries and the paragraph range [start, end) they occupy."""
    i = 0
    while i < len(texts):
        m = _TOC_ENTRY_RE.match(texts[i])
        if not (m and int(m.group(1)) == 1 and _LEADERS_RE.search(m.group(2))):
            i += 1
            continue
        entries, start, j = [], i, i
        while j < len(texts):
            m = _TOC_ENTRY_RE.match(texts[j])
            if m and int(m.group(1)) == len(entries) + 1:
                entries.append(TocEntry(int(m.group(1)), _LEADERS_RE.sub("", m.group(2)).strip()))
            elif entries and texts[j].strip() and not _TOC_ENTRY_RE.match(texts[j]) and len(texts[j]) < _MAX_HEADING_CHARS:
                # Continuation line of a wrapped title.
                entries[-1].title += " " + _LEADERS_RE.sub("", texts[j]).strip()
            else:
                break
            j += 1
        if len(entries) >= _MIN_TOC_ENTRIES:
            return entries, start, j
        i = j if j > i else i + 1
    return None


def _word_list_numbers(paragraphs) -> dict[int, int]:
    """The number Word displays for each paragraph with direct decimal
    numbering at level 0, keyed by paragraph id. Counters are kept per
    abstract list (separate w:num instances of one w:abstractNum continue
    each other) and restart where a w:num has a startOverride."""
    if not paragraphs:
        return {}
    try:
        numbering = paragraphs[0].part.numbering_part.element
    except (KeyError, NotImplementedError, AttributeError):
        return {}
    abstract_by_id = {a.get(qn("w:abstractNumId")): a for a in numbering.findall(qn("w:abstractNum"))}
    num_info = {}
    for num in numbering.findall(qn("w:num")):
        abs_id = num.find(qn("w:abstractNumId")).get(qn("w:val"))
        override = None
        for ov in num.findall(qn("w:lvlOverride")):
            so = ov.find(qn("w:startOverride"))
            if ov.get(qn("w:ilvl")) == "0" and so is not None:
                override = int(so.get(qn("w:val")))
        num_info[num.get(qn("w:numId"))] = (abs_id, override)

    def level0(abs_id):
        a = abstract_by_id.get(abs_id)
        if a is None:
            return None
        for lvl in a.findall(qn("w:lvl")):
            if lvl.get(qn("w:ilvl")) == "0":
                return lvl
        return None

    counters: dict[str, int] = {}
    started: set[str] = set()  # numIds whose startOverride has been applied
    shown: dict[int, int] = {}
    for pid, p in enumerate(paragraphs):
        num_pr = p._p.find(f"{qn('w:pPr')}/{qn('w:numPr')}")
        if num_pr is None:
            continue
        num_id_el, ilvl_el = num_pr.find(qn("w:numId")), num_pr.find(qn("w:ilvl"))
        if num_id_el is None:
            continue
        num_id = num_id_el.get(qn("w:val"))
        ilvl = ilvl_el.get(qn("w:val")) if ilvl_el is not None else "0"
        if num_id not in num_info or ilvl != "0":
            continue
        abs_id, override = num_info[num_id]
        lvl = level0(abs_id)
        if lvl is None:
            continue
        fmt = lvl.find(qn("w:numFmt"))
        if fmt is None or fmt.get(qn("w:val")) != "decimal":
            continue
        start_el = lvl.find(qn("w:start"))
        start = int(start_el.get(qn("w:val"))) if start_el is not None else 1
        if override is not None and num_id not in started:
            counters[abs_id] = override
            started.add(num_id)
        elif abs_id not in counters:
            counters[abs_id] = start
        else:
            counters[abs_id] += 1
        shown[pid] = counters[abs_id]
    return shown


def plan_fixes(paragraphs, texts: list[str]) -> list[ChapterFix]:
    toc = find_toc(texts)
    if toc is None:
        return []
    entries, toc_start, toc_end = toc
    list_numbers = _word_list_numbers(paragraphs)

    fixes = []
    pid = toc_end
    for entry in entries:
        want = _normalise_title(entry.title)
        # Headings appear in TOC order, so search forward from the last match.
        for j in range(pid, len(texts)):
            text = texts[j].strip()
            if not text or len(text) > _MAX_HEADING_CHARS:
                continue
            title = _normalise_title(_strip_leading_number(text))
            if difflib.SequenceMatcher(None, want, title).ratio() < _TITLE_MATCH_RATIO:
                continue
            typed = _TYPED_NUMBER_RE.match(texts[j])
            if typed:
                current = int(typed.group(2))
                if current != entry.number:
                    fixes.append(ChapterFix(j, current, entry.number, typed=True))
            elif j in list_numbers and list_numbers[j] != entry.number:
                fixes.append(ChapterFix(j, list_numbers[j], entry.number, typed=False))
            pid = j + 1
            break
    return fixes


def renumber_text(text: str, expected: int) -> str:
    """Replace a heading's typed leading number. Applied to the corrected
    text, so it composes with whatever Gemini did to the rest of the line."""
    return _TYPED_NUMBER_RE.sub(lambda m: f"{m.group(1)}{expected}", text, count=1)


def apply_list_fix(paragraph, expected: int, author: str, date: str, id_gen) -> None:
    """Move the heading onto its own copy of its list, starting at
    `expected`, and record the old paragraph properties in a w:pPrChange so
    the change is tracked. A copy of the abstract list (rather than a
    startOverride on the shared one) keeps every other paragraph in the old
    list numbered exactly as before."""
    numbering = paragraph.part.numbering_part.element
    p_pr = paragraph._p.find(qn("w:pPr"))
    num_id_el = p_pr.find(f"{qn('w:numPr')}/{qn('w:numId')}")
    old_num = next(n for n in numbering.findall(qn("w:num")) if n.get(qn("w:numId")) == num_id_el.get(qn("w:val")))
    old_abs_id = old_num.find(qn("w:abstractNumId")).get(qn("w:val"))
    old_abs = next(a for a in numbering.findall(qn("w:abstractNum")) if a.get(qn("w:abstractNumId")) == old_abs_id)

    new_abs_id = str(max(int(a.get(qn("w:abstractNumId"))) for a in numbering.findall(qn("w:abstractNum"))) + 1)
    new_num_id = str(max(int(n.get(qn("w:numId"))) for n in numbering.findall(qn("w:num"))) + 1)
    new_abs = copy.deepcopy(old_abs)
    new_abs.set(qn("w:abstractNumId"), new_abs_id)
    # nsid identifies a list to Word; a duplicate would merge the two again.
    nsid = new_abs.find(qn("w:nsid"))
    if nsid is not None:
        nsid.set(qn("w:val"), f"{random.getrandbits(32):08X}")
    # Word ignores a style link on a copy, but two lists claiming one style breaks it.
    for tag in ("w:styleLink", "w:numStyleLink"):
        el = new_abs.find(qn(tag))
        if el is not None:
            new_abs.remove(el)
    for lvl in new_abs.findall(qn("w:lvl")):
        if lvl.get(qn("w:ilvl")) == "0":
            start = lvl.find(qn("w:start"))
            if start is None:
                start = OxmlElement("w:start")
                lvl.insert(0, start)
            start.set(qn("w:val"), str(expected))

    new_num = OxmlElement("w:num")
    new_num.set(qn("w:numId"), new_num_id)
    abs_ref = OxmlElement("w:abstractNumId")
    abs_ref.set(qn("w:val"), new_abs_id)
    new_num.append(abs_ref)
    # Schema order: every w:abstractNum precedes every w:num.
    numbering.findall(qn("w:abstractNum"))[-1].addnext(new_abs)
    numbering.findall(qn("w:num"))[-1].addnext(new_num)

    old_p_pr = copy.deepcopy(p_pr)
    for child in list(old_p_pr):
        if child.tag in (qn("w:rPr"), qn("w:sectPr"), qn("w:pPrChange")):
            old_p_pr.remove(child)
    num_id_el.set(qn("w:val"), new_num_id)
    change = OxmlElement("w:pPrChange")
    change.set(qn("w:id"), str(next(id_gen)))
    change.set(qn("w:author"), author)
    change.set(qn("w:date"), date)
    change.append(old_p_pr)
    # w:pPrChange is the last child of w:pPr.
    p_pr.append(change)
