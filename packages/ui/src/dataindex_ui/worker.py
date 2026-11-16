"""Run every ``DataIndexClient`` call on one dedicated thread.

``DataIndexClient`` is not thread-safe and its SQLite connections belong to the
thread that opened them, so the client is created, used and closed on a single
worker thread. Requests that need it wait their turn; a long answer therefore
delays document listing or uploads, but never the saved chat history.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable
from typing import TypeVar

from dataindex import DataIndexClient

T = TypeVar("T")


class ClientWorker:
    def __init__(self, factory: Callable[[], DataIndexClient]) -> None:
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="dataindex-ui-client"
        )
        self._client: DataIndexClient | None = None
        try:
            self._executor.submit(self._open, factory).result()
        except BaseException:
            self._executor.shutdown(wait=False)
            raise

    def _open(self, factory: Callable[[], DataIndexClient]) -> None:
        self._client = factory()

    def _with_client(self, function: Callable[[DataIndexClient], T]) -> T:
        if self._client is None:
            raise RuntimeError("the DataIndex client is closed")
        return function(self._client)

    def submit(
        self, function: Callable[[DataIndexClient], T]
    ) -> concurrent.futures.Future[T]:
        """Queue ``function(client)``; it keeps running if the caller goes away."""
        return self._executor.submit(self._with_client, function)

    async def call(self, function: Callable[[DataIndexClient], T]) -> T:
        return await asyncio.wrap_future(self.submit(function))

    def close(self) -> None:
        def close_client() -> None:
            client, self._client = self._client, None
            if client is not None:
                client.close()

        try:
            self._executor.submit(close_client).result()
        finally:
            self._executor.shutdown(wait=True)
