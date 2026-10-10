from functools import lru_cache
from pathlib import Path

from config import STYLE_GUIDES

ROOT = Path(__file__).resolve().parent


# lru_cache rather than st.cache_resource: this also runs in the Cloud Run
# worker, outside any Streamlit runtime.
@lru_cache(maxsize=1)
def load_style_guides() -> dict[str, str]:
    guides = {}
    for key, info in STYLE_GUIDES.items():
        path = ROOT / info["path"]
        guides[key] = path.read_text(encoding="utf-8")
    return guides


def get_style_guide_text(key: str) -> str:
    return load_style_guides()[key]
