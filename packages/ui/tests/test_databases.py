"""Connecting a database server from the page."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from dataindex_ui.app import UISettings, create_app
from dataindex_ui.worker import ClientWorker
from ui_helpers import PORT, TOKEN, signed_in

SECRET = "s3cret-Hunter2"
URL = "postgresql+psycopg://reader@db.example.com:5432/sales"
CONFIG: dict[str, Any] = {
    "allowed_columns": {"orders": ["region", "total"]},
    "queries": [
        {
            "query_id": "orders.total_by_region",
            "table": "orders",
            "columns": [{"table": "orders", "column": "region"}],
            "filters": [
                {
                    "name": "region",
                    "column": {"table": "orders", "column": "region"},
                    "operator": "eq",
                }
            ],
            "aggregate": {
                "function": "sum",
                "column": {"table": "orders", "column": "total"},
                "alias": "revenue",
            },
            "group_by": [{"table": "orders", "column": "region"}],
        }
    ],
}
SERVER = {
    "doc_id": "db-0123456789abcdef",
    "name": "sales",
    "status": "completed",
    "change": "created",
    "node_count": 4,
    "path": None,
    "connection": {"engine": "postgresql", "url": URL, "password_env": "SALES_PASSWORD"},
}


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def submit_database(self, path: object = None, **arguments: Any) -> dict[str, Any]:
        self.calls.append({"path": path, **arguments})
        return dict(SERVER)

    def list_documents(self, limit: int = 100) -> dict[str, Any]:
        return {"documents": [dict(SERVER, path="C:/secret/place")], "total": 1}

    def close(self) -> None:
        pass


def test_the_connect_form_reaches_the_client_and_the_listing(storage: Path) -> None:
    fake = FakeClient()
    worker = ClientWorker(lambda: fake)  # type: ignore[arg-type, return-value]
    settings = UISettings(
        storage_path=storage, port=PORT, model_name=None, token=TOKEN, heartbeat_seconds=2.0
    )
    with signed_in(create_app(settings, worker)) as client:
        response = client.post(
            "/api/databases",
            json={
                "url": URL,
                "password_env": "SALES_PASSWORD",
                "schema": "public",
                "name": "sales",
                **CONFIG,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["document"]["doc_id"] == SERVER["doc_id"]
        listed = client.get("/api/documents").json()["documents"]

    assert fake.calls == [
        {
            "path": None,
            "url": URL,
            "password_env": "SALES_PASSWORD",
            "schema": "public",
            "name": "sales",
            **CONFIG,
        }
    ]
    assert listed == [{key: value for key, value in SERVER.items() if key != "path"}]


def test_the_connect_form_is_checked_and_never_echoes_a_secret(ui: TestClient) -> None:
    def connect(body: dict[str, Any], **headers: str) -> Any:
        return ui.post("/api/databases", json=body, headers=headers)

    in_url = connect({**CONFIG, "url": f"postgresql+psycopg://reader:{SECRET}@db/sales"})
    assert in_url.status_code == 400
    assert "password" in in_url.json()["detail"] and SECRET not in in_url.text

    extra = connect({**CONFIG, "url": URL, "password": SECRET})
    assert extra.status_code == 422 and SECRET not in extra.text
    assert extra.json()["detail"][0]["loc"] == ["body", "password"]

    too_long = connect({**CONFIG, "url": f"{URL}?x={SECRET}{'a' * 2048}"})
    assert too_long.status_code == 422 and SECRET not in too_long.text

    assert connect({"url": URL, "queries": CONFIG["queries"]}).status_code == 422
    assert connect({**CONFIG, "url": URL, "allowed_columns": {}}).status_code == 422
    assert connect({**CONFIG, "url": URL, "queries": []}).status_code == 422
    assert connect({**CONFIG, "url": URL, "allowed_columns": {"t": "c"}}).status_code == 422
    assert connect({**CONFIG, "url": URL, "password_env": ""}).status_code == 422
    assert connect({**CONFIG, "url": URL}, Origin="http://evil.example").status_code == 403
    assert ui.get("/api/documents").json()["documents"] == []
