import logging

import streamlit as st

import history_store
import job_launcher
from app_secrets import get_secret
from config import ENGLISH_VARIANTS, STYLE_GUIDES

logger = logging.getLogger(__name__)

# Proofreading runs in a Cloud Run Job (worker.py), not in this script. Runs
# used to execute here and were silently lost when a long run's websocket
# reconnected (Streamlit reruns the script and abandons the run in
# progress), and the app-wide lock this process needed meant editors queued
# behind each other. Now this page only creates a run, starts its job, and
# reads the run's Firestore doc, so refreshing, reconnecting, logging out
# or redeploying never loses a run, and any number of runs proceed at once.

STATUS_POLL_INTERVAL_S = 5

# Editor-facing messages per worker error kind (worker.py / history_store.fail_run).
FAILURE_MESSAGES = {
    "gemini_api": (
        "Gemini API error ({detail}). This is a billing/quota or authentication "
        "problem with the Gemini API key, not an issue with your document — the "
        "run was stopped immediately rather than retrying every paragraph. Please "
        "check the API key's billing status and try again."
    ),
    "paragraph_too_large": (
        "This document contains a paragraph too large to process ({detail}). "
        "Split that paragraph in Word and try again."
    ),
    "launch": (
        "The processing job couldn't be started ({detail}). Nothing was "
        "processed; please try again, or contact support if this keeps happening."
    ),
    "other": (
        "Something went wrong while processing this document ({detail}). This may "
        "mean the file isn't a valid/supported .docx. Try a different file, or "
        "contact support if this keeps happening."
    ),
}

st.set_page_config(page_title="Hubhawks Change Tracker", page_icon="📝", layout="wide")

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    st.title("Login")
    password_input = st.text_input("Enter Password", type="password")
    if st.button("Login"):
        if password_input == get_secret("APP_PASSWORD"):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password!")
    st.stop()

if "selected_run_id" not in st.session_state:
    st.session_state["selected_run_id"] = None


def render_failure_notice(failed_paragraphs, total_paragraphs) -> None:
    if not failed_paragraphs:
        return
    st.error(
        f"{len(failed_paragraphs)} of {total_paragraphs} paragraph(s) could not be "
        "checked at all — Gemini never returned a usable correction for them (API "
        "error, rate limit, or timeout) and they were left completely unreviewed, "
        "not just \"no changes needed\". Re-run the document to retry them."
    )


def render_flagged(flagged_list) -> None:
    with st.expander(
        f"{len(flagged_list)} paragraph(s) flagged for review "
        "(the correction changed an unusually large portion of the text)"
    ):
        for flagged in flagged_list:
            st.markdown("**Original:**")
            st.text(flagged["original"])
            st.markdown("**Corrected:**")
            st.text(flagged["corrected"])
            st.caption(f"~{flagged['changed_ratio']:.0%} of words changed")
            st.divider()


def render_run_failed(run: dict) -> None:
    if history_store.is_stale(run):
        st.error(
            "Processing stopped unexpectedly — the background job stopped reporting "
            "progress, so this run will not finish. Please run the document again."
        )
        return
    template = FAILURE_MESSAGES.get(run.get("error_kind"), FAILURE_MESSAGES["other"])
    st.error(template.format(detail=run.get("error_message", "no details")))


def status_label(run: dict) -> str:
    status = history_store.run_status(run)
    if history_store.is_stale(run) or status == history_store.STATUS_FAILED:
        return "❌ failed"
    if status == history_store.STATUS_QUEUED:
        return "⏳ starting"
    if status == history_store.STATUS_RUNNING:
        total = run.get("chunks_total") or 0
        return f"⏳ {run.get('chunks_done', 0)}/{total} parts" if total else "⏳ processing"
    stats = f"{run.get('edited_paragraphs', 0)}/{run.get('total_paragraphs', 0)} edited"
    if run.get("flagged_count"):
        stats += f" · {run['flagged_count']} flagged"
    return stats


def is_in_progress(run: dict) -> bool:
    return (
        history_store.run_status(run) in (history_store.STATUS_QUEUED, history_store.STATUS_RUNNING)
        and not history_store.is_stale(run)
    )


@st.fragment(run_every=STATUS_POLL_INTERVAL_S)
def render_progress(run_id: str) -> None:
    """Polls only this fragment, not the whole page. Once the worker finishes
    (or stops reporting), reruns the full app so the sidebar and the result
    view pick up the final state."""
    run = history_store.get_run(run_id)
    if run is None or not is_in_progress(run):
        st.rerun(scope="app")
    total = run.get("chunks_total") or 0
    done = run.get("chunks_done", 0)
    st.progress(done / total if total else 0.0)
    if history_store.run_status(run) == history_store.STATUS_QUEUED:
        st.caption("Starting the processing job…")
    elif total:
        st.caption(f"Processed {done} of {total} parts of the document…")
    else:
        st.caption("Reading the document…")


# ---------------------------------------------------------------- Sidebar --
with st.sidebar:
    st.header("History")

    if st.button("+ New proofreading", use_container_width=True):
        st.session_state["selected_run_id"] = None
        st.rerun()

    search = st.text_input("Search by filename or editor", placeholder="e.g. chapter1.docx or Ana")

    try:
        runs = history_store.list_runs(search=search)
        history_error = None
    except Exception as exc:
        runs = []
        history_error = exc

    if history_error is not None:
        st.error(f"Couldn't load history: {history_error}")
    elif not runs:
        st.caption("No matches." if search else "No runs yet — process a file to see it here.")

    for run in runs:
        created = run.get("created_at")
        created_label = created.strftime("%b %d, %H:%M") if created else "just now"

        with st.container(border=True):
            st.markdown(f"**{run.get('source_filename', 'Untitled')}**")
            st.caption(f"{created_label} · {run.get('editor', '')} · {status_label(run)}")
            col_view, col_delete = st.columns([3, 1])
            with col_view:
                if st.button("View", key=f"view_{run['run_id']}", use_container_width=True):
                    st.session_state["selected_run_id"] = run["run_id"]
                    st.rerun()
            with col_delete:
                # Not while it is still processing: the worker would only
                # stop at its next write, after spending the Gemini calls.
                if not is_in_progress(run) and st.button(
                    "🗑️", key=f"del_{run['run_id']}", help="Delete this run"
                ):
                    history_store.delete_run(run["run_id"])
                    if st.session_state["selected_run_id"] == run["run_id"]:
                        st.session_state["selected_run_id"] = None
                    st.rerun()


# -------------------------------------------------------------- Main area --
st.title("Proofreading — Track Changes")

selected_run_id = st.session_state["selected_run_id"]

if selected_run_id:
    run = history_store.get_run(selected_run_id)
    if run is None:
        st.warning("That run no longer exists — it may have been deleted.")
        st.session_state["selected_run_id"] = None
        st.rerun()

    st.caption(
        f"{run['source_filename']} · {run['editor']} · "
        f"{run['variant']} · {run['style_guide_label']}"
    )
    status = history_store.run_status(run)

    if is_in_progress(run):
        st.info(
            "Processing in the background. You can leave this page, start another "
            "document, or log out — the result will appear in History when it's done."
        )
        render_progress(selected_run_id)
    elif status == history_store.STATUS_DONE:
        st.success(
            f"{run['edited_paragraphs']} of {run['total_paragraphs']} paragraphs "
            "received tracked changes."
        )
        render_failure_notice(run.get("failed_paragraphs") or [], run["total_paragraphs"])

        if run.get("flagged"):
            render_flagged(run["flagged"])

        docx_bytes = history_store.get_docx_bytes(run["gcs_path"])
        st.download_button(
            "Download corrected .docx",
            data=docx_bytes,
            file_name=run["output_filename"],
            mime=history_store.DOCX_CONTENT_TYPE,
        )
    else:
        render_run_failed(run)
else:
    st.caption(
        "Upload a .docx manuscript. Grammar, spelling, punctuation, and style-guide "
        "corrections are returned as real Microsoft Word Track Changes — content, "
        "formatting, and images are left untouched."
    )

    uploaded_file = st.file_uploader("Upload .docx", type=["docx"])
    editor_name = st.text_input("Editor / Reviewer name", value="Editor")
    variant = st.radio("English convention", ENGLISH_VARIANTS, horizontal=True)
    style_guide_key = st.radio(
        "Style guide",
        options=list(STYLE_GUIDES.keys()),
        format_func=lambda key: STYLE_GUIDES[key]["label"],
    )

    run_clicked = st.button("Run Proofreading", type="primary", disabled=uploaded_file is None)

    if run_clicked and uploaded_file is not None:
        with st.spinner("Uploading and starting the job…"):
            try:
                new_run_id = history_store.create_run(
                    source_filename=uploaded_file.name,
                    docx_bytes=uploaded_file.getvalue(),
                    editor=editor_name.strip() or "Editor",
                    variant=variant,
                    style_guide_key=style_guide_key,
                    style_guide_label=STYLE_GUIDES[style_guide_key]["label"],
                )
            except Exception as exc:
                logger.exception("Couldn't create run")
                st.error(f"Couldn't upload the document: {exc}. Please try again.")
                st.stop()

            try:
                job_launcher.start_run(new_run_id)
            except Exception as exc:
                # Record it on the run so it shows as failed in History
                # rather than sitting "starting" until it goes stale.
                logger.exception("Couldn't start job for run %s", new_run_id)
                history_store.fail_run(new_run_id, "launch", str(exc))

        st.session_state["selected_run_id"] = new_run_id
        st.rerun()
