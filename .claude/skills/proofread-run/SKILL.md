---
name: proofread-run
description: Use when a change needs checking against real output, when a client reports a bug on a manuscript, when asked what corrections a .docx contains, or before reporting pipeline, prompt, style guide or model work as done. Runs the real pipeline headless (no UI, no history saved) and lists a .docx's tracked changes as compact text instead of XML.
---

# Proofread run and change listing

## Overview

Two scripts, both run from the repo root with the `book` conda env:

- `scripts/run.py` calls `main.process_document` exactly like `app.py`, writes the tracked docx to
  `.claude/runs/` (gitignored) and prints a JSON summary. It **never saves to GCS/Firestore**.
  It **calls the paid Gemini API**, so ask the user before running it.
- `scripts/changes.py` reads any `.docx` and prints each changed paragraph with inline marks. It is
  free, involves no API, and works on client-supplied output too.

## Quick reference

```bash
# Real run (paid; 1–3 min for a full manuscript). Progress goes to stderr, JSON to stdout.
conda run -n book python .claude/skills/proofread-run/scripts/run.py path/to/in.docx \
  --variant American

# What changed? (first 40 changed paragraphs, lines cut at 400 chars)
conda run -n book python .claude/skills/proofread-run/scripts/changes.py .claude/runs/in_American_old_inhouse_tracked.docx

# Narrow it
... changes.py out.docx --para 12 57          # these ids (also prints them if unchanged)
... changes.py out.docx --grep "Mossad"       # paragraphs containing text
... changes.py out.docx --author "Jane Doe"   # one reviewer's revisions only
... changes.py out.docx --limit 0 --width 0   # everything (large; avoid)
```

Marks: `[-deleted-]`, `{+inserted+}`, `{~formatting-only change (w:rPrChange)~}`, `->` tab, `|` break.

| Option | Script | Values |
|---|---|---|
| `--variant` | run | `British` (default), `American` |
| `--guide` | run | `old_inhouse` (default; keys of `config.STYLE_GUIDES`) |
| `--author` | run | revision author name (default `Claude test run`) |
| `--out` | run | output folder (default `.claude/runs/`) |
| `--para`, `--grep`, `--author` | changes | filters |
| `--limit`, `--width`, `--all` | changes | output size; `--all` also lists unchanged paragraphs |

## Paragraph ids

Both scripts use `docx_extract.iter_all_paragraphs` order: **all body paragraphs first, then
tables**, not document order. So `failed_paragraphs` / `flagged` ids from `run.py` can be passed
straight to `changes.py --para`.

## Verifying a fix for a client report

1. Get the manuscript (or the smallest excerpt that reproduces the bug) from the user.
2. Run with the variant and guide the client used.
3. Check the specific paragraphs with `--grep` / `--para`, not the full listing.
4. Put the summary numbers (paragraphs, edited, failed, seconds) in the commit message.
5. XML-structure changes still need the user to open the output in Word and Accept/Reject All.

## When it fails

| Symptom | Fix |
|---|---|
| `KeyError: GEMINI_API_KEY` / secrets not found | `.streamlit/secrets.toml` is missing on this machine. The user copies it by hand. |
| `APIError` 4xx on the first call | Billing, quota or auth, not code. Tell the user and don't retry. |
| `ParagraphTooLargeError` | A single paragraph is over 30k tokens. That is expected behaviour, not a bug. |
| Streamlit "missing ScriptRunContext" warnings | Harmless outside `streamlit run`. |

## Common mistakes

- **Reading the output docx's XML.** Use `changes.py`. A hook blocks reading `.docx` files.
- **Re-running Gemini to look at the same output again.** The docx in `.claude/runs/` is still there.
- **Running the whole manuscript to test one paragraph.** Make a small docx with the problem
  paragraphs (python-docx in a scratch script) and run that.
