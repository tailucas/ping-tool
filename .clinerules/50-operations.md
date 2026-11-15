# Operations and deployment

## Host and service

- Host: a Raspberry Pi (`armv6l`), reachable over SSH/Tailscale; referred to as `<host>`.
- App directory `<app-dir>` (a git clone with its own `.venv`).
- Unit `/etc/systemd/system/ping-tool.service`, `Restart=always`, `RestartSec=30`,
  `After=network.target tailscaled.service`.
- `ExecStart=<home>/.local/bin/poetry run app`, `WorkingDirectory=<app-dir>`.
- **No `User=` is set, so it runs as root** — required for `SO_BINDTODEVICE` and the
  raw sockets used by `traceroute()`. Adding a non-root user requires
  `AmbientCapabilities=CAP_NET_RAW` plus a review of the fork's binding path.

## Observability

```bash
ssh <host>
sudo journalctl -u ping-tool -f
sudo journalctl -u ping-tool --since "-1 hour" | grep -E "ERROR|NameLookupError"
systemctl status ping-tool
ss -ltn | grep 8080
```

Log lines to recognise:

- `Listening on 0.0.0.0:8080.` — startup OK.
- `GET /ping - headers [...]` — the header *names* only, not values.
- `Ping <target> from <iface>...` / `Traceroute to <target>...` — test started.
- `... is alive? False ... loss 100.0%.` — circuit carried no traffic.
- `Issue processing command.` + traceback — unexpected exception (see below).
- `192.0.2.5 - - [...] "GET /ping HTTP/1.1" 500 -` — the HTTP verdict (address is an
  example client).

`journalctl` shows the `ping-tool Server [LEVEL]` prefix from
`%(name)s %(threadName)s [%(levelname)s] %(message)s`; `Server` is the handler thread.

## Deploying a change

```bash
ssh <host>
cd <app-dir>
git pull --ff-only
poetry install --sync          # only if pyproject.toml/poetry.lock changed
sudo systemctl restart ping-tool
sudo journalctl -u ping-tool -n 20 --no-pager
```

Then verify from the home network that the probe endpoint answers, e.g.

```bash
curl -sS -w '\nHTTP %{http_code}\n' -H 'Host: arbitrary.internet.host.com' \
  -H 'Source: eth1' -H 'MinLatencyMs: 10' http://<host-lan-ip>:8080/ping
```

Do not restart the service to "test" a change; use the throwaway instance recipe in
`40-validation.md`.

## Configuration and secrets

- Runtime configuration is **environment only**. `config/app.conf` is for the container
  base image and is not read by this code.
- `IPINFO_TOKEN` is a secret: never commit `.env`, never echo it into a log line, never
  put it in a `curl` example in the README.
- Container path (`Taskfile.yml` + `docker-compose.template` + `app.yml`) is a
  convenience build; production does not use it. Keep the two paths behaviourally
  equivalent if you touch either.

## Known defects (fix deliberately, don't paper over)

The "no traffic" and "latency-before-aliveness" defects were fixed in `do_GET`: `/ping`
now checks `host.is_alive` first, reports `No ICMP replies ... loss 100.0%`, and returns
`503`. Remaining known issues:

1. `NameLookupError` (resolver unreachable or bad name) still falls into the generic
   `except Exception` → full traceback at ERROR and a raw `reason` string, returned as
   `500`. It should be a `503` with `dns_failure` (`app/__main__.py:256-259`).
2. Unknown paths log a warning but still return `200 {"result": "OK"}`
   (`app/__main__.py:254-255`).
3. `forbidden_orgs` is created but never populated; an org name is appended to
   `forbidden_hosts` (`app/__main__.py:208, 235`) — "Orgs forbidden from route" can
   never fire.
4. `_split_to_set()` does not strip whitespace, so `'a, b'` → `{'a', ' b'}`
   (`app/__main__.py:112-118`).
5. `/traceroute` only evaluates latency on the last hop seen; if `hops` is empty the test
   passes with no measurements (`app/__main__.py:245-253`).
6. A missing `Host` header and a bad `Source` value are still `500`s; they should be
   `400` (`missing_host`, `bad_source`).
8. `get_ip_org()` is called per hop with a 3 s timeout, on a single-threaded server.

## Repository workflow

- Branch `main`, remote `origin git@github.com:tailucas/ping-tool.git`.
- Commit subjects are short, imperative, capitalised, no prefix:
  `Fix missing keyword.`, `Improve logging.`, `Include support for org checks on IP ownership.`
- Keep commits focused; do not mix refactors with behaviour changes.
- `README.md` is the product documentation (design rationale, probe layout, header
  examples, install steps). Behaviour changes belong there in the same commit.
