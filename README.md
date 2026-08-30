# homelab-zabbix

Custom tooling for running Zabbix 7.x against a Proxmox homelab.

Six small programs, no dependencies beyond the Python standard library, built to
answer questions the Zabbix UI answers badly or not at all — starting with the
one that matters most: **is my monitoring actually monitoring what I think it
is?**

In production on a two-node estate: 75 monitored hosts, ~13,000 items, ~5,000
triggers.

---

## The tools

### `reconcile.py` — is Zabbix watching the right thing?

Monitoring lies quietly. A guest gets a new address, Zabbix keeps polling the
old one, and the "unreachable" alert that results is indistinguishable from a
real outage — so it gets investigated once and ignored thereafter. Meanwhile a
guest nobody added is not monitored at all, and nothing anywhere says so.

This compares the Proxmox inventory against the Zabbix inventory and reports
five classes of drift:

| Finding | Meaning |
|---|---|
| `no_address` | Zabbix host with a `0.0.0.0` or empty interface — nowhere to poll, so every check fails regardless of health |
| `drift` | Zabbix is polling a different address than Proxmox has for that guest |
| `unmonitored` | Running guest with no Zabbix host at all |
| `orphaned` | Enabled Zabbix host with no matching guest |
| `stopped` | Zabbix host enabled for a guest that is stopped — guaranteed alert noise |

```
── No usable address (9) — Zabbix has nowhere to poll
   homelab-dashboard          zabbix=0.0.0.0      proxmox=10.0.0.51
   ollama                     zabbix=0.0.0.0      proxmox=10.0.0.14
   …

── Address drift (1) — Zabbix is polling an address Proxmox disagrees with
   homeassistant              CT/VM 102  proxmox=10.0.0.68  zabbix=10.0.0.162  (guest-agent)
```

It is careful about what it does *not* claim. A DHCP container, or a VM without
the guest agent, has no address Proxmox can vouch for — those are reported as
**unverifiable** rather than silently counted as clean. Interfaces polled by
name rather than by IP are not compared at all, because comparing a hostname to
an address is meaningless.

```bash
./scripts/reconcile.py
./scripts/reconcile.py --only no_address
./scripts/reconcile.py --exclude-group Homelab/Network Homelab/Infrastructure
./scripts/reconcile.py --json | jq .totals
./scripts/reconcile.py --fail-on no_address drift     # for a scheduled job
```

Read-only against both APIs.

### `problems.py` — bulk triage

```bash
./scripts/problems.py list --min-severity 4
./scripts/problems.py close --stale 30            # dry run
./scripts/problems.py close --stale 30 --apply    # do it
```

Every `close` is a dry run until `--apply`, because acknowledging problems in
bulk is easy to get wrong and there is no undo. Problems whose trigger has
`manual_close` disabled are reported as **skipped** rather than counted as
closed — Zabbix will not close those, and pretending otherwise hides work.

### `inventory.py` — export, and audit DNS

```bash
./scripts/inventory.py --csv hosts.csv
./scripts/inventory.py --check-dns --only-problems
```

The DNS audit answers three questions that quietly rot in every homelab: does
the monitored name resolve, does it resolve to the address Zabbix is actually
polling, and does that address reverse-resolve back to the same name.

### `zbx.py` — Zabbix 7.x API client

Also the shared library the other tools use.

> **Zabbix 7.0 changed API authentication**, and it is the single most common
> reason a script written against 6.x breaks after an upgrade. The token from
> `user.login` must now be sent as an `Authorization: Bearer <token>` header.
> The `auth` field inside the JSON-RPC body is **ignored rather than rejected**,
> so every call fails with a permission error that never mentions
> authentication.

```bash
./scripts/zbx.py     # connectivity check: version, host/item/trigger/problem counts
```

### `templates.py` — templates under version control

Template definitions are configuration, and configuration that only exists
inside a database is configuration you cannot diff, review, or roll back.

```bash
./scripts/templates.py export                 # Zabbix -> templates/*.yaml
./scripts/templates.py import --dry-run       # report changes, write nothing
./scripts/templates.py import                 # apply
```

Import is idempotent — templates match on UUID, so re-importing updates in place
instead of creating `My Template_1`. Deletion is opt-in behind `--prune`, because
`deleteMissing` will happily remove items you added through the UI.

No templates ship here; `templates/*.yaml` is gitignored so nobody publishes
their own estate's configuration by forgetting to look.

### `install-agent.sh` / `bulk-install-agents.sh` — agent rollout

```bash
ZBX_SERVER=10.0.0.20 sudo ./scripts/install-agent.sh
ZBX_SERVER=10.0.0.20 ./scripts/bulk-install-agents.sh --dry-run   # on the PVE node
ZBX_SERVER=10.0.0.20 ./scripts/bulk-install-agents.sh
```

The bulk script drives `pct exec`, so it needs no SSH keys or credentials inside
the guests. One container failing never aborts the run; failures are collected
and reported so you can re-run just those with `--only`.

---

## Quick start

```bash
git clone https://github.com/NetworkBound/homelab-zabbix.git
cd homelab-zabbix

cp .env.example .env
$EDITOR .env
set -a; . ./.env; set +a

python3 scripts/zbx.py          # connectivity
python3 scripts/reconcile.py    # what is actually wrong
```

Python 3.9+. Nothing to `pip install` — the standard library only.

Prefer a scoped API token over a password: *Users → API tokens → Create API
token*, then set `ZBX_TOKEN` and leave `ZBX_USER` / `ZBX_PASS` empty. Give it a
read-only role unless you intend to import templates or close problems.

`reconcile.py` additionally needs a **PVEAuditor** (read-only) Proxmox token —
see `.env.example`.

## Running it on a schedule

Drift is worth catching every morning, not whenever someone remembers. Two
workflows ship in `.github/workflows/`:

- **`ci.yml`** — lint, unit tests and a secret scan. Runs anywhere; touches
  nothing but the repo.
- **`monitoring-drift.yml`** — the live reconcile. Needs a **self-hosted**
  runner, because a hosted one has no route to your private APIs.

Gitea Actions reads `.github/workflows/` too, so the same files work on a
self-hosted Gitea runner. Setup for both, plus the secrets to configure and
advice on which findings should fail the build:
**[docs/runners.md](docs/runners.md)**.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

41 tests, no network and no Zabbix required. The tools deliberately keep their
comparison logic separate from their I/O so the interesting parts are testable
anywhere — which is also what lets CI run on a hosted runner.

## Documentation

| | |
|---|---|
| [runners.md](docs/runners.md) | Scheduling these tools on GitHub or Gitea Actions |
| [architecture.md](docs/architecture.md) | How the monitoring is put together, and why |
| [auto-registration.md](docs/auto-registration.md) | Zero-touch onboarding, agentless and agent-based |
| [postgresql-timescaledb.md](docs/postgresql-timescaledb.md) | MariaDB → PostgreSQL + TimescaleDB, with the traps |
| [troubleshooting.md](docs/troubleshooting.md) | Failures that cost real time, and what they actually were |

## Security

- No credentials are committed. `.env` is gitignored; `.env.example` holds
  placeholders only. CI fails on a private address, a credential-shaped literal,
  a private key, or a tracked `.env`.
- Everything except `templates.py import`, `problems.py close --apply` and the
  agent installers is strictly read-only.
- Template exports contain no macro *values* — a secret macro such as a Proxmox
  token lives on the host object and never leaves the server.

## License

MIT — see [LICENSE](LICENSE).
