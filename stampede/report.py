"""Terminal and JSON reporting."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from typing import Iterable, TextIO

from . import __version__
from .metrics import FAILURE_CONNECTION, FAILURE_PROTOCOL, FAILURE_TIMEOUT, RequestRecord, Summary

__all__ = [
    "CSV_COLUMNS",
    "LiveReporter",
    "format_summary",
    "human_bytes",
    "write_csv",
    "write_json",
]

CSV_COLUMNS: tuple[str, ...] = ("timestamp", "method", "url", "status", "latency_ms", "bytes")


def human_bytes(count: int) -> str:
    """Render a byte count with a binary unit, e.g. 2048 -> '2.0 KiB'."""
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024.0:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def format_summary(summary: Summary) -> str:
    """Format the end of run summary as an aligned plain text table."""
    lines: list[str] = []

    def row(label: str, value: str) -> None:
        lines.append(f"  {label:<21}{value}")

    lines.append("Run summary")
    row("elapsed", f"{summary.elapsed_seconds:.2f} s")
    row("requests completed", str(summary.completed))
    row("requests failed", str(summary.failed))
    row("requests per second", f"{summary.requests_per_second:.1f}")
    if summary.target_rps is not None:
        row("target rps", f"{summary.target_rps:g}")
    row("bytes received", human_bytes(summary.bytes_received))

    lines.append("")
    lines.append("Latency (ms)")
    if summary.latency is None:
        lines.append("  no completed requests")
    else:
        latency = summary.latency
        for label, value in (
            ("mean", latency.mean_ms),
            ("p50", latency.p50_ms),
            ("p90", latency.p90_ms),
            ("p95", latency.p95_ms),
            ("p99", latency.p99_ms),
            ("max", latency.max_ms),
        ):
            lines.append(f"  {label:<6}{value:>10.1f}")

    lines.append("")
    lines.append("Status codes")
    if summary.status_counts:
        for status, count in sorted(summary.status_counts.items()):
            lines.append(f"  {status:<6}{count:>10}")
    else:
        lines.append("  none")

    lines.append("")
    lines.append("Failures")
    failure_rows = [
        ("timeout", summary.failure_counts.get(FAILURE_TIMEOUT, 0)),
        ("connection error", summary.failure_counts.get(FAILURE_CONNECTION, 0)),
        ("protocol error", summary.failure_counts.get(FAILURE_PROTOCOL, 0)),
        ("non-2xx responses", summary.non_2xx),
    ]
    nonzero = [(label, count) for label, count in failure_rows if count]
    if nonzero:
        for label, count in nonzero:
            lines.append(f"  {label:<19}{count:>8}")
    else:
        lines.append("  none")

    return "\n".join(lines)


def write_json(summary: Summary, path: str) -> None:
    """Write the full results to ``path`` as pretty printed JSON."""
    payload = {"stampede_version": __version__, **summary.to_dict()}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_csv(records: Iterable[RequestRecord], path: str) -> None:
    """Write one row per finished request to ``path`` as CSV.

    Columns, in order: ``timestamp`` (ISO 8601, UTC, when the request
    started), ``method``, ``url``, ``status`` (the HTTP status code for a
    completed request, or the failure category for a transport failure:
    timeout, connection, or protocol), ``latency_ms`` (time to the
    response or the failure, to three decimals), and ``bytes`` (response
    body size, 0 for a failure).
    """
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for record in records:
            timestamp = datetime.fromtimestamp(record.timestamp, timezone.utc).isoformat()
            writer.writerow(
                [
                    timestamp,
                    record.method,
                    record.url,
                    record.outcome,
                    f"{record.latency_ms:.3f}",
                    record.body_bytes,
                ]
            )


class LiveReporter:
    """Renders a once-per-second status line during a run.

    On a TTY the line updates in place with a carriage return. On other
    streams (log files, pipes) each update is printed on its own line.
    Output goes to stderr by default so stdout stays clean for the
    summary.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream: TextIO = stream if stream is not None else sys.stderr
        try:
            self._tty = self._stream.isatty()
        except (AttributeError, ValueError):
            self._tty = False
        self._last_width = 0

    def update(
        self,
        *,
        elapsed: float,
        active_workers: int,
        total_workers: int,
        done: int,
        rps: float,
        p95_ms: float | None,
    ) -> None:
        p95_text = f"{p95_ms:.1f} ms" if p95_ms is not None else "-"
        line = (
            f"{elapsed:6.1f}s  workers {active_workers}/{total_workers}"
            f"  done {done}  rps {rps:.1f}  p95 {p95_text}"
        )
        if self._tty:
            padded = line.ljust(self._last_width)
            self._last_width = len(line)
            self._stream.write(f"\r{padded}")
        else:
            self._stream.write(line + "\n")
        self._stream.flush()

    def finish(self) -> None:
        """Terminate the in-place line so the summary starts cleanly."""
        if self._tty and self._last_width:
            self._stream.write("\n")
            self._stream.flush()
        self._last_width = 0
