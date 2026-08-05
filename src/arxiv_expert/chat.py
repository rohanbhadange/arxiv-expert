"""Deterministic library-first chat routing."""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Sequence
from typing import Protocol

from .arxiv import ID_PATTERN
from .conversation_titles import derive_conversation_title
from .database import Database
from .models import ChatAnswer, Citation, Expert, LibraryHit, Message, Paper


class ChatBackend(Protocol):
    def search_library(
        self, expert: Expert, query: str, arxiv_ids: Sequence[str] | None = None
    ) -> list[LibraryHit]: ...

    def answer(
        self,
        *,
        expert: Expert,
        query: str,
        mode: str,
        history: Sequence[Message],
        library_context: str,
        library_citations: Sequence[Citation],
        use_web: bool,
        scope: str,
    ) -> ChatAnswer: ...


OUTSIDE_LIBRARY_PATTERN = re.compile(
    r"\b(search (?:the )?web|look online|outside (?:the )?library|other papers|"
    r"not (?:yet )?ingested|broader literature|latest research|find (?:a |the )?paper)\b",
    re.I,
)
FULL_LIBRARY_PATTERN = re.compile(
    r"\b(?:search|use|check|look through) (?:the )?(?:full|entire|global) library\b", re.I
)


class ChatService:
    def __init__(self, database: Database, backend: ChatBackend):
        self.database = database
        self.backend = backend

    def ask(self, expert: Expert, conversation_id: str, query: str, mode: str) -> ChatAnswer:
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("A chat message cannot be empty.")
        conversation = self.database.get_conversation(conversation_id)
        if not conversation or conversation.expert_id != expert.id:
            raise ValueError("The active conversation does not belong to this expert.")
        history = self.database.list_messages(conversation_id, limit=16)
        self.database.add_message(expert.id, conversation_id, "user", clean_query, mode)
        self.database.title_new_conversation(
            conversation_id, derive_conversation_title(clean_query)
        )
        focused = self.database.list_conversation_papers(conversation_id)
        bypass_focus = bool(FULL_LIBRARY_PATTERN.search(clean_query))
        explicit_web = bool(OUTSIDE_LIBRARY_PATTERN.search(clean_query)) or self._has_unknown_id(
            expert, clean_query
        )

        if focused and not bypass_focus:
            ready_ids = [paper.arxiv_id for paper in focused if paper.status == "ready"]
            try:
                hits = (
                    self.backend.search_library(expert, clean_query, ready_ids) if ready_ids else []
                )
            except Exception:
                hits = []
            context, citations = self._format_focused(focused, hits)
            use_web = explicit_web
            scope = "focused"
        else:
            try:
                hits = self.backend.search_library(expert, clean_query)
            except Exception:
                # An empty vector store can be searched differently across SDK/API
                # versions. Unfocused chat remains useful through web fallback.
                hits = []
            context, citations = self._format_global(expert, hits)
            use_web = not hits or explicit_web
            scope = "global"
        answer = self.backend.answer(
            expert=expert,
            query=clean_query,
            mode=mode,
            history=history,
            library_context=context,
            library_citations=citations,
            use_web=use_web,
            scope=scope,
        )
        self.database.add_message(
            expert.id,
            conversation_id,
            "assistant",
            answer.text,
            mode,
            answer.citations,
        )
        return answer

    def _has_unknown_id(self, expert: Expert, query: str) -> bool:
        for match in ID_PATTERN.finditer(query):
            if self.database.find_paper(expert.id, match.group(1)) is None:
                return True
        return False

    def _format_global(
        self, expert: Expert, hits: Sequence[LibraryHit]
    ) -> tuple[str, list[Citation]]:
        grouped: OrderedDict[str, list[LibraryHit]] = OrderedDict()
        for hit in hits:
            grouped.setdefault(hit.file_id, []).append(hit)
        blocks: list[str] = []
        citations: list[Citation] = []
        papers_by_file = {
            paper.openai_file_id: paper
            for paper in self.database.list_papers(expert.id, ("ready",))
            if paper.openai_file_id
        }
        for index, (file_id, file_hits) in enumerate(grouped.items(), start=1):
            paper = papers_by_file.get(file_id)
            title = paper.title if paper else file_hits[0].filename
            url = paper.abstract_url if paper else "https://arxiv.org"
            marker = f"L{index}"
            excerpts = "\n\n".join(hit.text for hit in file_hits)
            blocks.append(f"[{marker}] {title}\n{excerpts}")
            citations.append(Citation(kind="library", title=title, url=url, marker=marker))
        return "\n\n".join(blocks), citations

    @staticmethod
    def _format_focused(
        focused: Sequence[Paper], hits: Sequence[LibraryHit]
    ) -> tuple[str, list[Citation]]:
        hits_by_file: OrderedDict[str, list[LibraryHit]] = OrderedDict()
        for hit in hits:
            hits_by_file.setdefault(hit.file_id, []).append(hit)
        blocks: list[str] = []
        citations: list[Citation] = []
        for index, paper in enumerate(focused, start=1):
            marker = f"F{index}"
            authors = ", ".join(paper.authors)
            abstract = paper.summary[:2000]
            block = (
                f"[{marker}] {paper.title}\n"
                f"Authors: {authors}\n"
                f"arXiv: {paper.arxiv_id}\n"
                f"URL: {paper.abstract_url}\n"
                f"Index status: {paper.status}\n"
                f"Abstract: {abstract}"
            )
            retrieved = hits_by_file.get(paper.openai_file_id or "", [])
            if retrieved:
                excerpts = "\n\n".join(hit.text for hit in retrieved)
                block += f"\nRetrieved excerpts for [{marker}]:\n{excerpts}"
            blocks.append(block)
            citations.append(Citation("library", paper.title, paper.abstract_url, marker))
        return "\n\n".join(blocks), citations
