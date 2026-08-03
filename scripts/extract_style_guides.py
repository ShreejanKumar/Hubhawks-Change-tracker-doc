"""One-time dev script: extract the two style guide PDFs to plain text.

Not imported by the app at runtime. Run once, then hand-review the output
files under style_guides/ before committing them.

    python3 scripts/extract_style_guides.py
"""

import re
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "style_guides"

SOURCES = {
    "old_inhouse_stylesheet.txt": ROOT / "Old In House Stylesheet.pdf",
    "prhi_style_guide.txt": ROOT / "PRHI Style Guide (1) (1).pdf",
}


def clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_with_pdfplumber(pdf_path: Path) -> str:
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return clean("\n\n".join(pages))


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    for out_name, pdf_path in SOURCES.items():
        if not pdf_path.exists():
            print(f"SKIP (not found): {pdf_path}")
            continue
        text = extract_with_pdfplumber(pdf_path)
        out_path = OUT_DIR / out_name
        out_path.write_text(text, encoding="utf-8")
        word_count = len(text.split())
        print(f"Wrote {out_path} ({word_count} words, ~{word_count * 1.3:.0f} tokens est.)")
        print("  Review this file by hand before committing — check reading order,")
        print("  especially for multi-column layouts.")


if __name__ == "__main__":
    main()
