from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from dataindex_ui import history as history_module
from dataindex_ui.history import (
    DEFAULT_TITLE,
    ChatHistory,
    ConversationNotFoundError,
    HistoryError,
)

ANSWER = {
    "text": "Invoices are due net 30 days. [1]",
    "abstained": False,
    "citations": [
        {
            "number": 1,
            "source_id": "doc-1",
            "source_version": "sha256:abc",
            "locator": {"scheme": "markdown.heading", "version": "1", "value": {"line": 3}},
            "operation": "documents.search",
            "excerpt": "Invoices are due net 30 days after receipt.",
        }
    ],
}


def test_conversation_and_answer_survive_reopening(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    store = ChatHistory(path)
    first = store.create_conversation()
    second = store.create_conversation("Delivery questions")
    assert first["title"] == DEFAULT_TITLE

    store.add_message(first["id"], "user", "  What are the\npayment terms?  ", doc_ids=["doc-1"])
    store.add_message(first["id"], "assistant", ANSWER["text"], answer=ANSWER)
    store.add_message(second["id"], "user", "When do goods ship?")

    reopened = ChatHistory(path)
    conversation = reopened.get_conversation(first["id"])
    # The first question names an untitled chat; an explicit title is kept.
    assert conversation["title"] == "What are the payment terms?"
    assert reopened.get_conversation(second["id"])["title"] == "Delivery questions"
    assert [m["role"] for m in conversation["messages"]] == ["user", "assistant"]
    assert conversation["messages"][0]["doc_ids"] == ["doc-1"]
    assert conversation["messages"][1]["answer"] == ANSWER
    assert [item["id"] for item in reopened.list_conversations()] == [second["id"], first["id"]]


def test_rename_and_delete_cascades_to_messages(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    store = ChatHistory(path)
    conversation = store.create_conversation()
    store.add_message(conversation["id"], "user", "question")

    renamed = store.rename_conversation(conversation["id"], "  Contract   review ")
    assert renamed["title"] == "Contract review"
    assert renamed["message_count"] == 1

    assert store.delete_conversation(conversation["id"]) is True
    assert store.delete_conversation(conversation["id"]) is False
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    with pytest.raises(ConversationNotFoundError):
        store.get_conversation(conversation["id"])
    with pytest.raises(ConversationNotFoundError):
        store.add_message(conversation["id"], "user", "late question")


def test_limits_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ChatHistory(tmp_path / "history.sqlite3")
    conversation = store.create_conversation()
    with pytest.raises(HistoryError):
        store.rename_conversation(conversation["id"], "   ")
    with pytest.raises(HistoryError):
        store.rename_conversation(conversation["id"], "x" * 201)
    with pytest.raises(ValueError):
        store.add_message(conversation["id"], "system", "not a role")

    monkeypatch.setattr(history_module, "MAX_MESSAGES_PER_CONVERSATION", 2)
    store.add_message(conversation["id"], "user", "one")
    store.add_message(conversation["id"], "assistant", "two")
    with pytest.raises(HistoryError, match="start a new chat"):
        store.add_message(conversation["id"], "user", "three")


def test_a_newer_history_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    ChatHistory(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
    with pytest.raises(HistoryError, match="newer"):
        ChatHistory(path)
