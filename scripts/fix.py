#!/usr/bin/env python3
"""Remediate findings that audit.py reports.

    ./scripts/fix.py interfaces              # dry run
    ./scripts/fix.py interfaces --apply
    ./scripts/fix.py dependencies --apply
    ./scripts/fix.py maintenance --apply

Every subcommand is a dry run until ``--apply``. Each prints exactly what it
would change first, because these write to a production monitoring server and
there is no undo beyond doing the inverse by hand.

Nothing here deletes anything. The most destructive operation available is
setting an interface address or adding a trigger dependency, both of which are
reversible in the frontend.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pve import ProxmoxError, connect_all_or_exit  # noqa: E402
from zbx import ZabbixError, connect_or_exit  # noqa: E402

UNROUTABLE = ("", "0.0.0.0", "::")


def _guest_addresses() -> dict[str, dict]:
    """Name -> guest record, from every configured Proxmox node."""
    out = {}
    for node in connect_all_or_exit():
        try:
            for g in node.guests():
                out[g["name"].strip().lower()] = g
        except ProxmoxError as e:
            print(f"  ! {e}", file=sys.stderr)
    return out


# --------------------------------------------------------------------------
def fix_interfaces(z, args) -> int:
    """Give hosts with an unusable interface the address the inventory holds.

    A 0.0.0.0 interface is what auto-registration produces when the action has
    no interface operation. The host is created, templates attach, triggers
    evaluate — and every check fails because there is nowhere to poll. It is
    invisible in the frontend unless you open each host's interface tab.
    """
    guests = _guest_addresses()
    hosts = z.call("host.get", {"output": ["hostid", "host", "status"],
                                "selectInterfaces": ["interfaceid", "ip", "dns",
                                                     "useip", "main", "type"]})
    planned, unknown = [], []
    for h in hosts:
        if h["status"] != "0":
            continue
        for iface in h.get("interfaces") or []:
            if iface.get("useip") != "1" or iface.get("ip") not in UNROUTABLE:
                continue
            g = guests.get(h["host"].strip().lower())
            if not g or not g.get("address"):
                unknown.append(h["host"])
                continue
            planned.append({"host": h["host"], "interfaceid": iface["interfaceid"],
                            "from": iface.get("ip") or "(empty)", "to": g["address"],
                            "source": g["address_source"]})

    if not planned and not unknown:
        print("  No hosts with an unusable interface address.")
        return 0

    for p in planned:
        print(f"  {p['host'][:28]:<28} {p['from']:<10} -> {p['to']:<16} ({p['source']})")
    for u in unknown:
        print(f"  {u[:28]:<28} unusable address, and the inventory has none either "
              "— set it by hand")

    if not args.apply:
        print(f"\n  DRY RUN — would update {len(planned)} interface(s). "
              "Re-run with --apply.")
        return 0

    done = 0
    for p in planned:
        try:
            z.call("hostinterface.update", {"interfaceid": p["interfaceid"],
                                            "ip": p["to"], "useip": 1})
            done += 1
        except ZabbixError as e:
            print(f"  ! {p['host']}: {e}", file=sys.stderr)
    print(f"\n  Updated {done} of {len(planned)} interface(s).")
    if done:
        print("  Zabbix will retry the failed items on their next interval; use "
              "Execute now to check one immediately.")
    return 0 if done == len(planned) else 1


# --------------------------------------------------------------------------
def fix_dependencies(z, args) -> int:
    """Make each guest's unreachable trigger depend on its hypervisor's.

    Without this, a hypervisor going down raises one alert for the hypervisor
    and one for every guest on it. The guest alerts are true but useless: they
    describe a consequence, not a cause, and they bury the one alert that
    matters.

    Only 'unreachable'-class triggers are linked. Making a guest's disk-space
    trigger depend on its host would suppress a real, independent problem.
    """
    guests = _guest_addresses()

    # Map each Proxmox node name to its Zabbix host, if it has one.
    node_names = {g["node"].strip().lower() for g in guests.values()}
    zhosts = z.call("host.get", {"output": ["hostid", "host"]})
    by_name = {h["host"].strip().lower(): h for h in zhosts}
    node_hosts = {n: by_name[n] for n in node_names if n in by_name}

    missing = node_names - set(node_hosts)
    if missing:
        print(f"  ! No Zabbix host for node(s): {', '.join(sorted(missing))}. "
              "Guests on them cannot be linked.", file=sys.stderr)
    if not node_hosts:
        print("  No hypervisor is monitored in Zabbix, so there is nothing to "
              "depend on. Add the nodes as hosts first.", file=sys.stderr)
        return 1

    def unreachable_triggers(hostid):
        trigs = z.call("trigger.get", {
            "hostids": hostid, "output": ["triggerid", "description"],
            "selectDependencies": ["triggerid"], "monitored": True,
        })
        return [t for t in trigs
                if any(w in t["description"].lower()
                       for w in ("unreachable", "unavailable", "is down",
                                 "no data", "not available"))]

    node_trigger = {}
    for name, h in node_hosts.items():
        cands = unreachable_triggers(h["hostid"])
        if cands:
            node_trigger[name] = cands[0]
            print(f"  hypervisor {name}: depending on {cands[0]['description'][:52]!r}")
        else:
            print(f"  ! hypervisor {name}: no unreachable-style trigger found",
                  file=sys.stderr)

    planned = []
    for h in zhosts:
        g = guests.get(h["host"].strip().lower())
        if not g:
            continue
        parent = node_trigger.get(g["node"].strip().lower())
        if not parent:
            continue
        for t in unreachable_triggers(h["hostid"]):
            existing = {d["triggerid"] for d in (t.get("dependencies") or [])}
            if parent["triggerid"] in existing:
                continue
            planned.append({"host": h["host"], "triggerid": t["triggerid"],
                            "desc": t["description"], "parent": parent["triggerid"],
                            "node": g["node"], "existing": existing})

    if not planned:
        print("\n  Nothing to add; dependencies are already in place.")
        return 0

    print(f"\n  {len(planned)} dependency(ies) to add:")
    for p in planned[:20]:
        print(f"    {p['host'][:26]:<26} {p['desc'][:40]:<40} -> {p['node']}")
    if len(planned) > 20:
        print(f"    … and {len(planned) - 20} more")

    if not args.apply:
        print("\n  DRY RUN — re-run with --apply.")
        return 0

    done = 0
    for p in planned:
        # trigger.update replaces the dependency list, so existing entries have
        # to be sent back or they are silently dropped.
        deps = [{"triggerid": d} for d in p["existing"]] + [{"triggerid": p["parent"]}]
        try:
            z.call("trigger.update", {"triggerid": p["triggerid"], "dependencies": deps})
            done += 1
        except ZabbixError as e:
            print(f"  ! {p['host']} / {p['desc'][:40]}: {e}", file=sys.stderr)
    print(f"\n  Added {done} of {len(planned)} dependency(ies).")
    return 0 if done == len(planned) else 1


# --------------------------------------------------------------------------
def fix_maintenance(z, args) -> int:
    """Repair maintenance windows whose period has already elapsed.

    A one-time period runs once from its start. Once that slot passes the window
    remains listed, still inside its active range, and suppresses nothing — while
    everyone believes the hosts in it are quiet.
    """
    import time
    now = time.time()
    windows = z.call("maintenance.get", {"output": "extend",
                                         "selectTimeperiods": "extend",
                                         "selectHostGroups": ["groupid", "name"],
                                         "selectHosts": ["hostid", "host"],
                                         "selectTags": "extend"})
    broken = []
    for m in windows:
        since, till = int(m["active_since"]), int(m["active_till"])
        for tp in m.get("timeperiods", []):
            if tp["timeperiod_type"] != "0":
                continue
            start = int(tp.get("start_date") or since)
            if start + int(tp["period"]) < now <= till:
                broken.append((m, tp))

    if not broken:
        print("  No maintenance window with an elapsed one-time period.")
        return 0

    for m, tp in broken:
        groups = [g["name"] for g in m.get("hostgroups", m.get("groups", []))]
        print(f"  {m['name']}")
        print(f"    one-time period of {int(tp['period'])//3600}h elapsed; window "
              f"runs until {time.strftime('%Y-%m-%d', time.localtime(int(m['active_till'])))}")
        print(f"    scope: {len(m.get('hosts', []))} host(s), groups={groups}")
        print("    proposed: daily period, 24h, so the scope is continuously "
              "suppressed as intended")

    if not args.apply:
        print("\n  DRY RUN — re-run with --apply.")
        return 0

    done = 0
    for m, _tp in broken:
        try:
            z.call("maintenance.update", {
                "maintenanceid": m["maintenanceid"],
                "timeperiods": [{
                    "timeperiod_type": 3,   # weekly
                    "every": 1,
                    "dayofweek": 127,       # every day
                    "start_time": 0,
                    "period": 86340,        # 23h59m, the maximum inside one day
                }],
            })
            done += 1
            print(f"  updated {m['name']}")
        except ZabbixError as e:
            print(f"  ! {m['name']}: {e}", file=sys.stderr)
    print(f"\n  Repaired {done} of {len(broken)} window(s).")
    return 0 if done == len(broken) else 1


# --------------------------------------------------------------------------
COMMANDS = {
    "interfaces": (fix_interfaces, "give hosts with a 0.0.0.0 interface the address "
                                   "the inventory holds"),
    "dependencies": (fix_dependencies, "make each guest's unreachable trigger depend "
                                       "on its hypervisor's"),
    "maintenance": (fix_maintenance, "repair maintenance windows whose one-time "
                                     "period has elapsed"),
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, (fn, help_text) in COMMANDS.items():
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--apply", action="store_true",
                       help="make the change (without this it is a dry run)")
        p.set_defaults(func=fn)
    args = ap.parse_args()

    z = connect_or_exit()
    print(f"Zabbix {z.version()} at {z.url}\n")
    try:
        return args.func(z, args)
    except ZabbixError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
