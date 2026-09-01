# zabbix-ops

Operational tooling for Zabbix 7.x: auditing whether monitoring is doing what
you believe it is, reconciling it against inventory, triaging what has quietly
stopped working, safe promotion from test to production, and high availability.

Standard library only. No dependencies to install, nothing to keep upgraded.

New here? [docs/getting-started.md](docs/getting-started.md) goes from an empty
directory to a working install, including creating the API tokens.

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
| `delivery` | Did notifications actually arrive? Configuration being correct is not the same as delivery working, and nothing in the frontend surfaces the difference. |
| `suppression` | Is monitoring quietly switched off somewhere — a maintenance window that suppresses nothing, or one that never ends? |
| `collection` | Is data arriving? Unsupported items, hosts with an unusable interface address. |
| `noise` | Does the problem list mean anything? Trigger dependencies, week-old problems, untagged hosts. |
| `security` | Encrypted transport, token expiry, super-admin sprawl. |
| `capacity` | Proxy version skew, offline proxies, server processes running hot, cache pressure. |

The `delivery` check is the one to run first. Everything can be configured
correctly — action enabled, user has media, media type enabled — while every
send fails because a webhook was revoked at the far end, an SMTP relay started
requiring authentication, or the host a script posts to moved. The alert history
holds the answer and almost nobody looks at it, so a silent alerting outage can
run for weeks with the problem list looking entirely normal.

The suppression checks are the next ones worth running. A one-time maintenance
period whose slot has elapsed still displays as active, so the hosts it names
are believed to be suppressed while they alert normally — and nothing in the
frontend indicates this.

### `notify.py` — prove notifications reach somewhere

```bash
./scripts/notify.py history --days 7    # what the alert log actually says
./scripts/notify.py users               # who can be reached right now
./scripts/notify.py verify              # dry run
./scripts/notify.py verify --apply      # sends one real message per media type
```

`audit.py --only delivery` tells you something is wrong. This tells you what.

`history` groups the alert log by media type and separates two failure shapes
that need different responses: a media type that has **never** delivered is
broken and nothing depending on it is being notified, while one that delivers
intermittently is usually a rate limit or a flaky relay. Alerts discarded before
they reached any media type are counted separately, because those are a routing
fault rather than a delivery one.

`users` answers "if this fires at 3am, who actually gets it". A user is
unreachable for more than one reason — no media at all, a disabled medium, a
medium whose severity filter excludes the alert, or one outside its time period
— and the report names which. It then lists users that an enabled action
targets but cannot reach, which is the common failure after someone is added to
a group an action points at.

Two things it will not do. It never requests `output=extend` on media types,
because that returns SMTP passwords and webhook secrets. And webhook `sendto`
values are truncated to scheme and host, since chat integrations store the full
webhook token there and printing it would turn a report into a leak.

`verify` exits non-zero when an enabled media type fails, so a scheduled job can
gate on it. A media type nothing has been tried through is reported as
unverified rather than failed — claiming a path is broken on no evidence makes
the job untrustworthy, and a job people stop believing is worse than no job.

### `unsupported.py` — triage items that stopped collecting

```bash
./scripts/unsupported.py list                       # grouped by cause
./scripts/unsupported.py explain                    # what each cause means
./scripts/unsupported.py list --min-count 10 --fail-over 200
./scripts/unsupported.py disable --error "No Such Object"          # dry run
./scripts/unsupported.py disable --error "No Such Object" --apply
```

An unsupported item is still scheduled. It takes a poller slot on every
interval, fails, and returns nothing. They accumulate silently: a template
change, a firmware upgrade or an agent upgrade turns a working key into a
failing one, the item stops producing values, and the frontend still lists it as
monitored. Nothing alerts on it, so the count only goes up until someone looks.

The value is the grouping rather than the list. Several hundred unsupported
items are usually a handful of causes repeated across every host sharing a
template, and each cause has one fix that clears the whole group. Grouping on
the raw error text does not achieve this — a failed SNMP walk embeds the entire
response in its message, so every item produces a unique string. The signature
keeps the data type and the failure reason and discards the payload, which
collapses hundreds of items into the two or three real problems behind them.

`disable` refuses to run without a selector, and refuses templated items
outright: disabling the inherited copy leaves the template still producing it on
every other host, so the fix belongs on the template. For items created by
discovery it names the rule and the template that owns it.

### `triggerlint.py` — find triggers that read correctly and are wrong

```bash
./scripts/triggerlint.py check
./scripts/triggerlint.py check --min-confidence high
./scripts/triggerlint.py check --fail-on inverted_sense
./scripts/triggerlint.py check --json
```

A trigger that is wrong in an obvious way gets found within a week. The ones
that survive are the ones that read correctly. A CPU trigger built on the
**idle** percentage and wrapped in `>85` sits in the list looking like every
other CPU trigger, and fires when the machine is asleep instead of when it is
busy.

| Rule | Catches |
|---|---|
| `inverted_sense` | The expression measures the opposite of what the name says — idle versus utilisation, free versus used, available versus consumed |
| `nodata_no_manual_close` | A `nodata()` trigger with manual close disabled. If the host never returns, the problem cannot be cleared by hand |
| `missing_item` | The referenced item no longer exists, or is disabled or unsupported. The trigger looks configured and can never fire |
| `hardcoded_threshold` | The name advertises a macro while the expression compares against a literal, so setting the macro on a host does nothing |
| `severity_mismatch` | Severity contradicts the wording — a trigger named for an outage filed as a warning |
| `missing_dependency` | A guest trigger with no dependency on its hypervisor, so one host failure produces one alert per guest |
| `counter_threshold` | A monotonic counter compared against a fixed number, which is true forever once crossed |

Read-only; it never writes to Zabbix. It parses the **stored** expression rather
than asking the API to expand it, because expansion substitutes user macros
inside item keys — which breaks item identity and would print credentials such
as `{$PG.PASSWORD}` into the report.

By default it examines trigger *definitions* only: not inherited copies, not
discovered triggers, not vendor templates. Those are the ones you can actually
edit, and it is usually a small fraction of the total.

Findings carry a confidence. `high` means the expression cannot plausibly mean
what its description says; `medium` has an innocent reading but it is the rarer
one; `low` is worth a glance. Anything with a defensible alternative reading is
reported low and explained rather than asserted, because a linter people learn
to skip is worse than no linter.

### `agentfleet.py` — agent configuration across a Proxmox fleet

```bash
./scripts/agentfleet.py audit
./scripts/agentfleet.py audit --json
./scripts/agentfleet.py audit --fail-on server
./scripts/agentfleet.py audit --only 100,101
```

Run this **on** a Proxmox node. Each guest's agent configuration is read through
`pct exec`, so nothing has to be installed, opened or authenticated inside the
containers.

Agent configuration drifts from what the server believes, and it drifts
silently. `Server=` is an allowlist: the agent refuses connections from any
address not in it. Add a second HA node without updating the fleet and every
passive check fails the moment the standby takes over — the cluster fails over
into a blind spot, which is worse than having no HA, because by then you believe
you are covered.

| Check | Catches |
|---|---|
| `hostname` | Agent `Hostname=` not matching the Zabbix host name. Zabbix matches byte-for-byte, so case and suffix differences are reported separately from real mismatches |
| `server` | A poller address missing from the agent's allowlist |
| `serveractive` | Active checks pointed somewhere that will not accept them |
| `version` | Agent versions behind the server, and spread across the fleet |
| `coverage` | Guests running an agent that Zabbix does not monitor, and the reverse |
| `tls` | Unencrypted agent traffic, and hosts whose TLS settings disagree with the agent's |

Two details worth knowing. Guests are paired to Zabbix hosts by **address**, not
name — a container whose Proxmox hostname, agent `Hostname=` and Zabbix host name
all differ is common, and name matching reports it as both an unmonitored guest
and an orphaned host when nothing is wrong.

And Zabbix does not record where a proxy polls from: an active proxy's
`proxy.address` is `127.0.0.1`, so for hosts behind one the allowlist check has
no address to look for. Pass `--proxy-address NAME=ADDR` to supply it. Failing
that the tool infers one and labels the finding as inferred, rather than
reporting a clean result it cannot support.

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

### `canon.py` — make exports diffable

```bash
./scripts/canon.py export -o templates/         # Zabbix -> canonical files
./scripts/canon.py normalise export.json
./scripts/canon.py diff old.json new.json
```

A Zabbix export cannot be usefully diffed as it comes. The version header
changes on every server upgrade, list ordering is not consistent between
servers, and default emission has shifted between versions — so a one-line
change produces a diff nobody can review, and version-controlling configuration
gets abandoned.

Canonical form removes the version header, sorts every list whose order is not
semantically meaningful by a stable key, leaves alone the ones where order *is*
meaningful (preprocessing steps, dashboard layout, LLD overrides), and drops
empty values. UUIDs are untouched: import matches on UUID first, so changing
them turns an update into a duplicate.

Measured on a real template exported from two servers on different patch
versions: 92 changed lines by line diff, 46 actual changes semantically, and
none of them spurious.

The diff classifies every change as additive, mutating or destructive, and knows
which removals lose data:

```
DESTRUCTIVE  (2)
  templates[Linux by Agent].items[agent.ping]
    !! deletes the item and all of its collected history
```

Works in JSON rather than YAML — Zabbix handles both, and the standard library
parses one of them.

### `promote.py` — git as the source of truth

```bash
./scripts/promote.py plan  templates/*.json
./scripts/promote.py apply templates/*.json
./scripts/promote.py drift templates/*.json
```

`plan` diffs each file against what the target currently holds. `apply` imports
them, refusing anything destructive unless explicitly allowed. `drift` runs the
comparison in reverse and reports where the server has been edited outside git,
which is what happens during an incident.

> `configuration.importcompare` is not usable as a gate. It compares host groups
> and templates only, validates nothing semantic, and returns different results
> depending on the caller's permissions. In this project it reported no changes
> for an import that then failed outright because a referenced object was
> missing from the target.

`.github/workflows/promote.yml` wires this to pull requests: the plan is posted
as the job summary so a reviewer sees the exact change before approving,
destructive changes block a PR, and after applying it re-plans to confirm the
server converged on the files.

### `problems.py` — bulk triage

```bash
./scripts/problems.py list --min-severity 4
./scripts/problems.py close --stale 30            # dry run
./scripts/problems.py close --stale 30 --apply
```

Every `close` is a dry run until `--apply`. Problems whose trigger has
`manual_close` disabled are reported as skipped rather than counted as closed,
because Zabbix will not close them and reporting otherwise hides work.

### `tmpltest.py` — test a template before it ships

```bash
./scripts/tmpltest.py run templates/*.json
./scripts/tmpltest.py run templates/linux.json --against 10.0.0.55
```

There is no established way to test a Zabbix template. The frontend has item and
trigger test buttons, and people mirror trigger expressions into calculated
items to watch them evaluate — all manual, all after the fact.

This imports the template into a disposable instance, attaches it to a throwaway
host, and asserts:

| Check | Catches |
|---|---|
| The template imports | Trigger expressions naming items that do not exist, malformed keys, broken preprocessing — Zabbix validates references on import |
| It links to a host | Missing interface types, conflicting keys |
| Items are created | A template that imports but produces nothing |
| Trigger expressions resolve | Triggers whose functions reference nothing |
| Discovery rules have prototypes | A rule that will discover nothing |
| Nothing goes unsupported | A wrong OID, a bad key parameter, a macro that never resolves — needs `--against` a real address |

The host is removed afterwards. Refuses to run anywhere not marked `{$ENV}`
non-production, since it creates and deletes hosts.

The interface types are derived from the template's own item types, because a
template can only link to a host carrying the interfaces its items need — and
the resulting error reads like a broken template rather than a missing
interface.

### `inventory.py` — export and DNS audit

```bash
./scripts/inventory.py --csv hosts.csv
./scripts/inventory.py --check-dns --only-problems
```

Answers three questions that quietly rot in any long-running estate: does the
monitored name resolve, does it resolve to the address Zabbix is polling, and
does that address reverse-resolve to the same name.

### `walk2tmpl.py` — build an SNMP template from a device

```bash
snmpwalk -v2c -c public -On 10.0.0.5 1.3.6.1.2.1 > device.walk
./scripts/walk2tmpl.py device.walk --name "My Switch by SNMP" -o template.json
./scripts/tmpltest.py run template.json --against 10.0.0.5 \
    --macro '{$SNMP_COMMUNITY}=public'
```

The canonical MIB-to-Zabbix tool emits Zabbix 3.x XML and nothing has replaced
it. Generating from a MIB is also the wrong starting point: most of a vendor MIB
is not implemented on any given device, so you get thousands of items that will
sit unsupported forever and someone has to prune them by hand.

A walk contains exactly what the device answers, so everything generated from it
is known to work on that hardware.

It produces the modern structure rather than one item per OID:

- One `snmp.walk[...]` master item per subtree and per table. The device is
  polled once and the result split locally.
- Dependent items with `SNMP_WALK_VALUE` preprocessing.
- A discovery rule per detected table, dependent on that table's master, with
  item prototypes underneath.
- Counters get change-per-second preprocessing, since a raw counter is not what
  anyone alerts on.

Everything ships **disabled**. A generated template is a starting point, not a
finished one.

On a 24-port switch: 579 walked OIDs became 7 scalar items, 3 walk masters and
one discovery rule with 22 prototypes — not 579 items. Verified end to end
against the device: the template imported, discovery ran, and produced items
named after the actual ports.

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

No templates ship here. `templates/` is gitignored for YAML, JSON and XML, so
exporting your own configuration and publishing it is a deliberate act rather
than an accident.

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

240 tests, no network required. Comparison, parsing and classification logic
are kept separate from I/O, so the parts worth testing can be tested anywhere —
including on a machine with no Zabbix server to point at.

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
| [getting-started.md](docs/getting-started.md) | From nothing to a working install |
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
