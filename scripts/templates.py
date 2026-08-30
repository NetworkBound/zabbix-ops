#!/usr/bin/env python3
"""Put your Zabbix templates under version control, and put them back.

    ./scripts/templates.py export                 # Zabbix -> templates/*.yaml
    ./scripts/templates.py export --prefix Linux  # only templates matching a name
    ./scripts/templates.py import                 # templates/*.yaml -> Zabbix
    ./scripts/templates.py import --dry-run       # report changes, write nothing
    ./scripts/templates.py import templates/my-template.yaml

Template definitions are configuration, and configuration that only exists
inside a database is configuration you cannot diff, review, or roll back. This
exports yours to YAML so `git diff` shows exactly which item interval someone
changed, and imports them back to promote a change or rebuild a server.

No templates ship with this repo — the output directory is gitignored by
default so nobody accidentally publishes their own estate's configuration.
Commit yours deliberately, once you have checked what is in them.

Import is idempotent: templates match by UUID, so re-importing updates in place
rather than creating duplicates. Items and triggers that are no longer in the
file are *not* deleted by default — ``--prune`` allows that, and is off
deliberately because ``deleteMissing`` will happily remove anything added
through the UI.

> Exports contain no macro *values*. A secret macro such as a Proxmox API token
> is stored on the host object and never appears in a template export — but
> check your own diff before committing regardless.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from zbx import ZabbixError, connect_or_exit  # noqa: E402

TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"
SEARCH_PREFIX = os.environ.get("ZBX_TEMPLATE_PREFIX", "Homelab")


def do_export(z, args) -> int:
    TEMPLATE_DIR.mkdir(exist_ok=True)
    tmpls = z.call("template.get", {
        "output": ["templateid", "host"],
        "search": {"host": args.prefix},
    })
    if not tmpls:
        print(f"No templates matching {args.prefix!r} found.", file=sys.stderr)
        return 1
    for t in sorted(tmpls, key=lambda x: x["host"]):
        yaml_doc = z.call("configuration.export", {
            "format": "yaml",
            "options": {"templates": [t["templateid"]]},
        })
        slug = t["host"].lower().replace(" ", "-").replace("/", "-")
        path = TEMPLATE_DIR / f"{slug}.yaml"
        path.write_text(yaml_doc)
        n_items = z.count("item.get", {"templateids": t["templateid"]})
        n_trig = z.count("trigger.get", {"templateids": t["templateid"]})
        print(f"  {t['host']:<28} {n_items:>4} items {n_trig:>3} triggers -> {path.name}")
    print(f"\nExported {len(tmpls)} template(s) to {TEMPLATE_DIR}/")
    return 0


def do_import(z, args) -> int:
    paths = [pathlib.Path(p) for p in args.files] if args.files \
        else sorted(TEMPLATE_DIR.glob("*.yaml"))
    if not paths:
        print(f"No YAML files found in {TEMPLATE_DIR}/", file=sys.stderr)
        return 1

    # createMissing/updateExisting are what make this idempotent. deleteMissing
    # is destructive (it removes items/triggers absent from the file), so it is
    # opt-in via --prune.
    rules = {
        "template_groups": {"createMissing": True, "updateExisting": True},
        "host_groups": {"createMissing": True},
        "templates": {"createMissing": True, "updateExisting": True},
        "items": {"createMissing": True, "updateExisting": True, "deleteMissing": args.prune},
        "triggers": {"createMissing": True, "updateExisting": True, "deleteMissing": args.prune},
        "discoveryRules": {"createMissing": True, "updateExisting": True, "deleteMissing": args.prune},
        "graphs": {"createMissing": True, "updateExisting": True, "deleteMissing": args.prune},
        "valueMaps": {"createMissing": True, "updateExisting": True},
    }

    method = "configuration.importcompare" if args.dry_run else "configuration.import"
    failures = 0
    for p in paths:
        try:
            result = z.call(method, {
                "format": "yaml",
                "rules": rules,
                "source": p.read_text(),
            })
        except ZabbixError as e:
            print(f"  FAILED  {p.name}: {e}", file=sys.stderr)
            failures += 1
            continue
        if args.dry_run:
            changed = "changes pending" if result else "no changes"
            print(f"  {p.name:<36} {changed}")
        else:
            print(f"  imported {p.name}")

    if args.dry_run:
        print("\nDry run — nothing was written.")
    elif failures:
        print(f"\n{len(paths) - failures} imported, {failures} failed.", file=sys.stderr)
    else:
        print(f"\nImported {len(paths)} template(s).")
        if not args.prune:
            print("Note: items/triggers removed from the files were kept. "
                  "Re-run with --prune to delete them.")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="write Zabbix templates to templates/*.yaml")
    e.add_argument("--prefix", default=SEARCH_PREFIX,
                   help=f"template name substring to export (default: {SEARCH_PREFIX})")
    e.set_defaults(func=do_export)

    i = sub.add_parser("import", help="load templates/*.yaml into Zabbix")
    i.add_argument("files", nargs="*", help="specific YAML files (default: all)")
    i.add_argument("--dry-run", action="store_true",
                   help="report what would change without writing")
    i.add_argument("--prune", action="store_true",
                   help="DESTRUCTIVE: delete items/triggers absent from the files")
    i.set_defaults(func=do_import)

    args = ap.parse_args()
    z = connect_or_exit()
    try:
        return args.func(z, args)
    except ZabbixError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
