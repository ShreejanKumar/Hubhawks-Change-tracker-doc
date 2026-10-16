import os

GEMINI_MODEL = "gemini-3.8-flash"

# Fixed so repeated runs on the same document produce the same edits (best
# effort, not a guarantee per Google's own docs). temperature/top_p/top_k are
# deliberately NOT sent: since Gemini 3.6 Flash they have no effect, and
# Google has announced upcoming models will reject them with an error.
GEMINI_SEED = 42

# Per-attempt request timeout. With thinking_level "medium", a single
# ~10k-token chunk was measured at ~2 minutes (2026-10-11); full 30k-token
# chunks can take several times that, so 10 minutes leaves headroom rather
# than timing out (which would fall back to halving and burn retries).
# Transient errors (rate limits, transient 5xxs) are retried automatically
# by the SDK rather than falling through to gemini_client's "leave this
# paragraph unchanged" fallback.
GEMINI_REQUEST_TIMEOUT_MS = 600_000
GEMINI_RETRY_ATTEMPTS = 4
GEMINI_RETRY_INITIAL_DELAY_S = 2.0
GEMINI_RETRY_MAX_DELAY_S = 30.0

# HTTP status codes worth retrying (transient: rate limiting, server-side
# hiccups). Any other status code from the API (e.g. 401/403 auth errors,
# 402 exhausted billing, 400 bad request) is treated as fatal — retrying it
# is guaranteed to fail again, so both the SDK's own retry_options and our
# manual retry wrapper (gemini_client.py) skip retrying those and fail fast
# instead of burning through the full retry budget on every single paragraph
# in the document.
GEMINI_RETRYABLE_HTTP_STATUS_CODES = [429, 500, 502, 503, 504]

# thinking_level replaces the old thinking_budget (8192), which Google has
# announced upcoming models will reject with 400 INVALID_ARGUMENT. Measured on
# one Varun chunk (2026-10-11): the old budget was being remapped to ~940
# thinking tokens (32 paragraphs corrected); "low" 0 tokens (27); "medium",
# the model default, ~11.7k (35); "high" ~33k (34) at 2.4x medium's time,
# risking request timeouts on full-size chunks. "medium" thinks more than
# before and found the most corrections without that risk. gemini-3.8-flash
# supports low/medium/high.
GEMINI_THINKING_LEVEL = "MEDIUM"

# The SDK's own retry_options (below) only fires for specific HTTP status
# codes. A timeout or connection reset raises a different exception and
# would otherwise get exactly one attempt before the paragraph is given up
# on as "failed". This retries any other exception too.
GEMINI_MANUAL_RETRY_ATTEMPTS = 3

# Chunking
INPUT_BUDGET_TOKENS_PER_CHUNK = 30000
MAX_PARAGRAPHS_PER_CHUNK = 150
SINGLE_PARAGRAPH_HARD_CAP_TOKENS = 30000
CHARS_PER_TOKEN_ESTIMATE = 4

# How many chunks may be corrected concurrently within a single document
# run. This used to be 3 because runs happened inside the one Streamlit
# Community Cloud process shared by every visitor. Runs now execute in their
# own Cloud Run Job container (worker.py), so memory is no longer shared and
# the remaining limit is the Gemini key's quota, shared by every run at once
# (~150 RPM). Chunk calls take 2-3 minutes each, so even several concurrent
# runs at 10 chunks stay far below that; a 429 is backed off and retried by
# the SDK. Concurrency only changes wall-clock time, never the corrections:
# each chunk's input is identical regardless of what runs beside it.
# Overridable per deployment via the job's env without a code change.
GEMINI_MAX_CONCURRENT_CHUNKS = int(os.environ.get("GEMINI_MAX_CONCURRENT_CHUNKS", "10"))

# Guardrail: fraction of a paragraph's WORDS that may be changed before we
# retry with a stricter "make fewer changes" instruction (and, if the retry
# still exceeds it, flag the paragraph for review). Set to 0.60 on the
# project owner's request (2026-10-11), together with switching the measure
# to words only (docx_track_changes.changed_token_ratio). The old measure
# counted whitespace and punctuation tokens too, so its 0.35 already meant
# roughly "most words rewritten" and 0.60 would have let a paragraph with
# every word replaced (~0.52) through unchecked.
MAX_CHANGED_TOKEN_RATIO = 0.60

# The ratio above is only meaningful against a large-enough denominator. A
# paragraph with fewer words than this can have one entirely correct fix
# dominate its ratio purely because there is so little else to divide by, so
# below this floor the guardrail (retry + flagging) is skipped and the
# correction is just applied. 5 words matches the old floor of 10 tokens
# (each word plus its following space or punctuation mark).
MIN_TOKENS_FOR_CHANGE_RATIO_GUARDRAIL = 5

STYLE_GUIDES = {
    "old_inhouse": {
        "label": "Old In-House Stylesheet",
        "path": "style_guides/old_inhouse_stylesheet.txt",
    },
}

ENGLISH_VARIANTS = ["British", "American"]

# Background processing (Cloud Run Job, see worker.py). Same project and
# region as the Firestore database and the GCS bucket (us-central1).
GCP_PROJECT_ID = "opportune-baton-505607-c2"
CLOUD_RUN_REGION = "us-central1"
CLOUD_RUN_JOB = "hubhawks-worker"

# The worker refreshes `heartbeat_at` this often while a run is in progress,
# independent of chunk progress (a single chunk call can take minutes).
WORKER_HEARTBEAT_INTERVAL_S = 30
# A queued/running run whose heartbeat is older than this is shown as having
# stopped unexpectedly (container killed, job failed to start), so a run can
# never again vanish without the editor being told.
WORKER_STALE_AFTER_S = 600
