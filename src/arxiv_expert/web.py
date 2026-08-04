"""Minimal local Streamlit interface."""

from __future__ import annotations

from collections import Counter
from contextlib import suppress
from datetime import timezone

import streamlit as st

from arxiv_expert.arxiv import ArxivClient
from arxiv_expert.categories import CATEGORIES, CATEGORY_BY_CODE
from arxiv_expert.chat import ChatService
from arxiv_expert.config import default_paths, load_settings
from arxiv_expert.database import Database
from arxiv_expert.openai_service import OpenAIService
from arxiv_expert.paper_import import (
    MAX_FOCUS_PAPERS,
    PaperCommandService,
    PaperImportService,
    parse_paper_command,
)
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
        if st.button("Create expert", width="stretch"):
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

conversations = db.list_conversations(expert.id)
if not conversations:
    conversations = [db.create_conversation(expert.id)]
conversation_key = f"conversation_id_{expert.id}"
picker_key = f"conversation_picker_{expert.id}"
forced_conversation_id = st.session_state.pop(f"force_{conversation_key}", None)
if forced_conversation_id:
    st.session_state.pop(picker_key, None)
selected_conversation_id = forced_conversation_id or st.session_state.get(conversation_key)
conversation_ids = [item.id for item in conversations]
conversation_title_counts = Counter(item.title for item in conversations)
if selected_conversation_id not in conversation_ids:
    selected_conversation_id = conversation_ids[0]
conversation = st.sidebar.selectbox(
    "Conversation",
    conversations,
    index=conversation_ids.index(selected_conversation_id),
    format_func=lambda item: (
        item.title
        if conversation_title_counts[item.title] == 1
        else f"{item.title} · {item.id[:4]}"
    ),
    key=picker_key,
)
st.session_state[conversation_key] = conversation.id

if st.sidebar.button("New chat", width="stretch"):
    created_conversation = db.create_conversation(expert.id)
    st.session_state[f"force_{conversation_key}"] = created_conversation.id
    st.rerun()

with st.sidebar.expander("Delete conversation"):
    confirm_chat_delete = st.checkbox(
        f"Delete “{conversation.title}”", key=f"delete_conversation_{conversation.id}"
    )
    if st.button(
        "Delete conversation",
        key=f"delete_conversation_button_{conversation.id}",
        disabled=not confirm_chat_delete,
        width="stretch",
    ):
        db.delete_conversation(conversation.id)
        remaining = db.list_conversations(expert.id)
        if not remaining:
            remaining = [db.create_conversation(expert.id)]
        st.session_state[f"force_{conversation_key}"] = remaining[0].id
        pending = st.session_state.get("pending_imports", {})
        pending.pop(conversation.id, None)
        st.rerun()

with st.sidebar.expander("Rename conversation"):
    renamed = st.text_input(
        "Conversation name", value=conversation.title, key=f"rename_{conversation.id}"
    )
    if st.button("Save name", key=f"save_name_{conversation.id}", width="stretch"):
        try:
            db.rename_conversation(conversation.id, renamed)
            st.session_state[f"force_{conversation_key}"] = conversation.id
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

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

if st.sidebar.button("Sync now", width="stretch"):
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
    if st.button("Delete permanently", disabled=not confirm_delete, width="stretch"):
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
    focused_papers = db.list_conversation_papers(conversation.id)
    with st.container(border=True):
        st.markdown(f"**Focused papers ({len(focused_papers)}/{MAX_FOCUS_PAPERS})**")
        if focused_papers:
            st.caption(
                "Answers stay within these papers. Say “search the full library” or “search the web” for a one-turn exception."
            )
            for index, focused_paper in enumerate(focused_papers, start=1):
                paper_col, remove_col = st.columns([8, 1])
                with paper_col:
                    status = "" if focused_paper.status == "ready" else f" · {focused_paper.status}"
                    st.markdown(
                        f"**F{index}** · [{focused_paper.title}]({focused_paper.abstract_url})"
                        f" · arXiv:{focused_paper.arxiv_id}{status}"
                    )
                with remove_col:
                    if st.button(
                        "Remove",
                        key=f"unfocus_{conversation.id}_{focused_paper.id}",
                        width="stretch",
                    ):
                        db.unfocus_paper(conversation.id, focused_paper.id)
                        st.rerun()
        else:
            st.caption(
                "No focus yet: this chat searches the full saved library. To narrow it, use the Library tab or type “Import arXiv:…” below."
            )

    with st.expander("Paper commands"):
        st.markdown(
            """
- `Import arXiv:2401.12345` — import an exact paper now
- `Add the paper "DreamerV3"` — search arXiv by title, then choose
- `Focus on Learning to Move` — use a paper already in the library
- `Remove arXiv:2401.12345 from this chat` — remove it from this focus only
"""
        )

    messages = db.list_messages(conversation.id, limit=50)
    if not messages:
        if counts["ready"]:
            st.info(
                "Ask about the saved paper library, compare methods, or design a new experiment."
            )
        else:
            st.info(
                "This expert starts with no historical seed. You can ask web-backed questions or type “Import arXiv:2401.12345” to add a paper."
            )
    for message in messages:
        with st.chat_message(message.role):
            st.markdown(message.content)
            if message.role == "assistant":
                _citation_list(message.citations)
    pending_imports = st.session_state.setdefault("pending_imports", {})
    candidates = pending_imports.get(conversation.id, ())
    if candidates:
        st.markdown("**Select one paper:**")
        for candidate in candidates:
            candidate_col, action_col = st.columns([7, 2])
            with candidate_col:
                authors = ", ".join(candidate.authors[:3])
                st.markdown(
                    f"[{candidate.title}]({candidate.abstract_url})  \n"
                    f"{authors} · {candidate.published_at.date().isoformat()} · arXiv:{candidate.arxiv_id}"
                )
            with action_col:
                if st.button(
                    "Import and focus",
                    key=f"confirm_{conversation.id}_{candidate.arxiv_id}",
                    width="stretch",
                ):
                    arxiv = ArxivClient()
                    try:
                        with st.spinner("Importing and indexing the paper..."):
                            command_service = PaperCommandService(
                                db, PaperImportService(db, arxiv, backend)
                            )
                            command_service.confirm_candidate(
                                expert, conversation.id, candidate.arxiv_id, mode
                            )
                        pending_imports.pop(conversation.id, None)
                        st.rerun()
                    finally:
                        arxiv.close()

    prompt = st.chat_input("Ask this expert, or type “Import arXiv:…”")
    if prompt:
        try:
            with st.spinner("Reading the evidence..."):
                command = parse_paper_command(prompt)
                if command:
                    arxiv = ArxivClient()
                    try:
                        outcome = PaperCommandService(
                            db, PaperImportService(db, arxiv, backend)
                        ).handle(expert, conversation.id, prompt, mode)
                    finally:
                        arxiv.close()
                    if outcome and outcome.candidates:
                        pending_imports[conversation.id] = outcome.candidates
                    else:
                        pending_imports.pop(conversation.id, None)
                else:
                    ChatService(db, backend).ask(expert, conversation.id, prompt, mode)
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
                    "Source": "Manual" if paper.origin == "manual" else "Topic",
                    "Published": paper.published_at.date().isoformat(),
                    "Paper": paper.title,
                    "Authors": ", ".join(paper.authors[:4])
                    + (" et al." if len(paper.authors) > 4 else ""),
                    "arXiv": paper.abstract_url,
                    "Error": paper.error or "",
                }
                for paper in papers
            ],
            width="stretch",
            hide_index=True,
            column_config={"arXiv": st.column_config.LinkColumn("arXiv")},
        )
        focused_ids = {paper.id for paper in db.list_conversation_papers(conversation.id)}
        available_to_focus = [paper for paper in papers if paper.id not in focused_ids]
        if available_to_focus:
            selected_paper = st.selectbox(
                "Add a saved paper to this conversation",
                available_to_focus,
                format_func=lambda item: f"{item.title} · arXiv:{item.arxiv_id}",
            )
            if st.button(
                "Focus in this chat",
                disabled=len(focused_ids) >= MAX_FOCUS_PAPERS,
                width="stretch",
            ):
                try:
                    db.focus_paper(conversation.id, selected_paper.id)
                    db.title_new_conversation(conversation.id, selected_paper.title)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        elif papers:
            st.caption("Every saved paper is already focused in this chat.")
