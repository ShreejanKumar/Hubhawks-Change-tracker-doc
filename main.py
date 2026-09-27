"""Orchestrates the full pipeline: extract -> chunk -> Gemini ->
guardrail -> track-changes injection -> save.
"""

from __future__ import annotations

import asyncio
import io
import logging
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
from config import (
    GEMINI_MAX_CONCURRENT_CHUNKS,
    MAX_CHANGED_TOKEN_RATIO,
    MIN_TOKENS_FOR_CHANGE_RATIO_GUARDRAIL,
)

logger = logging.getLogger(__name__)

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


async def _correct_all_chunks(
    chunks: list[list[chunking.IndexedParagraph]],
    style_guide_text: str,
    variant: str,
    progress_callback: Optional[ProgressCallback],
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
        async with semaphore:
            chunk_result, chunk_failed = await gemini_client.correct_paragraphs(client, chunk, style_guide_text, variant)
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

    corrected_by_id, failed_ids = asyncio.run(
        _correct_all_chunks(chunks, style_guide_text, variant, progress_callback)
    )

    revision_date = datetime.now(timezone.utc).isoformat()
    id_gen = tc.make_id_counter(document)

    edited_count = 0
    flagged: list[FlaggedParagraph] = []

    for i, paragraph in enumerate(all_paragraphs):
        extraction = extractions[i]
        if not extraction.text or i not in corrected_by_id:
            continue

        try:
            raw_corrected_text = _restore_edge_whitespace(
                extraction.text, tc.sanitize_for_xml(corrected_by_id[i])
            )
            corrected_text, italic_spans = tc.strip_italic_markers(raw_corrected_text)
            if corrected_text == extraction.text and not italic_spans:
                continue

            ratio = tc.changed_token_ratio(extraction, corrected_text)
            guardrail_applies = tc.token_count(extraction.text) >= MIN_TOKENS_FOR_CHANGE_RATIO_GUARDRAIL
            if guardrail_applies and ratio > MAX_CHANGED_TOKEN_RATIO:
                retry_result, retry_failed = asyncio.run(
                    gemini_client.correct_paragraphs_standalone(
                        [chunking.IndexedParagraph(id=i, text=extraction.text)],
                        style_guide_text,
                        variant,
                        extra_instruction=GUARDRAIL_RETRY_INSTRUCTION,
                    )
                )
                failed_ids |= retry_failed
                raw_retried_text = _restore_edge_whitespace(
                    extraction.text,
                    tc.sanitize_for_xml(retry_result.get(i, raw_corrected_text)),
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
        except Exception:
            # A single paragraph's corrected text failing to apply (e.g. an
            # unexpected XML-incompatible edge case) must never take down the
            # whole run and lose every other paragraph's already-fetched
            # corrections — isolate it as a failure and keep going.
            logger.exception("Failed to apply track changes to paragraph %d; left unchanged", i)
            failed_ids.add(i)

    out_stream = io.BytesIO()
    document.save(out_stream)

    return ProcessResult(
        docx_bytes=out_stream.getvalue(),
        total_paragraphs=len(all_paragraphs),
        edited_paragraphs=edited_count,
        flagged_paragraphs=flagged,
        failed_paragraphs=sorted(failed_ids),
    )
