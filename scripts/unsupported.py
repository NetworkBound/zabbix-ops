#!/usr/bin/env python3
"""Triage items stuck in the unsupported state.

    ./scripts/unsupported.py list                       # grouped by cause
    ./scripts/unsupported.py list --host core-sw
    ./scripts/unsupported.py list --min-count 10 --fail-over 200
    ./scripts/unsupported.py explain                    # what each cause means
    ./scripts/unsupported.py disable --error "No Such Object"
    ./scripts/unsupported.py disable --error "No Such Object" --apply
    ./scripts/unsupported.py trends --days 30

An unsupported item is still scheduled. It takes a poller slot on every
interval, fails, and returns nothing. A few hundred of them is a measurable
share of the polling budget spent on data that will never arrive.

They accumulate silently. A template change, a firmware upgrade or an agent
upgrade turns a working key into a failing one, and nothing alerts on it — the
item simply stops producing values while the frontend still lists it as
monitored. The count only ever goes up until someone looks.

The value here is not the list. It is the grouping: a few hundred unsupported
items are usually a handful of causes repeated across every host that shares a
template, and each cause has one fix that clears the whole group.

``list``, ``explain`` and ``trends`` are read-only. ``disable`` is a dry run
until ``--apply`` and prints exactly which items it would touch first.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from zbx import ZabbixError, connect_or_exit  # noqa: E402

# What the recommended action actually asks the operator to do. The bucket
# matters more than the individual error: it decides whether disabling is
# reasonable at all, or whether disabling would hide a real fault.
BUCKETS = {
    "template": "Fix on the template. The same item is inherited by every host "
                "linked to it, so a host-level change leaves the rest broken.",
    "discovery": "Fix on the discovery rule. The entity behind the item is gone; "
                 "discovery should remove the item rather than anyone disabling it.",
    "macro": "A macro never resolved, or resolved to something this host does not "
             "have. Set it on the host or host group.",
    "params": "The key parameters are wrong for the agent or device on the other "
              "end. Correct the key.",
    "hardware": "The device genuinely does not have what is being asked for. "
                "Disabling is the correct outcome here.",
    "host": "Fix on the host: a missing plugin, a missing UserParameter, a "
            "permission, or a missing interface.",
    "transient": "The collection path failed, not the item. These usually clear "
                 "on their own once the host or proxy is reachable again.",
    "unknown": "Not recognised. Needs investigation before anything is changed.",
}

# Ordered. The first pattern that matches wins, so specific entries come before
# general ones. Every entry here was written against an error string a real
# server produced; anything unmatched is reported as needing investigation
# rather than guessed at.
CLASSES = (
    {
        "id": "snmp-walk-missing-oid",
        "pattern": re.compile(r"unable to extract value for given OID", re.I),
        "label": "SNMP walk does not contain the OID the item extracts",
        "bucket": "discovery",
        "cause": "These are dependent items fed by a master SNMP walk. The walk "
                 "itself succeeded, so the device is reachable and answering; the "
                 "specific OID instance this item pulls out of it was not in the "
                 "response. The index moved or the entity went away: a port was "
                 "renumbered, a stack member rebooted into a different position, a "
                 "transceiver was pulled, or a firmware upgrade stopped exporting "
                 "the column. The error text is the raw walk output, which is why "
                 "every affected item on a host shows the same wall of text.",
        "action": "Look at the master walk item first and check whether the OID is "
                  "in its value. If the instance is stale, discovery should have "
                  "removed the item, so the rule's lifetime setting is keeping it "
                  "alive past the point the entity disappeared. If the whole column "
                  "is missing from the walk, the template is asking for something "
                  "this firmware no longer exports and the fix belongs there.",
    },
    {
        "id": "bad-key-parameter-2",
        "pattern": re.compile(r"^Invalid second parameter", re.I),
        "label": "Agent rejected the second key parameter",
        "bucket": "params",
        "cause": "The agent parsed the key and refused its second parameter. On "
                 "vfs.dev.* and net.if.* keys this is what an agent returns for a "
                 "mode it does not implement. It is nearly always a template "
                 "written against a different agent major version, or an agent 2 "
                 "plugin that accepts a narrower set of parameters than the classic "
                 "agent did.",
        "action": "Check the key against the parameters documented for the agent "
                  "version actually installed on the host, not the newest one. The "
                  "correction belongs on the template, since every host linked to it "
                  "sends the same key.",
    },
    {
        "id": "bad-key-parameter-1",
        "pattern": re.compile(r"^Invalid first parameter", re.I),
        "label": "Agent or server rejected the first key parameter",
        "bucket": "params",
        "cause": "The first parameter names something the component on the other end "
                 "does not recognise. On internal zabbix[...] items this usually "
                 "means the key is valid on a server but not on a proxy, or the "
                 "reverse.",
        "action": "Confirm the key is supported by the component it is linked to. "
                  "Server-only internal items linked to a proxy host will never work "
                  "and should be unlinked.",
    },
    {
        "id": "block-device-missing",
        "pattern": re.compile(r"Cannot obtain device name used internally by the kernel", re.I),
        "label": "Block device in the key does not exist on this host",
        "bucket": "macro",
        "cause": "The agent could not map the device named in the key to anything in "
                 "/proc/diskstats. The device is not there: the template hardcodes "
                 "sda, or a macro such as {$DISK_DEVICE} kept its default while the "
                 "host actually presents vda, nvme0n1 or, in a container, no block "
                 "device at all. The check is well formed, it just names something "
                 "imaginary.",
        "action": "Set the macro on the host or host group to the device the host "
                  "really has, or correct the template default. Where the host has "
                  "no block device to speak of, unlink the disk template instead of "
                  "disabling the items one at a time.",
    },
    {
        "id": "interface-missing",
        "pattern": re.compile(r"Cannot find information for this network interface", re.I),
        "label": "Network interface in the key does not exist on this host",
        "bucket": "macro",
        "cause": "The interface name in the key is not in /proc/net/dev. Either a "
                 "macro such as {$NET.IFACE} was never set for this host and the "
                 "template default was used, or the host renamed its interface "
                 "(eth0 to ens18 to enp0s3 across distribution and virtualisation "
                 "changes).",
        "action": "Set the macro per host, or better, let the network interface "
                  "discovery rule create the items so the name comes from the host "
                  "rather than from an assumption.",
    },
    {
        "id": "oid-not-implemented",
        "pattern": re.compile(r"No Such Object available on this agent at this OID", re.I),
        "label": "Device does not implement that OID at all",
        "bucket": "hardware",
        "cause": "The device answered and said it has no such object. This is the "
                 "honest hardware case: the MIB the template assumes is not present "
                 "on this model or this firmware. Nothing on the Zabbix side can "
                 "make the value appear.",
        "action": "Confirm with a walk of the parent OID so you are sure it is the "
                  "object and not the community or context. Then disable it: at the "
                  "template if no host of that model has the OID, on the host if it "
                  "is one odd unit among many that work.",
    },
    {
        "id": "oid-instance-missing",
        "pattern": re.compile(r"No Such Instance currently exists at this OID", re.I),
        "label": "OID exists on the device but that instance is gone",
        "bucket": "discovery",
        "cause": "The table is there; the row is not. A port, fan tray, PSU slot or "
                 "sensor that existed when discovery last ran and does not exist "
                 "now. Distinct from the previous case: the device does support the "
                 "MIB, it just no longer has that element.",
        "action": "Re-run the discovery rule and let it remove the item. Disabling "
                  "by hand leaves a dead object that discovery will not clean up and "
                  "that nobody will remember the reason for.",
    },
    {
        "id": "agent-metric-unknown",
        "pattern": re.compile(r"^Unknown metric ", re.I),
        "label": "Agent does not know this key",
        "bucket": "host",
        "cause": "The agent is running and replied, so the host is fine. It simply "
                 "has no implementation for the key. The key comes from an agent 2 "
                 "plugin that is not present in the build installed, or from a "
                 "UserParameter file that was never deployed to this host.",
        "action": "Deploy the plugin or the UserParameter, then the items recover on "
                  "their own. Where the host will never have it, unlink the template "
                  "rather than disabling every item it brought.",
    },
    {
        "id": "server-process-not-started",
        "pattern": re.compile(r'^No "[^"]+" processes started', re.I),
        "label": "Internal item for a process type that is not started",
        "bucket": "template",
        "cause": "An internal zabbix[process,...] item asking about a server or proxy "
                 "process whose Start* count is zero. Nothing is broken. The health "
                 "template covers every process type, and any given server or proxy "
                 "runs only some of them.",
        "action": "Expected. Disable the item on the health template for components "
                  "that do not run that subsystem, or accept it as permanent noise. "
                  "Do not start the process just to silence the item.",
    },
    {
        "id": "calculated-formula",
        "pattern": re.compile(r"Cannot evaluate expression|division by zero", re.I),
        "label": "Calculated item whose formula cannot be evaluated",
        "bucket": "template",
        "cause": "Division by zero means the divisor item returned zero, and the "
                 "usual source is a host with no swap or no configured capacity for "
                 "whatever is being expressed as a percentage. An unresolved "
                 "reference means the formula names a key that is not on the host.",
        "action": "Guard the formula, or stop linking the calculated item to hosts "
                  "where the divisor is legitimately zero. Disabling it hides a "
                  "template that is making an assumption about the estate that is "
                  "not true.",
    },
    {
        "id": "simple-check-no-interface",
        "pattern": re.compile(r"must have IP parameter or host interface specified", re.I),
        "label": "Simple check with nowhere to connect",
        "bucket": "host",
        "cause": "A simple check needs an address. The host has no interface of a "
                 "usable type and the key does not carry one either, so there is no "
                 "target. Common on hosts created by auto-registration without an "
                 "interface operation.",
        "action": "Add an interface to the host, or put the address in the key. "
                  "audit.py reports hosts with an unusable interface address and "
                  "fix.py interfaces can set it from the inventory.",
    },
    {
        "id": "key-not-supported",
        "pattern": re.compile(r"Unsupported item key", re.I),
        "label": "Key not implemented by the collector at all",
        "bucket": "template",
        "cause": "The agent or collector does not recognise the key, as opposed to "
                 "disliking one of its parameters. A template built for a different "
                 "agent version, or an item whose type does not match the key it "
                 "carries.",
        "action": "Correct the key or the item type on the template.",
    },
    {
        "id": "value-type-mismatch",
        "pattern": re.compile(r"cannot be converted|value should be|is not suitable", re.I),
        "label": "Returned data does not fit the item's value type",
        "bucket": "template",
        "cause": "The check worked and returned something. It is text where a number "
                 "is expected, or a format the item's type cannot hold. Firmware "
                 "changes that alter a returned string cause this without anything "
                 "in Zabbix changing.",
        "action": "Add a preprocessing step to extract the value, or widen the value "
                  "type on the template. This one is worth fixing rather than "
                  "disabling, because the data is arriving.",
    },
    {
        "id": "permission",
        "pattern": re.compile(r"permission denied|access denied|operation not permitted", re.I),
        "label": "Check refused by the operating system",
        "bucket": "host",
        "cause": "The agent ran the check and the host refused it. A file or device "
                 "the zabbix user cannot read, or a check that needs privilege it "
                 "does not have.",
        "action": "Grant read access, or move the check to a UserParameter that "
                  "elevates. Do not disable it: the metric is real and only the "
                  "account is wrong.",
    },
    {
        "id": "transient-collection",
        "pattern": re.compile(r"timeout|cannot connect|connection refused|no route to host|"
                              r"host unreachable|no response|temporary failure", re.I),
        "label": "Collection path failed rather than the item",
        "bucket": "transient",
        "cause": "The item is unsupported as a side effect of something else being "
                 "unavailable: the host, the proxy, the credential, or a firewall in "
                 "between. Every item on the affected host tends to appear at once, "
                 "which is the signature to look for.",
        "action": "Fix the path and re-check. These clear on their own and should "
                  "not be disabled; disabling them means the host stays dark after "
                  "it comes back.",
    },
)

UNKNOWN_CLASS = {
    "id": "unknown",
    "pattern": None,
    "label": "Not recognised by this tool",
    "bucket": "unknown",
    "cause": "This error does not match any pattern here. That means nobody has "
             "written down what it implies, not that it is harmless.",
    "action": "Read the full error on one item and reproduce the check by hand "
              "against the host. Do not disable anything in this group until the "
              "cause is understood, and add the pattern here once it is.",
}

_OID = re.compile(r"\.?\b\d+(?:\.\d+){3,}\b")
_QUOTED = re.compile(r'"[^"]*"')
_INT = re.compile(r"(?<![\w.])\d+(?![\w.])")
_WALK = re.compile(r"^Preprocessing failed for:.*?=\s*(\w+):", re.S)
_WALK_REASON = re.compile(r"\n\s*\d+\.\s*Failed:\s*(.+)$", re.S)
_UNKNOWN_METRIC = re.compile(r"^(Unknown metric )\S+$", re.I)


def signature(error: str) -> str:
    """Reduce an error to the cause it represents, so equal causes group.

    Raw error strings almost never repeat exactly. They carry the OID that
    failed, the index, the device name, sometimes several kilobytes of SNMP walk
    output. Grouping on the raw text produces one group per item and hides the
    fact that four hundred of them are the same three problems.
    """
    if not error:
        return "(no error text recorded)"
    text = error.strip()

    # A failed SNMP walk embeds the entire walk response. Keep the data type and
    # the numbered failure reason; both are what distinguish one walk failure
    # from another, and the payload in between is per-item noise.
    walk = _WALK.match(text)
    if walk:
        reason = _WALK_REASON.search(text)
        tail = reason.group(1).strip() if reason else "no reason reported"
        return f"Preprocessing failed on a {walk.group(1)} SNMP walk: {tail}"

    # Some collectors repeat the same line once per element they tried. The
    # repetition says nothing beyond "several", so collapse it.
    lines, seen = [], set()
    for raw in text.splitlines():
        line = raw.strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    text = " | ".join(lines[:3])
    if len(lines) > 3:
        text += " | ..."

    # "Unknown metric x" names the key that failed. Left alone it produces one
    # group per key, when the cause is one missing plugin or UserParameter.
    text = _UNKNOWN_METRIC.sub(r"\1<key>", text)

    text = _OID.sub(" <oid>", text)
    text = _QUOTED.sub('"<value>"', text)
    text = _INT.sub("<n>", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 150:
        text = text[:147] + "..."
    return text


def classify(error: str) -> dict:
    """Map an error to what is actually wrong and what to do about it."""
    text = (error or "").strip()
    for entry in CLASSES:
        if entry["pattern"].search(text):
            return entry
    return UNKNOWN_CLASS


def group(records: list[dict]) -> list[dict]:
    """Collapse item records into one group per cause, most common first."""
    buckets: dict[str, list[dict]] = {}
    for rec in records:
        buckets.setdefault(rec["signature"], []).append(rec)

    groups = []
    for sig, members in buckets.items():
        cls = classify(members[0]["error"])
        groups.append({
            "signature": sig,
            "class": cls,
            "count": len(members),
            "hosts": sorted({m["host"] for m in members}),
            "templates": sorted({m["template"] for m in members if m["template"]}),
            "keys": sorted({m["key"] for m in members}),
            "members": members,
        })
    # Ties sort by signature so repeated runs produce identical output, which
    # matters when this is diffed between days.
    groups.sort(key=lambda g: (-g["count"], g["signature"]))
    return groups


def matches(rec: dict, host: str = "", template: str = "") -> bool:
    """Substring filters, case-insensitive, both optional."""
    if host and host.lower() not in rec["host"].lower():
        return False
    return not (template and template.lower() not in (rec["template"] or "").lower())


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def _resolve_templates(z, parent_ids: set) -> dict:
    """Parent item id -> the template that actually defines the item.

    An item's templateid points at the item it was inherited from, not at a
    template, and that parent can itself be inherited through a chain of nested
    templates. Reporting the first hop sends the operator to a template that is
    only passing the item along. Walk to the top, where the item is defined and
    where changing it fixes every host below.

    Discovery rules have to be looked up separately: item.get does not return
    them, so a discovered item's ancestry ends in a blank unless discoveryrule
    .get is asked for the ids item.get did not recognise.
    """
    info: dict[str, tuple] = {}
    missing: set = set()
    pending = {str(p) for p in parent_ids if p not in ("0", 0, "", None)}
    for _ in range(8):  # bounded: nested template chains are shallow, and a
        todo = sorted(pending - set(info) - missing)  # cycle would spin forever
        if not todo:
            break
        found = set()
        for method in ("item.get", "discoveryrule.get"):
            batch = sorted(set(todo) - found)
            if not batch:
                break
            for row in z.call(method, {"itemids": batch,
                                       "output": ["itemid", "templateid"],
                                       "selectHosts": ["host"]}):
                hosts = row.get("hosts") or [{}]
                info[row["itemid"]] = (str(row.get("templateid") or "0"),
                                       hosts[0].get("host", ""))
                found.add(row["itemid"])
        missing |= set(todo) - found
        pending = {t for t, _ in info.values() if t != "0"}

    resolved = {}
    for start in {str(p) for p in parent_ids if p not in ("0", 0, "", None)}:
        cur, name, hops = start, "", 0
        while cur in info and hops < 8:
            parent, host = info[cur]
            name = host or name
            if parent == "0":
                break
            cur, hops = parent, hops + 1
        resolved[start] = name
    return resolved


def fetch(z) -> list[dict]:
    """Every monitored item currently in the unsupported state, shaped flat."""
    raw = z.call("item.get", {
        "filter": {"state": "1"},
        "monitored": True,
        "output": ["itemid", "name", "key_", "error", "flags", "templateid", "lastclock"],
        "selectHosts": ["host"],
        "selectDiscoveryRule": ["itemid", "name", "templateid"],
        "limit": 100000,
    })

    parents = {i.get("templateid") for i in raw}
    parents |= {(i.get("discoveryRule") or {}).get("templateid") for i in raw}
    names = _resolve_templates(z, parents)

    now = time.time()
    out = []
    for i in raw:
        rule = i.get("discoveryRule") or {}
        templateid = str(i.get("templateid") or "0")
        # A discovered item carries no templateid of its own, but the rule that
        # created it usually does, and that is the template an operator has to
        # go and change.
        template = names.get(templateid, "")
        if not template and rule:
            template = names.get(str(rule.get("templateid") or "0"), "")
        lastclock = int(i.get("lastclock") or 0)
        out.append({
            "itemid": i["itemid"],
            "name": i.get("name", ""),
            "key": i.get("key_", ""),
            "host": (i.get("hosts") or [{}])[0].get("host", "?"),
            "error": i.get("error") or "",
            "signature": signature(i.get("error") or ""),
            "templated": templateid != "0",
            "discovered": i.get("flags") == "4",
            "rule": rule.get("name", ""),
            "template": template,
            # None means the server cannot tell us, which is not the same as
            # zero. Every age comparison has to keep that distinction.
            "age_days": (now - lastclock) / 86400 if lastclock else None,
        })
    return out


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------
def cmd_list(z, args) -> int:
    records = [r for r in fetch(z) if matches(r, args.host, args.template)]
    if not records:
        print("  No unsupported items match those filters.")
        return 0

    all_groups = group(records)
    groups = [g for g in all_groups if g["count"] >= args.min_count]
    shown = sum(g["count"] for g in groups)

    print(f"  {len(records)} unsupported item(s) in {len(all_groups)} cause group(s).")
    if args.min_count > 1:
        print(f"  Showing groups of {args.min_count} or more: {shown} item(s).")
    print()

    for g in groups:
        cls = g["class"]
        print(f"  [{g['count']:>4}]  {cls['label']}  ({cls['bucket']})")
        print(f"          {g['signature']}")
        tmpl = f"{len(g['templates'])} template(s)" if g["templates"] else "no template"
        print(f"          {len(g['hosts'])} host(s), {tmpl}, {len(g['keys'])} distinct key(s)")
        for name in g["templates"][:3]:
            print(f"            template: {name}")
        if len(g["templates"]) > 3:
            print(f"            template: ... and {len(g['templates']) - 3} more")
        for key in g["keys"][:4]:
            print(f"            e.g. {key}")
        if len(g["keys"]) > 4:
            print(f"            ... and {len(g['keys']) - 4} more key(s)")
        print()

    by_bucket = Counter()
    for g in groups:
        by_bucket[g["class"]["bucket"]] += g["count"]
    print("  " + "-" * 70)
    print("  by recommended action: " +
          ", ".join(f"{b}={n}" for b, n in by_bucket.most_common()))
    unknown = by_bucket.get("unknown", 0)
    if unknown:
        print(f"  {unknown} item(s) are not recognised and need investigation. "
              "Run explain.")

    if args.fail_over is not None and len(records) > args.fail_over:
        print(f"\n  FAIL: {len(records)} unsupported items, over the "
              f"{args.fail_over} threshold", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# explain
# --------------------------------------------------------------------------
def cmd_explain(z, args) -> int:
    """The reference table, annotated with what this server actually has."""
    records = fetch(z)
    live = Counter()
    for rec in records:
        live[classify(rec["error"])["id"]] += 1

    print("  Recognised error classes. Counts are what this server has right now.\n")
    for entry in CLASSES:
        n = live.get(entry["id"], 0)
        marker = f"[{n:>4}]" if n else "[   -]"
        print(f"  {marker}  {entry['label']}")
        print(f"          bucket : {entry['bucket']}")
        print(f"          cause  : {_wrap(entry['cause'])}")
        print(f"          action : {_wrap(entry['action'])}")
        print()

    unknown = [r for r in records if classify(r["error"])["id"] == "unknown"]
    print("  " + "=" * 70)
    if unknown:
        print(f"  {len(unknown)} item(s) do not match any pattern above. They are "
              "not classified,")
        print("  and nothing here should be read as a diagnosis of them.\n")
        for sig, n in Counter(r["signature"] for r in unknown).most_common(10):
            example = next(r for r in unknown if r["signature"] == sig)
            print(f"    [{n:>4}]  {sig}")
            print(f"            e.g. {example['host']} / {example['key']}")
        print()
        print(f"  {UNKNOWN_CLASS['action']}")
    else:
        print("  Every unsupported item on this server matches a known class.")

    print("\n  " + "=" * 70)
    print("  What each bucket means:\n")
    for name, meaning in BUCKETS.items():
        print(f"    {name:<10} {_wrap(meaning, indent=15)}")
    return 0


def _wrap(text: str, width: int = 66, indent: int = 19) -> str:
    """Wrap for the fixed-label layout above without pulling in textwrap."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return ("\n" + " " * indent).join(lines)


# --------------------------------------------------------------------------
# disable
# --------------------------------------------------------------------------
def cmd_disable(z, args) -> int:
    """Disable unsupported items the operator has explicitly selected.

    There is no default selection on purpose. Disabling every unsupported item
    on a server would silence real faults along with the dead ones, and the
    result is indistinguishable from monitoring that works until something
    breaks that nobody hears about.
    """
    if not args.error and args.older_than is None:
        print("  Refusing to run without a selection. Pass --error with a pattern "
              "from list,")
        print("  optionally narrowed with --older-than, --host or --template.")
        return 2

    records = [r for r in fetch(z) if matches(r, args.host, args.template)]
    if args.error:
        needle = args.error.lower()
        records = [r for r in records
                   if needle in r["error"].lower() or needle in r["signature"].lower()]

    unknown_age = []
    if args.older_than is not None:
        aged, unknown_age = [], []
        for r in records:
            if r["age_days"] is None:
                unknown_age.append(r)
            elif r["age_days"] >= args.older_than:
                aged.append(r)
        records = aged

    if not records and not unknown_age:
        print("  Nothing matches that selection.")
        return 0

    # Splitting before printing, because the templated group is the interesting
    # one: it is where the fix actually belongs.
    templated = [r for r in records if r["templated"]]
    local = [r for r in records if not r["templated"]]

    print(f"  Selected {len(records) + len(unknown_age)} item(s).\n")

    if unknown_age:
        print(f"  {len(unknown_age)} item(s) excluded: the server does not expose an "
              "age for them.")
        print("  The only timestamp available is the last collected value, held in "
              "the server's")
        print("  value cache, which is emptied on restart. An absent timestamp is "
              "not evidence")
        print("  that an item is old, so --older-than skips these rather than "
              "guessing.\n")

    if templated:
        print(f"  Refusing to disable {len(templated)} templated item(s):\n")
        by_template = {}
        for r in templated:
            by_template.setdefault(r["template"] or "(template not resolved)",
                                   []).append(r)
        for name, members in sorted(by_template.items(),
                                    key=lambda kv: (-len(kv[1]), kv[0])):
            print(f"    {name}  ({len(members)} item(s), "
                  f"{len({m['host'] for m in members})} host(s))")
            for r in members[:4]:
                print(f"      {r['host'][:24]:<24} {r['key'][:46]}")
            if len(members) > 4:
                print(f"      ... and {len(members) - 4} more")
        print()
        print("  Disabling a templated item on one host changes nothing anywhere "
              "else. The")
        print("  template keeps the item, every other linked host keeps polling it, "
              "and the")
        print("  next host linked to that template starts failing too. The host-level "
              "change")
        print("  also becomes an exception nobody remembers making. Disable the item "
              "on the")
        print("  templates named above, or unlink them from hosts that do not have "
              "the")
        print("  hardware.\n")

    if not local:
        print("  Nothing left to disable on the host itself.")
        return 0

    buckets = Counter(classify(r["error"])["bucket"] for r in local)
    risky = {b: n for b, n in buckets.items() if b not in ("hardware",)}
    print(f"  {len(local)} host-level item(s) would be disabled:\n")
    for r in local[:40]:
        mark = " (discovered)" if r["discovered"] else ""
        print(f"    {r['host'][:22]:<22} {r['key'][:48]:<48}{mark}")
    if len(local) > 40:
        print(f"    ... and {len(local) - 40} more")

    discovered = [r for r in local if r["discovered"]]
    if discovered:
        print(f"\n  {len(discovered)} of these were created by low-level discovery. "
              "Disabling stops")
        print("  the polling now, but the rule that created them is still running, "
              "so the")
        print("  durable fix is the rule's filter or the item prototype:")
        origins = Counter((r["rule"] or "(rule unknown)", r["template"] or "host-local")
                          for r in discovered)
        for (rule, template), n in origins.most_common(5):
            print(f"    {n:>4}  {rule}  on  {template}")

    if risky:
        print("\n  Note: not every selected item is the hardware case this command "
              "is for.")
        for bucket, n in sorted(risky.items(), key=lambda kv: -kv[1]):
            print(f"    {n} item(s) classified {bucket}: {_wrap(BUCKETS[bucket], indent=6)}")
        print("  Disabling those hides a fault rather than resolving one.")

    if not args.apply:
        print(f"\n  DRY RUN — would disable {len(local)} item(s). Re-run with --apply.")
        return 0

    done, failed = 0, []
    for r in local:
        try:
            z.call("item.update", {"itemid": r["itemid"], "status": 1})
            done += 1
        except ZabbixError as e:
            failed.append((r, e))
    print(f"\n  Disabled {done} of {len(local)} item(s).")
    for r, e in failed[:10]:
        print(f"  ! {r['host']} / {r['key']}: {e}", file=sys.stderr)
    if done:
        print("  Re-enable from the host's item list, or with item.update status 0.")
    return 0 if not failed else 1


# --------------------------------------------------------------------------
# trends
# --------------------------------------------------------------------------
def cmd_trends(z, args) -> int:
    """Is the unsupported count accumulating, or is it a stable old backlog?

    The distinction decides what to do. A stable set is a cleanup job that can
    wait for a maintenance window. A growing set means something changed
    recently and is still changing, and the cause is worth finding today.
    """
    records = fetch(z)
    print(f"  Unsupported now : {len(records)} item(s)")
    print(f"  Window          : {args.days} day(s)\n")

    # Zabbix records an item going unsupported as an internal event. That is the
    # only per-item history of the transition; the item object itself carries no
    # timestamp for when its state changed.
    since = int(time.time()) - args.days * 86400
    events = None
    try:
        events = z.call("event.get", {"source": 3, "object": 4, "value": 1,
                                      "time_from": since,
                                      "output": ["eventid", "clock", "objectid"],
                                      "limit": 100000})
    except ZabbixError as e:
        print(f"  Internal events are not readable: {e}")

    if events:
        objects = {e["objectid"] for e in events}
        print(f"  Items that went unsupported in the window : {len(objects)}")
        print(f"  Transitions recorded                      : {len(events)}")
        if len(events) > len(objects) * 2:
            print("\n  More transitions than items: items are flapping in and out of "
                  "unsupported")
            print("  rather than failing once. Look at timeouts and reachability "
                  "before keys.")
        share = len(objects) / len(records) if records else 0
        if share > 0.5:
            print(f"\n  {share:.0%} of the current backlog appeared inside the window. "
                  "This is")
            print("  accumulating, and something changed recently. Compare against "
                  "template")
            print("  changes and firmware or agent upgrades in the same period.")
        else:
            print(f"\n  {share:.0%} of the current backlog appeared inside the window. "
                  "The rest is")
            print("  older than that: a stable backlog, not an active regression.")
    elif events is not None:
        print("  The server returned no internal item events for this window.\n")
        print("  This is not evidence that nothing changed. Internal events are only "
              "kept if")
        print("  the server is configured to store them and housekeeping has not "
              "removed them")
        print("  yet, and both are commonly off. Without them the API cannot say "
              "when any")
        print("  individual item became unsupported, so this tool will not put a "
              "number on")
        print("  whether the backlog is growing.")
        print("\n  To get a real answer, either enable internal event storage and "
              "wait a")
        print("  window, or record the count from list on a schedule and compare "
              "the series.")

    known = sum(1 for r in records if r["age_days"] is not None)
    print(f"\n  Items with any timestamp at all : {known} of {len(records)}")
    if known < len(records):
        print("  The rest have no last-value timestamp. That comes from the server's "
              "value")
        print("  cache, which is emptied on restart, so it means the cache does not "
              "hold one")
        print("  right now — not that the item never worked.")

    buckets = Counter(classify(r["error"])["bucket"] for r in records)
    print("\n  Current backlog by recommended action:")
    for bucket, n in buckets.most_common():
        print(f"    {bucket:<10} {n:>5}")
    print("\n  Buckets other than transient do not clear on their own. If those are "
          "not")
    print("  falling between runs, nobody is working the backlog.")
    return 0


# --------------------------------------------------------------------------
COMMANDS = {
    "list": (cmd_list, "unsupported items grouped by cause, most common first"),
    "explain": (cmd_explain, "what each error class means and what fixes it"),
    "disable": (cmd_disable, "disable selected items (dry run until --apply)"),
    "trends": (cmd_trends, "whether the backlog is growing or stable"),
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help=COMMANDS["list"][1])
    p.add_argument("--host", default="", help="only hosts whose name contains this")
    p.add_argument("--template", default="", help="only items from a matching template")
    p.add_argument("--min-count", type=int, default=1,
                   help="hide cause groups smaller than this")
    p.add_argument("--fail-over", type=int, metavar="N",
                   help="exit non-zero if more than N items are unsupported")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("explain", help=COMMANDS["explain"][1])
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("disable", help=COMMANDS["disable"][1])
    p.add_argument("--apply", action="store_true",
                   help="make the change (without this it is a dry run)")
    p.add_argument("--error", default="",
                   help="only items whose error or cause contains this text")
    p.add_argument("--older-than", type=float, metavar="DAYS",
                   help="only items whose last value is at least this old")
    p.add_argument("--host", default="", help="only hosts whose name contains this")
    p.add_argument("--template", default="", help="only items from a matching template")
    p.set_defaults(func=cmd_disable)

    p = sub.add_parser("trends", help=COMMANDS["trends"][1])
    p.add_argument("--days", type=int, default=30, help="window to look back over")
    p.set_defaults(func=cmd_trends)

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
