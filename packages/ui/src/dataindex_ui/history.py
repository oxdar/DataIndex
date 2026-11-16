"""Saved conversations for the local chat interface.

Conversations and their messages, including each cited answer, are kept in one
SQLite file next to the DataIndex workspace. Answers hold excerpts of your
documents, so protect the storage directory like the workspace itself. Each
call opens its own connection, which keeps the store safe to use from the web
server's threads while a question is being answered.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HISTORY_FILE_NAME = "ui_history.sqlite3"
SCHEMA_VERSION = 1
DEFAULT_TITLE = "New chat"
MAX_TITLE_CHARS = 200
AUTO_TITLE_CHARS = 80
MAX_CONVERSATIONS_LISTED = 500
MAX_MESSAGES_PER_CONVERSATION = 1_000
MAX_MESSAGE_CHARS = 64 * 1024
MAX_ANSWER_JSON_BYTES = 1024 * 1024
MAX_DOC_IDS = 64
ROLES = frozenset({"user", "assistant", "error"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'error')),
    content TEXT NOT NULL,
    doc_ids TEXT,
    answer TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_conversation
    ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS conversations_by_update
    ON conversations(updated_at DESC);
"""


class HistoryError(Exception):
    """The history file cannot be used, or a request breaks one of its limits."""


class ConversationNotFoundError(HistoryError):
    """No conversation has the requested ID."""


class ChatHistory:
    """Create, list, rename and delete conversations and append messages."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise HistoryError(
                    f"{self._path.name} was written by a newer dataindex-ui "
                    f"(schema {version}); upgrade dataindex-ui to read it"
                )
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _restrict_permissions(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def list_conversations(
        self, limit: int = MAX_CONVERSATIONS_LISTED
    ) -> list[dict[str, Any]]:
        """Conversations, most recently updated first."""
        if not 1 <= limit <= MAX_CONVERSATIONS_LISTED:
            raise ValueError(f"limit must be 1 to {MAX_CONVERSATIONS_LISTED}")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT c.id, c.title, c.created_at, c.updated_at, "
                "(SELECT COUNT(*) FROM messages AS m WHERE m.conversation_id = c.id) "
                "FROM conversations AS c ORDER BY c.updated_at DESC, c.rowid DESC "
                "LIMIT ?",
                (limit,),
            ).fetchall()
        return [_conversation(row) for row in rows]

    def create_conversation(self, title: str | None = None) -> dict[str, Any]:
        clean = DEFAULT_TITLE if title is None else _title(title)
        now = _now()
        conversation_id = f"c-{secrets.token_hex(8)}"
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, clean, now, now),
            )
        return {
            "id": conversation_id,
            "title": clean,
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
        }

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """One conversation with all of its messages, oldest first."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT c.id, c.title, c.created_at, c.updated_at, "
                "(SELECT COUNT(*) FROM messages AS m WHERE m.conversation_id = c.id) "
                "FROM conversations AS c WHERE c.id = ?",
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise ConversationNotFoundError(conversation_id)
            messages = connection.execute(
                "SELECT id, role, content, doc_ids, answer, created_at FROM messages "
                "WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        result = _conversation(row)
        result["messages"] = [_message(item) for item in messages]
        return result

    def rename_conversation(self, conversation_id: str, title: str) -> dict[str, Any]:
        clean = _title(title)
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE conversations SET title = ? WHERE id = ?",
                (clean, conversation_id),
            ).rowcount
        if not updated:
            raise ConversationNotFoundError(conversation_id)
        return self._summary(conversation_id)

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation and its messages; ``False`` if it did not exist."""
        with self._connect() as connection:
            return bool(
                connection.execute(
                    "DELETE FROM conversations WHERE id = ?", (conversation_id,)
                ).rowcount
            )

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        doc_ids: Sequence[str] | None = None,
        answer: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a message. The first question also names an untitled chat."""
        if role not in ROLES:
            raise ValueError(f"role must be one of {sorted(ROLES)}")
        if not isinstance(content, str) or len(content) > MAX_MESSAGE_CHARS:
            raise HistoryError(
                f"a message must be text of at most {MAX_MESSAGE_CHARS} characters"
            )
        doc_json = None
        if doc_ids is not None:
            if len(doc_ids) > MAX_DOC_IDS:
                raise HistoryError(f"at most {MAX_DOC_IDS} documents per question")
            doc_json = json.dumps([str(item) for item in doc_ids])
        answer_json = None
        if answer is not None:
            answer_json = json.dumps(answer, ensure_ascii=False, allow_nan=False)
            if len(answer_json.encode("utf-8")) > MAX_ANSWER_JSON_BYTES:
                raise HistoryError("the answer is too large to save")
        now = _now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT title, (SELECT COUNT(*) FROM messages WHERE conversation_id = ?) "
                "FROM conversations WHERE id = ?",
                (conversation_id, conversation_id),
            ).fetchone()
            if row is None:
                raise ConversationNotFoundError(conversation_id)
            title, count = row
            if count >= MAX_MESSAGES_PER_CONVERSATION:
                raise HistoryError(
                    f"this chat has reached {MAX_MESSAGES_PER_CONVERSATION} messages; "
                    "start a new chat"
                )
            message_id = connection.execute(
                "INSERT INTO messages "
                "(conversation_id, role, content, doc_ids, answer, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (conversation_id, role, content, doc_json, answer_json, now),
            ).lastrowid
            if role == "user" and title == DEFAULT_TITLE and count == 0:
                title = _auto_title(content)
            connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, conversation_id),
            )
        return {
            "id": message_id,
            "role": role,
            "content": content,
            "doc_ids": None if doc_json is None else json.loads(doc_json),
            "answer": None if answer_json is None else json.loads(answer_json),
            "created_at": now,
        }

    def _summary(self, conversation_id: str) -> dict[str, Any]:
        conversation = self.get_conversation(conversation_id)
        conversation.pop("messages")
        return conversation

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(self._path, timeout=10)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                with connection:
                    yield connection
        except sqlite3.DatabaseError as error:
            raise HistoryError(
                f"cannot use the chat history file {self._path.name}: "
                f"{type(error).__name__}"
            ) from error


def _conversation(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "id": row[0],
        "title": row[1],
        "created_at": row[2],
        "updated_at": row[3],
        "message_count": int(row[4]),
    }


def _message(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "id": row[0],
        "role": row[1],
        "content": row[2],
        "doc_ids": None if row[3] is None else json.loads(row[3]),
        "answer": None if row[4] is None else json.loads(row[4]),
        "created_at": row[5],
    }


def _title(title: object) -> str:
    if not isinstance(title, str):
        raise HistoryError("a title must be text")
    clean = " ".join(title.split())
    if not clean or len(clean) > MAX_TITLE_CHARS:
        raise HistoryError(f"a title must have 1 to {MAX_TITLE_CHARS} characters")
    return clean


def _auto_title(question: str) -> str:
    clean = " ".join(question.split())
    if len(clean) <= AUTO_TITLE_CHARS:
        return clean or DEFAULT_TITLE
    return clean[: AUTO_TITLE_CHARS - 1].rstrip() + "\u2026"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _restrict_permissions(path: Path) -> None:
    """Make the history file private to the user where the OS supports it."""
    if os.name == "posix":
        os.chmod(path, 0o600)
