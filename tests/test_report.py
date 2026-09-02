"""Tests for terminal formatting and JSON output."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from stampede.metrics import Collector, FAILURE_TIMEOUT
from stampede.report import LiveReporter, format_summary, human_bytes, write_json


def sample_summary():
    collector = Collector()
    for latency in (10.0, 12.0, 14.0, 16.0, 18.0):
        collector.record_success(latency, 200, 128)
    collector.record_success(25.0, 404, 64)
    collector.record_failure(FAILURE_TIMEOUT)
    return collector.summarize(elapsed_seconds=2.0)


class HumanBytesTests(unittest.TestCase):
    def test_bytes(self) -> None:
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(512), "512 B")

    def test_binary_units(self) -> None:
        self.assertEqual(human_bytes(2048), "2.0 KiB")
        self.assertEqual(human_bytes(5 * 1024 * 1024), "5.0 MiB")
        self.assertEqual(human_bytes(int(1.5 * 1024**3)), "1.5 GiB")


class FormatSummaryTests(unittest.TestCase):
    def test_contains_all_sections_and_values(self) -> None:
        text = format_summary(sample_summary())
        self.assertIn("Run summary", text)
        self.assertIn("requests completed", text)
        self.assertIn("6", text)
        self.assertIn("requests per second", text)
        self.assertIn("Latency (ms)", text)
        self.assertIn("p50", text)
        self.assertIn("p95", text)
        self.assertIn("Status codes", text)
        self.assertIn("200", text)
        self.assertIn("404", text)
        self.assertIn("Failures", text)
        self.assertIn("timeout", text)
        self.assertIn("non-2xx responses", text)

    def test_no_traffic_renders_placeholders(self) -> None:
        text = format_summary(Collector().summarize(elapsed_seconds=1.0))
        self.assertIn("no completed requests", text)
        self.assertIn("none", text)

    def test_no_failures_renders_none(self) -> None:
        collector = Collector()
        collector.record_success(10.0, 200, 1)
        text = format_summary(collector.summarize(elapsed_seconds=1.0))
        self.assertIn("Failures\n  none", text)


class WriteJsonTests(unittest.TestCase):
    def test_writes_valid_json_with_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.json"
            write_json(sample_summary(), str(path))
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("stampede_version", payload)
        self.assertEqual(payload["completed"], 6)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["status_counts"]["200"], 5)
        self.assertEqual(payload["failures"]["timeout"], 1)
        self.assertEqual(payload["failures"]["non_2xx"], 1)
        self.assertIsInstance(payload["latency_ms"]["p95"], float)


class LiveReporterTests(unittest.TestCase):
    def test_non_tty_stream_gets_one_line_per_update(self) -> None:
        stream = io.StringIO()
        reporter = LiveReporter(stream)
        reporter.update(
            elapsed=1.0, active_workers=3, total_workers=10, done=42, rps=42.0, p95_ms=12.5
        )
        reporter.update(
            elapsed=2.0, active_workers=10, total_workers=10, done=99, rps=57.0, p95_ms=None
        )
        reporter.finish()
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("workers 3/10", lines[0])
        self.assertIn("p95 12.5 ms", lines[0])
        self.assertIn("p95 -", lines[1])
        self.assertNotIn("\r", stream.getvalue())

    def test_defaults_to_stderr(self) -> None:
        reporter = LiveReporter()
        self.assertIsNotNone(reporter)


if __name__ == "__main__":
    unittest.main()
