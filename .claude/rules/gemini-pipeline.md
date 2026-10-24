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
  Never cache it, because its httpx pool binds to the first event loop. `_correct_all_chunks` and
  `_guardrail_retries` each create one client for their own `asyncio.run()` and close it.
- `thinking_level="MEDIUM"` and `seed=42`, with a 600s request timeout. Reproducibility is best effort only.
  **Never send `thinking_budget`, `temperature`, `top_p` or `top_k`.** Google announced (Oct 2026)
  that upcoming models reject them with 400. The level was chosen by measurement (see `config.py`).
  `high` risks per-request timeouts on full chunks. `low` found fewer corrections.

## Per-paragraph post-processing (`main.py`)

Passes in `process_document`:
1. Per paragraph: `_postprocess` (`sanitize_for_xml` → `_restore_corrupted_words` →
   `_curl_introduced_straight_quotes` → `_convert_quotes` → `_normalise_dashes` → collapse double
   spaces → `_fix_dialogue_tags` (“Why?” She asked → she asked; speech verbs only, so action beats
   keep their capital) → `_restore_edge_whitespace`) → `strip_italic_markers` → `changed_token_ratio` → decide whether the
   guardrail applies.
2. All guardrail retries **concurrently** (`_guardrail_retries`, same semaphore cap, one
   single-paragraph request each, exceptions returned per paragraph). They used to run serially,
   about 5 minutes on `book 3`.
3. Per paragraph: pick the less-changed version and flag. Then `_first_use_italics`, then
   `apply_track_changes` for every paragraph.

Every paragraph step is inside its own try/except. A failure adds it to `failed_ids` and the run continues.

## Consistency sweep and acronyms (`main.py`)

- Chunks can't see each other, so after the main pass `_consistency_rules` collects one-word
  corrections that look like spelling (similarity >= `SWEEP_MIN_WORD_SIMILARITY`, not a plural or tense
  change, one target only, made in >= `SWEEP_MIN_FIXED_SHARE` of the word's occurrences).
  `_consistency_sweep` re-sends only the paragraphs still containing the old word, with the rules and
  "make no other changes". Gemini decides each occurrence. A failed sweep call keeps the first-pass text.
  On Prakash Shah: 50 paragraphs, 1 request.
- Each chunk is told which acronyms already appeared before it (`_earlier_acronyms_note`), so the
  style guide's "expand at first mention" lands on the real first mention.
- `_curl_introduced_straight_quotes`: a straight quote Gemini introduces into a paragraph that had
  none is curled in code.
- Document-wide typography, decided from the original text before any paragraph is processed:
  quote style (`_quote_style`, see `style-guides.md`) and em dash spacing between words
  (`_dash_style`: spaced or closed, majority wins, closed on a tie).
- Non-English words: Gemini wraps an uncommon one in `~…~` at **every** use; `_first_use_italics`
  italicises its earliest whole-word occurrence in the manuscript (marked there or not) and no other.
  A first use in a failed paragraph means no italics anywhere.
- Author italics: Gemini can't see formatting, so `_italic_candidates` collects the words the author
  italicised (stretches of <= `FOREIGN_TERM_MAX_WORDS` words, top `FOREIGN_TERM_MAX_CANDIDATES`) and
  one `gemini_client.find_foreign_terms` call per run picks the non-English ones. Those join the `~`
  terms. `_roman_later_uses` takes the author's italics off every later use (tracked `w:rPrChange`),
  only where the italic stretch is that word alone, so titles and italic sentences keep theirs.
  A failed classification call only means later uses keep their italics.
- `_restore_corrupted_words`: a word with a non-ASCII letter that comes back ASCII-only or with a new
  digit/backslash (fiancé → fiance9e) is put back. The payload is sent with `ensure_ascii=False`
  for the same reason: Gemini used to see `\u00e9` escapes.

## Review flags (`ReviewFlag`, client rules Oct 2026)

- Tense shifts within a scene, sentence fragments and wrong-word/word-class errors follow "detect →
  assess context → correct only when confident". Confident fixes are tracked changes; anything else
  comes back in the paragraph's `flags` (`category`, `quote`, `comment`) and becomes a Word comment
  (`tc.add_review_comment`), never a guessed edit. A sentence Gemini can only partly repair is
  flagged, never half-fixed.
- `awkward` (grammatical but tangled or ambiguous) and `repetition` (noticeably repeated sentence
  structure or word) are always flags, never edits: fixing them would mean rephrasing the author.
- Deliberate fragments in narration ("Until one day.", "Only him.") are not flagged; the client's
  file had several such flags. At most two flags per paragraph.
- `main._plain` strips `*`/`~` markers from a flag's quote and comment (a marker in the quote made it
  miss the text and fall back to the whole paragraph).
- `correct_paragraphs(..., flags_out=)` collects them. Main pass and guardrail retry (when the
  retried version is kept) only; sweep flags are ignored. Failed paragraphs get none.
- Count: `ProcessResult.review_comments` → run doc `review_comments` → shown in the app.

## Guardrail

- If more than `MAX_CHANGED_TOKEN_RATIO` (0.60, the user's choice) of the words changed, retry once with
  `GUARDRAIL_RETRY_INSTRUCTION` and keep whichever version changed less. If it is still over the
  limit, add it to `flagged_paragraphs`.
- The ratio is the share of **words** changed (punctuation and spacing fixes count 0). A full
  rewrite scores 1.0. Paragraphs under `MIN_TOKENS_FOR_CHANGE_RATIO_GUARDRAIL` (5) words skip it.

## The system prompt

- Light copyedit (client decision, Oct 2026): ungrammatical, incomplete or broken sentences are
  repaired minimally; correct sentences are never rephrased. The prompt owns BrE/AmE conversion and the "read the whole sentence first; a
  valid-but-wrong word is worse than leaving it" rule. See `style-guides.md` for how variant rules
  and style guide rules split.
- Prompt edits change output on every manuscript. Keep them minimal and targeted at the reported
  bug, and verify with a real run on the manuscript that showed the bug.

## Concurrency and speed

- `GEMINI_MAX_CONCURRENT_CHUNKS` = 10 per run (env-overridable on the job). Runs execute in parallel in
  separate Cloud Run containers but share one Gemini key (~150 RPM), so raise it only after checking
  for 429s in the job logs. 429s are retried with backoff. If they run out, paragraphs fail (red banner).
- **Never trade quality for speed.** Don't shrink chunks, cut the thinking budget or trim prompt
  context. Speed may only come from changes that leave each call's input unchanged: concurrency,
  parallel guardrail retries, removing serial overhead.
- The pipeline must stay free of Streamlit (it runs in `worker.py`). Secrets via `app_secrets`.

## Model or tunable changes

Verify a model bump end to end on a real manuscript, and record paragraphs, edited, failed and
seconds in the commit message. Tunables live only in `config.py`.
