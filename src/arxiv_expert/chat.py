"""Deterministic library-first chat routing."""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Sequence
from typing import Protocol

from .arxiv import ID_PATTERN
from .database import Database
from .models import ChatAnswer, Citation, Expert, LibraryHit, Message


class ChatBackend(Protocol):
    def search_library(self, expert: Expert, query: str) -> list[LibraryHit]: ...

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
    ) -> ChatAnswer: ...


OUTSIDE_LIBRARY_PATTERN = re.compile(
    r"\b(search (?:the )?web|look online|outside (?:the )?library|other papers|"
    r"not (?:yet )?ingested|broader literature|latest research|find (?:a |the )?paper)\b",
    re.I,
)


class ChatService:
    def __init__(self, database: Database, backend: ChatBackend):
        self.database = database
        self.backend = backend

    def ask(self, expert: Expert, query: str, mode: str) -> ChatAnswer:
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("A chat message cannot be empty.")
        history = self.database.list_messages(expert.id, limit=16)
        self.database.add_message(expert.id, "user", clean_query, mode)
        try:
            hits = self.backend.search_library(expert, clean_query)
        except Exception:
            # An empty vector store can be searched differently across SDK/API
            # versions. Chat remains useful through the explicit web fallback.
            hits = []
        context, citations = self._format_hits(expert, hits)
        use_web = (
            not hits
            or bool(OUTSIDE_LIBRARY_PATTERN.search(clean_query))
            or self._has_unknown_id(expert, clean_query)
        )
        answer = self.backend.answer(
            expert=expert,
            query=clean_query,
            mode=mode,
            history=history,
            library_context=context,
            library_citations=citations,
            use_web=use_web,
        )
        self.database.add_message(expert.id, "assistant", answer.text, mode, answer.citations)
        return answer

    def _has_unknown_id(self, expert: Expert, query: str) -> bool:
        for match in ID_PATTERN.finditer(query):
            if self.database.find_paper(expert.id, match.group(1)) is None:
                return True
        return False

    def _format_hits(
        self, expert: Expert, hits: Sequence[LibraryHit]
    ) -> tuple[str, list[Citation]]:
        grouped: OrderedDict[str, list[LibraryHit]] = OrderedDict()
        for hit in hits:
            grouped.setdefault(hit.file_id, []).append(hit)
        blocks: list[str] = []
        citations: list[Citation] = []
        for index, (file_id, file_hits) in enumerate(grouped.items(), start=1):
            paper = next(
                (
                    item
                    for item in self.database.list_papers(expert.id, ("ready",))
                    if item.openai_file_id == file_id
                ),
                None,
            )
            title = paper.title if paper else file_hits[0].filename
            url = paper.abstract_url if paper else "https://arxiv.org"
            marker = f"L{index}"
            excerpts = "\n\n".join(hit.text for hit in file_hits)
            blocks.append(f"[{marker}] {title}\n{excerpts}")
            citations.append(Citation(kind="library", title=title, url=url, marker=marker))
        return "\n\n".join(blocks), citations
