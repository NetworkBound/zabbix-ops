#!/usr/bin/env bash
#
# Teach every agent about all HA nodes.
#
#   Run ON a Proxmox node:
#       ./update-agent-servers.sh --nodes 10.0.0.20,10.0.0.21 --dry-run
#       ./update-agent-servers.sh --nodes 10.0.0.20,10.0.0.21
#
# The step people miss when enabling Zabbix HA
# --------------------------------------------
# A Zabbix agent's `Server=` is an ALLOWLIST: it only answers connections from
# the addresses listed there. Stand up a second HA node and every agent will
# refuse it, because that address is new.
#
# The failover itself works perfectly. The standby takes the active role in
# seconds, and then every passive check fails with "network error" because the
# agents will not talk to it. You have a cluster that fails over into a blind
# spot — which is worse than no HA, because you believe you are covered.
#
# This adds each HA node address to `Server=` on every running container.
#
# What it deliberately does NOT change
# ------------------------------------
# `ServerActive=` is left alone when it points at a proxy. Agents behind a proxy
# must keep sending their active checks to that proxy — repointing them at the
# servers would bypass it and orphan the proxy's host assignments. The address
# is only added when `ServerActive` already names a server directly.
#
set -uo pipefail

NODES=""; DRY_RUN=0; ONLY=""; SKIP=""; RESTART=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --nodes)      NODES="$2"; shift 2 ;;
        --only)       ONLY="$2"; shift 2 ;;
        --skip)       SKIP="$2"; shift 2 ;;
        --dry-run)    DRY_RUN=1; shift ;;
        --no-restart) RESTART=0; shift ;;
        -h|--help)    sed -n '2,32p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[[ -n "$NODES" ]] || { echo "error: --nodes is required, e.g. --nodes 10.0.0.20,10.0.0.21" >&2; exit 2; }
command -v pct >/dev/null 2>&1 || { echo "error: run this on the Proxmox node itself." >&2; exit 1; }

IFS=',' read -r -a NODE_LIST <<< "$NODES"

in_list() { [[ -n "$2" && ",$2," == *",$1,"* ]]; }

CONF=/etc/zabbix/zabbix_agent2.conf
changed=0; already=0; skipped=0; noagent=0; failed=0

for ct in $(pct list | awk 'NR>1 && $2=="running"{print $1}'); do
    name=$(pct config "$ct" 2>/dev/null | sed -n 's/^hostname: //p'); name="${name:-ct$ct}"

    if [[ -n "$ONLY" ]] && ! in_list "$ct" "$ONLY"; then skipped=$((skipped+1)); continue; fi
    if in_list "$ct" "$SKIP"; then skipped=$((skipped+1)); continue; fi

    current=$(pct exec "$ct" -- grep -h '^Server=' "$CONF" 2>/dev/null | head -1 | cut -d= -f2-)
    if [[ -z "$current" ]]; then noagent=$((noagent+1)); continue; fi

    # Add only the addresses that are genuinely absent, preserving order.
    new="$current"
    added=""
    for node in "${NODE_LIST[@]}"; do
        node="${node// /}"
        [[ -z "$node" ]] && continue
        if [[ ",${new//[[:space:]]/}," != *",${node},"* ]]; then
            new="${new},${node}"
            added="${added} ${node}"
        fi
    done

    if [[ -z "$added" ]]; then
        already=$((already+1))
        continue
    fi

    printf '%-6s %-26s Server: %s -> %s\n' "$ct" "${name:0:26}" "$current" "$new"
    if [[ $DRY_RUN -eq 1 ]]; then changed=$((changed+1)); continue; fi

    if ! pct exec "$ct" -- sh -c "
        cp '$CONF' '$CONF.bak-ha' 2>/dev/null || true
        sed -i 's|^Server=.*|Server=${new}|' '$CONF'
    " 2>/dev/null; then
        echo "       ! failed to update"; failed=$((failed+1)); continue
    fi

    # ServerActive: only extend it when it already names a server directly.
    # If it points at a proxy, leaving it alone is the whole point.
    sa=$(pct exec "$ct" -- grep -h '^ServerActive=' "$CONF" 2>/dev/null | head -1 | cut -d= -f2-)
    if [[ -n "$sa" ]]; then
        for node in "${NODE_LIST[@]}"; do
            node="${node// /}"
            if [[ ",${sa//[[:space:]]/}," == *",${node},"* ]]; then
                # already names this node -> it talks to servers, so add the rest
                for other in "${NODE_LIST[@]}"; do
                    other="${other// /}"
                    [[ ",${sa//[[:space:]]/}," == *",${other},"* ]] || sa="${sa},${other}"
                done
                pct exec "$ct" -- sed -i "s|^ServerActive=.*|ServerActive=${sa}|" "$CONF" 2>/dev/null \
                    && echo "       ServerActive -> ${sa}"
                break
            fi
        done
    fi

    if [[ $RESTART -eq 1 ]]; then
        pct exec "$ct" -- systemctl restart zabbix-agent2 2>/dev/null \
            || echo "       ! agent restart failed"
    fi
    changed=$((changed+1))
done

echo
echo "==================== SUMMARY ===================="
echo "updated        : ${changed}"
echo "already correct: ${already}"
echo "no agent       : ${noagent}"
echo "skipped        : ${skipped}"
echo "failed         : ${failed}"
[[ $DRY_RUN -eq 1 ]] && echo && echo "DRY RUN — nothing was changed."
echo
echo "Do not forget the Zabbix PROXY: its own Server= must also list every HA"
echo "node, or it cannot deliver to whichever node is active."
exit $(( failed > 0 ? 1 : 0 ))
