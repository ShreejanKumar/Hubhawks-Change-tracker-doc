# Hubhawks Change Tracker

Streamlit app for Hubhawks (a publisher). An editor uploads a `.docx` and picks British or American
English, a style guide and a reviewer name. Gemini proofreads the text, and the same `.docx` comes back
with every correction as real Word Track Changes (`w:ins` / `w:del` / `w:rPrChange`). Each run is saved
to GCS (docx) and Firestore (metadata).

The full history of problems, fixes and reasons is in `C:/Users/kumar/Downloads/README.md` (local, not in the repo). Read only the section you need
(grep it); don't load the whole file by default. Topic rules in `.claude/rules/` load on their own when
you touch the matching files.

## Map

| File | Role |
|---|---|
| `app.py` | UI: login, history sidebar, upload form, run lock, error banners |
| `main.py` | `process_document`: extract → chunk → Gemini (concurrent) → guardrail → track changes |
| `gemini_client.py` | System prompt, Gemini calls, retries, halve-and-retry |
| `chunking.py` | Pack paragraphs into chunks (never splits a paragraph) |
| `docx_extract.py` | Paragraph text + offset map back to the XML |
| `docx_track_changes.py` | Diff and rebuild paragraph XML with revisions |
| `history_store.py` | GCS + Firestore history |
| `config.py` | Every tunable and the style guide registry |
| `style_guides/*.txt` | Hand-edited guides sent to Gemini (source of truth) |

## Product rule

Proofread, never rewrite. No restructuring, no new content, no change of voice, no translating.

## Invariants (details in the rules files)

- Never split a paragraph across LLM calls. The LLM never touches tabs, breaks or hyperlink text.
- Returned paragraph ids must equal the ids sent, otherwise halve and retry.
- "Failed to check" must never look like "no changes needed" (`failed_paragraphs` → red banner).
- Only 429/5xx are retried. Every other API error fails fast.
- BrE/AmE rules live in the system prompt, never in the style guides.
- Valid OOXML: revision ids are unique and above existing ids. `w:rPrChange` is the last child of `w:rPr`.
- One run at a time app-wide, at most 3 concurrent chunk calls. A fresh Gemini client per `asyncio.run()`.
- No dictionary-based spelling pass, ever.
- Verify any model change end to end on a real manuscript before committing.

## Environment

- This machine: Windows, conda env `book` (Python 3.10.12). Run with
  `conda run -n book streamlit run app.py`, or the skill script for a headless run.
- Secrets: `.streamlit/secrets.toml` and `gcp-service-account.json`, both gitignored. Never read or print them.
- Deployed on Streamlit Community Cloud from `main`: one process shared by every visitor.

## Working conventions

- Commit messages: problem, root cause, fix, why, and how it was verified (with real numbers).
- Code comments explain *why*, at the density already in the file.
- Bugs usually arrive as client reviewer feedback about a specific manuscript.
- No automated tests. Verification is a real run (see the `proofread-run` skill).
