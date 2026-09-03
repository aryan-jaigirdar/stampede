"""Tests for argument parsing and end to end CLI runs."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from stampede.cli import _load_specs, build_parser, main, parse_header_args

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


class HeaderParsingTests(unittest.TestCase):
    def test_single_valid_header(self) -> None:
        self.assertEqual(
            parse_header_args(["Authorization: Bearer x"]),
            {"Authorization": "Bearer x"},
        )

    def test_multiple_headers(self) -> None:
        headers = parse_header_args(["Authorization: Bearer x", "X-Env: staging"])
        self.assertEqual(headers, {"Authorization": "Bearer x", "X-Env": "staging"})

    def test_trims_name_and_value(self) -> None:
        self.assertEqual(parse_header_args(["  X-Env :  staging  "]), {"X-Env": "staging"})

    def test_splits_on_first_colon_only(self) -> None:
        # A value may itself contain colons, e.g. a bearer token or a time.
        headers = parse_header_args(["Authorization: Bearer a:b:c"])
        self.assertEqual(headers, {"Authorization": "Bearer a:b:c"})

    def test_later_duplicate_wins(self) -> None:
        headers = parse_header_args(["X-Env: one", "X-Env: two"])
        self.assertEqual(headers, {"X-Env": "two"})

    def test_missing_colon_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_header_args(["no-colon-here"])

    def test_empty_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_header_args([": value-without-name"])


class ScenarioDefaultHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write_scenario(self, content: object) -> Path:
        path = self.dir / "scenario.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        return path

    def test_defaults_apply_and_per_request_headers_override(self) -> None:
        scenario_path = self.write_scenario(
            {
                "requests": [
                    {"url": "http://127.0.0.1:8080/a"},
                    {
                        "url": "http://127.0.0.1:8080/b",
                        "headers": {"X-Env": "prod"},
                    },
                ]
            }
        )
        parser = build_parser()
        args = parser.parse_args(
            ["--scenario", str(scenario_path), "-H", "X-Env: staging", "-H", "X-Common: 1"]
        )
        specs = _load_specs(args, parser)
        # First request has no header of its own, so both defaults apply.
        self.assertEqual(specs[0].headers, {"X-Env": "staging", "X-Common": "1"})
        # Second request declares X-Env, which overrides the default; the
        # unrelated default is still applied.
        self.assertEqual(specs[1].headers, {"X-Env": "prod", "X-Common": "1"})

    def test_override_is_case_insensitive(self) -> None:
        scenario_path = self.write_scenario(
            {"requests": [{"url": "http://127.0.0.1:8080/a", "headers": {"x-env": "prod"}}]}
        )
        parser = build_parser()
        args = parser.parse_args(["--scenario", str(scenario_path), "-H", "X-Env: staging"])
        specs = _load_specs(args, parser)
        self.assertEqual(specs[0].headers, {"x-env": "prod"})


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

    def test_single_url_run_with_csv_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "requests.csv"
            code, stdout, _ = run_main(
                [
                    self.server.url("/ok"),
                    "--requests",
                    "20",
                    "--concurrency",
                    "3",
                    "--quiet",
                    "-H",
                    "X-Test: 1",
                    "--csv",
                    str(csv_path),
                ]
            )
            self.assertEqual(code, 0)
            self.assertIn("requests completed   20", stdout)
            with csv_path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["timestamp", "method", "url", "status", "latency_ms", "bytes"])
        data_rows = rows[1:]
        self.assertEqual(len(data_rows), 20)
        for row in data_rows:
            self.assertEqual(len(row), 6)
            self.assertEqual(row[1], "GET")
            self.assertTrue(row[2].endswith("/ok"))
            self.assertEqual(row[3], "200")
            self.assertGreater(float(row[4]), 0.0)
            self.assertEqual(row[5], str(len(b"hello from the test server")))

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
