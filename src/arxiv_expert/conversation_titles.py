"""Conversation title helpers shared by chat and persistence migrations."""

from __future__ import annotations


def derive_conversation_title(message: str) -> str:
    """Create a concise local title from the first meaningful message."""
    clean = " ".join(message.strip().split()).strip("?!.,:; ")
    if not clean:
        return "New chat"
    words = clean.split()
    title = " ".join(words[:8])
    if len(words) > 8:
        title += "…"
    return title[:80]
