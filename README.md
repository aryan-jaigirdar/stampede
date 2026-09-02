# stampede

An asynchronous HTTP load testing CLI written in pure Python. No third party
dependencies, no compiled extensions: the HTTP/1.1 client, the concurrency
engine, and the metrics pipeline are all built directly on the standard
library.

```console
$ stampede http://127.0.0.1:8080/ --concurrency 50 --duration 30
```

## Why

I built stampede to load test my own services and to pair with synthetic
monitoring: the probes tell you a service is up, stampede tells you where it
falls over. I also wanted a tool that installs anywhere Python runs. There is
no dependency resolution, no virtualenv ceremony, and nothing to compile. If
you have Python 3.11 or newer, you have stampede.

It is deliberately small and readable. The raw asyncio HTTP client is the
core of the project: request serialization, response parsing, chunked
transfer decoding, and keep-alive connection reuse are all implemented by
hand and covered by tests against real sockets.

## Requirements

Python 3.11 or newer. Nothing else.

## Install

```console
$ pip install .
```

Or skip installation entirely and run it from a checkout:

```console
$ python3 -m stampede http://127.0.0.1:8080/ -c 20 -d 10
```

## Quickstart

Hammer one URL with 25 workers for 60 seconds, ramping the workers up over
the first 10 seconds, and save the full results as JSON:

```console
$ stampede http://127.0.0.1:8080/health -c 25 -d 60 --ramp 10 --json results.json
```

Send 10,000 POST requests as fast as 8 workers can manage:

```console
$ stampede http://127.0.0.1:8080/ingest -X POST \
    -H "Content-Type: application/json" \
    --body '{"probe": true}' \
    -c 8 -n 10000
```

Replay a weighted mix of requests from a scenario file:

```console
$ stampede --scenario examples/checkout.json -c 40 -d 120
```

While a run is active, stampede prints a single updating status line:

```
  12.0s  workers 25/25  done 14822  rps 1267.0  p95 21.4 ms
```

## CLI reference

| Option | Default | Description |
| --- | --- | --- |
| `url` | | Target URL. Omit when using `--scenario`. |
| `--scenario FILE` | | JSON scenario file describing a weighted list of requests. |
| `-c, --concurrency N` | 10 | Number of concurrent workers. |
| `-d, --duration SECONDS` | 10 | How long to run. The default applies only when `--requests` is not given. |
| `-n, --requests N` | | Stop after this many requests in total. Combine with `--duration` and whichever limit is hit first ends the run. |
| `--ramp SECONDS` | 0 | Ramp workers up linearly from 1 to the full concurrency over this long. |
| `-t, --timeout SECONDS` | 10 | Per request timeout, applied per attempt. |
| `-X, --method METHOD` | GET | HTTP method in single URL mode. |
| `-H, --header 'NAME: VALUE'` | | Extra request header in single URL mode. Repeatable. |
| `--body TEXT` | | Request body in single URL mode. |
| `--json FILE` | | Also write the full results to FILE as JSON. |
| `--seed N` | | Seed the scenario RNG for a reproducible request mix. |
| `--insecure` | off | Skip TLS certificate verification, for self signed test hosts. |
| `-q, --quiet` | off | Suppress the live status line. |
| `--version` | | Print the version and exit. |

Exit codes: `0` for a normal run, `1` when zero requests completed (the
target is effectively down), `2` for usage errors and unresolvable hosts,
`130` when interrupted before the run started. During a run, Ctrl+C stops
the workers gracefully and the summary still prints with everything measured
so far.

## Scenario files

A scenario is a JSON file with a list of request templates. Each iteration,
every worker picks one template by weighted random choice. With weights 6, 3,
and 1 below, roughly 60 percent of requests hit the product listing.

```json
{
  "requests": [
    {
      "method": "GET",
      "url": "http://127.0.0.1:8080/products",
      "weight": 6
    },
    {
      "method": "GET",
      "url": "http://127.0.0.1:8080/products/42",
      "headers": { "Accept": "application/json" },
      "weight": 3
    },
    {
      "method": "POST",
      "url": "http://127.0.0.1:8080/cart",
      "headers": { "Content-Type": "application/json" },
      "body": "{\"product_id\": 42, \"quantity\": 1}",
      "weight": 1
    }
  ]
}
```

Fields per request: `url` (required), `method` (GET, POST, PUT, DELETE, or
PATCH; default GET), `headers` (object of strings), `body` (string, sent as
UTF-8), and `weight` (positive number, default 1). Unknown keys are rejected
so typos fail loudly instead of silently skewing the mix. A bare JSON list of
request objects works too. Pass `--seed` to make the mix reproducible across
runs.

## Sample output

```
Run summary
  elapsed              30.01 s
  requests completed   38122
  requests failed      4
  requests per second  1270.3
  bytes received       44.6 MiB

Latency (ms)
  mean        18.6
  p50         16.2
  p90         28.9
  p95         34.0
  p99         61.5
  max        212.4

Status codes
  200        37984
  429          138

Failures
  timeout                  3
  connection error         1
  non-2xx responses      138
```

`--json` writes the same data in a stable machine readable shape, including
the full status code histogram and failure counts by category. In both
outputs, "completed" counts requests that received a full response of any
status, "failed" counts transport level failures (timeouts, connection
errors, protocol errors), and non-2xx responses are broken out separately.

## Design

**Raw asyncio HTTP client.** Requests go over `asyncio.open_connection`
streams; there is no `http.client` or third party HTTP stack underneath. The
client serializes requests by hand, parses the status line and headers, and
frames bodies by `Content-Length`, by chunked transfer decoding (including
chunk extensions and trailers), or by reading to EOF when the server closes
the connection. TLS comes from the standard `ssl` module.

**Keep-alive with stale connection retry.** Each worker holds one persistent
connection per origin and reuses it across iterations, which is what real
clients do and what keeps the tool fast. A reused keep-alive connection can
always have been closed by the server between requests; when that happens
before any response bytes arrive, the request is retried exactly once on a
fresh connection. Failures after response bytes have arrived are never
retried, so results are not double counted.

**Load model.** N workers run as asyncio tasks on one event loop, each
looping request, record, repeat. `--ramp` staggers worker start times
linearly, `--duration` and `--requests` are both stop conditions, and a
request cap is claimed by workers before sending so the cap is exact, not
approximate. In-flight requests are allowed to finish when the run stops.

**Percentiles.** Latencies are recorded per request in milliseconds and
percentiles use linear interpolation between closest ranks: percentile p maps
to fractional rank `p / 100 * (n - 1)` and the result is interpolated between
the two neighboring order statistics. This matches the default method in
common statistics packages and behaves sensibly for small samples. The live
status line reports p95 over the most recent one second window; the final
summary is computed over the whole run.

**Safety checks.** Every target host must resolve before the run starts, and
the live status line goes to stderr so stdout stays clean for the summary and
for piping.

## Limitations

- HTTP/1.1 only. No HTTP/2, no compression negotiation, no redirects, no
  cookies. Responses are read fully and discarded after counting bytes.
- Single machine, single event loop. Python's async I/O is efficient but one
  process will not saturate a large cluster; for that, run multiple instances
  and aggregate the JSON output.
- Latency is measured from the client, so it includes local scheduling noise
  at very high concurrency.
- The request cap is exact, but `--duration` allows in-flight requests to
  complete, so a run can end slightly after the deadline.

## Test only what you own

Load testing a service you do not own or operate is indistinguishable from a
denial of service attack. Only point stampede at services you own or have
explicit written authorization to test, and prefer isolated environments over
production. The host resolution check and the deliberate lack of any
distributed mode are guardrails, not permission.

## Development

Run the test suite (standard library `unittest`, no test dependencies):

```console
$ python3 -m unittest discover -s tests -v
```

The tests spin up real local HTTP servers, both `http.server` based and raw
asyncio sockets, to exercise parsing, keep-alive reuse, stale connection
retries, chunked encoding, timeouts, and refused connections against actual
network I/O.

## License

MIT. See [LICENSE](LICENSE).
