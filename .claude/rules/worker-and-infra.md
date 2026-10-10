---
paths:
  - 'worker.py'
  - 'job_launcher.py'
  - 'Dockerfile'
  - '.gcloudignore'
  - '.dockerignore'
  - 'requirements.txt'
  - 'config.py'
---

# Cloud Run Job worker and GCP setup

## The job

- Cloud Run Job `hubhawks-worker`, project `opportune-baton-505607-c2`, region **us-central1** (same as
  Firestore and the bucket). One execution per run, started by `job_launcher.start_run` with env
  `RUN_ID`.
- Settings that must stay this way:
  - **Task timeout 3h.** The 10-minute default would kill long manuscripts and recreate the original bug.
  - **Max retries 0.** A retry would re-bill Gemini. `worker.py` also refuses any run that isn't `queued`.
  - 2 GiB memory, 1 vCPU, 1 task.
- Identity: runs as `hubhawks-app@…` (object admin on the bucket, `datastore.user`, secret accessor on
  `gemini-api-key`). The Gemini key arrives as env `GEMINI_API_KEY` from Secret Manager secret
  `gemini-api-key`. The app's SA (same account) needs permission to run the job with overrides.

## Deploying

The UI deploys from `main` on Streamlit Cloud. **The worker does not.** After changing any module the
worker uses (`worker.py`, `main.py`, `gemini_client.py`, `chunking.py`, `docx_*.py`, `history_store.py`,
`config.py`, `style_guides*`, `requirements.txt`), redeploy the job:

```bash
gcloud run jobs deploy hubhawks-worker --source . --region us-central1 \
  --project opportune-baton-505607-c2 \
  --service-account hubhawks-app@opportune-baton-505607-c2.iam.gserviceaccount.com \
  --set-secrets GEMINI_API_KEY=gemini-api-key:latest \
  --memory 2Gi --cpu 1 --task-timeout 3h --max-retries 0 --tasks 1 --quiet
```

Deploy the worker **before** pushing app changes that depend on it. Runs already in progress keep the
image they started with.

## Image

- The `Dockerfile` copies an explicit file list. Add new worker modules there. Never use `COPY .`,
  because secrets must never reach the image. `.gcloudignore`/`.dockerignore` exclude them as well.
- Python 3.10.12 with the same pinned `requirements.txt` as the app, so behaviour is identical.

## Debugging a run

- Logs: `gcloud logging read 'resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="<exec>"' --limit 50`
  or `gcloud run jobs executions list --job hubhawks-worker --region us-central1`.
- Local run against real GCS/Firestore: `python worker.py --run-id <id>` (a queued doc must exist).
  This is paid, so ask first.
