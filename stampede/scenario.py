"""Scenario definitions: which requests to send and how often.

A scenario is a list of request specs. In single URL mode the CLI builds
a one element scenario; with ``--scenario`` the list is loaded from a
JSON file and each iteration picks a spec by weighted random choice.
"""

from __future__ import annotations

import json
import math
import random
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .client import parse_url

__all__ = [
    "ALLOWED_METHODS",
    "RequestSpec",
    "ScenarioError",
    "WeightedPicker",
    "load_scenario",
    "make_spec",
]

ALLOWED_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "DELETE", "PATCH")
_ALLOWED_KEYS = frozenset({"method", "url", "headers", "body", "weight"})


class ScenarioError(Exception):
    """Raised when a scenario file is missing, malformed, or invalid."""


@dataclass(slots=True)
class RequestSpec:
    """One request template: method, URL, headers, body, and pick weight."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    weight: float = 1.0


def make_spec(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    weight: float = 1.0,
) -> RequestSpec:
    """Build a validated RequestSpec. Raises ScenarioError on bad input."""
    if not isinstance(url, str) or not url:
        raise ScenarioError("'url' is required and must be a string")
    try:
        parse_url(url)
    except ValueError as exc:
        raise ScenarioError(str(exc)) from exc
    normalized_method = method.upper()
    if normalized_method not in ALLOWED_METHODS:
        allowed = ", ".join(ALLOWED_METHODS)
        raise ScenarioError(f"unsupported method {method!r} (expected one of: {allowed})")
    if isinstance(weight, bool) or not isinstance(weight, (int, float)):
        raise ScenarioError("'weight' must be a number")
    if not math.isfinite(weight) or weight <= 0:
        raise ScenarioError(f"'weight' must be a positive finite number, got {weight!r}")
    return RequestSpec(
        url=url,
        method=normalized_method,
        headers=dict(headers) if headers else {},
        body=body,
        weight=float(weight),
    )


def load_scenario(path: str | Path) -> list[RequestSpec]:
    """Load and validate a JSON scenario file.

    Accepts either a bare list of request objects or an object with a
    ``requests`` list. Raises ScenarioError with a message naming the
    offending entry when validation fails.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioError(f"cannot read scenario file {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"scenario file {path} is not valid JSON: {exc}") from exc
    if isinstance(data, dict):
        entries = data.get("requests")
    elif isinstance(data, list):
        entries = data
    else:
        entries = None
    if not isinstance(entries, list) or not entries:
        raise ScenarioError(
            "scenario must be a JSON list of requests, "
            "or an object with a non-empty 'requests' list"
        )
    return [_spec_from_entry(entry, index) for index, entry in enumerate(entries)]


def _spec_from_entry(entry: Any, index: int) -> RequestSpec:
    where = f"request #{index + 1}"
    if not isinstance(entry, dict):
        raise ScenarioError(f"{where}: each request must be a JSON object")
    unknown = set(entry) - _ALLOWED_KEYS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ScenarioError(f"{where}: unknown keys: {names}")

    headers = entry.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
    ):
        raise ScenarioError(f"{where}: 'headers' must be an object of string values")

    raw_body = entry.get("body")
    if raw_body is not None and not isinstance(raw_body, str):
        raise ScenarioError(f"{where}: 'body' must be a string")
    body = raw_body.encode("utf-8") if raw_body is not None else None

    method = entry.get("method", "GET")
    if not isinstance(method, str):
        raise ScenarioError(f"{where}: 'method' must be a string")

    try:
        return make_spec(
            entry.get("url"),
            method=method,
            headers=headers,
            body=body,
            weight=entry.get("weight", 1.0),
        )
    except ScenarioError as exc:
        raise ScenarioError(f"{where}: {exc}") from None


class WeightedPicker:
    """Weighted random selection over request specs.

    Selection is driven entirely by the supplied ``random.Random``, so a
    seeded generator produces a reproducible request sequence.
    """

    def __init__(self, specs: Sequence[RequestSpec], rng: random.Random | None = None) -> None:
        if not specs:
            raise ValueError("at least one request spec is required")
        self._specs = list(specs)
        self._rng = rng if rng is not None else random.Random()
        self._cumulative: list[float] = []
        total = 0.0
        for spec in self._specs:
            if spec.weight <= 0:
                raise ValueError(f"weights must be positive, got {spec.weight!r}")
            total += spec.weight
            self._cumulative.append(total)
        self._total = total

    def pick(self) -> RequestSpec:
        if len(self._specs) == 1:
            return self._specs[0]
        point = self._rng.random() * self._total
        index = bisect_right(self._cumulative, point)
        return self._specs[min(index, len(self._specs) - 1)]
