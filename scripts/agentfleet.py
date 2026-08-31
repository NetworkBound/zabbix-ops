#!/usr/bin/env python3
"""Audit Zabbix agent configuration across a Proxmox fleet.

    ./scripts/agentfleet.py audit                     # report
    ./scripts/agentfleet.py audit --json              # machine-readable
    ./scripts/agentfleet.py audit --fail-on server    # non-zero exit, for a scheduled job
    ./scripts/agentfleet.py audit --only 100,101      # just these containers

Run this ON a Proxmox node. Each guest's agent configuration is read through
``pct exec``, so nothing has to be installed, opened or authenticated inside the
containers — the same approach ``deploy/ha/update-agent-servers.sh`` takes.

Why this exists
---------------
Agent configuration drifts away from what the server believes, and it drifts
silently. Two cases from the environment this was written for.

A second HA node was added, and every agent's ``Server=`` allowlist still named
only the original one. ``Server=`` is an allowlist: the agent refuses
connections from any address not in it. So the instant the standby took the
active role, every passive check failed with a network error. The cluster
failed over into a blind spot, which is worse than having no HA at all, because
by then you believe you are covered.

Separately, the SNMP community macro in Zabbix stopped matching what the
guests' snmpd actually answered, and a different monitoring system found it
before Zabbix did. Same shape of fault: the server's idea of a guest and the
guest's own configuration parted company, and nothing in the frontend compares
the two.

That is the gap. Zabbix only knows what it was told. The agent only knows what
is in its file. This reads both ends and reports where they disagree.

Read-only, deliberately. There is no ``--apply``, because the one repair this
would want to make already has a tool: ``deploy/ha/update-agent-servers.sh``
rewrites ``Server=`` across the fleet and has a ``--dry-run`` of its own. The
output below points at it rather than growing a second, subtly different
implementation of the same edit.

Containers only. ``pct exec`` does not reach VMs. ``qm guest-exec`` would, but
only where the QEMU guest agent is installed, which is exactly the population
least likely to have it. VMs are counted as unchecked rather than assumed
clean, because an audit that quietly skips half the fleet is worse than no
audit.

Severity means:

    high    Monitoring is not doing what someone believes it is doing.
    medium  Real operational cost, or a posture that will be questioned.
    low     Worth tidying. Not urgent.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import pathlib
import re
import shutil
import subprocess
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pve import parse_net_ip  # noqa: E402
from zbx import ZabbixError, connect_or_exit  # noqa: E402

SEVERITIES = ("high", "medium", "low")

CATEGORIES = ("hostname", "server", "serveractive", "version", "coverage", "tls")

#: The two agent configurations, in the order the agent packages install them.
#: Agent 2 wins when both are present, which happens on any guest upgraded from
#: the C agent without the old file being removed.
AGENT_CONFS = ("/etc/zabbix/zabbix_agent2.conf", "/etc/zabbix/zabbix_agentd.conf")

#: Directives worth reading. Anything not listed here is none of this tool's
#: business, and pulling the whole file back for fifty guests is wasteful.
DIRECTIVES = ("Server", "ServerActive", "Hostname", "HostnameItem", "HostMetadata",
              "HostInterface", "TLSConnect", "TLSAccept", "TLSPSKIdentity",
              "TLSPSKFile", "Include")

#: Zabbix host.tls_connect / tls_accept encodings.
TLS_MODES = {1: "unencrypted", 2: "psk", 4: "cert"}

#: Runs inside each container. One round trip per guest on purpose: pct exec
#: costs roughly a second of setup, and asking for one directive at a time
#: turned a fifty-container audit into a coffee break.
COLLECT_SH = r"""
DIRS='^[[:space:]]*(Server|ServerActive|Hostname|HostnameItem|HostMetadata|HostInterface|TLSConnect|TLSAccept|TLSPSKIdentity|TLSPSKFile|Include)[[:space:]]*='
scan() {
    [ -f "$1" ] || return 0
    echo "FILE $1"
    grep -E "$DIRS" "$1" 2>/dev/null | sed 's/^/D /'
}
echo "SYSHOSTNAME $(hostname 2>/dev/null)"
for c in /etc/zabbix/zabbix_agent2.conf /etc/zabbix/zabbix_agentd.conf; do
    [ -f "$c" ] || continue
    echo "AGENT $c"
    scan "$c"
    # Include= is processed where it appears and may redefine anything above it.
    # An audit that reads only the main file will happily report a Hostname the
    # agent is not using, which is the exact failure this tool is meant to find.
    grep -E '^[[:space:]]*Include[[:space:]]*=' "$c" 2>/dev/null | cut -d= -f2- |
    while read -r inc; do
        for p in $inc; do
            if [ -d "$p" ]; then
                for f in "$p"/*; do scan "$f"; done
            else
                for f in $p; do scan "$f"; done
            fi
        done
    done
done
for b in zabbix_agent2 zabbix_agentd; do
    if command -v "$b" >/dev/null 2>&1; then
        echo "VERSION $b $($b -V 2>/dev/null | head -1)"
    fi
done
for s in zabbix-agent2 zabbix-agent; do
    if systemctl cat "$s" >/dev/null 2>&1; then
        echo "SERVICE $s $(systemctl is-active "$s" 2>/dev/null)"
    fi
done
"""


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
# Pure parsing and comparison. No I/O below this line until the collectors.
# --------------------------------------------------------------------------
def parse_agent_conf(text: str) -> dict:
    """Parse Zabbix agent configuration text into ``{directive: [values]}``.

    Values are kept in file order, and a directive appearing more than once
    keeps every occurrence. The agent takes the last one, but the fact that
    there is more than one is itself worth reporting: with includes in play,
    the effective value depends on the order files happen to be read in.

    Leading whitespace is tolerated when parsing even though the agent itself
    rejects it. An audit that skipped an indented ``Hostname=`` would report the
    guest as unconfigured instead of as misconfigured, which sends the reader
    looking in the wrong place.
    """
    out: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or key not in DIRECTIVES:
            continue
        out.setdefault(key, []).append(value.strip())
    return out


def effective(conf: dict, key: str) -> str:
    """The value the agent will actually use: the last definition read."""
    values = conf.get(key) or []
    return values[-1] if values else ""


def split_addresses(value: str, key: str = "Server") -> list[str]:
    """Split a Server / ServerActive value into its individual entries.

    ``ServerActive`` uses ';' to separate HA clusters and ',' to separate the
    nodes within one cluster, so both are separators here. ``Server`` has no
    cluster concept and a ';' in it is a typo rather than a second address, so
    it is left embedded where it will be visible in the report.
    """
    seps = ",;" if key == "ServerActive" else ","
    parts = [value]
    for sep in seps:
        parts = [p for chunk in parts for p in chunk.split(sep)]
    return [p.strip() for p in parts if p.strip()]


def strip_port(entry: str) -> str:
    """Drop a trailing ``:port`` from a ServerActive entry.

    IPv6 literals are bracketed in this context, so a bare colon only ever means
    a port. Anything with more than one colon is left alone.
    """
    if entry.count(":") == 1:
        host, _, port = entry.partition(":")
        if port.isdigit():
            return host
    return entry


def covers(entry: str, address: str) -> bool:
    """Does one ``Server=`` entry admit connections from ``address``?

    Zabbix accepts a plain address, a CIDR network, or a last-octet range such
    as ``10.0.0.1-64``. All three are common in an allowlist, and treating a
    network as a literal string would report a correctly configured fleet as
    entirely broken.
    """
    entry, address = entry.strip(), address.strip()
    if not entry or not address:
        return False
    if entry == address:
        return True
    try:
        target = ipaddress.ip_address(address)
    except ValueError:
        return False
    if "/" in entry:
        try:
            return target in ipaddress.ip_network(entry, strict=False)
        except ValueError:
            return False
    if "-" in entry:
        base, _, last = entry.rpartition("-")
        if last.isdigit():
            try:
                start = ipaddress.ip_address(base)
            except ValueError:
                return False
            head = base.rsplit(".", 1)[0]
            try:
                end = ipaddress.ip_address(f"{head}.{last}")
            except ValueError:
                return False
            return start <= target <= end
    return False


def missing_addresses(entries: list[str], expected: list[str]) -> list[str]:
    """Which expected poller addresses the allowlist does not admit."""
    return [a for a in expected if not any(covers(e, a) for e in entries)]


def unresolvable(entries: list[str]) -> list[str]:
    """Entries that are names rather than addresses.

    A DNS name in ``Server=`` may well be correct, but this tool does no name
    resolution — it runs on a hypervisor whose resolver is not necessarily the
    guest's. Those entries are reported as unverified rather than counted as
    coverage, so the answer is never confidently wrong.
    """
    out = []
    for e in entries:
        bare = strip_port(e).split("/")[0].split("-")[0]
        try:
            ipaddress.ip_address(bare)
        except ValueError:
            out.append(e)
    return out


def hostname_status(agent_hostname: str, zabbix_host: str) -> str:
    """Compare the agent's ``Hostname=`` with the Zabbix host's technical name.

    Zabbix matches the name an active agent sends byte for byte. There is no
    normalisation: no case folding, no stripping of a domain suffix. A name that
    differs only in case is therefore a total failure that looks identical to a
    working one in the frontend, so it gets its own result rather than being
    lumped in with the obvious mismatches.

    Returns one of: match, case, suffix, mismatch, unset.
    """
    agent = (agent_hostname or "").strip()
    zbx = (zabbix_host or "").strip()
    if not agent:
        return "unset"
    if agent == zbx:
        return "match"
    if agent.lower() == zbx.lower():
        return "case"
    if agent.lower().split(".")[0] == zbx.lower().split(".")[0]:
        return "suffix"
    return "mismatch"


def parse_version(banner: str) -> str:
    """Pull ``6.0.14`` out of ``zabbix_agent2 (Zabbix) 6.0.14``."""
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", banner or "")
    return m.group(1) if m else ""


def branch(version: str) -> str:
    """The ``major.minor`` of a version, which is what compatibility follows."""
    parts = (version or "").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else ""


def normalise(name: str) -> str:
    """Match key for a guest or host name; same rule reconcile.py uses."""
    return (name or "").strip().lower().split(".")[0]


def tls_modes(value) -> set:
    """Decode a Zabbix tls_connect / tls_accept bitmask into mode names."""
    try:
        bits = int(value)
    except (TypeError, ValueError):
        return set()
    return {n for b, n in TLS_MODES.items() if bits & b}


def agent_tls_modes(conf: dict) -> tuple[str, set]:
    """The agent's outgoing and incoming encryption settings, as (connect, accept).

    The two are independent and are compared against opposite ends of the
    server's configuration: ``TLSAccept`` governs incoming passive checks and
    must admit the host's ``tls_connect``, while ``TLSConnect`` governs the
    agent's outgoing active checks and must be admitted by the host's
    ``tls_accept``. Collapsing them into one value hides a half-configured host,
    which is the state that actually breaks.

    Both default to unencrypted when absent, which is how a fleet ends up in
    clear text without anyone having chosen it.
    """
    connect = (effective(conf, "TLSConnect") or "unencrypted").strip().lower()
    raw = (effective(conf, "TLSAccept") or "unencrypted").strip().lower()
    accept = {m.strip() for m in raw.split(",") if m.strip()}
    return connect, accept


def match_guests(guests: list[dict], hosts: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Pair each container with the Zabbix host that represents it.

    Pairing is by interface address first and by name only as a fallback. The
    agent's own ``Hostname=`` is deliberately not the key: it is the thing being
    audited, and keying on it would make every hostname mismatch look like two
    unrelated records that simply never met.

    Returns (pairs, guests with no Zabbix host, hosts with no container).
    """
    by_ip: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for h in hosts:
        for addr in h["addresses"]:
            by_ip.setdefault(addr, h)
        by_name.setdefault(normalise(h["host"]), h)

    pairs, orphan_guests = [], []
    taken = set()
    for g in guests:
        h = (by_ip.get(g["address"]) if g["address"] else None) or by_name.get(normalise(g["name"]))
        if h is None:
            orphan_guests.append(g)
            continue
        taken.add(h["hostid"])
        pairs.append({"guest": g, "host": h})
    orphan_hosts = [h for h in hosts if h["hostid"] not in taken]
    return pairs, orphan_guests, orphan_hosts


def expected_addresses(host: dict, ha: list[str], proxy_addr: dict, extra: list[str]) -> list[str]:
    """Every address Zabbix might poll this host from.

    A host behind a proxy is polled by the proxy, not by the server, so its
    allowlist needs the proxy's address rather than the cluster's. A host the
    server polls directly needs every HA node, because any of them can be the
    active one within a minute of the current one stopping.
    """
    out = list(extra)
    if host["proxyid"] and host["proxyid"] in proxy_addr:
        out.append(proxy_addr[host["proxyid"]])
    elif not host["proxyid"]:
        out.extend(ha)
    return sorted({a for a in out if a})


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def check_hostname(pairs: list[dict], f: Findings) -> None:
    """Active checks are filed by name, and only by name."""
    mismatched, cased, suffixed, unset = [], [], [], []
    for p in pairs:
        g, h = p["guest"], p["host"]
        if not g["agent"]:
            continue
        status = hostname_status(g["hostname"], h["host"])
        line = f"CT {g['ctid']}  agent={g['hostname'] or '(unset)'}  zabbix={h['host']}"
        if status == "mismatch":
            mismatched.append(line)
        elif status == "case":
            cased.append(line)
        elif status == "suffix":
            suffixed.append(line)
        elif status == "unset":
            unset.append(f"CT {g['ctid']}  zabbix={h['host']}  system hostname={g['syshostname']}")

    if mismatched:
        f.add("hostname", "high",
              "Agent Hostname does not match the Zabbix host",
              "Active checks are filed against the name the agent sends. When no "
              "host carries that name the server discards the data, and the host "
              "sits at 'no data' while the agent is running perfectly. This is the "
              "first thing to check when an agent is up and the graphs are empty.",
              count=len(mismatched), evidence=mismatched[:10],
              fix="Set Hostname= in the agent config to the Zabbix host's technical "
                  "name, or rename the host. Passive checks keep working throughout, "
                  "which is why this hides so well.")
    if cased:
        f.add("hostname", "high",
              "Agent Hostname differs only in case",
              "Zabbix compares the name byte for byte and does not fold case, so "
              "this fails exactly as hard as a completely wrong name while looking "
              "correct to anyone reading the two side by side.",
              count=len(cased), evidence=cased[:10],
              fix="Make the two identical, including case.")
    if suffixed:
        f.add("hostname", "high",
              "Agent Hostname differs by a domain suffix",
              "One end is fully qualified and the other is not. There is no partial "
              "match: the server treats these as different hosts and drops the data.",
              count=len(suffixed), evidence=suffixed[:10],
              fix="Pick one form and use it at both ends.")
    if unset:
        f.add("hostname", "medium",
              "Agent has no Hostname set",
              "The agent falls back to HostnameItem, which defaults to the guest's "
              "own system hostname. That works until someone renames the guest, at "
              "which point active checks stop arriving with no configuration change "
              "having been made anywhere.",
              count=len(unset), evidence=unset[:10],
              fix="Set Hostname= explicitly to the Zabbix host name.")


def check_server(pairs: list[dict], f: Findings, ha: list[str], proxy_addr: dict,
                 extra: list[str], unknown_proxies: list[str],
                 inferred_proxies: list[str]) -> None:
    """Server= is an allowlist, and an incomplete one is a failover blind spot."""
    gaps, empty, unverified = [], [], []
    for p in pairs:
        g, h = p["guest"], p["host"]
        if not g["agent"]:
            continue
        entries = split_addresses(effective(g["conf"], "Server"), "Server")
        if not entries:
            empty.append(f"CT {g['ctid']}  {g['name']}")
            continue
        want = expected_addresses(h, ha, proxy_addr, extra)
        missing = missing_addresses(entries, want)
        names = unresolvable(entries)
        if missing:
            gaps.append({"ctid": g["ctid"], "name": g["name"], "host": h["host"],
                         "missing": missing, "current": entries})
        if names and missing:
            unverified.append(f"CT {g['ctid']}  unresolved entries: {', '.join(names)}")

    if gaps:
        by_address = Counter(a for gap in gaps for a in gap["missing"])
        f.add("server", "high",
              "Agents that will refuse a poller Zabbix may use",
              "Server= is an allowlist: the agent answers nobody else. Every "
              "address below is one Zabbix can legitimately poll from and these "
              "agents will reject. If it is an HA node, the failure appears only "
              "when that node becomes active, which is the moment you least want "
              "to discover it.",
              count=len(gaps),
              evidence=[f"{c} agent(s) missing {a}" for a, c in by_address.most_common()]
                       + [f"CT {gap['ctid']} {gap['name'][:22]}: missing "
                          f"{', '.join(gap['missing'])}" for gap in gaps[:10]],
              fix="deploy/ha/update-agent-servers.sh --nodes "
                  f"{','.join(sorted(by_address)) or '<addresses>'} --dry-run, then "
                  "run it again without --dry-run. This tool does not write.")
    if empty:
        f.add("server", "high",
              "Agents with an empty Server=",
              "No passive check can reach these at all. The agent is listening and "
              "refusing every connection it gets.",
              count=len(empty), evidence=empty[:10])
    if unverified:
        f.add("server", "low",
              "Allowlist entries this audit could not verify",
              "These are DNS names, not addresses. They may well be correct; this "
              "tool does no resolution, because the hypervisor's resolver is not "
              "necessarily the guest's and a confident wrong answer is worse than "
              "an admitted gap.",
              count=len(unverified), evidence=unverified[:10])
    if unknown_proxies:
        f.add("server", "medium",
              "Proxy poller address unknown, so its hosts were not fully checked",
              "Hosts behind a proxy are polled by the proxy, and the API does not "
              "expose the address an active proxy connects out from — only the "
              "address the server uses to reach a passive one. Without it the "
              "allowlist check for those hosts is incomplete.",
              count=len(unknown_proxies), evidence=unknown_proxies,
              fix="Re-run with --proxy-address NAME=10.0.0.30 for each proxy.")
    if inferred_proxies:
        f.add("server", "low",
              "Proxy poller address was inferred, not read",
              "Zabbix does not record where a proxy polls from, so this was taken "
              "from a host that happens to share the proxy's name. That convention "
              "is usually right and is not guaranteed. Every allowlist result for "
              "hosts behind these proxies rests on it.",
              count=len(inferred_proxies), evidence=inferred_proxies,
              fix="Confirm with --proxy-address NAME=ADDR to take the guess out.")


def check_serveractive(pairs: list[dict], f: Findings, known: list[str]) -> None:
    """ServerActive decides where active checks go, if they go anywhere."""
    stranded, absent = [], []
    for p in pairs:
        g = p["guest"]
        if not g["agent"]:
            continue
        entries = split_addresses(effective(g["conf"], "ServerActive"), "ServerActive")
        if not entries:
            absent.append(f"CT {g['ctid']}  {g['name']}")
            continue
        # A name is left alone: this tool does not resolve, so it cannot say.
        bad = [e for e in entries
               if not unresolvable([e]) and strip_port(e) not in known]
        if bad and len(bad) == len(entries):
            stranded.append(f"CT {g['ctid']}  {g['name'][:22]}  -> {', '.join(bad)}")

    if stranded:
        f.add("serveractive", "high",
              "ServerActive points only at addresses nothing answers on",
              "No address the agent is sending active checks to belongs to a known "
              "server, HA node or proxy. The agent keeps retrying and the data "
              "arrives nowhere. Usually a decommissioned server left in the file.",
              count=len(stranded), evidence=stranded[:10],
              fix="Point it at a current node or proxy. If the address is a VIP or "
                  "a proxy this tool does not know about, declare it with --known "
                  "or --proxy-address so this check can see it.")
    if absent:
        f.add("serveractive", "medium",
              "Agents with no ServerActive",
              "These agents do no active checks at all. Everything the templates "
              "collect actively is simply absent, and an item that is never "
              "polled looks the same as one that has nothing to report.",
              count=len(absent), evidence=absent[:10])


def check_version(pairs: list[dict], orphan_guests: list[dict], f: Findings,
                  server_version: str) -> None:
    """Version skew is invisible until a template starts using a newer key."""
    agents = [g for g in [p["guest"] for p in pairs] + orphan_guests if g["agent"]]
    versions = Counter(g["version"] or "unknown" for g in agents)
    if not agents:
        return

    if len(versions) > 1:
        f.add("version", "low" if len(versions) == 2 else "medium",
              "Agent versions are not uniform across the fleet",
              "A mixed fleet means a template change can work on some guests and "
              "return unsupported on others, and the difference is not visible "
              "anywhere in the frontend.",
              count=len(versions),
              evidence=[f"{c:>3} guest(s) on {v}" for v, c in versions.most_common()])

    sb = branch(server_version)
    if sb:
        behind = [g for g in agents
                  if g["version"] and _older(branch(g["version"]), sb)]
        if behind:
            by_v = Counter(g["version"] for g in behind)
            f.add("version", "medium",
                  "Agents older than the server's release branch",
                  f"The server is {server_version}. An older agent stays supported, "
                  "but it does not implement keys added since its branch, so any "
                  "item using one goes unsupported on exactly these guests and "
                  "nowhere else.",
                  count=len(behind),
                  evidence=[f"{c:>3} guest(s) on {v}" for v, c in by_v.most_common()]
                           + [f"CT {g['ctid']} {g['name'][:22]} {g['version']}"
                              for g in behind[:8]],
                  fix="Upgrade the agent package on those guests; "
                      "scripts/install-agent.sh installs the current one.")


def _older(a: str, b: str) -> bool:
    """Is branch ``a`` behind branch ``b``, comparing numerically."""
    def parts(s):
        return tuple(int(x) for x in s.split(".") if x.isdigit())
    pa, pb = parts(a), parts(b)
    return bool(pa and pb and pa < pb)


def check_coverage(pairs: list[dict], orphan_guests: list[dict], f: Findings,
                   skipped_vms: int) -> None:
    """Both directions of "is this thing actually monitored"."""
    unmonitored = [g for g in orphan_guests if g["agent"]]
    if unmonitored:
        f.add("coverage", "high",
              "Guests running an agent that Zabbix does not monitor",
              "The agent is installed and configured, so somebody meant for these "
              "to be monitored. No host exists, so nothing is collected and no "
              "trigger can fire. This is the failure mode that produces no alert "
              "of any kind, including no alert about itself.",
              count=len(unmonitored),
              evidence=[f"CT {g['ctid']}  {g['name'][:24]:<24} "
                        f"{g['address'] or '(dhcp)'}" for g in unmonitored[:12]],
              fix="Create the host, or let auto-registration do it — "
                  "docs/auto-registration.md. Check HostMetadata matches the action.")

    noagent = [p for p in pairs if not p["guest"]["agent"] and p["host"]["agent_iface"]]
    if noagent:
        f.add("coverage", "high",
              "Zabbix hosts with an agent interface but no agent installed",
              "The host has an agent interface, so the templates on it expect an "
              "agent to answer on 10050. Nothing is listening. Every agent item is "
              "unsupported and the host is permanently unreachable, which is loud "
              "enough to get muted rather than fixed.",
              count=len(noagent),
              evidence=[f"CT {p['guest']['ctid']}  {p['host']['host'][:30]}"
                        for p in noagent[:12]],
              fix="Install the agent with scripts/install-agent.sh, or remove the "
                  "agent interface and its templates if the guest is monitored "
                  "another way.")

    noconf = [p for p in pairs if not p["guest"]["agent"] and not p["host"]["agent_iface"]]
    if noconf:
        f.add("coverage", "low",
              "Monitored guests with no agent",
              "Monitored by some other means — SNMP, an HTTP check, or only the "
              "hypervisor's view of them. Listed so the absence of an agent is a "
              "decision on the record rather than an oversight nobody noticed.",
              count=len(noconf),
              evidence=[f"CT {p['guest']['ctid']}  {p['host']['host'][:30]}"
                        for p in noconf[:10]])

    stale = [p for p in pairs if p["guest"].get("stale_confs")]
    if stale:
        f.add("coverage", "low",
              "Two agent configurations present, only one in use",
              "A leftover config from the other agent is still on disk. Nothing "
              "reads it, but anyone grepping for Server= across the fleet will "
              "find it and reach the wrong conclusion, and so will any tool that "
              "does not check which service is running.",
              count=len(stale),
              evidence=[f"CT {p['guest']['ctid']}  using {p['guest']['conf_path']}, "
                        f"stale: {', '.join(p['guest']['stale_confs'])}"
                        for p in stale[:10]],
              fix="Remove the unused file once you have confirmed which service "
                  "is running.")

    if skipped_vms:
        f.add("coverage", "low",
              "Virtual machines were not inspected",
              "pct exec only reaches containers. These guests may be perfectly "
              "configured or completely wrong; this audit does not know, and says "
              "so rather than counting them as clean.",
              count=skipped_vms)


def check_tls(pairs: list[dict], f: Findings) -> None:
    """The two ends have to agree, and disagreement is worse than plain text."""
    mismatch, plain, encrypted = [], [], []
    for p in pairs:
        g, h = p["guest"], p["host"]
        if not g["agent"]:
            continue
        a_connect, a_accept = agent_tls_modes(g["conf"])
        s_connect = tls_modes(h["tls_connect"])
        s_accept = tls_modes(h["tls_accept"])
        if a_accept == {"unencrypted"} and a_connect == "unencrypted" \
                and s_connect == {"unencrypted"}:
            plain.append(f"CT {g['ctid']}  {g['name']}")
            continue
        encrypted.append(g["ctid"])
        # Passive: the server dials with tls_connect, the agent must accept it.
        if s_connect and not (s_connect & a_accept):
            mismatch.append(f"CT {g['ctid']}  {g['name'][:20]:<20} passive: server "
                            f"connects {'+'.join(sorted(s_connect))}, agent accepts "
                            f"{'+'.join(sorted(a_accept))}")
        # Active: the agent dials with TLSConnect, the server must accept it.
        if s_accept and a_connect not in s_accept:
            mismatch.append(f"CT {g['ctid']}  {g['name'][:20]:<20} active: agent "
                            f"connects {a_connect}, server accepts "
                            f"{'+'.join(sorted(s_accept))}")

    if mismatch:
        f.add("tls", "high",
              "Agent and server disagree about encryption",
              "The server opens the connection one way and the agent will not "
              "accept it that way. Every passive check on these hosts fails, and "
              "the error names TLS rather than the configuration that caused it.",
              count=len(mismatch), evidence=mismatch[:10],
              fix="Make the host's Connect-to-host setting match the agent's "
                  "TLSAccept, and confirm the PSK identity is the same at both ends.")
    if plain and not encrypted:
        f.add("tls", "medium",
              "No agent uses encrypted transport",
              "All agent traffic is unauthenticated clear text. Reading the metrics "
              "matters less than the other direction: without a PSK anything on the "
              "path can impersonate a host to the server, or the server to a host.",
              count=len(plain),
              fix="PSK is the low-effort option: one key per host, TLSConnect and "
                  "TLSAccept on the agent, the matching identity on the host object.")
    elif plain:
        f.add("tls", "low",
              "Some agents still unencrypted",
              f"{len(encrypted)} guest(s) have TLS configured and these do not, so "
              "the fleet is halfway through a migration somebody stopped.",
              count=len(plain), evidence=plain[:10])


CHECKS = ("hostname", "server", "serveractive", "version", "coverage", "tls")


def evaluate(guests: list[dict], hosts: list[dict], ha: list[str], proxy_addr: dict,
             extra: list[str], server_version: str, unknown_proxies: list[str],
             inferred_proxies: list[str], skipped_vms: int,
             only: str = None) -> tuple[Findings, dict]:
    """Run every check. Pure: takes collected facts, returns findings."""
    f = Findings()
    pairs, orphan_guests, orphan_hosts = match_guests(guests, hosts)
    known = sorted(set(ha) | set(extra) | set(proxy_addr.values()))
    selected = [only] if only else list(CHECKS)

    if "hostname" in selected:
        check_hostname(pairs, f)
    if "server" in selected:
        check_server(pairs, f, ha, proxy_addr, extra, unknown_proxies, inferred_proxies)
    if "serveractive" in selected:
        check_serveractive(pairs, f, known)
    if "version" in selected:
        check_version(pairs, orphan_guests, f, server_version)
    if "coverage" in selected:
        check_coverage(pairs, orphan_guests, f, skipped_vms)
    if "tls" in selected:
        check_tls(pairs, f)

    context = {
        "matched": len(pairs),
        "guests_with_agent": sum(1 for g in guests if g["agent"]),
        "guests_inspected": len(guests),
        "zabbix_hosts": len(hosts),
        "unmatched_hosts": len(orphan_hosts),
        "ha_addresses": ha,
        "known_addresses": known,
        "server_version": server_version,
    }
    return f, context


# --------------------------------------------------------------------------
# Collection: Proxmox side
# --------------------------------------------------------------------------
class FleetError(RuntimeError):
    """A local failure that stops the audit before it starts."""


def require_pct() -> None:
    """Refuse clearly rather than failing somewhere deep in a subprocess.

    Being run from a laptop is the normal first mistake, and 'pct: command not
    found' fifty times over is a poor way to learn that.
    """
    if shutil.which("pct"):
        return
    raise FleetError(
        "pct not found — this tool must run ON a Proxmox node.\n"
        "It reads each container's agent config with 'pct exec', which needs no\n"
        "credentials inside the guests and therefore only works from the host.\n\n"
        "    scp scripts/agentfleet.py scripts/zbx.py scripts/pve.py root@pve:/root/\n"
        "    ssh root@pve 'ZBX_URL=... ZBX_USER=... ZBX_PASS=... python3 "
        "/root/agentfleet.py audit'"
    )


def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           check=False)
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError as e:
        return 127, str(e)
    return p.returncode, p.stdout


def pct_list() -> list[dict]:
    """Every container on this node, with its running state."""
    rc, out = _run(["pct", "list"], 60)
    if rc != 0:
        raise FleetError("pct list failed — is this a Proxmox node, and are you root?")
    guests = []
    for line in out.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 2 and cols[0].isdigit():
            guests.append({"ctid": cols[0], "status": cols[1]})
    return guests


def qm_count() -> int:
    """How many VMs exist, purely so the report can admit to skipping them."""
    rc, out = _run(["qm", "list"], 60)
    if rc != 0:
        return 0
    return sum(1 for line in out.splitlines()[1:] if line.split()[:1])


def pct_meta(ctid: str) -> tuple[str, str]:
    """Hostname and configured address from the container's own config.

    parse_net_ip is reused from pve.py: 'pct config' prints the netN string in
    exactly the format the API returns, so the DHCP and manual cases are already
    handled correctly there.
    """
    rc, out = _run(["pct", "config", ctid], 30)
    if rc != 0:
        return f"ct{ctid}", ""
    name, address = f"ct{ctid}", ""
    for line in out.splitlines():
        key, _, value = line.partition(":")
        if key == "hostname":
            name = value.strip() or name
        elif key.startswith("net") and not address:
            address = parse_net_ip(value.strip()).split("/")[0]
    return name, address


#: Which configuration file each agent's service reads. A guest upgraded from
#: the C agent keeps both files, and reading the wrong one produces a confident
#: report about a file nothing is using.
SERVICE_CONF = {"zabbix-agent2": AGENT_CONFS[0], "zabbix-agent": AGENT_CONFS[1]}


def choose_conf(configs: dict, services: dict) -> str:
    """Which of the two agent configurations is the live one.

    The running service decides. Only when nothing is running does the file
    itself get a vote, and then agent 2 wins because that is what the current
    packages install; the other file is almost always a leftover from before an
    upgrade, still full of the addresses of whatever the fleet used to talk to.
    """
    for service, path in SERVICE_CONF.items():
        if services.get(service) == "active" and path in configs:
            return path
    for path in AGENT_CONFS:
        if path in configs:
            return path
    return ""


def collect_guest(ctid: str, timeout: int) -> dict:
    """Read one container's agent configuration through pct exec."""
    name, address = pct_meta(ctid)
    guest = {"ctid": ctid, "name": name, "address": address, "agent": False,
             "conf": {}, "conf_path": "", "stale_confs": [], "files": [],
             "version": "", "service": "", "syshostname": "", "hostname": "",
             "error": ""}

    rc, out = _run(["pct", "exec", ctid, "--", "sh", "-c", COLLECT_SH], timeout)
    if rc == 124:
        guest["error"] = "timed out"
        return guest
    if rc == 127:
        guest["error"] = "pct exec unavailable"
        return guest

    configs: dict[str, list[str]] = {}
    services: dict[str, str] = {}
    current = ""
    for line in out.splitlines():
        tag, _, rest = line.partition(" ")
        if tag == "AGENT":
            current = rest.strip()
            configs.setdefault(current, [])
        elif tag == "D" and current:
            configs[current].append(rest)
        elif tag == "FILE":
            guest["files"].append(rest)
        elif tag == "SYSHOSTNAME":
            guest["syshostname"] = rest.strip()
        elif tag == "VERSION" and not guest["version"]:
            guest["version"] = parse_version(rest)
        elif tag == "SERVICE":
            unit, _, state = rest.strip().partition(" ")
            services[unit] = state
            if state == "active" and not guest["service"]:
                guest["service"] = unit

    chosen = choose_conf(configs, services)
    guest["conf_path"] = chosen
    guest["conf"] = parse_agent_conf("\n".join(configs.get(chosen, [])))
    guest["stale_confs"] = [p for p in configs if p != chosen]
    guest["agent"] = bool(chosen)
    guest["hostname"] = effective(guest["conf"], "Hostname")
    return guest


def collect_fleet(ctids: list[str], jobs: int, timeout: int) -> list[dict]:
    """Read the fleet in parallel. pct exec is nearly all setup latency."""
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(collect_guest, c, timeout): c for c in ctids}
        for fut in concurrent.futures.as_completed(futures):
            ctid = futures[fut]
            try:
                results[ctid] = fut.result()
            except Exception as e:  # noqa: BLE001 - one bad guest must not stop the audit
                results[ctid] = {"ctid": ctid, "name": f"ct{ctid}", "address": "",
                                 "agent": False, "conf": {}, "conf_path": "",
                                 "stale_confs": [], "files": [], "version": "",
                                 "service": "", "syshostname": "", "hostname": "",
                                 "error": str(e)}
    return [results[c] for c in ctids]


# --------------------------------------------------------------------------
# Collection: Zabbix side
# --------------------------------------------------------------------------
def zabbix_hosts(z, exclude_groups: list[str]) -> list[dict]:
    hosts = z.call("host.get", {
        "output": ["hostid", "host", "name", "status", "proxyid", "monitored_by",
                   "tls_connect", "tls_accept"],
        "selectInterfaces": ["ip", "dns", "useip", "type", "main"],
        "selectHostGroups": ["name"],
        "sortfield": "host",
    })
    excluded = set(exclude_groups or [])
    out = []
    for h in hosts:
        groups = [g["name"] for g in h.get("hostgroups", h.get("groups", []))]
        if excluded & set(groups):
            continue
        ifaces = h.get("interfaces") or []
        addresses = [i["ip"] for i in ifaces if i.get("useip") == "1" and i.get("ip")]
        out.append({
            "hostid": h["hostid"],
            "host": h["host"],
            "enabled": h["status"] == "0",
            "addresses": addresses,
            "agent_iface": any(i.get("type") == "1" for i in ifaces),
            # monitored_by: 0 server, 1 proxy, 2 proxy group.
            "proxyid": h.get("proxyid") if h.get("monitored_by") in ("1", "2") else "",
            "tls_connect": h.get("tls_connect"),
            "tls_accept": h.get("tls_accept"),
            "groups": groups,
        })
    return out


def ha_addresses(z) -> list[str]:
    """Every HA node's address.

    A node row with an empty name is what a standalone server registers, so the
    name is what proves HA was actually configured — see ha.py.
    """
    try:
        nodes = z.call("hanode.get", {"output": "extend"})
    except ZabbixError:
        return []
    return sorted({n["address"] for n in nodes if n.get("name") and n.get("address")})


def proxy_addresses(z, overrides: dict, hosts: list[dict]) -> tuple[dict, list[str], list[str]]:
    """Map proxyid to the address that proxy polls its hosts from.

    Zabbix does not record this anywhere. ``proxy.address`` is where the
    *server* connects to reach a passive proxy; for an active proxy it is
    meaningless, because the proxy dials out and the server therefore never
    needs to know where it lives. Yet that address is exactly what has to be in
    the allowlist of every guest behind it.

    Three sources, in decreasing order of trust: an explicit --proxy-address,
    the proxy's own recorded address where that is a real one, and finally the
    interface of a Zabbix host named after the proxy. The last is an inference
    from a convention rather than a fact, so it is reported as such — a proxy
    that cannot be placed at all is named in the output rather than guessed at.

    Returns (proxyid to address, proxies with no address, notes about inferences).
    """
    try:
        proxies = z.call("proxy.get", {"output": ["proxyid", "name", "address",
                                                  "local_address", "operating_mode"]})
    except ZabbixError:
        return {}, [], []
    by_name = {normalise(h["host"]): h for h in hosts}
    known, unknown, inferred = {}, [], []
    for p in proxies:
        override = overrides.get(p["name"])
        if override:
            known[p["proxyid"]] = override
            continue
        # local_address is what a proxy in a group advertises to active agents,
        # which is precisely the address wanted here when it is set at all.
        candidate = (p.get("local_address") or "").strip()
        if not candidate and p.get("operating_mode") == "1":
            candidate = (p.get("address") or "").strip()
        if not _routable(candidate):
            twin = by_name.get(normalise(p["name"]))
            candidate = twin["addresses"][0] if twin and twin["addresses"] else ""
            if _routable(candidate):
                inferred.append(f"{p['name']} assumed to poll from {candidate}, "
                                f"taken from the Zabbix host of the same name")
        if _routable(candidate):
            known[p["proxyid"]] = candidate
        else:
            unknown.append(p["name"])
    return known, unknown, inferred


def _routable(address: str) -> bool:
    """A loopback address in a proxy record means "not recorded", not "here"."""
    address = (address or "").strip()
    if not address or address == "localhost":
        return False
    try:
        return not ipaddress.ip_address(address).is_loopback
    except ValueError:
        return True


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def render(f: Findings, context: dict) -> None:
    print(f"\n{context['guests_inspected']} container(s) inspected, "
          f"{context['guests_with_agent']} with an agent, "
          f"{context['matched']} paired with a Zabbix host "
          f"({context['zabbix_hosts']} host(s) in scope)")
    if context["ha_addresses"]:
        print(f"HA nodes: {', '.join(context['ha_addresses'])}")
    print(f"Known poller addresses: {', '.join(context['known_addresses']) or 'none'}")

    if not f.items:
        print("\nNo findings. Confirm the API account can see every host group "
              "before believing it.")
        return
    for sev in SEVERITIES:
        group = f.by_severity(sev)
        if not group:
            continue
        print(f"\n{'=' * 72}\n{sev.upper()}  ({len(group)})\n{'=' * 72}")
        for item in group:
            n = f"  [{item['count']}]" if item["count"] is not None else ""
            print(f"\n{item['title']}{n}")
            print(f"  {item['detail']}")
            for e in item["evidence"]:
                print(f"    - {e}")
            if item["fix"]:
                print(f"  fix: {item['fix']}")
    t = Counter(i["severity"] for i in f.items)
    print(f"\n{'-' * 72}")
    print(f"{len(f.items)} finding(s): {t.get('high', 0)} high, "
          f"{t.get('medium', 0)} medium, {t.get('low', 0)} low")
    print("This tool writes nothing. deploy/ha/update-agent-servers.sh makes the "
          "Server= change.")


def parse_list(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def parse_proxy_overrides(values: list[str]) -> dict:
    out = {}
    for v in values or []:
        name, sep, addr = v.partition("=")
        if not sep or not name.strip() or not addr.strip():
            raise FleetError(f"--proxy-address wants NAME=ADDRESS, got {v!r}")
        out[name.strip()] = addr.strip()
    return out


def cmd_audit(args) -> int:
    require_pct()

    guests_meta = pct_list()
    only, skip = set(parse_list(args.only)), set(parse_list(args.skip))
    ctids = [g["ctid"] for g in guests_meta
             if g["status"] == "running"
             and (not only or g["ctid"] in only)
             and g["ctid"] not in skip]
    if not ctids:
        print("error: no running containers selected.", file=sys.stderr)
        return 1

    overrides = parse_proxy_overrides(args.proxy_address)
    z = connect_or_exit()

    # Say what is about to happen before doing it. The read is harmless, but the
    # habit is the same one that makes the writing tools in this repo safe.
    if not args.json:
        print(f"Reading {' and '.join(AGENT_CONFS)} from {len(ctids)} running "
              f"container(s) via pct exec.")
        print(f"Comparing against {z.url}. Read-only at both ends; nothing is "
              "written to a guest or to Zabbix.")

    guests = collect_fleet(ctids, args.jobs, args.timeout)
    failed = [g for g in guests if g["error"]]
    if failed and not args.json:
        for g in failed:
            print(f"  ! CT {g['ctid']}: {g['error']}", file=sys.stderr)

    try:
        hosts = zabbix_hosts(z, args.exclude_group)
        ha = ha_addresses(z)
        proxy_addr, unknown_proxies, inferred = proxy_addresses(z, overrides, hosts)
        server_version = z.version()
    except ZabbixError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    f, context = evaluate(guests, hosts, ha, proxy_addr, args.known,
                          server_version, unknown_proxies, inferred,
                          qm_count() if not args.no_vm_note else 0,
                          only=args.only_check)

    if args.json:
        print(json.dumps({
            "findings": f.items,
            "context": context,
            "totals": dict(Counter(i["severity"] for i in f.items)),
            "categories": dict(Counter(i["category"] for i in f.items)),
            "guests": [{k: v for k, v in g.items() if k != "conf"} for g in guests],
        }, indent=2))
    else:
        render(f, context)

    for want in args.fail_on:
        if want in SEVERITIES:
            threshold = SEVERITIES.index(want)
            hit = [i for i in f.items if SEVERITIES.index(i["severity"]) <= threshold]
        else:
            hit = [i for i in f.items if i["category"] == want]
        if hit:
            print(f"\nFAIL: {len(hit)} finding(s) matching {want}", file=sys.stderr)
            return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    a = sub.add_parser("audit", help="compare every agent config against Zabbix",
                       description=__doc__,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--only", default="", metavar="CTID,...",
                   help="inspect only these containers")
    a.add_argument("--skip", default="", metavar="CTID,...",
                   help="skip these containers")
    a.add_argument("--only-check", choices=CATEGORIES, help="run one check")
    a.add_argument("--known", nargs="*", default=[], metavar="ADDR",
                   help="extra addresses Zabbix legitimately polls or receives from "
                        "-- a VIP, or a proxy this tool cannot discover")
    a.add_argument("--proxy-address", nargs="*", default=[], metavar="NAME=ADDR",
                   help="the address a proxy polls its hosts from; Zabbix does not "
                        "record this for an active proxy")
    a.add_argument("--exclude-group", nargs="*", default=[], metavar="GROUP",
                   help="Zabbix host groups to ignore -- kit that is not a guest")
    a.add_argument("--jobs", type=int, default=8, metavar="N",
                   help="containers to read at once (default 8)")
    a.add_argument("--timeout", type=int, default=30, metavar="S",
                   help="seconds to wait for one container (default 30)")
    a.add_argument("--no-vm-note", action="store_true",
                   help="do not report that VMs were skipped")
    a.add_argument("--json", action="store_true", help="emit JSON")
    a.add_argument("--fail-on", nargs="*", default=[],
                   choices=list(SEVERITIES) + list(CATEGORIES),
                   help="exit non-zero on findings in these categories, or at this "
                        "severity or above")
    args = ap.parse_args()

    try:
        return cmd_audit(args)
    except FleetError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
