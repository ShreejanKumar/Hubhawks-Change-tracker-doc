---
paths:
  - 'gemini_client.py'
  - 'main.py'
  - 'chunking.py'
  - 'config.py'
---

# Gemini calls, chunking, retries and the guardrail

History: `C:/Users/kumar/Downloads/README.md` (local, not in the repo) §4.5, §4.8, §4.10, §4.11, §4.12, and §7 for the tunables table.

## Chunking

- Greedy packing up to `INPUT_BUDGET_TOKENS_PER_CHUNK` (chars/4) and `MAX_PARAGRAPHS_PER_CHUNK`.
  Empty paragraphs are dropped. A paragraph over the cap raises `ParagraphTooLargeError`. Never split
  a paragraph.
- Context such as tense and capitalisation consistency spans one chunk only. Concurrency changes
  wall-clock time, not results.

## Calls (`gemini_client.py`)

- The payload is a JSON array `[{id, text}]`, and the response schema is `list[ParagraphCorrection]`.
  The returned id set must **equal** the id set sent. On a mismatch or transient failure, halve
  recursively down to one paragraph. A paragraph that fails alone stays unchanged and goes into `failed_ids`.
- The halves are awaited **sequentially** on purpose, so retries can't push past the semaphore.
- Retries: the SDK `HttpRetryOptions` covers 429/5xx (`GEMINI_RETRY_ATTEMPTS`), and a manual loop covers
  timeouts and connection resets (`GEMINI_MANUAL_RETRY_ATTEMPTS`). `_is_fatal_api_error`: any
  `APIError` whose code is outside `GEMINI_RETRYABLE_HTTP_STATUS_CODES` is raised at once, with no
  halving, and sibling chunks are cancelled. Billing or auth problems must never grind through retries.
- `new_client()` gives a fresh client for each `asyncio.run()`, closed with `await client.aio.aclose()`.
  Never cache it, because its httpx pool binds to the first event loop. The guardrail retry uses
  `correct_paragraphs_standalone`, which owns its own client.
- `temperature=0`, `seed=42`, `thinking_budget` capped (8192). Reproducibility is best effort only.

## Per-paragraph post-processing (`main.py`)

In this order: `sanitize_for_xml` → `_restore_edge_whitespace` → `strip_italic_markers` →
`changed_token_ratio` → guardrail → `apply_track_changes`. Each paragraph is inside its own
try/except. A failure adds it to `failed_ids` and the run continues.

## Guardrail

- If more than `MAX_CHANGED_TOKEN_RATIO` (0.35) of tokens changed, retry once with
  `GUARDRAIL_RETRY_INSTRUCTION` and keep whichever version changed less. If it is still over the
  limit, add it to `flagged_paragraphs`.
- Paragraphs under `MIN_TOKENS_FOR_CHANGE_RATIO_GUARDRAIL` (10) tokens skip the guardrail.

## The system prompt

- Proofread only. The prompt owns BrE/AmE conversion and the "read the whole sentence first; a
  valid-but-wrong word is worse than leaving it" rule. See `style-guides.md` for how variant rules
  and style guide rules split.
- Prompt edits change output on every manuscript. Keep them minimal and targeted at the reported
  bug, and verify with a real run on the manuscript that showed the bug.

## Model or tunable changes

Verify a model bump end to end on a real manuscript, and record paragraphs, edited, failed and
seconds in the commit message. Tunables live only in `config.py`.
