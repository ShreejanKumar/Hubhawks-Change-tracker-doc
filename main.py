"""Orchestrates the full pipeline: extract -> chunk -> Gemini ->
guardrail -> track-changes injection -> save.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from docx import Document

import chunking
import docx_extract as de
import docx_track_changes as tc
import gemini_client
import style_guides
from config import MAX_CHANGED_TOKEN_RATIO

GUARDRAIL_RETRY_INSTRUCTION = (
    "Your previous correction changed too much text. Make ONLY essential "
    "grammar/spelling/punctuation corrections and preserve the original wording."
)


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

    corrected_by_id: dict[int, str] = {}
    failed_ids: set[int] = set()
    for chunk_num, chunk in enumerate(chunks):
        chunk_result, chunk_failed = gemini_client.correct_paragraphs(chunk, style_guide_text, variant)
        corrected_by_id.update(chunk_result)
        failed_ids |= chunk_failed
        if progress_callback:
            progress_callback(chunk_num + 1, max(len(chunks), 1))

    revision_date = datetime.now(timezone.utc).isoformat()
    id_gen = tc.make_id_counter(document)

    edited_count = 0
    flagged: list[FlaggedParagraph] = []

    for i, paragraph in enumerate(all_paragraphs):
        extraction = extractions[i]
        if not extraction.text or i not in corrected_by_id:
            continue

        raw_corrected_text = _restore_edge_whitespace(extraction.text, corrected_by_id[i])
        corrected_text, italic_spans = tc.strip_italic_markers(raw_corrected_text)
        if corrected_text == extraction.text and not italic_spans:
            continue

        ratio = tc.changed_token_ratio(extraction, corrected_text)
        if ratio > MAX_CHANGED_TOKEN_RATIO:
            retry_result, retry_failed = gemini_client.correct_paragraphs(
                [chunking.IndexedParagraph(id=i, text=extraction.text)],
                style_guide_text,
                variant,
                extra_instruction=GUARDRAIL_RETRY_INSTRUCTION,
            )
            failed_ids |= retry_failed
            raw_retried_text = _restore_edge_whitespace(
                extraction.text, retry_result.get(i, raw_corrected_text)
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
        edited_count += 1

    out_stream = io.BytesIO()
    document.save(out_stream)

    return ProcessResult(
        docx_bytes=out_stream.getvalue(),
        total_paragraphs=len(all_paragraphs),
        edited_paragraphs=edited_count,
        flagged_paragraphs=flagged,
        failed_paragraphs=sorted(failed_ids),
    )
