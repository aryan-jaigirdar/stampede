"""Run a small, reproducible request mix against this demo's own local server.

Use ``python3 -m examples.demo`` from the repository root. Timing depends on
hardware. The request mix is seeded; measured latency is never fabricated.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from stampede.report import format_summary, write_csv, write_json
from stampede.runner import RunConfig, run_load
from stampede.scenario import make_spec


class DemoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        status = 429 if self.path == "/busy" else 200
        body = json.dumps({"status": "busy" if status == 429 else "ok"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", metavar="FILE", help="save the measured summary")
    parser.add_argument("--csv", metavar="FILE", help="save every request")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        specs = [make_spec(origin + "/health", weight=4), make_spec(origin + "/busy", weight=1)]
        config = RunConfig(concurrency=4, total_requests=30, duration=None, rps=100, seed=7)
        summary = asyncio.run(run_load(specs, config, record_requests=args.csv is not None))
        assert summary.completed == 30 and summary.failed == 0
        assert sum(summary.status_counts.values()) == 30
        assert summary.non_2xx > 0
        print("stampede · 30 real requests to an ephemeral local HTTP server\n")
        print(format_summary(summary))
        print("\nThe 429s are complete HTTP responses, not transport failures.")
        print("Latency and throughput depend on this machine. No public host was contacted.")
        if args.json:
            write_json(summary, args.json)
        if args.csv:
            write_csv(summary.records, args.csv)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
