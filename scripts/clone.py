#!/usr/bin/env python3
"""Clone production Zabbix configuration into a test instance.

    ./scripts/clone.py --dry-run          # what would be copied, writes nothing
    ./scripts/clone.py                    # clone custom templates + media types
    ./scripts/clone.py --all-templates    # include the ~313 vendor templates too
    ./scripts/clone.py --include hosts    # also clone hosts (created DISABLED)
    ./scripts/clone.py --mirror           # delete objects in test absent from prod

Source is read from ``ZBX_URL`` / ``ZBX_TOKEN`` and is **never written to**.
Destination comes from ``ZBX_TEST_URL`` / ``ZBX_TEST_TOKEN``.

Why this is not just an export/import loop
------------------------------------------

Two things make a naive clone actively dangerous, and both are handled here:

**1. A cloned test instance will page real people.** Prod's actions and media
types come across with it, still pointing at your real Discord webhook, your
real SMTP relay, your real ntfy topic. The moment a trigger fires in test,
someone's phone buzzes at 3am about a problem that does not exist. So every
action and every media type is **disabled after import**, always, unless you
explicitly ask otherwise.

**2. A cloned host list makes test poll production.** Cloning 88 hosts means a
second Zabbix server starts hammering the same agents, doubling load and
producing a second, conflicting opinion about whether they are up. Hosts are
therefore opt-in, and when cloned they are created **disabled**.

Guard rails
-----------

The destination must identify itself as non-production by carrying a global
macro ``{$ENV}`` set to one of test/dev/staging/lab. That is a deliberate
speed bump: a mistyped URL cannot silently overwrite production, because
production will not be carrying that macro.

Set it once on the test instance:

    Administration -> Macros -> {$ENV} = test
"""
from __future__ import annotations

import argparse
import contextlib
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from zbx import Zabbix, ZabbixError, connect_or_exit  # noqa: E402

#: Values of {$ENV} that mark an instance as safe to write to.
SAFE_ENVS = {"test", "dev", "staging", "lab", "sandbox"}

#: Export sections, in dependency order. Groups must exist before the templates
#: and hosts that reference them.
SECTIONS = ["template_groups", "host_groups", "templates", "images", "maps"]


def connect_target_or_exit() -> Zabbix:
    url = os.environ.get("ZBX_TEST_URL", "").strip()
    if not url:
        print("error: ZBX_TEST_URL is not set — refusing to guess a destination.",
              file=sys.stderr)
        sys.exit(2)
    try:
        return Zabbix(
            url=url,
            user=os.environ.get("ZBX_TEST_USER", "").strip(),
            password=os.environ.get("ZBX_TEST_PASS", "").strip(),
            token=os.environ.get("ZBX_TEST_TOKEN", "").strip(),
        )
    except ZabbixError as e:
        print(f"error connecting to test instance: {e}", file=sys.stderr)
        sys.exit(2)


def env_marker(z: Zabbix) -> str:
    """Read the {$ENV} global macro. Empty string when unset."""
    for m in z.call("usermacro.get", {"globalmacro": True, "output": ["macro", "value"]}):
        if m["macro"] == "{$ENV}":
            return (m["value"] or "").strip().lower()
    return ""


def check_safety(src: Zabbix, dst: Zabbix, force: bool) -> None:
    """Everything that must hold before a single write happens."""
    problems = []

    if src.url.rstrip("/") == dst.url.rstrip("/"):
        # No override for this one. There is no legitimate reason to clone an
        # instance onto itself, and --force must not be able to cause it.
        sys.stdout.flush()
        print("error: source and destination are the same instance. Refusing.",
              file=sys.stderr)
        sys.exit(2)

    marker = env_marker(dst)
    if marker not in SAFE_ENVS:
        problems.append(
            f"destination {{$ENV}} is {marker or 'unset'!r}, not one of "
            f"{'/'.join(sorted(SAFE_ENVS))}. Set it on the test instance: "
            "Administration -> Macros -> {$ENV} = test"
        )

    with contextlib.suppress(ZabbixError):
        src_hosts = src.count("host.get")
        dst_hosts = dst.count("host.get")
        if dst_hosts > src_hosts:
            problems.append(
                f"destination has more hosts ({dst_hosts}) than source ({src_hosts}) — "
                "this looks like the source and destination are swapped"
            )

    if not problems:
        return

    sys.stdout.flush()
    print("\nRefusing to write to the destination:\n", file=sys.stderr)
    for p in problems:
        print(f"  * {p}", file=sys.stderr)
    if not force:
        print("\nFix the above, or re-run with --force if you are certain.",
              file=sys.stderr)
        sys.exit(2)
    print("\n!! --force given: proceeding anyway. This had better not be production.\n",
          file=sys.stderr)


def select_templates(src: Zabbix, all_templates: bool) -> list[dict]:
    """Custom templates by default.

    Vendor templates ship with Zabbix, so the test instance already has its own
    copies. Cloning all ~320 is slow and buys nothing; what you actually want in
    test is the handful you wrote.
    """
    templates = src.call("template.get", {"output": ["templateid", "host", "vendor_name"]})
    if all_templates:
        return templates
    return [t for t in templates if not t.get("vendor_name")]


def export_section(src: Zabbix, section: str, ids: list[str]) -> str | None:
    if not ids:
        return None
    return src.call("configuration.export", {
        "format": "yaml",
        "options": {section: ids},
    })


def gather(src: Zabbix, args) -> dict[str, str]:
    """Export each section from the source. Returns section -> YAML."""
    out: dict[str, str] = {}

    templates = select_templates(src, args.all_templates)
    tpl_ids = [t["templateid"] for t in templates]
    print(f"  templates        {len(tpl_ids):>4}"
          f"{'' if args.all_templates else '  (custom only; --all-templates for vendor)'}")

    tg = src.call("templategroup.get", {"output": ["groupid", "name"]})
    hg = src.call("hostgroup.get", {"output": ["groupid", "name"]})
    mt = src.call("mediatype.get", {"output": ["mediatypeid", "name", "type"]})
    print(f"  template groups  {len(tg):>4}")
    print(f"  host groups      {len(hg):>4}")
    print(f"  media types      {len(mt):>4}"
          f"  ({sum(1 for m in mt if m['type'] == '4')} webhooks)")

    sections = {
        "template_groups": [g["groupid"] for g in tg],
        "host_groups": [g["groupid"] for g in hg],
        "templates": tpl_ids,
    }
    # Media types are exported one at a time, not as a batch — see
    # clone_media_types() for why a batch import cannot be used here.
    out["_media_ids"] = [(m["mediatypeid"], m["name"]) for m in mt]

    if "hosts" in args.include:
        hosts = src.call("host.get", {"output": ["hostid", "host"]})
        sections["hosts"] = [h["hostid"] for h in hosts]
        print(f"  hosts            {len(hosts):>4}  (will be created DISABLED)")

    for section in SECTIONS:
        ids = sections.get(section)
        if not ids:
            continue
        try:
            doc = export_section(src, section, ids)
        except ZabbixError as e:
            print(f"    ! export of {section} failed: {e}", file=sys.stderr)
            continue
        if doc:
            out[section] = doc
    return out


def import_section(dst: Zabbix, section: str, doc: str, mirror: bool, dry_run: bool) -> bool:
    """Import one exported document. Returns True on success."""
    flag = {"createMissing": True, "updateExisting": True}
    prune = dict(flag, deleteMissing=mirror)
    rules = {
        "template_groups": flag,
        "host_groups": flag,
        "templates": flag,
        "hosts": flag,
        "items": prune,
        "triggers": prune,
        "discoveryRules": prune,
        "graphs": prune,
        "httptests": prune,
        "valueMaps": flag,
        "mediaTypes": flag,
        "images": flag,
        "maps": flag,
    }
    method = "configuration.importcompare" if dry_run else "configuration.import"
    try:
        result = dst.call(method, {"format": "yaml", "rules": rules, "source": doc})
    except ZabbixError as e:
        print(f"    ! {section}: {e}", file=sys.stderr)
        return False
    if dry_run:
        print(f"    {section:<18} {'changes pending' if result else 'no changes'}")
    else:
        print(f"    {section:<18} imported")
    return True


def clone_media_types(src: Zabbix, dst: Zabbix, media: list[tuple[str, str]],
                      dry_run: bool) -> tuple[int, list[str]]:
    """Copy media types one at a time.

    They cannot be imported as a batch. Zabbix strips credentials from a
    configuration export — deliberately, and correctly — but leaves the field
    present and empty. An SMTP media type with authentication configured
    therefore exports with ``username: ""``, which the importer then rejects as
    "cannot be empty". One such media type fails the entire batch, taking the
    other 42 with it.

    Importing individually means a media type that cannot cross is reported by
    name and skipped, rather than silently costing you all of them. Anything
    skipped needs its credentials re-entered in test by hand, which is the right
    outcome: those are production credentials and should not be cloned anyway.
    """
    ok, failed = 0, []
    rules = {"mediaTypes": {"createMissing": True, "updateExisting": True}}
    method = "configuration.importcompare" if dry_run else "configuration.import"
    for mid, name in media:
        try:
            doc = src.call("configuration.export",
                           {"format": "yaml", "options": {"mediaTypes": [mid]}})
            dst.call(method, {"format": "yaml", "rules": rules, "source": doc})
            ok += 1
        except ZabbixError as e:
            reason = "credentials stripped by export" if "cannot be empty" in str(e) else str(e)[:70]
            failed.append(f"{name} ({reason})")
    return ok, failed


def quiesce(dst: Zabbix, keep_notifications: bool) -> None:
    """Disable everything in the destination that could contact the outside world.

    This is the whole reason a clone is safe. A freshly-cloned test instance has
    production's actions and media types, still pointing at production's Discord
    webhook and SMTP relay. Left enabled, the first trigger that fires in test
    pages a real person about a problem that does not exist.
    """
    if keep_notifications:
        print("\n  !! --keep-notifications: actions and media types left ENABLED.")
        print("     Test will send real notifications to real endpoints.")
        return

    print("\n  Disabling outbound notification paths in the destination:")

    actions = dst.call("action.get", {"output": ["actionid", "name", "status"]})
    live = [a for a in actions if a["status"] == "0"]
    if live:
        # action.update takes one object or a list; chunk so a single bad action
        # cannot lose the whole batch.
        for i in range(0, len(live), 50):
            chunk = live[i:i + 50]
            dst.call("action.update", [{"actionid": a["actionid"], "status": "1"} for a in chunk])
    print(f"    actions      {len(live):>3} disabled ({len(actions)} total)")

    media = dst.call("mediatype.get", {"output": ["mediatypeid", "name", "status"]})
    live_m = [m for m in media if m["status"] == "0"]
    if live_m:
        for i in range(0, len(live_m), 50):
            chunk = live_m[i:i + 50]
            dst.call("mediatype.update",
                     [{"mediatypeid": m["mediatypeid"], "status": "1"} for m in chunk])
    print(f"    media types  {len(live_m):>3} disabled ({len(media)} total)")


def disable_cloned_hosts(dst: Zabbix) -> None:
    """Every host in the destination goes to 'not monitored'.

    Otherwise the test server begins polling the same agents production polls —
    doubling load on every guest and producing a second, conflicting opinion
    about whether each one is up.
    """
    hosts = dst.call("host.get", {"output": ["hostid", "status"]})
    live = [h for h in hosts if h["status"] == "0"]
    if live:
        for i in range(0, len(live), 100):
            chunk = live[i:i + 100]
            dst.call("host.update", [{"hostid": h["hostid"], "status": "1"} for h in chunk])
    print(f"    hosts        {len(live):>3} set to 'not monitored'")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change in the destination, write nothing")
    ap.add_argument("--all-templates", action="store_true",
                    help="include vendor templates (default: custom templates only)")
    ap.add_argument("--include", nargs="*", default=[], choices=["hosts"],
                    help="extra sections to clone; hosts are created disabled")
    ap.add_argument("--mirror", action="store_true",
                    help="DESTRUCTIVE: delete items/triggers in test that prod no longer has")
    ap.add_argument("--keep-notifications", action="store_true",
                    help="DANGEROUS: leave actions and media types enabled in the destination")
    ap.add_argument("--force", action="store_true",
                    help="override the {$ENV} safety check")
    args = ap.parse_args()

    src = connect_or_exit()
    dst = connect_target_or_exit()

    print(f"source      {src.url}")
    print(f"destination {dst.url}")
    with contextlib.suppress(ZabbixError):
        print(f"versions    src {src.version()} -> dst {dst.version()}")

    check_safety(src, dst, args.force)

    print("\nExporting from source (read-only):")
    docs = gather(src, args)
    if not docs:
        print("Nothing to clone.", file=sys.stderr)
        return 1

    print(f"\n{'Comparing against' if args.dry_run else 'Importing into'} destination:")
    failures = 0
    for section in SECTIONS:
        if section in docs and not import_section(dst, section, docs[section],
                                                  args.mirror, args.dry_run):
            failures += 1

    media = docs.pop("_media_ids", [])
    if media:
        ok, failed = clone_media_types(src, dst, media, args.dry_run)
        print(f"    {'media types':<18} {ok}/{len(media)} "
              f"{'comparable' if args.dry_run else 'imported'}")
        for f in failed:
            print(f"      skipped: {f}")
        if failed:
            print("      (re-enter those credentials in test by hand — they are "
                  "production secrets\n       and should not be cloned)")

    if args.dry_run:
        print("\nDry run — the destination was not modified.")
        print("Note: a real run also disables all actions and media types in the "
              "destination.")
        return 1 if failures else 0

    quiesce(dst, args.keep_notifications)
    if "hosts" in args.include:
        disable_cloned_hosts(dst)

    print("\nClone complete.")
    print("  Actions and media types are DISABLED in test. Enable individual ones "
          "deliberately,\n  after pointing them somewhere that is not a production endpoint.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
