#!/usr/bin/env python3
"""Canonicalise Zabbix exports so they can be diffed, reviewed and promoted.

    ./scripts/canon.py export -o templates/          # Zabbix -> canonical files
    ./scripts/canon.py normalise export.json         # any export -> canonical form
    ./scripts/canon.py diff old.json new.json        # semantic diff
    ./scripts/canon.py diff old.json new.json --json

Why this exists
---------------

A Zabbix export is not stable enough to put in git and diff:

* The version header changes on server upgrade, so every file appears to change
  at once and a review of one template becomes a review of all of them.
* List ordering is not guaranteed consistent between servers or between exports.
  Two servers holding identical configuration produce different files.
* Whether default-valued fields are emitted has shifted between versions.

The result is that config-versioning efforts produce diffs nobody can read, and
are abandoned. Normalisation logic exists inside individual exporters but is not
available as a primitive, so everyone rebuilds a worse version of it.

This works in JSON rather than YAML. Zabbix exports and imports both, and the
standard library parses JSON — which keeps this dependency-free.

What canonical means here
-------------------------

1. The version header is removed. It carries no configuration meaning and
   churns on upgrade. Import supplies its own.
2. Every list whose order is not semantically meaningful is sorted by a stable
   key appropriate to its contents: items by key, triggers by name and
   expression, macros by macro name.
3. Lists whose order *is* meaningful — preprocessing steps, dashboard widgets,
   override operations — are left exactly as they are. Sorting them would change
   behaviour.
4. Empty strings, empty lists and empty objects are dropped.
5. Object keys are sorted, and output is written with a trailing newline and
   two-space indent so the file is diff-friendly.

UUIDs are preserved untouched. Import matches on UUID first and name second;
regenerating or stripping them turns an update into a duplicate.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from zbx import ZabbixError, connect_or_exit  # noqa: E402

#: Sort key per list name. A tuple means "sort by these fields in order".
#: Anything not listed and not order-significant is sorted by its JSON text,
#: which is stable even if not semantically pretty.
SORT_KEYS: dict[str, tuple[str, ...]] = {
    "template_groups": ("name",),
    "host_groups": ("name",),
    "groups": ("name",),
    "templates": ("template",),
    "hosts": ("host",),
    "items": ("key",),
    "discovery_rules": ("key",),
    "item_prototypes": ("key",),
    "triggers": ("name", "expression"),
    "trigger_prototypes": ("name", "expression"),
    "graphs": ("name",),
    "graph_prototypes": ("name",),
    "host_prototypes": ("host",),
    "macros": ("macro",),
    "tags": ("tag", "value"),
    "valuemaps": ("name",),
    "value_maps": ("name",),
    "mappings": ("value",),
    "media_types": ("name",),
    "mediaTypes": ("name",),
    "dashboards": ("name",),
    "images": ("name",),
    "maps": ("name",),
    "parameters": ("name",),
    "headers": ("name",),
    "interfaces": ("interface_ref",),
    "templates_links": ("name",),
    "linked_templates": ("name",),
}

#: Lists whose order carries meaning. Sorting these changes what Zabbix does.
#:  - preprocessing runs in sequence, each step consuming the previous result
#:  - dashboard pages, widgets and fields are a layout
#:  - LLD overrides are evaluated in order and can stop processing
ORDER_SIGNIFICANT = frozenset({
    "preprocessing", "steps", "pages", "widgets", "fields", "overrides",
    "operations", "filters", "conditions",
    # A preprocessing step's parameters are positional: for SNMP_WALK_TO_JSON
    # they are (macro, oid, format) triplets, and reordering them changes what
    # the step does. The same key is also used for script item parameters,
    # which are name/value objects and would be safe to sort -- the scalar-list
    # rule below distinguishes them.
    "parameters", "params",
})


def _sort_key(name: str, obj):
    if not isinstance(obj, dict):
        return (0, json.dumps(obj, sort_keys=True))
    fields = SORT_KEYS.get(name)
    if fields:
        # Missing field sorts first rather than raising; exports are not
        # guaranteed to carry every optional key.
        return (0, tuple(str(obj.get(f, "")) for f in fields))
    return (1, json.dumps(obj, sort_keys=True))


def canonicalise(node, name: str = ""):
    """Recursively normalise an export tree."""
    if isinstance(node, dict):
        out = {}
        for k in sorted(node):
            v = canonicalise(node[k], k)
            # Drop empties. A missing key and an empty one mean the same thing
            # to the importer, but only one of them shows up in a diff.
            if v in ("", [], {}, None):
                continue
            out[k] = v
        return out
    if isinstance(node, list):
        items = [canonicalise(v, name) for v in node]
        if name in ORDER_SIGNIFICANT:
            return items
        # A list of scalars is almost always positional — arguments, an ordered
        # sequence of values — and has no identity to sort by. Sorting one
        # silently changes meaning, so scalar lists are never reordered.
        if any(not isinstance(v, dict) for v in items):
            return items
        return sorted(items, key=lambda o: _sort_key(name, o))
    return node


def normalise_document(raw: str) -> dict:
    """Parse an export and return its canonical form, header removed."""
    doc = json.loads(raw)
    body = doc.get("zabbix_export", doc)
    # The version header identifies the exporting server, not the configuration.
    # Keeping it makes every file diff after an upgrade.
    body = {k: v for k, v in body.items() if k != "version"}
    return canonicalise(body)


def dumps(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# Semantic diff
# --------------------------------------------------------------------------
#: Removing one of these destroys collected data or open state, not just config.
DESTRUCTIVE_LOSS = {
    "items": "deletes the item and all of its collected history",
    "item_prototypes": "deletes every item it discovered, and their history",
    "discovery_rules": "deletes everything the rule discovered",
    "triggers": "closes any open problem it raised, losing the event",
    "trigger_prototypes": "closes open problems on discovered triggers",
    "templates": "unlinks from every host, deleting inherited items and history",
    "hosts": "deletes the host and everything collected for it",
}


def _identity(name: str, obj) -> str:
    if not isinstance(obj, dict):
        return json.dumps(obj, sort_keys=True)
    for field in SORT_KEYS.get(name, ()):
        if obj.get(field):
            return str(obj[field])
    for field in ("uuid", "name", "key", "template", "host", "macro"):
        if obj.get(field):
            return str(obj[field])
    return json.dumps(obj, sort_keys=True)


def diff(old, new, path: str = "", name: str = "", out: list | None = None) -> list:
    """Compare two canonical trees. Returns a list of classified changes."""
    out = [] if out is None else out

    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new)):
            sub = f"{path}.{k}" if path else k
            if k not in new:
                out.append({"kind": "removed", "path": sub, "what": k,
                            "detail": DESTRUCTIVE_LOSS.get(k, ""),
                            "old": _short(old[k]), "new": None})
            elif k not in old:
                out.append({"kind": "added", "path": sub, "what": k,
                            "old": None, "new": _short(new[k])})
            else:
                diff(old[k], new[k], sub, k, out)
        return out

    if isinstance(old, list) and isinstance(new, list):
        o = {_identity(name, v): v for v in old}
        n = {_identity(name, v): v for v in new}
        for key in sorted(set(o) | set(n)):
            sub = f"{path}[{key}]"
            if key not in n:
                out.append({"kind": "removed", "path": sub, "what": name,
                            "detail": DESTRUCTIVE_LOSS.get(name, ""),
                            "old": _short(o[key]), "new": None})
            elif key not in o:
                out.append({"kind": "added", "path": sub, "what": name,
                            "old": None, "new": _short(n[key])})
            else:
                diff(o[key], n[key], sub, name, out)
        return out

    if old != new:
        out.append({"kind": "changed", "path": path, "what": name,
                    "old": _short(old), "new": _short(new)})
    return out


def _short(v, limit: int = 70) -> str:
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True)
    return s if len(s) <= limit else s[:limit - 1] + "…"


def classify(changes: list) -> dict:
    additive = [c for c in changes if c["kind"] == "added"]
    mutating = [c for c in changes if c["kind"] == "changed"]
    destructive = [c for c in changes if c["kind"] == "removed"]
    data_loss = [c for c in destructive if c.get("detail")]
    return {"additive": additive, "mutating": mutating,
            "destructive": destructive, "data_loss": data_loss}


# --------------------------------------------------------------------------
def cmd_normalise(args) -> int:
    raw = sys.stdin.read() if args.file == "-" else pathlib.Path(args.file).read_text()
    doc = normalise_document(raw)
    if args.in_place and args.file != "-":
        pathlib.Path(args.file).write_text(dumps(doc))
        print(f"  normalised {args.file}")
    else:
        sys.stdout.write(dumps(doc))
    return 0


def cmd_export(args) -> int:
    z = connect_or_exit()
    out = pathlib.Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    tmpls = z.call("template.get", {"output": ["templateid", "host", "vendor_name"],
                                    "search": {"host": args.prefix} if args.prefix else None}
                   if args.prefix else {"output": ["templateid", "host", "vendor_name"]})
    if not args.all_templates:
        tmpls = [t for t in tmpls if not t.get("vendor_name")]
    if not tmpls:
        print("  no templates matched", file=sys.stderr)
        return 1
    for t in sorted(tmpls, key=lambda x: x["host"]):
        try:
            raw = z.call("configuration.export",
                         {"format": "json", "options": {"templates": [t["templateid"]]}})
        except ZabbixError as e:
            print(f"  ! {t['host']}: {e}", file=sys.stderr)
            continue
        slug = t["host"].lower().replace(" ", "-").replace("/", "-")
        path = out / f"{slug}.json"
        path.write_text(dumps(normalise_document(raw)))
        print(f"  {t['host'][:40]:<40} -> {path.name}")
    print(f"\n  {len(tmpls)} template(s) written to {out}/ in canonical form.")
    return 0


def cmd_diff(args) -> int:
    a = normalise_document(pathlib.Path(args.old).read_text())
    b = normalise_document(pathlib.Path(args.new).read_text())
    changes = diff(a, b)
    c = classify(changes)

    if args.json:
        print(json.dumps({"changes": changes,
                          "totals": {k: len(v) for k, v in c.items()}}, indent=2))
    else:
        if not changes:
            print("  No differences.")
            return 0
        for label, key in (("DESTRUCTIVE", "destructive"),
                           ("MUTATING", "mutating"),
                           ("ADDITIVE", "additive")):
            group = c[key]
            if not group:
                continue
            print(f"\n{label}  ({len(group)})")
            for ch in group[:args.limit]:
                print(f"  {ch['path']}")
                if ch["kind"] == "changed":
                    print(f"    {ch['old']}  ->  {ch['new']}")
                elif ch["kind"] == "removed":
                    print(f"    was: {ch['old']}")
                    if ch.get("detail"):
                        print(f"    !! {ch['detail']}")
                else:
                    print(f"    now: {ch['new']}")
            if len(group) > args.limit:
                print(f"  … and {len(group) - args.limit} more")
        print(f"\n{len(c['additive'])} additive, {len(c['mutating'])} mutating, "
              f"{len(c['destructive'])} destructive "
              f"({len(c['data_loss'])} of which lose collected data)")

    if args.fail_on_destructive and c["destructive"]:
        print("\nFAIL: destructive changes present", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("normalise", help="rewrite an export in canonical form")
    n.add_argument("file", help="export file, or - for stdin")
    n.add_argument("--in-place", action="store_true")
    n.set_defaults(func=cmd_normalise)

    e = sub.add_parser("export", help="export templates from Zabbix, canonical")
    e.add_argument("-o", "--output", default="templates", help="output directory")
    e.add_argument("--prefix", help="only templates whose name contains this")
    e.add_argument("--all-templates", action="store_true",
                   help="include vendor templates")
    e.set_defaults(func=cmd_export)

    d = sub.add_parser("diff", help="semantic diff between two exports")
    d.add_argument("old")
    d.add_argument("new")
    d.add_argument("--json", action="store_true")
    d.add_argument("--limit", type=int, default=25, help="entries shown per class")
    d.add_argument("--fail-on-destructive", action="store_true",
                   help="exit non-zero if anything would be removed")
    d.set_defaults(func=cmd_diff)

    args = ap.parse_args()
    try:
        return args.func(args)
    except (ZabbixError, OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
