---
paths:
  - 'app.py'
  - 'history_store.py'
---

# UI, run lock and history

History: `C:/Users/kumar/Downloads/README.md` (local, not in the repo) §3 "Secrets" and "Deployment", §4.2, §4.5, §4.8, §4.9.

## The shared process

Streamlit Community Cloud runs **one process for every visitor**, with a memory ceiling.

- `_processing_lock()` is a process-wide `threading.Lock` (a singleton via `st.cache_resource`) around
  `process_document`. A second user waits and sees a "another run in progress" message. Never remove it.
- Don't keep large per-user objects in module globals or `cache_resource`.

## Errors the user sees

- `failed_paragraphs` must show the red "not checked" banner (`render_failure_notice`), both live and
  in the history view. "Unchecked" must never look like "clean".
- `flagged_paragraphs` go in the review expander (`render_flagged`).
- `genai_errors.APIError` → a clear "billing/quota or auth problem, not your document" message. Other
  exceptions get a generic fallback (corrupt upload, paragraph too large).

## History (`history_store.py`)

- GCS bucket `opportune-baton-505607-c2-hubhawks-docs`, objects at
  `runs/<run_id>/<file>_tracked.docx`. Firestore collection `runs` holds metadata, flagged paragraphs
  and failed ids.
- Credentials resolve in this order: `GCP_SERVICE_ACCOUNT_JSON` in secrets → local
  `gcp-service-account.json` → ADC. The GCS and Firestore clients are cached. The Gemini client is not.
- Search is a client-side substring match on filename or editor, over the latest 300 runs.
- This is the client's live data. Don't write to, delete from or test against it without asking.
  The skill's `run.py` never saves history.

## Auth

A single shared `APP_PASSWORD` gate. Never log or display secrets.
