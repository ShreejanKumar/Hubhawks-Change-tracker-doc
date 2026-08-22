"""Gemini API integration: prompt assembly, structured-output calls, and the
id-validation retry/split fallback for unreliable chunk responses.
"""

from __future__ import annotations

import json

import streamlit as st
from google import genai
from google.genai import types
from pydantic import BaseModel

from chunking import IndexedParagraph, split_in_half
from config import GEMINI_MODEL


class ParagraphCorrection(BaseModel):
    id: int
    corrected_text: str


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

Apply the style guide below wherever it is relevant (numbers, capitalization,
punctuation, abbreviations, italics conventions, etc.).

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


@st.cache_resource
def _client() -> genai.Client:
    return genai.Client(api_key=st.secrets["GEMINI_API_KEY"])


def _build_system_instruction(style_guide_text: str, variant: str, extra_instruction: str) -> str:
    instruction = SYSTEM_PREAMBLE.format(variant=variant, style_guide=style_guide_text)
    if extra_instruction:
        instruction += f"\n\n{extra_instruction}"
    return instruction


def _call_gemini(
    paragraphs: list[IndexedParagraph],
    style_guide_text: str,
    variant: str,
    extra_instruction: str,
) -> dict[int, str]:
    client = _client()
    system_instruction = _build_system_instruction(style_guide_text, variant, extra_instruction)
    payload = json.dumps([{"id": p.id, "text": p.text} for p in paragraphs])

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=payload,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=list[ParagraphCorrection],
            thinking_config=types.ThinkingConfig(thinking_budget=-1),
            temperature=0,
        ),
    )

    corrections: list[ParagraphCorrection] = response.parsed or []
    return {c.id: c.corrected_text for c in corrections}


def correct_paragraphs(
    paragraphs: list[IndexedParagraph],
    style_guide_text: str,
    variant: str,
    extra_instruction: str = "",
) -> dict[int, str]:
    """Correct a list of paragraphs via Gemini, validating the response's
    id-set matches exactly what was sent. On any mismatch or error,
    recursively halves the chunk and retries, down to per-paragraph calls.
    A paragraph that still fails alone falls back to its original text
    unchanged, rather than being silently dropped.
    """
    if not paragraphs:
        return {}

    expected_ids = {p.id for p in paragraphs}
    try:
        result = _call_gemini(paragraphs, style_guide_text, variant, extra_instruction)
    except Exception:
        result = {}

    if set(result.keys()) == expected_ids:
        return result

    if len(paragraphs) == 1:
        return {paragraphs[0].id: paragraphs[0].text}

    left, right = split_in_half(paragraphs)
    merged: dict[int, str] = {}
    merged.update(correct_paragraphs(left, style_guide_text, variant, extra_instruction))
    merged.update(correct_paragraphs(right, style_guide_text, variant, extra_instruction))
    return merged
