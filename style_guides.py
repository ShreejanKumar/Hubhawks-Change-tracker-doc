from pathlib import Path

import streamlit as st

from config import STYLE_GUIDES

ROOT = Path(__file__).resolve().parent


@st.cache_resource
def load_style_guides() -> dict[str, str]:
    guides = {}
    for key, info in STYLE_GUIDES.items():
        path = ROOT / info["path"]
        guides[key] = path.read_text(encoding="utf-8")
    return guides


def get_style_guide_text(key: str) -> str:
    return load_style_guides()[key]
