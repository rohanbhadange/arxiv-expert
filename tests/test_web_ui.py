from datetime import timedelta
from pathlib import Path

from streamlit.testing.v1 import AppTest
from test_database import sample_paper

from arxiv_expert import arxiv as arxiv_module
from arxiv_expert import openai_service as openai_module
from arxiv_expert.config import Settings, save_settings
from arxiv_expert.database import Database
from arxiv_expert.models import ChatAnswer, LibraryHit


def test_configured_user_sees_expert_chat_and_library(monkeypatch, paths, created_at):
    monkeypatch.setenv("ARXIV_EXPERT_HOME", str(paths.config_dir.parent))
    save_settings(Settings.create("sk-test"), paths)
    db = Database(paths.database_file)
    db.initialize()
    expert = db.create_expert("Robotics", "cs.RO", "vs_ui", created_at=created_at)
    paper, _ = db.add_paper(expert.id, sample_paper(created_at))
    db.set_paper_status(paper.id, "ready", openai_file_id="file_ui")
    script = Path(__file__).parents[1] / "src" / "arxiv_expert" / "web.py"

    app = AppTest.from_file(str(script)).run(timeout=15)

    assert not app.exception
    assert "Robotics" in [item.value for item in app.title]
    assert [tab.label for tab in app.tabs] == ["Chat", "Library"]
    assert not any(item.label == "Conversation" for item in app.selectbox)
    assert any(
        item.key == _chat_key("chat_select", db.list_conversations(expert.id)[0].id)
        for item in app.button
    )
    assert any(
        item.key == _chat_key("chat_menu", db.list_conversations(expert.id)[0].id)
        for item in app.menu_button
    )
    assert not any(
        item.label in {"Rename conversation", "Delete conversation"} for item in app.expander
    )
    assert any("No focus yet" in item.value for item in app.caption)
    assert any(item.label == "Focus in this chat" for item in app.button)


def _element(elements, label):
    return next(item for item in elements if item.label == label)


def _keyed_button(app, key):
    return next(item for item in app.button if item.key == key)


def _chat_key(prefix, conversation_id):
    return f"{prefix}_{conversation_id.replace('-', '_')}"


def _chat_button(app, conversation_id):
    return _keyed_button(app, _chat_key("chat_select", conversation_id))


def _chat_menu(app, conversation_id):
    return next(
        item for item in app.menu_button if item.key == _chat_key("chat_menu", conversation_id)
    )


def _active_conversation_id(app, expert_id):
    return app.session_state.filtered_state[f"active_conversation_id_{expert_id}"]


def test_new_rename_delete_conversation_flow_is_persistent(monkeypatch, paths, created_at):
    monkeypatch.setenv("ARXIV_EXPERT_HOME", str(paths.config_dir.parent))
    save_settings(Settings.create("sk-test"), paths)
    db = Database(paths.database_file)
    db.initialize()
    expert = db.create_expert("Robotics", "cs.RO", "vs_chats", created_at=created_at)
    previous = db.list_conversations(expert.id)[0]
    db.rename_conversation(previous.id, "Original chat")
    db.add_message(expert.id, previous.id, "user", "Existing question", "Analyze")
    db.add_message(expert.id, previous.id, "assistant", "Existing answer", "Analyze")
    script = Path(__file__).parents[1] / "src" / "arxiv_expert" / "web.py"
    app = AppTest.from_file(str(script)).run(timeout=15)

    _element(app.button, "New chat").click().run(timeout=15)
    assert len(db.list_conversations(expert.id)) == 2
    selected_id = _active_conversation_id(app, expert.id)
    assert selected_id == db.list_conversations(expert.id)[0].id

    _chat_menu(app, selected_id).click("Rename").run(timeout=15)
    _element(app.text_input, "Chat name").set_value("Locomotion ablations").run(timeout=15)
    _element(app.button, "Save").click().run(timeout=15)
    assert db.get_conversation(selected_id).title == "Locomotion ablations"
    assert _active_conversation_id(app, expert.id) == selected_id

    app = AppTest.from_file(str(script)).run(timeout=15)
    assert _active_conversation_id(app, expert.id) == selected_id
    _chat_menu(app, selected_id).click("Delete").run(timeout=15)
    _element(app.button, "Delete").click().run(timeout=15)
    assert db.get_conversation(selected_id) is None
    assert len(db.list_conversations(expert.id)) == 1
    assert _active_conversation_id(app, expert.id) == previous.id
    assert not app.exception


def test_new_chat_first_prompt_never_falls_back_to_previous_chat(monkeypatch, paths, created_at):
    class FakeOpenAIService:
        def __init__(self, settings):
            self.settings = settings

        def search_library(self, expert, query, arxiv_ids=None):
            return []

        def answer(self, **kwargs):
            return ChatAnswer(
                f"Answer to {kwargs['query']}",
                tuple(kwargs["library_citations"]),
                kwargs["use_web"],
            )

    monkeypatch.setattr(openai_module, "OpenAIService", FakeOpenAIService)
    monkeypatch.setenv("ARXIV_EXPERT_HOME", str(paths.config_dir.parent))
    save_settings(Settings.create("sk-test"), paths)
    db = Database(paths.database_file)
    db.initialize()
    expert = db.create_expert("Robotics", "cs.RO", "vs_regression", created_at=created_at)
    previous = db.list_conversations(expert.id)[0]
    empty_one = db.create_conversation(expert.id)
    empty_two = db.create_conversation(expert.id)
    db.rename_conversation(previous.id, "Original chat")
    db.add_message(expert.id, previous.id, "user", "Old question", "Analyze")
    db.add_message(expert.id, previous.id, "assistant", "Old answer", "Analyze")
    script = Path(__file__).parents[1] / "src" / "arxiv_expert" / "web.py"
    app = AppTest.from_file(str(script)).run(timeout=15)

    assert _active_conversation_id(app, expert.id) == previous.id
    _element(app.button, "New chat").click().run(timeout=15)
    selected_id = _active_conversation_id(app, expert.id)
    assert selected_id not in {previous.id, empty_one.id, empty_two.id}

    duplicate_buttons = [
        _chat_button(app, conversation_id)
        for conversation_id in (selected_id, empty_one.id, empty_two.id)
    ]
    assert {item.label for item in duplicate_buttons} == {"New chat"}

    app.chat_input[0].set_value("A question for the new chat").run(timeout=15)

    assert [item.content for item in db.list_messages(previous.id)] == [
        "Old question",
        "Old answer",
    ]
    assert [item.content for item in db.list_messages(selected_id)] == [
        "A question for the new chat",
        "Answer to A question for the new chat",
    ]
    assert db.list_messages(empty_one.id) == []
    assert db.list_messages(empty_two.id) == []
    assert _active_conversation_id(app, expert.id) == selected_id
    active_button = _chat_button(app, selected_id)
    assert active_button.label == "A question for the new chat"

    _chat_button(app, previous.id).click().run(timeout=15)
    assert _active_conversation_id(app, expert.id) == previous.id
    app.chat_input[0].set_value("A follow-up for the old chat").run(timeout=15)
    assert [item.content for item in db.list_messages(previous.id)][-2:] == [
        "A follow-up for the old chat",
        "Answer to A follow-up for the old chat",
    ]
    assert len(db.list_messages(selected_id)) == 2

    _chat_button(app, selected_id).click().run(timeout=15)
    assert _active_conversation_id(app, expert.id) == selected_id
    app.chat_input[0].set_value("A second new-chat question").run(timeout=15)
    assert [item.content for item in db.list_messages(selected_id)][-2:] == [
        "A second new-chat question",
        "Answer to A second new-chat question",
    ]
    assert len(db.list_messages(previous.id)) == 4
    assert not app.exception


def test_switching_experts_replaces_the_sidebar_chat_workspace(monkeypatch, paths, created_at):
    monkeypatch.setenv("ARXIV_EXPERT_HOME", str(paths.config_dir.parent))
    save_settings(Settings.create("sk-test"), paths)
    db = Database(paths.database_file)
    db.initialize()
    robotics = db.create_expert("Robotics", "cs.RO", "vs_robotics", created_at=created_at)
    learning = db.create_expert(
        "Machine Learning",
        "cs.LG",
        "vs_learning",
        created_at=created_at + timedelta(minutes=1),
    )
    robotics_chat = db.list_conversations(robotics.id)[0]
    learning_chat = db.list_conversations(learning.id)[0]
    db.rename_conversation(robotics_chat.id, "Robot policies")
    db.rename_conversation(learning_chat.id, "Learning theory")
    script = Path(__file__).parents[1] / "src" / "arxiv_expert" / "web.py"

    app = AppTest.from_file(str(script)).run(timeout=15)

    assert "Robotics" in [item.value for item in app.title]
    assert _chat_button(app, robotics_chat.id).label == "Robot policies"
    assert not any(item.key == _chat_key("chat_select", learning_chat.id) for item in app.button)
    _keyed_button(app, f"switch_expert_{learning.id}").click().run(timeout=15)

    assert "Machine Learning" in [item.value for item in app.title]
    assert _chat_button(app, learning_chat.id).label == "Learning theory"
    assert not any(item.key == _chat_key("chat_select", robotics_chat.id) for item in app.button)
    assert _active_conversation_id(app, learning.id) == learning_chat.id
    assert not app.exception


def test_new_chat_reuses_pristine_chat_but_preserves_configured_drafts(
    monkeypatch, paths, created_at
):
    monkeypatch.setenv("ARXIV_EXPERT_HOME", str(paths.config_dir.parent))
    save_settings(Settings.create("sk-test"), paths)
    db = Database(paths.database_file)
    db.initialize()
    expert = db.create_expert("Robotics", "cs.RO", "vs_pristine", created_at=created_at)
    pristine = db.list_conversations(expert.id)[0]
    script = Path(__file__).parents[1] / "src" / "arxiv_expert" / "web.py"
    app = AppTest.from_file(str(script)).run(timeout=15)

    _element(app.button, "New chat").click().run(timeout=15)
    _element(app.button, "New chat").click().run(timeout=15)
    assert [item.id for item in db.list_conversations(expert.id)] == [pristine.id]
    assert _active_conversation_id(app, expert.id) == pristine.id

    db.rename_conversation(pristine.id, "Named draft")
    app.run(timeout=15)
    _element(app.button, "New chat").click().run(timeout=15)
    focused_draft = db.get_conversation(_active_conversation_id(app, expert.id))
    assert len(db.list_conversations(expert.id)) == 2

    paper, _ = db.add_paper(expert.id, sample_paper(created_at))
    db.focus_paper(focused_draft.id, paper.id)
    app.run(timeout=15)
    _element(app.button, "New chat").click().run(timeout=15)

    assert len(db.list_conversations(expert.id)) == 3
    assert db.get_conversation(pristine.id).title == "Named draft"
    assert db.list_conversation_papers(focused_draft.id)[0].id == paper.id
    pending_draft_id = _active_conversation_id(app, expert.id)
    assert pending_draft_id not in {pristine.id, focused_draft.id}

    app.session_state["pending_imports"] = {pending_draft_id: ("pending",)}
    _element(app.button, "New chat").click().run(timeout=15)
    assert len(db.list_conversations(expert.id)) == 4
    assert _active_conversation_id(app, expert.id) not in {
        pristine.id,
        focused_draft.id,
        pending_draft_id,
    }
    assert not app.exception


def test_library_focus_flow_survives_a_fresh_browser_session(monkeypatch, paths, created_at):
    monkeypatch.setenv("ARXIV_EXPERT_HOME", str(paths.config_dir.parent))
    save_settings(Settings.create("sk-test"), paths)
    db = Database(paths.database_file)
    db.initialize()
    expert = db.create_expert("Robotics", "cs.RO", "vs_focus_ui", created_at=created_at)
    paper, _ = db.add_paper(expert.id, sample_paper(created_at), origin="manual")
    db.set_paper_status(paper.id, "ready", openai_file_id="file_focus_ui")
    conversation = db.list_conversations(expert.id)[0]
    script = Path(__file__).parents[1] / "src" / "arxiv_expert" / "web.py"

    app = AppTest.from_file(str(script)).run(timeout=15)
    _element(app.button, "Focus in this chat").click().run(timeout=15)

    assert db.list_conversation_papers(conversation.id)[0].id == paper.id
    assert any("**F1**" in item.value for item in app.markdown)
    assert any("Manual" in str(item.value) for item in app.dataframe)

    reopened = AppTest.from_file(str(script)).run(timeout=15)
    assert not reopened.exception
    assert any("**F1**" in item.value for item in reopened.markdown)


def test_chat_input_import_then_question_flow_uses_focused_paper(monkeypatch, paths, created_at):
    candidate = sample_paper(created_at)

    class FakeArxivClient:
        def paper_by_id(self, value):
            return candidate if value == candidate.arxiv_id else None

        def download_pdf(self, url, target):
            target.write_bytes(b"%PDF-1.4 fake")

        def close(self):
            pass

    class FakeOpenAIService:
        def __init__(self, settings):
            self.settings = settings

        def upload_pdf(self, expert, paper, pdf_path):
            return "file_imported_ui"

        def search_library(self, expert, query, arxiv_ids=None):
            assert arxiv_ids == [candidate.arxiv_id]
            return [LibraryHit("file_imported_ui", "paper.pdf", 0.9, "Policy evidence")]

        def answer(self, **kwargs):
            return ChatAnswer(
                "The paper uses a learned policy. [F1]",
                tuple(kwargs["library_citations"]),
                kwargs["use_web"],
            )

    monkeypatch.setattr(arxiv_module, "ArxivClient", FakeArxivClient)
    monkeypatch.setattr(openai_module, "OpenAIService", FakeOpenAIService)
    monkeypatch.setenv("ARXIV_EXPERT_HOME", str(paths.config_dir.parent))
    save_settings(Settings.create("sk-test"), paths)
    db = Database(paths.database_file)
    db.initialize()
    expert = db.create_expert("Robotics", "cs.RO", "vs_import_ui", created_at=created_at)
    conversation = db.list_conversations(expert.id)[0]
    script = Path(__file__).parents[1] / "src" / "arxiv_expert" / "web.py"
    app = AppTest.from_file(str(script)).run(timeout=15)

    app.chat_input[0].set_value(f"Import arXiv:{candidate.arxiv_id}").run(timeout=15)
    imported = db.find_paper(expert.id, candidate.arxiv_id)
    assert imported.status == "ready" and imported.origin == "manual"
    assert db.list_conversation_papers(conversation.id)[0].id == imported.id
    assert any("Imported and focused" in item.value for item in app.markdown)

    app.chat_input[0].set_value("What policy does it use?").run(timeout=15)
    messages = db.list_messages(conversation.id)
    assert messages[-1].content == "The paper uses a learned policy. [F1]"
    assert messages[-1].citations[0].marker == "F1"
    assert not app.exception
