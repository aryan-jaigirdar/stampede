"""Tests for the raw asyncio HTTP client."""

from __future__ import annotations

import asyncio
import unittest

from stampede.client import (
    Client,
    ConnectionClosed,
    HTTPConnection,
    ProtocolError,
    parse_url,
)

from tests.support import TestHTTPServer, free_tcp_port


class ParseURLTests(unittest.TestCase):
    def test_defaults_for_http(self) -> None:
        parsed = parse_url("http://example.com")
        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(parsed.host, "example.com")
        self.assertEqual(parsed.port, 80)
        self.assertEqual(parsed.target, "/")

    def test_defaults_for_https(self) -> None:
        parsed = parse_url("https://example.com/health")
        self.assertEqual(parsed.port, 443)
        self.assertEqual(parsed.target, "/health")

    def test_explicit_port_and_query(self) -> None:
        parsed = parse_url("https://example.com:8443/search?q=cows&limit=5")
        self.assertEqual(parsed.port, 8443)
        self.assertEqual(parsed.target, "/search?q=cows&limit=5")

    def test_rejects_unsupported_scheme(self) -> None:
        with self.assertRaises(ValueError):
            parse_url("ftp://example.com/file")

    def test_rejects_missing_host(self) -> None:
        with self.assertRaises(ValueError):
            parse_url("http:///nohost")


class ClientAgainstLocalServerTests(unittest.IsolatedAsyncioTestCase):
    server: TestHTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = TestHTTPServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    async def asyncSetUp(self) -> None:
        self.client = Client(timeout=5.0)

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    def url(self, path: str) -> str:
        return self.server.url(path)

    async def test_get_parses_status_reason_headers_and_body(self) -> None:
        response = await self.client.request("GET", self.url("/ok"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.reason, "OK")
        self.assertTrue(response.ok)
        self.assertEqual(response.body, b"hello from the test server")
        self.assertEqual(response.headers["content-type"], "text/plain")
        self.assertEqual(response.headers["content-length"], str(len(response.body)))

    async def test_post_sends_body_and_headers(self) -> None:
        payload = b'{"answer": 42}'
        response = await self.client.request(
            "POST",
            self.url("/echo"),
            headers={"Content-Type": "application/json"},
            body=payload,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, payload)
        self.assertEqual(response.headers["x-method"], "POST")
        self.assertEqual(response.headers["x-received-content-type"], "application/json")

    async def test_put_and_delete(self) -> None:
        put_response = await self.client.request("PUT", self.url("/echo"), body=b"cargo")
        self.assertEqual(put_response.headers["x-method"], "PUT")
        self.assertEqual(put_response.body, b"cargo")
        delete_response = await self.client.request("DELETE", self.url("/anything"))
        self.assertEqual(delete_response.body, b"deleted")

    async def test_chunked_response_is_reassembled(self) -> None:
        response = await self.client.request("GET", self.url("/chunked"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"alpha beta gamma")
        self.assertIn("chunked", response.headers["transfer-encoding"])

    async def test_chunked_response_with_extensions(self) -> None:
        response = await self.client.request("GET", self.url("/chunked-ext"))
        self.assertEqual(response.body, b"with extensions")

    async def test_keep_alive_reuses_the_connection(self) -> None:
        first = await self.client.request("GET", self.url("/ok"))
        second = await self.client.request("GET", self.url("/chunked"))
        self.assertEqual(
            first.headers["x-client-port"],
            second.headers["x-client-port"],
            "expected both requests to share one client socket",
        )

    async def test_connection_close_header_is_honored(self) -> None:
        first = await self.client.request("GET", self.url("/close"))
        second = await self.client.request("GET", self.url("/ok"))
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertNotEqual(
            first.headers["x-client-port"],
            second.headers["x-client-port"],
            "expected a fresh socket after Connection: close",
        )

    async def test_non_2xx_status_is_parsed_not_raised(self) -> None:
        response = await self.client.request("GET", self.url("/status/404"))
        self.assertEqual(response.status, 404)
        self.assertFalse(response.ok)
        self.assertEqual(response.body, b"status 404")

    async def test_204_has_no_body(self) -> None:
        response = await self.client.request("GET", self.url("/status/204"))
        self.assertEqual(response.status, 204)
        self.assertEqual(response.body, b"")

    async def test_empty_body_with_content_length_zero(self) -> None:
        response = await self.client.request("GET", self.url("/empty"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"")

    async def test_timeout_raises_and_connection_recovers(self) -> None:
        client = Client(timeout=0.1)
        try:
            with self.assertRaises(TimeoutError):
                await client.request("GET", self.url("/slow"))
            # The timed out connection must not poison the next request.
            response = await client.request("GET", self.url("/ok"))
            self.assertEqual(response.status, 200)
        finally:
            await client.aclose()

    async def test_connection_refused_raises_oserror(self) -> None:
        port = free_tcp_port()
        with self.assertRaises(OSError):
            await self.client.request("GET", f"http://127.0.0.1:{port}/")


class RawSocketBehaviorTests(unittest.IsolatedAsyncioTestCase):
    """Behaviors that need a hand-rolled server: stale sockets, bad bytes."""

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = await reader.read(1024)
            if not chunk:
                break
            data += chunk
        return data

    async def test_stale_keep_alive_connection_is_retried(self) -> None:
        connection_count = 0

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal connection_count
            connection_count += 1
            await self._read_request(reader)
            # Advertise keep-alive implicitly (HTTP/1.1, no Connection header),
            # then close the socket anyway: the classic stale keep-alive.
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = Client(timeout=5.0)
        try:
            first = await client.request("GET", f"http://127.0.0.1:{port}/")
            self.assertEqual(first.status, 200)
            await asyncio.sleep(0.05)  # let the server side close land
            second = await client.request("GET", f"http://127.0.0.1:{port}/")
            self.assertEqual(second.status, 200)
            self.assertEqual(connection_count, 2, "expected a retry on a fresh connection")
        finally:
            await client.aclose()
            server.close()
            await server.wait_closed()

    async def test_body_read_to_eof_when_unframed(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._read_request(reader)
            writer.write(b"HTTP/1.0 200 OK\r\n\r\nbody until close")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = Client(timeout=5.0)
        try:
            response = await client.request("GET", f"http://127.0.0.1:{port}/")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.body, b"body until close")
        finally:
            await client.aclose()
            server.close()
            await server.wait_closed()

    async def test_malformed_status_line_raises_protocol_error(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._read_request(reader)
            writer.write(b"NOT-HTTP nonsense\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = Client(timeout=5.0)
        try:
            with self.assertRaises(ProtocolError):
                await client.request("GET", f"http://127.0.0.1:{port}/")
        finally:
            await client.aclose()
            server.close()
            await server.wait_closed()

    async def test_truncated_body_raises_connection_closed(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._read_request(reader)
            # Promise 100 bytes, deliver 5, then close.
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = Client(timeout=5.0)
        try:
            with self.assertRaises(ConnectionClosed):
                await client.request("GET", f"http://127.0.0.1:{port}/")
        finally:
            await client.aclose()
            server.close()
            await server.wait_closed()

    async def test_informational_1xx_responses_are_skipped(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._read_request(reader)
            writer.write(
                b"HTTP/1.1 102 Processing\r\n\r\n"
                b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ndone"
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = Client(timeout=5.0)
        try:
            response = await client.request("GET", f"http://127.0.0.1:{port}/")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.body, b"done")
        finally:
            await client.aclose()
            server.close()
            await server.wait_closed()


class ConnectionUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_tracks_completed_requests(self) -> None:
        server = TestHTTPServer()
        server.start()
        try:
            connection = HTTPConnection("http", "127.0.0.1", server.port)
            await connection.connect()
            headers = {"Host": f"127.0.0.1:{server.port}", "Connection": "keep-alive"}
            await connection.request("GET", "/ok", headers, None)
            await connection.request("GET", "/ok", headers, None)
            self.assertEqual(connection.completed_requests, 2)
            self.assertTrue(connection.is_open)
            await connection.close()
            self.assertFalse(connection.is_open)
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
