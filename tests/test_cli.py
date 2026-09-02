"""Tests for argument parsing and end to end CLI runs."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from stampede.cli import build_parser, main

from tests.support import TestHTTPServer, free_tcp_port


def run_main(argv: list[str]) -> tuple[int, str, str]:
    """Run main() with captured stdout and stderr."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def run_main_expecting_exit(argv: list[str]) -> int:
    """Run main() where argparse is expected to bail out with SystemExit."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            main(argv)
        except SystemExit as exc:
            return int(exc.code or 0)
    raise AssertionError("expected SystemExit")


class ParserTests(unittest.TestCase):
    def test_defaults(self) -> None:
        args = build_parser().parse_args(["http://127.0.0.1:8080/"])
        self.assertEqual(args.concurrency, 10)
        self.assertIsNone(args.duration)
        self.assertIsNone(args.requests)
        self.assertEqual(args.ramp, 0.0)
        self.assertEqual(args.timeout, 10.0)
        self.assertEqual(args.method, "GET")
        self.assertFalse(args.quiet)
        self.assertFalse(args.insecure)

    def test_requires_a_target(self) -> None:
        self.assertEqual(run_main_expecting_exit([]), 2)

    def test_rejects_url_and_scenario_together(self) -> None:
        code = run_main_expecting_exit(["http://x/", "--scenario", "s.json"])
        self.assertEqual(code, 2)

    def test_rejects_bad_header(self) -> None:
        code = run_main_expecting_exit(["http://x/", "-H", "no-colon-here"])
        self.assertEqual(code, 2)

    def test_rejects_bad_method(self) -> None:
        code = run_main_expecting_exit(["http://x/", "-X", "TRACE"])
        self.assertEqual(code, 2)

    def test_rejects_zero_concurrency(self) -> None:
        code = run_main_expecting_exit(["http://x/", "-c", "0"])
        self.assertEqual(code, 2)

    def test_rejects_body_with_scenario(self) -> None:
        code = run_main_expecting_exit(["--scenario", "s.json", "--body", "x"])
        self.assertEqual(code, 2)

    def test_unresolvable_host_refused(self) -> None:
        code, _, stderr = run_main(
            ["http://this-host-does-not-exist.invalid/", "-n", "1", "--quiet"]
        )
        self.assertEqual(code, 2)
        self.assertIn("cannot resolve host", stderr)


class CliEndToEndTests(unittest.TestCase):
    server: TestHTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = TestHTTPServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def test_single_url_run_with_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "results.json"
            code, stdout, _ = run_main(
                [
                    self.server.url("/ok"),
                    "--requests",
                    "20",
                    "--concurrency",
                    "3",
                    "--quiet",
                    "--json",
                    str(json_path),
                ]
            )
            self.assertEqual(code, 0)
            self.assertIn("Run summary", stdout)
            self.assertIn("requests completed   20", stdout)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["completed"], 20)
        self.assertEqual(payload["failed"], 0)
        self.assertEqual(payload["status_counts"], {"200": 20})
        self.assertGreater(payload["latency_ms"]["p95"], 0.0)

    def test_post_with_header_and_body(self) -> None:
        code, stdout, _ = run_main(
            [
                self.server.url("/echo"),
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                "--body",
                '{"k": 1}',
                "-n",
                "5",
                "-q",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("requests completed   5", stdout)

    def test_scenario_run(self) -> None:
        scenario = {
            "requests": [
                {"url": self.server.url("/ok"), "weight": 3},
                {"url": self.server.url("/status/404"), "weight": 1},
                {
                    "method": "POST",
                    "url": self.server.url("/echo"),
                    "headers": {"Content-Type": "text/plain"},
                    "body": "hello",
                    "weight": 1,
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            scenario_path = Path(tmp) / "scenario.json"
            scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
            json_path = Path(tmp) / "results.json"
            code, stdout, _ = run_main(
                [
                    "--scenario",
                    str(scenario_path),
                    "-n",
                    "30",
                    "-c",
                    "4",
                    "--seed",
                    "7",
                    "-q",
                    "--json",
                    str(json_path),
                ]
            )
            self.assertEqual(code, 0)
            self.assertIn("requests completed   30", stdout)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["completed"], 30)
        total = sum(payload["status_counts"].values())
        self.assertEqual(total, 30)

    def test_exit_code_one_when_nothing_completes(self) -> None:
        port = free_tcp_port()
        code, stdout, _ = run_main([f"http://127.0.0.1:{port}/", "-n", "3", "-q"])
        self.assertEqual(code, 1)
        self.assertIn("connection error", stdout)


if __name__ == "__main__":
    unittest.main()
