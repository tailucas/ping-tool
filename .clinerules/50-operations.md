# Operations and deployment

## Host and service

- Host: a Raspberry Pi (`armv6l`), reachable over SSH/Tailscale; referred to as `<host>`.
- App directory `<app-dir>` (a git clone with its own `.venv`).
- Unit `/etc/systemd/system/ping-tool.service`, `Restart=always`, `RestartSec=30`,
  `After=network.target tailscaled.service`.
- `ExecStart=<app-dir>/.venv/bin/app`, `WorkingDirectory=<app-dir>`. The venv is
  provisioned by `make setup` / `make sync-prod` (`uv sync --frozen --no-dev`);
  `uv` is not in the service's process tree or signal path.
- The unit is **generated, never hand-edited**: `ping-tool.service.in` is rendered
  by `make unit` into `build/ping-tool.service` with `APP_DIR=$(CURDIR)` and the
  `APP_NAME`/`SERVER_ADDRESS`/`SERVER_PORT` contract from the Makefile, then
  installed with `make install-unit` (install + `daemon-reload` + `enable` +
  `restart`, then `is-active`). Run it as the normal user — the recipe escalates
  only for those steps — so `build/` and `.venv` stay owned by you.
- `make install-unit` **never touches the network**: it only requires
  `<app-dir>/.venv/bin/app` and says so if it is missing. Dependency syncing is
  `make sync`, which is the one step that needs DNS and PyPI — keep them separate
  so the unit can be installed or repaired while the uplink is degraded.
- The rendered unit reads `<app-dir>/.env` as an optional `EnvironmentFile`; that
  is how `IPINFO_TOKEN` and other optional settings reach the service. A missing
  file is fine, the mandatory variables are baked into `Environment=`.
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

From the checkout, `make status` covers `systemctl status` plus the port check,
and `make logs` follows the journal.

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
uv sync --frozen --no-dev      # only if pyproject.toml/uv.lock/.python-version changed
sudo systemctl restart ping-tool
sudo journalctl -u ping-tool -n 20 --no-pager
```

`make setup` is the host-side equivalent of the uv line plus the unit render, and
`make install-unit` then reloads systemd, re-enables and restarts the service.

Then verify from the home network that the probe endpoint answers, e.g.

```bash
curl -sS -w '\nHTTP %{http_code}\n' -H 'Host: arbitrary.internet.host.com' \
  -H 'Source: eth1' -H 'MinLatencyMs: 10' http://<host-lan-ip>:8080/ping
```

If `ping-tool.service.in` or the Makefile's `APP_NAME`/`SERVER_*` values changed,
`make install-unit` is the deploy step instead: it re-renders the unit, installs
it, reloads systemd, re-enables and restarts the service. `make status` and
`make logs` cover the checks above.

`uv sync` is the **only** network-dependent step. When the uplink is degraded
(e.g. the mobile circuit is up but carries nothing), install the unit first with
`make install-unit` — it is offline-safe — and sync later. With the required
wheels/sdists already in `~/.cache/uv` *for this platform*, `uv sync --frozen
--offline` makes the dependency step fail fast instead of hanging on DNS.

To replace a **hand-edited** unit (or one with a `ping-tool.service.d/` drop-in)
with the generated one, remove it first, then install:

```bash
sudo systemctl cat ping-tool.service      # read-only: shows the merged unit + drop-ins
make uninstall-unit                       # disable --now, rm unit + drop-ins, daemon-reload
make install-unit
```

`uninstall-unit` stops before deleting on purpose: removing the file first leaves
dangling `*.wants` symlinks. The reload you need after touching unit files is
`systemctl daemon-reload`; `systemctl reload` only re-reads the *service's* own
configuration, which this app does not implement. Journal history survives the
removal (`journalctl -u ping-tool` still shows it).

Do not restart the service to "test" a change; use the throwaway instance recipe in
`40-validation.md`.

## Package downloads (uv) on the SBC

`uv` for `armv6l` is a **statically linked musl** binary, so it uses musl's stub
resolver rather than glibc/NSS: it reads `/etc/resolv.conf` directly, sends no
EDNS0 (answers over 512 bytes come back truncated and must be retried over TCP),
and reports any temporary failure as `EAI_AGAIN` ("Try again"). `curl`, `ping`,
`getent` and `python3` are glibc binaries and can therefore succeed while `uv`
fails. That asymmetry is a client-side difference, **not** uv choosing an
interface: uv has no device binding, so routing is always the kernel's.

When `uv sync` fails with `dns error` / `failed to lookup address information:
Try again` while `ping <host>`, `getent hosts <host>` and `ip route get <ip>`
all look healthy, check in this order:

1. `dig +tcp @<resolver> <host>` — TCP/53 must work, or a truncated answer can
   never be completed.
2. `dig +noedns @<resolver> <host> A` and `... AAAA` — a record type that is
   *silently dropped* (timeout) instead of answered with NODATA makes musl's
   `getaddrinfo` fail outright. `dig +stats <host> | grep 'Query time'` is the
   other half of this check: single-digit milliseconds is a healthy local
   resolver, while ~1 s means MagicDNS is waiting on its upstream and musl's
   fixed budget is the first thing to give out.
3. `strace -f -tt -e trace=network uv sync --frozen`, or
   `tcpdump -ni tailscale0 port 53`, run alongside it — shows whether the query
   left the host and whether an answer came back inside musl's budget.
4. `env | grep -iE 'proxy|^UV_'` and `grep -i proxy .env` — a proxy changes
   which hostname is actually resolved.

`make install-unit` is deliberately network-free, so the unit can be installed
or repaired while this is being debugged; dependency syncing is `make sync`.

## Configuration and secrets

- Runtime configuration is **environment only**. `config/app.conf` is for the container
  base image and is not read by this code.
- `IPINFO_TOKEN` is a secret: never commit `.env`, never echo it into a log line, never
  put it in a `curl` example in the README.
- Container path (`make configure` + `make build`, i.e. a generated `.env` and a
  generated `docker-compose.yml` from `docker-compose.template`) is a convenience
  build; production does not use it. Keep the two paths behaviourally equivalent
  if you touch either.

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
7. A failed `/traceroute` replaces the whole response body, so the `traceroute`
   measurement key is dropped even though `20-http-api-contract.md` says failure
   bodies keep it (`app/__main__.py:250-253`).
8. `get_ip_org()` is called per hop with a 3 s timeout, on a single-threaded server.

Each item above (except 8) has a matching `xfail` test in `tests/test_app.py`; fixing
one means deleting its marker and updating the regression table in `40-validation.md`.

## Repository workflow

- Branch `main`, remote `origin git@github.com:tailucas/ping-tool.git`.
- Commit subjects are short, imperative, capitalised, no prefix:
  `Fix missing keyword.`, `Improve logging.`, `Include support for org checks on IP ownership.`
- Keep commits focused; do not mix refactors with behaviour changes.
- `README.md` is the product documentation (design rationale, probe layout, header
  examples, install steps). Behaviour changes belong there in the same commit.
