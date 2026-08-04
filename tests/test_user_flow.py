from datetime import timedelta

from test_database import sample_paper
from test_sync import FakeArxiv, FakeIndexer

from arxiv_expert.chat import ChatService
from arxiv_expert.database import Database
from arxiv_expert.models import ChatAnswer, Citation, LibraryHit
from arxiv_expert.sync import SyncService


class FlowChatBackend:
    def __init__(self, file_id):
        self.file_id = file_id
        self.web_calls = 0

    def search_library(self, expert, query, arxiv_ids=None):
        if "not-yet-known" in query:
            return []
        return [
            LibraryHit(
                self.file_id, "2608.00001v1.pdf", 0.92, "The method uses an adaptive policy."
            )
        ]

    def answer(self, **kwargs):
        if kwargs["use_web"]:
            self.web_calls += 1
            return ChatAnswer(
                "A newer external paper takes a different approach.",
                (Citation("web", "New paper", "https://arxiv.org/abs/2608.99999", "W1"),),
                True,
            )
        return ChatAnswer("Use an adaptive policy. [L1]", tuple(kwargs["library_citations"]), False)


def test_complete_create_sync_restart_chat_and_web_fallback_flow(db, paths, created_at):
    expert = db.create_expert("Robotics", "cs.RO", "vs_flow", created_at=created_at)
    sync = SyncService(db, FakeArxiv([sample_paper(created_at)]), FakeIndexer())
    result = sync.sync_expert(expert, now=created_at + timedelta(hours=1))
    assert result.ingested == 1

    reopened = Database(paths.database_file)
    reopened.initialize()
    restored_expert = reopened.get_expert(expert.id)
    ready_paper = reopened.list_papers(expert.id)[0]
    backend = FlowChatBackend(ready_paper.openai_file_id)
    chat = ChatService(reopened, backend)
    first_chat = reopened.list_conversations(expert.id)[0]
    second_chat = reopened.create_conversation(expert.id)
    reopened.focus_paper(first_chat.id, ready_paper.id)
    library_answer = chat.ask(
        restored_expert,
        first_chat.id,
        "Design a control experiment",
        "Experiment Designer",
    )
    web_answer = chat.ask(restored_expert, second_chat.id, "Find not-yet-known work", "Analyze")

    assert library_answer.citations[0].url == ready_paper.abstract_url
    assert web_answer.used_web and backend.web_calls == 1
    assert len(reopened.list_messages(first_chat.id)) == 2
    assert len(reopened.list_messages(second_chat.id)) == 2
    assert len(reopened.list_conversation_papers(first_chat.id)) == 1
    assert reopened.list_conversation_papers(second_chat.id) == []
