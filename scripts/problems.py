#!/usr/bin/env python3
"""Inspect and close Zabbix problems in bulk.

    ./scripts/problems.py list                        # active problems, worst first
    ./scripts/problems.py list --min-severity 4
    ./scripts/problems.py close --host oldbox         # close everything on a host
    ./scripts/problems.py close --stale 30            # untouched for 30+ days
    ./scripts/problems.py close --stale 30 --apply    # actually do it

Closing is a two-step by design: every ``close`` is a dry run that prints what
it *would* close until you add ``--apply``. Acknowledging problems in bulk is
easy to get wrong and there is no undo.

Only problems whose trigger allows manual close can actually be closed. Zabbix
rejects the rest; they are reported as skipped rather than counted as done.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from zbx import ZabbixError, connect_or_exit  # noqa: E402

SEVERITY = {0: "not classified", 1: "information", 2: "warning",
            3: "average", 4: "high", 5: "disaster"}

# event.acknowledge action is a bitmask. 1 = close, 2 = acknowledge, 4 = message.
ACK_CLOSE = 1 | 2 | 4


def fetch(z, min_severity=0):
    problems = z.call("problem.get", {
        "output": "extend",
        "severities": list(range(min_severity, 6)),
        "sortfield": ["eventid"],
        "sortorder": "DESC",
        "recent": False,
    })
    if not problems:
        return []
    # problem.get does not return the host, so resolve via the trigger.
    trigger_ids = sorted({p["objectid"] for p in problems})
    triggers = z.call("trigger.get", {
        "triggerids": trigger_ids,
        "output": ["triggerid", "manual_close"],
        "selectHosts": ["host"],
    })
    by_trigger = {t["triggerid"]: t for t in triggers}
    now = time.time()
    out = []
    for p in problems:
        t = by_trigger.get(p["objectid"], {})
        hosts = [h["host"] for h in t.get("hosts", [])]
        age_days = (now - int(p["clock"])) / 86400
        out.append({
            "eventid": p["eventid"],
            "name": p["name"],
            "severity": int(p["severity"]),
            "host": hosts[0] if hosts else "(unknown)",
            "age_days": age_days,
            "acknowledged": p.get("acknowledged") == "1",
            "manual_close": t.get("manual_close") == "1",
        })
    out.sort(key=lambda x: (-x["severity"], -x["age_days"]))
    return out


def do_list(z, args) -> int:
    problems = fetch(z, args.min_severity)
    if not problems:
        print("No active problems.")
        return 0
    print(f"{'SEV':<14} {'AGE':>7}  {'HOST':<24} PROBLEM")
    for p in problems:
        flag = "" if p["manual_close"] else "  [no manual close]"
        print(f"{SEVERITY[p['severity']]:<14} {p['age_days']:>6.1f}d  "
              f"{p['host'][:24]:<24} {p['name'][:70]}{flag}")
    print(f"\n{len(problems)} active problem(s).")
    return 0


def do_close(z, args) -> int:
    if args.stale is None and not args.host and not args.eventid:
        print("error: give at least one of --stale, --host, or --eventid.",
              file=sys.stderr)
        return 2

    problems = fetch(z, args.min_severity)
    selected = []
    for p in problems:
        if args.eventid and p["eventid"] not in args.eventid:
            continue
        if args.host and args.host.lower() not in p["host"].lower():
            continue
        if args.stale is not None and p["age_days"] < args.stale:
            continue
        selected.append(p)

    if not selected:
        print("Nothing matched.")
        return 0

    closable = [p for p in selected if p["manual_close"]]
    skipped = [p for p in selected if not p["manual_close"]]

    for p in selected:
        mark = " " if p["manual_close"] else "-"
        print(f" {mark} {p['host'][:24]:<24} {p['age_days']:>6.1f}d  {p['name'][:64]}")
    if skipped:
        print(f"\n{len(skipped)} problem(s) marked '-' cannot be closed manually "
              "(their trigger has manual_close disabled) and will be skipped.")

    if not args.apply:
        print(f"\nDRY RUN — would close {len(closable)} problem(s). "
              "Re-run with --apply to do it.")
        return 0

    if not closable:
        print("\nNothing closable.")
        return 0

    # event.acknowledge accepts a list, but a single oversized call fails as a
    # unit; chunk so one bad event cannot lose the whole batch.
    done = 0
    for i in range(0, len(closable), 50):
        chunk = closable[i:i + 50]
        try:
            z.call("event.acknowledge", {
                "eventids": [p["eventid"] for p in chunk],
                "action": ACK_CLOSE,
                "message": args.message,
            })
            done += len(chunk)
        except ZabbixError as e:
            print(f"  chunk starting at {i} failed: {e}", file=sys.stderr)
    print(f"\nClosed {done} problem(s).")
    return 0 if done == len(closable) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-severity", type=int, default=0, choices=range(6),
                    help="0=not classified .. 5=disaster")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="show active problems")
    ls.set_defaults(func=do_list)

    cl = sub.add_parser("close", help="acknowledge and close problems in bulk")
    cl.add_argument("--stale", type=float, metavar="DAYS",
                    help="only problems at least this many days old")
    cl.add_argument("--host", help="substring match on host name")
    cl.add_argument("--eventid", nargs="*", help="explicit event ids")
    cl.add_argument("--message", default="Closed in bulk via homelab-zabbix",
                    help="note recorded against each closed problem")
    cl.add_argument("--apply", action="store_true",
                    help="actually close (without this it is a dry run)")
    cl.set_defaults(func=do_close)

    args = ap.parse_args()
    z = connect_or_exit()
    try:
        return args.func(z, args)
    except ZabbixError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
