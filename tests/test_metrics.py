"""Tests for percentile math and run accounting."""

from __future__ import annotations

import json
import unittest

from stampede.metrics import (
    Collector,
    FAILURE_CONNECTION,
    FAILURE_TIMEOUT,
    LatencyStats,
    percentile,
)


class PercentileTests(unittest.TestCase):
    def test_empty_data_raises(self) -> None:
        with self.assertRaises(ValueError):
            percentile([], 50.0)

    def test_out_of_range_pct_raises(self) -> None:
        with self.assertRaises(ValueError):
            percentile([1.0], -1.0)
        with self.assertRaises(ValueError):
            percentile([1.0], 100.5)

    def test_single_value(self) -> None:
        self.assertEqual(percentile([42.0], 0.0), 42.0)
        self.assertEqual(percentile([42.0], 50.0), 42.0)
        self.assertEqual(percentile([42.0], 100.0), 42.0)

    def test_endpoints_are_min_and_max(self) -> None:
        data = [1.0, 2.0, 3.0, 10.0]
        self.assertEqual(percentile(data, 0.0), 1.0)
        self.assertEqual(percentile(data, 100.0), 10.0)

    def test_linear_interpolation_between_ranks(self) -> None:
        data = [10.0, 20.0, 30.0, 40.0]
        # rank = 0.5 * 3 = 1.5, halfway between 20 and 30
        self.assertAlmostEqual(percentile(data, 50.0), 25.0)
        # rank = 0.25 * 3 = 0.75, three quarters of the way from 10 to 20
        self.assertAlmostEqual(percentile(data, 25.0), 17.5)

    def test_exact_rank_needs_no_interpolation(self) -> None:
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(percentile(data, 50.0), 3.0)
        self.assertEqual(percentile(data, 25.0), 2.0)

    def test_two_values(self) -> None:
        data = [100.0, 200.0]
        self.assertAlmostEqual(percentile(data, 50.0), 150.0)
        self.assertAlmostEqual(percentile(data, 90.0), 190.0)

    def test_p99_on_a_larger_series(self) -> None:
        data = [float(value) for value in range(1, 101)]  # 1..100
        # rank = 0.99 * 99 = 98.01, between 99 and 100
        self.assertAlmostEqual(percentile(data, 99.0), 99.01)


class LatencyStatsTests(unittest.TestCase):
    def test_from_latencies_handles_unsorted_input(self) -> None:
        stats = LatencyStats.from_latencies([30.0, 10.0, 20.0, 40.0])
        self.assertAlmostEqual(stats.mean_ms, 25.0)
        self.assertAlmostEqual(stats.p50_ms, 25.0)
        self.assertEqual(stats.max_ms, 40.0)

    def test_empty_input_raises(self) -> None:
        with self.assertRaises(ValueError):
            LatencyStats.from_latencies([])

    def test_to_dict_has_all_keys(self) -> None:
        stats = LatencyStats.from_latencies([1.0, 2.0, 3.0])
        self.assertEqual(set(stats.to_dict()), {"mean", "p50", "p90", "p95", "p99", "max"})


class CollectorTests(unittest.TestCase):
    def test_counts_and_bytes(self) -> None:
        collector = Collector()
        collector.record_success(10.0, 200, 100)
        collector.record_success(20.0, 200, 100)
        collector.record_success(30.0, 404, 50)
        collector.record_failure(FAILURE_TIMEOUT)
        collector.record_failure(FAILURE_CONNECTION)

        self.assertEqual(collector.attempts, 5)
        self.assertEqual(collector.completed, 3)
        self.assertEqual(collector.failed, 2)

        summary = collector.summarize(elapsed_seconds=2.0)
        self.assertEqual(summary.completed, 3)
        self.assertEqual(summary.failed, 2)
        self.assertEqual(summary.attempts, 5)
        self.assertEqual(summary.bytes_received, 250)
        self.assertEqual(summary.status_counts, {200: 2, 404: 1})
        self.assertEqual(summary.failure_counts, {FAILURE_TIMEOUT: 1, FAILURE_CONNECTION: 1})
        self.assertEqual(summary.non_2xx, 1)
        self.assertAlmostEqual(summary.requests_per_second, 1.5)

    def test_summary_with_no_traffic(self) -> None:
        summary = Collector().summarize(elapsed_seconds=1.0)
        self.assertEqual(summary.completed, 0)
        self.assertIsNone(summary.latency)
        self.assertEqual(summary.requests_per_second, 0.0)

    def test_window_advances_and_resets(self) -> None:
        collector = Collector()
        collector.record_success(10.0, 200, 1)
        collector.record_success(20.0, 200, 1)
        collector.record_failure(FAILURE_TIMEOUT)

        first = collector.window()
        self.assertEqual(first.attempts, 3)
        self.assertIsNotNone(first.p95_ms)

        second = collector.window()
        self.assertEqual(second.attempts, 0)
        self.assertIsNone(second.p95_ms)

        collector.record_success(99.0, 200, 1)
        third = collector.window()
        self.assertEqual(third.attempts, 1)
        self.assertAlmostEqual(third.p95_ms or 0.0, 99.0)

    def test_to_dict_is_json_serializable_with_stable_schema(self) -> None:
        collector = Collector()
        collector.record_success(12.5, 200, 64)
        collector.record_success(15.0, 503, 32)
        summary = collector.summarize(elapsed_seconds=1.0)
        payload = summary.to_dict()
        encoded = json.dumps(payload)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["completed"], 2)
        self.assertEqual(decoded["status_counts"], {"200": 1, "503": 1})
        self.assertEqual(
            set(decoded["failures"]), {"timeout", "connection", "protocol", "non_2xx"}
        )
        self.assertEqual(decoded["failures"]["non_2xx"], 1)
        self.assertEqual(
            set(decoded["latency_ms"]), {"mean", "p50", "p90", "p95", "p99", "max"}
        )


if __name__ == "__main__":
    unittest.main()
