# Architecture

## Shape

```
                     ┌────────────────────────────┐
   Agent 2 :10050 ───▶│                            │
   (every guest)      │   Zabbix Server  :10051    │──▶ PostgreSQL 16
                      │                            │    + TimescaleDB
   Proxmox API :8006 ─▶│  ~13,000 items             │      (hypertables,
   (agentless LLD)    │  ~5,000 triggers           │       compressed >7d)
                      │  75 hosts                  │
   SNMP :161 ────────▶│                            │
   (switches, APs)     └────────────┬───────────────┘
                                    │
                     ┌──────────────┴──────────────┐
                     ▼                             ▼
              Zabbix frontend                webhook / alert actions
```

A **Zabbix proxy** is worth adding for any remote site. It buffers locally, so a
WAN outage delays data rather than losing it, and the site keeps collecting
while disconnected.

## Collection methods, and when to use which

| Method | Use for | Cost |
|---|---|---|
| **Agent 2, active** | Anything you control the inside of | Lowest — agent pushes, server does not poll |
| **Agent 2, passive** | Hosts behind a firewall you can traverse inbound | Server-side poller per check |
| **Proxmox API (HTTP agent + LLD)** | Every guest, with zero per-guest setup | HTTP pollers on the server |
| **SNMP** | Switches, APs, UPSes, printers | Cheap, but polling only |
| **ICMP (fping)** | Anything you can only reach at layer 3 | Trivial |

Prefer **active** agent checks where you can. The agent connects outward to
`ServerActive`, which means guests on any subnet work without inbound firewall
rules, and the server does not need a poller slot per check.

## Host groups

Groups drive permissions, dashboards, and — critically — which alerts an action
matches. Group by **what a thing is**, not where it happens to run, so a guest
migrating between nodes does not change its alerting:

| Group | Contains |
|---|---|
| `Homelab/Containers` | LXC guests |
| `Homelab/VMs` | Full VMs |
| `Homelab/Infrastructure` | Hypervisors, Zabbix itself, backup servers |
| `Homelab/Network` | Switches, APs, routers, UPSes |
| `Homelab/Docker` | Container hosts |
| `Homelab/Lab` | Disposable lab guests — noisy by design, alert differently |

`Homelab/Lab` earns its place: lab machines are rebuilt constantly and would
otherwise generate a steady stream of unreachable alerts that trains you to
ignore the alert you actually care about.

## Template design

The templates themselves are no longer bundled here — `templates.py` exports
yours instead. But the design principles are the reason the tooling looks the
way it does, and they are worth stating.

Three of them, all learned by getting them wrong first.

**1. Interval by volatility, not by habit.** CPU at 60s, memory at 60s, disk at
5m, kernel max-fds at 1h. An item polled ten times more often than its value
changes is pure history volume.

**2. Trends over history.** History is per-sample and expensive; trends are
hourly min/avg/max and cheap. Most items want 30d history and 365d trends. For
items you only ever read as a current value, set `trends: 0` and skip the
aggregation entirely.

**3. A trigger must imply an action.** If nobody would do anything about it, it
is a graph, not a trigger. This is the discipline that keeps the problem list
short enough to actually read — and a problem list nobody reads is the failure
mode that makes the whole system decorative.

### Severity convention

Consistency matters more than the exact thresholds. The point is that sorting by
severity produces a genuine work queue:

| Severity | Means | Example |
|---|---|---|
| `DISASTER` | A segment may be down | Network device unreachable |
| `HIGH` | A host or service is gone | `nodata(agent.ping, 5m)` |
| `AVERAGE` | Degraded, will get worse | Memory > 85%, swap > 50% |
| `WARNING` | Attention within days | Disk > 80%, latency > 150 ms |
| `INFO` | Record only | Configuration change detected |

### `manual_close`

Set `manual_close: YES` on triggers whose recovery expression may never fire —
anything `nodata`-based on a host that might be decommissioned rather than
fixed. Without it the problem is permanent and cannot be cleared from the UI.
`problems.py` reports these separately for exactly that reason.

### Why `no_address` is its own finding

A host with a `0.0.0.0` interface produces exactly the same symptom as a host
that is genuinely down: `nodata` fires, the trigger goes HIGH, and the problem
list fills up. The difference is that no amount of fixing the guest will clear
it.

This is common enough to deserve its own category in `reconcile.py`. It is what
auto-registration produces when the action creates a host without an interface
operation, and it is invisible in the Zabbix UI unless you go looking at each
host's interface tab.

## Data retention

| Setting | Value | Why |
|---|---|---|
| History | 30d | Enough to investigate last month's incident |
| Trends | 365d | Year-over-year capacity planning |
| Events | 365d | Alert archaeology |
| Compression | > 7d | TimescaleDB, large win, no query change |

Housekeeping on a large non-partitioned `history` table is slow and lock-heavy.
On TimescaleDB, dropping expired data is dropping a chunk. See
[postgresql-timescaledb.md](postgresql-timescaledb.md).

## Scaling notes

Figures below are from an estate of roughly 75 hosts and 13,000 items:

- **`StartHTTPPollers`** — the default of 1 saturates as soon as Proxmox LLD is
  producing hundreds of HTTP items. Raising it to 5 cleared a persistent
  poller-queue backlog.
- **`CacheSize` / `HistoryCacheSize`** — watch `Zabbix server health`. Cache
  utilisation trending up is the early warning before value loss.
- **Unsupported items** — audit periodically. They cost poller time and return
  nothing, and they accumulate silently after template changes.
