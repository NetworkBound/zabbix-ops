# homelab-zabbix

Zabbix monitoring for a Proxmox homelab, as code.

Seven templates, an idempotent import/export tool, agent rollout scripts, and
the runbooks for the two things that actually bite: **auto-registration** (so
new guests monitor themselves) and **PostgreSQL + TimescaleDB** (so history
does not eat your disk).

Everything here runs against **Zabbix 7.x** and is in production on a two-node
Proxmox estate: 75 monitored hosts, ~13,000 items, ~5,000 triggers.

---

## Why

The default Zabbix experience for a homelab is rough in a specific way. The
stock `Linux by Zabbix agent` template is built for servers you care about
individually — 43 items, a dense trigger set, and history settings sized for a
datacentre. Apply it to 50 LXC containers and you get a monitoring system that
costs more attention than it returns: disks fill, false positives train you to
ignore alerts, and every new container is a manual chore.

This repo is the opposite bet:

- **Templates sized for containers**, not for bare metal. Fewer items, longer
  intervals on things that do not move, and triggers tuned so that "unreachable"
  is loud and "disk at 81%" is not.
- **Zero-touch onboarding.** A new LXC gets monitored without anyone opening the
  Zabbix UI — agentless via the Proxmox API immediately, and agent-based within
  a couple of minutes if the agent is baked into the image.
- **History that fits.** TimescaleDB hypertables with compression, so 40M rows
  of history is a few gigabytes rather than a capacity incident.

## What is here

```
templates/    7 Zabbix 7.4 templates, exported as YAML
scripts/      API client + template/problem/inventory tooling + agent rollout
docs/         architecture, auto-registration, PostgreSQL migration, troubleshooting
```

### Templates

| Template | Items | Triggers | For |
|---|---:|---:|---|
| `Homelab LXC Container` | 46 | 16 | Proxmox LXC guests — CPU, load, memory, disk, net I/O, swap, fds |
| `Homelab VM` | 44 | 15 | Full VMs — as above plus disk IOPS and swap pressure |
| `Homelab Docker Host` | 42 | 12 | Container hosts — adds running/total container and image counts |
| `Homelab Frigate NVR` | 50 | 11 | NVR — process liveness, recording disk, camera availability |
| `Homelab AI GPU` | 10 | 5 | GPU boxes — utilisation, VRAM, temperature, power |
| `Homelab Network Device` | 5 | 6 | Switches, APs, routers — ICMP plus SNMP sysName/uptime |
| `Homelab Proxmox Host` | 3 | 4 | Hypervisor reachability and Proxmox API health |

Trigger severities follow one convention throughout, so severity actually means
something when you sort by it:

| Severity | Means |
|---|---|
| `DISASTER` | Network device unreachable — a whole segment may be down |
| `HIGH` | Host unreachable, or a service process is gone |
| `AVERAGE` | Memory above 85%, swap above 50% |
| `WARNING` | Disk above 80%, latency above 150 ms |

The `Homelab Proxmox Host` template is deliberately thin: three items and four
triggers for reachability and API health. The heavy lifting — 395 discovered
items covering every VM and container — comes from the stock
`Proxmox VE by HTTP` template, which does low-level discovery against the API.
Duplicating that here would be a maintenance burden for no gain.

## Quick start

```bash
git clone https://github.com/NetworkBound/homelab-zabbix.git
cd homelab-zabbix

cp .env.example .env
$EDITOR .env
set -a; . ./.env; set +a

python3 scripts/zbx.py                       # verify connectivity
python3 scripts/templates.py import --dry-run # see what would change
python3 scripts/templates.py import           # load the templates
```

The scripts use only the Python standard library — no `pip install` needed.

### Authentication

> **Zabbix 7.0 changed API authentication, and this is the single most common
> reason an older script breaks after upgrading.** The token returned by
> `user.login` must now be sent as an `Authorization: Bearer <token>` header.
> The `auth` field inside the JSON-RPC body is ignored, and every call fails
> with a permission error that does not mention authentication at all.

`scripts/zbx.py` handles this. Prefer a scoped API token over a password:

*Users → API tokens → Create API token*, then set `ZBX_TOKEN` in `.env` and
leave `ZBX_USER` / `ZBX_PASS` empty.

## Tools

### `scripts/templates.py` — template lifecycle

```bash
./scripts/templates.py export                  # Zabbix -> templates/*.yaml
./scripts/templates.py import --dry-run        # report changes, write nothing
./scripts/templates.py import                  # apply
./scripts/templates.py import --prune          # also delete items removed from the files
```

Import is idempotent — templates match on UUID, so re-importing updates in
place instead of creating `Homelab VM_1`. Deletion is opt-in behind `--prune`
because `deleteMissing` will happily remove items you added through the UI.

The round trip is lossless: exporting and re-importing these files reports
"no changes".

### `scripts/problems.py` — bulk triage

```bash
./scripts/problems.py list --min-severity 4
./scripts/problems.py close --stale 30           # dry run
./scripts/problems.py close --stale 30 --apply   # do it
```

Every `close` is a dry run until `--apply`. Problems whose trigger has
`manual_close` disabled are reported as skipped rather than silently counted as
closed — Zabbix will not close those, and pretending otherwise hides work.

### `scripts/inventory.py` — export and DNS audit

```bash
./scripts/inventory.py --csv hosts.csv
./scripts/inventory.py --check-dns --only-problems
```

The DNS audit answers three questions that quietly rot in every homelab: does
the monitored name resolve, does it resolve to the address Zabbix is actually
polling, and does that address reverse-resolve back to the same name. Any of
those drifting means your alerts point at the wrong box. It reports; it changes
nothing.

### Agent rollout

```bash
# One host
ZBX_SERVER=10.0.0.20 sudo ./scripts/install-agent.sh

# Every running LXC on a Proxmox node — run on the node itself
ZBX_SERVER=10.0.0.20 ./scripts/bulk-install-agents.sh --dry-run
ZBX_SERVER=10.0.0.20 ./scripts/bulk-install-agents.sh
```

The bulk script drives `pct exec`, so it needs no SSH keys or credentials inside
the guests. One container failing never aborts the run; failures are collected
and reported at the end so you can re-run just those with `--only`.

## Documentation

| | |
|---|---|
| [architecture.md](docs/architecture.md) | How the pieces fit — server, proxy, agents, templates, host groups |
| [auto-registration.md](docs/auto-registration.md) | Zero-touch onboarding, agentless and agent-based |
| [postgresql-timescaledb.md](docs/postgresql-timescaledb.md) | MariaDB → PostgreSQL + TimescaleDB migration, with the traps |
| [troubleshooting.md](docs/troubleshooting.md) | The failures that cost real time, and their fixes |

## Requirements

- Zabbix Server 7.0+
- Python 3.9+ (standard library only)
- Proxmox VE 8.x/9.x for the agent rollout script
- Debian/Ubuntu or RHEL-family guests for `install-agent.sh`

## Security

- No credentials are committed. `.env` is gitignored; `.env.example` holds
  placeholders only.
- The exported templates contain no macro *values* — secret macros such as a
  Proxmox API token are set on the host object in Zabbix and never leave it.
- Use a read-only API token for anything that only reads. `inventory.py` and
  `problems.py list` never write.

## License

MIT — see [LICENSE](LICENSE).
