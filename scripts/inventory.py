#!/usr/bin/env python3
"""Export the Zabbix host inventory, and audit it against DNS.

    ./scripts/inventory.py                       # table to stdout
    ./scripts/inventory.py --csv hosts.csv       # CSV export
    ./scripts/inventory.py --check-dns           # forward/reverse DNS audit

The DNS audit is the useful part. It answers three questions that quietly rot in
any estate that has been running for a while:

  * Does the name Zabbix monitors resolve at all?
  * Does it resolve to the address Zabbix is actually polling?
  * Does that address reverse-resolve back to the same name?

Any of those going out of sync means alerts point at the wrong box. Nothing is
changed — this only reports.
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import socket
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from zbx import ZabbixError, connect_or_exit  # noqa: E402

AVAILABLE = {"0": "unknown", "1": "available", "2": "unavailable"}
IFACE_TYPE = {"1": "agent", "2": "SNMP", "3": "IPMI", "4": "JMX"}


def collect(z):
    hosts = z.call("host.get", {
        "output": ["hostid", "host", "name", "status", "description"],
        "selectInterfaces": ["ip", "dns", "useip", "type", "available", "port"],
        "selectHostGroups": ["name"],
        "selectParentTemplates": ["host"],
        "sortfield": "host",
    })
    rows = []
    for h in hosts:
        ifaces = h.get("interfaces") or []
        primary = ifaces[0] if ifaces else {}
        rows.append({
            "host": h["host"],
            "visible_name": h["name"],
            "enabled": h["status"] == "0",
            "address": primary.get("ip") or primary.get("dns") or "",
            "uses": "ip" if primary.get("useip") == "1" else "dns",
            "iface_type": IFACE_TYPE.get(primary.get("type"), "?"),
            "available": AVAILABLE.get(primary.get("available"), "?"),
            "groups": "|".join(sorted(g["name"] for g in h.get("hostgroups", h.get("groups", [])))),
            "templates": "|".join(sorted(t["host"] for t in h.get("parentTemplates", []))),
            "description": (h.get("description") or "").replace("\n", " ")[:120],
        })
    return rows


def dns_findings(name, addr, resolved, ptr):
    """Compare what DNS says against what Zabbix polls. Pure — no lookups here,
    so it is testable without a resolver.

    ``resolved`` is the list of A records for ``name`` (empty means NXDOMAIN),
    ``ptr`` is the reverse name for ``addr`` (empty means no PTR).
    Returns a list of human-readable findings; empty means healthy.
    """
    problems = []
    if not resolved:
        problems.append("name does not resolve")
    elif addr not in resolved:
        problems.append(f"resolves to {','.join(resolved)}, Zabbix polls {addr}")

    if not ptr:
        problems.append("no PTR")
    elif not ptr.lower().startswith(name.lower().split(".")[0]):
        problems.append(f"PTR is {ptr}")
    return problems


def audit_dns(rows):
    """Annotate each row with forward/reverse DNS findings."""
    for r in rows:
        addr = r["address"]
        name = r["host"]
        r["dns_forward"] = ""
        r["dns_reverse"] = ""
        r["dns_status"] = ""
        if not addr:
            r["dns_status"] = "no interface"
            continue

        # Forward: does the monitored name resolve, and to this address?
        try:
            resolved = sorted({ai[4][0] for ai in socket.getaddrinfo(name, None)})
            r["dns_forward"] = ",".join(resolved)
        except socket.gaierror:
            resolved = []
            r["dns_forward"] = "NXDOMAIN"

        # Reverse: does the polled address map back to the same name?
        try:
            r["dns_reverse"] = socket.gethostbyaddr(addr)[0]
        except (socket.herror, socket.gaierror, OSError):
            r["dns_reverse"] = "none"

        ptr = "" if r["dns_reverse"] == "none" else r["dns_reverse"]
        problems = dns_findings(name, addr, resolved, ptr)
        r["dns_status"] = "; ".join(problems) if problems else "ok"
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", metavar="PATH", help="write CSV to this path")
    ap.add_argument("--check-dns", action="store_true",
                    help="audit forward and reverse DNS for every host")
    ap.add_argument("--only-problems", action="store_true",
                    help="with --check-dns, print only hosts that have findings")
    args = ap.parse_args()

    z = connect_or_exit()
    try:
        rows = collect(z)
    except ZabbixError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.check_dns:
        print(f"Auditing DNS for {len(rows)} host(s)...", file=sys.stderr)
        rows = audit_dns(rows)

    if args.csv:
        path = pathlib.Path(args.csv)
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {len(rows)} rows to {path}")
        return 0

    shown = rows
    if args.check_dns and args.only_problems:
        shown = [r for r in rows if r["dns_status"] not in ("ok", "")]

    if args.check_dns:
        print(f"{'HOST':<28} {'ADDRESS':<16} FINDING")
        for r in shown:
            print(f"{r['host'][:28]:<28} {r['address']:<16} {r['dns_status']}")
        bad = [r for r in rows if r["dns_status"] not in ("ok", "")]
        print(f"\n{len(rows)} host(s); {len(bad)} with DNS findings.")
    else:
        print(f"{'HOST':<28} {'ADDRESS':<16} {'TYPE':<6} {'STATE':<12} TEMPLATES")
        for r in shown:
            state = r["available"] if r["enabled"] else "disabled"
            print(f"{r['host'][:28]:<28} {r['address']:<16} {r['iface_type']:<6} "
                  f"{state:<12} {r['templates'][:50]}")
        print(f"\n{len(rows)} host(s), {sum(1 for r in rows if r['enabled'])} enabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
