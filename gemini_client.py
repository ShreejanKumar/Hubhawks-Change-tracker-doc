"""Gemini API integration: prompt assembly, structured-output calls, and the
id-validation retry/split fallback for unreliable chunk responses.
"""

from __future__ import annotations

import asyncio
import json
import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from app_secrets import get_secret
from chunking import IndexedParagraph, split_in_half
from config import (
    GEMINI_MANUAL_RETRY_ATTEMPTS,
    GEMINI_MODEL,
    GEMINI_REQUEST_TIMEOUT_MS,
    GEMINI_RETRY_ATTEMPTS,
    GEMINI_RETRY_INITIAL_DELAY_S,
    GEMINI_RETRY_MAX_DELAY_S,
    GEMINI_RETRYABLE_HTTP_STATUS_CODES,
    GEMINI_SEED,
    GEMINI_THINKING_LEVEL,
)

logger = logging.getLogger(__name__)


class ParagraphCorrection(BaseModel):
    id: int
    corrected_text: str


def _is_fatal_api_error(exc: Exception) -> bool:
    """True for a Gemini API error that retrying cannot fix (bad API key,
    exhausted billing, a request the API itself rejects, etc.), as opposed
    to a transient one (rate limit, timeout, server hiccup) — those raise
    exception types other than APIError, or an APIError whose code is in
    the retryable list, and are not fatal."""
    return isinstance(exc, genai_errors.APIError) and exc.code not in GEMINI_RETRYABLE_HTTP_STATUS_CODES


SYSTEM_PREAMBLE = """You are a professional copyeditor performing proofreading only.

Rules you MUST follow:
- Only correct grammar, punctuation, spelling, and language consistency.
- Do NOT rewrite sentences, restructure content, or change the author's voice or style.
- Do NOT generate new content, or expand/shorten passages beyond fixing actual errors.
- Do NOT translate foreign-language words, names, quotations, or intentionally
  non-English text into English. Leave them unchanged unless they contain an
  obvious typographical error.
- Preserve the author's original wording wherever it is not actually incorrect.

Before changing any word, read the full sentence — and the surrounding
paragraphs you were given, where relevant — and confirm the change is
correct in that context. Never substitute a word purely because it matches a
spelling pattern or dictionary entry: a substitution that is technically a
valid English word but wrong or archaic in context (for example swapping
"filter" for "philtre", or "draft" for "draught" when the sense is a written
draft) is a worse error than leaving the original word alone.

Actively check for, and correct, all of the following. These are real
errors, not stylistic choices, and are in scope even where they require
reading more than one word at a time:
- Missing, doubled, or repeated words (e.g. "the the", "and and").
- Subject-verb agreement and verb tense errors, including a verb tense that
  is inconsistent with the tense used in the surrounding sentences of the
  same scene.
- Missing or incorrect punctuation around direct address and vocatives
  (e.g. "Good morning Sanjay" needs a comma: "Good morning, Sanjay").
- Inconsistent quotation marks, spacing around punctuation, and misplaced or
  missing commas.
- Incomplete or malformed sentences, and incorrect contractions
  (e.g. "dint" for "didn't").
- Unnatural or incorrect idioms, prepositions, and fixed expressions (e.g.
  "did not leave the sight of him" should read "did not let him out of his
  sight"). Correct only the broken phrase itself, not the rest of the sentence.
- Redundant phrasing within a sentence, such as an unnecessary repeated verb
  or noun construction (e.g. "tried to lift the box and tried to fit it in"
  can drop the second "tried to"). Tighten only the specific redundant
  words — do not otherwise rephrase the sentence.
- Capitalization, hyphenation, and spelling of a given term or name kept
  consistent every time it recurs across the paragraphs you were given in
  this request (e.g. if "MOSSAD" should read "Mossad", correct every
  occurrence you see, not just the first).
- The {variant} English convention applied consistently and correctly to
  every word, including less common vocabulary — but only to words that are
  genuinely spelling variants of each other in the sense used; never change
  a word into a different word that happens to be spelled similarly.
- Quote-mark style: keep the style the manuscript already uses. Look at the
  dialogue and quotations across all the paragraphs you were given, see
  whether single (‘ ’) or double (“ ”) quote marks are used for them most
  often, and make any primary quotation that uses the other style match
  that majority; a quote within a quote uses the other style. Never convert
  the manuscript's prevailing style to a different one. Only if the
  paragraphs give no clear majority, use the {variant} English default:
  single quote marks for British English, double for American English.
- Placement of a period or comma against the closing quote mark, per the
  {variant} English convention. Under British English, when a complete
  sentence is quoted (including all dialogue), the period or comma goes
  inside, before the closing quote mark (‘Stay there.’); when only a phrase
  or part of a sentence is quoted, it goes outside, after the closing quote
  mark (she called it ‘a ruse’.). Under American English, a period or comma
  always goes inside, before the closing quote mark. Under both, dialogue
  followed by a dialogue tag ends with a comma inside the closing quote
  mark, never after it: ‘Stay there for a while,’ the tutor said — not
  ‘Stay there for a while’, the tutor said.
- Irregular verbs that take a different preterite/past-participle form
  between the two variants: under British English, prefer the traditional
  "-t" form (e.g. dreamt, learnt, burnt, spelt, spilt, leapt, smelt, knelt)
  rather than the "-ed" form; under American English, use the standard
  "-ed" form instead (dreamed, learned, burned, spelled, spilled, leaped,
  smelled, kneeled).

For all of the above — spelling variant, quote-mark style and placement,
and preterite verb forms — the rules as instructed here are authoritative
and must be followed regardless of anything the STYLE GUIDE below says or
implies about language convention (for example a stated preference for
"British spellings" or for single quotation marks): these are matters of
grammar/language convention, decided by which variant the user selected
(and, for quote-mark style, by the manuscript's own prevailing style),
never by the style guide. The STYLE GUIDE
governs everything outside of that — numbers, dates, citation and
footnote style, capitalization of specific terms, abbreviations, italics
conventions, hyphenation, and word-choice/usage preferences — apply it
fully for those regardless of variant.

You are returning plain text, which has no way to represent italics directly.
Where the style guide requires italics (for example the title of a book,
poem, film, album, or similar complete work) and the text you were given is
not already marked, indicate it by wrapping ONLY that exact span in a single
pair of asterisks, e.g. *In Memoriam A.H.H.* — the system converts this
marker into real italic formatting and removes the asterisks themselves, so
they must never remain as literal characters in your output. Reserve
asterisks exclusively for this purpose: never use them for bold, emphasis,
or any other markdown, never nest a pair inside another, and never leave one
unmatched.

STYLE GUIDE:
{style_guide}

You will receive a JSON array of paragraphs, each with an "id" and "text",
in their original document order. Use the surrounding paragraphs as context
for tense and terminology consistency, but return corrections for each
paragraph independently. Return a JSON array with EXACTLY one corrected
entry per input paragraph, in any order, each with the same "id" and a
"corrected_text" field. If a paragraph needs no changes, return its "text"
unchanged as "corrected_text". Never omit, merge, or split paragraphs — the
set of ids in your response must exactly match the set of ids you were
given."""


def new_client() -> genai.Client:
    """A fresh client for the caller to use for the lifetime of one
    asyncio.run() call, then close via `await client.aio.aclose()`.

    Deliberately NOT cached/reused across calls (unlike this module's other
    Gemini/GCS/Firestore-style singletons elsewhere in the app): genai.Client
    eagerly constructs an httpx.AsyncClient at __init__, and that async
    client's connection pool binds internal synchronization primitives to
    whichever asyncio event loop is running when it's first used. Reusing
    one cached instance across multiple asyncio.run() calls -- each of
    which spins up and tears down its own event loop -- risks binding it to
    a now-closed loop. Constructing a client is cheap (no network I/O), so
    a fresh one per run avoids that risk entirely."""
    api_key = get_secret("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set (env var or .streamlit/secrets.toml)")
    return genai.Client(api_key=api_key)


def _build_system_instruction(style_guide_text: str, variant: str, extra_instruction: str) -> str:
    instruction = SYSTEM_PREAMBLE.format(variant=variant, style_guide=style_guide_text)
    if extra_instruction:
        instruction += f"\n\n{extra_instruction}"
    return instruction


async def _call_gemini(
    client: genai.Client,
    paragraphs: list[IndexedParagraph],
    style_guide_text: str,
    variant: str,
    extra_instruction: str,
) -> dict[int, str]:
    system_instruction = _build_system_instruction(style_guide_text, variant, extra_instruction)
    payload = json.dumps([{"id": p.id, "text": p.text} for p in paragraphs])

    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=payload,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=list[ParagraphCorrection],
            thinking_config=types.ThinkingConfig(thinking_level=GEMINI_THINKING_LEVEL),
            seed=GEMINI_SEED,
            http_options=types.HttpOptions(
                timeout=GEMINI_REQUEST_TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(
                    attempts=GEMINI_RETRY_ATTEMPTS,
                    initial_delay=GEMINI_RETRY_INITIAL_DELAY_S,
                    max_delay=GEMINI_RETRY_MAX_DELAY_S,
                    http_status_codes=GEMINI_RETRYABLE_HTTP_STATUS_CODES,
                ),
            ),
        ),
    )

    corrections: list[ParagraphCorrection] = response.parsed or []
    if not corrections:
        # A blocked or truncated reply parses to nothing and is otherwise
        # indistinguishable from an id mismatch in the logs; record why so a
        # "could not be checked" paragraph can be diagnosed after the run.
        candidate = response.candidates[0] if response.candidates else None
        logger.warning(
            "Gemini returned no usable corrections for paragraph ids %d-%d: "
            "finish_reason=%s, prompt_feedback=%s, text=%.300r",
            paragraphs[0].id, paragraphs[-1].id,
            candidate.finish_reason if candidate else None,
            response.prompt_feedback, response.text if candidate else None,
        )
    return {c.id: c.corrected_text for c in corrections}


async def _call_gemini_with_retries(
    client: genai.Client,
    paragraphs: list[IndexedParagraph],
    style_guide_text: str,
    variant: str,
    extra_instruction: str,
) -> dict[int, str]:
    """_call_gemini's own http_options.retry_options only retries specific
    HTTP status codes. A timeout or connection reset raises a different
    exception and would otherwise get exactly one attempt before the whole
    paragraph range falls through to the split/give-up logic below. Retry
    those too, so a single transient blip doesn't need a full split-and-retry
    detour (or, at the single-paragraph leaf, doesn't mean giving up outright).

    A fatal API error (bad key, exhausted billing, etc.) is the opposite
    case: it will fail identically on every attempt, so it's raised
    immediately on the first attempt rather than burning the retry budget."""
    for attempt in range(GEMINI_MANUAL_RETRY_ATTEMPTS):
        try:
            return await _call_gemini(client, paragraphs, style_guide_text, variant, extra_instruction)
        except Exception as exc:
            if _is_fatal_api_error(exc):
                logger.error(
                    "Gemini call hit a non-retryable API error (%s); failing "
                    "immediately instead of retrying", exc,
                )
                raise
            if attempt == GEMINI_MANUAL_RETRY_ATTEMPTS - 1:
                raise
            ids = {p.id for p in paragraphs}
            logger.warning(
                "Gemini call failed (attempt %d/%d) for paragraph ids %d-%d; retrying",
                attempt + 1, GEMINI_MANUAL_RETRY_ATTEMPTS, min(ids), max(ids),
                exc_info=True,
            )
            await asyncio.sleep(GEMINI_RETRY_INITIAL_DELAY_S * (attempt + 1))


async def correct_paragraphs(
    client: genai.Client,
    paragraphs: list[IndexedParagraph],
    style_guide_text: str,
    variant: str,
    extra_instruction: str = "",
) -> tuple[dict[int, str], set[int]]:
    """Correct a list of paragraphs via Gemini, validating the response's
    id-set matches exactly what was sent. On a transient error or id
    mismatch, recursively halves the chunk and retries, down to
    per-paragraph calls. A paragraph that still fails alone falls back to
    its original text unchanged rather than being silently dropped, but its
    id is reported in the second return value so callers can tell "left
    unchanged because nothing needed fixing" apart from "left unchanged
    because Gemini never returned a usable correction" — the two look
    identical in the output docx otherwise.

    The recursive left/right halves are awaited sequentially rather than
    concurrently: this fallback path only runs after something has already
    gone wrong, so it's rare, and keeping it sequential keeps the maximum
    number of concurrent Gemini calls in flight bounded by whatever the
    caller's own concurrency limit is (e.g. main.py's per-chunk semaphore),
    rather than multiplying it out during retries.

    A fatal (non-retryable) API error is deliberately NOT caught here: it
    propagates straight out, skipping the halve-and-retry dance entirely,
    so a systemic failure (exhausted billing, revoked key, etc.) stops the
    whole run on the first paragraph it hits instead of grinding through
    every remaining paragraph/chunk in the document repeating the same
    doomed call.
    """
    if not paragraphs:
        return {}, set()

    expected_ids = {p.id for p in paragraphs}
    try:
        result = await _call_gemini_with_retries(client, paragraphs, style_guide_text, variant, extra_instruction)
    except Exception as exc:
        if _is_fatal_api_error(exc):
            raise
        logger.exception(
            "Gemini call failed for paragraph ids %d-%d (%d paragraphs)",
            min(expected_ids), max(expected_ids), len(paragraphs),
        )
        result = {}

    if set(result.keys()) == expected_ids:
        return result, set()
    if result:
        logger.warning(
            "Gemini id mismatch for paragraph ids %d-%d: missing %s, unexpected %s",
            min(expected_ids), max(expected_ids),
            sorted(expected_ids - result.keys()), sorted(result.keys() - expected_ids),
        )

    if len(paragraphs) == 1:
        pid = paragraphs[0].id
        logger.error("Giving up on paragraph %d after exhausting retries; left unchanged", pid)
        return {pid: paragraphs[0].text}, {pid}

    left, right = split_in_half(paragraphs)
    merged: dict[int, str] = {}
    failed: set[int] = set()
    left_result, left_failed = await correct_paragraphs(client, left, style_guide_text, variant, extra_instruction)
    right_result, right_failed = await correct_paragraphs(client, right, style_guide_text, variant, extra_instruction)
    merged.update(left_result)
    merged.update(right_result)
    failed |= left_failed | right_failed
    return merged, failed
