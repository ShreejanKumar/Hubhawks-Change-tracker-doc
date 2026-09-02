"""GCS + Firestore backed storage for run history.

Every completed proofreading run is written to GCS (the corrected .docx) and
Firestore (metadata: filenames, stats, flagged paragraphs) so it can be
browsed, searched, and re-downloaded later from the sidebar history panel.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from uuid import uuid4

import streamlit as st
from google.cloud import firestore, storage
from google.oauth2 import service_account

from main import ProcessResult

PROJECT_ID = "opportune-baton-505607-c2"
BUCKET_NAME = "opportune-baton-505607-c2-hubhawks-docs"
COLLECTION = "runs"

_LOCAL_KEY_PATH = Path(__file__).parent / "gcp-service-account.json"


@st.cache_resource
def _credentials() -> Optional[service_account.Credentials]:
    """Resolve credentials for local dev (key file next to this module) or
    Streamlit Cloud (secrets). Falls back to None so the google-cloud
    libraries can pick up Application Default Credentials on their own."""
    if "GCP_SERVICE_ACCOUNT_JSON" in st.secrets:
        info = json.loads(st.secrets["GCP_SERVICE_ACCOUNT_JSON"])
        return service_account.Credentials.from_service_account_info(info)
    if _LOCAL_KEY_PATH.exists():
        return service_account.Credentials.from_service_account_file(str(_LOCAL_KEY_PATH))
    return None


@st.cache_resource
def _storage_client() -> storage.Client:
    return storage.Client(project=PROJECT_ID, credentials=_credentials())


@st.cache_resource
def _firestore_client() -> firestore.Client:
    return firestore.Client(project=PROJECT_ID, credentials=_credentials())


def save_run(
    *,
    source_filename: str,
    output_filename: str,
    editor: str,
    variant: str,
    style_guide_key: str,
    style_guide_label: str,
    result: ProcessResult,
) -> str:
    """Uploads the corrected docx to GCS and writes a metadata doc to
    Firestore. Returns the new run id."""
    run_id = uuid4().hex
    gcs_path = f"runs/{run_id}/{output_filename}"

    bucket = _storage_client().bucket(BUCKET_NAME)
    bucket.blob(gcs_path).upload_from_string(
        result.docx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    _firestore_client().collection(COLLECTION).document(run_id).set(
        {
            "source_filename": source_filename,
            "output_filename": output_filename,
            "editor": editor,
            "variant": variant,
            "style_guide_key": style_guide_key,
            "style_guide_label": style_guide_label,
            "created_at": firestore.SERVER_TIMESTAMP,
            "total_paragraphs": result.total_paragraphs,
            "edited_paragraphs": result.edited_paragraphs,
            "flagged_count": len(result.flagged_paragraphs),
            "flagged": [asdict(f) for f in result.flagged_paragraphs],
            "failed_paragraphs": result.failed_paragraphs,
            "gcs_path": gcs_path,
        }
    )
    return run_id


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
    snapshot = _firestore_client().collection(COLLECTION).document(run_id).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict()
    data["run_id"] = snapshot.id
    return data


def get_docx_bytes(gcs_path: str) -> bytes:
    return _storage_client().bucket(BUCKET_NAME).blob(gcs_path).download_as_bytes()


def delete_run(run_id: str, gcs_path: str) -> None:
    _storage_client().bucket(BUCKET_NAME).blob(gcs_path).delete()
    _firestore_client().collection(COLLECTION).document(run_id).delete()
