# Validation and testing

There is **no test suite and no CI** in this repo. Validation is manual and must not
disturb the production probe on `<host>`.

## Static checks (safe anywhere)

```bash
python3 -m compileall -q app            # syntax only
python3 -m py_compile app/__main__.py   # single file
```

There is no local virtualenv in the working copy; `icmplib` is only installed on
the deployed host (`<app-dir>/.venv`). Do not add a `.venv` to the repo.

## Run a throwaway instance on `<host>`

Never experiment on port 8080. Start a second instance on a spare port:

```bash
ssh <host>
cd <app-dir>
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
| DNS failure | `Host: nonexistent.invalid` | `503`, `dns_failure`, **no traceback in journal** | open: `500` + `NameLookupError` traceback |
| Bad interface | `Source: bogus0` | `400`, `bad_source` | open: `500` `[Errno 19] No such device` |
| Missing target | no `Host` | `400`, `missing_host` | open: `500` `NameLookupError` traceback |
| Unknown path | `GET /nope` | `4xx` | open: `200 {"result":"OK"}` |

Treat any "open" row as a to-do list; a change that moves a row toward the expectation is
a bug fix, and this table must be updated with it.

## Before declaring work done

1. `python3 -m compileall -q app` passes.
2. Throwaway instance starts and logs `Listening on ...`.
3. Each affected endpoint exercised with `curl`, including at least one negative case.
4. Prod service still `active` and still listening on 8080
   (`systemctl is-active ping-tool`, `ss -ltn | grep 8080`).
5. `git diff` contains no secrets, no `__pycache__`, no `.venv`, no debug prints.
