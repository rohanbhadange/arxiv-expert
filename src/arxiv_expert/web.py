"""Minimal local Streamlit interface."""

from __future__ import annotations

from contextlib import suppress
from datetime import timezone

import streamlit as st

from arxiv_expert.arxiv import ArxivClient
from arxiv_expert.categories import CATEGORIES, CATEGORY_BY_CODE
from arxiv_expert.chat import ChatService
from arxiv_expert.config import default_paths, load_settings
from arxiv_expert.database import Database
from arxiv_expert.openai_service import OpenAIService
from arxiv_expert.sync import SyncService

st.set_page_config(page_title="arXiv Expert", page_icon="📚", layout="wide")


def _resources():
    paths = default_paths()
    settings = load_settings(paths)
    db = Database(paths.database_file)
    db.initialize()
    backend = OpenAIService(settings)
    return paths, db, backend


def _citation_list(citations) -> None:
    if not citations:
        return
    with st.expander(f"Sources ({len(citations)})"):
        for citation in citations:
            badge = "Library" if citation.kind == "library" else "Web"
            st.markdown(f"**{citation.marker} · {badge}:** [{citation.title}]({citation.url})")


try:
    paths, db, backend = _resources()
except RuntimeError:
    st.title("arXiv Expert")
    st.info("Setup has not been completed. Run `arxiv-expert setup` in your terminal.")
    st.stop()

experts = db.list_experts()

st.sidebar.title("arXiv Expert")
with st.sidebar.expander("New expert"):
    existing_categories = {expert.category for expert in experts}
    available = [category for category in CATEGORIES if category.code not in existing_categories]
    if available:
        selected_category = st.selectbox(
            "Official arXiv topic",
            available,
            format_func=lambda category: category.label,
            key="new_category",
        )
        new_name = st.text_input("Expert name", value=selected_category.name)
        if st.button("Create expert", use_container_width=True):
            clean_name = new_name.strip()
            if not clean_name:
                st.error("Expert name cannot be empty.")
            else:
                try:
                    store_id = backend.create_vector_store(clean_name, selected_category.code)
                    try:
                        created = db.create_expert(clean_name, selected_category.code, store_id)
                    except Exception:
                        backend.delete_vector_store(store_id)
                        raise
                    st.session_state["expert_id"] = created.id
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not create expert: {exc}")
    else:
        st.caption("Every official topic already has an expert.")

if not experts:
    st.title("Create your first expert")
    st.write("Choose an official arXiv topic in the sidebar. Tracking starts at creation time.")
    st.stop()

expert_ids = [expert.id for expert in experts]
selected_id = st.session_state.get("expert_id")
if selected_id not in expert_ids:
    selected_id = expert_ids[0]
selected_index = expert_ids.index(selected_id)
expert = st.sidebar.selectbox(
    "Expert",
    experts,
    index=selected_index,
    format_func=lambda item: item.name,
)
st.session_state["expert_id"] = expert.id

counts = db.paper_counts(expert.id)
category = CATEGORY_BY_CODE[expert.category]
st.sidebar.caption(category.label)
st.sidebar.metric("Ready papers", counts["ready"])
last_sync = (
    expert.last_sync_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if expert.last_sync_at
    else "Not yet"
)
st.sidebar.caption(f"Last sync: {last_sync}")

if st.sidebar.button("Sync now", use_container_width=True):
    arxiv = ArxivClient()
    try:
        with st.spinner("Finding and indexing new papers..."):
            result = SyncService(db, arxiv, backend).sync_expert(expert)
        if result.skipped:
            st.sidebar.info("A background sync is already running.")
        else:
            st.sidebar.success(f"{result.ingested} indexed, {result.failed} failed")
        st.rerun()
    except Exception as exc:
        st.sidebar.error(f"Sync failed: {exc}")
    finally:
        arxiv.close()

with st.sidebar.expander("Delete expert"):
    st.warning("This removes the saved library and its chat history.")
    confirm_delete = st.checkbox(f"Delete {expert.name}", key=f"confirm_delete_{expert.id}")
    if st.button("Delete permanently", disabled=not confirm_delete, use_container_width=True):
        try:
            file_ids = [
                saved_paper.openai_file_id
                for saved_paper in db.list_papers(expert.id)
                if saved_paper.openai_file_id
            ]
            backend.delete_vector_store(expert.vector_store_id)
            for file_id in file_ids:
                with suppress(Exception):
                    backend.delete_file(file_id)
            db.delete_expert(expert.id)
            st.session_state.pop("expert_id", None)
            st.rerun()
        except Exception as exc:
            st.error(f"Could not delete expert: {exc}")

st.title(expert.name)
st.caption(
    f"Tracking {category.label} since {expert.created_at.astimezone(timezone.utc):%Y-%m-%d %H:%M UTC}"
)

chat_tab, library_tab = st.tabs(["Chat", "Library"])

with chat_tab:
    mode = st.radio(
        "Mode", ["Analyze", "Experiment Designer"], horizontal=True, label_visibility="collapsed"
    )
    top_left, top_right = st.columns([5, 1])
    with top_right:
        if st.button("Clear chat", use_container_width=True):
            db.clear_messages(expert.id)
            st.rerun()
    messages = db.list_messages(expert.id, limit=50)
    if not messages:
        if counts["ready"]:
            st.info(
                "Ask about the saved paper library, compare methods, or design a new experiment."
            )
        else:
            st.info(
                "This expert starts with no historical seed. You can still ask web-backed questions while it builds history."
            )
    for message in messages:
        with st.chat_message(message.role):
            st.markdown(message.content)
            if message.role == "assistant":
                _citation_list(message.citations)
    prompt = st.chat_input("Ask this expert...")
    if prompt:
        try:
            with st.spinner("Reading the evidence..."):
                answer = ChatService(db, backend).ask(expert, prompt, mode)
            st.rerun()
        except Exception as exc:
            st.error(f"The expert could not answer: {exc}")

with library_tab:
    papers = db.list_papers(expert.id)
    if not papers:
        st.info("No papers have been published for this expert since tracking began.")
    else:
        st.dataframe(
            [
                {
                    "Status": paper.status.title(),
                    "Published": paper.published_at.date().isoformat(),
                    "Paper": paper.title,
                    "Authors": ", ".join(paper.authors[:4])
                    + (" et al." if len(paper.authors) > 4 else ""),
                    "arXiv": paper.abstract_url,
                    "Error": paper.error or "",
                }
                for paper in papers
            ],
            use_container_width=True,
            hide_index=True,
            column_config={"arXiv": st.column_config.LinkColumn("arXiv")},
        )
