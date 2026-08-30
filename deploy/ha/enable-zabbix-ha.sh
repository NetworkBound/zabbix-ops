#!/usr/bin/env bash
#
# Turn a standalone Zabbix server into an HA cluster node.
#
#   On node A:  sudo ./enable-zabbix-ha.sh --name zbx-site-a --address 10.0.0.20
#   On node B:  sudo ./enable-zabbix-ha.sh --name zbx-site-b --address 10.0.1.20
#
# All that HA requires is that each server has a unique HANodeName and a
# NodeAddress its peers can reach. They coordinate through the shared database:
# the active node writes a heartbeat, and a standby takes over if that heartbeat
# goes stale. There is no separate cluster protocol and nothing to install.
#
# Two consequences worth internalising before you rely on it:
#
#   * Every node must point at the SAME database. HA does not replicate data.
#     Two servers against two separate databases is not a cluster, it is two
#     monitoring systems that will both alert you.
#   * Failover takes roughly a minute (the heartbeat interval plus the failover
#     delay). It is not instant, and it is not meant to be.
#
set -euo pipefail

NODE_NAME=""; NODE_ADDRESS=""; NODE_PORT="10051"; CONF="/etc/zabbix/zabbix_server.conf"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)    NODE_NAME="$2"; shift 2 ;;
        --address) NODE_ADDRESS="$2"; shift 2 ;;
        --port)    NODE_PORT="$2"; shift 2 ;;
        --conf)    CONF="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "error: run as root." >&2; exit 1; }
[[ -n "$NODE_NAME" ]] || { echo "error: --name is required and must be unique per node." >&2; exit 2; }
[[ -n "$NODE_ADDRESS" ]] || { echo "error: --address is required (peers connect to it)." >&2; exit 2; }
[[ -f "$CONF" ]] || { echo "error: $CONF not found." >&2; exit 2; }

echo "==> Configuring this server as HA node '${NODE_NAME}' at ${NODE_ADDRESS}:${NODE_PORT}"

# Guard against the mistake that produces a split cluster: two nodes sharing a
# name silently fight over the active role.
CURRENT=$(grep -oP '^HANodeName=\K.*' "$CONF" 2>/dev/null || true)
if [[ -n "$CURRENT" && "$CURRENT" != "$NODE_NAME" ]]; then
    echo "    note: HANodeName is currently '${CURRENT}', changing to '${NODE_NAME}'"
fi

set_kv() {  # set_kv KEY VALUE — replace the active line, or the commented one, or append
    local key="$1" val="$2"
    if grep -qE "^${key}=" "$CONF"; then
        [[ $DRY_RUN -eq 1 ]] && { echo "  [dry-run] set ${key}=${val}"; return; }
        sed -i -E "s|^${key}=.*|${key}=${val}|" "$CONF"
    elif grep -qE "^#[[:space:]]*${key}=" "$CONF"; then
        [[ $DRY_RUN -eq 1 ]] && { echo "  [dry-run] uncomment ${key}=${val}"; return; }
        sed -i -E "s|^#[[:space:]]*${key}=.*|${key}=${val}|" "$CONF"
    else
        [[ $DRY_RUN -eq 1 ]] && { echo "  [dry-run] append ${key}=${val}"; return; }
        printf '\n%s=%s\n' "$key" "$val" >> "$CONF"
    fi
    echo "    ${key}=${val}"
}

if [[ $DRY_RUN -eq 0 ]]; then
    cp "$CONF" "${CONF}.bak-$(date +%Y%m%d-%H%M%S)"
fi

set_kv HANodeName "$NODE_NAME"
set_kv NodeAddress "${NODE_ADDRESS}:${NODE_PORT}"

if [[ $DRY_RUN -eq 1 ]]; then
    echo
    echo "Dry run — nothing changed."
    exit 0
fi

echo "    restarting zabbix-server"
systemctl restart zabbix-server
sleep 12

if systemctl is-active --quiet zabbix-server; then
    echo "    zabbix-server is running"
    grep -iE 'HA manager|node .* (started|became)' /var/log/zabbix/zabbix_server.log 2>/dev/null | tail -3 || true
else
    echo "error: zabbix-server failed to start. Recent log:" >&2
    tail -20 /var/log/zabbix/zabbix_server.log >&2 2>/dev/null || true
    echo "    restore with: cp ${CONF}.bak-* ${CONF} && systemctl restart zabbix-server" >&2
    exit 1
fi

cat <<EOF

Node configured. Check the cluster from either node:

    python3 scripts/ha.py

You want to see exactly one 'active' and at least one 'standby'. Two actives
means the nodes cannot see each other's heartbeats through the database — check
that both really are pointed at the same DBHost.
EOF
