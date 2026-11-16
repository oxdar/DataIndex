"""End to end: upload a document, ask over SSE, and find the chat saved."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from ui_helpers import CLAIM, DOCUMENT, QUESTION, ScriptedModel, make_app, read_events, signed_in

pytest.importorskip("dataindex_ext_documents")


def _upload(client, name: str, content: str) -> dict:
    response = client.post(
        "/api/documents",
        params={"filename": name},
        content=content.encode("utf-8"),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_cited_answer_streams_and_is_saved_across_restarts(storage: Path) -> None:
    model = ScriptedModel()
    app, _ = make_app(storage, model)
    with signed_in(app) as client:
        uploaded = _upload(client, "..\\..\\terms.md", DOCUMENT)["document"]
        assert uploaded["status"] == "completed" and uploaded["name"] == "terms.md"
        assert (storage / "ui_uploads" / "terms.md").read_text(encoding="utf-8") == DOCUMENT

        documents = client.get("/api/documents").json()["documents"]
        assert [doc["doc_id"] for doc in documents] == [uploaded["doc_id"]]
        assert all("path" not in doc for doc in documents)

        conversation = client.post("/api/conversations", json={}).json()
        response = client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={"question": QUESTION, "doc_ids": [uploaded["doc_id"]]},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = read_events(response)

    names = [name for name, _ in events if name != "status"]
    assert names == ["message", "message", "done"]
    question, reply, done = (data for name, data in events if name != "status")
    assert question["role"] == "user" and question["content"] == QUESTION
    assert reply["role"] == "assistant"
    answer = reply["answer"]
    assert answer["abstained"] is False
    assert answer["claims"][0]["text"] == CLAIM
    citation = answer["citations"][0]
    assert citation["source_id"] == uploaded["doc_id"]
    assert citation["source_version"] == uploaded["version"]
    assert citation["operation"] and citation["locator"]["scheme"]
    assert "net 30 days" in citation["excerpt"]
    assert done["conversation"]["title"] == QUESTION
    assert done["conversation"]["message_count"] == 2
    # Local paths never reach the model.
    sent = "".join(message.content for request in model.requests for message in request)
    assert str(storage) not in sent and "ui_uploads" not in sent

    restarted, _ = make_app(storage, ScriptedModel())
    with signed_in(restarted) as client:
        listing = client.get("/api/conversations").json()["conversations"]
        assert [item["id"] for item in listing] == [conversation["id"]]
        saved = client.get(f"/api/conversations/{conversation['id']}").json()
    assert [message["role"] for message in saved["messages"]] == ["user", "assistant"]
    assert saved["messages"][0]["doc_ids"] == [uploaded["doc_id"]]
    assert saved["messages"][1]["answer"] == answer


def test_progress_events_and_errors_are_streamed_and_saved(storage: Path) -> None:
    app, _ = make_app(storage, ScriptedModel(delay=0.2, fail=True), heartbeat_seconds=0.02)
    with signed_in(app) as client:
        _upload(client, "terms.md", DOCUMENT)
        conversation = client.post("/api/conversations", json={}).json()
        events = read_events(
            client.post(
                f"/api/conversations/{conversation['id']}/messages",
                json={"question": QUESTION},
            )
        )
        saved = client.get(f"/api/conversations/{conversation['id']}").json()

    statuses = [data for name, data in events if name == "status"]
    assert statuses and all(item["state"] == "thinking" for item in statuses)
    reply = next(data for name, data in events if name == "message" and data["role"] != "user")
    assert reply["role"] == "error"
    assert "unavailable" in reply["content"]
    assert [message["role"] for message in saved["messages"]] == ["user", "error"]


def test_an_answer_is_saved_even_if_the_page_goes_away(storage: Path) -> None:
    gate = threading.Event()
    app, worker = make_app(storage, ScriptedModel(gate=gate), heartbeat_seconds=0.02)
    with signed_in(app) as client:
        _upload(client, "terms.md", DOCUMENT)
        conversation = client.post("/api/conversations", json={}).json()
        with client.stream(
            "POST",
            f"/api/conversations/{conversation['id']}/messages",
            json={"question": QUESTION},
        ) as response:
            first = next(response.iter_lines())
            assert first == "event: message"
        # The browser left before the model replied.
        gate.set()
        worker.submit(lambda _: None).result(timeout=10)
        saved = client.get(f"/api/conversations/{conversation['id']}").json()
    assert [message["role"] for message in saved["messages"]] == ["user", "assistant"]


def test_removing_a_document_deletes_only_the_uploaded_copy(
    storage: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(DOCUMENT, encoding="utf-8")
    app, worker = make_app(storage, ScriptedModel())
    with signed_in(app) as client:
        uploaded = _upload(client, "terms.md", DOCUMENT)["document"]
        external = worker.submit(lambda c: c.submit_document(outside)).result()
        assert client.delete(f"/api/documents/{uploaded['doc_id']}").status_code == 204
        assert client.delete(f"/api/documents/{external['doc_id']}").status_code == 204
        assert client.delete(f"/api/documents/{external['doc_id']}").status_code == 404
        assert client.get("/api/documents").json()["documents"] == []
    assert not (storage / "ui_uploads" / "terms.md").exists()
    assert outside.exists()


def test_a_failed_upload_leaves_no_file(storage: Path) -> None:
    app, _ = make_app(storage, ScriptedModel())
    with signed_in(app) as client:
        response = client.post(
            "/api/documents",
            params={"filename": "binary.md"},
            content=b"\xff\xfe\x00 not utf-8 \x00",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 400
        assert client.get("/api/documents").json()["documents"] == []
    assert not (storage / "ui_uploads" / "binary.md").exists()
