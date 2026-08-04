"""Composition helpers shared by the CLI and Streamlit UI."""

from __future__ import annotations

from .arxiv import ArxivClient
from .chat import ChatService
from .config import AppPaths, default_paths, load_settings
from .database import Database
from .openai_service import OpenAIService
from .sync import SyncService


def database(paths: AppPaths | None = None) -> Database:
    resolved = paths or default_paths()
    resolved.ensure()
    result = Database(resolved.database_file)
    result.initialize()
    return result


def openai_service(paths: AppPaths | None = None) -> OpenAIService:
    return OpenAIService(load_settings(paths))


def sync_service(paths: AppPaths | None = None) -> tuple[SyncService, ArxivClient]:
    resolved = paths or default_paths()
    arxiv = ArxivClient()
    service = SyncService(database(resolved), arxiv, openai_service(resolved))
    return service, arxiv


def chat_service(paths: AppPaths | None = None) -> ChatService:
    resolved = paths or default_paths()
    return ChatService(database(resolved), openai_service(resolved))
