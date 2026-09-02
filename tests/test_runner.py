"""Tests for the load runner: caps, durations, ramping, failure categories."""

from __future__ import annotations

import io
import unittest

from stampede.metrics import FAILURE_CONNECTION, FAILURE_TIMEOUT
from stampede.report import LiveReporter
from stampede.runner import RunConfig, _ramp_delays, run_load
from stampede.scenario import RequestSpec

from tests.support import TestHTTPServer, free_tcp_port


class RampDelayTests(unittest.TestCase):
    def test_no_ramp_starts_everyone_immediately(self) -> None:
        self.assertEqual(_ramp_delays(4, 0.0), [0.0, 0.0, 0.0, 0.0])

    def test_single_worker_never_waits(self) -> None:
        self.assertEqual(_ramp_delays(1, 30.0), [0.0])

    def test_linear_spacing(self) -> None:
        delays = _ramp_delays(5, 8.0)
        self.assertEqual(delays, [0.0, 2.0, 4.0, 6.0, 8.0])


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    server: TestHTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = TestHTTPServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def spec(self, path: str = "/ok") -> RequestSpec:
        return RequestSpec(url=self.server.url(path))

    async def test_requests_cap_is_respected_exactly(self) -> None:
        config = RunConfig(concurrency=4, duration=None, total_requests=25, timeout=5.0)
        summary = await run_load([self.spec()], config)
        self.assertEqual(summary.completed, 25)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.status_counts, {200: 25})
        self.assertGreater(summary.bytes_received, 0)

    async def test_duration_stops_the_run(self) -> None:
        config = RunConfig(concurrency=2, duration=0.4, total_requests=None, timeout=5.0)
        summary = await run_load([self.spec()], config)
        self.assertGreater(summary.completed, 0)
        self.assertGreaterEqual(summary.elapsed_seconds, 0.4)
        self.assertLess(summary.elapsed_seconds, 5.0)

    async def test_ramp_still_honors_the_cap(self) -> None:
        config = RunConfig(
            concurrency=3, duration=None, total_requests=12, ramp=0.2, timeout=5.0
        )
        summary = await run_load([self.spec()], config)
        self.assertEqual(summary.completed, 12)

    async def test_connection_failures_are_categorized(self) -> None:
        port = free_tcp_port()
        spec = RequestSpec(url=f"http://127.0.0.1:{port}/")
        config = RunConfig(concurrency=2, duration=None, total_requests=5, timeout=2.0)
        summary = await run_load([spec], config)
        self.assertEqual(summary.completed, 0)
        self.assertEqual(summary.failed, 5)
        self.assertEqual(summary.failure_counts.get(FAILURE_CONNECTION), 5)

    async def test_timeouts_are_categorized(self) -> None:
        config = RunConfig(concurrency=1, duration=None, total_requests=2, timeout=0.1)
        summary = await run_load([self.spec("/slow")], config)
        self.assertEqual(summary.completed, 0)
        self.assertEqual(summary.failure_counts.get(FAILURE_TIMEOUT), 2)

    async def test_non_2xx_responses_are_completed_requests(self) -> None:
        config = RunConfig(concurrency=2, duration=None, total_requests=8, timeout=5.0)
        summary = await run_load([self.spec("/status/404")], config)
        self.assertEqual(summary.completed, 8)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.status_counts, {404: 8})
        self.assertEqual(summary.non_2xx, 8)

    async def test_weighted_mix_reaches_both_endpoints(self) -> None:
        specs = [
            RequestSpec(url=self.server.url("/ok"), weight=1.0),
            RequestSpec(url=self.server.url("/status/404"), weight=1.0),
        ]
        config = RunConfig(
            concurrency=2, duration=None, total_requests=40, timeout=5.0, seed=11
        )
        summary = await run_load(specs, config)
        self.assertEqual(summary.completed, 40)
        self.assertIn(200, summary.status_counts)
        self.assertIn(404, summary.status_counts)

    async def test_requires_a_stop_condition(self) -> None:
        config = RunConfig(concurrency=1, duration=None, total_requests=None)
        with self.assertRaises(ValueError):
            await run_load([self.spec()], config)

    async def test_live_reporter_receives_ticks(self) -> None:
        stream = io.StringIO()
        reporter = LiveReporter(stream)
        config = RunConfig(concurrency=2, duration=1.3, total_requests=None, timeout=5.0)
        summary = await run_load([self.spec()], config, reporter=reporter)
        self.assertGreater(summary.completed, 0)
        output = stream.getvalue()
        self.assertIn("workers", output)
        self.assertIn("rps", output)
        self.assertIn("p95", output)


if __name__ == "__main__":
    unittest.main()
