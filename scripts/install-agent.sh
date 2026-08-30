#!/usr/bin/env bash
#
# Install and configure Zabbix Agent 2 on a Debian/Ubuntu or RHEL-family host.
#
#   ZBX_SERVER=10.0.0.20 ./install-agent.sh [hostname-in-zabbix]
#
# The hostname argument defaults to the machine's own hostname. If you rely on
# auto-registration (see docs/auto-registration.md) that default is usually what
# you want, because the metadata below is what the registration action matches.
#
# Idempotent: re-running upgrades the package and rewrites the config.
#
set -euo pipefail

ZBX_SERVER="${ZBX_SERVER:-}"
ZBX_HOSTNAME="${1:-$(hostname -s)}"
# Sent to the server at registration time; the auto-registration action matches
# on it. Keep "Linux" in the string unless you also change the action condition.
ZBX_METADATA="${ZBX_METADATA:-Linux}"

if [[ -z "${ZBX_SERVER}" ]]; then
    echo "error: set ZBX_SERVER to your Zabbix server or proxy address." >&2
    echo "usage: ZBX_SERVER=10.0.0.20 $0 [hostname-in-zabbix]" >&2
    exit 2
fi

if [[ $EUID -ne 0 ]]; then
    echo "error: must run as root." >&2
    exit 1
fi

# --- detect distribution ---------------------------------------------------
. /etc/os-release
echo "==> ${PRETTY_NAME}  ->  Zabbix host '${ZBX_HOSTNAME}', server ${ZBX_SERVER}"

install_debian() {
    local codename="${VERSION_CODENAME:-}"
    local relpkg="zabbix-release_latest+${ID}${VERSION_ID}_all.deb"
    local url="https://repo.zabbix.com/zabbix/7.0/${ID}/pool/main/z/zabbix-release/${relpkg}"

    if ! dpkg -l zabbix-release >/dev/null 2>&1; then
        echo "==> Adding Zabbix repository (${codename})"
        tmp="$(mktemp -d)"
        # The release package name scheme differs slightly between Debian and
        # Ubuntu; fall back to the distro repo if the fetch fails rather than
        # aborting the whole install.
        if curl -fsSL -o "${tmp}/${relpkg}" "${url}"; then
            dpkg -i "${tmp}/${relpkg}"
        else
            echo "    (upstream release package not found; using distro packages)"
        fi
        rm -rf "${tmp}"
    fi
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq zabbix-agent2
}

install_rhel() {
    local major="${VERSION_ID%%.*}"
    if ! rpm -q zabbix-release >/dev/null 2>&1; then
        echo "==> Adding Zabbix repository (el${major})"
        rpm -Uvh "https://repo.zabbix.com/zabbix/7.0/rhel/${major}/x86_64/zabbix-release-latest.el${major}.noarch.rpm" || \
            echo "    (upstream repo unavailable; using distro packages)"
    fi
    (command -v dnf >/dev/null && dnf install -y -q zabbix-agent2) || yum install -y -q zabbix-agent2
}

case "${ID}" in
    debian|ubuntu)          install_debian ;;
    rhel|centos|rocky|almalinux|fedora) install_rhel ;;
    *) echo "error: unsupported distribution '${ID}'." >&2; exit 1 ;;
esac

# --- configure -------------------------------------------------------------
CONF=/etc/zabbix/zabbix_agent2.conf
echo "==> Writing ${CONF}"
[[ -f "${CONF}" && ! -f "${CONF}.orig" ]] && cp "${CONF}" "${CONF}.orig"

cat > "${CONF}" <<EOF
# Managed by zabbix-ops/scripts/install-agent.sh — local edits will be lost.
PidFile=/run/zabbix/zabbix_agent2.pid
LogFile=/var/log/zabbix/zabbix_agent2.log
LogFileSize=10

# Passive checks (server polls the agent) and active checks (agent pushes).
Server=${ZBX_SERVER}
ServerActive=${ZBX_SERVER}

Hostname=${ZBX_HOSTNAME}
# Matched by the auto-registration action; see docs/auto-registration.md.
HostMetadata=${ZBX_METADATA}

# Buffer active-check data so a brief server outage does not lose samples.
BufferSend=5
BufferSize=100

Include=/etc/zabbix/zabbix_agent2.d/*.conf
EOF

install -d -o zabbix -g zabbix -m 0755 /var/log/zabbix /run/zabbix 2>/dev/null || true

systemctl enable zabbix-agent2 >/dev/null 2>&1 || true
systemctl restart zabbix-agent2

sleep 2
if systemctl is-active --quiet zabbix-agent2; then
    echo "==> zabbix-agent2 running as '${ZBX_HOSTNAME}'"
    echo "    Verify from the server:  zabbix_get -s <this-host-ip> -k agent.ping"
else
    echo "error: zabbix-agent2 failed to start. Recent log:" >&2
    journalctl -u zabbix-agent2 --no-pager --lines=20 >&2 || true
    exit 1
fi
