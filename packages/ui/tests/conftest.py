from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ui_helpers import make_app, signed_in


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "DATAINDEX_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def storage(tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture
def ui(storage: Path) -> Iterator[TestClient]:
    """A signed-in client for an app with no chat model."""
    app, _ = make_app(storage)
    with signed_in(app) as client:
        yield client
