#!/usr/bin/env python3
"""Prove that Zabbix notifications actually reach somewhere.

    ./scripts/notify.py history --days 7                   # what the alert log says
    ./scripts/notify.py users                              # who can be reached right now
    ./scripts/notify.py verify                             # dry run
    ./scripts/notify.py verify --apply                     # sends one real message per media type
    ./scripts/notify.py test --media Email --to alerts@example.com --apply

An installation can be configured perfectly and still notify nobody. The action
is enabled, the user has media, the media type is enabled — and every send fails
because a webhook was revoked at the far end, an SMTP relay started requiring
authentication, or the host a script posts to moved. Nothing in the frontend
surfaces the difference. On the server this was written against, 9,984 of 9,997
attempts over seven days had failed and nobody knew.

``history`` and ``users`` are read-only. ``test`` and ``verify`` send real
messages to real people, so both are a dry run until ``--apply``.

``verify`` exits non-zero when any enabled media type fails, so a scheduled job
can gate on it. A media type with no evidence either way is reported as
unverified and does not fail the run — claiming a path is broken when nothing
has been tried through it would make the job untrustworthy.
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

# Only the fields that are needed. mediatype.get with output=extend also returns
# passwd, client_secret and access_token, which would then be one careless print
# away from a log file.
MEDIATYPE_FIELDS = ["mediatypeid", "name", "type", "status",
                    "maxattempts", "attempt_interval"]

MEDIA_KIND = {"0": "email", "1": "script", "2": "sms", "4": "webhook"}

# alert.status for a message alert. 0 and 3 both mean the server has not
# finished with it, which is why a read-back has to wait before it judges.
SENT, FAILED = "1", "2"

STATE_LABEL = {
    "healthy": "delivering",
    "flaky": "intermittent",
    "broken": "never delivered",
    "pending": "queued, no verdict yet",
    "idle": "no attempts",
}

TEST_SUBJECT = "Zabbix notification path test"


# --------------------------------------------------------------------------
# Pure logic. None of this touches the network, so it can be tested.
# --------------------------------------------------------------------------
def summarise(alerts, names=None) -> dict:
    """Per media type: attempts, outcomes, distinct errors, last of each.

    Keyed by media type id as a string. Alerts whose mediatypeid is 0 are kept
    under "0": those never reached a media type at all, which is a different
    failure from a media type that tried and could not deliver.
    """
    names = names or {}
    out: dict[str, dict] = {}
    for a in alerts:
        mtid = str(a.get("mediatypeid") or "0")
        st = out.setdefault(mtid, {
            "mediatypeid": mtid, "name": names.get(mtid, f"mediatypeid {mtid}"),
            "attempts": 0, "delivered": 0, "failed": 0, "pending": 0,
            "errors": Counter(), "last_success": None, "last_failure": None,
        })
        clock = int(a.get("clock") or 0)
        st["attempts"] += 1
        status = str(a.get("status"))
        if status == SENT:
            st["delivered"] += 1
            if st["last_success"] is None or clock > st["last_success"]:
                st["last_success"] = clock
        elif status == FAILED:
            st["failed"] += 1
            st["errors"][(a.get("error") or "(no error text recorded)").strip()] += 1
            if st["last_failure"] is None or clock > st["last_failure"]:
                st["last_failure"] = clock
        else:
            st["pending"] += 1
    return out


def classify(stats) -> str:
    """Sort a media type into broken, flaky, or working.

    The distinction that matters is never-succeeded versus sometimes-fails. A
    media type that has delivered once is a reliability problem; one that has
    never delivered is switched off in every sense except the frontend's.
    """
    if not stats["attempts"]:
        return "idle"
    if stats["delivered"]:
        return "flaky" if stats["failed"] else "healthy"
    if stats["failed"]:
        return "broken"
    return "pending"


def group_errors(errors) -> list:
    """Collapse error strings that differ only in a number, commonest first.

    A connection failure carries the elapsed time in its text, so a thousand
    instances of one dead endpoint arrive as a dozen distinct strings that each
    look rare. Grouping on the shape and showing one real example keeps the
    count meaning what a reader assumes it means.
    """
    groups: dict[str, dict] = {}
    for text, count in errors.items():
        # Webhook errors arrive with an embedded stack trace across several
        # lines, which would break every table this appears in.
        flat = " ".join(str(text).split())
        shape = re.sub(r"\d+", "#", flat)
        g = groups.setdefault(shape, {"text": flat, "count": 0})
        g["count"] += count
    return sorted(((g["text"], g["count"]) for g in groups.values()),
                  key=lambda pair: -pair[1])


def _minutes(hhmm: str) -> int:
    hours, _, mins = hhmm.strip().partition(":")
    return int(hours) * 60 + int(mins or 0)


def period_covers(period: str, when=None) -> bool:
    """Whether a user media time period includes this moment.

    A medium outside its period is enabled and correct and still delivers
    nothing right now, which is the answer someone asking "can I reach this
    person" actually wants.
    """
    if not period:
        return True
    when = when or time.localtime()
    day = when.tm_wday + 1
    minute = when.tm_hour * 60 + when.tm_min
    for entry in period.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        days, _, hours = entry.partition(",")
        try:
            first, _, last = days.partition("-")
            first_day, last_day = int(first), int(last or first)
            start_h, _, end_h = hours.partition("-")
            start, end = _minutes(start_h), _minutes(end_h)
        except ValueError:
            # An unrecognised period is a gap in this parser, not proof that
            # nobody can be reached. Never report unreachable on our own bug.
            return True
        if first_day <= day <= last_day and start <= minute < end:
            return True
    return False


def mask_sendto(value: str) -> str:
    """Hide the credential in a webhook target.

    A Discord or Slack media entry stores the whole webhook URL, token
    included, in sendto. Printing it turns a monitoring report into a leak.
    """
    value = value or ""
    if "://" not in value:
        return value
    scheme, _, rest = value.partition("://")
    host, _, tail = rest.partition("/")
    return f"{scheme}://{host}/…" if tail else f"{scheme}://{host}"


def medium_status(medium, media_type, when=None) -> dict:
    """Whether one user media entry can deliver at this moment, and why not."""
    name = (media_type or {}).get("name", f"mediatypeid {medium.get('mediatypeid')}")
    result = {
        "mediatypeid": str(medium.get("mediatypeid")),
        "name": name,
        "sendto": mask_sendto(_sendto_text(medium.get("sendto"))),
        "usable": False,
        "reason": "",
    }
    if media_type is None:
        result["reason"] = "media type no longer exists"
    elif str(media_type.get("status")) != "0":
        result["reason"] = "media type disabled"
    elif str(medium.get("active")) != "0":
        result["reason"] = "medium disabled on the user"
    elif int(medium.get("severity") or 0) == 0:
        result["reason"] = "no severities selected, so nothing ever matches"
    elif not period_covers(medium.get("period") or "", when):
        result["reason"] = f"outside its time period ({medium.get('period')})"
    else:
        result["usable"] = True
    return result


def _sendto_text(sendto) -> str:
    # Email media accept several addresses and the API returns a list for them.
    if isinstance(sendto, list):
        return ", ".join(str(s) for s in sendto)
    return str(sendto or "")


def user_status(user, media_types, when=None) -> dict:
    """Can this user be reached right now, and if not, what is in the way."""
    groups = user.get("usrgrps") or []
    disabled = any(str(g.get("users_status")) == "1" for g in groups)
    media = [medium_status(m, media_types.get(str(m.get("mediatypeid"))), when)
             for m in (user.get("medias") or [])]
    usable = [m for m in media if m["usable"]]
    if disabled:
        # A disabled user is not notified regardless of how healthy the media
        # entry looks, so the media verdicts below it are decoration.
        reason = "user is disabled (member of a disabled user group)"
    elif not media:
        reason = "no media configured"
    elif not usable:
        reason = "every medium is unusable"
    else:
        reason = ""
    return {
        "userid": str(user.get("userid")),
        "username": user.get("username", ""),
        "disabled": disabled,
        "media": media,
        "reachable": bool(usable) and not disabled,
        "reason": reason,
    }


def targeted_userids(actions, group_members) -> set:
    """Users an enabled action would try to message.

    Operation type 0 is the only one that names recipients. The "notify all
    involved" operations resolve at event time and have no target list to read.
    """
    out = set()
    for action in actions:
        if str(action.get("status")) != "0":
            continue
        for key in ("operations", "recovery_operations", "update_operations"):
            for op in action.get(key) or []:
                if str(op.get("operationtype")) != "0":
                    continue
                for u in op.get("opmessage_usr") or []:
                    out.add(str(u["userid"]))
                for g in op.get("opmessage_grp") or []:
                    out.update(group_members.get(str(g["usrgrpid"]), []))
    return out


# --------------------------------------------------------------------------
# Server access
# --------------------------------------------------------------------------
def _stamp(clock) -> str:
    if not clock:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(clock))


def _media_types(z) -> dict:
    return {m["mediatypeid"]: m
            for m in z.call("mediatype.get", {"output": MEDIATYPE_FIELDS})}


def _alerts(z, days: int) -> list:
    since = int(time.time()) - days * 86400
    # Message body and sendto are deliberately not requested: the report never
    # needs them and both carry recipient data.
    return z.call("alert.get", {
        "output": ["clock", "status", "error", "mediatypeid", "userid", "alerttype"],
        "time_from": since, "limit": 200000,
    })


def test_method_available(z) -> bool:
    """Whether this server exposes mediatype.test over the API.

    Sending an empty params object is a safe probe: a server that has the
    method rejects the parameters, a server that does not have it says so.
    Neither delivers a message.
    """
    try:
        z.call("mediatype.test", {})
    except ZabbixError as e:
        return "method not found" not in str(e).lower()
    return True


def _test_message(now=None) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now or time.time()))
    return (f"Notification path test sent by notify.py at {stamp}. "
            "Receiving this confirms the media type can reach you. No action is needed.")


def _send_test(z, media_type, sendto: str) -> dict:
    """Run one mediatype.test and normalise the several shapes it comes back in."""
    params = {"mediatypeid": media_type["mediatypeid"], "sendto": sendto,
              "subject": TEST_SUBJECT, "message": _test_message()}
    try:
        raw = z.call("mediatype.test", params)
    except ZabbixError as e:
        return {"ok": False, "detail": str(e)}
    if isinstance(raw, dict):
        response = str(raw.get("response", "")).lower()
        detail = raw.get("error") or raw.get("value") or ""
        if response:
            return {"ok": response == "success", "detail": str(detail)}
        return {"ok": not raw.get("error"), "detail": str(detail)}
    return {"ok": True, "detail": str(raw)}


def _recipient_for(z_users, mediatypeid: str) -> str:
    """A real address already configured for this media type, if there is one."""
    for u in z_users:
        for m in u.get("medias") or []:
            if str(m.get("mediatypeid")) == str(mediatypeid) and m.get("sendto"):
                return _sendto_text(m["sendto"])
    return ""


# --------------------------------------------------------------------------
def cmd_history(z, args) -> int:
    """Read the alert log and report what each media type actually did."""
    types = _media_types(z)
    names = {k: v["name"] for k, v in types.items()}
    alerts = _alerts(z, args.days)
    if not alerts:
        print(f"  No notification has been attempted in {args.days} days. Either "
              "nothing has gone wrong, or nothing is configured to tell anyone.")
        return 0

    stats = summarise(alerts, names)
    orphans = stats.pop("0", None)
    total = len(alerts)
    delivered = sum(s["delivered"] for s in stats.values()) + (
        orphans["delivered"] if orphans else 0)

    print(f"── {total} attempt(s) over {args.days} day(s) — {delivered} delivered, "
          f"{total - delivered} not")

    print(f"\n   {'MEDIA TYPE':<28} {'ATTEMPTS':>8} {'SENT':>6} {'FAILED':>7}  STATE")
    ordered = sorted(stats.values(), key=lambda s: -s["attempts"])
    for s in ordered:
        state = classify(s)
        print(f"   {s['name'][:28]:<28} {s['attempts']:>8} {s['delivered']:>6} "
              f"{s['failed']:>7}  {STATE_LABEL[state]}")

    broken = [s for s in ordered if classify(s) == "broken"]
    if broken:
        print(f"\n── Never delivered ({len(broken)}) — broken, not flaky")
        print("   Every attempt through these failed. Anything relying on them is "
              "not being notified at all.")
        for s in broken:
            print(f"   {s['name']}  ({s['failed']} failed, last {_stamp(s['last_failure'])})")
            for err, count in group_errors(s["errors"])[:4]:
                print(f"     · {count}x  {err[:120]}")

    flaky = [s for s in ordered if classify(s) == "flaky"]
    if flaky:
        print(f"\n── Intermittent ({len(flaky)}) — delivers sometimes")
        for s in flaky:
            rate = s["failed"] / s["attempts"] * 100
            print(f"   {s['name']}  {rate:.0f}% failed, last success "
                  f"{_stamp(s['last_success'])}, last failure {_stamp(s['last_failure'])}")
            for err, count in group_errors(s["errors"])[:3]:
                print(f"     · {count}x  {err[:120]}")

    healthy = [s for s in ordered if classify(s) == "healthy"]
    if healthy:
        print(f"\n── Delivering ({len(healthy)})")
        for s in healthy:
            print(f"   {s['name']}  {s['delivered']} delivered, last "
                  f"{_stamp(s['last_success'])}")

    if orphans:
        print(f"\n── Never reached a media type ({orphans['attempts']})")
        print("   The action resolved to a user with nothing to send to, so the "
              "notification was discarded before any media type saw it.")
        for err, count in group_errors(orphans["errors"])[:3]:
            print(f"     · {count}x  {err[:120]}")
        who = {str(a.get("userid")) for a in alerts
               if str(a.get("mediatypeid") or "0") == "0"}
        users = {u["userid"]: u["username"]
                 for u in z.call("user.get", {"output": ["userid", "username"]})}
        named = sorted(users.get(uid, uid) for uid in who if uid and uid != "0")
        if named:
            print(f"     users: {', '.join(named[:10])}")
    return 0


# --------------------------------------------------------------------------
def cmd_users(z, args) -> int:
    """Report who can actually be reached, and who an action expects to reach."""
    types = _media_types(z)
    users = z.call("user.get", {"output": ["userid", "username"],
                                "selectMedias": "extend",
                                "selectUsrgrps": ["usrgrpid", "name", "users_status"],
                                "selectRole": ["name"]})
    now = time.localtime()
    report = [user_status(u, types, now) for u in users]
    report.sort(key=lambda r: (r["reachable"], r["username"]))

    reachable = [r for r in report if r["reachable"]]
    print(f"── {len(report)} user(s) — {len(reachable)} reachable right now")
    for r in report:
        verdict = "reachable" if r["reachable"] else f"UNREACHABLE ({r['reason']})"
        print(f"\n   {r['username'][:30]:<30} {verdict}")
        if not r["media"]:
            print("     no media entries")
        for m in r["media"]:
            mark = "ok  " if m["usable"] else "no  "
            note = "" if m["usable"] else f"  — {m['reason']}"
            print(f"     {mark}{m['name'][:24]:<24} {m['sendto'][:44]}{note}")

    actions = z.call("action.get", {"output": ["actionid", "name", "status", "eventsource"],
                                    "selectOperations": "extend",
                                    "selectRecoveryOperations": "extend",
                                    "selectUpdateOperations": "extend"})
    groups = z.call("usergroup.get", {"output": ["usrgrpid", "name"],
                                      "selectUsers": ["userid"]})
    members = {g["usrgrpid"]: [u["userid"] for u in g.get("users") or []] for g in groups}
    targeted = targeted_userids(actions, members)

    by_id = {r["userid"]: r for r in report}
    stranded = sorted((by_id[uid] for uid in targeted
                       if uid in by_id and not by_id[uid]["reachable"]),
                      key=lambda r: r["username"])
    print(f"\n── Targeted by an enabled action but unreachable ({len(stranded)})")
    if not stranded:
        print("   none")
    else:
        print("   An enabled action will try to notify these users and the "
              "notification will go nowhere. The action looks healthy while this "
              "is happening.")
        for r in stranded:
            print(f"   {r['username'][:30]:<30} {r['reason']}")
    return 1 if (stranded and args.fail_on_unreachable) else 0


# --------------------------------------------------------------------------
def _unavailable_notice() -> None:
    print("   mediatype.test is not exposed by this server's API, so nothing can "
          "be sent from here.")
    print("   Zabbix keeps that test in the frontend on some releases: "
          "Alerts -> Media types -> Test, one media type at a time.")
    print("   The alert log is the other source of truth and needs no send at "
          "all; notify.py history reads it.")


def cmd_test(z, args) -> int:
    """Send one real message through one media type."""
    types = _media_types(z)
    wanted = args.media.strip().lower()
    matches = [m for m in types.values() if m["name"].lower() == wanted]
    if not matches:
        matches = [m for m in types.values() if wanted in m["name"].lower()]
    if not matches:
        print(f"  No media type matches {args.media!r}.", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"  {args.media!r} matches {len(matches)} media types: "
              f"{', '.join(sorted(m['name'] for m in matches))}", file=sys.stderr)
        return 1
    mt = matches[0]

    sendto = args.to
    if not sendto:
        users = z.call("user.get", {"output": ["userid"], "selectMedias": "extend"})
        sendto = _recipient_for(users, mt["mediatypeid"])
    if not sendto:
        print(f"  {mt['name']} has no configured recipient to test against. "
              "Pass --to.", file=sys.stderr)
        return 1

    kind = MEDIA_KIND.get(str(mt["type"]), f"type {mt['type']}")
    enabled = "enabled" if str(mt["status"]) == "0" else "DISABLED"
    print(f"   media type : {mt['name']}  ({kind}, {enabled})")
    print(f"   to         : {mask_sendto(sendto)}")
    print(f"   subject    : {TEST_SUBJECT}")
    print(f"   message    : {_test_message()}")

    if not args.apply:
        print("\n   DRY RUN — nothing sent. Re-run with --apply to send this for real.")
        return 0

    if not test_method_available(z):
        print()
        _unavailable_notice()
        return 1

    result = _send_test(z, mt, sendto)
    if result["ok"]:
        print(f"\n   delivered through {mt['name']}. {result['detail']}".rstrip())
        return 0
    print(f"\n   FAILED through {mt['name']}: {result['detail']}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------
def _verify_from_history(z, enabled, args) -> int:
    """Judge each enabled media type on what the alert log already recorded.

    This is what remains when the server will not let the API send a test. It
    is weaker evidence than a fresh send, but it is real evidence: a media type
    that failed every attempt this week is broken whether or not a test runs.
    """
    names = {k: v["name"] for k, v in enabled.items()}
    stats = summarise(_alerts(z, args.days), names)
    failing, working, unverified = [], [], []
    for mtid, mt in sorted(enabled.items(), key=lambda kv: kv[1]["name"].lower()):
        s = stats.get(mtid)
        state = classify(s) if s else "idle"
        if state in ("broken", "flaky"):
            failing.append((mt, s, state))
        elif state == "healthy":
            working.append((mt, s))
        else:
            unverified.append((mt, state))

    for mt, s in working:
        print(f"   ok     {mt['name'][:30]:<30} {s['delivered']} delivered, last "
              f"{_stamp(s['last_success'])}")
    for mt, s, state in failing:
        print(f"   FAIL   {mt['name'][:30]:<30} {STATE_LABEL[state]}, "
              f"{s['failed']} of {s['attempts']} failed")
        for err, count in group_errors(s["errors"])[:2]:
            print(f"            · {count}x  {err[:120]}")
    for mt, state in unverified:
        print(f"   ?      {mt['name'][:30]:<30} {STATE_LABEL[state]} in "
              f"{args.days} day(s) — unverified")

    print(f"\n   {len(working)} working, {len(failing)} failing, "
          f"{len(unverified)} unverified, from {args.days} day(s) of alert history")
    if failing:
        print(f"\nFAIL: {len(failing)} enabled media type(s) are not delivering",
              file=sys.stderr)
        return 1
    return 0


def cmd_verify(z, args) -> int:
    """Send through every enabled media type and read back what happened."""
    types = _media_types(z)
    enabled = {k: v for k, v in types.items() if str(v["status"]) == "0"}
    if not enabled:
        print("   No media type is enabled. Nothing can be delivered to anyone.",
              file=sys.stderr)
        return 1

    users = z.call("user.get", {"output": ["userid"], "selectMedias": "extend"})
    plan = []
    for mtid, mt in sorted(enabled.items(), key=lambda kv: kv[1]["name"].lower()):
        sendto = args.to or _recipient_for(users, mtid)
        plan.append((mt, sendto))

    print(f"── {len(plan)} enabled media type(s)")
    for mt, sendto in plan:
        kind = MEDIA_KIND.get(str(mt["type"]), f"type {mt['type']}")
        target = mask_sendto(sendto) if sendto else "(no recipient configured)"
        print(f"   {mt['name'][:30]:<30} {kind:<8} -> {target[:44]}")

    available = test_method_available(z)
    if not available:
        print()
        _unavailable_notice()
        print("\n── Falling back to delivery evidence")
        return _verify_from_history(z, enabled, args)

    if not args.apply:
        print(f"\n   DRY RUN — would send {len([p for p in plan if p[1]])} real "
              "message(s) and read the result back after "
              f"{args.wait}s. Re-run with --apply.")
        print("   Nothing was sent, so no media type has been proven either way.")
        return 0

    started = int(time.time())
    results = []
    for mt, sendto in plan:
        if not sendto:
            results.append((mt, None, "no recipient configured, nothing to send to"))
            continue
        outcome = _send_test(z, mt, sendto)
        results.append((mt, outcome["ok"], outcome["detail"]))

    # The send returns before the server has finished with the queue on some
    # media types, so the log is only worth reading after a pause.
    print(f"\n   waiting {args.wait}s for the server to finish the queue")
    time.sleep(args.wait)
    logged = summarise(z.call("alert.get", {
        "output": ["clock", "status", "error", "mediatypeid"],
        "mediatypeids": sorted(enabled), "time_from": started, "limit": 10000,
    }), {k: v["name"] for k, v in enabled.items()})

    print("\n── Result")
    failed = 0
    unverified = 0
    for mt, ok, detail in results:
        after = logged.get(mt["mediatypeid"])
        trail = ""
        if after:
            trail = (f"  [log: {after['delivered']} sent, {after['failed']} failed "
                     "since the test]")
        if ok is None:
            unverified += 1
            print(f"   ?      {mt['name'][:30]:<30} {detail}")
        elif ok:
            print(f"   ok     {mt['name'][:30]:<30} {detail[:44]}{trail}")
        else:
            failed += 1
            print(f"   FAIL   {mt['name'][:30]:<30} {detail[:64]}{trail}")

    print(f"\n   {len(results) - failed - unverified} working, {failed} failing, "
          f"{unverified} unverified")
    if failed:
        print(f"\nFAIL: {failed} enabled media type(s) could not deliver",
              file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
COMMANDS = {
    "history": (cmd_history, "what each media type actually delivered, from the alert log"),
    "test": (cmd_test, "send one real message through one media type"),
    "verify": (cmd_verify, "test every enabled media type and report which paths work"),
    "users": (cmd_users, "who can actually be reached right now"),
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, (fn, help_text) in COMMANDS.items():
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=fn)
        if name in ("history", "verify"):
            p.add_argument("--days", type=int, default=7,
                           help="days of alert history to read (default 7)")
        if name in ("test", "verify"):
            p.add_argument("--to", help="send to this address instead of the one "
                                        "already configured on a user")
            p.add_argument("--apply", action="store_true",
                           help="send for real (without this it is a dry run)")
        if name == "test":
            p.add_argument("--media", required=True, help="media type name")
        if name == "verify":
            p.add_argument("--wait", type=int, default=10,
                           help="seconds to wait before reading the result back")
        if name == "users":
            p.add_argument("--fail-on-unreachable", action="store_true",
                           help="exit non-zero if an enabled action targets a user "
                                "who cannot be reached")
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
