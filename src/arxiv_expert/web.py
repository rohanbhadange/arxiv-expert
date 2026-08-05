"""Minimal local Streamlit interface."""

from __future__ import annotations

from contextlib import suppress
from datetime import timezone

import streamlit as st

from arxiv_expert.arxiv import ArxivClient
from arxiv_expert.categories import CATEGORIES, CATEGORY_BY_CODE
from arxiv_expert.chat import ChatService
from arxiv_expert.config import default_paths, load_settings
from arxiv_expert.database import NEW_CHAT_TITLE, Database
from arxiv_expert.openai_service import OpenAIService
from arxiv_expert.paper_import import (
    MAX_FOCUS_PAPERS,
    PaperCommandService,
    PaperImportService,
    parse_paper_command,
)
from arxiv_expert.sync import SyncService

st.set_page_config(page_title="arXiv Expert", page_icon="📚", layout="wide")

CHAT_DIALOG_KEY = "chat_dialog"


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


def _create_expert_form(experts, db, backend) -> None:
    existing_categories = {saved_expert.category for saved_expert in experts}
    available = [category for category in CATEGORIES if category.code not in existing_categories]
    if not available:
        st.caption("Every official topic already has an expert.")
        return
    selected_category = st.selectbox(
        "Official arXiv topic",
        available,
        format_func=lambda category: category.label,
        key="new_category",
    )
    new_name = st.text_input("Expert name", value=selected_category.name, key="new_expert_name")
    if st.button("Create expert", key="create_expert", width="stretch"):
        clean_name = new_name.strip()
        if not clean_name:
            st.error("Expert name cannot be empty.")
            return
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


def _dismiss_chat_dialog() -> None:
    st.session_state.pop(CHAT_DIALOG_KEY, None)


@st.dialog("Rename chat", on_dismiss=_dismiss_chat_dialog)
def _rename_chat_dialog(db, conversation) -> None:
    renamed = st.text_input(
        "Chat name", value=conversation.title, key=f"rename_dialog_{conversation.id}"
    )
    save_col, cancel_col = st.columns(2)
    if save_col.button("Save", key=f"save_name_{conversation.id}", type="primary", width="stretch"):
        try:
            db.rename_conversation(conversation.id, renamed)
            st.session_state.pop(CHAT_DIALOG_KEY, None)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    if cancel_col.button("Cancel", key=f"cancel_rename_{conversation.id}", width="stretch"):
        st.session_state.pop(CHAT_DIALOG_KEY, None)
        st.rerun()


@st.dialog("Delete chat", on_dismiss=_dismiss_chat_dialog)
def _delete_chat_dialog(db, expert, conversation, active_key, pending_imports) -> None:
    st.write(f"Delete **{conversation.title}** and all of its messages?")
    st.caption("This cannot be undone.")
    delete_col, cancel_col = st.columns(2)
    if delete_col.button(
        "Delete", key=f"confirm_delete_chat_{conversation.id}", type="primary", width="stretch"
    ):
        db.delete_conversation(conversation.id)
        remaining = db.list_conversations(expert.id)
        if not remaining:
            remaining = [db.create_conversation(expert.id)]
        st.session_state[active_key] = remaining[0].id
        pending_imports.pop(conversation.id, None)
        st.session_state.pop(CHAT_DIALOG_KEY, None)
        st.rerun()
    if cancel_col.button("Cancel", key=f"cancel_delete_{conversation.id}", width="stretch"):
        st.session_state.pop(CHAT_DIALOG_KEY, None)
        st.rerun()


try:
    paths, db, backend = _resources()
except RuntimeError:
    st.title("arXiv Expert")
    st.info("Setup has not been completed. Run `arxiv-expert setup` in your terminal.")
    st.stop()

experts = db.list_experts()

if not experts:
    st.title("Create your first expert")
    st.write("Choose an official arXiv topic. Tracking starts at creation time.")
    _create_expert_form(experts, db, backend)
    st.stop()

expert_ids = [expert.id for expert in experts]
selected_id = st.session_state.get("expert_id")
if selected_id not in expert_ids:
    selected_id = expert_ids[0]
expert = next(item for item in experts if item.id == selected_id)
st.session_state["expert_id"] = expert.id

st.markdown(
    """
    <style>
    [data-testid="stSidebar"] [class*="st-key-chat_select_"] button {
        justify-content: flex-start;
        text-align: left;
    }
    [data-testid="stSidebar"] [class*="st-key-chat_select_"] button p {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    [data-testid="stSidebar"] [class*="st-key-chat_menu_"] {
        opacity: 0;
        transition: opacity 120ms ease-in-out;
    }
    [data-testid="stSidebar"] [class*="st-key-chat_row_"]:hover [class*="st-key-chat_menu_"],
    [data-testid="stSidebar"] [class*="st-key-chat_row_"]:focus-within [class*="st-key-chat_menu_"] {
        opacity: 1;
    }
    @media (hover: none) {
        [data-testid="stSidebar"] [class*="st-key-chat_menu_"] {
            opacity: 1;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar.popover(
    expert.name,
    icon=":material/school:",
    type="tertiary",
    width="stretch",
    key=f"expert_switcher_{expert.id}",
):
    st.caption("Switch expert")
    for saved_expert in experts:
        if st.button(
            saved_expert.name,
            key=f"switch_expert_{saved_expert.id}",
            disabled=saved_expert.id == expert.id,
            type="tertiary",
            width="stretch",
        ):
            st.session_state["expert_id"] = saved_expert.id
            st.rerun()
        st.caption(CATEGORY_BY_CODE[saved_expert.category].label)
    st.divider()
    st.markdown("**Add expert**")
    _create_expert_form(experts, db, backend)

conversations = db.list_conversations(expert.id)
if not conversations:
    conversations = [db.create_conversation(expert.id)]
active_conversation_key = f"active_conversation_id_{expert.id}"
for obsolete_key in (
    f"conversation_id_{expert.id}",
    f"conversation_picker_{expert.id}",
    f"force_conversation_id_{expert.id}",
):
    st.session_state.pop(obsolete_key, None)

conversations_by_id = {item.id: item for item in conversations}
active_conversation_id = st.session_state.get(active_conversation_key)
if active_conversation_id not in conversations_by_id:
    active_conversation_id = conversations[0].id
    st.session_state[active_conversation_key] = active_conversation_id
conversation = conversations_by_id[active_conversation_id]
pending_imports = st.session_state.setdefault("pending_imports", {})

if st.sidebar.button("New chat", width="stretch"):
    is_pristine = (
        conversation.title == NEW_CHAT_TITLE
        and not db.list_messages(conversation.id, limit=1)
        and not db.list_conversation_papers(conversation.id)
        and not pending_imports.get(conversation.id)
    )
    if not is_pristine:
        conversation = db.create_conversation(expert.id)
        st.session_state[active_conversation_key] = conversation.id
    st.rerun()

st.sidebar.caption("Chats")
chat_action = None
for saved_conversation in conversations:
    safe_id = saved_conversation.id.replace("-", "_")
    with st.sidebar.container(key=f"chat_row_{safe_id}"):
        title_col, menu_col = st.columns([8, 1], gap="small", vertical_alignment="center")
        if title_col.button(
            saved_conversation.title,
            key=f"chat_select_{safe_id}",
            type="secondary" if saved_conversation.id == conversation.id else "tertiary",
            width="stretch",
        ):
            st.session_state[active_conversation_key] = saved_conversation.id
            st.rerun()
        action = menu_col.menu_button(
            ":material/more_horiz:",
            ["Rename", "Delete"],
            key=f"chat_menu_{safe_id}",
            help=f"Options for {saved_conversation.title}",
            type="tertiary",
        )
        if action:
            chat_action = (action, saved_conversation)

counts = db.paper_counts(expert.id)
category = CATEGORY_BY_CODE[expert.category]
last_sync = (
    expert.last_sync_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if expert.last_sync_at
    else "Not yet"
)

with st.sidebar.popover(
    "Expert settings",
    icon=":material/settings:",
    type="tertiary",
    width="stretch",
    key=f"expert_settings_{expert.id}",
):
    st.caption(category.label)
    st.metric("Ready papers", counts["ready"])
    st.caption(f"Last sync: {last_sync}")
    if st.button("Sync now", key=f"sync_{expert.id}", width="stretch"):
        arxiv = ArxivClient()
        try:
            with st.spinner("Finding and indexing new papers..."):
                result = SyncService(db, arxiv, backend).sync_expert(expert)
            if result.skipped:
                st.info("A background sync is already running.")
            else:
                st.success(f"{result.ingested} indexed, {result.failed} failed")
            st.rerun()
        except Exception as exc:
            st.error(f"Sync failed: {exc}")
        finally:
            arxiv.close()
    st.divider()
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

if chat_action:
    action, action_conversation = chat_action
    st.session_state[CHAT_DIALOG_KEY] = {
        "action": action,
        "conversation_id": action_conversation.id,
        "expert_id": expert.id,
    }

dialog_state = st.session_state.get(CHAT_DIALOG_KEY)
if dialog_state and dialog_state["expert_id"] == expert.id:
    target_conversation = db.get_conversation(dialog_state["conversation_id"])
    if not target_conversation:
        st.session_state.pop(CHAT_DIALOG_KEY, None)
    elif dialog_state["action"] == "Rename":
        _rename_chat_dialog(db, target_conversation)
    else:
        _delete_chat_dialog(
            db, expert, target_conversation, active_conversation_key, pending_imports
        )
elif dialog_state:
    st.session_state.pop(CHAT_DIALOG_KEY, None)

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
