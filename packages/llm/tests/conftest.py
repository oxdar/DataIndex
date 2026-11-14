from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from llm_fakes import FakeServer

# Proxy and credential variables must never leak from a developer machine into
# a test; the adapter reads them from the process environment by default.
_ISOLATED_VARIABLES = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ISOLATED_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def server_factory() -> Iterator[Callable[[], FakeServer]]:
    created: list[FakeServer] = []

    def make() -> FakeServer:
        fake = FakeServer()
        fake.start()
        created.append(fake)
        return fake

    yield make
    for fake in created:
        fake.close()


@pytest.fixture
def server(server_factory: Callable[[], FakeServer]) -> FakeServer:
    return server_factory()
