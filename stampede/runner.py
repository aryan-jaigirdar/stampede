"""The concurrency engine: workers, ramp up, stop conditions, live ticks."""

from __future__ import annotations

import asyncio
import contextlib
import random
import signal
import time
from dataclasses import dataclass
from typing import Sequence

from .client import Client, ProtocolError
from .metrics import (
    Collector,
    FAILURE_CONNECTION,
    FAILURE_PROTOCOL,
    FAILURE_TIMEOUT,
    Summary,
)
from .report import LiveReporter
from .scenario import RequestSpec, WeightedPicker

__all__ = ["RunConfig", "run_load"]

_TICK_SECONDS = 1.0


@dataclass(slots=True)
class RunConfig:
    """Knobs for a load run. At least one stop condition is required."""

    concurrency: int = 10
    duration: float | None = 10.0
    total_requests: int | None = None
    ramp: float = 0.0
    timeout: float = 10.0
    insecure: bool = False
    seed: int | None = None


def _ramp_delays(concurrency: int, ramp: float) -> list[float]:
    """Start delays that ramp workers linearly from 1 to ``concurrency``."""
    if concurrency <= 1 or ramp <= 0:
        return [0.0] * concurrency
    step = ramp / (concurrency - 1)
    return [index * step for index in range(concurrency)]


async def run_load(
    specs: Sequence[RequestSpec],
    config: RunConfig,
    *,
    reporter: LiveReporter | None = None,
) -> Summary:
    """Run the load model and return a Summary of everything recorded.

    Workers loop until the duration elapses or the request cap is
    reached; in-flight requests are allowed to finish. SIGINT stops the
    run gracefully where the platform supports signal handlers, so an
    interrupted run still reports what it measured.
    """
    if config.duration is None and config.total_requests is None:
        raise ValueError("a duration or a total request cap is required")
    if config.concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    collector = Collector()
    picker = WeightedPicker(specs, random.Random(config.seed))
    stop = asyncio.Event()
    started = time.perf_counter()
    active_workers = 0
    claimed = 0

    def claim() -> bool:
        nonlocal claimed
        if config.total_requests is not None and claimed >= config.total_requests:
            return False
        claimed += 1
        return True

    async def worker(start_delay: float) -> None:
        nonlocal active_workers
        if start_delay > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=start_delay)
                return  # stopped before this worker ramped in
            except TimeoutError:
                pass
        active_workers += 1
        client = Client(timeout=config.timeout, insecure=config.insecure)
        try:
            while not stop.is_set():
                if not claim():
                    stop.set()
                    break
                spec = picker.pick()
                request_started = time.perf_counter()
                try:
                    response = await client.request(
                        spec.method, spec.url, headers=spec.headers, body=spec.body
                    )
                except TimeoutError:
                    collector.record_failure(FAILURE_TIMEOUT)
                except ProtocolError:
                    collector.record_failure(FAILURE_PROTOCOL)
                except OSError:
                    collector.record_failure(FAILURE_CONNECTION)
                else:
                    latency_ms = (time.perf_counter() - request_started) * 1000.0
                    collector.record_success(latency_ms, response.status, len(response.body))
        finally:
            active_workers -= 1
            await client.aclose()

    async def timer() -> None:
        assert config.duration is not None
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.duration)
        except TimeoutError:
            stop.set()

    async def ticker() -> None:
        assert reporter is not None
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=_TICK_SECONDS)
                return
            except TimeoutError:
                pass
            window = collector.window()
            reporter.update(
                elapsed=time.perf_counter() - started,
                active_workers=active_workers,
                total_workers=config.concurrency,
                done=collector.attempts,
                rps=window.attempts / _TICK_SECONDS,
                p95_ms=window.p95_ms,
            )

    loop = asyncio.get_running_loop()
    signal_installed = False
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        signal_installed = True
    except (NotImplementedError, RuntimeError):
        pass  # not supported on this platform or loop; Ctrl+C aborts instead

    side_tasks: list[asyncio.Task[None]] = []
    if config.duration is not None:
        side_tasks.append(asyncio.create_task(timer()))
    if reporter is not None:
        side_tasks.append(asyncio.create_task(ticker()))
    workers = [
        asyncio.create_task(worker(delay))
        for delay in _ramp_delays(config.concurrency, config.ramp)
    ]
    try:
        await asyncio.gather(*workers)
    finally:
        stop.set()
        for task in side_tasks:
            task.cancel()
        await asyncio.gather(*side_tasks, return_exceptions=True)
        if signal_installed:
            with contextlib.suppress(ValueError, RuntimeError):
                loop.remove_signal_handler(signal.SIGINT)
        if reporter is not None:
            reporter.finish()

    return collector.summarize(time.perf_counter() - started)
