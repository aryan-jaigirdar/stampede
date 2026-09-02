"""A small threaded HTTP server used by the client, runner, and CLI tests."""

from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep test output quiet

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Client-Port", str(self.client_address[1]))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_chunked(self, chunks: list[bytes], *, with_extension: bool = False) -> None:
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Client-Port", str(self.client_address[1]))
        self.end_headers()
        for chunk in chunks:
            size = f"{len(chunk):x}"
            if with_extension:
                size += ";note=ignored"
            self.wfile.write(size.encode("ascii") + b"\r\n" + chunk + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/ok":
            self._send_bytes(200, b"hello from the test server")
        elif path == "/empty":
            self._send_bytes(200, b"")
        elif path == "/chunked":
            self._send_chunked([b"alpha ", b"beta ", b"gamma"])
        elif path == "/chunked-ext":
            self._send_chunked([b"with ", b"extensions"], with_extension=True)
        elif path == "/slow":
            time.sleep(0.5)
            self._send_bytes(200, b"finally")
        elif path == "/close":
            body = b"closing"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Client-Port", str(self.client_address[1]))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/status/"):
            status = int(path.rsplit("/", 1)[1])
            if status in (204, 304):
                self.send_response(status)
                self.send_header("X-Client-Port", str(self.client_address[1]))
                self.end_headers()
            else:
                self._send_bytes(status, f"status {status}".encode("ascii"))
        else:
            self._send_bytes(404, b"not found")

    def _echo(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self._send_bytes(
            200,
            body,
            extra_headers={
                "X-Method": self.command,
                "X-Received-Content-Type": self.headers.get("Content-Type", ""),
            },
        )

    do_POST = _echo
    do_PUT = _echo

    def do_DELETE(self) -> None:
        self._send_bytes(200, b"deleted")


class TestHTTPServer:
    """Runs the handler above on a background thread on a random port."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        # Broken pipes are expected when tests exercise client timeouts.
        self._server.handle_error = lambda *args: None  # type: ignore[method-assign]
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


def free_tcp_port() -> int:
    """Reserve and release a local port, so connecting to it is refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
