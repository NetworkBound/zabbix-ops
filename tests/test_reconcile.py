"""Tests for the Proxmox↔Zabbix reconciler.

The comparison logic is deliberately pure, so all of this runs with no Proxmox,
no Zabbix, and no network.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from pve import parse_net_ip  # noqa: E402
from reconcile import normalise, reconcile  # noqa: E402


def guest(name, address="10.0.0.5", status="running", vmid=100,
          source="config:net0", node="pve1", kind="lxc"):
    return {"vmid": vmid, "name": name, "type": kind, "status": status,
            "node": node, "address": address, "address_source": source}


def host(name, address="10.0.0.5", enabled=True, uses="ip", groups=None, hostid="1"):
    return {"hostid": hostid, "host": name, "enabled": enabled,
            "address": address, "uses": uses, "groups": groups or []}


class TestParseNetIp(unittest.TestCase):
    def test_extracts_static_ipv4(self):
        cfg = "name=eth0,bridge=vmbr0,gw=10.0.0.1,hwaddr=AA:BB:CC:DD:EE:FF,ip=10.0.0.51/24,type=veth"
        self.assertEqual(parse_net_ip(cfg), "10.0.0.51")

    def test_strips_the_cidr_suffix(self):
        self.assertEqual(parse_net_ip("ip=192.0.2.10/24"), "192.0.2.10")

    def test_dhcp_is_unknown_not_an_address(self):
        # Must be "" so the caller treats it as unverifiable rather than as a
        # mismatch against whatever Zabbix has.
        self.assertEqual(parse_net_ip("name=eth0,bridge=vmbr0,ip=dhcp,type=veth"), "")

    def test_manual_and_auto_are_unknown(self):
        self.assertEqual(parse_net_ip("ip=manual"), "")
        self.assertEqual(parse_net_ip("ip=auto"), "")

    def test_missing_ip_key(self):
        self.assertEqual(parse_net_ip("name=eth0,bridge=vmbr0,type=veth"), "")

    def test_empty_input(self):
        self.assertEqual(parse_net_ip(""), "")


class TestNormalise(unittest.TestCase):
    def test_case_insensitive(self):
        self.assertEqual(normalise("Web01"), normalise("web01"))

    def test_strips_domain_suffix(self):
        self.assertEqual(normalise("web01.lan"), "web01")

    def test_strips_whitespace(self):
        self.assertEqual(normalise("  web01  "), "web01")

    def test_handles_none(self):
        self.assertEqual(normalise(None), "")


class TestReconcile(unittest.TestCase):
    def test_matching_pair_is_clean(self):
        r = reconcile([guest("web01")], [host("web01")])
        self.assertEqual(r["totals"]["drift"], 0)
        self.assertEqual(r["totals"]["unmonitored"], 0)
        self.assertEqual(r["totals"]["orphaned"], 0)
        self.assertEqual(r["totals"]["no_address"], 0)

    def test_detects_address_drift(self):
        r = reconcile([guest("web01", address="10.0.0.51")],
                      [host("web01", address="10.0.0.50")])
        self.assertEqual(r["totals"]["drift"], 1)
        d = r["drift"][0]
        self.assertEqual(d["proxmox_address"], "10.0.0.51")
        self.assertEqual(d["zabbix_address"], "10.0.0.50")

    def test_unroutable_address_is_not_reported_as_drift(self):
        # 0.0.0.0 means Zabbix has nowhere to poll at all. Reporting it as
        # "drift" would imply the two systems merely disagree, which understates
        # it and buries it among cosmetic mismatches.
        r = reconcile([guest("web01", address="10.0.0.51")],
                      [host("web01", address="0.0.0.0")])
        self.assertEqual(r["totals"]["drift"], 0)
        self.assertEqual(r["totals"]["no_address"], 1)
        self.assertEqual(r["no_address"][0]["proxmox_address"], "10.0.0.51")

    def test_empty_address_counts_as_no_address(self):
        r = reconcile([guest("web01")], [host("web01", address="")])
        self.assertEqual(r["totals"]["no_address"], 1)

    def test_disabled_host_with_no_address_is_ignored(self):
        r = reconcile([guest("web01")], [host("web01", address="0.0.0.0", enabled=False)])
        self.assertEqual(r["totals"]["no_address"], 0)

    def test_dns_based_interface_is_not_drift(self):
        # A host polled by name legitimately has a hostname where the IP would
        # be; comparing it to a Proxmox address is meaningless.
        r = reconcile([guest("web01", address="10.0.0.51")],
                      [host("web01", address="web01.lan", uses="dns")])
        self.assertEqual(r["totals"]["drift"], 0)

    def test_running_guest_with_no_host_is_unmonitored(self):
        r = reconcile([guest("web01")], [])
        self.assertEqual(r["totals"]["unmonitored"], 1)

    def test_stopped_guest_with_no_host_is_not_unmonitored(self):
        r = reconcile([guest("web01", status="stopped")], [])
        self.assertEqual(r["totals"]["unmonitored"], 0)

    def test_stopped_guest_with_enabled_host_is_alert_noise(self):
        r = reconcile([guest("web01", status="stopped")], [host("web01")])
        self.assertEqual(r["totals"]["stopped"], 1)

    def test_host_with_no_guest_is_orphaned(self):
        r = reconcile([], [host("switch01")])
        self.assertEqual(r["totals"]["orphaned"], 1)

    def test_dhcp_guest_is_unverifiable_not_drift(self):
        r = reconcile([guest("web01", address="", source="dhcp")],
                      [host("web01", address="10.0.0.50")])
        self.assertEqual(r["totals"]["drift"], 0)
        self.assertEqual(r["totals"]["unverifiable"], 1)

    def test_name_matching_tolerates_case_and_domain(self):
        r = reconcile([guest("Web01")], [host("web01.lan")])
        self.assertEqual(r["totals"]["unmonitored"], 0)
        self.assertEqual(r["totals"]["orphaned"], 0)


class TestExcludeGroups(unittest.TestCase):
    def test_excluded_group_is_not_orphaned(self):
        r = reconcile([], [host("switch01", groups=["Homelab/Network"])],
                      exclude_groups=["Homelab/Network"])
        self.assertEqual(r["totals"]["orphaned"], 0)

    def test_excluding_a_group_does_not_orphan_its_guests(self):
        """Regression: excluding a group used to drop those hosts from the match
        index, so their perfectly-monitored guests were reported as unmonitored.
        The exclusion must only suppress the orphaned list."""
        r = reconcile(
            [guest("pihole", address="10.0.0.9")],
            [host("pihole", address="10.0.0.9", groups=["Homelab/Infrastructure"])],
            exclude_groups=["Homelab/Infrastructure"],
        )
        self.assertEqual(r["totals"]["unmonitored"], 0)
        self.assertEqual(r["totals"]["orphaned"], 0)
        self.assertEqual(r["totals"]["drift"], 0)

    def test_excluded_group_still_reports_its_own_drift(self):
        r = reconcile(
            [guest("pihole", address="10.0.0.9")],
            [host("pihole", address="10.0.0.99", groups=["Homelab/Infrastructure"])],
            exclude_groups=["Homelab/Infrastructure"],
        )
        self.assertEqual(r["totals"]["drift"], 1)


if __name__ == "__main__":
    unittest.main()
