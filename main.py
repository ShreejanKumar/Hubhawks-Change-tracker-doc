"""Orchestrates the full pipeline: extract -> chunk -> Gemini ->
consistency sweep -> chapter numbers -> guardrail -> track-changes
injection -> save.
"""

from __future__ import annotations

import asyncio
import difflib
import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from docx import Document

import chapter_numbering
import chunking
import docx_extract as de
import docx_track_changes as tc
import gemini_client
import style_guides
from config import (
    GEMINI_MAX_CONCURRENT_CHUNKS,
    MAX_CHANGED_TOKEN_RATIO,
    MIN_TOKENS_FOR_CHANGE_RATIO_GUARDRAIL,
    SWEEP_MAX_RULES_PER_REQUEST,
    SWEEP_MIN_FIXED_SHARE,
    SWEEP_MIN_WORD_LENGTH,
    SWEEP_MIN_WORD_SIMILARITY,
)

logger = logging.getLogger(__name__)

GUARDRAIL_RETRY_INSTRUCTION = (
    "Your previous correction changed too much text. Make ONLY essential "
    "grammar/spelling/punctuation corrections and preserve the original wording."
)

SWEEP_INSTRUCTION = (
    "These paragraphs have already been copyedited. Elsewhere in this same "
    "manuscript the corrections listed below were made. Apply the same "
    "correction to each occurrence in these paragraphs where it is the same "
    "error in the same sense, and leave an occurrence unchanged where the "
    "word is correct as it stands (a different sense, a name, a quotation, "
    "or non-English text). Make NO other changes of any kind. A span wrapped "
    "in a pair of asterisks is already marked for italics: keep those "
    "asterisks exactly where they are.\n\nCorrections made elsewhere:\n"
)

_WORD_RE = re.compile(r"\w+(?:['’]\w+)*")
# Two to eight capitals (optionally a plural s): NASA, WHO, HMT, MPs.
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z&]{1,7}s?\b")
# A paragraph in capitals (a heading, a shouted line) isn't a usable first
# mention: every word in it looks like an acronym.
_MAX_CAPITALS_SHARE = 0.6


def _is_mostly_capitals(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) > _MAX_CAPITALS_SHARE


def _acronym_first_mentions(texts: dict[int, str]) -> dict[str, int]:
    first: dict[str, int] = {}
    for i in sorted(texts):
        if _is_mostly_capitals(texts[i]):
            continue
        for acronym in _ACRONYM_RE.findall(texts[i]):
            first.setdefault(acronym, i)
    return first


def _earlier_acronyms_note(paragraphs: list[chunking.IndexedParagraph], first_mentions: dict[str, int]) -> str:
    """Chunks are corrected independently, so a chunk can't tell an
    acronym's first mention in the manuscript from a later one. List the
    acronyms in this chunk that already appeared before it, so the style
    guide's "expand at first mention" lands only on the real first mention."""
    start = paragraphs[0].id
    seen = sorted({
        a for p in paragraphs for a in _ACRONYM_RE.findall(p.text)
        if first_mentions.get(a, start) < start
    })
    if not seen:
        return ""
    return (
        "These acronyms already appeared earlier in the manuscript, so they are "
        "not a first mention here and must not be expanded: " + ", ".join(seen)
    )


def _word_substitutions(original: str, corrected: str) -> list[tuple[str, str]]:
    """One-word-for-one-word replacements between two versions of a paragraph."""
    old_words, new_words = _WORD_RE.findall(original), _WORD_RE.findall(corrected)
    matcher = difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False)
    return [
        (old_words[i1], new_words[j1])
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag == "replace" and i2 - i1 == 1 and j2 - j1 == 1
    ]


def _is_consistency_rule(old: str, new: str) -> bool:
    """A correction worth repeating wherever the same word recurs: a
    spelling, variant or capitalisation fix, not a grammar change that only
    holds in its own sentence."""
    if len(old) < SWEEP_MIN_WORD_LENGTH or old == new:
        return False
    lower_old, lower_new = old.lower(), new.lower()
    if lower_old == lower_new:
        # Capitalising just the first letter usually follows the word's
        # position in the sentence; MOSSAD -> Mossad is a real rule.
        return old[1:] != new[1:]
    if lower_new.startswith(lower_old) or lower_old.startswith(lower_new):
        # heart -> hearts, learn -> learned: number and tense depend on the sentence.
        return False
    return difflib.SequenceMatcher(None, lower_old, lower_new).ratio() >= SWEEP_MIN_WORD_SIMILARITY


def _consistency_rules(originals: dict[int, str], corrected_by_id: dict[int, str]) -> dict[str, str]:
    """old word -> new word for every repeatable correction Gemini made. A
    word corrected to two different things in different places is
    context-dependent and is left out, and so is one Gemini corrected in
    only a small share of the places it occurs: "has" -> "had" once among
    hundreds of untouched "has" is a fix to one sentence, while utilize ->
    utilise is made almost everywhere and the few misses are the point."""
    targets: dict[str, set[str]] = {}
    fixed: Counter[str] = Counter()
    for i, original in originals.items():
        corrected = corrected_by_id.get(i)
        if corrected is None or corrected == original:
            continue
        for old, new in _word_substitutions(original, corrected):
            if _is_consistency_rule(old, new):
                targets.setdefault(old, set()).add(new)
                fixed[old] += 1
    remaining = Counter(w for text in corrected_by_id.values() for w in _WORD_RE.findall(text))
    return {
        old: next(iter(news))
        for old, news in targets.items()
        if len(news) == 1 and fixed[old] / (fixed[old] + remaining[old]) >= SWEEP_MIN_FIXED_SHARE
    }


async def _consistency_sweep(
    corrected_by_id: dict[int, str],
    rules: dict[str, str],
    skip_ids: set[int],
    style_guide_text: str,
    variant: str,
) -> dict[int, str]:
    """Re-check every paragraph that still contains a word corrected
    elsewhere, telling Gemini which corrections were made, and return the
    updated texts. Gemini decides each occurrence in context; the code only
    finds where to look. A paragraph whose sweep call fails keeps its
    first-pass correction, so a failure here never loses work."""
    relevant: dict[int, list[str]] = {}
    for i, text in corrected_by_id.items():
        if i in skip_ids:
            continue
        words = set(_WORD_RE.findall(text))
        hits = [old for old in rules if old in words]
        if hits:
            relevant[i] = hits
    if not relevant:
        return {}

    chunks = chunking.build_chunks(
        [chunking.IndexedParagraph(id=i, text=corrected_by_id[i]) for i in sorted(relevant)]
    )
    logger.info(
        "Consistency sweep: %d rules, %d paragraphs in %d requests",
        len(rules), len(relevant), len(chunks),
    )
    client = gemini_client.new_client()
    semaphore = asyncio.Semaphore(GEMINI_MAX_CONCURRENT_CHUNKS)

    async def sweep_chunk(chunk: list[chunking.IndexedParagraph]):
        olds = sorted({old for p in chunk for old in relevant[p.id]})[:SWEEP_MAX_RULES_PER_REQUEST]
        instruction = SWEEP_INSTRUCTION + "\n".join(f"- {old} -> {rules[old]}" for old in olds)
        async with semaphore:
            return await gemini_client.correct_paragraphs(
                client, chunk, style_guide_text, variant, extra_instruction=instruction
            )

    tasks = [asyncio.create_task(sweep_chunk(chunk)) for chunk in chunks]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        await client.aio.aclose()

    swept: dict[int, str] = {}
    for chunk_result, chunk_failed in results:
        if chunk_failed:
            logger.warning("Consistency sweep kept the first-pass text for paragraphs %s", sorted(chunk_failed))
        swept.update({i: t for i, t in chunk_result.items() if i not in chunk_failed})
    return swept


def _restore_edge_whitespace(original: str, corrected: str) -> str:
    """Gemini's JSON output doesn't reliably round-trip leading/trailing
    whitespace even when nothing else changed, which otherwise surfaces as a
    spurious "deleted space" tracked change with no visible content. Since
    edge whitespace is never something worth flagging as an edit, re-anchor
    the corrected text to the original's leading/trailing whitespace."""
    core = corrected.strip()
    if not core:
        return original
    leading = re.match(r"\s*", original).group(0)
    trailing = re.search(r"\s*\Z", original).group(0)
    return leading + core + trailing


_OPENING_CONTEXT = " \t\n([{—–-/"


def _curl_introduced_straight_quotes(original: str, corrected: str) -> str:
    """Gemini occasionally returns straight quote marks where the manuscript
    has typographic ones (on Prakash Shah, Oct 2026, a chunk turned
    “urgent work” into "urgent work"). Curl every straight quote of
    a kind the original paragraph never contained; a paragraph that already
    used straight quotes is left to the prompt, since that's the author's
    own typing."""
    for straight, opening, closing in (('"', "“", "”"), ("'", "‘", "’")):
        if straight in original or straight not in corrected:
            continue
        chars = list(corrected)
        for k, ch in enumerate(chars):
            if ch == straight:
                is_opening = k == 0 or chars[k - 1] in _OPENING_CONTEXT
                # A straight ' between letters is an apostrophe (it's), never an opening quote.
                chars[k] = opening if is_opening else closing
        corrected = "".join(chars)
    return corrected


@dataclass
class FlaggedParagraph:
    paragraph_id: int
    original: str
    corrected: str
    changed_ratio: float


@dataclass
class ProcessResult:
    docx_bytes: bytes
    total_paragraphs: int
    edited_paragraphs: int
    flagged_paragraphs: list[FlaggedParagraph] = field(default_factory=list)
    failed_paragraphs: list[int] = field(default_factory=list)


ProgressCallback = Callable[[int, int], None]


@dataclass
class _PreparedCorrection:
    raw_corrected_text: str
    corrected_text: str
    italic_spans: list[tuple[int, int]]
    ratio: float
    needs_retry: bool


async def _guardrail_retries(
    texts_by_id: dict[int, str],
    style_guide_text: str,
    variant: str,
) -> dict[int, tuple[dict[int, str], set[int]] | BaseException]:
    """One "make fewer changes" retry per paragraph, concurrently (bounded
    like the chunks) over one shared client. Each paragraph is still its own
    single-paragraph request, as before. Exceptions are returned per
    paragraph rather than raised, so one paragraph's failure is isolated to
    that paragraph exactly as when these ran one at a time."""
    client = gemini_client.new_client()
    semaphore = asyncio.Semaphore(GEMINI_MAX_CONCURRENT_CHUNKS)

    async def retry_one(i: int, text: str):
        async with semaphore:
            return await gemini_client.correct_paragraphs(
                client,
                [chunking.IndexedParagraph(id=i, text=text)],
                style_guide_text,
                variant,
                extra_instruction=GUARDRAIL_RETRY_INSTRUCTION,
            )

    try:
        ids = list(texts_by_id)
        outcomes = await asyncio.gather(
            *(retry_one(i, texts_by_id[i]) for i in ids), return_exceptions=True
        )
    finally:
        await client.aio.aclose()
    return dict(zip(ids, outcomes))


async def _correct_all_chunks(
    chunks: list[list[chunking.IndexedParagraph]],
    style_guide_text: str,
    variant: str,
    progress_callback: Optional[ProgressCallback],
    acronym_first_mentions: dict[str, int],
) -> tuple[dict[int, str], set[int]]:
    """Corrects every chunk concurrently (bounded by
    GEMINI_MAX_CONCURRENT_CHUNKS), sharing one Gemini client across them for
    the lifetime of this event loop. Chunks have no cross-chunk dependency
    -- gemini_client's "surrounding paragraph" context only spans paragraphs
    within a single chunk to begin with -- so this only changes wall-clock
    time, never the corrections themselves.

    If any chunk hits a fatal (non-retryable) API error, the remaining
    in-flight chunks are cancelled before the client is closed and the
    exception propagates to the caller, rather than letting them keep
    running against a client that's about to be torn down.
    """
    client = gemini_client.new_client()
    semaphore = asyncio.Semaphore(GEMINI_MAX_CONCURRENT_CHUNKS)
    total = max(len(chunks), 1)
    done = 0

    async def run_chunk(chunk: list[chunking.IndexedParagraph]) -> tuple[dict[int, str], set[int]]:
        nonlocal done
        note = _earlier_acronyms_note(chunk, acronym_first_mentions)
        async with semaphore:
            chunk_result, chunk_failed = await gemini_client.correct_paragraphs(
                client, chunk, style_guide_text, variant, extra_instruction=note
            )
        done += 1
        if progress_callback:
            progress_callback(done, total)
        return chunk_result, chunk_failed

    tasks = [asyncio.create_task(run_chunk(chunk)) for chunk in chunks]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        await client.aio.aclose()

    corrected_by_id: dict[int, str] = {}
    failed_ids: set[int] = set()
    for chunk_result, chunk_failed in results:
        corrected_by_id.update(chunk_result)
        failed_ids |= chunk_failed
    return corrected_by_id, failed_ids


def process_document(
    docx_bytes: bytes,
    author: str,
    variant: str,
    style_guide_key: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> ProcessResult:
    document = Document(io.BytesIO(docx_bytes))
    style_guide_text = style_guides.get_style_guide_text(style_guide_key)

    all_paragraphs = list(de.iter_all_paragraphs(document))
    extractions = {i: de.extract_paragraph(p) for i, p in enumerate(all_paragraphs)}

    indexed = [
        chunking.IndexedParagraph(id=i, text=ext.text)
        for i, ext in extractions.items()
    ]
    chunks = chunking.build_chunks(indexed)
    originals = {i: ext.text for i, ext in extractions.items() if ext.text}

    corrected_by_id, failed_ids = asyncio.run(
        _correct_all_chunks(
            chunks, style_guide_text, variant, progress_callback, _acronym_first_mentions(originals)
        )
    )

    # Carry each repeatable correction to every paragraph the chunk that
    # made it couldn't see.
    rules = _consistency_rules(originals, corrected_by_id)
    if rules:
        corrected_by_id.update(asyncio.run(
            _consistency_sweep(corrected_by_id, rules, failed_ids, style_guide_text, variant)
        ))

    # Chapter numbers against the table of contents. Typed numbers become a
    # text fix on top of Gemini's correction; list numbers are fixed in the
    # paragraph properties after the text revisions are in place.
    chapter_fixes = chapter_numbering.plan_fixes(all_paragraphs, [ext.text for ext in extractions.values()])
    for fix in chapter_fixes:
        logger.info("Chapter heading %d is numbered %d; table of contents says %d", fix.paragraph_id, fix.current, fix.expected)
        if fix.typed:
            i = fix.paragraph_id
            corrected_by_id[i] = chapter_numbering.renumber_text(corrected_by_id.get(i, originals[i]), fix.expected)

    revision_date = datetime.now(timezone.utc).isoformat()
    id_gen = tc.make_id_counter(document)

    edited_ids: set[int] = set()
    flagged: list[FlaggedParagraph] = []

    # Pass 1: prepare each paragraph's correction and find the ones whose
    # change ratio trips the guardrail.
    prepared: dict[int, _PreparedCorrection] = {}
    for i in range(len(all_paragraphs)):
        extraction = extractions[i]
        if not extraction.text or i not in corrected_by_id:
            continue
        try:
            raw_corrected_text = _restore_edge_whitespace(
                extraction.text,
                _curl_introduced_straight_quotes(extraction.text, tc.sanitize_for_xml(corrected_by_id[i])),
            )
            corrected_text, italic_spans = tc.strip_italic_markers(raw_corrected_text)
            if corrected_text == extraction.text and not italic_spans:
                continue
            ratio = tc.changed_token_ratio(extraction, corrected_text)
            guardrail_applies = tc.token_count(extraction.text) >= MIN_TOKENS_FOR_CHANGE_RATIO_GUARDRAIL
            prepared[i] = _PreparedCorrection(
                raw_corrected_text, corrected_text, italic_spans, ratio,
                needs_retry=guardrail_applies and ratio > MAX_CHANGED_TOKEN_RATIO,
            )
        except Exception:
            logger.exception("Failed to prepare correction for paragraph %d; left unchanged", i)
            failed_ids.add(i)

    # Pass 2: all guardrail retries at once. They used to run one after
    # another after every chunk had finished; on a 2,100-paragraph manuscript
    # ~23 sequential single-paragraph calls took ~5 minutes, longer than the
    # chunks themselves. Each retry sends exactly the same request as before,
    # so this changes wall-clock time only, never the corrections.
    retry_ids = [i for i, p in prepared.items() if p.needs_retry]
    retry_outcomes = (
        asyncio.run(_guardrail_retries(
            {i: extractions[i].text for i in retry_ids}, style_guide_text, variant
        ))
        if retry_ids
        else {}
    )

    # Pass 3: pick the less-changed version where retried, flag what still
    # changes too much, and inject the tracked changes.
    for i, prep in prepared.items():
        paragraph, extraction = all_paragraphs[i], extractions[i]
        try:
            corrected_text, italic_spans, ratio = prep.corrected_text, prep.italic_spans, prep.ratio
            if prep.needs_retry:
                outcome = retry_outcomes[i]
                if isinstance(outcome, BaseException):
                    raise outcome
                retry_result, retry_failed = outcome
                failed_ids |= retry_failed
                raw_retried_text = _restore_edge_whitespace(
                    extraction.text,
                    _curl_introduced_straight_quotes(
                        extraction.text, tc.sanitize_for_xml(retry_result.get(i, prep.raw_corrected_text))
                    ),
                )
                retried_text, retried_spans = tc.strip_italic_markers(raw_retried_text)
                retried_ratio = tc.changed_token_ratio(extraction, retried_text)
                if retried_ratio <= ratio:
                    corrected_text, ratio, italic_spans = retried_text, retried_ratio, retried_spans
                if ratio > MAX_CHANGED_TOKEN_RATIO:
                    flagged.append(FlaggedParagraph(i, extraction.text, corrected_text, ratio))

            if corrected_text == extraction.text and not italic_spans:
                continue

            tc.apply_track_changes(
                paragraph, extraction, corrected_text, author, revision_date, id_gen, italic_spans
            )
            edited_ids.add(i)
        except Exception:
            # A single paragraph's corrected text failing to apply (e.g. an
            # unexpected XML-incompatible edge case) must never take down the
            # whole run and lose every other paragraph's already-fetched
            # corrections — isolate it as a failure and keep going.
            logger.exception("Failed to apply track changes to paragraph %d; left unchanged", i)
            failed_ids.add(i)

    for fix in chapter_fixes:
        if fix.typed:
            continue
        try:
            chapter_numbering.apply_list_fix(all_paragraphs[fix.paragraph_id], fix.expected, author, revision_date, id_gen)
            edited_ids.add(fix.paragraph_id)
        except Exception:
            logger.exception("Failed to renumber chapter heading %d; left unchanged", fix.paragraph_id)

    out_stream = io.BytesIO()
    document.save(out_stream)

    return ProcessResult(
        docx_bytes=out_stream.getvalue(),
        total_paragraphs=len(all_paragraphs),
        edited_paragraphs=len(edited_ids),
        flagged_paragraphs=flagged,
        failed_paragraphs=sorted(failed_ids),
    )
