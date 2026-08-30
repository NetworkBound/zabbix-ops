#!/usr/bin/env bash
#
# A floating IP that follows the active Zabbix HA node.
#
#   On each node:
#       sudo ./setup-vip.sh --vip 10.0.0.25 --interface eth0 --vrid 71
#
# Why a VIP is not optional once you have proxies
# ------------------------------------------------
# Zabbix agents accept a comma-separated `Server=` list, so they can be taught
# about every HA node and need nothing else.
#
# **A Zabbix proxy cannot.** Its `Server` parameter takes exactly one address —
# `zabbix_proxy` refuses to start with "must not contain comma". So a proxy is
# nailed to one node. When the other node takes the active role, the proxy has
# nowhere to deliver, and every host behind it goes dark.
#
# On an estate where the proxy carries most of the hosts, that is the difference
# between HA and the appearance of HA: failover succeeds, and takes the majority
# of your monitoring with it.
#
# The fix is an address that moves. keepalived runs on both nodes; a track
# script asks the local `zabbix_server` whether it currently holds the active
# role, and the VIP follows whoever says yes. The proxy points at the VIP and
# stops caring which node is running.
#
set -euo pipefail

VIP=""; IFACE="eth0"; VRID="71"; PASS=""; DRY_RUN=0
CONF="/etc/zabbix/zabbix_server.conf"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --vip)       VIP="$2"; shift 2 ;;
        --interface) IFACE="$2"; shift 2 ;;
        --vrid)      VRID="$2"; shift 2 ;;
        --pass)      PASS="$2"; shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        -h|--help)   sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "error: run as root." >&2; exit 1; }
[[ -n "$VIP" ]] || { echo "error: --vip is required." >&2; exit 2; }
[[ -f "$CONF" ]] || { echo "error: $CONF not found — is this a Zabbix server node?" >&2; exit 2; }

NODE_NAME=$(grep -oP '^HANodeName=\K.*' "$CONF" 2>/dev/null || true)
[[ -n "$NODE_NAME" ]] || {
    echo "error: HANodeName is not set in $CONF." >&2
    echo "       Run enable-zabbix-ha.sh first — the VIP follows the active HA" >&2
    echo "       node, so there has to be one." >&2
    exit 2
}

# VRRP authentication is a shared secret, identical on every node. It is not
# real security (VRRP auth is plaintext); it stops two unrelated clusters on the
# same LAN from fighting over a VRID.
[[ -n "$PASS" ]] || PASS="zbxha$(echo -n "$VRID" | md5sum | cut -c1-3)"

PREFIX=$(ip -o -4 addr show dev "$IFACE" | awk '{print $4}' | head -1 | cut -d/ -f2)
PREFIX="${PREFIX:-24}"

echo "==> VIP ${VIP}/${PREFIX} on ${IFACE}, VRID ${VRID}, node '${NODE_NAME}'"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [dry-run] would install keepalived, the track script, and the config"
    exit 0
fi

export DEBIAN_FRONTEND=noninteractive
command -v keepalived >/dev/null 2>&1 || {
    echo "    installing keepalived"
    apt-get update -qq && apt-get install -y -qq keepalived >/dev/null
}

# --- track script: does THIS node currently hold the active role? -----------
# `zabbix_server -R ha_status` prints the cluster table. We match our own node
# name and require it to say "active". Exit 0 => hold the VIP.
cat > /usr/local/bin/zbx-ha-active <<'TRACK'
#!/bin/sh
# Exit 0 when this node holds the active Zabbix HA role, 1 otherwise.
# Used by keepalived to decide who owns the VIP.
CONF=/etc/zabbix/zabbix_server.conf
NAME=$(sed -n 's/^HANodeName=//p' "$CONF" | head -1)
[ -n "$NAME" ] || exit 1
# A stopped server cannot be active, and -R would fail anyway.
systemctl is-active --quiet zabbix-server || exit 1
zabbix_server -R ha_status 2>/dev/null \
  | awk -v n="$NAME" '$0 ~ ("[[:space:]]" n "[[:space:]]") && /active/ { found = 1 } END { exit !found }'
TRACK
chmod 0755 /usr/local/bin/zbx-ha-active

cat > /etc/keepalived/keepalived.conf <<EOF
# Managed by homelab-zabbix deploy/ha/setup-vip.sh — local edits will be lost.
#
# The VIP follows the active Zabbix HA node rather than a fixed priority, so it
# is always where the proxy needs to send data.

global_defs {
    router_id ${NODE_NAME}
    enable_script_security
    script_user root
}

vrrp_script chk_zbx_active {
    script "/usr/local/bin/zbx-ha-active"
    interval 5
    timeout 4
    weight 50      # holding the active role outweighs the base priority
    fall 2         # two consecutive failures before giving up the VIP
    rise 2
}

vrrp_instance ZBX_HA {
    # Both nodes start as BACKUP; the track script decides who wins. Nobody is
    # a permanent MASTER, so the VIP never sits on a node that is not active.
    state BACKUP
    interface ${IFACE}
    virtual_router_id ${VRID}
    priority 100
    advert_int 1
    # Preemption MUST stay enabled. With nopreempt the current holder keeps the
    # VIP even after its priority drops, so a failover moves the Zabbix role to
    # the other node while the VIP stays behind — and the proxy keeps delivering
    # to a server that is no longer active. Verified the hard way.

    authentication {
        auth_type PASS
        auth_pass ${PASS}
    }

    virtual_ipaddress {
        ${VIP}/${PREFIX} dev ${IFACE}
    }

    track_script {
        chk_zbx_active
    }
}
EOF
chmod 0640 /etc/keepalived/keepalived.conf

systemctl enable -q keepalived
systemctl restart keepalived
sleep 6

echo -n "    keepalived: "; systemctl is-active keepalived
echo -n "    this node is active per the track script: "
if /usr/local/bin/zbx-ha-active; then echo "yes"; else echo "no"; fi
echo    "    VIP present here: $(ip -o -4 addr show dev "$IFACE" | grep -c "${VIP}/")"

cat <<EOF

Done on this node. Once BOTH nodes are configured, point the proxy at the VIP:

    # /etc/zabbix/zabbix_proxy.conf
    Server=${VIP}

    systemctl restart zabbix-proxy

Agents do not need the VIP — they take a comma list and already know both nodes.
EOF
