"""Deterministic second pass enforcing the selected British/American English
convention, on top of whatever Gemini already produced. Runs before the
text is diffed against the original, so the fix flows through the same
track-changes injection pipeline as any other correction.

Only lowercase words are converted, deliberately: a capitalized word is very
often a proper noun or a sentence-initial common word, and breame has no
general dictionary to tell those apart reliably. Erring toward leaving a
capitalized word untouched is the safer failure mode here, matching the
project's "don't touch names/foreign words" requirement.

Note: breame only knows general BrE/AmE vocabulary pairs (colour/color,
organise/organize, etc). It is NOT a substitute for the PRHI style guide's
Indian-English/"Troublesome Words" glossary — that's handled by Gemini via
the style-guide prompt text, not by this layer.
"""

from __future__ import annotations

import re

from breame.spelling import (
    american_spelling_exists,
    british_spelling_exists,
    get_american_spelling,
    get_british_spelling,
)

_WORD_RE = re.compile(r"[a-zA-Z]+")


def _convert_word(word: str, variant: str) -> str:
    if not word.islower():
        return word

    if variant == "British":
        if american_spelling_exists(word) and not british_spelling_exists(word):
            return get_british_spelling(word)
    elif variant == "American":
        if british_spelling_exists(word) and not american_spelling_exists(word):
            return get_american_spelling(word)

    return word


def enforce_variant(text: str, variant: str) -> str:
    def replace(match: re.Match) -> str:
        return _convert_word(match.group(0), variant)

    return _WORD_RE.sub(replace, text)
