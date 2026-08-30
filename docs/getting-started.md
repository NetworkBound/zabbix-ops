# Getting started

This walks from an empty directory to a working install against your own Zabbix
server. It takes about ten minutes for the read-only tools. The optional parts
at the end take longer and are worth doing separately, once the basics work.

## Is this for you

These tools assume you run Zabbix 7.0 or newer and administer it yourself. They
are read-heavy: most of what is here tells you whether your monitoring is doing
what you think it is, and only a few commands write anything back.

If you are looking for something to configure Zabbix declaratively, the Ansible
`community.zabbix` collection is a better fit and this does not try to replace
it. See the scope table in the [README](../README.md).

## Requirements

- Python 3.9 or newer. Nothing to install beyond it.
- A Zabbix server you can reach on its API endpoint.
- An account on that server that can read configuration.

Two tools need more: `reconcile.py` needs a Proxmox VE cluster to compare
against, and `ha.py` needs `psql` on your PATH to inspect database replication.
Both work without those, they just report less.

## 1. Get the code

```bash
git clone https://github.com/NetworkBound/zabbix-ops.git
cd zabbix-ops
```

There is no build step and no dependency install. The scripts run directly from
the checkout.

## 2. Create a Zabbix API token

Use a token rather than a password. It can be scoped, revoked without changing
anyone's login, and does not end up in a shell history.

In the frontend: **Users → API tokens → Create API token**. Assign it to a user,
leave the expiry set if your policy wants one, and copy the token when it is
shown — it is not displayed again.

For the read-only tools, that user needs no more than a **read-only role** with
read permission on the host groups you care about. Give it more only when you
start using the commands that write:

| Command | Needs write access |
|---|---|
| `templates.py import`, `promote.py apply` | Templates |
| `problems.py close --apply` | Problem acknowledgement |
| `fix.py ... --apply` | Hosts, interfaces, triggers, maintenance |
| `tmpltest.py`, `clone.py` | Host create and delete on the test instance |

Everything else only reads.

## 3. Configure

```bash
cp .env.example .env
$EDITOR .env
```

At minimum set:

```ini
ZBX_URL=http://10.0.0.20/zabbix/api_jsonrpc.php
ZBX_TOKEN=<the token you just created>
```

`ZBX_URL` must be the full path to `api_jsonrpc.php`, not the frontend URL. A
common first error is pointing it at `http://10.0.0.20/zabbix` and getting an
HTML page back.

Load it into your shell:

```bash
set -a; . ./.env; set +a
```

`.env` is gitignored. Keep it that way.

## 4. Confirm it connects

```bash
python3 scripts/zbx.py
```

```
Connected to http://10.0.0.20/zabbix/api_jsonrpc.php
  API version : 7.4.13
  Hosts       : 75 enabled
  Items       : 13093
  Triggers    : 5008
  Problems    : 31 active
```

If this fails, the message says which part is wrong. The one worth knowing about
in advance: **Zabbix 7.0 changed API authentication.** The token now goes in an
`Authorization: Bearer` header, and the old `auth` field in the request body is
ignored rather than rejected — so a script written against 6.x fails with a
permission error that never mentions authentication. This client uses the header
form, but if you are debugging your own scripts alongside it, that is usually
the cause.

## 5. Run the audit

```bash
python3 scripts/audit.py
```

This is the one to run first. It checks around twenty things across alerting,
suppression, collection, noise, security and capacity, and reports what it
found with the evidence and a suggested fix. It writes nothing.

Expect findings. A server that has been running for a while accumulates them,
and most estates have at least one of: an action that reaches nobody, a
maintenance window that suppresses nothing, hosts whose interface address is
unusable, or several hundred unsupported items quietly consuming poller time.

Work through the high-severity findings before anything else here. They mean
monitoring is not doing what someone believes it is.

To run one category, or to gate a scheduled job:

```bash
python3 scripts/audit.py --only suppression
python3 scripts/audit.py --fail-on high
```

## 6. Optional: compare against Proxmox

`reconcile.py` answers a question the audit cannot: is Zabbix watching the
things that actually exist? It needs a read-only Proxmox token.

On each Proxmox node:

```bash
pveum user add zabbix@pve
pveum aclmod / --users zabbix@pve --roles PVEAuditor
pveum user token add zabbix@pve monitoring --privsep 0
```

`PVEAuditor` is read-only. Copy the printed secret into `.env`:

```ini
PVE_0_NAME=pve1
PVE_0_URL=https://10.0.0.10:8006
PVE_0_TOKEN=zabbix@pve!monitoring=<the secret>
PVE_VERIFY_TLS=false
```

`PVE_0_NAME` must match the node's own hostname as Proxmox knows it. Nodes do
not have to be clustered — declare each one as its own numbered block.

`PVE_VERIFY_TLS` is off because Proxmox ships a self-signed certificate. Turn it
on if your nodes present a trusted one.

```bash
python3 scripts/reconcile.py
python3 scripts/reconcile.py --exclude-group Network Infrastructure
```

The `--exclude-group` flag matters more than it looks. Anything that is not a
Proxmox guest — switches, access points, a UPS, the hypervisors themselves —
will always appear as "orphaned". Exclude those groups so the output is signal
rather than a list you learn to ignore.

If a token is wrong, Proxmox delays the response by about three seconds, which
lands just past the HTTP timeout. You get a timeout that looks exactly like a
network fault. If every item fails at almost exactly 3000ms, check the
credential before the network.

## 7. Optional: a test instance

Once you want to change templates rather than only inspect them, you need
somewhere safe to do it. [test-environment.md](test-environment.md) covers
building one and cloning production into it.

The short version: stand up a second Zabbix, set a global macro `{$ENV}` to
`test` on it, then `clone.py` will copy configuration across. It refuses to
write anywhere without that macro, so production cannot become the destination
by mistyping a URL.

## 8. Optional: put templates in git

```bash
python3 scripts/canon.py export -o templates/
```

This writes each template as canonical JSON — deterministic, so the same
configuration always produces the same bytes and a diff shows only real changes.

`templates/` is gitignored for YAML, JSON and XML, because an export describes
your estate and publishing it should be a decision rather than an accident. If
you want them in your own repository, remove those lines from `.gitignore` after
reading what is in the files.

From there, `promote.py` moves them between servers with a plan you can review,
and refuses removals unless you allow them explicitly. See the README.

## 9. Optional: run it on a schedule

Drift is worth catching every morning rather than whenever someone remembers.
Four workflows ship in `.github/workflows/`:

| Workflow | Runner | What it does |
|---|---|---|
| `ci.yml` | any | Lint, unit tests, secret scan. Touches no Zabbix. |
| `monitoring-drift.yml` | self-hosted | Scheduled `reconcile.py` and DNS audit |
| `test-refresh.yml` | self-hosted | Refreshes the test instance nightly |
| `promote.yml` | self-hosted | Plans template changes on a PR, applies on merge |

Everything except `ci.yml` needs a **self-hosted** runner, because a hosted one
has no route to a management network. [runners.md](runners.md) covers setting one
up on GitHub or Gitea and which secrets to configure.

## Where to go next

| | |
|---|---|
| [architecture.md](architecture.md) | How to structure collection, host groups and severities |
| [test-environment.md](test-environment.md) | Cloning production somewhere you can break things |
| [ha.md](ha.md) | Clustering the server, and the two things that catch people out |
| [auto-registration.md](auto-registration.md) | Onboarding hosts without touching the UI |
| [postgresql-timescaledb.md](postgresql-timescaledb.md) | Moving history to TimescaleDB |
| [troubleshooting.md](troubleshooting.md) | Failures that cost real time |
| [runners.md](runners.md) | Scheduling any of this |
