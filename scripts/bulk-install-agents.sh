#!/usr/bin/env bash
#
# Install Zabbix Agent 2 into every running LXC container on a Proxmox node.
#
#   Run ON the Proxmox node:
#       ZBX_SERVER=10.0.0.20 ./bulk-install-agents.sh
#
#   Preview first (always do this):
#       ZBX_SERVER=10.0.0.20 ./bulk-install-agents.sh --dry-run
#
#   Limit or exclude:
#       ./bulk-install-agents.sh --only 101,102,105
#       ./bulk-install-agents.sh --skip 113,120
#
# Uses `pct exec`, so it needs no SSH keys or credentials inside the guests.
# Each container is named in Zabbix after its Proxmox hostname.
#
# One container failing never stops the run; failures are collected and
# reported at the end with a non-zero exit.
#
set -uo pipefail

ZBX_SERVER="${ZBX_SERVER:-}"
DRY_RUN=0
ONLY=""
SKIP=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --only)    ONLY="$2"; shift 2 ;;
        --skip)    SKIP="$2"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "${ZBX_SERVER}" ]]; then
    echo "error: set ZBX_SERVER to your Zabbix server or proxy address." >&2
    exit 2
fi
if ! command -v pct >/dev/null 2>&1; then
    echo "error: 'pct' not found — run this on the Proxmox node itself." >&2
    exit 1
fi

in_list() {  # in_list <needle> <comma,list>
    [[ -z "$2" ]] && return 1
    [[ ",$2," == *",$1,"* ]]
}

mapfile -t CTIDS < <(pct list | awk 'NR>1 && $2=="running" {print $1}')
if [[ ${#CTIDS[@]} -eq 0 ]]; then
    echo "No running containers found."
    exit 0
fi

echo "==> ${#CTIDS[@]} running container(s) on $(hostname)"
declare -a PLANNED=() FAILED=() SKIPPED=()

for id in "${CTIDS[@]}"; do
    name="$(pct config "$id" | sed -n 's/^hostname: //p')"
    name="${name:-ct$id}"
    if [[ -n "$ONLY" ]] && ! in_list "$id" "$ONLY"; then
        SKIPPED+=("$id/$name (not in --only)"); continue
    fi
    if in_list "$id" "$SKIP"; then
        SKIPPED+=("$id/$name (--skip)"); continue
    fi
    PLANNED+=("$id|$name")
done

echo "==> ${#PLANNED[@]} to install, ${#SKIPPED[@]} skipped"
for p in "${PLANNED[@]}"; do echo "    ${p%%|*}  ${p##*|}"; done

if [[ ${DRY_RUN} -eq 1 ]]; then
    echo
    echo "DRY RUN — nothing was changed."
    exit 0
fi

for p in "${PLANNED[@]}"; do
    id="${p%%|*}"; name="${p##*|}"
    echo
    echo "======== ${id} / ${name} ========"
    # Push the single-host installer in and run it. Written to /tmp inside the
    # guest so nothing is left behind on a failure.
    if ! pct push "$id" "${SCRIPT_DIR}/install-agent.sh" /tmp/install-agent.sh --perms 0755 2>/dev/null; then
        echo "    FAILED: could not push installer"
        FAILED+=("$id/$name (push)")
        continue
    fi
    if pct exec "$id" -- env ZBX_SERVER="${ZBX_SERVER}" /tmp/install-agent.sh "$name"; then
        echo "    OK"
    else
        echo "    FAILED"
        FAILED+=("$id/$name (install)")
    fi
    pct exec "$id" -- rm -f /tmp/install-agent.sh 2>/dev/null || true
done

echo
echo "==================== SUMMARY ===================="
echo "installed : $(( ${#PLANNED[@]} - ${#FAILED[@]} ))"
echo "failed    : ${#FAILED[@]}"
for f in "${FAILED[@]:-}"; do [[ -n "$f" ]] && echo "            $f"; done
echo "skipped   : ${#SKIPPED[@]}"

if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo
    echo "Re-run just the failures with --only <ids>."
    exit 1
fi
echo
echo "New hosts appear in Zabbix once auto-registration fires (usually < 2 min)."
