from test_database import sample_paper

from arxiv_expert.chat import ChatService
from arxiv_expert.models import ChatAnswer, Citation, LibraryHit


class FakeChatBackend:
    def __init__(self, hits):
        self.hits = hits
        self.requests = []

    def search_library(self, expert, query):
        return self.hits

    def answer(self, **kwargs):
        self.requests.append(kwargs)
        citations = tuple(kwargs["library_citations"])
        if kwargs["use_web"]:
            citations += (
                Citation("web", "External paper", "https://arxiv.org/abs/2608.99999", "W1"),
            )
        return ChatAnswer("Grounded answer.", citations, kwargs["use_web"])


def ready_expert(db, created_at):
    expert = db.create_expert("Robotics", "cs.RO", "vs_chat", created_at=created_at)
    paper, _ = db.add_paper(expert.id, sample_paper(created_at))
    db.set_paper_status(paper.id, "ready", openai_file_id="file_1")
    return expert


def test_library_answer_uses_persistent_citation_without_web(db, created_at):
    expert = ready_expert(db, created_at)
    backend = FakeChatBackend([LibraryHit("file_1", "paper.pdf", 0.9, "Evidence text")])
    answer = ChatService(db, backend).ask(expert, "What method does this work use?", "Analyze")
    assert answer.used_web is False
    assert answer.citations[0].kind == "library"
    assert backend.requests[0]["library_context"].startswith("[L1] Learning to Move")
    assert len(db.list_messages(expert.id)) == 2


def test_unknown_arxiv_id_forces_web_even_with_library_hits(db, created_at):
    expert = ready_expert(db, created_at)
    backend = FakeChatBackend([LibraryHit("file_1", "paper.pdf", 0.8, "Related evidence")])
    answer = ChatService(db, backend).ask(expert, "What about arXiv:2608.99999?", "Analyze")
    assert answer.used_web is True
    assert {item.kind for item in answer.citations} == {"library", "web"}


def test_empty_expert_uses_web(db, created_at):
    expert = db.create_expert("New Robotics", "cs.RO", "vs_empty", created_at=created_at)
    backend = FakeChatBackend([])
    answer = ChatService(db, backend).ask(expert, "What is happening in robotics?", "Analyze")
    assert answer.used_web is True
