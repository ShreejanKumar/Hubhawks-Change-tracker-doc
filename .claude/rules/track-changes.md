---
paths:
  - 'docx_extract.py'
  - 'docx_track_changes.py'
  - 'scripts/verify_roundtrip.py'
---

# Extraction and Track Changes XML

History: `C:/Users/kumar/Downloads/README.md` (local, not in the repo) §2 "Track changes algorithm", §4.1, §4.4, §4.6.

## Extraction (`docx_extract.py`)

- Each paragraph becomes one flat string plus `Item`s that map character ranges back to XML:
  `TEXT` (a `w:t` with its run's `rPr`), `PROTECTED_CHAR` (`w:tab`/`w:br`/`w:cr`, one placeholder
  char each), `HYPERLINK` (a whole `w:hyperlink`, never edited), `ANCHOR` (zero width: drawings, field
  codes, footnote refs, `w:sym`…).
- Anchors are never sent to the LLM. They are re-spliced at their exact offset. A new element type
  that holds content must become an Item, or it will be lost when the paragraph is rebuilt.
- `iter_all_paragraphs` recurses into tables and nested tables. Paragraph ids are their position in
  that order, so any change to the traversal changes the ids.

## Diff and rebuild (`docx_track_changes.py`)

- Token diff (`\s+|\w+|[^\w\s]`, `SequenceMatcher(autojunk=False)`), mapped to character offsets.
- `_suppress_respacing_opcodes`: a change that only resizes whitespace becomes "equal" (it would be
  an invisible revision). Inserting a space where there was none, or removing one outright, is kept.
- `changed_token_ratio` / `token_count` (the guardrail) count **words only** (`\w+`), not the diff
  tokens. Whitespace tokens almost always align, which made a full rewrite score only ~0.5.
- `_clip_opcodes`: any edit that touches a protected range is dropped for its whole span. This is
  deliberately conservative. Don't loosen it without a test that covers hyperlinks and tabs.
- Opcodes are split at Item boundaries so every run keeps its own formatting. A replace becomes a
  `w:del` (original `rPr`) plus a `w:ins` (the `rPr` of the first replaced run).
- Remove and replace only the `w:r`/`w:hyperlink` elements that were extracted. Bookmarks, `proofErr`
  and existing revisions stay where they are.

## OOXML validity (Word refuses to open the file if these break)

- Revision `w:id`s come from `make_id_counter`: unique, and above every id already in the document.
- `w:rPrChange` is the **last** child of `w:rPr` and contains a `w:rPr` with the prior properties.
- `chapter_numbering.apply_list_fix` moves a mis-numbered chapter heading onto a copy of its list
  (new `w:abstractNum` with a fresh `w:nsid`, new `w:num`) and records the old `w:pPr` in a
  `w:pPrChange`, the last child of `w:pPr`. `make_id_counter` counts `w:pPrChange` ids too.
- Output text passes through `sanitize_for_xml` (XML 1.0 valid characters only).
- Italics: the model marks a span with one pair of `*…*` (always italic) or `~…~` (non-English word,
  italic at first use only; returned separately). `strip_italic_markers` removes both. A
  changed span gets italic inside its `w:ins`. Unchanged text that only needs italics gets a
  `w:rPrChange`. `roman_spans` (original-text offsets) do the reverse: unchanged italic text loses
  `w:i`/`w:iCs`, recorded in a `w:rPrChange`.

## Review comments

`add_review_comment` runs after `apply_track_changes`. It finds the quote in the paragraph's visible
text (deletions left out), splits a plain run where the quote starts or ends mid-run, and wraps it
in `w:commentRangeStart`/`End` plus a `w:commentReference` run. Quote not found → the whole paragraph,
with the quote named in the comment. Comment ids come from `make_id_counter`, which counts existing
`w:commentRangeStart` ids.

## Testing

Call `extract_paragraph` / `apply_track_changes` on a tiny docx built in a scratch script, then check
the result with `.claude/skills/proofread-run/scripts/changes.py`. No Gemini call is needed. A
change to XML structure still needs a real check in Word (Accept/Reject All) before it ships, and
the user has to do that part.
