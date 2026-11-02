"""``roobotto ui``: a standard-library HTTP server for the workbench page (ADR-018).

It binds to 127.0.0.1 only, refuses requests whose Host header is not local (so a page on
another site cannot reach it through DNS rebinding), and does nothing the CLI cannot do: each
endpoint calls the same function as the matching ``roobotto bench`` command.
"""

import json
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from roobotto.ui import worksheet

LOOPBACK = "127.0.0.1"
MAX_BODY = 64 * 1024


def page_html() -> str:
    return resources.files("roobotto.ui").joinpath("index.html").read_text(encoding="utf-8")


class Workbench(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], doc: Path, rig: Path) -> None:
        super().__init__(address, Handler)
        self.doc = doc
        self.rig = rig

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/"


class Handler(BaseHTTPRequestHandler):
    server: Workbench  # pyright: ignore[reportIncompatibleVariableOverride]

    def log_message(self, format: str, *args: Any) -> None:
        return

    # -- helpers --

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.rsplit(":", 1)[0] if ":" in host else host
        return name in {LOOPBACK, "localhost"}

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, data: Any) -> None:
        self._send(status, json.dumps(data, indent=1).encode("utf-8"), "application/json")

    def _refuse_foreign_host(self) -> bool:
        if self._host_ok():
            return False
        self._json(HTTPStatus.FORBIDDEN, {"error": "the workbench answers localhost only"})
        return True

    def _worksheet(self) -> dict[str, Any]:
        return {
            "doc": str(self.server.doc),
            "rig": str(self.server.rig),
            "tests": [asdict(t) for t in worksheet.TESTS],
            "records": worksheet.records(self.server.rig),
            "results": worksheet.logged_results(self.server.doc),
        }

    # -- routes --

    def do_GET(self) -> None:
        if self._refuse_foreign_host():
            return
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(HTTPStatus.OK, page_html().encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/worksheet":
            try:
                self._json(HTTPStatus.OK, self._worksheet())
            except worksheet.WorksheetError as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self._refuse_foreign_host():
            return
        if self.path.split("?", 1)[0] != "/api/results":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad request body"})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            result = worksheet.result_from_dict(data)  # pyright: ignore[reportUnknownArgumentType]
            written = worksheet.record(result, self.server.doc, self.server.rig)
        except (ValueError, UnicodeDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._json(HTTPStatus.OK, {"written": [str(p) for p in written]})


def make_server(doc: Path, rig: Path, port: int = 0, host: str = LOOPBACK) -> Workbench:
    """Bind the workbench. Any host other than 127.0.0.1 is refused (ADR-018)."""
    if host != LOOPBACK:
        raise ValueError(f"the workbench binds to {LOOPBACK} only, not '{host}'")
    return Workbench((host, port), doc=doc, rig=rig)
