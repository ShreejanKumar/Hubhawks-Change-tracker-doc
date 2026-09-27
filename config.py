GEMINI_MODEL = "gemini-3.8-flash"

# Fixed so repeated runs on the same document produce the same edits (best
# effort — combined with temperature=0, this is the standard lever Gemini
# exposes for run-to-run reproducibility, though it isn't an absolute
# guarantee per Google's own docs).
GEMINI_SEED = 42

# A full-size chunk (MAX_PARAGRAPHS_PER_CHUNK paragraphs, unbounded thinking)
# has been observed taking 2-3 minutes to respond. The timeout must comfortably
# exceed that per attempt, and transient errors (rate limits, transient 5xxs)
# are retried automatically by the SDK rather than falling through to
# gemini_client's "leave this paragraph unchanged" fallback.
GEMINI_REQUEST_TIMEOUT_MS = 300_000
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

# Bounded rather than -1 (dynamic/unlimited): a 150-paragraph chunk has been
# observed spending ~27,000 thinking tokens and taking minutes, which is most
# of the tool's worst-case latency and timeout risk. Isolated single
# paragraphs need only ~300-500 thinking tokens even with the full system
# prompt and style guide, so this still leaves large headroom for genuinely
# hard cases while capping how long any one call can run.
GEMINI_THINKING_BUDGET = 8192

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
# run. Bounded rather than unlimited: Streamlit Community Cloud runs this
# app as a single process shared by every visitor (see the processing lock
# in app.py), so firing every chunk in a large document at once would
# multiply that one document's peak memory/API load well beyond what a
# sequential run needs, and risks tripping Gemini's own rate limits. 3
# gives a meaningful wall-clock speedup for multi-chunk documents while
# keeping that peak bounded and predictable.
GEMINI_MAX_CONCURRENT_CHUNKS = 3

# Guardrail: fraction of a paragraph's tokens that may be changed before
# we retry with a stricter "make fewer changes" instruction.
MAX_CHANGED_TOKEN_RATIO = 0.35

# The ratio above is only meaningful against a large-enough denominator. A
# paragraph below this many tokens (e.g. a one-line "Please come in..."
# exclamation, tokenizing to ~8 tokens including each punctuation mark as
# its own token) can have a single, entirely correct mechanical fix -- like
# expanding one ellipsis character into the style guide's " . . ." spacing
# -- dominate its ratio and trip the guardrail purely because there was so
# little else in the paragraph to divide by. Below this floor, the ratio
# guardrail (retry + flagging) is skipped entirely and the correction is
# just applied.
MIN_TOKENS_FOR_CHANGE_RATIO_GUARDRAIL = 10

STYLE_GUIDES = {
    "old_inhouse": {
        "label": "Old In-House Stylesheet",
        "path": "style_guides/old_inhouse_stylesheet.txt",
    },
    "prhi": {
        "label": "PRHI Style Guide",
        "path": "style_guides/prhi_style_guide.txt",
    },
}

ENGLISH_VARIANTS = ["British", "American"]
