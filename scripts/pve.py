#!/usr/bin/env python3
"""Minimal Proxmox VE API client — read-only.

Used by ``reconcile.py`` to compare what Proxmox believes about a guest against
what Zabbix believes about the matching host.

Authentication is by API token, never a login ticket::

    Authorization: PVEAPIToken=<user>@<realm>!<tokenid>=<secret>

Create a read-only token on the node::

    pveum user add zabbix@pve
    pveum aclmod / --users zabbix@pve --roles PVEAuditor
    pveum user token add zabbix@pve monitoring --privsep 0

Nodes are configured with contiguous indexed environment variables, so any
cluster size works without editing code::

    PVE_0_NAME=pve1
    PVE_0_URL=https://10.0.0.10:8006
    PVE_0_TOKEN=zabbix@pve!monitoring=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

Nodes do not need to be clustered — declare each standalone node separately.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request


class ProxmoxError(RuntimeError):
    """An error from the Proxmox API or its transport."""


# net0: name=eth0,bridge=vmbr0,gw=10.0.0.1,hwaddr=AA:BB:...,ip=10.0.0.51/24,type=veth
_IP_IN_NETCFG = re.compile(r"\bip=([^,/\s]+)")


def parse_net_ip(netcfg: str) -> str:
    """Pull the static IPv4 out of a container's ``netN`` config string.

    Returns "" for DHCP, for ``manual``, or when no ``ip=`` key is present —
    all of which mean "Proxmox does not know this guest's address", which the
    caller must treat as *unknown* rather than as a mismatch.
    """
    if not netcfg:
        return ""
    m = _IP_IN_NETCFG.search(netcfg)
    if not m:
        return ""
    value = m.group(1).strip().lower()
    if value in ("dhcp", "manual", "auto", ""):
        return ""
    return value


class Proxmox:
    def __init__(self, name: str, url: str, token: str, verify_tls: bool = False,
                 timeout: int = 20):
        if not (name and url and token):
            raise ProxmoxError("Proxmox node needs a name, url and token.")
        self.name = name
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Proxmox ships a self-signed certificate by default, so verification is
        # off unless the operator has put a trusted cert on the node.
        self._ctx = ssl.create_default_context()
        if not verify_tls:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    @staticmethod
    def from_env(verify_tls: bool | None = None) -> list[Proxmox]:
        if verify_tls is None:
            verify_tls = os.environ.get("PVE_VERIFY_TLS", "").lower() in ("1", "true", "yes", "on")
        nodes = []
        for i in range(16):
            name = os.environ.get(f"PVE_{i}_NAME", "").strip()
            if not name:
                break
            nodes.append(Proxmox(
                name=name,
                url=os.environ.get(f"PVE_{i}_URL", "").strip(),
                token=os.environ.get(f"PVE_{i}_TOKEN", "").strip(),
                verify_tls=verify_tls,
            ))
        return nodes

    def _get(self, path: str):
        req = urllib.request.Request(
            f"{self.url}/api2/json{path}",
            headers={"Authorization": f"PVEAPIToken={self.token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                return json.load(r).get("data")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise ProxmoxError(
                    f"{self.name}: 401 Unauthorized — check PVE_*_TOKEN."
                ) from e
            raise ProxmoxError(f"{self.name}: HTTP {e.code} on {path}") from e
        except urllib.error.URLError as e:
            # A wrong token secret makes Proxmox delay ~3s before answering,
            # which surfaces as a timeout rather than a 401. Say so, because the
            # symptom looks exactly like a network fault.
            raise ProxmoxError(
                f"{self.name}: cannot reach {self.url} ({e.reason}). "
                "If this is a ~3s timeout rather than a refusal, suspect the "
                "token secret before the network."
            ) from e

    def guests(self) -> list[dict]:
        """Every VM and container on this node, with its configured address.

        ``address`` is "" when Proxmox genuinely does not know it (DHCP, or a VM
        without the guest agent) — never a guess.
        """
        out = []
        for res in self._get("/cluster/resources?type=vm") or []:
            if res.get("node") != self.name:
                continue
            vmid, kind = res.get("vmid"), res.get("type")
            guest = {
                "vmid": vmid,
                "name": res.get("name") or str(vmid),
                "type": kind,
                "status": res.get("status"),
                "node": self.name,
                "address": "",
                "address_source": "unknown",
            }
            try:
                cfg = self._get(f"/nodes/{self.name}/{kind}/{vmid}/config") or {}
            except ProxmoxError:
                out.append(guest)
                continue

            if kind == "lxc":
                for key in sorted(k for k in cfg if k.startswith("net")):
                    ip = parse_net_ip(cfg.get(key, ""))
                    if ip:
                        guest["address"] = ip
                        guest["address_source"] = f"config:{key}"
                        break
                else:
                    guest["address_source"] = "dhcp"
            else:
                # VMs carry no address in their config; ask the guest agent when
                # it is present. Absent agent stays "unknown", not a mismatch.
                if guest["status"] == "running" and cfg.get("agent"):
                    try:
                        ifaces = self._get(
                            f"/nodes/{self.name}/qemu/{vmid}/agent/network-get-interfaces"
                        ) or {}
                        for iface in ifaces.get("result", []):
                            if iface.get("name") in ("lo", "lo0"):
                                continue
                            for a in iface.get("ip-addresses", []):
                                if a.get("ip-address-type") == "ipv4":
                                    addr = a.get("ip-address", "")
                                    if addr and not addr.startswith("127."):
                                        guest["address"] = addr
                                        guest["address_source"] = "guest-agent"
                                        break
                            if guest["address"]:
                                break
                    except ProxmoxError:
                        pass
                else:
                    guest["address_source"] = "no-agent"
            out.append(guest)
        return out


def connect_all_or_exit() -> list[Proxmox]:
    nodes = Proxmox.from_env()
    if not nodes:
        print("error: no Proxmox nodes configured. Set PVE_0_NAME / PVE_0_URL / "
              "PVE_0_TOKEN (see .env.example).", file=sys.stderr)
        sys.exit(2)
    return nodes


if __name__ == "__main__":
    for node in connect_all_or_exit():
        try:
            guests = node.guests()
        except ProxmoxError as e:
            print(f"error: {e}", file=sys.stderr)
            continue
        print(f"{node.name}: {len(guests)} guest(s)")
        for g in guests:
            print(f"  {g['vmid']:>5} {g['name'][:28]:<28} {g['type']:<4} "
                  f"{g['status']:<8} {g['address'] or '-':<16} {g['address_source']}")
