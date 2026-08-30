# zabbix-ops

Operational tooling for Zabbix 7.x: inventory reconciliation, bulk problem
triage, safe promotion from test to production, and high availability.

Standard library only. No dependencies to install, nothing to keep upgraded.

## Scope

This is deliberately narrow. The Zabbix ecosystem already has good answers for
several problems, and this does not try to replace them:

| Need | Use |
|---|---|
| API client library | [`zabbix_utils`](https://github.com/zabbix/python-zabbix-utils) (official) |
| Host, user and macro administration | [`zabbix-cli`](https://github.com/unioslo/zabbix-cli) |
| Configuration management | [`community.zabbix`](https://github.com/ansible-collections/community.zabbix) Ansible collection |
| Template synchronisation with git | [ZabbixCI](https://github.com/retigra/ZabbixCI) |
| Dashboards and graphing | [grafana-zabbix](https://github.com/grafana/grafana-zabbix) |

What remains poorly served is the **operational read side** — confirming that
monitoring is actually monitoring what you believe it is — and **safely moving
configuration between environments**. That is what this covers.

---

## Tools

### `reconcile.py` — verify monitoring against reality

Monitoring fails quietly. A host is renumbered, Zabbix keeps polling the old
address, and the resulting "unreachable" alert is indistinguishable from a real
outage. It gets investigated once, found to be false, and skimmed past every
time after that. Meanwhile a host nobody added is not monitored at all, and
nothing reports that either.

This compares an authoritative inventory against Zabbix and reports five classes
of disagreement.

| Finding | Meaning |
|---|---|
| `no_address` | Zabbix host with a `0.0.0.0` or empty interface. Nowhere to poll, so every check fails regardless of host health. |
| `drift` | Zabbix is polling a different address than the inventory holds. |
| `unmonitored` | Running host absent from Zabbix entirely. |
| `orphaned` | Enabled Zabbix host with no matching inventory entry. |
| `stopped` | Host enabled in Zabbix for a system that is powered off. Guaranteed alert noise. |

```
── No usable address (9) — Zabbix has nowhere to poll
   app-01              zabbix=0.0.0.0      inventory=10.0.0.51
   app-02              zabbix=0.0.0.0      inventory=10.0.0.14

── Address drift (1)
   gw-01               proxmox=10.0.0.68   zabbix=10.0.0.162   (guest-agent)
```

`no_address` is separated from `drift` deliberately. A `0.0.0.0` interface is not
two systems disagreeing; it is Zabbix having nowhere to poll at all, which no
amount of fixing the host will resolve. It is the usual result of an
auto-registration action that creates hosts without an interface operation, and
it is invisible in the frontend unless you open each host in turn.

The tool is careful about what it does not claim. A DHCP host, or a virtual
machine without a guest agent, has no address the inventory can vouch for; those
are reported as **unverifiable** rather than counted as clean. Interfaces polled
by hostname are not compared against addresses at all.

```bash
./scripts/reconcile.py
./scripts/reconcile.py --only no_address
./scripts/reconcile.py --exclude-group Network Infrastructure
./scripts/reconcile.py --json | jq .totals
./scripts/reconcile.py --fail-on no_address drift    # for a scheduled check
```

Proxmox VE is the inventory source today (`scripts/pve.py`). The comparison
logic is separate from the source, so another inventory can be added without
touching it.

### `audit.py` — production readiness

```bash
./scripts/audit.py
./scripts/audit.py --only alerting
./scripts/audit.py --fail-on high      # for a scheduled check
```

Around twenty checks across six categories, each one present because it has
gone wrong in a real installation. Read-only.

| Category | Answers |
|---|---|
| `alerting` | Would a trigger firing now actually reach a person, and does anything escalate if the first notification is missed? |
| `suppression` | Is monitoring quietly switched off somewhere — a maintenance window that suppresses nothing, or one that never ends? |
| `collection` | Is data arriving? Unsupported items, hosts with an unusable interface address. |
| `noise` | Does the problem list mean anything? Trigger dependencies, week-old problems, untagged hosts. |
| `security` | Encrypted transport, token expiry, super-admin sprawl. |
| `capacity` | Proxy version skew, offline proxies, server processes running hot, cache pressure. |

The suppression checks are the ones worth running first. A one-time maintenance
period whose slot has elapsed still displays as active, so the hosts it names
are believed to be suppressed while they alert normally — and nothing in the
frontend indicates this.

### `clone.py` — a test instance that cannot hurt production

Copies configuration from production into a test instance so templates, triggers
and webhooks can be developed against real data.

A naive copy is dangerous in two specific ways, so both are defaults:

- **It notifies real people.** Production actions and media types arrive still
  pointing at the real webhook and mail relay. Everything is disabled after
  import unless you explicitly ask otherwise.
- **It polls production.** Two options, depending on what you are doing:
  `--include hosts` alone creates them disabled and polls nothing;
  `--test-proxy NAME --interval 12h` keeps them enabled behind a dedicated proxy
  with every interval stretched, so test collects real data rarely and *Execute
  now* gives you an immediate value while developing.

The destination must carry a global macro `{$ENV}` set to `test`, `dev`,
`staging`, `lab` or `sandbox`. The tool refuses to write anywhere that does not.
Production does not carry it, so production cannot be a destination.

See [test-environment.md](docs/test-environment.md).

### `ha.py` — cluster and replication health

> Zabbix native HA runs several servers against **one shared database**. It
> protects against losing a server, not against losing a site. Two servers with
> two separate databases is not HA; it is two monitoring systems that both alert
> you and drift apart.

Cross-site redundancy needs two mechanisms: Zabbix HA for automatic server
failover, and PostgreSQL streaming replication for the database. This reports on
both and is explicit about what is missing.

```bash
./scripts/ha.py
./scripts/ha.py --require-ha --require-replication
```

Two things in [`deploy/ha/`](deploy/ha/) exist because a working cluster is not
sufficient on its own:

- **`setup-vip.sh`** — a proxy's `Server` parameter accepts exactly one address
  (`zabbix_proxy` refuses to start with a comma in it), so a proxy is pinned to
  one node and every host behind it stops being monitored on failover.
  keepalived moves a floating address to whichever node holds the active role.
- **`update-agent-servers.sh`** — an agent's `Server` is an allowlist, so a new
  node is an address no agent will answer. Every passive check against it fails
  until the fleet is updated.

Full build and failover procedure: [ha.md](docs/ha.md).

### `problems.py` — bulk triage

```bash
./scripts/problems.py list --min-severity 4
./scripts/problems.py close --stale 30            # dry run
./scripts/problems.py close --stale 30 --apply
```

Every `close` is a dry run until `--apply`. Problems whose trigger has
`manual_close` disabled are reported as skipped rather than counted as closed,
because Zabbix will not close them and reporting otherwise hides work.

### `inventory.py` — export and DNS audit

```bash
./scripts/inventory.py --csv hosts.csv
./scripts/inventory.py --check-dns --only-problems
```

Answers three questions that quietly rot in any long-running estate: does the
monitored name resolve, does it resolve to the address Zabbix is polling, and
does that address reverse-resolve to the same name.

### `zbx.py` — Zabbix 7.x API client

The shared library the other tools use.

> Zabbix 7.0 changed API authentication. The token from `user.login` must now be
> sent as an `Authorization: Bearer` header. The `auth` field inside the
> JSON-RPC body is ignored rather than rejected, so scripts written against 6.x
> fail with a permission error that never mentions authentication.

### `templates.py` — templates under version control

```bash
./scripts/templates.py export
./scripts/templates.py import --dry-run
./scripts/templates.py import
```

Idempotent: templates match on UUID, so re-importing updates in place. Deletion
is opt-in behind `--prune`, because `deleteMissing` will remove items added
through the frontend.

No templates ship here. `templates/*.yaml` is gitignored so that exporting your
own configuration and publishing it is a deliberate act.

### Agent rollout

```bash
ZBX_SERVER=10.0.0.20 sudo ./scripts/install-agent.sh
ZBX_SERVER=10.0.0.20 ./scripts/bulk-install-agents.sh --dry-run
```

The bulk script drives `pct exec` on a Proxmox node, so it needs no credentials
inside the guests. One failure never aborts the run.

---

## Quick start

```bash
git clone https://github.com/NetworkBound/zabbix-ops.git
cd zabbix-ops

cp .env.example .env
$EDITOR .env
set -a; . ./.env; set +a

python3 scripts/zbx.py          # connectivity
python3 scripts/reconcile.py    # what is actually wrong
```

Python 3.9 or newer. Nothing to install.

Prefer a scoped API token over a password: *Users → API tokens*, then set
`ZBX_TOKEN` and leave `ZBX_USER` and `ZBX_PASS` empty. A read-only role is
sufficient for everything except template import and problem closure.

## Scheduled checks

Two workflows ship in `.github/workflows/`:

- **`ci.yml`** — lint, unit tests and a secret scan. Runs anywhere.
- **`monitoring-drift.yml`** — live reconciliation. Requires a self-hosted
  runner, since a hosted runner has no route to a private management network.

Gitea Actions reads `.github/workflows/` as well, so the same files serve both.
Setup, required secrets, and advice on which findings should fail a build:
[runners.md](docs/runners.md).

## Tests

```bash
python3 -m unittest discover -s tests -v
```

41 tests, no network required. Comparison and DNS logic are kept separate from
I/O so the parts worth testing can be tested anywhere.

## Direction

[ROADMAP.md](ROADMAP.md) records a survey of the existing Zabbix tooling
ecosystem: what is already well maintained and should not be rebuilt, which gaps
were confirmed against current repositories, and what this project intends to
build next. The short version is that configuration management is well served
and the operational read side is not.

## Documentation

| | |
|---|---|
| [architecture.md](docs/architecture.md) | Collection methods, host grouping, template and severity design |
| [test-environment.md](docs/test-environment.md) | Cloning production into a test instance safely |
| [ha.md](docs/ha.md) | Two-node HA, the floating address, and failover |
| [auto-registration.md](docs/auto-registration.md) | Zero-touch host onboarding, agentless and agent-based |
| [postgresql-timescaledb.md](docs/postgresql-timescaledb.md) | Migrating history to PostgreSQL with TimescaleDB |
| [runners.md](docs/runners.md) | Scheduling these tools on GitHub or Gitea Actions |
| [troubleshooting.md](docs/troubleshooting.md) | Failures that cost real time, and their causes |

## Security

- No credentials are committed. `.env` is gitignored; `.env.example` contains
  placeholders only. CI fails on a private address, a credential-shaped literal,
  a private key, or a tracked `.env`.
- Everything except `templates.py import`, `problems.py close --apply` and the
  agent installers is read-only.
- Template exports contain no macro values. A secret macro is stored on the host
  object and does not leave the server.

## License

MIT. See [LICENSE](LICENSE).
