"""Day-1 spike: confirm python-docx + lxml can hand-inject real Word Track
Changes (w:ins/w:del) and have them survive a save/reload round-trip.

This is the go/no-go gate for the whole project's architecture. Run it,
then open the output file in actual Microsoft Word and confirm:
  - "old" appears struck through (a deletion)
  - "beautiful" appears underlined (an insertion)
  - both are attributed to "Test Reviewer" in the Reviewing pane
  - Accept/Reject All works and produces "Hello beautiful world"
"""

import copy
import sys
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

OUT_PATH = Path(__file__).resolve().parent.parent / "scratch_roundtrip_test.docx"
AUTHOR = "Test Reviewer"
DATE = "2026-07-31T00:00:00Z"


def make_ins_run(text: str, rpr=None):
    r = OxmlElement("w:r")
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    return r


def make_del_run(text: str, rpr=None):
    r = OxmlElement("w:r")
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = OxmlElement("w:delText")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    return r


def wrap(tag: str, id_: int, author: str, date: str, runs):
    el = OxmlElement(tag)
    el.set(qn("w:id"), str(id_))
    el.set(qn("w:author"), author)
    el.set(qn("w:date"), date)
    for r in runs:
        el.append(r)
    return el


def build_test_doc() -> None:
    doc = Document()
    p = doc.add_paragraph()
    run1 = p.add_run("Hello ")
    run2 = p.add_run("old")
    run2.bold = True  # deliberately formatted, to confirm rPr is preserved on the deletion
    run3 = p.add_run(" world")

    p_elem = p._p
    r2_elem = run2._r
    rpr2 = r2_elem.find(qn("w:rPr"))

    del_el = wrap("w:del", 1, AUTHOR, DATE, [make_del_run("old", rpr2)])
    ins_el = wrap("w:ins", 2, AUTHOR, DATE, [make_ins_run("beautiful")])

    idx = list(p_elem).index(r2_elem)
    p_elem.insert(idx, del_el)
    p_elem.insert(idx + 1, ins_el)
    p_elem.remove(r2_elem)

    doc.save(OUT_PATH)


def verify_roundtrip() -> bool:
    doc = Document(OUT_PATH)
    p_elem = doc.paragraphs[0]._p
    ins_elems = p_elem.findall(qn("w:ins"))
    del_elems = p_elem.findall(qn("w:del"))

    ok = True
    if len(ins_elems) != 1:
        print(f"FAIL: expected 1 w:ins, found {len(ins_elems)}")
        ok = False
    if len(del_elems) != 1:
        print(f"FAIL: expected 1 w:del, found {len(del_elems)}")
        ok = False

    if ins_elems:
        ins = ins_elems[0]
        assert ins.get(qn("w:author")) == AUTHOR
        ins_text = "".join(t.text or "" for t in ins.iter(qn("w:t")))
        if ins_text != "beautiful":
            print(f"FAIL: w:ins text = {ins_text!r}, expected 'beautiful'")
            ok = False
        else:
            print("OK: w:ins survived round-trip with correct author/text")

    if del_elems:
        d = del_elems[0]
        assert d.get(qn("w:author")) == AUTHOR
        del_text = "".join(t.text or "" for t in d.iter(qn("w:delText")))
        rpr = d.find(f".//{qn('w:r')}/{qn('w:rPr')}")
        bold_preserved = rpr is not None and rpr.find(qn("w:b")) is not None
        if del_text != "old":
            print(f"FAIL: w:del text = {del_text!r}, expected 'old'")
            ok = False
        elif not bold_preserved:
            print("FAIL: bold formatting (w:rPr/w:b) was NOT preserved on the deleted run")
            ok = False
        else:
            print("OK: w:del survived round-trip with correct author/text and preserved bold rPr")

    return ok


if __name__ == "__main__":
    build_test_doc()
    print(f"Wrote {OUT_PATH}")
    success = verify_roundtrip()
    if success:
        print("\npython-docx round-trip check PASSED.")
        print(f"Now open {OUT_PATH} in Microsoft Word to confirm it renders as real")
        print("tracked changes (strikethrough delete, underlined insert, correct")
        print("author, and Accept All produces 'Hello beautiful world').")
    else:
        print("\npython-docx round-trip check FAILED — see above.")
        sys.exit(1)
