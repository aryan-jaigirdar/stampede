"""Latency statistics and per-run accounting."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "Collector",
    "FAILURE_CONNECTION",
    "FAILURE_PROTOCOL",
    "FAILURE_TIMEOUT",
    "LatencyStats",
    "Summary",
    "WindowStats",
    "percentile",
]

FAILURE_TIMEOUT = "timeout"
FAILURE_CONNECTION = "connection"
FAILURE_PROTOCOL = "protocol"
FAILURE_CATEGORIES: tuple[str, ...] = (FAILURE_TIMEOUT, FAILURE_CONNECTION, FAILURE_PROTOCOL)


def percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Percentile of ``sorted_values`` with linear interpolation.

    Uses the closest ranks method: the percentile maps to the fractional
    rank ``pct / 100 * (n - 1)`` and the result is interpolated linearly
    between the two neighboring values. ``sorted_values`` must be sorted
    ascending and non-empty.
    """
    if not sorted_values:
        raise ValueError("percentile of empty data")
    if not 0.0 <= pct <= 100.0:
        raise ValueError(f"pct must be between 0 and 100, got {pct!r}")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(sorted_values[lower])
    fraction = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


@dataclass(slots=True)
class LatencyStats:
    """Summary statistics over per-request latencies, in milliseconds."""

    mean_ms: float
    p50_ms: float
    p90_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float

    @classmethod
    def from_latencies(cls, latencies: Sequence[float]) -> "LatencyStats":
        if not latencies:
            raise ValueError("no latencies to summarize")
        ordered = sorted(latencies)
        return cls(
            mean_ms=statistics.fmean(ordered),
            p50_ms=percentile(ordered, 50.0),
            p90_ms=percentile(ordered, 90.0),
            p95_ms=percentile(ordered, 95.0),
            p99_ms=percentile(ordered, 99.0),
            max_ms=ordered[-1],
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "mean": round(self.mean_ms, 3),
            "p50": round(self.p50_ms, 3),
            "p90": round(self.p90_ms, 3),
            "p95": round(self.p95_ms, 3),
            "p99": round(self.p99_ms, 3),
            "max": round(self.max_ms, 3),
        }


@dataclass(slots=True)
class WindowStats:
    """Stats over the interval since the previous live tick."""

    attempts: int
    p95_ms: float | None


@dataclass(slots=True)
class Summary:
    """The final results of a run.

    ``completed`` counts requests that received a full response of any
    status; ``failed`` counts transport level failures (timeouts,
    connection errors, protocol errors). Non-2xx responses are completed
    requests and are broken out via ``status_counts`` and ``non_2xx``.
    """

    elapsed_seconds: float
    completed: int
    failed: int
    requests_per_second: float
    bytes_received: int
    latency: LatencyStats | None
    status_counts: dict[int, int]
    failure_counts: dict[str, int]

    @property
    def attempts(self) -> int:
        return self.completed + self.failed

    @property
    def non_2xx(self) -> int:
        return sum(
            count for status, count in self.status_counts.items() if not 200 <= status < 300
        )

    def to_dict(self) -> dict[str, Any]:
        failures = {category: self.failure_counts.get(category, 0) for category in FAILURE_CATEGORIES}
        failures["non_2xx"] = self.non_2xx
        return {
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "completed": self.completed,
            "failed": self.failed,
            "requests_per_second": round(self.requests_per_second, 2),
            "bytes_received": self.bytes_received,
            "latency_ms": self.latency.to_dict() if self.latency else None,
            "status_counts": {str(status): count for status, count in sorted(self.status_counts.items())},
            "failures": failures,
        }


class Collector:
    """Accumulates per-request outcomes during a run.

    Runs execute on a single event loop, so no locking is needed.
    """

    def __init__(self) -> None:
        self._latencies: list[float] = []
        self._status_counts: Counter[int] = Counter()
        self._failure_counts: Counter[str] = Counter()
        self._bytes_received = 0
        self._attempts = 0
        self._window_attempts = 0
        self._window_latency_start = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def completed(self) -> int:
        return len(self._latencies)

    @property
    def failed(self) -> int:
        return self._attempts - len(self._latencies)

    def record_success(self, latency_ms: float, status: int, body_bytes: int) -> None:
        """Record a request that received a full response of any status."""
        self._attempts += 1
        self._latencies.append(latency_ms)
        self._status_counts[status] += 1
        self._bytes_received += body_bytes

    def record_failure(self, category: str) -> None:
        """Record a transport level failure by category."""
        self._attempts += 1
        self._failure_counts[category] += 1

    def window(self) -> WindowStats:
        """Return stats since the previous call and advance the window.

        Used by the live status line, so RPS and p95 reflect the most
        recent interval instead of the whole run.
        """
        attempts = self._attempts - self._window_attempts
        window_latencies = self._latencies[self._window_latency_start :]
        self._window_attempts = self._attempts
        self._window_latency_start = len(self._latencies)
        p95 = percentile(sorted(window_latencies), 95.0) if window_latencies else None
        return WindowStats(attempts=attempts, p95_ms=p95)

    def summarize(self, elapsed_seconds: float) -> Summary:
        completed = len(self._latencies)
        latency = LatencyStats.from_latencies(self._latencies) if self._latencies else None
        rps = completed / elapsed_seconds if elapsed_seconds > 0 else 0.0
        return Summary(
            elapsed_seconds=elapsed_seconds,
            completed=completed,
            failed=self.failed,
            requests_per_second=rps,
            bytes_received=self._bytes_received,
            latency=latency,
            status_counts=dict(self._status_counts),
            failure_counts=dict(self._failure_counts),
        )
