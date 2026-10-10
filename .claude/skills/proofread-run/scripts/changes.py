"""List the tracked changes in a .docx as compact text, so nobody has to read the XML.

Paragraph ids match the pipeline's (docx_extract.iter_all_paragraphs order:
body paragraphs first, then tables), so they line up with failed/flagged ids.

Marks: [-deleted-]  {+inserted+}  {~formatting change~}  -> tab  | line break

    conda run -n book python .claude/skills/proofread-run/scripts/changes.py <file.docx> \
        [--para ID ...] [--grep TEXT] [--author NAME] [--limit N] [--width N] [--all]
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from docx import Document  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402

import docx_extract  # noqa: E402

W_INS, W_DEL, W_RPRCHANGE = qn("w:ins"), qn("w:del"), qn("w:rPrChange")
W_T, W_DELTEXT, W_TAB, W_BR, W_CR = qn("w:t"), qn("w:delText"), qn("w:tab"), qn("w:br"), qn("w:cr")
W_R, W_RPR, W_AUTHOR = qn("w:r"), qn("w:rPr"), qn("w:author")


def _revision_of(el, p_el):
    """('ins'|'del'|'fmt'|None, author) for the run containing a text element."""
    run = None
    node = el.getparent()
    while node is not None and node is not p_el:
        if node.tag == W_R and run is None:
            run = node
        if node.tag in (W_INS, W_DEL):
            return ("ins" if node.tag == W_INS else "del"), node.get(W_AUTHOR)
        node = node.getparent()
    if run is not None:
        rpr = run.find(W_RPR)
        change = rpr.find(W_RPRCHANGE) if rpr is not None else None
        if change is not None:
            return "fmt", change.get(W_AUTHOR)
    return None, None


def render(p_el, author_filter):
    """Inline-marked paragraph text, plus a Counter of revision kinds."""
    out, counts = [], Counter()
    open_kind = None
    marks = {"ins": ("{+", "+}"), "del": ("[-", "-]"), "fmt": ("{~", "~}")}
    for el in p_el.iter(W_T, W_DELTEXT, W_TAB, W_BR, W_CR):
        kind, author = _revision_of(el, p_el)
        if kind and author_filter and author != author_filter:
            kind = None
        if el.tag in (W_T, W_DELTEXT):
            text = el.text or ""
        else:
            text = "->" if el.tag == W_TAB else "|"
        if kind != open_kind:
            if open_kind:
                out.append(marks[open_kind][1])
            if kind:
                out.append(marks[kind][0])
                counts[kind] += 1
            open_kind = kind
        out.append(text)
    if open_kind:
        out.append(marks[open_kind][1])
    return "".join(out), counts


def main_cli() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("docx")
    p.add_argument("--para", type=int, nargs="*", help="only these paragraph ids")
    p.add_argument("--grep", help="only paragraphs whose text contains this")
    p.add_argument("--author", help="only revisions by this author")
    p.add_argument("--limit", type=int, default=40, help="max paragraphs to print (0 = all)")
    p.add_argument("--width", type=int, default=400, help="truncate each line (0 = never)")
    p.add_argument("--all", action="store_true", help="also print paragraphs with no revisions")
    args = p.parse_args()
    # Windows pipes default to cp1252, which can't encode every character a
    # manuscript may contain (and mangles curly quotes for downstream tools).
    sys.stdout.reconfigure(encoding="utf-8")

    document = Document(args.docx)
    totals, authors = Counter(), Counter()
    lines, changed = [], 0
    for i, paragraph in enumerate(docx_extract.iter_all_paragraphs(document)):
        p_el = paragraph._p
        text, counts = render(p_el, args.author)
        for rev in p_el.iter(W_INS, W_DEL, W_RPRCHANGE):
            authors[rev.get(W_AUTHOR)] += 1
        if counts:
            changed += 1
            totals.update(counts)
        if args.para is not None and i not in args.para:
            continue
        if args.grep and args.grep not in text:
            continue
        if not counts and not args.all and args.para is None:
            continue
        if args.width and len(text) > args.width:
            text = text[: args.width] + " …"
        lines.append(f"#{i}: {text}")

    print(
        f"paragraphs with revisions: {changed} | ins {totals['ins']} del {totals['del']} "
        f"fmt {totals['fmt']} | authors: {dict(authors) or 'none'}"
    )
    shown = lines if not args.limit else lines[: args.limit]
    print("\n".join(shown))
    if len(shown) < len(lines):
        print(f"... {len(lines) - len(shown)} more (raise --limit or narrow with --para/--grep)")


if __name__ == "__main__":
    main_cli()
