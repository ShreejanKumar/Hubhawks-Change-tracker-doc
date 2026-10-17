"""GCS + Firestore backed storage for runs and their history.

A run's Firestore doc is also its job record. The Streamlit app creates it
(status "queued", input .docx uploaded to GCS) and starts the Cloud Run Job;
the worker (worker.py) moves it through "running" (with chunk progress and a
heartbeat) to "done" (corrected .docx in GCS, stats) or "failed" (error kind
and message). The app only ever reads that state, so a page refresh,
reconnect or redeploy can't lose a run. Docs written before background
processing existed have no status and are complete runs.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional
from uuid import uuid4

from google.api_core import exceptions as gapi_exceptions
from google.cloud import firestore, storage
from google.oauth2 import service_account

from app_secrets import get_secret
from config import GCP_PROJECT_ID, WORKER_STALE_AFTER_S
from main import ProcessResult

PROJECT_ID = GCP_PROJECT_ID
BUCKET_NAME = "opportune-baton-505607-c2-hubhawks-docs"
COLLECTION = "runs"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_LOCAL_KEY_PATH = Path(__file__).parent / "gcp-service-account.json"


class RunDeletedError(Exception):
    """The run's doc was deleted (from the history sidebar) while the worker
    was still processing it. The worker stops quietly rather than recreating it."""


# lru_cache rather than st.cache_resource: this module also runs in the Cloud
# Run worker, outside any Streamlit runtime.
@lru_cache(maxsize=1)
def credentials() -> Optional[service_account.Credentials]:
    """Resolve credentials for Streamlit Cloud (secrets) or local dev (key
    file next to this module). Falls back to None so the google-cloud
    libraries pick up Application Default Credentials on their own, which is
    how the Cloud Run worker authenticates (its attached service account)."""
    info = get_secret("GCP_SERVICE_ACCOUNT_JSON")
    if info:
        return service_account.Credentials.from_service_account_info(json.loads(info))
    if _LOCAL_KEY_PATH.exists():
        return service_account.Credentials.from_service_account_file(str(_LOCAL_KEY_PATH))
    return None


@lru_cache(maxsize=1)
def _storage_client() -> storage.Client:
    return storage.Client(project=PROJECT_ID, credentials=credentials())


@lru_cache(maxsize=1)
def _firestore_client() -> firestore.Client:
    return firestore.Client(project=PROJECT_ID, credentials=credentials())


def _doc(run_id: str):
    return _firestore_client().collection(COLLECTION).document(run_id)


def _bucket():
    return _storage_client().bucket(BUCKET_NAME)


# ------------------------------------------------------------- App side --


def create_run(
    *,
    source_filename: str,
    docx_bytes: bytes,
    editor: str,
    variant: str,
    style_guide_key: str,
    style_guide_label: str,
) -> str:
    """Uploads the input docx and writes a queued run doc. Returns the run id."""
    run_id = uuid4().hex
    output_filename = f"{Path(source_filename).stem}_tracked.docx"
    input_gcs_path = f"runs/{run_id}/input.docx"

    _bucket().blob(input_gcs_path).upload_from_string(docx_bytes, content_type=DOCX_CONTENT_TYPE)
    _doc(run_id).set(
        {
            "status": STATUS_QUEUED,
            "source_filename": source_filename,
            "output_filename": output_filename,
            "editor": editor,
            "variant": variant,
            "style_guide_key": style_guide_key,
            "style_guide_label": style_guide_label,
            "created_at": firestore.SERVER_TIMESTAMP,
            "heartbeat_at": firestore.SERVER_TIMESTAMP,
            "input_gcs_path": input_gcs_path,
            "gcs_path": f"runs/{run_id}/{output_filename}",
            "chunks_done": 0,
            "chunks_total": 0,
        }
    )
    return run_id


def fail_run(run_id: str, error_kind: str, error_message: str) -> None:
    """Also used by the app when the job can't even be started."""
    _update(run_id, {"status": STATUS_FAILED, "error_kind": error_kind, "error_message": error_message})


def run_status(run: dict) -> str:
    return run.get("status", STATUS_DONE)


def is_stale(run: dict) -> bool:
    """A queued/running run whose worker has stopped reporting in: the
    container died, or the job never started. Shown to the editor as failed."""
    if run_status(run) not in (STATUS_QUEUED, STATUS_RUNNING):
        return False
    heartbeat = run.get("heartbeat_at") or run.get("created_at")
    if heartbeat is None:  # server timestamp not resolved yet: just written
        return False
    return (datetime.now(timezone.utc) - heartbeat).total_seconds() > WORKER_STALE_AFTER_S


def list_runs(search: str = "", limit: int = 300) -> list[dict]:
    """Most recent runs first, optionally filtered by a case-insensitive
    substring match on filename or editor name."""
    query = (
        _firestore_client()
        .collection(COLLECTION)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    runs = []
    for snapshot in query.stream():
        data = snapshot.to_dict()
        data["run_id"] = snapshot.id
        runs.append(data)

    needle = search.strip().lower()
    if needle:
        runs = [
            r
            for r in runs
            if needle in r.get("source_filename", "").lower()
            or needle in r.get("editor", "").lower()
        ]
    return runs


def get_run(run_id: str) -> Optional[dict]:
    snapshot = _doc(run_id).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict()
    data["run_id"] = snapshot.id
    return data


def get_docx_bytes(gcs_path: str) -> bytes:
    return _bucket().blob(gcs_path).download_as_bytes()


def delete_run(run_id: str) -> None:
    """Deletes every object under the run's prefix (input and output) and its doc."""
    for blob in _storage_client().list_blobs(BUCKET_NAME, prefix=f"runs/{run_id}/"):
        blob.delete()
    _doc(run_id).delete()


# ---------------------------------------------------------- Worker side --


def _update(run_id: str, fields: dict) -> None:
    """update() (not set/merge) so a run deleted mid-processing is never
    recreated; every write doubles as a heartbeat."""
    try:
        _doc(run_id).update({**fields, "heartbeat_at": firestore.SERVER_TIMESTAMP})
    except gapi_exceptions.NotFound as exc:
        raise RunDeletedError(run_id) from exc


def mark_running(run_id: str) -> None:
    _update(run_id, {"status": STATUS_RUNNING, "started_at": firestore.SERVER_TIMESTAMP})


def heartbeat(run_id: str) -> None:
    _update(run_id, {})


def update_progress(run_id: str, done: int, total: int) -> None:
    _update(run_id, {"chunks_done": done, "chunks_total": total})


def complete_run(run_id: str, gcs_path: str, result: ProcessResult) -> None:
    _bucket().blob(gcs_path).upload_from_string(result.docx_bytes, content_type=DOCX_CONTENT_TYPE)
    _update(
        run_id,
        {
            "status": STATUS_DONE,
            "finished_at": firestore.SERVER_TIMESTAMP,
            "total_paragraphs": result.total_paragraphs,
            "edited_paragraphs": result.edited_paragraphs,
            "flagged_count": len(result.flagged_paragraphs),
            "flagged": [asdict(f) for f in result.flagged_paragraphs],
            "failed_paragraphs": result.failed_paragraphs,
            "review_comments": result.review_comments,
        },
    )
