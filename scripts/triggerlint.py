#!/usr/bin/env python3
"""Lint trigger expressions for mistakes plausible enough to survive review.

    ./scripts/triggerlint.py check
    ./scripts/triggerlint.py check --host db01
    ./scripts/triggerlint.py check --template "Linux by Zabbix agent"
    ./scripts/triggerlint.py check --min-confidence high
    ./scripts/triggerlint.py check --json
    ./scripts/triggerlint.py check --fail-on inverted_sense

A trigger that is wrong in an obvious way is found within a week. The ones that
last are the ones that read correctly. A CPU trigger built on the *idle*
percentage and wrapped in ``>80`` sits in the list looking exactly like every
other CPU trigger, and fires when the machine is asleep instead of when it is
busy. That one ran for months before anyone worked out why the quiet containers
were the noisy ones.

Read-only. This tool never writes to Zabbix.

Confidence means:

    high    The expression cannot plausibly mean what its description says.
    medium  Suspicious. There is an innocent reading, but it is the rarer one.
    low     Worth a glance. Reported because it costs seconds to dismiss.

A linter people learn to skip is worse than no linter, so anything with a
defensible alternative reading is reported low and explained rather than
asserted. Nothing here is authoritative; every finding is a question.

Scope
-----

Only trigger *definitions* are examined by default: triggers that are not
inherited copies (``templateid`` is 0) and were not created by low-level
discovery. An inherited trigger is the same trigger seen from another angle, and
a discovered one is a prototype seen several hundred times. Reporting either
buries the single definition somebody can actually edit.

Vendor-supplied templates are skipped for the same reason. They are reviewed
upstream, an edit is lost on the next template update, and on a normal server
they outnumber locally written triggers by twenty to one. ``--include-vendor``
brings them back, which is mostly useful for checking that a rule is not
over-firing: a rule that lights up across stock templates is wrong.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from zbx import ZabbixError, connect_or_exit  # noqa: E402

CONFIDENCE = ("high", "medium", "low")

#: Rule name -> one-line summary. Iteration order is output order.
RULES = {
    "inverted_sense": "Expression may test the opposite of what it describes",
    "nodata_no_manual_close": "nodata problem nobody can clear from the UI",
    "severity_mismatch": "Severity disagrees with the wording",
    "missing_dependency": "Guest availability trigger with no dependency",
    "counter_threshold": "Threshold compared against a raw counter",
    "hardcoded_threshold": "Literal threshold where a macro looks intended",
    "missing_item": "Expression references an item that is not usable",
}
RULE_NAMES = tuple(RULES)

#: docs/architecture.md, "Severity convention". Names as written there.
SEVERITY_NAMES = {0: "NOT CLASSIFIED", 1: "INFO", 2: "WARNING", 3: "AVERAGE",
                  4: "HIGH", 5: "DISASTER"}


# --------------------------------------------------------------------------
# Expression parsing
# --------------------------------------------------------------------------
# The API returns the stored form of an expression, in which each function call
# is a functionid reference: "{12641}>75". That form is parsed here rather than
# the expanded one, because expansion substitutes user macros inside item keys
# and the key can then no longer be matched back to the item it came from.
# Expansion for display is done separately, from the same function records.
_FUNCID = re.compile(r"\{(\d+)\}")
_COMPARISON = re.compile(
    r"""\s*(?P<op><>|>=|<=|=|>|<)\s*
        (?: "\{\$(?P<qmacro>[^"}]*)\}"
          | \{\$(?P<macro>[^}]*)\}
          | (?P<num>-?\d+(?:\.\d+)?)(?P<suffix>[KMGTsmhdw]?)
        )""",
    re.VERBOSE,
)


def split_args(text: str) -> list[str]:
    """Split a Zabbix argument list on top-level commas.

    Item keys carry their own bracketed, quoted parameters, so a naive split
    tears ``proc.num[,,,-m nginx]`` in half.
    """
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    quoted = False
    i = 0
    while i < len(text):
        c = text[i]
        if quoted:
            buf.append(c)
            if c == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                quoted = False
        elif c == '"':
            quoted = True
            buf.append(c)
        elif c in "[(":
            depth += 1
            buf.append(c)
        elif c in "])":
            depth -= 1
            buf.append(c)
        elif c == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf))
    return out


def parse_refs(expression: str) -> list[dict]:
    """Find every function reference and the constant it is compared against.

    Returns one dict per ``{functionid}`` in the expression::

        functionid  the reference as stored
        op          comparison operator immediately following, or ""
        num         numeric constant on the right, or None
        suffix      Zabbix unit suffix on that constant ("", "M", "m", ...)
        macro       user macro name on the right, or None
        arith       True when the reference is part of an arithmetic term

    Only the operator directly after the reference is attributed to it. A
    comparison written the other way round (``80 < {12641}``) is not parsed,
    which loses a finding rather than inventing one; that trade is deliberate
    everywhere in this file.
    """
    refs = []
    for m in _FUNCID.finditer(expression):
        before = expression[:m.start()].rstrip()
        ref = {
            "functionid": m.group(1),
            "op": "",
            "num": None,
            "suffix": "",
            "macro": None,
            # A reference inside a calculation means the comparison applies to
            # the result, not to the item, so its sense cannot be read off the
            # item alone. "100-{123}>80" is a correct utilisation trigger.
            "arith": bool(before) and before[-1] in "+-*/",
        }
        c = _COMPARISON.match(expression, m.end())
        if c:
            ref["op"] = c.group("op")
            ref["macro"] = c.group("qmacro") or c.group("macro")
            if c.group("num") is not None:
                ref["num"] = float(c.group("num"))
                ref["suffix"] = c.group("suffix") or ""
            # A constant that is itself the start of a calculation is not a
            # threshold: ">(90/100)*{456}" compares against another item.
            tail = expression[c.end():].lstrip()
            if tail[:1] in ("+", "-", "*", "/"):
                ref["num"] = None
                ref["macro"] = None
        refs.append(ref)
    return refs


def render_expression(expression: str, functions: dict, items: dict,
                      hosts: dict) -> str:
    """Rebuild the readable form of a stored expression.

    Equivalent to the API's expandExpression, except that user macros inside
    item keys are left as they were written. Seeing ``{$PG.PASSWORD}`` rather
    than its value also keeps credentials out of the report.
    """
    def sub(m):
        fn = functions.get(m.group(1))
        if not fn:
            return "<missing function>"
        item = items.get(fn.get("itemid"))
        if not item:
            return f"{fn.get('function', '?')}(<missing item>)"
        host = hosts.get(item.get("hostid"), {}).get("host", "?")
        args = split_args(fn.get("parameter") or "$")
        args[0] = f"/{host}/{item.get('key_', '?')}"
        return f"{fn.get('function', '?')}({','.join(args)})"

    return _FUNCID.sub(sub, expression)


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------
# Sense is read off the item key first and the item name second. Order matters:
# "system.cpu.util[,idle]" contains both an inverse and a direct word, and the
# inverse one is the parameter that decides what the item actually holds.
INVERSE_WORDS = frozenset({"idle", "pfree", "pavail", "pavailable", "free",
                           "available", "avail", "remaining", "unused",
                           "uptime"})
DIRECT_WORDS = frozenset({"pused", "util", "used", "usage", "busy", "consumed",
                          "occupied", "utilization", "utilisation"})

#: Only percentage-shaped comparisons are judged for sense. An absolute value
#: has no "high" or "low" that can be recognised without knowing the machine,
#: and uptime is inverse but is never a percentage, so "uptime > 0" -- a guard
#: clause seen in several vendor templates -- never reaches this rule.
PERCENT_HINTS = ("pused", "pfree", "pavail", "percent", "util", "idle",
                 "usage", "loss", "utilization", "utilisation")

#: Wording that promises something is gone. docs/architecture.md places these
#: at HIGH ("a host or service is gone") or DISASTER ("a segment may be down").
#: "no data" is deliberately absent. Stock templates put "(or no data for 30m)"
#: in the event name of perfectly ordinary fetch-failure triggers, and matching
#: it turned this rule into forty findings about nothing.
OUTAGE_PATTERNS = (r"\bunreachable\b", r"\bis down\b", r"\bdown\b", r"\boffline\b",
                   r"\bnot responding\b", r"\bunavailable\b", r"\bcritical\b")
#: Wording that records something. INFO in the same table: "Record only".
RECORD_PATTERNS = (r"\brestarted\b", r"\brebooted\b", r"\bchanged\b",
                   r"\bconfiguration change\b", r"\bhas been\b", r"\bnew version\b",
                   r"\bdetected\b")

#: Keys whose value accumulates from boot and never decreases on its own.
#: Deliberately narrow. Words like "total" and "count" appear on plenty of
#: gauges -- docker.containers.total is a current count, not an accumulator --
#: and including them turned this rule into a list of things that were fine.
COUNTER_HINTS = ("octets", "packets", "ifinerrors", "ifouterrors",
                 "ifindiscards", "ifoutdiscards", "net.if.in", "net.if.out",
                 "vfs.dev.read", "vfs.dev.write", "system.cpu.intr",
                 "system.cpu.switches", ".errors", ".discards", ".dropped",
                 "collisions")
#: Preprocessing that converts a counter into a per-interval or per-second
#: figure. API returns numeric types; export files spell them out.
COUNTER_PREPROCESSING = {"9", "10", 9, 10, "SIMPLE_CHANGE", "CHANGE_PER_SECOND"}
#: Trigger functions that already differentiate, so the raw counter is fine.
DIFFERENCING_FUNCTIONS = {"change", "abschange", "changecount", "rate",
                          "forecast", "timeleft", "nodata", "fuzzytime"}

#: Group and template naming that suggests a guest, and its host. The
#: hypervisor side is kept narrow on purpose: a loose match here nominated
#: every host in an "Infrastructure" group, and a dependency suggestion
#: pointing at the wrong parent is worse than no suggestion.
GUEST_HINTS = ("container", "/vms", " vm", "vm ", "guest", "lxc", "docker")
HYPERVISOR_HINTS = ("proxmox", "hypervisor", "vmware", "esxi", "xenserver")

#: Item keys that answer "is this host there at all", as opposed to "is this
#: service on it healthy". Only the first kind belongs behind a hypervisor
#: dependency; a service check failing says nothing about the node.
AVAILABILITY_KEYS = ("agent.ping", "icmpping", "zabbix[host,agent,available]",
                     "net.tcp.service[tcp,,10050]")

#: Macro name parts that say how a threshold is used rather than what it is.
#: Stripping them leaves the part that has to match an item to be relevant.
MACRO_QUALIFIERS = {"max", "min", "crit", "critical", "warn", "warning", "high",
                    "low", "time", "threshold", "limit", "pct", "percent"}


def _sense(item: dict) -> str:
    """Return "inverse", "direct" or "" for what an item counts.

    Whole words only. A substring match reads "availability manager" as an
    inverse metric, and the Zabbix server health template has a busy-percentage
    item by that name which then looks like the bug this tool is named after.
    """
    for field in ("key_", "name"):
        words = set(re.split(r"[^a-z0-9]+", (item.get(field) or "").lower()))
        if words & INVERSE_WORDS:
            return "inverse"
        if words & DIRECT_WORDS:
            return "direct"
    return ""


def _looks_like_percent(item: dict, ref: dict) -> bool:
    if ref["suffix"] or ref["num"] is None or not 0 <= ref["num"] <= 100:
        return False
    if (item.get("units") or "").strip() == "%":
        return True
    text = ((item.get("key_") or "") + " " + (item.get("name") or "")).lower()
    return any(h in text for h in PERCENT_HINTS)


def _is_availability(item: dict) -> bool:
    key = (item.get("key_") or "").lower()
    return any(key.startswith(k) for k in AVAILABILITY_KEYS)


def is_hypervisor(host: dict) -> bool:
    """Whether a host looks like it runs guests.

    Group names and linked template names are all Zabbix knows about the
    relationship; the Proxmox side of the estate is not consulted, so this
    stays a guess and the rule that uses it says so.
    """
    text = " ".join(host.get("groups", []) + host.get("templates", [])).lower()
    return any(hint in text for hint in HYPERVISOR_HINTS)


def _matches(patterns, text: str) -> str:
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(0)
    return ""


# --------------------------------------------------------------------------
# Estate
# --------------------------------------------------------------------------
class Estate:
    """Everything the rules read, fetched once.

    The rules are pure functions of this object so that they can be exercised
    against hand-built data without a server.
    """

    def __init__(self, definitions=None, host_triggers=None, items=None,
                 hosts=None, functions=None, macros=None, availability=None):
        self.definitions = definitions or []
        self.host_triggers = host_triggers or []
        self.items = items or {}
        self.hosts = hosts or {}
        self.functions = functions or {}
        #: hostid -> list of macro dicts, already including inherited ones.
        self.macros = macros or {}
        #: hostid of every host carrying its own availability trigger.
        self.availability = availability or set()

    def owner(self, trigger: dict) -> dict:
        hosts = trigger.get("hosts") or []
        return hosts[0] if hosts else {}

    def refs(self, trigger: dict) -> list[dict]:
        """Parsed references with their function and item attached."""
        out = []
        for ref in parse_refs(trigger.get("expression") or ""):
            fn = self.functions.get(ref["functionid"]) or {}
            ref = dict(ref)
            ref["function"] = fn.get("function", "")
            ref["item"] = self.items.get(fn.get("itemid")) or {}
            ref["itemid"] = fn.get("itemid")
            out.append(ref)
        return out

    def readable(self, trigger: dict) -> str:
        text = render_expression(trigger.get("expression") or "",
                                 self.functions, self.items, self.hosts)
        # Multi-line expressions are stored with the author's line breaks. One
        # finding should be one line, so the report can be grepped.
        return " ".join(text.split())


def finding(est: Estate, trigger: dict, rule: str, confidence: str, why: str,
            hint: str) -> dict:
    owner = est.owner(trigger)
    return {
        "rule": rule,
        "confidence": confidence,
        "trigger": trigger.get("description", ""),
        "owner": owner.get("host", ""),
        "owner_kind": "template" if owner.get("status") == "3" else "host",
        "severity": SEVERITY_NAMES.get(int(trigger.get("priority", 0) or 0), "?"),
        "expression": est.readable(trigger),
        "why": why,
        "hint": hint,
    }


# --------------------------------------------------------------------------
# Rule 1: inverted sense
# --------------------------------------------------------------------------
def rule_inverted_sense(est: Estate) -> list[dict]:
    """An expression that tests the opposite of what its description claims.

    The shape that survives review is an inverse metric -- idle, free,
    available -- compared upward against a threshold that reads like a
    utilisation limit. The trigger fires on an idle machine and stays quiet on a
    saturated one, and both the name and the number look right in the UI.

    The mirror case, a utilisation item compared downward, is reported too but
    only at low confidence: deliberately alerting on a host that has gone quiet
    is a real technique, just an uncommon one.
    """
    out = []
    for t in est.definitions:
        for ref in est.refs(t):
            item = ref["item"]
            if not item or ref["arith"] or not _looks_like_percent(item, ref):
                continue
            sense = _sense(item)
            key = item.get("key_", "")
            if sense == "inverse" and ref["op"] in (">", ">="):
                # Above half, an inverse metric being high is the healthy
                # state, so alerting on it is almost certainly the wrong way
                # round. Below half it could be a deliberate "too much free
                # space, the mount has been wiped" check.
                conf = "high" if ref["num"] >= 50 else "medium"
                out.append(finding(
                    est, t, "inverted_sense", conf,
                    f"{key!r} counts what is left over, and the trigger fires "
                    f"when that figure rises above {ref['num']:g}. The problem "
                    "state described is the opposite of the state tested: this "
                    "alerts when the resource is free and stays silent when it "
                    "runs out.",
                    "Compare against the matching used or utilisation item, or "
                    "keep this item and invert the operator."))
            elif sense == "direct" and ref["op"] in ("<", "<=") and ref["num"] <= 25:
                out.append(finding(
                    est, t, "inverted_sense", "low",
                    f"{key!r} counts what is in use, and the trigger fires when "
                    f"it drops below {ref['num']:g}. That is a deliberate check "
                    "on some estates and an inverted comparison on others.",
                    "If the intent was a busy-host alert the operator is the "
                    "wrong way round. If the intent was an idle-host alert, "
                    "say so in the description."))
    return out


# --------------------------------------------------------------------------
# Rule 2: nodata without manual_close
# --------------------------------------------------------------------------
def rule_nodata_no_manual_close(est: Estate) -> list[dict]:
    """A nodata problem that no operator can get rid of.

    nodata recovers when data starts arriving again. For a host that was
    decommissioned rather than repaired, data never arrives again, so the
    problem sits in the list permanently. docs/architecture.md asks for
    manual_close on exactly these, and problems.py already reports the ones
    that got through.
    """
    out = []
    for t in est.definitions:
        functions = {(est.functions.get(r["functionid"]) or {}).get("function")
                     for r in parse_refs(t.get("expression") or "")}
        if "nodata" not in functions or t.get("manual_close") == "1":
            continue
        # recovery_mode 2 is "no recovery expression at all", which removes the
        # last route back to a normal state.
        only = len(functions) == 1
        conf = "high" if only or t.get("recovery_mode") == "2" else "medium"
        out.append(finding(
            est, t, "nodata_no_manual_close", conf,
            "Recovery depends on data arriving again. If the host was retired "
            "rather than fixed it never will, and the problem cannot be closed "
            "from the UI because manual_close is off."
            + ("" if only else " Recovery may still come from the other terms "
                                "in the expression, which makes this less certain."),
            "Set manual_close: YES on the trigger, as docs/architecture.md "
            "specifies for nodata-based triggers."))
    return out


# --------------------------------------------------------------------------
# Rule 3: severity against wording
# --------------------------------------------------------------------------
def rule_severity_mismatch(est: Estate) -> list[dict]:
    """Severity that contradicts what the trigger says it found.

    The convention in docs/architecture.md only earns its keep if sorting by
    severity produces a real work queue. One "host unreachable" filed at
    WARNING is enough to teach people that the ordering means nothing.
    """
    out = []
    for t in est.definitions:
        text = f"{t.get('description', '')} {t.get('event_name', '')}"
        priority = int(t.get("priority", 0) or 0)
        outage = _matches(OUTAGE_PATTERNS, text)
        record = _matches(RECORD_PATTERNS, text)
        if priority == 0:
            out.append(finding(
                est, t, "severity_mismatch", "low",
                "Severity is NOT CLASSIFIED, so this trigger sorts below "
                "everything in the problem list regardless of what it found.",
                "Place it on the scale in docs/architecture.md."))
        elif outage and not record and priority <= 2:
            # An outage word alongside a recovery-ish word is usually something
            # like "link down count changed", which is genuinely informational.
            conf = "medium" if priority == 2 else "high"
            out.append(finding(
                est, t, "severity_mismatch", conf,
                f"The wording says {outage!r} but the severity is "
                f"{SEVERITY_NAMES[priority]}. docs/architecture.md puts a host "
                "or service being gone at HIGH and a segment at DISASTER.",
                "Raise the severity, or reword if the trigger really is "
                "reporting something smaller than it sounds."))
        elif record and not outage and priority >= 4:
            out.append(finding(
                est, t, "severity_mismatch", "medium",
                f"The wording says {record!r}, which reads as a record of "
                f"something happening, but the severity is "
                f"{SEVERITY_NAMES[priority]}. Severities that overstate get "
                "filtered out by hand, and then the real ones are too.",
                "Lower to INFO or WARNING, or reword to say what is broken."))
    return out


# --------------------------------------------------------------------------
# Rule 4: missing dependencies
# --------------------------------------------------------------------------
def rule_missing_dependency(est: Estate) -> list[dict]:
    """A guest that alerts separately when its hypervisor is the thing at fault.

    This is an opportunity rather than a defect. Nothing is misconfigured; the
    problem list is just longer than it needs to be when a node goes down,
    because every guest on it reports its own unreachability alongside the one
    alert that explains all of them.

    The guest-to-hypervisor mapping is inferred from group naming, so it is
    reported at low confidence when more than one hypervisor is plausible.
    """
    hypervisors = sorted(h.get("host", "") for hid, h in est.hosts.items()
                         if hid in est.availability and is_hypervisor(h))
    if not hypervisors:
        return []

    out = []
    for t in est.host_triggers:
        if t.get("dependencies"):
            continue
        text = f"{t.get('description', '')} {t.get('event_name', '')}"
        if not _matches((r"\bunreachable\b", r"\bis down\b", r"\bno data\b",
                         r"\bnot responding\b"), text):
            continue
        # The wording alone is not enough. "LDAP port 389 not responding" reads
        # like an availability trigger and is a service check; suppressing it
        # behind the hypervisor would hide a real LDAP fault.
        if not any(_is_availability(r["item"]) for r in est.refs(t)):
            continue
        owner = est.owner(t)
        host = est.hosts.get(owner.get("hostid"), {})
        groups = [g.lower() for g in host.get("groups", [])]
        if not any(hint in g for g in groups for hint in GUEST_HINTS):
            continue
        if host.get("host") in hypervisors:
            continue
        conf = "medium" if len(hypervisors) == 1 else "low"
        where = (f"its hypervisor {hypervisors[0]!r}" if len(hypervisors) == 1
                 else "one of " + ", ".join(repr(h) for h in hypervisors[:4]))
        out.append(finding(
            est, t, "missing_dependency", conf,
            f"A guest availability trigger with no dependency, while {where} "
            "carries an equivalent host-level one. When the node goes down this "
            "fires alongside every other guest on it, and the alert that "
            "explains the outage is buried among the ones caused by it.",
            f"Add a dependency on the availability trigger of {where}. Guest to "
            "hypervisor here is inferred from host group naming, so confirm the "
            "pairing before applying it."))
    return out


# --------------------------------------------------------------------------
# Rule 5: thresholds on a raw counter
# --------------------------------------------------------------------------
def rule_counter_threshold(est: Estate) -> list[dict]:
    """A threshold on a value that only ever goes up.

    An interface error counter reads 0 at boot and climbs for the life of the
    device. Compared against a fixed number it trips once, months in, and then
    never recovers, which looks identical to a fault that nobody is fixing.
    """
    out = []
    for t in est.definitions:
        for ref in est.refs(t):
            item = ref["item"]
            if not item or ref["num"] is None or ref["op"] not in (">", ">="):
                continue
            if ref["function"] in DIFFERENCING_FUNCTIONS:
                continue
            key = (item.get("key_") or "").lower()
            if not any(h in key for h in COUNTER_HINTS):
                continue
            steps = item.get("preprocessing") or []
            if any(str(s.get("type")) in {str(x) for x in COUNTER_PREPROCESSING}
                   for s in steps):
                continue
            # An unsigned item with no differencing anywhere is the textbook
            # case. A float is more often already a rate the key name hides.
            conf = "medium" if item.get("value_type") in ("3", 3) else "low"
            fname = ref["function"] or "the function"
            out.append(finding(
                est, t, "counter_threshold", conf,
                f"{item.get('key_', '')!r} reads like a counter that accumulates "
                "from boot, and nothing differentiates it: no change or rate "
                f"preprocessing on the item, and {fname}() does not take a "
                "difference. A fixed threshold on such a value trips once and "
                "then stays tripped forever.",
                "Add Change per second preprocessing to the item, or use "
                "change() or rate() in the expression."))
    return out


# --------------------------------------------------------------------------
# Rule 6: hardcoded thresholds
# --------------------------------------------------------------------------
_NAMED_MACRO = re.compile(r"\{\$([A-Z0-9_.]+)")


def _macro_tokens(name: str) -> set:
    parts = re.split(r"[._\-]", name.strip("{}$").split(":")[0].lower())
    return {p for p in parts if len(p) >= 2 and p not in MACRO_QUALIFIERS}


def _numeric(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _macro_usage(est: Estate) -> dict:
    """Which macros are already used as thresholds, per owner and item key."""
    usage = defaultdict(lambda: defaultdict(set))
    for t in est.definitions:
        owner = est.owner(t).get("hostid")
        for ref in est.refs(t):
            if ref["macro"] and ref["item"]:
                name = "{$" + ref["macro"].split(":")[0] + "}"
                usage[owner][ref["item"].get("key_", "")].add(name)
    return usage


def rule_hardcoded_threshold(est: Estate) -> list[dict]:
    """A number written into an expression that a macro was meant to hold.

    Weak by construction: a literal is not wrong, and the tool is guessing at
    intent. It earns its place because of one specific failure. A template
    defines {$FS.PUSED.MAX}, most of its disk triggers use it, one does not, and
    raising the macro on a host then changes every disk alert except that one.

    Three signals, in descending order of how much they are worth. The trigger's
    own name promising a macro its expression does not use is close to
    conclusive; a sibling trigger on the same template comparing the same item
    against a macro is strong; a name that merely looks related is a guess and
    is reported as one. Only macros holding a number are considered, which keeps
    credentials out of the report as well as out of the comparison.
    """
    usage = _macro_usage(est)
    out = []
    for t in est.definitions:
        owner = est.owner(t).get("hostid")
        refs = est.refs(t)
        # A macro anywhere in the expression means the author knew about them
        # and chose a literal for this term. That is usually deliberate.
        if any(r["macro"] for r in refs):
            continue
        # Only an ordering comparison is a threshold. "nodata(...)=1" and
        # "last(...)=0" are state tests, and a macro would not improve them; a
        # duration or a forecast horizon is not on the item's scale at all.
        literals = [r for r in refs
                    if r["num"] is not None and not r["suffix"]
                    and r["op"] in (">", ">=", "<", "<=")
                    and r["function"] not in ("timeleft", "forecast")]
        if not literals:
            continue

        promised = sorted({"{$" + n + "}" for n in _NAMED_MACRO.findall(
            f"{t.get('description', '')} {t.get('event_name', '')}")})
        if promised:
            out.append(finding(
                est, t, "hardcoded_threshold", "medium",
                f"The trigger is named after {promised[0]} but its expression "
                f"compares against the literal {literals[0]['num']:g}. Anyone "
                "reading the problem list believes the macro is in force, and "
                "setting it on a host changes nothing.",
                f"Substitute {promised[0]} into the expression, or take it out "
                "of the name."))
            continue

        pool = {m.get("macro"): m for m in est.macros.get(owner, [])
                if _numeric(m.get("value")) is not None}
        for ref in literals:
            item = ref["item"]
            if not item:
                continue
            key = item.get("key_", "")
            siblings = sorted(usage.get(owner, {}).get(key, ()))
            if siblings:
                # Low, not medium: a second threshold on the same item is very
                # often a deliberate second tier, warning against critical, and
                # only one of the two was ever meant to be tunable.
                out.append(finding(
                    est, t, "hardcoded_threshold", "low",
                    f"Another trigger here compares {key!r} against "
                    f"{siblings[0]}, and this one uses the literal "
                    f"{ref['num']:g}. Only one of the two moves when the macro "
                    "is overridden on a host.",
                    f"Substitute {siblings[0]} if this is the same threshold. "
                    "If it is a second tier, it wants a macro of its own."))
                break
            haystack = f"{key} {item.get('name', '')} {t.get('description', '')}".lower()
            hit = next((n for n, m in sorted(pool.items())
                        if len(_macro_tokens(n)) >= 2
                        and all(tok in haystack for tok in _macro_tokens(n))
                        and _numeric(m.get("value")) == ref["num"]), None)
            if hit:
                out.append(finding(
                    est, t, "hardcoded_threshold", "low",
                    f"{hit} is defined here and already holds {ref['num']:g}, "
                    "the same number this expression has written into it. That "
                    "may be coincidence: the match is made on naming alone.",
                    f"If {hit} was meant for this item, substitute it. If it "
                    "was not, ignore this."))
                break
    return out


# --------------------------------------------------------------------------
# Rule 7: references to items that are not usable
# --------------------------------------------------------------------------
def rule_missing_item(est: Estate) -> list[dict]:
    """An expression pointing at an item that is not there, or not collecting.

    Zabbix will not normally let this be created. It arises afterwards: a
    partial import that brought triggers without their items, a template
    unlinked with "clear when unlinking", an item disabled by hand while the
    trigger that reads it was left alone. The trigger stays in the list looking
    configured and never evaluates.
    """
    out = []
    for t in est.definitions:
        for ref in parse_refs(t.get("expression") or ""):
            fn = est.functions.get(ref["functionid"])
            item = est.items.get((fn or {}).get("itemid")) if fn else None
            if fn and item and item.get("status") not in ("1", 1) \
                    and item.get("state") not in ("1", 1):
                continue
            if not fn or not item:
                out.append(finding(
                    est, t, "missing_item", "high",
                    "The expression refers to an item that no longer resolves. "
                    "The trigger cannot be evaluated, so it will never fire and "
                    "nothing in the problem list says so.",
                    "Re-import the template that owns the item, or delete the "
                    "trigger. Compare the two sides with canon.py diff."))
            elif item.get("status") in ("1", 1):
                out.append(finding(
                    est, t, "missing_item", "medium",
                    f"{item.get('key_', '')!r} is disabled, so this trigger has "
                    "nothing to evaluate. It still appears configured and "
                    "enabled in the trigger list.",
                    "Re-enable the item, or disable the trigger so the gap in "
                    "coverage is visible."))
            else:
                out.append(finding(
                    est, t, "missing_item", "medium",
                    f"{item.get('key_', '')!r} is unsupported: "
                    f"{(item.get('error') or 'no error recorded').strip()[:90].rstrip('.')}. "
                    "The trigger reads a value that is not arriving.",
                    "Fix the item, usually at template level. audit.py's "
                    "collection check groups these by cause."))
            break
    return out


CHECKS = {
    "inverted_sense": rule_inverted_sense,
    "nodata_no_manual_close": rule_nodata_no_manual_close,
    "severity_mismatch": rule_severity_mismatch,
    "missing_dependency": rule_missing_dependency,
    "counter_threshold": rule_counter_threshold,
    "hardcoded_threshold": rule_hardcoded_threshold,
    "missing_item": rule_missing_item,
}


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def _macro_pool(hosts: dict, parents: dict, own: dict) -> dict:
    """Macros visible to each host: its own, plus every template above it."""
    pool = {}
    for hostid in hosts:
        seen = []
        stack = [hostid]
        visited = set()
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            seen.extend(own.get(cur, []))
            stack.extend(parents.get(cur, []))
        # First definition wins, mirroring how Zabbix resolves the chain.
        by_name = {}
        for m in seen:
            by_name.setdefault(m.get("macro"), m)
        pool[hostid] = list(by_name.values())
    return pool


def _fetch_items(z, itemids):
    return z.call("item.get", {"itemids": list(itemids), "webitems": True,
                               "output": ["itemid", "hostid", "key_", "name",
                                          "value_type", "units", "status",
                                          "state", "error"],
                               "selectPreprocessing": "extend"})


def load(z, args) -> Estate:
    """Fetch everything the rules need, in as few calls as will do."""
    templates = z.call("template.get",
                       {"output": ["templateid", "host", "vendor_name"],
                        "selectParentTemplates": ["templateid", "host"]})
    vendor = {t["templateid"] for t in templates if t.get("vendor_name")}

    hostlist = z.call("host.get", {"output": ["hostid", "host", "status"],
                                   "selectHostGroups": ["name"],
                                   "selectParentTemplates": ["templateid", "host"]})
    hosts = {}
    parents = {}
    for h in hostlist + templates:
        hid = h.get("hostid") or h["templateid"]
        hosts[hid] = {
            "host": h["host"], "status": h.get("status", "3"),
            "groups": [g["name"] for g in h.get("hostgroups", [])],
            "templates": [p["host"] for p in h.get("parentTemplates", [])],
        }
        parents[hid] = [p["templateid"] for p in h.get("parentTemplates", [])]

    params = {"output": "extend", "selectHosts": ["hostid", "host", "status"],
              "selectFunctions": "extend", "selectDependencies": ["triggerid"]}
    wanted = None
    if args.host or args.template:
        wanted = [hid for hid, h in hosts.items()
                  if (args.host and h["status"] != "3"
                      and args.host.lower() in h["host"].lower())
                  or (args.template and h["status"] == "3"
                      and args.template.lower() in h["host"].lower())]
        if not wanted:
            raise ZabbixError("no host or template matched that name")
        params["hostids"] = wanted
    triggers = z.call("trigger.get", params)

    # Rule 4 compares a guest against its hypervisor, which a --host filter
    # would otherwise hide. Those triggers are fetched regardless.
    hyper_ids = [hid for hid, h in hosts.items()
                 if h["status"] != "3" and is_hypervisor(h)]
    if wanted is not None and hyper_ids:
        have = {t["triggerid"] for t in triggers}
        extra = z.call("trigger.get", dict(params, hostids=hyper_ids))
        triggers += [t for t in extra if t["triggerid"] not in have]

    functions = {}
    for t in triggers:
        for fn in t.get("functions") or []:
            functions[fn["functionid"]] = fn
    itemids = sorted({fn["itemid"] for fn in functions.values()})
    items = {}
    for i in range(0, len(itemids), 2000):
        for it in _fetch_items(z, itemids[i:i + 2000]):
            items[it["itemid"]] = it

    # Triggers and items come from separate calls, so discovery running between
    # the two deletes an interface and leaves a trigger here pointing at an item
    # that no longer exists. That is indistinguishable from a genuinely dangling
    # reference, and on the first run against a server with live LLD churn it
    # was reported as one, at high confidence. Anything that looks dangling is
    # now confirmed twice before it is believed.
    absent = [i for i in itemids if i not in items]
    if absent:
        for it in _fetch_items(z, absent):
            items[it["itemid"]] = it
        gone = {i for i in absent if i not in items}
        touched = {t["triggerid"] for t in triggers
                   for fn in (t.get("functions") or [])
                   if fn["itemid"] in gone}
        if touched:
            alive = {t["triggerid"] for t in z.call(
                "trigger.get", {"triggerids": sorted(touched), "output": ["triggerid"]})}
            triggers = [t for t in triggers
                        if t["triggerid"] not in touched or t["triggerid"] in alive]

    own = defaultdict(list)
    for m in z.call("usermacro.get", {"output": ["hostid", "macro", "value"]}):
        own[m["hostid"]].append(m)

    def is_definition(t):
        owner = (t.get("hosts") or [{}])[0]
        if not args.include_vendor and owner.get("hostid") in vendor:
            return False
        if t.get("flags") == "4":
            return args.include_discovered
        if t.get("templateid") != "0":
            return args.include_inherited
        return True

    definitions = [t for t in triggers if is_definition(t)]
    if wanted is not None:
        # The hypervisor triggers fetched above are context for rule 4 only.
        # Someone who asked about one host should not be shown findings on a
        # different one.
        definitions = [t for t in definitions
                       if (t.get("hosts") or [{}])[0].get("hostid") in set(wanted)]

    # A host counts as having its own availability check if any trigger on it
    # reads a ping or agent item and says so.
    availability = set()
    for t in triggers:
        owner = (t.get("hosts") or [{}])[0]
        if owner.get("status") == "3":
            continue
        if _matches((r"\bunreachable\b", r"\bis down\b", r"\bno data\b",
                     r"\bnot responding\b", r"\bping failed\b"),
                    t.get("description", "")):
            availability.add(owner.get("hostid"))

    host_triggers = [t for t in triggers
                     if (t.get("hosts") or [{}])[0].get("status") == "0"
                     and t.get("flags") != "4"]

    return Estate(definitions=definitions, host_triggers=host_triggers,
                  items=items, hosts=hosts, functions=functions,
                  macros=_macro_pool(hosts, parents, own),
                  availability=availability)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def render(findings: list, limit: int) -> None:
    if not findings:
        print("\nNothing flagged. Worth confirming the account can read the "
              "templates you expected, and that --include-vendor is not the "
              "only thing standing between you and a finding.")
        return
    by_rule = defaultdict(list)
    for f in findings:
        by_rule[f["rule"]].append(f)
    for rule in RULE_NAMES:
        group = by_rule.get(rule)
        if not group:
            continue
        group.sort(key=lambda f: CONFIDENCE.index(f["confidence"]))
        counts = Counter(f["confidence"] for f in group)
        tally = ", ".join(f"{counts[c]} {c}" for c in CONFIDENCE if counts[c])
        print(f"\n{'═' * 72}\n{rule}  ({len(group)}: {tally})\n{'═' * 72}")
        print(f"{RULES[rule]}.")
        for f in group[:limit]:
            print(f"\n  [{f['confidence']}] {f['trigger']}")
            print(f"    on {f['owner_kind']} {f['owner']!r}, severity {f['severity']}")
            print(f"    {f['expression']}")
            print(f"    why: {f['why']}")
            print(f"    try: {f['hint']}")
        if len(group) > limit:
            print(f"\n  … and {len(group) - limit} more, see --json or --limit")
    total = Counter(f["confidence"] for f in findings)
    print(f"\n{'─' * 72}")
    print(f"{len(findings)} finding(s): "
          f"{total.get('high', 0)} high, {total.get('medium', 0)} medium, "
          f"{total.get('low', 0)} low confidence")


def cmd_check(args) -> int:
    z = connect_or_exit()
    est = load(z, args)

    findings = []
    for rule in RULE_NAMES:
        findings.extend(CHECKS[rule](est))
    cutoff = CONFIDENCE.index(args.min_confidence)
    findings = [f for f in findings if CONFIDENCE.index(f["confidence"]) <= cutoff]

    if args.json:
        print(json.dumps({
            "findings": findings,
            "examined": len(est.definitions),
            "totals": dict(Counter(f["rule"] for f in findings)),
        }, indent=2))
    else:
        print(f"Zabbix {z.version()} at {z.url}")
        print(f"{len(est.definitions)} trigger definition(s) examined.")
        render(findings, args.limit)

    if args.fail_on:
        wanted = {r.strip() for r in args.fail_on.split(",")}
        hit = [f for f in findings if "any" in wanted or f["rule"] in wanted]
        if hit:
            print(f"\nFAIL: {len(hit)} finding(s) matching {args.fail_on}",
                  file=sys.stderr)
            return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="lint trigger expressions")
    c.add_argument("--host", help="only triggers on hosts matching this name")
    c.add_argument("--template", help="only triggers on templates matching this name")
    c.add_argument("--min-confidence", choices=CONFIDENCE, default="low",
                   help="drop findings below this confidence (default: low)")
    c.add_argument("--include-vendor", action="store_true",
                   help="also lint vendor-supplied templates")
    c.add_argument("--include-inherited", action="store_true",
                   help="also lint inherited copies of template triggers")
    c.add_argument("--include-discovered", action="store_true",
                   help="also lint triggers created by low-level discovery")
    c.add_argument("--limit", type=int, default=12,
                   help="findings shown per rule (default: 12)")
    c.add_argument("--json", action="store_true", help="emit JSON")
    c.add_argument("--fail-on", metavar="RULE",
                   help="exit non-zero on findings from this rule; accepts a "
                        "comma-separated list, or 'any'")
    c.set_defaults(func=cmd_check)

    args = ap.parse_args()
    try:
        return args.func(args)
    except ZabbixError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
