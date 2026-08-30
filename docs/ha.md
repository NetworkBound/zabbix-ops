# Two-site HA for Zabbix

## What Zabbix HA is, and what it is not

Zabbix native HA (6.0+) runs several `zabbix_server` processes against **one
shared database**. One node is active; the others stand by. If the active node
stops writing its heartbeat, a standby takes over in roughly a minute.

It protects you against **losing a server**. It does nothing about losing the
database, and nothing about losing a site — because every node depends on the
same database, and Zabbix does not replicate it.

That is the whole reason this is two mechanisms rather than one:

| Layer | Mechanism | Failover |
|---|---|---|
| Zabbix server | Native HA cluster | **Automatic**, ~1 minute |
| Database | PostgreSQL streaming replication | **Deliberate** — a human promotes |

If you take one thing from this page: **two Zabbix servers with two separate
databases is not HA.** It is two monitoring systems that both alert you, drift
apart in configuration, and disagree about history. Native HA requires a shared
database, full stop.

## Target architecture

```
            SITE A (primary)                      SITE B
  ┌────────────────────────────┐      ┌────────────────────────────┐
  │  zabbix-a   HANodeName=A   │◀────▶│  zabbix-b   HANodeName=B   │
  │  status: active            │  DB  │  status: standby           │
  └─────────────┬──────────────┘      └─────────────┬──────────────┘
                │ writes                            │ reads (idle until active)
                ▼                                   ▼
  ┌────────────────────────────┐      ┌────────────────────────────┐
  │  PostgreSQL 16  PRIMARY    │─────▶│  PostgreSQL 16  STANDBY    │
  │  + TimescaleDB             │ WAL  │  + TimescaleDB             │
  └────────────────────────────┘      └────────────────────────────┘
                        ▲
                        │ both Zabbix nodes point at whichever
                        │ database is currently primary
```

Both Zabbix nodes use the **same `DBHost`**. During normal operation that is
site A. After a promotion you change it on both nodes — see the failover
runbook below.

Agents and proxies connect to whichever server holds the active role. Point them
at both: `ServerActive=zabbix-a,zabbix-b`.

## Why the database failover is not automatic

Automatic promotion is a trap, and it is worth understanding before you wish it
were there.

Site B cannot distinguish "site A is down" from "I cannot reach site A". If it
promotes during a network partition while site A is alive and still collecting,
you have two primaries taking writes. The recovery is rebuilding one side from
scratch and losing everything it gathered in the meantime — considerably worse
than the outage you were trying to survive.

A human, with a phone and a second opinion, can tell the difference. So server
failover is automatic (a standby server does nothing until it takes the role,
so promoting one costs nothing if you are wrong), and database promotion asks
first.

`promote-standby.sh --check` does the check for you: it confirms the standby is
current, and refuses if the old primary is still accepting connections on 5432.

---

## Building it

Everything below is in [`deploy/ha/`](../deploy/ha/). Every script supports
`--dry-run`; use it first, every time.

### Prerequisites

- Site B needs a host that can run PostgreSQL and `zabbix-server`, reachable
  from site A on **5432** (replication) and **10051** (agents/proxies).
- Disk at site B for a full copy of the database, plus headroom.
- Routing between sites. A WireGuard tunnel is fine — replication is a single
  TCP stream and tolerates modest latency well.

### 1. Prepare the primary

```bash
sudo ./deploy/ha/setup-replication.sh primary --standby-ip <site-b-ip> --dry-run
sudo ./deploy/ha/setup-replication.sh primary --standby-ip <site-b-ip>
```

Creates a `replicator` role, a physical replication slot, and a `pg_hba` entry.
Additive and safe: it does not touch data, and only restarts PostgreSQL if
`wal_level` or `max_wal_senders` genuinely had to change.

> **`listen_addresses` is the one thing it will not change for you.** If it is
> `localhost`, the standby cannot connect, and the script says so rather than
> silently widening what your database listens on. Change it deliberately.

The replication password is generated on the primary and written to
`/root/.pg_replication_credentials` (mode 0600). Copy it to the standby by hand.
It is never printed and never committed.

### 2. Build the standby

```bash
scp /root/.pg_replication_credentials root@<site-b>:/root/
sudo ./deploy/ha/setup-replication.sh standby --primary-ip <site-a-ip>
```

> **This erases the target's data directory.** `pg_basebackup` requires an empty
> one. The script refuses if the target already holds a `zabbix` database unless
> you pass `--i-know-this-erases-the-target`.

Streaming a 5 GB database takes a few minutes on a LAN, longer over a tunnel.
`-R` writes `standby.signal` and `primary_conninfo`, so it comes up as a standby
with no further configuration.

TimescaleDB needs no special handling here: physical replication copies the
entire cluster byte-for-byte, hypertables and all. (Logical replication is a
different story and is not what this uses.)

### 3. Enable Zabbix HA on both nodes

```bash
# site A
sudo ./deploy/ha/enable-zabbix-ha.sh --name zbx-site-a --address <site-a-ip>
# site B
sudo ./deploy/ha/enable-zabbix-ha.sh --name zbx-site-b --address <site-b-ip>
```

Sets `HANodeName` and `NodeAddress`, backs up the config, restarts, and checks
the server actually came back.

### 4. Verify

```bash
python3 scripts/ha.py
```

You want exactly one `active`, at least one `standby`, and a connected replica
with low lag.

> **Two nodes both reporting `active` means split brain** — they cannot see each
> other's heartbeats. Almost always they are pointed at different databases.
> Check `DBHost` on both.

---

## Failover

### The server fails (site A's Zabbix process dies)

Nothing to do. A standby takes the active role within about a minute. Confirm:

```bash
python3 scripts/ha.py
```

### The site fails (site A's database is gone)

```bash
# On site B — check first. It refuses if the old primary is still up.
sudo ./deploy/ha/promote-standby.sh --check --primary-ip <site-a-ip>

# If it says promotion is safe:
sudo ./deploy/ha/promote-standby.sh --promote --primary-ip <site-a-ip>
```

Then, and this is the step people forget:

1. **Point both Zabbix nodes at the new database.** Update `DBHost` in
   `zabbix_server.conf` on *both* nodes and restart. Until you do, the cluster
   is still trying to reach a database that no longer exists.
2. `python3 scripts/ha.py` to confirm the cluster is healthy again.
3. **Rebuild site A as a standby of site B** when it returns. It cannot simply
   be restarted — it still believes it is a primary, and starting it alongside
   the new primary is precisely the split brain you avoided. Use `pg_rewind`, or
   rebuild with `setup-replication.sh standby --primary-ip <site-b>`.

Promotion is one way. Plan on failing back deliberately, later, not
automatically.

---

## Monitoring the monitoring

The obvious gap: if Zabbix is down, Zabbix cannot tell you.

- `scripts/ha.py --require-ha --require-replication` exits non-zero on any
  problem. Run it from something that is not Zabbix — a cron job, a CI runner,
  an uptime checker.
- Watch the **replication slot**. An inactive slot retains WAL forever and will
  eventually fill the primary's disk — a replication setup that fails *closed*
  onto your production database. `ha.py` reports inactive slots for this reason.
- Watch replay lag. A standby hours behind is not a standby, it is a slow
  backup.

## Current state

As of the last check, this estate is **standalone**: one Zabbix server, one
database, no HA node name set, no replica connected. `wal_level` is already
`replica` with 10 WAL senders, so the primary side needs no reconfiguration —
replication can be started without a restart.

The blocker is site B: there is no compute at the second location yet. The
scripts here are written and validated against the real primary in dry-run, and
are ready to run once that host exists.

```
── Zabbix HA cluster
   (unnamed — standalone)  active  localhost:10051
   ! HA is not configured — the server is standalone.

── PostgreSQL replication
   role: primary
   wal_level=replica max_wal_senders=10
   replicas: none connected
   ! no standby is connected — the database has no replica.
```
