"""The server answers only its own page on this computer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from dataindex import local
from fastapi.testclient import TestClient

from ui_helpers import ORIGIN, PORT, TOKEN, make_app, signed_in


def test_a_foreign_host_header_is_refused(storage: Path) -> None:
    # DNS rebinding: a hostile site resolves its own name to 127.0.0.1.
    app, _ = make_app(storage)
    with signed_in(app) as client:
        response = client.get("/api/status", headers={"Host": f"evil.example:{PORT}"})
        assert response.status_code == 400
        assert client.get("/api/status", headers={"Host": "127.0.0.1:9999"}).status_code == 400
        assert client.get("/api/status", headers={"Host": f"localhost:{PORT}"}).status_code == 200


def test_sign_in_needs_the_session_token(storage: Path) -> None:
    app, _ = make_app(storage)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        assert client.get("/api/conversations").status_code == 401
        assert client.get("/").status_code == 401
        assert client.get("/", params={"token": "wrong"}).status_code == 401

        response = client.get("/", params={"token": TOKEN})
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        cookie = response.headers["set-cookie"].lower()
        assert f"dataindex_ui_{PORT}=" in cookie
        assert "httponly" in cookie and "samesite=strict" in cookie

        page = client.get("/")
        assert page.status_code == 200
        assert '<script src="/static/app.js">' in page.text
        assert client.get("/api/conversations").status_code == 200


def test_security_headers_are_set(ui: TestClient) -> None:
    for path in ("/", "/api/status", "/static/app.js"):
        headers = ui.get(path).headers
        policy = headers["content-security-policy"]
        assert "script-src 'self'" in policy and "frame-ancestors 'none'" in policy
        assert "unsafe-inline" not in policy
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["cache-control"] == "no-store"
    assert ui.get("/static/../app.py").status_code == 404
    assert ui.get("/static/missing.js").status_code == 404
    assert ui.get("/docs").status_code == 404
    assert ui.get("/openapi.json").status_code == 404


def test_changes_must_come_from_the_page_origin(ui: TestClient) -> None:
    hostile = {"Origin": "http://evil.example"}
    assert ui.post("/api/conversations", json={}, headers=hostile).status_code == 403
    del ui.headers["Origin"]
    assert ui.post("/api/conversations", json={}).status_code == 403
    ui.headers["Origin"] = ORIGIN
    assert ui.post("/api/conversations", json={}).status_code == 201
    # Reading does not change data, so a missing Origin is fine for GET.
    del ui.headers["Origin"]
    assert ui.get("/api/conversations").status_code == 200


def test_conversation_routes(ui: TestClient) -> None:
    created = ui.post("/api/conversations", json={"title": "Payments"}).json()
    assert created["title"] == "Payments"
    renamed = ui.patch(f"/api/conversations/{created['id']}", json={"title": "Invoices"})
    assert renamed.json()["title"] == "Invoices"
    assert ui.patch(
        f"/api/conversations/{created['id']}", json={"title": "x", "extra": 1}
    ).status_code == 422
    listing = ui.get("/api/conversations").json()["conversations"]
    assert [item["title"] for item in listing] == ["Invoices"]
    assert ui.delete(f"/api/conversations/{created['id']}").status_code == 204
    assert ui.delete(f"/api/conversations/{created['id']}").status_code == 404
    assert ui.get(f"/api/conversations/{created['id']}").status_code == 404


def test_asking_without_a_model_is_refused_and_saves_nothing(ui: TestClient) -> None:
    status = ui.get("/api/status").json()
    assert status["chat_enabled"] is False
    conversation = ui.post("/api/conversations", json={}).json()
    response = ui.post(
        f"/api/conversations/{conversation['id']}/messages", json={"question": "Hello?"}
    )
    assert response.status_code == 409
    assert "--model" in response.json()["detail"]
    assert ui.get(f"/api/conversations/{conversation['id']}").json()["messages"] == []


def test_question_bounds_are_checked(storage: Path) -> None:
    from ui_helpers import ScriptedModel

    app, _ = make_app(storage, ScriptedModel())
    with signed_in(app) as client:
        conversation = client.post("/api/conversations", json={}).json()
        url = f"/api/conversations/{conversation['id']}/messages"
        assert client.post(url, json={"question": ""}).status_code == 422
        assert client.post(url, json={"question": "x" * 2049}).status_code == 422
        assert client.post(
            url, json={"question": "ok", "doc_ids": ["d"] * 65}
        ).status_code == 422
        assert client.post(
            "/api/conversations/c-missing/messages", json={"question": "ok"}
        ).status_code == 404


def test_uploads_are_validated(ui: TestClient, storage: Path) -> None:
    def upload(name: str, content: bytes) -> int:
        return ui.post(
            "/api/documents",
            params={"filename": name},
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        ).status_code

    assert upload("photo.png", b"\x89PNG\r\n\x1a\n") == 415
    assert upload("contracts.sqlite", b"SQLite") == 415
    assert upload(".md", b"# no stem") == 415
    assert upload(".notes.md", b"# hidden") == 400
    assert upload("..\\..\\_.md", b"# traversal") == 400
    assert upload("empty.md", b"") == 400
    assert upload("big.md", b"x" * (5 * 1024 * 1024 + 1)) == 413
    assert upload("big.csv", b"x" * (5 * 1024 * 1024 + 1)) == 413
    assert not (storage / "ui_uploads").exists() or not any(
        (storage / "ui_uploads").iterdir()
    )


def test_office_and_pdf_uploads_have_the_larger_size_limit(
    ui: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local, "MAX_SUBMITTED_BINARY_FILE_BYTES", 6 * 1024 * 1024)

    def upload(name: str, size: int) -> Any:
        return ui.post(
            "/api/documents",
            params={"filename": name},
            content=b"x" * size,
            headers={"Content-Type": "application/octet-stream"},
        )

    over = upload("scan.pdf", 6 * 1024 * 1024 + 1)
    assert over.status_code == 413
    assert "6 MiB" in over.json()["detail"]
    assert upload("deck.pptx", 5 * 1024 * 1024 + 1).status_code != 413
    assert ui.get("/api/status").json()["limits"]["upload_text_bytes"] == 5 * 1024 * 1024
