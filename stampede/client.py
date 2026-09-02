"""HTTP/1.1 client built directly on asyncio streams.

This module implements just enough of HTTP/1.1 to drive a load test:
request serialization, status line and header parsing, response bodies
framed by Content-Length or chunked transfer encoding, and keep-alive
connection reuse with a single retry when a pooled connection turns out
to be stale.
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from . import __version__

__all__ = [
    "Client",
    "ClientError",
    "ConnectionClosed",
    "HTTPConnection",
    "ParsedURL",
    "ProtocolError",
    "Response",
    "parse_url",
]

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}
USER_AGENT = f"stampede/{__version__}"


class ClientError(Exception):
    """Base class for errors raised by this client."""


class ProtocolError(ClientError):
    """The server sent a response this client cannot parse."""


class ConnectionClosed(ClientError, ConnectionError):
    """The connection closed before a full response was received.

    ``stale`` is True when a previously used keep-alive connection turned
    out to be dead before any response bytes arrived, which makes the
    request safe to retry on a fresh connection.
    """

    def __init__(self, message: str, *, stale: bool = False) -> None:
        super().__init__(message)
        self.stale = stale


@dataclass(frozen=True, slots=True)
class ParsedURL:
    """The pieces of an http or https URL a request needs."""

    scheme: str
    host: str
    port: int
    target: str


def parse_url(url: str) -> ParsedURL:
    """Split an http(s) URL into scheme, host, port, and request target.

    Raises ValueError for unsupported schemes or URLs without a host.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise ValueError(f"unsupported URL scheme in {url!r} (expected http or https)")
    host = parts.hostname
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    try:
        port = parts.port or DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise ValueError(f"invalid port in {url!r}") from exc
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    return ParsedURL(scheme=scheme, host=host, port=port, target=target)


@dataclass(slots=True)
class Response:
    """A fully read HTTP response.

    Header names are lower cased; repeated headers are joined with ", ".
    """

    status: int
    reason: str
    headers: dict[str, str]
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


async def _readline(reader: asyncio.StreamReader) -> bytes:
    """Read one CRLF terminated line, mapping oversized lines to ProtocolError."""
    try:
        return await reader.readline()
    except ValueError as exc:
        raise ProtocolError("response line exceeds the maximum length") from exc


def _parse_status_line(line: bytes) -> tuple[str, int, str]:
    text = line.decode("latin-1").rstrip("\r\n")
    parts = text.split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise ProtocolError(f"malformed status line: {text!r}")
    try:
        status = int(parts[1])
    except ValueError:
        raise ProtocolError(f"malformed status code in: {text!r}") from None
    if not 100 <= status <= 599:
        raise ProtocolError(f"status code out of range: {status}")
    reason = parts[2] if len(parts) == 3 else ""
    return parts[0], status, reason


async def _read_header_block(reader: asyncio.StreamReader) -> dict[str, str]:
    headers: dict[str, str] = {}
    while True:
        line = await _readline(reader)
        if not line:
            raise ConnectionClosed("connection closed inside response headers")
        if line in (b"\r\n", b"\n"):
            return headers
        text = line.decode("latin-1").rstrip("\r\n")
        name, sep, value = text.partition(":")
        if not sep or not name.strip():
            raise ProtocolError(f"malformed header line: {text!r}")
        key = name.strip().lower()
        value = value.strip()
        if key in headers:
            headers[key] = f"{headers[key]}, {value}"
        else:
            headers[key] = value


async def _read_chunked_body(reader: asyncio.StreamReader) -> bytes:
    """Read a chunked transfer encoded body, including the trailer section."""
    parts: list[bytes] = []
    while True:
        size_line = await _readline(reader)
        if not size_line:
            raise ConnectionClosed("connection closed inside chunked body")
        size_text = size_line.split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError:
            raise ProtocolError(f"invalid chunk size line: {size_line!r}") from None
        if size < 0:
            raise ProtocolError(f"negative chunk size: {size}")
        if size == 0:
            break
        parts.append(await reader.readexactly(size))
        terminator = await reader.readexactly(2)
        if terminator != b"\r\n":
            raise ProtocolError("missing CRLF after chunk data")
    while True:
        line = await _readline(reader)
        if not line:
            raise ConnectionClosed("connection closed inside chunked trailers")
        if line in (b"\r\n", b"\n"):
            return b"".join(parts)


class HTTPConnection:
    """A single, optionally reused, connection to one origin."""

    def __init__(self, scheme: str, host: str, port: int, *, insecure: bool = False) -> None:
        self.scheme = scheme
        self.host = host
        self.port = port
        self.insecure = insecure
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.completed_requests = 0

    @property
    def is_open(self) -> bool:
        return self._writer is not None

    async def connect(self) -> None:
        ssl_context: ssl.SSLContext | None = None
        if self.scheme == "https":
            ssl_context = ssl.create_default_context()
            if self.insecure:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port, ssl=ssl_context
        )
        self.completed_requests = 0

    async def close(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def request(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> Response:
        """Send one request and read the full response.

        Reuses the open connection when possible. Raises ConnectionClosed
        with ``stale=True`` when a reused connection died before any
        response bytes arrived, so callers can retry safely.
        """
        reused = self.is_open and self.completed_requests > 0
        if not self.is_open:
            await self.connect()
        assert self._reader is not None and self._writer is not None
        try:
            self._writer.write(_serialize_request(method, target, headers, body))
            await self._writer.drain()
            response, keep_alive = await self._read_response(reused)
        except ConnectionClosed:
            await self.close()
            raise
        except (ConnectionResetError, BrokenPipeError) as exc:
            await self.close()
            message = str(exc) or "connection reset by peer"
            raise ConnectionClosed(message, stale=reused) from exc
        except asyncio.IncompleteReadError as exc:
            await self.close()
            raise ConnectionClosed("connection closed mid response") from exc
        except ProtocolError:
            await self.close()
            raise
        self.completed_requests += 1
        if not keep_alive:
            await self.close()
        return response

    async def _read_response(self, reused: bool) -> tuple[Response, bool]:
        reader = self._reader
        assert reader is not None
        while True:
            status_line = await _readline(reader)
            if not status_line:
                raise ConnectionClosed(
                    "server closed the connection before responding", stale=reused
                )
            version, status, reason = _parse_status_line(status_line)
            headers = await _read_header_block(reader)
            if status >= 200:
                break
            # 1xx informational responses carry no body; read the next response.
        body, framed = await self._read_body(reader, status, headers)
        keep_alive = framed and _keep_alive(version, headers)
        return Response(status=status, reason=reason, headers=headers, body=body), keep_alive

    @staticmethod
    async def _read_body(
        reader: asyncio.StreamReader, status: int, headers: Mapping[str, str]
    ) -> tuple[bytes, bool]:
        """Read the response body. Returns (body, framed).

        ``framed`` is False when the body was delimited only by the server
        closing the connection, which rules out keep-alive.
        """
        if status in (204, 304):
            return b"", True
        if "chunked" in headers.get("transfer-encoding", "").lower():
            return await _read_chunked_body(reader), True
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError:
                raise ProtocolError(f"invalid Content-Length: {content_length!r}") from None
            if length < 0:
                raise ProtocolError(f"negative Content-Length: {length}")
            body = await reader.readexactly(length) if length else b""
            return body, True
        return await reader.read(), False


def _serialize_request(
    method: str, target: str, headers: Mapping[str, str], body: bytes | None
) -> bytes:
    lines = [f"{method} {target} HTTP/1.1"]
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    return head + body if body else head


def _keep_alive(version: str, headers: Mapping[str, str]) -> bool:
    connection = headers.get("connection", "").lower()
    if version == "HTTP/1.1":
        return "close" not in connection
    if version == "HTTP/1.0":
        return "keep-alive" in connection
    return False


class Client:
    """Issues requests, pooling one keep-alive connection per origin.

    Each request is bounded by ``timeout`` seconds per attempt. When a
    reused keep-alive connection turns out to be stale, the request is
    retried once on a fresh connection.
    """

    def __init__(self, *, timeout: float = 10.0, insecure: bool = False) -> None:
        self._timeout = timeout
        self._insecure = insecure
        self._connections: dict[tuple[str, str, int], HTTPConnection] = {}

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> Response:
        parsed = parse_url(url)
        key = (parsed.scheme, parsed.host, parsed.port)
        connection = self._connections.get(key)
        if connection is None:
            connection = HTTPConnection(
                parsed.scheme, parsed.host, parsed.port, insecure=self._insecure
            )
            self._connections[key] = connection
        request_headers = _build_headers(parsed, headers, body)
        for attempt in range(2):
            try:
                async with asyncio.timeout(self._timeout):
                    return await connection.request(
                        method, parsed.target, request_headers, body
                    )
            except TimeoutError:
                await connection.close()
                raise
            except ConnectionClosed as exc:
                if exc.stale and attempt == 0:
                    continue
                raise
            except OSError:
                await connection.close()
                raise
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        connections = list(self._connections.values())
        self._connections.clear()
        for connection in connections:
            await connection.close()


def _build_headers(
    parsed: ParsedURL, user_headers: Mapping[str, str] | None, body: bytes | None
) -> dict[str, str]:
    """Merge default headers with user headers, case insensitively."""
    host = parsed.host
    if parsed.port != DEFAULT_PORTS[parsed.scheme]:
        host = f"{host}:{parsed.port}"
    merged: dict[str, str] = {
        "Host": host,
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Connection": "keep-alive",
    }

    def put(name: str, value: str) -> None:
        lowered = name.lower()
        for existing in list(merged):
            if existing.lower() == lowered:
                del merged[existing]
        merged[name] = value

    if user_headers:
        for name, value in user_headers.items():
            put(name, value)
    lowered_keys = {key.lower() for key in merged}
    if body is not None and "content-length" not in lowered_keys and "transfer-encoding" not in lowered_keys:
        merged["Content-Length"] = str(len(body))
    return merged
