# ICMP / networking semantics

Everything here was verified against the deployed fork in the service's virtualenv
(`<app-dir>/.venv/lib/python3.11/site-packages/icmplib`). Trust these rules over
intuition; several of them are non-obvious.

## The fork is load-bearing

`pyproject.toml` pins `icmplib` to `https://github.com/tglucas/icmplib.git`. The fork:

- sets `SO_BINDTODEVICE` when `source` is not an IP address, so packets really leave the
  nominated interface (`sockets.py`), and
- exchanges the interface name for its address via `SIOCGIFADDR` for reporting.

Upstream PyPI `icmplib` cannot pin a device. Do not "upgrade" to it, and do not pass an
interface name to any stdlib socket call — only this fork understands it.

## `ping()` resolves DNS *outside* the interface binding

`icmplib/ping.py` does:

```python
if is_hostname(address):
    address = resolve(address, family)[0]   # OS resolver, default route
...
with _Socket(source, privileged) as sock:   # binding happens after resolution
```

Consequences that matter for a multi-homed, data-capped host:

- DNS resolution uses the **host resolver**, i.e. the primary path (`eth0` via Tailscale
  MagicDNS at `100.100.100.100`, a fixed Tailscale address), **not** the interface named
  in `Source`.
- If the fibre path is healthy but the mobile circuit is capped/dead, the name resolves
  and you get **100 % loss** on `eth1`.
- If no path can reach a resolver (whole uplink down, Tailscale DNS unavailable), you get
  `NameLookupError` **before any packet is sent**, even though `eth1` itself may be
  perfectly up and link-local-reachable.

So the *same* physical condition ("this circuit carries no traffic") surfaces as two
different results depending on which path DNS happened to use. Any fix must treat DNS
as its own sub-check and must not conflate it with interface state.

On this host that resolver is Tailscale MagicDNS, which forwards to **NextDNS over
DoH** (`tailscale dns query <name>` prints the resolver URL). Cold lookups measure
~1 s and Fastly's A records carry a 60 s TTL, so the Pi re-resolves constantly.
Resolver latency and resolver failure are therefore a first-class part of a probe's
timing and outcome — but never part of the ICMP verdict, and never evidence about the
circuit under test.

Practical rules:

- Resolve explicitly, then report `dns_ok` / `dns_error` separately from the ICMP result.
- Consider accepting a literal IP (already supported) and/or an optional
  `Address`/`Resolve` header so a probe can pin the address and keep testing the path
  during a resolver outage.
- Never let a DNS failure reach the generic `except Exception` handler.

## `ping()` never raises for packet loss

Per-packet errors are swallowed:

```python
try:
    sock.send(request)
    reply = sock.receive(request, timeout)
    reply.raise_for_status()
    rtts.append(rtt)
except ICMPLibError:
    pass
return Host(address, packets_sent, rtts)
```

Therefore:

- `Host.is_alive` is `len(self._rtts) > 0`.
- `Host.min_rtt` returns **`0.0`** when nothing came back.
- A completely dead circuit is a *normal return value*, not an exception. **The caller
  must check `host.is_alive` / `host.rtts`.**

## Failure taxonomy — classify, don't crash

| Symptom | Exception / value | Meaning | Correct handling |
| --- | --- | --- | --- |
| Target name cannot resolve | `NameLookupError` (`ICMPLibError`) | Resolver unreachable **or** name genuinely wrong — ambiguous | Non-2xx, `error_code=dns_failure`, log one WARNING line, no traceback. Optionally distinguish with a resolver reachability probe. |
| `source` interface does not exist | `SocketAddressError('[Errno 19] No such device')` (also `-9/49/99/11001`) | Caller mistake | 400, `error_code=bad_source` |
| Insufficient privileges | `SocketPermissionError` (`errno 1/13/10013`) | Service not root / no `CAP_NET_RAW` | 500, `error_code=privilege` — configuration defect, not a network verdict |
| Socket/other ICMP problem | `ICMPSocketError`, `TimeoutExceeded` | Local socket failure | 500, `error_code=socket_error`, log with `exc_info=True` |
| Interface up, zero replies | `host.is_alive == False`, `rtts == []` | **The data-cap / no-traffic case** | Non-2xx, `error_code=no_replies`, `packet_loss=100` |
| Replies, but RTT below threshold | `min_rtt < MinLatencyMs` | Traffic leaking via the faster fibre path | Non-2xx, `error_code=latency_too_low` |
| Missing `Host` | `host_name is None` → `NameLookupError` | Caller mistake | 400, `error_code=missing_host` |

`NameLookupError`, `SocketAddressError`, `SocketPermissionError`, `ICMPSocketError` and
`TimeoutExceeded` are all already imported in `app/__main__.py`; use them instead of a
broad `except`.

## Distinguishing "interface is fine" from "circuit carries no traffic"

Useful cheap local signals, all read-only and safe to add:

- `/sys/class/net/<iface>/operstate` (`up`/`down`/`unknown`), `carrier`, `speed`.
- Source address presence: `ip -4 -brief addr show dev <iface>`.
- Neighbour reachability of the gateway: `ip neigh show dev <iface>`
  (`REACHABLE`/`STALE`/`FAILED`).
- Route selection for the target: `ip route get <target> oif <iface>`.

Reporting `interface`, `operstate` and `gateway_reachable` alongside `no_replies`
converts an ambiguous 500 into an actionable verdict: *"link up, gateway reachable, zero
ICMP replies — circuit capped or upstream blocked"*. Keep these checks bounded (no
subprocess loops, no 3 s timeouts per hop).

## Latency, timeouts and blocking

- `/ping` is `count=3, interval=1`, receive timeout defaults to 2 s → worst case roughly
  **9 s** per request.
- `/traceroute` is `count=1` with up to 30 hops, and `get_ip_org()` is called **per hop**
  with `timeout=3` → an unreachable destination can occupy the single-threaded server
  for minutes.
- Because `HTTPServer` is not threaded, one slow probe delays all others. If you touch
  this area, prefer `ThreadingHTTPServer` plus a bounded overall deadline, and/or cache
  IPinfo answers per IP.
- `get_ip_org()` is best-effort: it already catches `RequestException` and returns
  `None`. Keep it that way; never let enrichment fail a test.

## Multi-homing facts on the deployed host

- `eth0` — the home LAN (fibre/Tailscale side), default route metric **100**.
- `eth1` — the backup AP LAN (mobile side), default route metric **101**.
- `tailscale0` is up; `/etc/resolv.conf` points at Tailscale MagicDNS
  (`100.100.100.100`).
- Because `eth0` has the lower metric it owns the default route. `Source: eth1` is the
  *only* thing that forces traffic onto the backup circuit: never "simplify" it away, and
  never assume the default route is the interface under test.
