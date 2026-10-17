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
import unicodedata
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
    FOREIGN_TERM_MAX_CANDIDATES,
    FOREIGN_TERM_MAX_WORDS,
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
    "in a pair of asterisks or tildes is already marked for italics: keep "
    "those markers exactly where they are.\n\nCorrections made elsewhere:\n"
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


_DIFF_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")


def _is_corrupted_word(old: str, new: str) -> bool:
    """A word with an accented or other non-ASCII letter (fiancé, café,
    Zoë) that came back with its special character lost or mangled: no
    non-ASCII letter left, or a digit or backslash it never had (on a client
    manuscript, Oct 2026, "fiancé" came back as "fiance9e")."""
    if old.isascii() or unicodedata.normalize("NFC", old) == unicodedata.normalize("NFC", new):
        return False
    gained = lambda ch: any(ch(c) for c in new) and not any(ch(c) for c in old)
    return new.isascii() or gained(str.isdigit) or "\\" in new


def _restore_corrupted_words(original: str, corrected: str) -> str:
    """Put back every word whose special characters Gemini corrupted. A
    real fix to such a word (fiancè -> fiancé) keeps its non-ASCII letter
    and is left alone."""
    old_tokens, new_tokens = _DIFF_TOKEN_RE.findall(original), _DIFF_TOKEN_RE.findall(corrected)
    matcher = difflib.SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=False)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old, new = "".join(old_tokens[i1:i2]), "".join(new_tokens[j1:j2])
        corrupted = tag == "replace" and i2 - i1 == 1 and old[0].isalnum() and _is_corrupted_word(old, new)
        out.append(old if corrupted else new)
    return "".join(out)


# Quote marks. The style (single or double) is set once for the whole
# manuscript by counting the opening quote marks of each kind in the
# original text, and every quotation is then converted to it in code:
# chunks can't see each other, so leaving it to Gemini gave a different
# majority in different chunks. Opening marks are counted because a closing
# single quote looks exactly like an apostrophe.
_QUOTE_OPENERS_BEFORE = " \t\n([{—–-/"
# A ’ that can only be a closing quote: after punctuation, or before
# punctuation or the end of the paragraph.
_LATER_SINGLE_CLOSE_RE = re.compile(r"[,.;:!?—–]’|’(?=[,.;:!?—–]|\s*$)")


def _quote_marks(text: str) -> list[tuple[int, str, bool]]:
    """(offset, "double" | "single", is_opening) for every quotation mark
    in `text`, curly or straight, skipping apostrophes (it’s, ’90s, the
    students’ books outside a single quotation)."""
    marks: list[tuple[int, str, bool]] = []
    single_open = 0
    for k, ch in enumerate(text):
        before = text[k - 1] if k else " "
        after = text[k + 1] if k + 1 < len(text) else " "
        opening_position = before in _QUOTE_OPENERS_BEFORE or before in "“‘\"'"
        if ch in "“”\"":
            is_open = ch == "“" or (ch == '"' and opening_position)
            marks.append((k, "double", is_open))
        elif ch in "‘'" and opening_position and not after.isspace() and not after.isdigit():
            marks.append((k, "single", True))
            single_open += 1
        elif ch in "’'" and single_open and not before.isspace() and not after.isalnum():
            if before.isalpha() and after == " " and _LATER_SINGLE_CLOSE_RE.search(text, k + 1):
                # ‘The students’ books,’ he said: a plural possessive inside
                # the quotation, which closes at the later ’.
                continue
            marks.append((k, "single", False))
            single_open -= 1
    return marks


def _quote_style(originals: dict[int, str], variant: str) -> str:
    counts = Counter(kind for text in originals.values() for _, kind, is_open in _quote_marks(text) if is_open)
    if counts["single"] != counts["double"]:
        return "single" if counts["single"] > counts["double"] else "double"
    return "single" if variant == "British" else "double"


_QUOTE_CHARS = {("double", True): "“", ("double", False): "”", ("single", True): "‘", ("single", False): "’"}


def _convert_quotes(text: str, style: str) -> str:
    """Every quotation in `style`, a quote within a quote in the other one.
    A paragraph whose marks don't pair up (a closing mark of one kind while
    a quotation of the other kind is open) is left as it is. An opening mark
    left unclosed is converted only where it opens the paragraph (dialogue
    running on into the next paragraph); anywhere else it is more likely a
    mistyped apostrophe (‘em, ‘cause) and is left alone."""
    other = "single" if style == "double" else "double"
    chars = list(text)
    stack: list[tuple[str, int, str]] = []  # (kind, offset, original char)
    for k, kind, is_open in _quote_marks(text):
        if is_open:
            depth = len(stack)
            stack.append((kind, k, text[k]))
        else:
            if stack and stack[-1][0] != kind:
                return text
            if stack:
                stack.pop()
            depth = len(stack)
        chars[k] = _QUOTE_CHARS[(style if depth % 2 == 0 else other, is_open)]
    paragraph_start = len(text) - len(text.lstrip())
    for _, k, ch in stack:
        if k != paragraph_start:
            chars[k] = ch
    return "".join(chars)


# Em dashes between two words, spaced on either side or not at all
# ("Morals —teachers of life— can"). Dashes against a quote mark or at a
# paragraph edge (dialogue breaking off) are left alone.
_EM_DASH_RE = re.compile(r"(?<=[\w),.;:!?])( ?)—( ?)(?=[\w(])")


def _dash_style(originals: dict[int, str]) -> str:
    """The manuscript's own majority em dash spacing, "spaced" or "closed"
    (closed when tied)."""
    counts = Counter(
        "spaced" if m.group(1) and m.group(2) else "closed"
        for text in originals.values()
        for m in _EM_DASH_RE.finditer(text)
        if bool(m.group(1)) == bool(m.group(2))
    )
    return "spaced" if counts["spaced"] > counts["closed"] else "closed"


def _normalise_dashes(text: str, style: str) -> str:
    return _EM_DASH_RE.sub(" — " if style == "spaced" else "—", text)


def _first_use_italics(
    texts: dict[int, str], terms: set[str], skip_ids: set[int]
) -> dict[int, list[tuple[int, int]]]:
    """The style guide italicises an uncommon non-English word at its first
    use only. Gemini marks every use it sees, but each chunk sees only part
    of the manuscript, so the code picks the first: for every marked word,
    its earliest whole-word occurrence in the manuscript (marked there or
    not) gets the italics and every later one stays roman. A first use in
    a paragraph that couldn't be checked gets none, so no later use is
    wrongly italicised in its place."""
    kept: dict[int, list[tuple[int, int]]] = {}
    for term in sorted(terms):
        pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        for i in sorted(texts):
            m = pattern.search(texts[i])
            if m:
                if i not in skip_ids:
                    kept.setdefault(i, []).append(m.span())
                break
    return kept


def _italic_mask(extraction: de.ParagraphExtraction) -> list[bool]:
    """Per character of the paragraph text: is it italic in the manuscript."""
    mask = [False] * len(extraction.text)
    for it in extraction.items:
        if it.kind == de.TEXT and tc.rpr_has_italic(it.rpr):
            mask[it.start:it.end] = [True] * (it.end - it.start)
    return mask


def _italic_segments(text: str, mask: list[bool]) -> list[tuple[int, int]]:
    segments, k = [], 0
    while k < len(text):
        if not mask[k]:
            k += 1
            continue
        start = k
        while k < len(text) and mask[k]:
            k += 1
        segments.append((start, k))
    return segments


_TERM_WORD_RE = re.compile(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*")


def _italic_candidates(originals: dict[int, str], masks: dict[int, list[bool]]) -> dict[str, str]:
    """Words the author italicised in short italic stretches (up to
    FOREIGN_TERM_MAX_WORDS words, so not titles or emphasised sentences),
    most frequent first: term -> an example paragraph. Gemini can't see
    formatting, so these are sent to it to pick out the non-English ones."""
    counts: Counter[str] = Counter()
    examples: dict[str, str] = {}
    for i in sorted(originals):
        text = originals[i]
        for a, b in _italic_segments(text, masks[i]):
            words = _TERM_WORD_RE.findall(text[a:b])
            if not words or len(words) > FOREIGN_TERM_MAX_WORDS:
                continue
            for word in words:
                key = word.casefold()
                counts[key] += 1
                examples.setdefault(key, text[max(0, a - 150):b + 150])
    return {t: examples[t] for t, _ in counts.most_common(FOREIGN_TERM_MAX_CANDIDATES)}


def _roman_later_uses(
    originals: dict[int, str], masks: dict[int, list[bool]], terms: set[str], skip_ids: set[int]
) -> dict[int, list[tuple[int, int]]]:
    """Ranges of the original text to take out of italics: every use of a
    non-English word after its first one, where the author set it in
    italics on its own (an italic stretch holding only that word, so a
    title or an italicised sentence containing it is left alone)."""
    roman: dict[int, list[tuple[int, int]]] = {}
    for term in terms:
        pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        seen_first = False
        for i in sorted(originals):
            text, mask = originals[i], masks[i]
            for m in pattern.finditer(text):
                if not seen_first:
                    seen_first = True
                    continue
                if i in skip_ids or not all(mask[m.start():m.end()]):
                    continue
                a, b = m.start(), m.end()
                while a > 0 and mask[a - 1]:
                    a -= 1
                while b < len(text) and mask[b]:
                    b += 1
                if [w.casefold() for w in _TERM_WORD_RE.findall(text[a:b])] == _TERM_WORD_RE.findall(term):
                    roman.setdefault(i, []).append((m.start(), m.end()))
    return {i: sorted(spans) for i, spans in roman.items()}


async def _classify_italic_candidates(candidates: dict[str, str], style_guide_text: str) -> list[str]:
    client = gemini_client.new_client()
    try:
        return await gemini_client.find_foreign_terms(client, candidates, style_guide_text)
    finally:
        await client.aio.aclose()


def _postprocess(original: str, corrected: str, quote_style: str, dash_style: str) -> str:
    """Code-level fixes to Gemini's text, before the italic markers are
    stripped (none of them touch a * or ~)."""
    corrected = _restore_corrupted_words(original, tc.sanitize_for_xml(corrected))
    corrected = _curl_introduced_straight_quotes(original, corrected)
    corrected = _normalise_dashes(_convert_quotes(corrected, quote_style), dash_style)
    return _restore_edge_whitespace(original, corrected)


_FLAG_LABELS = {"tense": "Tense", "fragment": "Sentence fragment", "word_choice": "Word choice"}


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
    review_comments: int = 0


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
    flags_out: dict[int, list[gemini_client.ReviewFlag]],
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
                flags_out=flags_out,
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
    flags_out: dict[int, list[gemini_client.ReviewFlag]],
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
                client, chunk, style_guide_text, variant, extra_instruction=note, flags_out=flags_out
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

    review_flags: dict[int, list[gemini_client.ReviewFlag]] = {}
    corrected_by_id, failed_ids = asyncio.run(
        _correct_all_chunks(
            chunks, style_guide_text, variant, progress_callback, _acronym_first_mentions(originals),
            review_flags,
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

    # One quote-mark style and one em dash spacing for the whole manuscript,
    # from the original text.
    quote_style, dash_style = _quote_style(originals, variant), _dash_style(originals)
    logger.info("Quote marks: %s; em dashes: %s", quote_style, dash_style)

    revision_date = datetime.now(timezone.utc).isoformat()
    id_gen = tc.make_id_counter(document)

    edited_ids: set[int] = set()
    flagged: list[FlaggedParagraph] = []

    # Pass 1: prepare each paragraph's correction and find the ones whose
    # change ratio trips the guardrail.
    prepared: dict[int, _PreparedCorrection] = {}
    foreign_terms: set[str] = set()
    for i in range(len(all_paragraphs)):
        extraction = extractions[i]
        if not extraction.text or i not in corrected_by_id:
            continue
        try:
            raw_corrected_text = _postprocess(extraction.text, corrected_by_id[i], quote_style, dash_style)
            corrected_text, italic_spans, foreign_spans = tc.strip_italic_markers(raw_corrected_text)
            foreign_terms |= {corrected_text[s:e].casefold() for s, e in foreign_spans}
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
    retry_flags: dict[int, list[gemini_client.ReviewFlag]] = {}
    retry_outcomes = (
        asyncio.run(_guardrail_retries(
            {i: extractions[i].text for i in retry_ids}, style_guide_text, variant, retry_flags
        ))
        if retry_ids
        else {}
    )

    # Pass 3: pick the less-changed version where retried and flag what
    # still changes too much.
    finals: dict[int, tuple[str, list[tuple[int, int]]]] = {}
    for i, prep in prepared.items():
        extraction = extractions[i]
        try:
            corrected_text, italic_spans, ratio = prep.corrected_text, prep.italic_spans, prep.ratio
            if prep.needs_retry:
                outcome = retry_outcomes[i]
                if isinstance(outcome, BaseException):
                    raise outcome
                retry_result, retry_failed = outcome
                failed_ids |= retry_failed
                raw_retried_text = _postprocess(
                    extraction.text, retry_result.get(i, prep.raw_corrected_text), quote_style, dash_style
                )
                retried_text, retried_spans, retried_foreign = tc.strip_italic_markers(raw_retried_text)
                foreign_terms |= {retried_text[s:e].casefold() for s, e in retried_foreign}
                retried_ratio = tc.changed_token_ratio(extraction, retried_text)
                if retried_ratio <= ratio:
                    corrected_text, ratio, italic_spans = retried_text, retried_ratio, retried_spans
                    review_flags[i] = retry_flags.get(i, [])
                if ratio > MAX_CHANGED_TOKEN_RATIO:
                    flagged.append(FlaggedParagraph(i, extraction.text, corrected_text, ratio))
            finals[i] = (corrected_text, italic_spans)
        except Exception:
            logger.exception("Failed to choose correction for paragraph %d; left unchanged", i)
            failed_ids.add(i)

    # Non-English words: italic at their first use in the manuscript only,
    # roman after that. Gemini marks the ones it spots; the words the author
    # italicised are judged separately, since Gemini can't see formatting.
    masks = {i: _italic_mask(extractions[i]) for i in originals}
    candidates = _italic_candidates(originals, masks)
    if candidates:
        try:
            classified = asyncio.run(_classify_italic_candidates(candidates, style_guide_text))
            logger.info("Author-italicised non-English words: %s", classified)
            foreign_terms |= set(classified)
        except Exception:
            logger.exception("Couldn't classify the author's italicised words; their later uses keep italics")
    final_texts = {i: finals[i][0] if i in finals else text for i, text in originals.items()}
    for i, spans in _first_use_italics(final_texts, foreign_terms, failed_ids).items():
        corrected_text, italic_spans = finals.get(i, (originals[i], []))
        spans = [(s, e) for s, e in spans if all(e <= s2 or s >= e2 for s2, e2 in italic_spans)]
        if corrected_text == originals[i]:
            # Already italic in the manuscript: nothing to change.
            spans = [(s, e) for s, e in spans if not all(masks[i][s:e])]
        if spans:
            finals[i] = (corrected_text, sorted(italic_spans + spans))
    roman_by_id = _roman_later_uses(originals, masks, foreign_terms, failed_ids)

    # Pass 4: inject the tracked changes.
    for i in sorted(set(finals) | set(roman_by_id)):
        paragraph, extraction = all_paragraphs[i], extractions[i]
        corrected_text, italic_spans = finals.get(i, (extraction.text, []))
        roman_spans = roman_by_id.get(i, [])
        try:
            if corrected_text == extraction.text and not italic_spans and not roman_spans:
                continue

            tc.apply_track_changes(
                paragraph, extraction, corrected_text, author, revision_date, id_gen, italic_spans, roman_spans
            )
            edited_ids.add(i)
        except Exception:
            # A single paragraph's corrected text failing to apply (e.g. an
            # unexpected XML-incompatible edge case) must never take down the
            # whole run and lose every other paragraph's already-fetched
            # corrections — isolate it as a failure and keep going.
            logger.exception("Failed to apply track changes to paragraph %d; left unchanged", i)
            failed_ids.add(i)

    # Issues Gemini flagged but didn't correct become Word comments, after
    # the tracked changes so they anchor on the paragraph's final text.
    review_comments = 0
    for i in sorted(review_flags):
        if i in failed_ids or not originals.get(i):
            continue
        for flag in review_flags[i]:
            try:
                label = _FLAG_LABELS.get(flag.category, "Review")
                tc.add_review_comment(
                    document, all_paragraphs[i], flag.quote.strip(), f"{label}: {flag.comment.strip()}",
                    author, next(id_gen),
                )
                review_comments += 1
            except Exception:
                logger.exception("Failed to add a review comment to paragraph %d", i)

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
        review_comments=review_comments,
    )
