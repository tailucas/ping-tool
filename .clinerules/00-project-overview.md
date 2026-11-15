# ping-tool — project overview

## What this is

`ping-tool` is a small single-process HTTP service that performs *deep* ICMP health
checks against a **specifically chosen network interface**. Its reason for existing is
a **data-capped mobile backup circuit**: an Uptime Kuma probe on the home LAN issues
`GET /ping` or `GET /traceroute`, and this service runs ICMP out of the backup
interface (e.g. `eth1`), then decides success from latency, route and IP-ownership
criteria.

Without the tool there is no way to prove that traffic is leaving via the backup
circuit instead of silently leaking onto the fibre circuit.

Deployed on a Raspberry Pi (`armv6l`, Debian, systemd) as the `ping-tool.service`
unit, listening on port `8080`. Throughout these rules `<host>` is that machine and
`<app-dir>` is the git clone (with its own `.venv`) that the unit runs from. Other
placeholders are self-describing: `<backup-gateway>`, `<host-lan-ip>`, `<home>`,
`<target>`, `<iface>`.

## Layout

| Path | Purpose |
| --- | --- |
| `app/__main__.py` | The whole service: logging bootstrap, IP-org lookup, `WebRequestHandler`, endpoints, `main()`. |
| `app/__init__.py` | `APP_NAME` (read from env at import) and `WORK_DIR`. |
| `pyproject.toml` | PEP 621 metadata, runtime `[project.dependencies]`, `dev` dependency group, `app` console script, pytest config. |
| `uv.lock` | Committed lockfile; `uv sync --frozen` is the only supported install. |
| `.python-version` | Pins the interpreter to **3.11**, matching the SBC. |
| `tests/` | `pytest` suite for the handler, helpers and IPinfo enrichment. |
| `ping-tool.service.in` | systemd unit **template**; `make unit` renders it for the checkout and `make install-unit` installs it to `/etc/systemd/system/`. |
| `Makefile` | The **only** task runner: dev container, `uv` sync/sync-prod/setup/lock/test/verify, and container `configure`/`build`/`run`/`connect` plus `unit`/`install-unit`. `make help` is authoritative. |
| `docker-compose.template`, `Dockerfile`, `app_entrypoint.sh`, `.dockerignore` | Container build/run path (secondary; production is a venv + systemd). |
| `config/app.conf` | Tiny templated config for the container base image, **not read by the Python code**. |
| `config_interpol`, `yaml_interpol`, `cred_tool`, `make-app-user.sh`, `connect_to_app.sh` | Helper scripts inherited from the base-app template; do not "fix" them as part of app work. |
| `README.md` | Authoritative user-facing description of the probe design, headers and deployment. Update it with any behaviour change. |
| `.devcontainer/` | VS Code devcontainer (`--network=host`), needs Docker + devcontainer CLI (`make build`). |

## Runtime facts (verified on the deployed host)

- Python **3.11** (pinned in `.python-version`), dependencies managed by **uv**
  (`pyproject.toml` + committed `uv.lock`). The interpreter is *discovered* from
  the host — uv has no managed CPython build for `armv6l`, so `uv python install`
  fails there. Entry point is `app = "app.__main__:main"` in `[project.scripts]`,
  installed as `<app-dir>/.venv/bin/app`.
- `icmplib` is a **git fork**: `https://github.com/tglucas/icmplib.git`. The fork adds
  `SO_BINDTODEVICE` support and lets `source=` take an **interface name** as well as an
  IP address. Never replace it with the PyPI release — interface pinning is the entire
  point of the tool.
- `requests` is used only for `ipinfo.io/<ip>/org` lookups (optional feature).
- HTTP server is `http.server.HTTPServer` — **single-threaded**, `HTTP/1.0`, so probes
  are serialised and a slow `/traceroute` blocks subsequent `/ping` requests.
- The unit has no `User=`/`Group=`; the service runs as **root**. That is required:
  `traceroute()` uses raw sockets and `SO_BINDTODEVICE` needs `CAP_NET_RAW`.
  Do not add `User=` without also granting `CAP_NET_RAW`/`AmbientCapabilities`.
- Logging goes to syslog (`SYSLOG_ADDRESS`, else `/dev/log`, else stdout when the
  process has a TTY or `SUPERVISOR_ENABLED` is set). On `<host>` this means
  `sudo journalctl -u ping-tool`.

## Environment variables

| Variable | Required | Used by | Notes |
| --- | --- | --- | --- |
| `APP_NAME` | **yes** | `app/__init__.py` at import | Import fails without it. `ping-tool` in production. |
| `SERVER_ADDRESS` | **yes** | `main()` | `0.0.0.0` in production. |
| `SERVER_PORT` | **yes** | `main()` | `8080` in production. |
| `IPINFO_TOKEN` | no | `get_ip_org()` | Missing token disables org checks with a warning. Never commit it. |
| `SYSLOG_ADDRESS` | no | logging bootstrap | `udp://host:port` etc. Takes precedence over `/dev/log`. |

## Change-scope rules

- Keep the service **dependency-light and single-module** unless a real requirement
  forces a split. Do not introduce a web framework, async runtime or plugin system.
- New dependencies must be added through uv (`uv add` / `uv add --dev`) so
  `pyproject.toml` and the committed `uv.lock` stay in sync; do not `pip install`
  into the deployed venv by hand and do not run `uv sync` without `--frozen` on
  the host.
- The **`Makefile` is the only task runner** — there is no Taskfile or `app.yml`.
  New build/operational commands belong in the Makefile as a target with a `##`
  description so `make help` stays complete. Identity and build variables
  (`PROJECT_NAME`, `SERVER_*`, `LANG`, ...) use `:=`, not `?=`, so an ambient
  shell variable cannot silently retag the container; only secrets such as
  `IPINFO_TOKEN` are read from the environment.
- The caller is a monitoring system (Uptime Kuma): **2xx = healthy, non-2xx = down**.
  Never turn a failed network test into a 2xx, and never turn a caller/configuration
  mistake into a "network is down" verdict (see `20-http-api-contract.md`).
- This tool's *output* is an operational verdict. Prefer explicit, machine-readable
  fields (`error_code`, `interface`, counters) over prose-only messages.
- Changes to headers, status codes or response shape are **breaking changes** for
  existing probes: update `README.md` in the same commit.
