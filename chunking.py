"""Groups paragraphs into LLM-call-sized chunks, in document order, never
splitting a paragraph across two calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import (
    CHARS_PER_TOKEN_ESTIMATE,
    INPUT_BUDGET_TOKENS_PER_CHUNK,
    MAX_PARAGRAPHS_PER_CHUNK,
    SINGLE_PARAGRAPH_HARD_CAP_TOKENS,
)


@dataclass
class IndexedParagraph:
    id: int
    text: str


class ParagraphTooLargeError(Exception):
    def __init__(self, paragraph_id: int, token_estimate: int):
        self.paragraph_id = paragraph_id
        self.token_estimate = token_estimate
        super().__init__(
            f"Paragraph {paragraph_id} is ~{token_estimate} tokens, exceeding the "
            f"hard cap of {SINGLE_PARAGRAPH_HARD_CAP_TOKENS}. It can't be processed "
            "as a single unit without splitting mid-paragraph, which this tool "
            "deliberately does not support."
        )


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN_ESTIMATE)


def build_chunks(paragraphs: list[IndexedParagraph]) -> list[list[IndexedParagraph]]:
    """Greedy bin-pack paragraphs into chunks bounded by token budget and
    paragraph count. Empty paragraphs (nothing to edit) are dropped here and
    never sent anywhere.
    """
    chunks: list[list[IndexedParagraph]] = []
    current: list[IndexedParagraph] = []
    current_tokens = 0

    for para in paragraphs:
        if not para.text:
            continue

        tokens = estimate_tokens(para.text)
        if tokens > SINGLE_PARAGRAPH_HARD_CAP_TOKENS:
            raise ParagraphTooLargeError(para.id, tokens)

        would_exceed = (
            current_tokens + tokens > INPUT_BUDGET_TOKENS_PER_CHUNK
            or len(current) + 1 > MAX_PARAGRAPHS_PER_CHUNK
        )
        if current and would_exceed:
            chunks.append(current)
            current = []
            current_tokens = 0

        current.append(para)
        current_tokens += tokens

    if current:
        chunks.append(current)

    return chunks


def split_in_half(
    chunk: list[IndexedParagraph],
) -> tuple[list[IndexedParagraph], list[IndexedParagraph]]:
    """Used by the Gemini retry path when a chunk's response doesn't match
    the paragraphs sent — recursively halves down to per-paragraph calls.
    """
    mid = max(1, len(chunk) // 2)
    return chunk[:mid], chunk[mid:]
