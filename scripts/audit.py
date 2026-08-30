#!/usr/bin/env python3
"""Audit a Zabbix server against production-readiness checks.

    ./scripts/audit.py                      # full report
    ./scripts/audit.py --only alerting      # one category
    ./scripts/audit.py --json               # machine-readable
    ./scripts/audit.py --fail-on high       # non-zero exit, for a scheduled job

Every check answers a question an operator would otherwise have to ask by
clicking through the frontend, and each one exists because it has gone wrong in
a real installation.

Read-only. This tool never writes to Zabbix.

Severity means:

    high    Monitoring is not doing what someone believes it is doing, or an
            alert would not reach a human.
    medium  Real operational cost: wasted polling, alert noise, or a security
            posture that will be questioned in an audit.
    low     Worth tidying. Not urgent.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from zbx import ZabbixError, connect_or_exit  # noqa: E402

SEVERITIES = ("high", "medium", "low")

CATEGORIES = ("alerting", "suppression", "collection", "noise", "security", "capacity")


class Findings:
    """Collects findings so every check can fail independently."""

    def __init__(self):
        self.items: list[dict] = []

    def add(self, category, severity, title, detail, count=None, fix=None, evidence=None):
        self.items.append({
            "category": category, "severity": severity, "title": title,
            "detail": detail, "count": count, "fix": fix,
            "evidence": evidence or [],
        })

    def by_severity(self, sev):
        return [f for f in self.items if f["severity"] == sev]


# --------------------------------------------------------------------------
# Alerting: would anything actually reach a person?
# --------------------------------------------------------------------------
def check_alerting(z, f: Findings) -> None:
    actions = z.call("action.get", {"output": "extend", "selectOperations": "extend"})
    enabled = [a for a in actions if a["status"] == "0" and a.get("eventsource") == "0"]

    # An action whose operations all sit on the same escalation step sends one
    # notification and then stops. If the recipient is asleep, that is the end
    # of the escalation.
    flat = []
    for a in enabled:
        ops = a.get("operations") or []
        steps = {(o.get("esc_step_from"), o.get("esc_step_to")) for o in ops}
        if ops and len(steps) <= 1:
            flat.append(a["name"])
    if flat:
        f.add("alerting", "medium",
              "Trigger actions with no escalation",
              "These actions notify once and stop. Nothing repeats or escalates if "
              "the first notification is missed, which is the common case at night.",
              count=len(flat), evidence=flat[:8],
              fix="Add a second escalation step with a delay, or an operation "
                  "targeting a wider group at a later step.")

    # An enabled action is worthless if no recipient has usable media.
    users = z.call("user.get", {"output": ["userid", "username"],
                                "selectMedias": "extend", "selectRole": ["name"]})
    media_types = {m["mediatypeid"]: m for m in
                   z.call("mediatype.get", {"output": ["mediatypeid", "name", "status"]})}
    reachable = []
    for u in users:
        for med in (u.get("medias") or []):
            mt = media_types.get(med.get("mediatypeid"))
            if mt and mt["status"] == "0" and med.get("active") == "0":
                reachable.append(u["username"])
                break
    if not reachable:
        f.add("alerting", "high",
              "No user can currently be notified",
              "Every configured medium is either disabled at the media type level "
              "or disabled on the user. A trigger firing right now would reach "
              "nobody.",
              count=len(users),
              fix="Enable the media type and the user's media entry, and send a test.")
    elif len(reachable) < 2:
        f.add("alerting", "medium",
              "Only one user can be notified",
              f"Notifications depend entirely on {reachable[0]!r}. There is no "
              "second recipient if that address stops working.",
              count=1, evidence=reachable)

    if not enabled:
        f.add("alerting", "high",
              "No enabled trigger actions",
              "Problems will be recorded but nobody will be told about them.",
              count=0)


# --------------------------------------------------------------------------
# Suppression: is monitoring quietly switched off somewhere?
# --------------------------------------------------------------------------
def check_suppression(z, f: Findings) -> None:
    now = time.time()
    windows = z.call("maintenance.get", {"output": "extend",
                                         "selectTimeperiods": "extend",
                                         "selectHostGroups": ["name"],
                                         "selectHosts": ["host"]})
    dead, permanent = [], []
    for m in windows:
        since, till = int(m["active_since"]), int(m["active_till"])
        for tp in m.get("timeperiods", []):
            # A one-time period runs once, starting at active_since. If that
            # moment has passed, the window still shows as active in the UI and
            # suppresses nothing — the failure is invisible.
            if tp["timeperiod_type"] == "0":
                # start_date is set for a one-time period; fall back to the
                # window's own start when it is absent.
                start = int(tp.get("start_date") or since)
                if start + int(tp["period"]) < now <= till:
                    dead.append(m["name"])
            # A period covering essentially the whole cycle is permanent
            # suppression wearing a maintenance-window costume.
            elif tp["timeperiod_type"] == "2" and int(tp["period"]) >= 86000:
                permanent.append(m["name"])
    if dead:
        f.add("suppression", "high",
              "Maintenance window that suppresses nothing",
              "A one-time period whose slot has already elapsed. The window is "
              "still listed and still looks active, so hosts it names are believed "
              "to be suppressed while they are alerting normally.",
              count=len(dead), evidence=sorted(set(dead)),
              fix="Convert to a daily or weekly period, or delete the window.")
    if permanent:
        f.add("suppression", "medium",
              "Effectively permanent maintenance window",
              "A recurring period long enough to cover the whole day. Anything in "
              "scope is never alerting.",
              count=len(permanent), evidence=sorted(set(permanent)))

    hosts = z.call("host.get", {"output": ["host", "status", "maintenance_status"]})
    in_maint = [h["host"] for h in hosts if h.get("maintenance_status") == "1"]
    if len(in_maint) > len(hosts) * 0.25:
        f.add("suppression", "high",
              "Large share of hosts currently in maintenance",
              f"{len(in_maint)} of {len(hosts)} hosts are suppressed right now.",
              count=len(in_maint), evidence=in_maint[:10])

    disabled = [h["host"] for h in hosts if h["status"] == "1"]
    if disabled:
        f.add("suppression", "low",
              "Disabled hosts still configured",
              "Not monitored, but still present. Worth confirming each is "
              "deliberate rather than forgotten.",
              count=len(disabled), evidence=disabled[:10])


# --------------------------------------------------------------------------
# Collection: is data actually arriving?
# --------------------------------------------------------------------------
def check_collection(z, f: Findings) -> None:
    unsupported = z.call("item.get", {"filter": {"state": "1"}, "monitored": True,
                                      "output": ["name", "error", "key_"],
                                      "limit": 20000})
    if unsupported:
        causes = Counter((i.get("error") or "unknown")[:70] for i in unsupported)
        f.add("collection", "medium",
              "Unsupported items",
              "Each consumes a poller slot on every interval and returns nothing. "
              "They accumulate silently after template or firmware changes.",
              count=len(unsupported),
              evidence=[f"{c}x  {e}" for e, c in causes.most_common(6)],
              fix="Group by cause. Fix at template level where the key is wrong; "
                  "disable where the hardware genuinely lacks the OID.")

    # A host whose interface cannot be polled fails every check regardless of
    # how healthy it is. reconcile.py covers this against an inventory; this is
    # the standalone version.
    hosts = z.call("host.get", {"output": ["host", "status"],
                                "selectInterfaces": ["ip", "useip", "main"]})
    unroutable = []
    for h in hosts:
        if h["status"] != "0":
            continue
        ifaces = h.get("interfaces") or []
        main = next((i for i in ifaces if i.get("main") == "1"), ifaces[0] if ifaces else None)
        if main and main.get("useip") == "1" and main.get("ip") in ("", "0.0.0.0", "::"):
            unroutable.append(h["host"])
    if unroutable:
        f.add("collection", "high",
              "Enabled hosts with an unusable interface address",
              "Zabbix has nowhere to poll. Every check fails and the host reports "
              "unreachable no matter how healthy it is. Usually the result of "
              "auto-registration creating a host without an interface operation.",
              count=len(unroutable), evidence=unroutable[:10],
              fix="Set a real address on the interface, or add HostInterface= to "
                  "the agent configuration so registration records one.")


# --------------------------------------------------------------------------
# Noise: does the problem list mean anything?
# --------------------------------------------------------------------------
def check_noise(z, f: Findings) -> None:
    triggers = z.call("trigger.get", {"output": ["triggerid", "description", "manual_close"],
                                      "selectDependencies": "count",
                                      "monitored": True, "limit": 200000})
    if triggers:
        with_deps = sum(1 for t in triggers if int(t.get("dependencies") or 0) > 0)
        if with_deps == 0:
            f.add("noise", "high",
                  "No trigger dependencies anywhere",
                  "Nothing suppresses downstream alerts. One upstream failure "
                  "raises a separate alert for every host behind it, which is how "
                  "a single fault produces an unreadable problem list.",
                  count=len(triggers),
                  fix="Make each guest's unreachable trigger depend on its "
                      "hypervisor's, and each device's on its upstream link.")
        elif with_deps < len(triggers) * 0.02:
            f.add("noise", "medium",
                  "Almost no trigger dependencies",
                  f"{with_deps} of {len(triggers)} monitored triggers have one.",
                  count=with_deps)

    problems = z.call("problem.get", {"output": "extend", "recent": False})
    if problems:
        now = time.time()
        stale = [p for p in problems if now - int(p["clock"]) > 7 * 86400]
        unacked = [p for p in problems if p.get("acknowledged") == "0"]
        if stale:
            f.add("noise", "medium",
                  "Problems open longer than a week",
                  "A problem list containing entries nobody has closed in seven "
                  "days trains people to skim past it.",
                  count=len(stale),
                  evidence=[p["name"][:70] for p in stale[:6]],
                  fix="Triage with problems.py, then fix the trigger or close it.")
        if len(unacked) == len(problems) and len(problems) > 5:
            f.add("noise", "low",
                  "No problems acknowledged",
                  "Nothing in the current problem list has been acknowledged, "
                  "which suggests the list is not being worked.",
                  count=len(unacked))

    hosts = z.call("host.get", {"output": ["host"], "selectTags": "extend"})
    untagged = [h["host"] for h in hosts if not h.get("tags")]
    if untagged:
        f.add("noise", "low",
              "Hosts with no tags",
              "Tags drive filtering, action conditions and event correlation. "
              "Untagged hosts cannot be targeted by anything except group.",
              count=len(untagged), evidence=untagged[:10])


# --------------------------------------------------------------------------
# Security
# --------------------------------------------------------------------------
def check_security(z, f: Findings) -> None:
    hosts = z.call("host.get", {"output": ["host", "tls_connect", "tls_accept"]})
    plain = [h["host"] for h in hosts if h.get("tls_connect") == "1"]
    if plain and len(plain) == len(hosts):
        f.add("security", "medium",
              "No host uses encrypted agent transport",
              "All agent traffic is unauthenticated and in clear text. Anyone on "
              "the path can read metrics and, more importantly, a host can be "
              "impersonated to the server.",
              count=len(plain),
              fix="PSK is the low-effort option: generate a key per host, set "
                  "TLSConnect/TLSAccept on the agent and the host object.")
    elif plain:
        f.add("security", "low", "Some hosts unencrypted",
              "A subset still uses unencrypted transport.",
              count=len(plain), evidence=plain[:10])

    try:
        tokens = z.call("token.get", {"output": ["name", "expires_at", "status", "lastaccess"]})
    except ZabbixError:
        tokens = []
    never = [t["name"] for t in tokens if t.get("expires_at") in ("0", 0)]
    if never:
        f.add("security", "medium",
              "API tokens that never expire",
              "A token with no expiry is a credential that outlives the reason it "
              "was created. Any leak is permanent until someone notices.",
              count=len(never), evidence=never[:10],
              fix="Set an expiry and rotate. Delete tokens whose lastaccess is old.")
    now = time.time()
    unused = [t["name"] for t in tokens
              if t.get("lastaccess") in ("0", 0)
              or (t.get("lastaccess") and now - int(t["lastaccess"]) > 90 * 86400)]
    if unused:
        f.add("security", "low",
              "API tokens unused for 90 days or never used",
              "Live credentials nothing appears to be using.",
              count=len(unused), evidence=unused[:10])

    users = z.call("user.get", {"output": ["username"], "selectRole": ["name", "type"]})
    supers = [u["username"] for u in users
              if (u.get("role") or {}).get("type") == "3"]
    if len(supers) > 2:
        f.add("security", "medium",
              "Several super-admin accounts",
              "Super admin bypasses all permission checks, including host group "
              "restrictions. Automation should hold the narrowest role that works.",
              count=len(supers), evidence=supers,
              fix="Move API consumers to a scoped role. Read-only where they only read.")


# --------------------------------------------------------------------------
# Capacity
# --------------------------------------------------------------------------
def check_capacity(z, f: Findings) -> None:
    proxies = z.call("proxy.get", {"output": ["name", "version", "compatibility", "state"],
                                   "selectHosts": "count"})
    for p in proxies:
        # compatibility: 0 undefined, 1 current, 2 outdated, 3 unsupported
        if p.get("compatibility") in ("2", "3"):
            f.add("capacity", "high" if p["compatibility"] == "3" else "medium",
                  "Proxy version does not match the server",
                  f"Proxy {p['name']!r} reports version {p.get('version')}. An "
                  "unsupported proxy stops delivering after a server upgrade.",
                  count=1, evidence=[p["name"]],
                  fix="Upgrade the proxy to the server's version and hold it there.")
        if p.get("state") == "1":
            f.add("capacity", "high", "Proxy offline",
                  f"Proxy {p['name']!r} is not connected. Every host behind it "
                  f"({p.get('hosts')}) has stopped reporting.",
                  count=1, evidence=[p["name"]])

    # Zabbix exposes its own health as internal items on the server host.
    try:
        server = z.call("host.get", {"output": ["hostid"],
                                     "filter": {"host": ["Zabbix server"]}})
        if server:
            items = z.call("item.get", {
                "hostids": server[0]["hostid"],
                "search": {"key_": "zabbix["},
                "output": ["key_", "lastvalue", "name"], "limit": 500})
            busy = [(i["name"], float(i["lastvalue"]))
                    for i in items
                    if "busy" in i["key_"] and i.get("lastvalue")
                    and _is_num(i["lastvalue"]) and float(i["lastvalue"]) > 75]
            if busy:
                f.add("capacity", "medium",
                      "Server processes running hot",
                      "A process type above 75% busy is close to becoming the "
                      "bottleneck. Raise its Start* count before values start "
                      "queueing.",
                      count=len(busy),
                      evidence=[f"{n}: {v:.0f}% busy" for n, v in busy[:8]])
            cache = [(i["name"], float(i["lastvalue"]))
                     for i in items
                     if "pfree" in i["key_"] and i.get("lastvalue")
                     and _is_num(i["lastvalue"]) and float(i["lastvalue"]) < 25]
            if cache:
                f.add("capacity", "medium",
                      "Cache free space low",
                      "A cache below 25% free will start rejecting writes, which "
                      "loses values silently.",
                      count=len(cache),
                      evidence=[f"{n}: {v:.0f}% free" for n, v in cache[:8]])
    except ZabbixError:
        pass


def _is_num(s) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


CHECKS = {
    "alerting": check_alerting,
    "suppression": check_suppression,
    "collection": check_collection,
    "noise": check_noise,
    "security": check_security,
    "capacity": check_capacity,
}


def render(f: Findings) -> None:
    if not f.items:
        print("\nNo findings. That is unusual — confirm the account can see "
              "everything it should.")
        return
    for sev in SEVERITIES:
        group = f.by_severity(sev)
        if not group:
            continue
        print(f"\n{'═' * 72}\n{sev.upper()}  ({len(group)})\n{'═' * 72}")
        for item in group:
            n = f"  [{item['count']}]" if item["count"] is not None else ""
            print(f"\n{item['title']}{n}")
            print(f"  {item['detail']}")
            for e in item["evidence"]:
                print(f"    · {e}")
            if item["fix"]:
                print(f"  fix: {item['fix']}")
    t = Counter(i["severity"] for i in f.items)
    print(f"\n{'─' * 72}")
    print(f"{len(f.items)} finding(s): "
          f"{t.get('high', 0)} high, {t.get('medium', 0)} medium, {t.get('low', 0)} low")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=CATEGORIES, help="run one category")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--fail-on", choices=SEVERITIES,
                    help="exit non-zero if any finding at this severity or above")
    args = ap.parse_args()

    z = connect_or_exit()
    f = Findings()

    selected = [args.only] if args.only else list(CATEGORIES)
    for name in selected:
        try:
            CHECKS[name](z, f)
        except ZabbixError as e:
            print(f"  ! {name} check failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps({"findings": f.items,
                          "totals": dict(Counter(i["severity"] for i in f.items))},
                         indent=2))
    else:
        print(f"Zabbix {z.version()} at {z.url}")
        render(f)

    if args.fail_on:
        threshold = SEVERITIES.index(args.fail_on)
        hit = [i for i in f.items if SEVERITIES.index(i["severity"]) <= threshold]
        if hit:
            print(f"\nFAIL: {len(hit)} finding(s) at {args.fail_on} or above",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
