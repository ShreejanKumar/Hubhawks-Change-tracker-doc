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
- Follow the {variant} English convention consistently in all suggestions.
- Apply the style guide below wherever it is relevant (numbers, capitalization,
  punctuation, abbreviations, italics conventions, etc.).

STYLE GUIDE:
{style_guide}

You will receive a JSON array of paragraphs, each with an "id" and "text".
Return a JSON array with EXACTLY one corrected entry per input paragraph, in
any order, each with the same "id" and a "corrected_text" field. If a
paragraph needs no changes, return its "text" unchanged as "corrected_text".
Never omit, merge, or split paragraphs — the set of ids in your response must
exactly match the set of ids you were given."""


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
            thinking_config=types.ThinkingConfig(thinking_budget=0),
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
