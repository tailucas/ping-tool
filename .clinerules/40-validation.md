# Validation and testing

There is **no CI**. Validation is (a) the offline `pytest` suite, which is the fast
gate, and (b) a manual throwaway instance on `<host>`, which must not disturb the
production probe.

## Test suite (offline, no network, no privileges)

`tests/` mocks ICMP and drives the real `BaseHTTPRequestHandler`, so it exercises
status codes, JSON bodies and `Content-length` exactly as a monitoring probe sees
them. It needs no interface, no privileges and no network.

```bash
make verify              # uv lock --check + pytest + compileall: the fast gate
make test                # the suite on its own (24 passed, 8 xfailed when healthy)
make sync                # uv sync --frozen, i.e. install exactly the lockfile
```

Rules for the suite:

- `pytest` is the only test framework; it lives in the `dev` dependency group and
  must not be added to `[project.dependencies]`.
- Patch the module attributes (`app.__main__.ping`, `app.__main__.traceroute`,
  `app.__main__.get_ip_org`) — never shell out to real ICMP, never require root.
- `xfail_strict = true` is set in `pyproject.toml`. The `xfail` tests encode the
  "open" rows of the regression table below; when a defect is fixed, delete its
  marker **and** update this file in the same change, otherwise the run fails with
  `XPASS(strict)`.

## Static checks

```bash
make verify                             # lockfile + tests + compileall (preferred)
python3 -m compileall -q app            # syntax only
python3 -m py_compile app/__main__.py   # single file
```

Do not commit a `.venv` (it is in `.gitignore`); `make sync` / `uv sync --frozen`
recreate it from `uv.lock` and `.python-version` (3.11).

## Run a throwaway instance on `<host>`

Never experiment on port 8080. Start a second instance on a spare port:

```bash
ssh <host>
cd <app-dir>
make sync-prod
APP_NAME=ping-tool-test SERVER_ADDRESS=127.0.0.1 SERVER_PORT=18080 \
  timeout 120 .venv/bin/python -m app >/tmp/pt-test.log 2>&1 &
```

Notes:

- `APP_NAME` is mandatory (`app/__init__.py` fails at import without it).
- The app logs to `/dev/log`, **not** to `/tmp/pt-test.log`, whenever `/dev/log` exists.
  Watch output with `sudo journalctl -f | grep ping-tool-test` or set
  `SYSLOG_ADDRESS=udp://127.0.0.1:5514` and listen there.
- Kill it by PID (`kill <pid>` or let `timeout` expire). **Do not** use
  `pkill -f "python -m app"` over SSH: the pattern matches your own remote shell command
  and kills the session (observed: exit code 143).
- A test instance can survive its `timeout`/SIGTERM (the handler calls
  `server.shutdown()` and the process may linger, still holding the port). If the port
  stays bound after `timeout`, confirm the PID with `ss -ltnp` and `kill -9`. A stale
  instance will otherwise serve **old** code and make a fix look broken.

## Request recipes

```bash
# success path (mobile circuit)
curl -sS -w '\nHTTP %{http_code}\n' \
  -H 'Host: arbitrary.internet.host.com' -H 'Source: eth1' -H 'MinLatencyMs: 10' \
  http://127.0.0.1:18080/ping

# traceroute with route/org constraints
# 198.51.100.1 / 203.0.113.1 are placeholders for the backup-AP and home gateways;
# AS12345 / AS54321 stand in for the expected and forbidden provider orgs.
curl -sS -w '\nHTTP %{http_code}\n' \
  -H 'Host: arbitrary.internet.host.com' -H 'Source: eth1' -H 'MinLatencyMs: 10' \
  -H 'RouteIncludeCsv: 198.51.100.1' -H 'RouteExcludeCsv: 203.0.113.1' \
  -H 'HopsMustIncludeOrg: AS12345' -H 'HopsMustExcludeOrg: AS54321' \
  http://127.0.0.1:18080/traceroute
```

## Regression expectations

Bugs reproduce without any special hardware — use a TEST-NET address for "no traffic"
(`192.0.2.1` is unroutable) and `.invalid` for DNS failure:

| Test | Headers | Expectation | Status |
| --- | --- | --- | --- |
| No traffic possible | `Host: 192.0.2.1`, `Source: eth1`, **no** `MinLatencyMs` | `503`, reason `No ICMP replies ... loss 100.0%`, empty `rtts` | **fixed** |
| No traffic, latency enforced | `Host: 192.0.2.1`, `MinLatencyMs: 10` | `503` with the *no replies* reason, not a latency message | **fixed** |
| Passing | `<backup-gateway>`, `MinLatencyMs: 0` | `200` with three `rtts` | works |
| Latency below threshold | `<backup-gateway>`, `MinLatencyMs: 10` | `503`, reason names the minimum RTT, `rtts` retained in the body | **fixed** |
| Route/trace passing | `RouteIncludeCsv: <backup-gateway>`, `RouteExcludeCsv: <home-gateway>`, `HopsMustIncludeOrg: AS12345` | `200`, last hop in the body | works |
| Forbidden gateway in trace | `RouteExcludeCsv: <home-gateway>` matched | `503`, reason `Hosts forbidden from route` | works |
| Missing required gateway | `RouteIncludeCsv` not present in the trace | `503`, reason `Hosts missing from route` | works |
| DNS failure | `Host: nonexistent.invalid` | `503`, `dns_failure`, **no traceback in journal** | open: `500` + `NameLookupError` traceback |
| Bad interface | `Source: bogus0` | `400`, `bad_source` | open: `500` `[Errno 19] No such device` |
| Missing target | no `Host` | `400`, `missing_host` | open: `500` `NameLookupError` traceback |
| Unknown path | `GET /nope` | `4xx` | open: `200 {"result":"OK"}` |
| Failed trace body | any `/traceroute` that returns `503` | measurement key (`traceroute`) kept in the body | open: failure replaces the whole dict |
| CSV whitespace | `RouteIncludeCsv: 'a, b'` | `{'a', 'b'}` | open: `{'a', ' b'}` |
| Empty trace | destination never answers, `traceroute()` returns `[]` | non-2xx, no measurements | open: `200` with no measurements |
| Forbidden org label | `HopsMustExcludeOrg: AS54321` matched | reason says `Orgs forbidden from route` | open: reported under `Hosts forbidden` |

Treat any "open" row as a to-do list; a change that moves a row toward the expectation is
a bug fix, and this table must be updated with it — including deleting the matching
`xfail` marker in `tests/test_app.py`.

Cleared rows are covered by name: `test_ping_no_replies_is_a_circuit_verdict_not_a_crash`,
`test_ping_latency_below_minimum_fails_and_keeps_measurements`,
`test_traceroute_success_satisfies_route_and_org_constraints`,
`test_traceroute_forbidden_gateway_fails_the_test`,
`test_traceroute_missing_required_gateway_fails_the_test`.

## Before declaring work done

1. `make verify` passes (lockfile agrees, tests pass, no `XPASS(strict)`, sources
   compile).
2. `make help` still lists every target with a description (new work adds one).
3. If `ping-tool.service.in` changed: `make unit` renders and
   `systemd-analyze verify build/ping-tool.service` passes.
4. Throwaway instance starts and logs `Listening on ...`.
5. Each affected endpoint exercised with `curl`, including at least one negative case.
6. Prod service still `active` and still listening on 8080
   (`make status`, or `systemctl is-active ping-tool` + `ss -ltn | grep 8080`).
7. `git diff` contains no secrets, no `__pycache__`, no `.venv`, no `.env`, no
   `docker-compose.yml`, no `build/`, no `uv.lock` churn from a sync that was not
   intentional, no debug prints.
