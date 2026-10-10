"""Secret lookup that works both in the Streamlit app and in the Cloud Run
worker.

The worker has no `.streamlit/secrets.toml` (and must never ship one in its
image): its Gemini key arrives as an env var mounted from Secret Manager and
its GCP identity is the job's attached service account. The Streamlit app
keeps reading `st.secrets` as before. Env wins so either side can be
overridden without touching the other.
"""

import os
from typing import Optional


def get_secret(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value:
        return value
    try:
        import streamlit as st

        # Touching st.secrets with no secrets.toml present raises rather than
        # returning empty, which is exactly the worker's situation.
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return None
