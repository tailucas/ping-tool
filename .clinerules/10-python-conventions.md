# Python conventions

Match the existing single-module style in `app/__main__.py`. There is **no formatter,
linter or type checker configured** (no ruff/black/mypy in `pyproject.toml`), so
consistency with the surrounding code is the standard — do not reformat whole files.

## Imports

Three groups, separated by blank lines, roughly alphabetical inside each group:

1. stdlib (`json`, `logging`, `logging.handlers`, `ipaddress`, `os`, `signal`,
   `socket`, `sys`, ...), then a second block for `functools`, `http.*`,
   `urllib.parse` as the file already does;
2. third-party (`requests`, `icmplib`);
3. local (`from . import APP_NAME`).

Import names directly (`from icmplib import ping, traceroute, resolve`) rather than
using `icmplib.` prefixes in the body.

## Constants

- Module-level constants are `UPPER_SNAKE_CASE`.
- Header names are declared once as `HEADER_*` constants next to `OS_ENV_IPINFO_TOKEN`
  and referenced symbolically (e.g. `HEADER_MIN_LATENCY_MS`). **Never** hard-code a
  header string in a second place.

## Logging

- One module logger: `log = logging.getLogger(APP_NAME)`. Use `log.<level>(f'...')`.
- The format is fixed by the handler:
  `%(name)s %(threadName)s [%(levelname)s] %(message)s`. Do not add timestamps,
  prefixes or emojis.
- **Never use `print()`.** Under systemd the process logs to `/dev/log`, so stdout is
  lost; `log` is the only place output is observable.
- Reserve `log.exception(...)` / `exc_info=True` for genuine defects. An expected
  network condition (DNS failure, 100 % loss, bad `Source` value) is a **test result**,
  not an exception: log one WARNING/INFO line and return a structured failure.
- Keep messages parseable and factual, in the current voice:
  `f'{host_name} ({host.address}) is alive? {host.is_alive} with ... loss {host.packet_loss*100}%.'`

## Functions and classes

- Helpers that are request-scoped live on `WebRequestHandler` and are named with a
  leading underscore (`_split_to_set`, `_check_latency`, `_included_net`,
  `_included_org`).
- Request parsing uses `@cached_property` (`url`, `query_data`, `post_data`,
  `form_data`, `cookies`) so each parse happens once per request.
- Free functions take keyword arguments at call sites (`get_ip_org(ip=...)`,
  `ping(address=..., count=...)`, `_check_latency(test_latency=...)`). Keep that style;
  it is used consistently and makes ICMP calls self-documenting.
- Small helpers return `None` for "not applicable" and a dict
  `{'result': 'error', 'reason': '...'}` for "failed", as `_check_latency` does.
  Do not raise for expected outcomes.

## Language features

- Python 3.11: use `match`/`case` for endpoint routing (as in `do_GET`), f-strings
  everywhere, `typing.List` style annotations for containers, and `cached_property`.
- Avoid adding dataclasses/TypedDict/Enums for the sake of it; the response is plain
  dicts serialised with `json.dumps`.

## Error handling

- One broad `except Exception as e:` at the request boundary is acceptable **only** as
  a last-resort guard; it must log (`log.exception`) and return a 5xx JSON body.
- For network conditions, catch the specific `icmplib` exceptions
  (`NameLookupError`, `SocketAddressError`, `SocketPermissionError`,
  `ICMPSocketError`, `TimeoutExceeded`) before the broad handler. See
  `30-icmplib-network-semantics.md`.
- Never use a bare `except:`; never swallow an exception without either logging it or
  deliberately documenting why it is irrelevant (the existing `# don't care, tested
  later` comment is the accepted idiom).

## Tests

Tests live in `tests/` and run with `pytest` (`uv run --frozen pytest`). They are not
shipped: `pytest` belongs to the `dev` dependency group, never to
`[project.dependencies]`.

- `tests/conftest.py` sets `APP_NAME` before `app` is imported and clears
  `IPINFO_TOKEN`; do not rely on the ambient environment in a test.
- Request-level tests build a raw request, serve it through a `socketpair` with the
  real `WebRequestHandler` (subclassed only to silence the access log), and parse the
  reply with `http.client.HTTPResponse`. Assert on the status code and the decoded JSON
  body, not on log lines.
- Patch the module attributes the handler calls — `app.__main__.ping`,
  `app.__main__.traceroute`, `app.__main__.get_ip_org` — and use the real `icmplib`
  `Host`/`Hop` classes so latency and liveness semantics stay faithful.
- Test names are full sentences describing the verdict:
  `test_ping_no_replies_is_a_circuit_verdict_not_a_crash`.
- Known defects are marked `pytest.mark.xfail` with a reason naming
  `40-validation.md`. `xfail_strict` is on, so a fixed defect must have its marker
  removed in the same change.

## Style details to preserve

- 4-space indentation, single quotes for short strings, double quotes only where the
  file already uses them (`self.headers.get("Content-Length", 0)`).
- Long f-string logs continue with a trailing `\` and align to the opening brace, as in
  the `is alive?` log lines.
- Keep `self.protocol_version = 'HTTP/1.0'` and the explicit `Content-length` header;
  they are deliberate (one request per connection, no chunking).
