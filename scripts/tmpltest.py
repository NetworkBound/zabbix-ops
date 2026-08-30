#!/usr/bin/env python3
"""Test a template before it reaches production.

    ./scripts/tmpltest.py run templates/*.json
    ./scripts/tmpltest.py run templates/linux.json --settle 120
    ./scripts/tmpltest.py run templates/linux.json --against 10.0.0.55
    ./scripts/tmpltest.py run templates/linux.json --keep     # leave the host

There is no established way to test a Zabbix template. The available aids are
the frontend's item and trigger test buttons, calculated items mirroring a
trigger expression against real history, and zabbix_sender driving a dummy
trigger. All manual, all after the fact.

This does what a test framework would: import the template into a disposable
instance, attach it to a throwaway host, and then assert on the result rather
than looking at it.

What it checks
--------------

1. **The template imports.** Zabbix validates references on import, so this
   alone catches trigger expressions naming items that do not exist, malformed
   keys, and broken preprocessing.
2. **Items are created.** A template that imports but produces nothing has a
   discovery-only structure or an empty item set.
3. **Nothing goes unsupported.** After a settle period, any item in the
   unsupported state is reported with its error. This is the check that catches
   a wrong OID, a bad key parameter, or a macro that never resolves.
4. **Triggers resolve.** Every trigger's expression must reference items that
   exist on the host.
5. **Discovery rules have prototypes.** A rule with no prototypes discovers
   nothing, which is a common result of a bad copy.

The host is deleted afterwards unless you keep it, along with everything the
template created on it.

Safety
------

Refuses to run against an instance that is not marked as non-production with
the ``{$ENV}`` global macro, for the same reason clone.py does: this creates and
deletes hosts, and doing that on a production server by mistyping a URL would be
unpleasant.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from canon import normalise_document  # noqa: E402
from zbx import ZabbixError, connect_or_exit  # noqa: E402

SAFE_ENVS = {"test", "dev", "staging", "lab", "sandbox"}
SCRATCH_GROUP = "zbxops-template-tests"
HOST_PREFIX = "zbxops-test-"

ITEM_STATE = {"0": "normal", "1": "unsupported"}


def _require_non_production(z, force: bool) -> None:
    marker = ""
    for m in z.call("usermacro.get", {"globalmacro": True, "output": ["macro", "value"]}):
        if m["macro"] == "{$ENV}":
            marker = (m["value"] or "").strip().lower()
    if marker in SAFE_ENVS:
        return
    sys.stdout.flush()
    print(f"\nerror: target {{$ENV}} is {marker or 'unset'!r}, not one of "
          f"{'/'.join(sorted(SAFE_ENVS))}.", file=sys.stderr)
    print("       This creates and deletes hosts. Point it at a test instance, "
          "or pass --force\n       if you are certain.", file=sys.stderr)
    if not force:
        sys.exit(2)
    print("!! --force given: proceeding against an unmarked instance.\n", file=sys.stderr)


def _scratch_group(z) -> str:
    found = z.call("hostgroup.get", {"output": ["groupid"],
                                     "filter": {"name": SCRATCH_GROUP}})
    if found:
        return found[0]["groupid"]
    return z.call("hostgroup.create", {"name": SCRATCH_GROUP})["groupids"][0]


class Result:
    def __init__(self, template: str):
        self.template = template
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, passed, detail))

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [c for c in self.checks if not c[1]]

    @property
    def ok(self) -> bool:
        return not self.failed


def test_template(z, path: pathlib.Path, args, groupid: str) -> Result:
    doc = normalise_document(path.read_text())
    names = [t["template"] for t in doc.get("templates", []) if t.get("template")]
    r = Result(", ".join(names) or path.name)
    if not names:
        r.check("file contains a template", False, "no templates section")
        return r

    # 1. Import. Zabbix validates references here, so a failure is a real
    #    finding rather than a setup problem.
    rules = {
        "template_groups": {"createMissing": True, "updateExisting": True},
        "host_groups": {"createMissing": True},
        "templates": {"createMissing": True, "updateExisting": True},
        "items": {"createMissing": True, "updateExisting": True},
        "triggers": {"createMissing": True, "updateExisting": True},
        "discoveryRules": {"createMissing": True, "updateExisting": True},
        "graphs": {"createMissing": True, "updateExisting": True},
        "valueMaps": {"createMissing": True, "updateExisting": True},
    }
    try:
        z.call("configuration.import", {"format": "json", "rules": rules,
                                        "source": json.dumps({"zabbix_export":
                                                              dict(doc, version="7.4")})})
        r.check("template imports", True)
    except ZabbixError as e:
        r.check("template imports", False, str(e)[:160])
        return r

    tmpl = z.call("template.get", {"output": ["templateid", "host"],
                                   "filter": {"host": names}})
    if not tmpl:
        r.check("template present after import", False, "not found")
        return r
    tids = [t["templateid"] for t in tmpl]

    # 5. Discovery rules should have prototypes; a rule without them discovers
    #    nothing and usually means a partial copy.
    lld = z.call("discoveryrule.get", {"templateids": tids, "output": ["itemid", "name"]})
    if lld:
        empty = []
        for rule in lld:
            protos = z.call("itemprototype.get", {"discoveryids": rule["itemid"],
                                                  "output": ["itemid"], "limit": 1})
            if not protos:
                empty.append(rule["name"])
        r.check(f"discovery rules have prototypes ({len(lld)} rule(s))",
                not empty, "; ".join(empty[:4]))

    # 2/3/4. Attach to a throwaway host and see what actually happens.
    hostname = HOST_PREFIX + names[0].lower().replace(" ", "-")[:40]
    for stale in z.call("host.get", {"output": ["hostid"], "filter": {"host": hostname}}):
        z.call("host.delete", [stale["hostid"]])

    # A template can only link to a host that has the interface types its items
    # need. Linking an SNMP template to an agent-only host fails with "cannot
    # inherit item ... because the host has no interface" -- which reads like a
    # broken template but is a missing interface on the test host.
    ITEM_TYPE_TO_IFACE = {"0": 1, "7": 1, "3": 1,      # agent / active / simple
                          "20": 2,                       # SNMP
                          "12": 3,                       # IPMI
                          "16": 4}                       # JMX
    IFACE_PORT = {1: "10050", 2: "161", 3: "623", 4: "12345"}
    tmpl_items = z.call("item.get", {"templateids": tids, "output": ["type"]})
    needed = {ITEM_TYPE_TO_IFACE[i["type"]]
              for i in tmpl_items if i.get("type") in ITEM_TYPE_TO_IFACE}
    needed = needed or {1}
    addr = args.against or "127.0.0.1"
    iface = []
    for n, itype in enumerate(sorted(needed)):
        entry = {"type": itype, "main": 1, "useip": 1, "ip": addr, "dns": "",
                 "port": IFACE_PORT[itype], "interface_ref": f"if{n + 1}"}
        if itype == 2:
            # SNMP interfaces require a details block or creation is rejected.
            entry["details"] = {"version": 2, "community": "{$SNMP_COMMUNITY}"}
        iface.append(entry)
    try:
        hostid = z.call("host.create", {
            "host": hostname, "groups": [{"groupid": groupid}],
            "templates": [{"templateid": t} for t in tids],
            "interfaces": iface, "status": 0 if args.against else 1,
        })["hostids"][0]
    except ZabbixError as e:
        r.check("template links to a host", False, str(e)[:160])
        return r
    r.check(f"template links to a host ({len(iface)} interface type(s))", True)

    try:
        items = z.call("item.get", {"hostids": hostid,
                                    "output": ["itemid", "key_", "state", "error"]})
        r.check(f"items created ({len(items)})", bool(items),
                "template produced no items")

        triggers = z.call("trigger.get", {"hostids": hostid,
                                          "output": ["triggerid", "description"],
                                          "selectFunctions": "extend"})
        broken = [t["description"] for t in triggers if not t.get("functions")]
        r.check(f"trigger expressions resolve ({len(triggers)})",
                not broken, "; ".join(broken[:4]))

        # Only meaningful when pointed at something real; a disabled host never
        # polls, so every item would sit in the unknown state forever.
        if args.against:
            print(f"    settling {args.settle}s against {args.against} …")
            time.sleep(args.settle)
            items = z.call("item.get", {"hostids": hostid,
                                        "output": ["itemid", "key_", "state", "error"]})
            unsupported = [i for i in items if i.get("state") == "1"]
            r.check(f"no unsupported items ({len(items)} checked)",
                    not unsupported,
                    "; ".join(f"{i['key_']}: {(i.get('error') or '')[:60]}"
                              for i in unsupported[:5]))
            collected = [i for i in items if i.get("state") == "0"]
            r.check("at least one item collected a value", bool(collected))
    finally:
        if not args.keep:
            try:
                z.call("host.delete", [hostid])
            except ZabbixError as e:
                print(f"    ! could not remove {hostname}: {e}", file=sys.stderr)
        else:
            print(f"    kept host {hostname}")
    return r


def cmd_run(z, args) -> int:
    _require_non_production(z, args.force)
    groupid = _scratch_group(z)

    results = []
    for f in args.files:
        path = pathlib.Path(f)
        print(f"\n{path.name}")
        try:
            r = test_template(z, path, args, groupid)
        except (ZabbixError, json.JSONDecodeError, OSError) as e:
            r = Result(path.name)
            r.check("test ran", False, str(e)[:160])
        results.append(r)
        for name, passed, detail in r.checks:
            print(f"  {'PASS' if passed else 'FAIL'}  {name}")
            if detail and not passed:
                print(f"        {detail}")

    failed = [r for r in results if not r.ok]
    total_checks = sum(len(r.checks) for r in results)
    print(f"\n{'─' * 66}")
    print(f"{len(results)} template(s), {total_checks} check(s), "
          f"{len(failed)} template(s) with failures")
    if not args.against:
        print("\nNote: run with --against <ip> to additionally verify that items "
              "collect\nand none go unsupported. Without a real target that "
              "cannot be checked.")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="test template files against a disposable instance")
    p.add_argument("files", nargs="+")
    p.add_argument("--against", metavar="IP",
                   help="a real address to poll, so unsupported items can be detected")
    p.add_argument("--settle", type=int, default=90,
                   help="seconds to wait for values before checking state")
    p.add_argument("--keep", action="store_true", help="leave the test host behind")
    p.add_argument("--force", action="store_true",
                   help="run against an instance not marked non-production")
    p.set_defaults(func=cmd_run)

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
