# HTTP API contract

The caller is Uptime Kuma (or `curl`). It only understands HTTP status: **2xx = up,
anything else = down**. The JSON body is for humans and for post-mortems.

## Endpoints

| Path | Meaning |
| --- | --- |
| `/ping` | Send 3 ICMP echo requests (`count=3, interval=1, privileged=False`) out of `Source` to `Host`, then evaluate latency. |
| `/traceroute` | Trace to `Host` (`count=1`), evaluate hops against include/exclude network and org sets plus final-hop latency. |
| anything else | Currently only logged as a warning and **still answered 200 `{"result": "OK"}`**. This is a bug: an unknown path must be a 4xx/5xx, never a success. |

## Request headers

The target host is carried in the non-standard **`Host`** header. This is intentional
(Uptime Kuma sends `Host` from its probe config) and it means `self.headers.get(HEADER_HOST)`
is the ping target, not the TCP server name. Keep it that way; document any addition.

| Header | Required | Meaning |
| --- | --- | --- |
| `Host` | yes for a real test | Hostname or IP to test. Missing → currently a `NameLookupError` traceback; should be a 400. |
| `Source` | effectively yes | Interface name (`eth1`) or source IP. Free functions accept both via the icmplib fork. Missing → warning, kernel routing decides. |
| `MinLatencyMs` | no (but see below) | Minimum acceptable RTT. Missing → latency is **not** enforced. |
| `RouteIncludeCsv` | `/traceroute` | Networks that must appear in the path (e.g. the backup gateway, shown as `198.51.100.1` in the examples). |
| `RouteExcludeCsv` | `/traceroute` | Networks that must **not** appear (e.g. the fibre gateway). |
| `HopsMustIncludeOrg` | `/traceroute` | Substrings of IPinfo org names that must appear. |
| `HopsMustExcludeOrg` | `/traceroute` | Substrings of org names that must not appear. |

CSV headers are parsed by `_split_to_set()`; **strip whitespace** when you touch it
(`'a, b'` currently yields `{'a', ' b'}`). Empty/absent sets mean "skip this check" and
emit a warning — that is by design, not an error.

## Response

Success:

```json
{"result": "OK", "ping": {"host": "203.0.113.10", "rtts": [15.2, 15.9, 16.1]}}
```

Failure (circuit carried no traffic):

```json
{"result": "error", "reason": "No ICMP replies from arbitrary.internet.host.com via eth1 (3 packets sent, loss 100.0%).", "ping": {"host": "203.0.113.10", "rtts": []}}
```

- The body key for the measurement is `self.path[1:]` (`ping` / `traceroute`). Failure
  bodies keep it so the caller can see the empty `rtts` as evidence. `/ping` does this;
  `/traceroute` currently does not (see the defect list in `50-operations.md`).
- Status is chosen explicitly by the handler (`status`, initialised to `200`):
  - `200` — the test passed.
  - `503` — the circuit under test could not carry traffic, or carried it via the wrong
    path: no replies, latency below `MinLatencyMs`, or any `/traceroute` constraint
    failure. This is a **verdict**, not a defect. Unresolvable names belong here too,
    but currently still fall through to `500` (see `50-operations.md`).
  - `500` — the generic `except Exception` path only, i.e. a defect in `ping-tool`
    itself. An alert on `500` is always actionable.
- Do **not** use `403`: it means "authorization denied" and is misread by proxies and
  monitoring UIs. Do **not** use `500` for a network verdict — that is exactly what made
  DNS failures look like application crashes.
- On the generic `except Exception` path the body is
  `{"result": "error", "reason": "<str(exc)>"}` — a raw exception string.

## Non-negotiable rules

1. **A test that did not demonstrably pass must not return 2xx.** In particular
   100 % packet loss (`host.is_alive is False`) must fail even when `MinLatencyMs` is
   absent. Implemented: `/ping` takes the `503` path before touching latency.
2. **Latency checks only run when there is at least one reply.** `min_rtt` is `0.0`
   when `rtts` is empty, so checking it first reports "too fast" (a fibre-leak symptom)
   for a circuit that carried *no* traffic. Implemented as
   `if not host.is_alive: ... else: self._check_latency(...)`.
3. **Distinguish verdicts from mistakes.** Bad `Source` = caller error (400).
   Unresolvable `Host` / no replies = network verdict (503) — DNS still reports 500
   today. Internal exception = 500.
4. **Never let optional enrichment decide the verdict.** `get_ip_org()` failure must
   only suppress org checks; it must not flip the result or leak into `reason`.
5. Add a stable machine-readable discriminator (e.g. `"error_code": "dns_failure"`,
   `"no_replies"`, `"latency_too_low"`, `"bad_source"`) before writing more prose.
   Keep `reason` human-readable for the journal.
6. When adding fields, extend the existing dict — do not restructure the body.
   Probes and dashboards may already assert on `result` and the path key.
7. Any change here requires a `README.md` update in the same commit, including a
   `curl` example.
