#!/usr/bin/env python3
"""Build a Zabbix 7.x SNMP template from a device walk.

    snmpwalk -v2c -c public -On 10.0.0.5 1.3.6.1.2.1 > device.walk
    ./scripts/walk2tmpl.py device.walk --name "My Switch by SNMP" > template.json
    ./scripts/tmpltest.py run template.json --against 10.0.0.5

Why generate from a walk rather than a MIB
------------------------------------------

The canonical MIB-to-Zabbix tool emits Zabbix 3.x XML, three major versions out
of date, and nothing has replaced it. The other reason to avoid MIB compilation
is that most of a vendor MIB is not implemented on any given device: compiling
the MIB produces thousands of items that will sit unsupported forever, and
someone then has to prune them by hand.

A walk contains exactly what the device answers. Everything generated from it is
known to work on that hardware.

What it produces
----------------

Modern Zabbix SNMP structure, not one item per OID:

* One ``snmp.walk[...]`` master item per subtree. The device is polled once and
  the result is split locally, instead of one GET per metric.
* Dependent items with SNMP-walk-value preprocessing for scalars.
* A discovery rule per detected table, with ``snmp.walk`` and LLD macros
  built from the table index, plus dependent item prototypes.

Counters get change-per-second preprocessing, since a raw counter is rarely what
anyone wants to alert on. Text and enumerated values are left alone.

Output is canonical JSON, so it can go straight into git and be diffed with
``canon.py``.

Everything is generated **disabled**. A generated template is a starting point:
review it, keep what is useful, and enable what you keep. Importing several
hundred enabled items against production equipment without reading them first is
how a generator earns a bad reputation.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from canon import canonicalise, dumps  # noqa: E402

# .1.3.6.1.2.1.1.5.0 = STRING: switch01
WALK_LINE = re.compile(r"^(\.[\d.]+)\s*=\s*([A-Za-z0-9-]+):?\s*(.*)$")

#: SNMP types that are monotonically increasing and want a rate, not a level.
COUNTER_TYPES = {"Counter32", "Counter64"}
#: Types Zabbix stores as text.
TEXT_TYPES = {"STRING", "OID", "Hex-STRING", "IpAddress", "Network"}

VALUE_TYPE = {"float": "FLOAT", "char": "CHAR", "log": "LOG",
              "uint": "UNSIGNED", "text": "TEXT"}

#: Well-known subtrees, so generated names read like something rather than a
#: numeric OID. Anything unlisted keeps its OID, which is honest.
KNOWN = {
    "1.3.6.1.2.1.1": "System",
    "1.3.6.1.2.1.2.2.1": "Interface",
    "1.3.6.1.2.1.25.1": "Host",
    "1.3.6.1.2.1.25.2.3.1": "Storage",
    "1.3.6.1.2.1.25.3.3.1": "Processor",
    "1.3.6.1.2.1.31.1.1.1": "Interface (extended)",
    "1.3.6.1.2.1.4": "IP",
    "1.3.6.1.2.1.6": "TCP",
    "1.3.6.1.2.1.7": "UDP",
}

SCALAR_NAMES = {
    "1.3.6.1.2.1.1.1": "System description", "1.3.6.1.2.1.1.3": "Uptime",
    "1.3.6.1.2.1.1.4": "System contact", "1.3.6.1.2.1.1.5": "System name",
    "1.3.6.1.2.1.1.6": "System location", "1.3.6.1.2.1.1.2": "System object ID",
}

COLUMN_NAMES = {
    "1.3.6.1.2.1.2.2.1.2": "Interface name", "1.3.6.1.2.1.2.2.1.5": "Speed",
    "1.3.6.1.2.1.2.2.1.7": "Admin status", "1.3.6.1.2.1.2.2.1.8": "Operational status",
    "1.3.6.1.2.1.2.2.1.10": "Bits received", "1.3.6.1.2.1.2.2.1.16": "Bits sent",
    "1.3.6.1.2.1.2.2.1.13": "Inbound packets discarded",
    "1.3.6.1.2.1.2.2.1.14": "Inbound packets with errors",
    "1.3.6.1.2.1.2.2.1.19": "Outbound packets discarded",
    "1.3.6.1.2.1.2.2.1.20": "Outbound packets with errors",
}


def _uuid() -> str:
    return uuid.uuid4().hex


def parse_walk(text: str) -> list[tuple[str, str, str]]:
    """Return (oid, type, value) for each line, ignoring noise."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("Timeout", "snmpwalk:", "End of MIB")):
            continue
        m = WALK_LINE.match(line)
        if not m:
            continue
        oid, typ, val = m.group(1).lstrip("."), m.group(2), m.group(3).strip()
        out.append((oid, typ, val))
    return out


def split_scalars_and_tables(entries):
    """A scalar ends in .0. Anything else is a table cell: <column>.<index>."""
    scalars, tables = {}, {}
    for oid, typ, val in entries:
        if oid.endswith(".0"):
            # Keyed on the base OID for readability; the full OID is retained
            # because preprocessing has to match the walk exactly.
            scalars[oid[:-2]] = (typ, val, oid)
            continue
        parts = oid.rsplit(".", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        column, index = parts
        table = column.rsplit(".", 1)[0]
        tables.setdefault(table, {}).setdefault(column, {})[index] = (typ, val)
    # A "table" with one index and one column is almost certainly a
    # misclassified scalar; requiring two indexes avoids inventing discovery.
    tables = {t: cols for t, cols in tables.items()
              if max((len(v) for v in cols.values()), default=0) >= 2}
    return scalars, tables


def _value_type(snmp_type: str) -> str:
    if snmp_type in TEXT_TYPES:
        return VALUE_TYPE["char"]
    return VALUE_TYPE["float"]


def _preprocessing(oid: str, snmp_type: str) -> list[dict]:
    steps = [{"type": "SNMP_WALK_VALUE", "parameters": [oid, "0"]}]
    if snmp_type in COUNTER_TYPES:
        # A raw counter is not useful to alert on and wraps. Rate per second is
        # what anyone actually graphs.
        steps.append({"type": "CHANGE_PER_SECOND"})
    return steps


def _pretty(oid: str, fallback: str) -> str:
    return SCALAR_NAMES.get(oid) or COLUMN_NAMES.get(oid) or fallback


def build_template(entries, name: str, delay: str,
                   community: str = "public") -> dict:
    scalars, tables = split_scalars_and_tables(entries)
    items, discovery_rules = [], []

    # Scalars can live in several subtrees (system, interfaces, host resources).
    # One master per subtree, derived from the data rather than assumed, so no
    # scalar ends up with a master whose walk does not contain it.
    by_subtree: dict[str, list] = {}
    for base_oid, (typ, _val, full_oid) in sorted(scalars.items()):
        subtree = base_oid.rsplit(".", 1)[0]
        by_subtree.setdefault(subtree, []).append((base_oid, typ, full_oid))

    for subtree, group in sorted(by_subtree.items()):
        label = KNOWN.get(subtree, subtree)
        master_key = f"snmp.walk[{subtree}]"
        items.append({
            "uuid": _uuid(), "name": f"{label}: walk of {subtree}",
            "type": "SNMP_AGENT", "key": master_key,
            "snmp_oid": f"walk[{subtree}]", "delay": delay,
            "value_type": VALUE_TYPE["text"], "history": "0", "trends": "0",
            "status": "DISABLED",
            "description": "Master item. One request per interval; the values "
                           "below are extracted from it locally.",
        })
        for base_oid, typ, full_oid in group:
            items.append({
                "uuid": _uuid(), "name": _pretty(base_oid, f"OID {base_oid}"),
                "type": "DEPENDENT", "key": f"snmp[{base_oid}]",
                "value_type": _value_type(typ), "status": "DISABLED",
                "master_item": {"key": master_key},
                "preprocessing": _preprocessing(full_oid, typ),
                "description": f"From {full_oid} ({typ}).",
            })

    for table, columns in sorted(tables.items()):
        label = KNOWN.get(table, f"Table {table}")
        # Prefer a descriptive column as the LLD name; fall back to the index.
        name_col = next((c for c in columns
                         if COLUMN_NAMES.get(c, "").lower().endswith("name")), None)

        # The table needs its own master item. A discovery rule cannot be the
        # master of an item prototype -- the master must be an item -- so the
        # walk is collected once here and both the rule and the prototypes hang
        # off it as dependents.
        table_master = f"snmp.walk[{table}]"
        items.append({
            "uuid": _uuid(), "name": f"{label}: walk of {table}",
            "type": "SNMP_AGENT", "key": table_master,
            "snmp_oid": f"walk[{table}]", "delay": delay,
            "value_type": VALUE_TYPE["text"], "history": "0", "trends": "0",
            "status": "DISABLED",
            "description": "Master item for the table below. One request per "
                           "interval, split locally.",
        })

        proto = []
        for column, cells in sorted(columns.items()):
            sample_type = next(iter(cells.values()))[0]
            proto.append({
                "uuid": _uuid(),
                "name": f"{label} {{#ENTRY}}: {_pretty(column, 'OID ' + column)}",
                "type": "DEPENDENT", "key": f"snmp[{column},{{#SNMPINDEX}}]",
                "value_type": _value_type(sample_type), "status": "DISABLED",
                "master_item": {"key": table_master},
                "preprocessing": [
                    {"type": "SNMP_WALK_VALUE",
                     "parameters": [f"{column}.{{#SNMPINDEX}}", "0"]},
                    *([{"type": "CHANGE_PER_SECOND"}]
                      if sample_type in COUNTER_TYPES else []),
                ],
                "description": f"From {column}.{{#SNMPINDEX}} ({sample_type}).",
            })

        discovery_rules.append({
            "uuid": _uuid(), "name": f"{label} discovery",
            "type": "DEPENDENT", "key": f"snmp.discovery[{table}]",
            "master_item": {"key": table_master}, "status": "DISABLED",
            "preprocessing": [{
                "type": "SNMP_WALK_TO_JSON",
                # Triplets of (macro, oid, format). Zabbix supplies
                # {#SNMPINDEX} itself, so this adds the readable macro only.
                "parameters": [
                    "{#ENTRY}", name_col or next(iter(columns)), "0",
                ],
            }],
            "item_prototypes": proto,
            "description": f"Discovers rows of {table} from the walk above.",
        })

    doc = {
        "templates": [{
            "uuid": _uuid(), "template": name, "name": name,
            "description": ("Generated from an SNMP walk by walk2tmpl.py. Every "
                            "item ships disabled: review, keep what is useful, "
                            "and enable only that."),
            "groups": [{"name": "Templates/Network devices"}],
            "items": items,
            "discovery_rules": discovery_rules,
            "macros": [{"macro": "{$SNMP_COMMUNITY}", "value": community}],
        }],
        "template_groups": [{"uuid": _uuid(), "name": "Templates/Network devices"}],
    }
    return canonicalise(doc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("walk", help="snmpwalk output (use -On for numeric OIDs)")
    ap.add_argument("--name", required=True, help="template name")
    ap.add_argument("--delay", default="1m", help="polling interval for masters")
    ap.add_argument("--community", default="public",
                    help="default {$SNMP_COMMUNITY} in the generated template")
    ap.add_argument("-o", "--output", help="write here instead of stdout")
    ap.add_argument("--summary", action="store_true",
                    help="describe what was generated instead of emitting it")
    args = ap.parse_args()

    text = pathlib.Path(args.walk).read_text(errors="replace")
    entries = parse_walk(text)
    if not entries:
        print("error: no usable OID lines. Was the walk captured with -On?",
              file=sys.stderr)
        return 1

    doc = build_template(entries, args.name, args.delay, args.community)
    t = doc["templates"][0]
    scalars = len([i for i in t.get("items", []) if i["type"] == "DEPENDENT"])
    masters = len([i for i in t.get("items", []) if i["type"] == "SNMP_AGENT"])
    rules = t.get("discovery_rules", [])
    protos = sum(len(r.get("item_prototypes", [])) for r in rules)

    if args.summary:
        print(f"  parsed          {len(entries)} OID line(s)")
        print(f"  scalar items    {scalars} (plus {masters} walk master(s))")
        print(f"  discovery rules {len(rules)}")
        for r in rules:
            print(f"    {r['name']}  ->  {len(r.get('item_prototypes', []))} prototype(s)")
        print(f"  prototypes      {protos}")
        print("\n  Everything is disabled. Review before enabling.")
        return 0

    out = dumps({"zabbix_export": dict(doc, version="7.4")})
    if args.output:
        pathlib.Path(args.output).write_text(out)
        print(f"  wrote {args.output}: {scalars} scalar item(s), {masters} walk "
              f"master(s), {len(rules)} discovery rule(s), {protos} prototype(s), "
              "all disabled")
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
