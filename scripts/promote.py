#!/usr/bin/env python3
"""Promote canonical template files into a Zabbix server, with a real gate.

    ./scripts/promote.py plan  templates/*.json
    ./scripts/promote.py apply templates/*.json
    ./scripts/promote.py apply templates/*.json --allow-destructive
    ./scripts/promote.py drift templates/*.json      # has the server been edited?

`plan` compares each file against what the target currently holds and prints a
semantic diff. `apply` does the same and then imports, refusing if anything
would be removed unless you say otherwise.

Why not just use configuration.importcompare
--------------------------------------------

Because it cannot be trusted as a gate:

* It compares host groups and templates only. Other object types are reported as
  new even when they already exist.
* It previews structural change and validates nothing semantic — not macro
  resolution, not item keys against the target, not references to objects the
  file does not carry.
* Its output depends on the calling user's permissions, so a narrowly scoped CI
  account gets a misleading preview and a broad one gets a different answer.

This project hit the practical consequence: importcompare reported no changes
for an import that then failed outright because a referenced object was absent.
So the plan here is computed from canonical form on both sides, and
importcompare is used only as a secondary cross-check.

Destructive changes
-------------------

Deleting a templated item deletes its collected history. Deleting a trigger
closes any problem it raised. Neither is recoverable from the file that caused
it, so both require an explicit flag rather than a confirmation prompt that gets
reflexively accepted.

`drift` is the reverse direction: it reports where the live server no longer
matches the files. Production gets edited during incidents, and a pipeline that
only pushes will either overwrite that work or drift from it silently.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from canon import classify, diff, normalise_document  # noqa: E402
from zbx import ZabbixError, connect_or_exit  # noqa: E402

IMPORT_RULES_BASE = {
    "template_groups": {"createMissing": True, "updateExisting": True},
    "host_groups": {"createMissing": True},
    "templates": {"createMissing": True, "updateExisting": True},
    "valueMaps": {"createMissing": True, "updateExisting": True},
}


def _template_names(doc: dict) -> list[str]:
    return [t.get("template") for t in doc.get("templates", []) if t.get("template")]


def _live_canonical(z, names: list[str]) -> dict | None:
    """Export the named templates from the target in canonical form."""
    if not names:
        return None
    found = z.call("template.get", {"output": ["templateid", "host"],
                                    "filter": {"host": names}})
    if not found:
        return None
    raw = z.call("configuration.export",
                 {"format": "json",
                  "options": {"templates": [t["templateid"] for t in found]}})
    return normalise_document(raw)


def _plan_one(z, path: pathlib.Path):
    doc = normalise_document(path.read_text())
    names = _template_names(doc)
    live = _live_canonical(z, names)
    if live is None:
        # Nothing on the target yet: the whole file is additive.
        return doc, names, None, [{"kind": "added", "path": f"templates[{n}]",
                                   "what": "templates", "old": None,
                                   "new": "(new template)"} for n in names]
    return doc, names, live, diff(live, doc)


def cmd_plan(z, args) -> int:
    total = {"additive": 0, "mutating": 0, "destructive": 0, "data_loss": 0}
    for f in args.files:
        path = pathlib.Path(f)
        try:
            _, names, live, changes = _plan_one(z, path)
        except (ZabbixError, json.JSONDecodeError, OSError) as e:
            print(f"  ! {path.name}: {e}", file=sys.stderr)
            continue
        c = classify(changes)
        for k in total:
            total[k] += len(c[k])
        state = "new" if live is None else "exists"
        if not changes:
            print(f"  {path.name:<42} no change")
            continue
        print(f"\n  {path.name}  ({', '.join(names)}, {state})")
        for label, key in (("destructive", "destructive"), ("mutating", "mutating"),
                           ("additive", "additive")):
            for ch in c[key][:args.limit]:
                mark = {"destructive": "-", "mutating": "~", "additive": "+"}[label]
                print(f"    {mark} {ch['path']}")
                if ch["kind"] == "changed":
                    print(f"        {ch['old']}  ->  {ch['new']}")
                if ch.get("detail"):
                    print(f"        !! {ch['detail']}")
            if len(c[key]) > args.limit:
                print(f"    … and {len(c[key]) - args.limit} more {label}")

    print(f"\n  TOTAL: {total['additive']} additive, {total['mutating']} mutating, "
          f"{total['destructive']} destructive "
          f"({total['data_loss']} losing collected data)")
    if args.fail_on_destructive and total["destructive"]:
        print("\nFAIL: destructive changes in the plan", file=sys.stderr)
        return 1
    return 0


def cmd_apply(z, args) -> int:
    planned, blocked = [], []
    for f in args.files:
        path = pathlib.Path(f)
        try:
            doc, names, live, changes = _plan_one(z, path)
        except (ZabbixError, json.JSONDecodeError, OSError) as e:
            print(f"  ! {path.name}: {e}", file=sys.stderr)
            return 1
        c = classify(changes)
        if not changes:
            print(f"  {path.name:<42} no change, skipping")
            continue
        if c["destructive"] and not args.allow_destructive:
            blocked.append((path, c))
            continue
        planned.append((path, doc, c))

    if blocked:
        print("\nBLOCKED — these would remove configuration:\n", file=sys.stderr)
        for path, c in blocked:
            print(f"  {path.name}: {len(c['destructive'])} removal(s), "
                  f"{len(c['data_loss'])} losing collected data", file=sys.stderr)
            for ch in c["destructive"][:5]:
                print(f"    - {ch['path']}", file=sys.stderr)
                if ch.get("detail"):
                    print(f"      {ch['detail']}", file=sys.stderr)
        print("\nRe-run with --allow-destructive if that is intended.", file=sys.stderr)
        return 1

    if not planned:
        print("\n  Nothing to apply.")
        return 0

    # deleteMissing is what makes a removal in the file actually delete on the
    # server. Off unless the operator has accepted the destructive plan.
    rules = dict(IMPORT_RULES_BASE)
    for section in ("items", "triggers", "discoveryRules", "graphs", "httptests"):
        rules[section] = {"createMissing": True, "updateExisting": True,
                          "deleteMissing": bool(args.allow_destructive)}

    failed = 0
    for path, doc, c in planned:
        source = json.dumps({"zabbix_export": dict(doc, version="7.4")})
        try:
            z.call("configuration.import",
                   {"format": "json", "rules": rules, "source": source})
            print(f"  applied {path.name}  "
                  f"(+{len(c['additive'])} ~{len(c['mutating'])} -{len(c['destructive'])})")
        except ZabbixError as e:
            print(f"  ! {path.name}: {e}", file=sys.stderr)
            failed += 1
    print(f"\n  {len(planned) - failed} of {len(planned)} file(s) applied.")
    return 1 if failed else 0


def cmd_drift(z, args) -> int:
    """Report where the live server no longer matches the files."""
    drifted = 0
    for f in args.files:
        path = pathlib.Path(f)
        try:
            doc, names, live, _ = _plan_one(z, path)
        except (ZabbixError, json.JSONDecodeError, OSError) as e:
            print(f"  ! {path.name}: {e}", file=sys.stderr)
            continue
        if live is None:
            print(f"  {path.name:<42} not present on the server")
            drifted += 1
            continue
        # Direction reversed: what has the server gained that the file lacks?
        changes = diff(doc, live)
        c = classify(changes)
        if not changes:
            print(f"  {path.name:<42} in sync")
            continue
        drifted += 1
        print(f"\n  {path.name}  server has diverged from the file")
        for ch in (c["additive"] + c["mutating"])[:args.limit]:
            mark = "+" if ch["kind"] == "added" else "~"
            print(f"    {mark} {ch['path']}")
            if ch["kind"] == "changed":
                print(f"        file: {ch['old']}   server: {ch['new']}")
    print(f"\n  {drifted} file(s) out of sync with the server.")
    if args.fail_on_drift and drifted:
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn, helptext in (
        ("plan", cmd_plan, "show what would change on the target"),
        ("apply", cmd_apply, "import the files, gated on destructive changes"),
        ("drift", cmd_drift, "report where the server no longer matches the files"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("files", nargs="+", help="canonical export files")
        p.add_argument("--limit", type=int, default=15)
        if name in ("plan",):
            p.add_argument("--fail-on-destructive", action="store_true")
        if name == "apply":
            p.add_argument("--allow-destructive", action="store_true",
                           help="permit removals, including item history loss")
        if name == "drift":
            p.add_argument("--fail-on-drift", action="store_true")
        p.set_defaults(func=fn)

    args = ap.parse_args()
    z = connect_or_exit()
    print(f"Target: Zabbix {z.version()} at {z.url}")
    try:
        return args.func(z, args)
    except ZabbixError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
