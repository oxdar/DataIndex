"""``dataindex-ui``: serve the local chat interface on this computer only."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from dataindex import DataIndexClient, DataIndexError
from dataindex.local import DEFAULT_STORAGE_PATH

from dataindex_ui import __version__
from dataindex_ui.app import UISettings, create_app
from dataindex_ui.history import HistoryError
from dataindex_ui.worker import ClientWorker

DEFAULT_PORT = 8765
MODEL_ENVIRONMENT_VARIABLE = "DATAINDEX_MODEL"
LOOPBACK_HOSTS = {"127.0.0.1": socket.AF_INET, "::1": socket.AF_INET6}
STARTUP_ERRORS = (DataIndexError, HistoryError, OSError, ValueError)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dataindex-ui",
        description=(
            "Chat with your DataIndex documents in a browser. The server listens "
            "only on this computer; open the printed link to sign in."
        ),
    )
    parser.add_argument(
        "--storage",
        default=DEFAULT_STORAGE_PATH,
        metavar="DIR",
        help=f"workspace directory, created if missing (default: {DEFAULT_STORAGE_PATH})",
    )
    parser.add_argument(
        "--model",
        metavar="NAME",
        help=(
            f"chat model name (default: ${MODEL_ENVIRONMENT_VARIABLE}); without one "
            "you can still manage documents and read saved chats"
        ),
    )
    parser.add_argument(
        "--instructions",
        metavar="TEXT",
        help="extra guidance for the model, at most 4000 characters",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        choices=sorted(LOOPBACK_HOSTS),
        help="loopback address to listen on (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port to listen on; 0 picks a free one (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open a browser window"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 <= args.port <= 65535:
        print("dataindex-ui: --port must be 0 to 65535", file=sys.stderr)
        return 2
    model = (args.model or os.environ.get(MODEL_ENVIRONMENT_VARIABLE, "")).strip() or None
    storage = Path(args.storage)
    try:
        listener = _listen(args.host, args.port)
    except OSError as error:
        print(
            f"dataindex-ui: cannot listen on {args.host} port {args.port}: "
            f"{error.strerror or error}",
            file=sys.stderr,
        )
        return 2
    try:
        worker = ClientWorker(
            lambda: DataIndexClient(
                model, storage_path=storage, instructions=args.instructions
            )
        )
        try:
            settings = UISettings(
                storage_path=storage, port=listener.getsockname()[1], model_name=model
            )
            app = create_app(settings, worker)
        except BaseException:
            worker.close()
            raise
    except STARTUP_ERRORS as error:
        listener.close()
        print(f"dataindex-ui: {error}", file=sys.stderr)
        return 2
    address = f"[{args.host}]" if ":" in args.host else args.host
    url = f"http://{address}:{settings.port}/?token={settings.token}"
    print(f"DataIndex chat {__version__} is running. Open this link to sign in:")
    print(f"    {url}")
    if model is None:
        print(
            f"No chat model is set; pass --model NAME or set {MODEL_ENVIRONMENT_VARIABLE} "
            "to ask questions.",
            file=sys.stderr,
        )
    print("Press Ctrl+C to stop.")
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")
    )
    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(server, url), daemon=True).start()
    try:
        server.run(sockets=[listener])
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()
    return 0


def _listen(host: str, port: int) -> socket.socket:
    listener = socket.socket(LOOPBACK_HOSTS[host], socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            # Windows' SO_REUSEADDR would let another process share the port.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(128)
        listener.set_inheritable(True)
    except BaseException:
        listener.close()
        raise
    return listener


def _open_when_ready(server: uvicorn.Server, url: str) -> None:
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    if server.started:
        webbrowser.open(url)
