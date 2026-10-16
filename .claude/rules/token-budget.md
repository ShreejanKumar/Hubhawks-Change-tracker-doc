# Working within a token budget

These rules apply to every task. They keep reads, runs and replies small.

## Reading

- Grep for the function or term first, then Read only that range (`offset`/`limit`). Files worth
  being careful with: `docx_track_changes.py` (~460 lines), `gemini_client.py` (~320, most of it the
  prompt), `C:/Users/kumar/Downloads/README.md` (local, not in the repo) (~370).
- Never read the PDFs, `.docx` files or secrets. A hook blocks it. To see what a `.docx` contains,
  use `.claude/skills/proofread-run/scripts/changes.py`.
- Don't re-read a file after editing it. Don't read a file already read in this session unless it changed.
- `CLAUDE.md` and the rules are the summary. Only open `C:/Users/kumar/Downloads/README.md` (local, not in the repo) for the history behind a
  decision, and grep it for the section.

## Running

- A real proofreading run costs money (paid Gemini key) and takes 1–3 minutes. Ask before running one,
  and prefer the smallest document that reproduces the problem.
- For changes that don't touch Gemini (extraction, diffing, XML), test those functions directly with
  a few hand-written strings or a tiny docx. No API call is needed.
- Don't start the Streamlit UI to check pipeline behaviour. Use the skill's `run.py`.
- Summarise command output. Pipe long output through `head`, or use `--limit` on the skill scripts.

## Replying

- Keep replies short. Report what changed and what was verified. Don't paste code that's already
  in the files, and don't restate the plan after doing it.
- Ask one clear question when blocked instead of exploring several branches speculatively.
