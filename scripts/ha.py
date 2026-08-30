#!/usr/bin/env python3
"""Report the state of a Zabbix HA cluster and its database replication.

    ./scripts/ha.py                    # full report
    ./scripts/ha.py --json             # machine-readable
    ./scripts/ha.py --require-ha       # non-zero exit if HA is not healthy

What Zabbix HA actually is, because the name oversells it
---------------------------------------------------------

Zabbix native HA (6.0+) runs several ``zabbix_server`` processes that all point
at **the same database**. One is active, the rest stand by; if the active node
stops updating its heartbeat, a standby takes over within roughly a minute.

That means it protects you against **losing a server**, not against losing a
site. The database is a single shared dependency, and Zabbix does nothing about
it. If the database is gone, every node in the cluster is equally dead.

Cross-site redundancy therefore needs two independent mechanisms:

  * **Zabbix HA** — automatic server failover.        (Zabbix does this.)
  * **PostgreSQL streaming replication** — a promotable standby of the database
    at the second site.                               (You do this.)

Only the first is automatic. Promoting a standby is a deliberate act, because
an automatic promotion during a WAN partition gives you two primaries and a
split brain that is far worse than the outage.

This tool reports on both, and is explicit about which parts are missing.

Replication status needs a database connection, so ``psql`` must be on PATH and
``PGDSN`` (or the standard PG* variables) must point at the node you want to
inspect. Without it the Zabbix half is still reported.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from zbx import ZabbixError, connect_or_exit  # noqa: E402

#: hanode.get status codes.
HA_STATUS = {"0": "standby", "1": "stopped", "2": "unavailable", "3": "active"}

#: A node whose heartbeat is older than this is not coming back on its own.
STALE_AFTER_S = 180


def ha_nodes(z) -> list[dict]:
    try:
        nodes = z.call("hanode.get", {"output": "extend"})
    except ZabbixError:
        return []
    now = time.time()
    out = []
    for n in nodes:
        last = int(n.get("lastaccess") or 0)
        out.append({
            "name": n.get("name") or "",
            "address": n.get("address"),
            "port": n.get("port"),
            "status": HA_STATUS.get(n.get("status"), n.get("status")),
            "age_s": int(now - last) if last else None,
        })
    return sorted(out, key=lambda x: (x["status"] != "active", x["name"]))


def ha_verdict(nodes: list[dict]) -> dict:
    """Is this a real HA cluster, and is it healthy?

    A single node with an empty name is what a *standalone* server registers.
    Zabbix still writes a row, so the presence of a node proves nothing — the
    name being set is what indicates HANodeName was configured.
    """
    named = [n for n in nodes if n["name"]]
    active = [n for n in nodes if n["status"] == "active"]
    standby = [n for n in nodes if n["status"] == "standby"]
    stale = [n for n in named
             if n["age_s"] is not None and n["age_s"] > STALE_AFTER_S
             and n["status"] in ("active", "standby")]

    problems = []
    if not named:
        problems.append(
            "HA is not configured — the server is standalone. Set HANodeName in "
            "zabbix_server.conf on each node (a node row with an empty name is "
            "what a standalone server registers)."
        )
    else:
        if len(named) < 2:
            problems.append(
                f"only {len(named)} HA node registered — a cluster needs at least 2 "
                "for failover to be possible."
            )
        if not active:
            problems.append("no node is currently active.")
        if len(active) > 1:
            problems.append(
                f"{len(active)} nodes report active — split brain. Nodes are not "
                "seeing each other's heartbeats through the database."
            )
        if named and not standby and len(named) > 1:
            problems.append("no node is in standby — nothing to fail over to.")
        for n in stale:
            problems.append(
                f"node {n['name']!r} last checked in {n['age_s']}s ago "
                f"(> {STALE_AFTER_S}s) — treat it as down."
            )

    return {
        "configured": bool(named),
        "nodes": len(named),
        "active": [n["name"] for n in active],
        "standby": [n["name"] for n in standby],
        "problems": problems,
    }


def _psql(dsn: str, sql: str) -> str | None:
    """Run a query, or return None if psql is unavailable or the query fails."""
    if not shutil.which("psql"):
        return None
    cmd = ["psql", "-tAX", "-c", sql]
    if dsn:
        cmd[1:1] = [dsn]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def replication(dsn: str) -> dict:
    """Streaming replication state, from whichever side we are pointed at."""
    if not shutil.which("psql"):
        return {"available": False,
                "reason": "psql not on PATH — cannot inspect replication"}

    in_recovery = _psql(dsn, "select pg_is_in_recovery()")
    if in_recovery is None:
        return {"available": False,
                "reason": "could not connect (set PGDSN or the standard PG* variables)"}

    info: dict = {"available": True, "role": "standby" if in_recovery == "t" else "primary"}

    if in_recovery == "t":
        # On a standby: how far behind are we, and is the stream connected?
        lag = _psql(dsn, "select coalesce(extract(epoch from now() - "
                         "pg_last_xact_replay_timestamp())::int, -1)")
        info["replay_lag_s"] = int(lag) if lag and lag.lstrip("-").isdigit() else None
        info["receiving"] = _psql(dsn, "select count(*) from pg_stat_wal_receiver") not in (None, "0")
    else:
        # On a primary: who is connected, and how far behind are they?
        rows = _psql(dsn,
                     "select application_name, state, "
                     "coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn),0)::bigint "
                     "from pg_stat_replication")
        replicas = []
        for line in (rows or "").splitlines():
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) == 3:
                replicas.append({"name": parts[0], "state": parts[1],
                                 "replay_lag_bytes": int(parts[2] or 0)})
        info["replicas"] = replicas
        slots = _psql(dsn, "select slot_name, active::text from pg_replication_slots")
        info["slots"] = [
            {"name": p.split("|")[0], "active": p.split("|")[1] == "t"}
            for p in (slots or "").splitlines() if "|" in p
        ]
        for key, sql in (("wal_level", "show wal_level"),
                         ("max_wal_senders", "show max_wal_senders")):
            info[key] = _psql(dsn, sql)
    return info


def replication_problems(rep: dict) -> list[str]:
    if not rep.get("available"):
        return []
    problems = []
    if rep["role"] == "primary":
        if rep.get("wal_level") not in ("replica", "logical"):
            problems.append(
                f"wal_level is {rep.get('wal_level')!r} — streaming replication "
                "requires 'replica' or 'logical'."
            )
        if not rep.get("replicas"):
            problems.append(
                "no standby is connected — the database has no replica, so a site "
                "failure loses it entirely."
            )
        for s in rep.get("slots", []):
            if not s["active"]:
                problems.append(
                    f"replication slot {s['name']!r} is inactive — it is retaining "
                    "WAL for a standby that is not connected, and will fill the disk."
                )
    else:
        if not rep.get("receiving"):
            problems.append("standby is not receiving WAL — the stream is broken.")
        lag = rep.get("replay_lag_s")
        if lag is not None and lag > 300:
            problems.append(f"standby is {lag}s behind the primary.")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--require-ha", action="store_true",
                    help="exit non-zero unless the HA cluster is healthy")
    ap.add_argument("--require-replication", action="store_true",
                    help="exit non-zero unless a standby is connected and current")
    ap.add_argument("--dsn", default=os.environ.get("PGDSN", ""),
                    help="PostgreSQL connection string (default: $PGDSN, else PG* vars)")
    args = ap.parse_args()

    z = connect_or_exit()
    nodes = ha_nodes(z)
    verdict = ha_verdict(nodes)
    rep = replication(args.dsn)
    rep_problems = replication_problems(rep)

    if args.json:
        print(json.dumps({"ha": verdict, "nodes": nodes, "replication": rep,
                          "replication_problems": rep_problems}, indent=2))
    else:
        print(f"Zabbix {z.version()} at {z.url}\n")
        print("── Zabbix HA cluster")
        if not nodes:
            print("   no node records at all (Zabbix < 6.0, or no permission)")
        for n in nodes:
            age = f"{n['age_s']}s ago" if n["age_s"] is not None else "never"
            print(f"   {n['name'] or '(unnamed — standalone)':<28} "
                  f"{n['status']:<12} {n['address']}:{n['port']:<8} seen {age}")
        for p in verdict["problems"]:
            print(f"   ! {p}")
        if verdict["configured"] and not verdict["problems"]:
            print("   healthy")

        print("\n── PostgreSQL replication")
        if not rep.get("available"):
            print(f"   not inspected: {rep.get('reason')}")
        else:
            print(f"   role: {rep['role']}")
            if rep["role"] == "primary":
                print(f"   wal_level={rep.get('wal_level')} "
                      f"max_wal_senders={rep.get('max_wal_senders')}")
                for r in rep.get("replicas", []):
                    print(f"   replica {r['name']:<20} {r['state']:<12} "
                          f"{r['replay_lag_bytes']} bytes behind")
                if not rep.get("replicas"):
                    print("   replicas: none connected")
                for s in rep.get("slots", []):
                    print(f"   slot {s['name']:<24} {'active' if s['active'] else 'INACTIVE'}")
            else:
                print(f"   receiving WAL: {rep.get('receiving')}  "
                      f"replay lag: {rep.get('replay_lag_s')}s")
            for p in rep_problems:
                print(f"   ! {p}")

    rc = 0
    if args.require_ha and (verdict["problems"] or not verdict["configured"]):
        rc = 1
    if args.require_replication and (rep_problems or not rep.get("available")):
        rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
