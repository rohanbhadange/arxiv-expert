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
    assert any(item.label == "Conversation" for item in app.selectbox)
    assert any("No focus yet" in item.value for item in app.caption)
    assert any(item.label == "Focus in this chat" for item in app.button)


def _element(elements, label):
    return next(item for item in elements if item.label == label)


def test_new_rename_delete_conversation_flow_is_persistent(monkeypatch, paths, created_at):
    monkeypatch.setenv("ARXIV_EXPERT_HOME", str(paths.config_dir.parent))
    save_settings(Settings.create("sk-test"), paths)
    db = Database(paths.database_file)
    db.initialize()
    expert = db.create_expert("Robotics", "cs.RO", "vs_chats", created_at=created_at)
    script = Path(__file__).parents[1] / "src" / "arxiv_expert" / "web.py"
    app = AppTest.from_file(str(script)).run(timeout=15)

    _element(app.button, "New chat").click().run(timeout=15)
    assert len(db.list_conversations(expert.id)) == 2
    selected = _element(app.selectbox, "Conversation").value
    assert selected.id == db.list_conversations(expert.id)[0].id

    _element(app.text_input, "Conversation name").set_value("Locomotion ablations").run(timeout=15)
    _element(app.button, "Save name").click().run(timeout=15)
    assert db.get_conversation(selected.id).title == "Locomotion ablations"

    delete_checkbox = next(
        item for item in app.checkbox if item.label.startswith("Delete “Locomotion ablations")
    )
    delete_checkbox.set_value(True).run(timeout=15)
    _element(app.button, "Delete conversation").click().run(timeout=15)
    assert db.get_conversation(selected.id) is None
    assert len(db.list_conversations(expert.id)) == 1
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
