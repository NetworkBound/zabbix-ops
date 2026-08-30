# A test Zabbix you can actually break

The point of a test instance is to develop templates, triggers and webhooks
without a mistake reaching production or paging anyone. That only works if the
clone is genuinely inert — and a naive copy of production is the opposite of
inert.

## The two ways a cloned test instance bites you

**It pages real people.** Production's actions and media types come across with
the configuration, still pointing at your real Discord webhook, your real SMTP
relay, your real ntfy topic. The first trigger that fires in test sends a real
notification about a problem that does not exist. If you are iterating on
trigger expressions, that is not one alert — it is dozens.

**It polls production.** Clone the host list and a second Zabbix server starts
hammering the same agents. You double the load on every guest and create a
second, conflicting opinion about whether each one is up.

`clone.py` treats both as defaults rather than options:

- Every action and media type is **disabled after import**, always, unless you
  pass `--keep-notifications`.
- Hosts are **not cloned** unless you ask for them with `--include hosts`, and
  when you do they are created **disabled**.

## The `{$ENV}` guard rail

The destination must carry a global macro `{$ENV}` set to one of
`test` / `dev` / `staging` / `lab` / `sandbox`. `clone.py` refuses to write
anywhere that does not.

This exists because the failure mode is catastrophic and the mistake is trivial:
one wrong environment variable and you have overwritten production's templates
with an older copy of themselves. Production does not carry the macro, so it
cannot be a destination — even with `--force`, which only bypasses the marker
check, never the "source and destination are the same instance" check.

Set it once, on the test instance:

*Administration → Macros → `{$ENV}` = `test`*

## Using it

```bash
cp .env.example .env      # fill in both prod and test blocks
set -a; . ./.env; set +a

python3 scripts/clone.py --dry-run    # what would change
python3 scripts/clone.py              # do it
```

| Flag | Effect |
|---|---|
| *(default)* | Custom templates, all groups, all media types |
| `--all-templates` | Also copy the ~313 vendor templates |
| `--include hosts` | Also copy hosts, created disabled |
| `--mirror` | Delete items/triggers in test that prod no longer has |
| `--keep-notifications` | **Dangerous.** Leave actions and media types enabled |
| `--force` | Bypass the `{$ENV}` check |

Vendor templates are skipped by default because the test instance already has
its own copies from its own install. What you actually want in test is the
handful you wrote.

## Media types that will not clone

Zabbix strips credentials from a configuration export — correctly. But it leaves
the field present and empty, so an SMTP media type with authentication exports
with `username: ""`, which the importer then rejects as *"cannot be empty"*.

In a batch import, one such media type fails **all** of them. So `clone.py`
imports media types one at a time, and reports by name the ones that could not
cross:

```
    media types        41/43 imported
      skipped: Gmail (credentials stripped by export)
      skipped: Office365 (credentials stripped by export)
```

Re-enter those credentials in test by hand. That is the right outcome — they are
production secrets and should not be cloned into a test box anyway.

## Developing against it

**Templates and triggers.** Iterate in test, export when happy, and promote the
YAML through git rather than by clicking in production:

```bash
# in test
python3 scripts/templates.py export --prefix "My Template"
git add templates/ && git commit

# then against production, with a dry run first
ZBX_URL=<prod> python3 scripts/templates.py import --dry-run templates/my-template.yaml
ZBX_URL=<prod> python3 scripts/templates.py import templates/my-template.yaml
```

**Webhooks.** They arrive disabled and pointing at production endpoints. Before
enabling one in test, repoint it at something that cannot bother anyone — a
local request bin, a scratch ntfy topic, a webhook.site URL. Then enable that
single media type, never the whole set.

**Actions.** Same: enable one, deliberately, once its media type is safe.

## Refreshing

Re-running `clone.py` is idempotent — objects match on UUID, so it updates in
place. Run it whenever production has drifted ahead.

`--mirror` additionally deletes items and triggers that production no longer
has. Use it when you want test to be a faithful copy rather than a superset;
leave it off when test contains work in progress you do not want removed.

Scheduling it nightly keeps test honest without anyone remembering to. See
[runners.md](runners.md) — it needs a self-hosted runner, since both instances
are on a private network.

## A reference deployment

| | |
|---|---|
| Host | An unprivileged LXC container, 2 vCPU / 4 GB / 20 GB |
| Stack | Zabbix server, PostgreSQL, nginx frontend, and a Zabbix proxy, all in the one container |
| Marker | `{$ENV} = test` |
| Credentials | An `0600` environment file inside the container, never in a repository |

Sizing assumes configuration only. The instance holds no meaningful history, so
it needs no room for one. Set it to start on boot, and treat it as disposable:
if you break it, rebuild and re-clone. Nothing in it is precious by design.

Running the test proxy inside the same container is deliberate. It keeps the
whole test environment to one host, and it makes stopping all test polling a
single `systemctl stop`. Give the proxy its own `ListenPort` — the server is
already using 10051 and the proxy will not start alongside it otherwise.

> One gotcha from building it: loading the schema as the `postgres` superuser
> leaves every table owned by `postgres`, and the `zabbix` role cannot read
> them. Zabbix reports this as **"database is not a Zabbix database"**, which
> sends you looking at the schema when the problem is ownership. Load the schema
> as the `zabbix` user, or reassign afterwards.
