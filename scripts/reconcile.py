#!/usr/bin/env python3
"""Reconcile Proxmox against Zabbix, and report the drift.

    ./scripts/reconcile.py                  # full report
    ./scripts/reconcile.py --only drift     # just the wrong-address hosts
    ./scripts/reconcile.py --json           # machine-readable, for CI
    ./scripts/reconcile.py --fail-on drift  # non-zero exit, for a scheduled job

Monitoring lies quietly. A guest gets a new address, Zabbix keeps polling the
old one, and the resulting "unreachable" alert looks exactly like a real
outage — so it either wastes an investigation or, worse, gets muted along with
everything else. Meanwhile a guest nobody added to Zabbix is not monitored at
all, and nothing anywhere says so.

This compares the two inventories and reports four classes of drift:

  no_address  Zabbix host whose interface is 0.0.0.0 or empty — it has nowhere
              to poll, so every check fails no matter how healthy the guest is
  drift       monitored, but Zabbix has a different address than Proxmox
  unmonitored running guest with no Zabbix host at all
  orphaned    enabled Zabbix host with no matching Proxmox guest
  stopped     Zabbix host enabled for a guest that is stopped (alert noise)

Hosts that legitimately are not Proxmox guests — switches, APs, a UPS, the
hypervisors themselves — will always look "orphaned". Filter them out by group:

    ./scripts/reconcile.py --exclude-group Homelab/Network Homelab/Infrastructure

Read-only against both APIs. It reports; it changes nothing.

Real example this was written for: a container's address changed from .50 to
.51, Zabbix kept the .50 interface, and "unreachable" fired for five days while
the service was healthy the whole time.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pve import ProxmoxError, connect_all_or_exit  # noqa: E402
from zbx import ZabbixError, connect_or_exit  # noqa: E402


def normalise(name: str) -> str:
    """Match key for a guest/host name.

    Zabbix host names and Proxmox hostnames drift in case and in domain suffix
    (``web01`` vs ``web01.lan``), which is cosmetic. Anything beyond that is a
    real naming inconsistency and should show up as drift rather than be papered
    over by fuzzy matching.
    """
    return (name or "").strip().lower().split(".")[0]


def zabbix_hosts(z) -> list[dict]:
    hosts = z.call("host.get", {
        "output": ["hostid", "host", "name", "status"],
        "selectInterfaces": ["ip", "dns", "useip", "type", "main"],
        "selectHostGroups": ["name"],
        "sortfield": "host",
    })
    out = []
    for h in hosts:
        # Prefer the main agent interface; fall back to the first one present.
        ifaces = h.get("interfaces") or []
        primary = next((i for i in ifaces if i.get("main") == "1"), ifaces[0] if ifaces else {})
        groups = h.get("hostgroups", h.get("groups", []))
        out.append({
            "hostid": h["hostid"],
            "host": h["host"],
            "enabled": h["status"] == "0",
            "address": (primary.get("ip") or primary.get("dns") or ""),
            "uses": "ip" if primary.get("useip") == "1" else "dns",
            "groups": [g["name"] for g in groups],
        })
    return out


#: An interface Zabbix will never reach. 0.0.0.0 is what a host ends up with
#: when it is created without a real address — auto-registration without an
#: interface operation is the usual way in.
UNROUTABLE = ("", "0.0.0.0", "::")


def reconcile(guests: list[dict], hosts: list[dict],
              exclude_groups: list[str] | None = None) -> dict:
    """Pure comparison of the two inventories. Kept free of I/O so it is testable."""
    excluded = set(exclude_groups or [])

    def is_excluded(h: dict) -> bool:
        return bool(set(h.get("groups", [])) & excluded)

    # Excluded hosts stay in the match index — removing them would make their
    # perfectly-monitored guests show up as "unmonitored". The exclusion only
    # suppresses the orphaned list, which is what it is for.
    by_name_host = {normalise(h["host"]): h for h in hosts}
    by_name_guest = {normalise(g["name"]): g for g in guests}

    drift, unmonitored, stopped, unknown_addr, no_address = [], [], [], [], []

    # A Zabbix host with no usable address is broken regardless of what Proxmox
    # thinks, so this is checked first and independently of guest matching.
    for h in hosts:
        if not h["enabled"]:
            continue
        if h["uses"] == "ip" and h["address"] in UNROUTABLE:
            g = by_name_guest.get(normalise(h["host"]))
            no_address.append({
                "host": h["host"],
                "hostid": h["hostid"],
                "zabbix_address": h["address"] or "(empty)",
                "proxmox_address": (g or {}).get("address", ""),
                "vmid": (g or {}).get("vmid"),
            })

    for g in guests:
        key = normalise(g["name"])
        h = by_name_host.get(key)

        if h is None:
            if g["status"] == "running":
                unmonitored.append(g)
            continue

        if g["status"] != "running" and h["enabled"]:
            stopped.append({**g, "zabbix_host": h["host"]})
            continue

        if not g["address"]:
            # Proxmox does not know the address (DHCP, or a VM with no guest
            # agent). Not a mismatch — an unverifiable host. Reported separately
            # so it never masquerades as a clean result.
            unknown_addr.append({**g, "zabbix_address": h["address"]})
            continue

        if h["address"] in UNROUTABLE:
            continue  # already reported under no_address

        if h["uses"] == "ip" and h["address"] and h["address"] != g["address"]:
            drift.append({
                "name": g["name"],
                "vmid": g["vmid"],
                "node": g["node"],
                "proxmox_address": g["address"],
                "zabbix_address": h["address"],
                "address_source": g["address_source"],
                "hostid": h["hostid"],
            })

    orphaned = [
        h for h in hosts
        if h["enabled"] and not is_excluded(h)
        and normalise(h["host"]) not in by_name_guest
    ]

    return {
        "no_address": no_address,
        "drift": drift,
        "unmonitored": unmonitored,
        "orphaned": orphaned,
        "stopped": stopped,
        "unverifiable": unknown_addr,
        "totals": {
            "proxmox_guests": len(guests),
            "zabbix_hosts": len(hosts),
            "no_address": len(no_address),
            "drift": len(drift),
            "unmonitored": len(unmonitored),
            "orphaned": len(orphaned),
            "stopped": len(stopped),
            "unverifiable": len(unknown_addr),
        },
    }


def render(result: dict, only: str | None) -> None:
    t = result["totals"]

    def want(section: str) -> bool:
        return only is None or only == section

    if want("no_address"):
        print(f"\n── No usable address ({t['no_address']}) "
              "— Zabbix has nowhere to poll; every check fails regardless of health")
        if not result["no_address"]:
            print("   none")
        for n in result["no_address"]:
            hint = f"proxmox={n['proxmox_address']}" if n["proxmox_address"] else "no matching guest"
            print(f"   {n['host'][:26]:<26} zabbix={n['zabbix_address']:<12} {hint}")

    if want("drift"):
        print(f"\n── Address drift ({t['drift']}) "
              "— Zabbix is polling an address Proxmox disagrees with")
        if not result["drift"]:
            print("   none")
        for d in result["drift"]:
            print(f"   {d['name'][:26]:<26} CT/VM {d['vmid']:<6} "
                  f"proxmox={d['proxmox_address']:<16} zabbix={d['zabbix_address']:<16} "
                  f"({d['address_source']})")

    if want("unmonitored"):
        print(f"\n── Unmonitored ({t['unmonitored']}) — running, but not in Zabbix")
        if not result["unmonitored"]:
            print("   none")
        for g in result["unmonitored"]:
            print(f"   {g['name'][:26]:<26} {g['type']:<4} {g['node']:<14} "
                  f"{g['address'] or '(dhcp)'}")

    if want("orphaned"):
        print(f"\n── Orphaned ({t['orphaned']}) — enabled in Zabbix, no such guest")
        if not result["orphaned"]:
            print("   none")
        for h in result["orphaned"]:
            print(f"   {h['host'][:26]:<26} {h['address']}")

    if want("stopped"):
        print(f"\n── Stopped but enabled ({t['stopped']}) — guaranteed alert noise")
        if not result["stopped"]:
            print("   none")
        for g in result["stopped"]:
            print(f"   {g['name'][:26]:<26} {g['type']:<4} {g['node']}")

    if want("unverifiable"):
        print(f"\n── Unverifiable ({t['unverifiable']}) "
              "— Proxmox has no address (DHCP / no guest agent)")
        if not result["unverifiable"]:
            print("   none")
        for g in result["unverifiable"][:20]:
            print(f"   {g['name'][:26]:<26} zabbix={g['zabbix_address']:<16} "
                  f"({g['address_source']})")
        if len(result["unverifiable"]) > 20:
            print(f"   … and {len(result['unverifiable']) - 20} more")

    print(f"\n{t['proxmox_guests']} Proxmox guest(s) vs {t['zabbix_hosts']} Zabbix host(s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["no_address", "drift", "unmonitored",
                                       "orphaned", "stopped", "unverifiable"],
                    help="show only one section")
    ap.add_argument("--exclude-group", nargs="*", default=[], metavar="GROUP",
                    help="Zabbix host groups to ignore entirely — use for kit that "
                         "is not a Proxmox guest (switches, APs, UPS, hypervisors)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--fail-on", nargs="*",
                    choices=["no_address", "drift", "unmonitored", "orphaned", "stopped"],
                    default=[],
                    help="exit non-zero if any of these are found (for CI)")
    args = ap.parse_args()

    z = connect_or_exit()
    nodes = connect_all_or_exit()

    guests = []
    for node in nodes:
        try:
            guests.extend(node.guests())
        except ProxmoxError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    try:
        hosts = zabbix_hosts(z)
    except ZabbixError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    result = reconcile(guests, hosts, exclude_groups=args.exclude_group)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        render(result, args.only)

    failures = sum(result["totals"][k] for k in args.fail_on)
    if failures:
        print(f"\nFAIL: {failures} finding(s) in {', '.join(args.fail_on)}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
