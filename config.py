GEMINI_MODEL = "gemini-2.5-flash"

# Chunking
INPUT_BUDGET_TOKENS_PER_CHUNK = 30000
MAX_PARAGRAPHS_PER_CHUNK = 150
SINGLE_PARAGRAPH_HARD_CAP_TOKENS = 30000
CHARS_PER_TOKEN_ESTIMATE = 4

# Guardrail: fraction of a paragraph's tokens that may be changed before
# we retry with a stricter "make fewer changes" instruction.
MAX_CHANGED_TOKEN_RATIO = 0.35

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
