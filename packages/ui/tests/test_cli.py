from __future__ import annotations

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from dataindex_ui.cli import main


def test_only_loopback_addresses_are_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--host", "0.0.0.0", "--no-browser"])
    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_a_busy_port_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        code = main(["--port", str(port), "--no-browser", "--storage", str(tmp_path)])
    assert code == 2
    assert f"cannot listen on 127.0.0.1 port {port}" in capsys.readouterr().err


def test_a_model_that_cannot_be_configured_stops_start_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A remote OpenAI-compatible model needs OPENAI_API_KEY, which is unset here.
    code = main(["--port", "0", "--no-browser", "--storage", str(tmp_path), "--model", "gpt-4o"])
    assert code == 2
    error = capsys.readouterr().err
    assert error.startswith("dataindex-ui: ")


def test_the_server_starts_and_signs_in_with_the_printed_link(tmp_path: Path) -> None:
    environment = {**os.environ, "PYTHONUNBUFFERED": "1"}
    environment.pop("DATAINDEX_MODEL", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "dataindex_ui", "--port", "0", "--no-browser",
         "--storage", str(tmp_path / "store")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        assert process.stdout is not None
        url = ""
        for _ in range(5):
            line = process.stdout.readline().strip()
            if line.startswith("http://127.0.0.1:"):
                url = line
                break
        assert url, "the start link was not printed"
        base = url.split("/?", 1)[0]
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        page = _wait_for(opener, url)
        assert "DataIndex chat" in page
        with opener.open(f"{base}/api/status", timeout=10) as response:
            status = json.load(response)
        assert status["chat_enabled"] is False
        assert (tmp_path / "store" / "ui_history.sqlite3").exists()
    finally:
        process.terminate()
        process.communicate(timeout=10)


def _wait_for(opener: urllib.request.OpenerDirector, url: str) -> str:
    last: Exception | None = None
    for _ in range(50):
        try:
            with opener.open(url, timeout=10) as response:
                return response.read().decode("utf-8")
        except OSError as error:
            last = error
            time.sleep(0.1)
    raise AssertionError(f"the server did not answer: {last}")
