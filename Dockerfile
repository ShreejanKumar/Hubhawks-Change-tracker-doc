# Cloud Run Job image for worker.py (one proofreading run per execution).
# Same Python and pinned requirements the app was developed and verified on,
# so the pipeline behaves identically in the worker.
FROM python:3.10.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Explicit list rather than `COPY .`: secrets.toml and the service account
# key must never end up in the image, whatever the ignore files say. The
# worker authenticates as the job's service account, and its Gemini key is
# mounted from Secret Manager as an env var.
COPY app_secrets.py chapter_numbering.py chunking.py config.py docx_extract.py docx_track_changes.py \
     gemini_client.py history_store.py main.py style_guides.py worker.py ./
COPY style_guides/ ./style_guides/

CMD ["python", "worker.py"]
