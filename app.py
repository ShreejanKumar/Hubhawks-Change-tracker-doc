from pathlib import Path

import streamlit as st

import main
from config import ENGLISH_VARIANTS, STYLE_GUIDES

st.set_page_config(page_title="Hubhawks Change Tracker", page_icon="📝")

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

st.title("Proofreading — Track Changes")
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

    st.success(
        f"Done. {result.edited_paragraphs} of {result.total_paragraphs} "
        f"paragraphs received tracked changes."
    )

    if result.flagged_paragraphs:
        with st.expander(
            f"{len(result.flagged_paragraphs)} paragraph(s) flagged for review "
            "(the correction changed an unusually large portion of the text)"
        ):
            for flagged in result.flagged_paragraphs:
                st.markdown("**Original:**")
                st.text(flagged.original)
                st.markdown("**Corrected:**")
                st.text(flagged.corrected)
                st.caption(f"~{flagged.changed_ratio:.0%} of words changed")
                st.divider()

    out_name = f"{Path(uploaded_file.name).stem}_tracked.docx"
    st.download_button(
        "Download corrected .docx",
        data=result.docx_bytes,
        file_name=out_name,
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
