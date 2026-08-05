from dataclasses import replace

from test_database import sample_paper

from arxiv_expert.chat import ChatService
from arxiv_expert.models import ChatAnswer, Citation, LibraryHit


class FakeChatBackend:
    def __init__(self, hits):
        self.hits = hits
        self.searches = []
        self.requests = []

    def search_library(self, expert, query, arxiv_ids=None):
        self.searches.append((query, arxiv_ids))
        return self.hits

    def answer(self, **kwargs):
        self.requests.append(kwargs)
        citations = tuple(kwargs["library_citations"])
        if kwargs["use_web"]:
            citations += (
                Citation("web", "External paper", "https://arxiv.org/abs/2608.99999", "W1"),
            )
        return ChatAnswer("Grounded answer.", citations, kwargs["use_web"])


def ready_expert(db, created_at, *, long_abstract=False):
    expert = db.create_expert("Robotics", "cs.RO", "vs_chat", created_at=created_at)
    candidate = sample_paper(created_at)
    if long_abstract:
        candidate = replace(candidate, summary="x" * 2500)
    paper, _ = db.add_paper(expert.id, candidate)
    db.set_paper_status(paper.id, "ready", openai_file_id="file_1")
    conversation = db.list_conversations(expert.id)[0]
    return expert, conversation, db.get_paper(paper.id)


def test_unfocused_answer_searches_global_library_and_persists(db, created_at):
    expert, conversation, _ = ready_expert(db, created_at)
    backend = FakeChatBackend([LibraryHit("file_1", "paper.pdf", 0.9, "Evidence text")])
    answer = ChatService(db, backend).ask(
        expert, conversation.id, "What method does this work use?", "Analyze"
    )
    assert answer.used_web is False
    assert answer.citations[0].marker == "L1"
    assert backend.searches == [("What method does this work use?", None)]
    assert backend.requests[0]["scope"] == "global"
    assert backend.requests[0]["library_context"].startswith("[L1] Learning to Move")
    assert len(db.list_messages(conversation.id)) == 2
    assert db.get_conversation(conversation.id).title == "What method does this work use"


def test_focused_answer_filters_retrieval_and_frontloads_stable_context(db, created_at):
    expert, conversation, paper = ready_expert(db, created_at, long_abstract=True)
    db.focus_paper(conversation.id, paper.id)
    backend = FakeChatBackend([LibraryHit("file_1", "paper.pdf", 0.9, "Full text evidence")])

    answer = ChatService(db, backend).ask(expert, conversation.id, "Compare the setup", "Analyze")

    request = backend.requests[0]
    assert backend.searches == [("Compare the setup", ["2608.00001"])]
    assert request["scope"] == "focused"
    assert request["use_web"] is False
    assert "[F1] Learning to Move" in request["library_context"]
    assert "Full text evidence" in request["library_context"]
    assert "x" * 2000 in request["library_context"]
    assert "x" * 2001 not in request["library_context"]
    assert answer.citations[0].marker == "F1"


def test_focused_chat_does_not_automatically_broaden_when_retrieval_is_empty(db, created_at):
    expert, conversation, paper = ready_expert(db, created_at)
    db.focus_paper(conversation.id, paper.id)
    backend = FakeChatBackend([])

    answer = ChatService(db, backend).ask(
        expert, conversation.id, "What evidence is missing?", "Analyze"
    )

    assert answer.used_web is False
    assert backend.requests[0]["scope"] == "focused"
    assert db.list_conversation_papers(conversation.id) == [paper]


def test_explicit_full_library_bypasses_focus_for_one_turn_without_changing_it(db, created_at):
    expert, conversation, paper = ready_expert(db, created_at)
    db.focus_paper(conversation.id, paper.id)
    backend = FakeChatBackend([LibraryHit("file_1", "paper.pdf", 0.8, "Related evidence")])

    ChatService(db, backend).ask(
        expert, conversation.id, "Search the full library for alternatives", "Analyze"
    )

    assert backend.searches[0][1] is None
    assert backend.requests[0]["scope"] == "global"
    assert db.list_conversation_papers(conversation.id) == [paper]


def test_unknown_id_or_explicit_web_request_allows_web_even_when_focused(db, created_at):
    expert, conversation, paper = ready_expert(db, created_at)
    db.focus_paper(conversation.id, paper.id)
    backend = FakeChatBackend([])
    answer = ChatService(db, backend).ask(
        expert, conversation.id, "What about arXiv:2608.99999?", "Analyze"
    )
    assert answer.used_web is True
    assert {item.kind for item in answer.citations} == {"library", "web"}


def test_empty_unfocused_expert_uses_web(db, created_at):
    expert = db.create_expert("New Robotics", "cs.RO", "vs_empty", created_at=created_at)
    conversation = db.list_conversations(expert.id)[0]
    backend = FakeChatBackend([])
    answer = ChatService(db, backend).ask(
        expert, conversation.id, "What is happening in robotics?", "Analyze"
    )
    assert answer.used_web is True
