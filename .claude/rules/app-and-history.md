---
paths:
  - 'app.py'
  - 'history_store.py'
  - 'job_launcher.py'
  - 'app_secrets.py'
---

# UI, run lifecycle and history

History: `C:/Users/kumar/Downloads/README.md` (local, not in the repo) §3 "Secrets" and "Deployment", §4.2, §4.5, §4.8.
§4.9 (the app-wide lock) is superseded: see "Why the app doesn't process" below.

## Why the app doesn't process

Runs used to execute inside the Streamlit script. A 20–30 minute run was silently lost whenever the
browser's websocket reconnected: Streamlit reran the script, abandoning the run, and the editor saw the
page "refresh" while still logged in, with no error. The app-wide lock (needed because everything ran
in one shared Cloud process) also made editors wait for each other.

So `app.py` must **never** call `process_document` or Gemini. It only:
1. `history_store.create_run` (input to `runs/<id>/input.docx`, doc with `status: queued`),
2. `job_launcher.start_run` (on failure: `fail_run(id, "launch", …)`, never leave it queued),
3. reads the run doc. `render_progress` is a `st.fragment(run_every=5)` that polls only itself and
   reruns the whole app once the run leaves queued/running.

## Run doc (`runs/<id>`)

- `status`: `queued` → `running` → `done` | `failed`. **No status = an old run, and it counts as done.**
- The worker writes `chunks_done/total`, `heartbeat_at` (every write and every 30s), and either the
  stats (`total_paragraphs`, `edited_paragraphs`, `flagged`, `failed_paragraphs`) or
  `error_kind` + `error_message`.
- `is_stale`: queued/running with heartbeat older than `WORKER_STALE_AFTER_S` (10 min) is shown as
  failed ("stopped unexpectedly"). Display only; nothing writes it.
- Worker writes use `update()`, never `set`/merge, so a run deleted mid-processing isn't recreated
  (`RunDeletedError`). Delete is hidden in the UI while a run is in progress.
- `error_kind` values map to editor messages in `FAILURE_MESSAGES`: `gemini_api`, `paragraph_too_large`,
  `launch`, `other`. Add the message when you add a kind.

## Errors the user sees

- `failed_paragraphs` must show the red "not checked" banner (`render_failure_notice`). "Unchecked"
  must never look like "clean".
- `flagged` goes in the review expander (`render_flagged`, which takes dicts from Firestore).

## Storage and credentials

- GCS bucket `opportune-baton-505607-c2-hubhawks-docs`, everything for a run under `runs/<run_id>/`.
  `delete_run` removes the whole prefix. Firestore collection `runs`.
- `history_store.credentials()`: `GCP_SERVICE_ACCOUNT_JSON` (env or secrets) → local
  `gcp-service-account.json` → ADC (the worker's attached service account). Clients are
  `lru_cache`d, not `st.cache_resource`, because these modules also run in the worker.
- Secrets go through `app_secrets.get_secret` (env first, then `st.secrets`). Never `st.secrets` directly.
- This is the client's live data. Don't write to, delete from or test against it without asking.

## Auth

A single shared `APP_PASSWORD` gate. Never log or display secrets.
