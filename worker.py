"""Cloud Run Job entry point: process one queued run end to end.

Runs used to execute inside the Streamlit page's script. A long manuscript
(20-30 minutes) was then lost whenever the browser's websocket reconnected
mid-run: Streamlit reran the script, silently abandoning the run, and the
editor saw the page "refresh" with no error. Here the run lives in its own
container, independent of any browser session, and reports every state
change to the run's Firestore doc, which is all the app reads.

    RUN_ID=<id> python worker.py        (how the job is started)
    python worker.py --run-id <id>      (local testing)
"""

import argparse
import logging
import os
import sys
import threading

from google.genai import errors as genai_errors

import history_store
import main
from chunking import ParagraphTooLargeError
from config import WORKER_HEARTBEAT_INTERVAL_S

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("worker")


def _heartbeat_loop(run_id: str, stop: threading.Event) -> None:
    """A single chunk call can take minutes, so chunk progress alone can't
    show the run is alive. Failures here are logged, never fatal: a missed
    heartbeat only risks a false "stale" warning, not a lost run."""
    while not stop.wait(WORKER_HEARTBEAT_INTERVAL_S):
        try:
            history_store.heartbeat(run_id)
        except history_store.RunDeletedError:
            return
        except Exception:
            logger.exception("Heartbeat failed for run %s", run_id)


def process_run(run_id: str) -> int:
    run = history_store.get_run(run_id)
    if run is None:
        logger.warning("Run %s no longer exists (deleted before it started)", run_id)
        return 0
    if history_store.run_status(run) != history_store.STATUS_QUEUED:
        # Max retries is 0, so this would only be a manual re-execution.
        # Never reprocess (and re-bill) a run that already started.
        logger.warning("Run %s is %s, not queued; skipping", run_id, history_store.run_status(run))
        return 0

    history_store.mark_running(run_id)
    stop = threading.Event()
    threading.Thread(target=_heartbeat_loop, args=(run_id, stop), daemon=True).start()

    def on_progress(done: int, total: int) -> None:
        try:
            history_store.update_progress(run_id, done, total)
        except Exception:
            # Progress display only; never let it abort the run's real work.
            logger.exception("Progress update failed for run %s", run_id)

    try:
        docx_bytes = history_store.get_docx_bytes(run["input_gcs_path"])
        result = main.process_document(
            docx_bytes=docx_bytes,
            author=run["editor"],
            variant=run["variant"],
            style_guide_key=run["style_guide_key"],
            progress_callback=on_progress,
        )
        history_store.complete_run(run_id, run["gcs_path"], result)
        logger.info(
            "Run %s done: %d/%d paragraphs edited, %d failed, %d flagged, %d review comments",
            run_id, result.edited_paragraphs, result.total_paragraphs,
            len(result.failed_paragraphs), len(result.flagged_paragraphs), result.review_comments,
        )
        return 0
    except history_store.RunDeletedError:
        logger.info("Run %s was deleted while processing; stopping", run_id)
        return 0
    except genai_errors.APIError as exc:
        logger.exception("Run %s stopped by a non-retryable Gemini API error", run_id)
        _fail(run_id, "gemini_api", f"{exc.code} {exc.status}: {exc.message or exc}")
        return 1
    except ParagraphTooLargeError as exc:
        logger.exception("Run %s has a paragraph too large to process", run_id)
        _fail(run_id, "paragraph_too_large", str(exc))
        return 1
    except Exception as exc:
        logger.exception("Run %s failed", run_id)
        _fail(run_id, "other", f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        stop.set()


def _fail(run_id: str, kind: str, message: str) -> None:
    try:
        history_store.fail_run(run_id, kind, message)
    except history_store.RunDeletedError:
        pass
    except Exception:
        # Can't even record the failure: the app's stale-heartbeat check
        # will still surface it to the editor after WORKER_STALE_AFTER_S.
        logger.exception("Could not record failure for run %s", run_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=os.environ.get("RUN_ID"))
    run_id = parser.parse_args().run_id
    if not run_id:
        sys.exit("No run id: set RUN_ID or pass --run-id")
    sys.exit(process_run(run_id))
