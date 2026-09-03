"""Command line interface for stampede."""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
from dataclasses import replace
from typing import Sequence

from . import __version__
from .client import parse_url
from .report import LiveReporter, format_summary, write_csv, write_json
from .runner import RunConfig, run_load
from .scenario import (
    ALLOWED_METHODS,
    RequestSpec,
    ScenarioError,
    load_scenario,
    make_spec,
    merge_headers,
)

__all__ = ["build_parser", "main", "parse_header_args"]

_DEFAULT_DURATION = 10.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stampede",
        description=(
            "An asynchronous HTTP load generator. Point it at a single URL, or "
            "describe a weighted mix of requests with a JSON scenario file."
        ),
        epilog=(
            "Only run stampede against services you own or are explicitly "
            "authorized to load test."
        ),
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="target URL, e.g. http://127.0.0.1:8080/ (omit when using --scenario)",
    )
    parser.add_argument(
        "--scenario",
        metavar="FILE",
        help="JSON scenario file describing a weighted list of requests",
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=10,
        metavar="N",
        help="number of concurrent workers (default: 10)",
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help=f"how long to run (default: {_DEFAULT_DURATION:g} unless --requests is given)",
    )
    parser.add_argument(
        "-n",
        "--requests",
        type=int,
        default=None,
        metavar="N",
        help="stop after this many requests in total",
    )
    parser.add_argument(
        "--ramp",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="ramp workers up linearly from 1 to the full concurrency over this long",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="per request timeout (default: 10)",
    )
    parser.add_argument(
        "-X",
        "--method",
        default="GET",
        metavar="METHOD",
        help="HTTP method in single URL mode (default: GET)",
    )
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        metavar="'NAME: VALUE'",
        help="extra request header, repeatable; in scenario mode a per-request header overrides it",
    )
    parser.add_argument(
        "--body",
        default=None,
        metavar="TEXT",
        help="request body in single URL mode",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        metavar="FILE",
        help="also write the full results to FILE as JSON",
    )
    parser.add_argument(
        "--csv",
        dest="csv_path",
        metavar="FILE",
        help="also write one row per request to FILE as CSV",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help="seed the scenario RNG for a reproducible request mix",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS certificate verification (self signed test hosts)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress the live status line",
    )
    parser.add_argument("--version", action="version", version=f"stampede {__version__}")
    return parser


def parse_header_args(raw_headers: Sequence[str]) -> dict[str, str]:
    """Parse repeated ``'Name: value'`` header arguments into a dict.

    Each argument is split on the first colon and both sides are trimmed,
    so a value may itself contain colons. Raises ValueError with a clear
    message when an argument has no colon or an empty name.
    """
    headers: dict[str, str] = {}
    for raw in raw_headers:
        name, sep, value = raw.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"invalid header {raw!r}, expected 'Name: value'")
        headers[name.strip()] = value.strip()
    return headers


def _load_specs(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[RequestSpec]:
    if args.url and args.scenario:
        parser.error("give either a target URL or --scenario, not both")
    if not args.url and not args.scenario:
        parser.error("a target URL or a --scenario file is required")

    try:
        default_headers = parse_header_args(args.header)
    except ValueError as exc:
        parser.error(str(exc))

    if args.scenario:
        if args.body is not None:
            parser.error("--body applies only to single URL mode; put a body in the scenario file")
        try:
            specs = load_scenario(args.scenario)
        except ScenarioError as exc:
            parser.error(str(exc))
        if default_headers:
            specs = [
                replace(spec, headers=merge_headers(default_headers, spec.headers))
                for spec in specs
            ]
        return specs

    body = args.body.encode("utf-8") if args.body is not None else None
    method = args.method.upper()
    if method not in ALLOWED_METHODS:
        parser.error(f"unsupported method {args.method!r} (expected one of: {', '.join(ALLOWED_METHODS)})")
    try:
        return [make_spec(args.url, method=method, headers=default_headers, body=body)]
    except ScenarioError as exc:
        parser.error(str(exc))
    raise AssertionError("unreachable")


def _build_config(args: argparse.Namespace, parser: argparse.ArgumentParser) -> RunConfig:
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.requests is not None and args.requests < 1:
        parser.error("--requests must be at least 1")
    if args.duration is not None and args.duration <= 0:
        parser.error("--duration must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.ramp < 0:
        parser.error("--ramp must not be negative")
    duration = args.duration
    if duration is None and args.requests is None:
        duration = _DEFAULT_DURATION
    return RunConfig(
        concurrency=args.concurrency,
        duration=duration,
        total_requests=args.requests,
        ramp=args.ramp,
        timeout=args.timeout,
        insecure=args.insecure,
        seed=args.seed,
    )


def _unresolvable_hosts(specs: Sequence[RequestSpec]) -> list[str]:
    """Return hosts from the specs that do not resolve, without duplicates."""
    checked: set[tuple[str, int]] = set()
    unresolvable: list[str] = []
    for spec in specs:
        parsed = parse_url(spec.url)
        key = (parsed.host, parsed.port)
        if key in checked:
            continue
        checked.add(key)
        try:
            socket.getaddrinfo(parsed.host, parsed.port, type=socket.SOCK_STREAM)
        except OSError:
            unresolvable.append(parsed.host)
    return unresolvable


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    specs = _load_specs(args, parser)
    config = _build_config(args, parser)

    unresolvable = _unresolvable_hosts(specs)
    if unresolvable:
        for host in unresolvable:
            print(f"stampede: cannot resolve host: {host}", file=sys.stderr)
        return 2

    reporter = None if args.quiet else LiveReporter()
    try:
        summary = asyncio.run(
            run_load(
                specs,
                config,
                reporter=reporter,
                record_requests=args.csv_path is not None,
            )
        )
    except KeyboardInterrupt:
        print("stampede: interrupted", file=sys.stderr)
        return 130

    print(format_summary(summary))
    if args.json_path:
        write_json(summary, args.json_path)
    if args.csv_path:
        write_csv(summary.records, args.csv_path)
    if summary.completed == 0:
        return 1
    return 0
