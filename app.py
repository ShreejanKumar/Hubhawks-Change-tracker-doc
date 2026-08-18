from pathlib import Path

import streamlit as st

import history_store
import main
from config import ENGLISH_VARIANTS, STYLE_GUIDES

st.set_page_config(page_title="Hubhawks Change Tracker", page_icon="📝", layout="wide")

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    st.title("Login")
    password_input = st.text_input("Enter Password", type="password")
    if st.button("Login"):
        if password_input == st.secrets["APP_PASSWORD"]:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password!")
    st.stop()

if "selected_run_id" not in st.session_state:
    st.session_state["selected_run_id"] = None


def render_flagged(flagged_list) -> None:
    with st.expander(
        f"{len(flagged_list)} paragraph(s) flagged for review "
        "(the correction changed an unusually large portion of the text)"
    ):
        for flagged in flagged_list:
            is_dict = isinstance(flagged, dict)
            original = flagged["original"] if is_dict else flagged.original
            corrected = flagged["corrected"] if is_dict else flagged.corrected
            ratio = flagged["changed_ratio"] if is_dict else flagged.changed_ratio
            st.markdown("**Original:**")
            st.text(original)
            st.markdown("**Corrected:**")
            st.text(corrected)
            st.caption(f"~{ratio:.0%} of words changed")
            st.divider()


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
        stats = f"{run.get('edited_paragraphs', 0)}/{run.get('total_paragraphs', 0)} edited"
        if run.get("flagged_count"):
            stats += f" · {run['flagged_count']} flagged"

        with st.container(border=True):
            st.markdown(f"**{run.get('source_filename', 'Untitled')}**")
            st.caption(f"{created_label} · {run.get('editor', '')} · {stats}")
            col_view, col_delete = st.columns([3, 1])
            with col_view:
                if st.button("View", key=f"view_{run['run_id']}", use_container_width=True):
                    st.session_state["selected_run_id"] = run["run_id"]
                    st.rerun()
            with col_delete:
                if st.button("🗑️", key=f"del_{run['run_id']}", help="Delete this run"):
                    history_store.delete_run(run["run_id"], run["gcs_path"])
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
    else:
        st.caption(
            f"Viewing history · {run['source_filename']} · {run['editor']} · "
            f"{run['variant']} · {run['style_guide_label']}"
        )
        st.success(
            f"{run['edited_paragraphs']} of {run['total_paragraphs']} paragraphs "
            "received tracked changes."
        )

        if run.get("flagged"):
            render_flagged(run["flagged"])

        docx_bytes = history_store.get_docx_bytes(run["gcs_path"])
        st.download_button(
            "Download corrected .docx",
            data=docx_bytes,
            file_name=run["output_filename"],
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
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
        progress_bar = st.progress(0.0)
        status_text = st.empty()

        def on_progress(done: int, total: int) -> None:
            progress_bar.progress(done / total)
            status_text.text(f"Processing chunk {done} of {total}...")

        with st.spinner("Running proofreading..."):
            result = main.process_document(
                docx_bytes=uploaded_file.getvalue(),
                author=editor_name.strip() or "Editor",
                variant=variant,
                style_guide_key=style_guide_key,
                progress_callback=on_progress,
            )

        progress_bar.progress(1.0)
        status_text.empty()

        out_name = f"{Path(uploaded_file.name).stem}_tracked.docx"

        try:
            new_run_id = history_store.save_run(
                source_filename=uploaded_file.name,
                output_filename=out_name,
                editor=editor_name.strip() or "Editor",
                variant=variant,
                style_guide_key=style_guide_key,
                style_guide_label=STYLE_GUIDES[style_guide_key]["label"],
                result=result,
            )
        except Exception as exc:
            new_run_id = None
            st.warning(f"Processed successfully, but couldn't save to history: {exc}")

        if new_run_id:
            # Hand off to the history viewer above so the sidebar and main
            # area both reflect the freshly saved run on the next rerun.
            st.session_state["selected_run_id"] = new_run_id
            st.rerun()

        st.success(
            f"Done. {result.edited_paragraphs} of {result.total_paragraphs} "
            f"paragraphs received tracked changes."
        )

        if result.flagged_paragraphs:
            render_flagged(result.flagged_paragraphs)

        st.download_button(
            "Download corrected .docx",
            data=result.docx_bytes,
            file_name=out_name,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
