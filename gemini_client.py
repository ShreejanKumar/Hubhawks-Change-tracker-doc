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


class ReviewFlag(BaseModel):
    # "tense", "fragment", "word_choice", "awkward" or "repetition"
    category: str
    # The exact words in corrected_text the flag is about.
    quote: str
    # The issue and suggested fix, addressed to the editor.
    comment: str


class ParagraphCorrection(BaseModel):
    id: int
    corrected_text: str
    flags: list[ReviewFlag]


def _is_fatal_api_error(exc: Exception) -> bool:
    """True for a Gemini API error that retrying cannot fix (bad API key,
    exhausted billing, a request the API itself rejects, etc.), as opposed
    to a transient one (rate limit, timeout, server hiccup) — those raise
    exception types other than APIError, or an APIError whose code is in
    the retryable list, and are not fatal."""
    return isinstance(exc, genai_errors.APIError) and exc.code not in GEMINI_RETRYABLE_HTTP_STATUS_CODES


SYSTEM_PREAMBLE = """You are a professional copyeditor performing a light copyedit and proofread.

Rules you MUST follow:
- Correct grammar, punctuation, spelling, and language consistency, and
  repair sentences that are grammatically incorrect, incomplete, or unclear
  because of a broken construction (see below).
- Do NOT rewrite sentences that are already correct.
- Do NOT generate new content, or expand/shorten passages beyond fixing actual errors.
- Do NOT translate foreign-language words, names, quotations, or intentionally
  non-English text into English, and never replace a non-English word with
  its nearest English equivalent. Leave them unchanged unless they contain an
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
- Subject-verb agreement and verb tense errors.
- Tense consistency within a scene. Work out the dominant narrative tense
  of the passage and find any unexplained shift from past to present or
  present to past within a continuous scene or sequence of actions: "He
  began counting eighteen steps. Then he accurately aligns his pace and
  takes a short jump." shifts into the present ("aligned", "took"). A
  shift is legitimate, and must be left alone, in direct speech or
  dialogue, internal thought, a flashback or move to another time, a
  general truth or factual statement, or a deliberate stylistic device the
  context clearly supports.
- Commas, applied consistently in every paragraph, not only where the
  error is obvious. Add a missing comma, and fix a misplaced one, in each
  of these cases:
  - Between items in a list of three or more, always with the serial
    (Oxford) comma before the final "and" or "or": "apples, oranges, and
    bananas".
  - Before "and", "but", "or", "nor", "for", "so" or "yet" joining two
    independent clauses: "She wanted to leave, but he asked her to stay."
    Not when the second part has no subject of its own ("She stood up and
    left.").
  - After an introductory clause, phrase, word or transitional adverb,
    including time and conditional clauses and participial phrases: "When
    the meeting ended, everyone left.", "After dinner, we went for a
    walk.", "However, she refused.", "Meanwhile, everyone waited.", "If you
    need help, call me.", "Walking down the street, she saw him."
  - Around non-restrictive clauses and appositives, both sides unless the
    element ends the sentence: "My brother, who lives in Delhi, is
    visiting.", "Riya, my oldest friend, called me." Never add commas
    around restrictive (essential) information ("The man who called is
    here.").
  - Around a name or term of direct address (vocative): "Riya, come
    here.", "Thank you, John.", "Good morning, Sanjay."
  - Between coordinate adjectives, those that would read naturally joined
    by "and": "a long, difficult journey" (but "a bright red car").
  - Before a contrasting element: "She wanted tea, not coffee."
  - To introduce a quotation ("She said, “I don't know.”") and with
    dialogue tags ("“Come here,” she whispered.").
  - Around parenthetical or interrupting expressions and absolute phrases:
    "He was, however, already there.", "She was, in fact, right.", "Her
    hands trembling, she opened the letter."
  - Where a sentence would otherwise be misread: "Let's eat, Grandma."
  - In place names, with a closing comma when the sentence continues:
    "Delhi, India, is a major city."
  - Around degrees, credentials and non-restrictive titles after a name:
    "Ravi Sharma, PhD, spoke at the event.", "Meera Kapoor,
    Editor-in-Chief, approved the manuscript."
  Commas in dates and in numbers of a thousand or more follow the style
  guide. Keep a comma the author used for stylistic separation where it is
  grammatically correct ("And then, suddenly, everything changed."); do
  not remove such commas or add new ones for rhythm alone.
- Inconsistent quotation marks and spacing around punctuation.
- Incomplete or malformed sentences, and incorrect contractions
  (e.g. "dint" for "didn't"). A sentence that is grammatically incomplete,
  ungrammatical in its construction, or unclear because its construction
  is broken must be repaired, with the smallest change that makes it
  correct and clear, keeping the author's words, word order and meaning
  wherever possible. That includes a sentence fragment: a sentence with no
  finite verb, made only of a noun phrase, an adjectival, participial or
  prepositional phrase, or an incomplete subordinate clause, where a
  complete sentence is expected ("Wind caressing her curly, short hair
  like a lullaby would caress her thoughts." has only the participle
  "caressing"). Fragments are legitimate, and must be left alone, in
  dialogue, headings and titles, lists, captions, and deliberate literary
  fragments the context clearly supports.
  For example: "It is not important what your background." → "It is not
  important what your background is."; "I will strongly suggest every
  reader to try it" → "I strongly suggest that every reader try it", and
  "What all benefits does a reader get from this?" → "What benefits does a
  reader get from this?". Do not leave a partial fix: if you touch a
  sentence, the result must be fully grammatical.
- Excessive or stacked punctuation: reduce "!!", "!!!", "??", "?!" and
  similar to a single mark (keep "?!" only inside dialogue where it
  expresses an incredulous question), and a stray ".." to a single full
  stop, or to an ellipsis where a trailing-off is clearly intended.
- Wrong word class or wrong word: a word whose part of speech or meaning
  does not fit its position in the sentence, even though it is a valid
  word on its own. In "fight just to get her — a situation, a
  prospective, a lover", "prospective" is an adjective where the list
  needs a noun like "situation" and "lover" ("prospect", "prospective
  match", "prospective suitor"). Tell apart a grammatical error (wrong
  word class or usage) from a semantic one (grammatical but apparently
  not the intended meaning).
- Articles: a wrong, missing or extra article, checked before every noun
  in every paragraph, not only where the error is obvious. A singular
  countable noun almost always needs "a", "an" or "the" ("She is writer"
  → "She is a writer").
  - "A"/"an" for one non-specific thing: a first mention ("I saw a dog
    outside."), an unspecified one ("I need a pen."), a member of a
    category or a profession ("He is a doctor.", "She became an editor."),
    after "there is/was" ("There was an accident."), in rates and
    frequencies ("twice a day", "₹500 a month", "60 kilometres an hour"),
    and in exclamations ("What a shame!").
  - "A" or "an" by sound, not spelling: "a university", "a European", "a
    one-time event", "a useful idea", but "an hour", "an honest person",
    "an MBA", "an umbrella" ("a hour" → "an hour").
  - "The" for a specific or identifiable one: already mentioned ("I saw a
    dog. The dog was barking."), known from context ("Please close the
    door."), unique ("the sun", "the Earth", "the internet", "the
    President of India"), made specific by a phrase or clause ("The book
    on the table is mine."), with superlatives and ordinals ("the best
    book", "the first chapter", "the last page"), instruments being played
    ("She plays the piano."), a species as a whole ("The tiger is
    endangered."), and adjectives for a group of people ("the rich", "the
    elderly").
  - "The" with proper names that conventionally take it: rivers, seas,
    oceans, canals, mountain ranges and island groups ("the Ganges", "the
    Arabian Sea", "the Suez Canal", "the Himalayas", "the Maldives"),
    countries with plural names or "Republic", "Kingdom", "States" or
    "Emirates" ("the United States", "the Netherlands"), and organisations
    and institutions that use it ("the United Nations", "the World Health
    Organization"). A title that has "The" as part of its name keeps the
    capital ("The Times").
  - No article for a general sense: plural or uncountable nouns and
    abstract ideas used generally ("Books are expensive.", "Water is
    essential.", "Freedom is important."; "trying to make the society
    happy" → "trying to make society happy"; likewise nature, humanity,
    mankind), most proper names ("India", "Delhi", "Mount Everest", never
    "the India"), languages and school subjects ("She speaks English.",
    "He studied history."), meals ("We had breakfast."), sports ("He plays
    cricket."), most diseases ("He has diabetes", but "the flu", "the
    measles"), transport after "by" ("by train"), and institutions used for
    their purpose ("She is at school.", "He is in prison.", "He is at
    university").
  Keep "the" wherever the noun is made specific ("the society she grew up
  in", "The dinner we had yesterday was excellent.", "She went to the
  school to meet the principal."). Under British English a person goes "to
  hospital" as a patient; under American English, "to the hospital".
- Unnatural or incorrect idioms, prepositions, and fixed expressions (e.g.
  "did not leave the sight of him" should read "did not let him out of his
  sight"). Correct only the broken phrase itself, not the rest of the sentence.
- Non-standard English usage, including constructions common in South
  Asian English, corrected to standard English every time it occurs, with
  the smallest change: "since long" → "for a long time", "discuss about" →
  "discuss", "cope up with" → "cope with", "return back" → "return",
  "revert back to you" → "get back to you", "one of my friend" → "one of
  my friends", "prepone" → "bring forward", "out of station" → "out of
  town", and similar.
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
  a word into a different word that happens to be spelled similarly. Check
  every word of every paragraph, not only the obvious ones: the convention
  covers far more than -ise/-ize and -our/-or. Under British English that
  includes -ise/-isation (utilise, organisation, realise, criticise,
  specialise, emphasise), -yse (analyse, paralyse), -our (behaviour,
  favourite), -re (centre, theatre, metre as a unit), -ence (defence,
  offence, licence as a noun, practice as a noun but practise as a verb),
  doubled consonants (travelled, cancelled, modelling, counsellor), -ogue
  (catalogue, dialogue), ae/oe (anaemia, manoeuvre), and word forms such
  as towards, afterwards, judgement, ageing, fulfil, enrol, skilful,
  programme (but computer program), grey, jewellery, aluminium, mum. Under
  American English apply the reverse of each.
- Quote marks: never convert quotation marks between single and double;
  the system sets one style for the whole manuscript after you. Always use
  typographic (curly) quote marks and apostrophes (‘ ’ “ ”), never straight
  ones (' "), and turn any straight ones in the text into the matching
  curly form.
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

Correct, or flag for the editor (detect, assess the context, then correct
only when confident). For tense shifts, sentence fragments and wrong
words, make the correction yourself only when you are highly confident:
there is exactly one reasonable fix and the intended meaning is clear from
the context. Leave the text unchanged and add a flag instead when more than
one correction is grammatically possible, the intended meaning or tense
cannot be established, the context you were given is not enough, or it
could be a deliberate literary or stylistic choice. Never guess: a wrong
"fix" is worse than a flag. The same applies to any sentence you can only
partly repair: never leave a half-fixed sentence; either make it fully
correct or leave it and flag it.

Two more kinds of problem are always flagged and never corrected, because
fixing them would mean rephrasing the author:
- "awkward": a sentence that is grammatical but so awkward, convoluted or
  ambiguous that a reader would stumble or misread it (a dangling
  modifier, a stacked or tangled clause, an unclear "it"/"this" referent).
  Not a sentence that merely could be phrased more elegantly.
- "repetition": the same sentence structure or opening used several times
  in close succession so that it becomes noticeable (e.g. three sentences
  in a row starting "She …"), or the same distinctive word repeated within
  a sentence or two. Flag it once, at the occurrence that makes it
  noticeable.

Do not flag a deliberate fragment in narration: a short fragment used for
emphasis, rhythm or suspense ("Until one day.", "Only him.", "Checkmate.",
"Just the right shade of lipstick."), the answer to a question the text
just asked, or a fragment in dialogue or internal thought. Flag a fragment
only where it reads as an error, such as a subordinate or participial
clause cut off from the sentence it belongs to.

Flag only what a professional copyeditor would raise as a query for the
author, at most two flags per paragraph (the most important ones). Each
flag has a "category" ("tense", "fragment", "word_choice", "awkward" or
"repetition"), a "quote" (the exact words of the problem in your
corrected_text, as short as identifies it: a word, phrase or one sentence)
and a "comment" for the editor stating the issue and a suggested fix, e.g.
"Shifts from past to present tense mid-scene; consider 'aligned … took'."
or "Wrong word class: 'prospective' is an adjective here; consider
'prospect' or 'prospective suitor'.". The suggested fix in a comment must
read as natural, idiomatic English that a native-speaking editor would
write: keep the author's content and as many of their words as possible,
reordering or adjusting them where needed so the sentence flows. For
"Running for the bus, her bag fell open.", suggest "As she ran for the
bus, her bag fell open.", not "Running for the bus, there was her bag,
which fell open.". Write quote and comment as plain
text, without asterisks or tildes. Do not flag what you corrected, and do
not flag anything else (spelling, punctuation, preferences of style).

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

Acronyms: follow the STYLE GUIDE's rule for an acronym's first mention.
Expand one only where you are certain of its expansion, from the manuscript
itself or because it is a widely known organisation or term; never guess an
expansion, and never expand an acronym the author coined without explaining
it. If you are told that an acronym already appeared earlier in the
manuscript, it is not a first mention: do not expand it.

You are returning plain text, which has no way to represent italics directly.
Where the style guide requires italics (for example the title of a book,
film, play, or similar complete work) and the text you were given is not
already marked, indicate it by wrapping ONLY that exact span in a single
pair of asterisks, e.g. *In Memoriam A.H.H.* — the system converts this
marker into real italic formatting and removes the asterisks themselves, so
they must never remain as literal characters in your output. Reserve
asterisks exclusively for this purpose: never use them for bold, emphasis,
or any other markdown, never nest a pair inside another, and never leave one
unmatched.

An uncommon non-English word (one the style guide does not list as common)
is different: wrap it in a pair of tildes instead, e.g. ~swayamvar~, at
EVERY appearance in the paragraphs you were given, including where it is
capitalised (~Kundali~). The system italicises only its first appearance in
the whole manuscript and keeps every later one in roman, so mark them all
and never decide which one is first yourself. Use tildes for nothing else.

STYLE GUIDE:
{style_guide}

You will receive a JSON array of paragraphs, each with an "id" and "text",
in their original document order. Use the surrounding paragraphs as context
for tense and terminology consistency, but return corrections for each
paragraph independently. Return a JSON array with EXACTLY one corrected
entry per input paragraph, in any order, each with the same "id" and a
"corrected_text" field, and a "flags" list (empty when there is nothing
to flag). If a paragraph needs no changes, return its "text"
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


def _generate_config(system_instruction: str, response_schema) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        response_mime_type="application/json",
        response_schema=response_schema,
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
    )


FOREIGN_TERMS_INSTRUCTION = """You are a copyeditor applying the STYLE GUIDE below.
You will receive a JSON array of words and short phrases that the author
set in italics somewhere in a manuscript, each with an example sentence.
Return a JSON array of exactly those entries (spelled as given) that are
an uncommon non-English word or phrase which the style guide says to
italicise at its first use only and keep in roman after that. Leave out
English words italicised for emphasis, titles of books, films and other
works, names of ships, Latin abbreviations such as et al., and any word
the style guide lists as common enough to need no italics. Return [] if
none qualify.

STYLE GUIDE:
{style_guide}"""


async def find_foreign_terms(
    client: genai.Client, candidates: dict[str, str], style_guide_text: str
) -> list[str]:
    """Which of the author's italicised words (candidate -> an example
    sentence) are uncommon non-English words. Gemini can't see the
    manuscript's formatting, so this is the only way it judges them."""
    payload = json.dumps([{"term": t, "example": ex} for t, ex in candidates.items()], ensure_ascii=False)
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=payload,
        config=_generate_config(FOREIGN_TERMS_INSTRUCTION.format(style_guide=style_guide_text), list[str]),
    )
    return [t for t in (response.parsed or []) if t in candidates]


async def _call_gemini(
    client: genai.Client,
    paragraphs: list[IndexedParagraph],
    style_guide_text: str,
    variant: str,
    extra_instruction: str,
) -> dict[int, ParagraphCorrection]:
    system_instruction = _build_system_instruction(style_guide_text, variant, extra_instruction)
    # ensure_ascii=False: with the default, Gemini saw "fiancé" as "fiancé"
    # and sometimes echoed a mangled escape back ("fiance9e").
    payload = json.dumps([{"id": p.id, "text": p.text} for p in paragraphs], ensure_ascii=False)

    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=payload,
        config=_generate_config(system_instruction, list[ParagraphCorrection]),
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
    return {c.id: c for c in corrections}


async def _call_gemini_with_retries(
    client: genai.Client,
    paragraphs: list[IndexedParagraph],
    style_guide_text: str,
    variant: str,
    extra_instruction: str,
) -> dict[int, ParagraphCorrection]:
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
    flags_out: dict[int, list[ReviewFlag]] | None = None,
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

    Gemini's review flags for each paragraph go into `flags_out` when the
    caller passes one.
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
        if flags_out is not None:
            flags_out.update({i: c.flags for i, c in result.items()})
        return {i: c.corrected_text for i, c in result.items()}, set()
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
    left_result, left_failed = await correct_paragraphs(client, left, style_guide_text, variant, extra_instruction, flags_out)
    right_result, right_failed = await correct_paragraphs(client, right, style_guide_text, variant, extra_instruction, flags_out)
    merged.update(left_result)
    merged.update(right_result)
    failed |= left_failed | right_failed
    return merged, failed
