"""Run the real proofreading pipeline on a .docx from the command line.

Calls main.process_document exactly as app.py does, but never saves to the
GCS/Firestore history (that is the client's live data). Writes the tracked
docx to .claude/runs/ and prints a compact JSON summary.

COSTS MONEY: every run calls the paid Gemini API. Ask the user first.

    conda run -n book python .claude/skills/proofread-run/scripts/run.py <in.docx> \
        [--variant British|American] [--guide old_inhouse] [--author NAME] [--out DIR]
"""
import argparse
import json
import os
import sys
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
CALLER_CWD = Path.cwd()
# st.secrets reads .streamlit/secrets.toml relative to the working directory.
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

import logging  # noqa: E402

# Streamlit warns loudly about running outside `streamlit run`; that is expected here.
logging.getLogger("streamlit").setLevel(logging.ERROR)

import main  # noqa: E402
from config import STYLE_GUIDES  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("docx")
    p.add_argument("--variant", default="British", choices=["British", "American"])
    p.add_argument("--guide", default="old_inhouse", choices=list(STYLE_GUIDES))
    p.add_argument("--author", default="Claude test run")
    p.add_argument("--out", default=str(ROOT / ".claude" / "runs"))
    return p.parse_args()


def main_cli() -> None:
    args = parse_args()
    src = (CALLER_CWD / args.docx).resolve()
    out_dir = CALLER_CWD / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    progress = lambda done, total: print(f"chunk {done}/{total}", file=sys.stderr, flush=True)  # noqa: E731
    result = main.process_document(src.read_bytes(), args.author, args.variant, args.guide, progress)
    seconds = round(time.monotonic() - started, 1)

    out_path = out_dir / f"{src.stem}_{args.variant}_{args.guide}_tracked.docx"
    out_path.write_bytes(result.docx_bytes)

    print(json.dumps({
        "output": str(out_path),
        "variant": args.variant,
        "guide": args.guide,
        "seconds": seconds,
        "total_paragraphs": result.total_paragraphs,
        "edited_paragraphs": result.edited_paragraphs,
        "failed_paragraphs": result.failed_paragraphs,
        "flagged": [
            {"id": f.paragraph_id, "ratio": round(f.changed_ratio, 2)}
            for f in result.flagged_paragraphs
        ],
    }, indent=1))


if __name__ == "__main__":
    main_cli()
