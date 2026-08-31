"""Tests for the agent-fleet comparison logic.

Only the pure half is exercised here: parsing an agent config, deciding whether
an allowlist admits every poller, and comparing a hostname. No pct, no API, no
network — the collectors are thin wrappers around subprocess and one host.get,
and the interesting mistakes are all in the comparison.

The allowlist tests earn their keep. A false clean result there is the worst
possible outcome for this tool: it would confirm that HA failover is safe on
exactly the fleet where it is not.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from agentfleet import (  # noqa: E402
    AGENT_CONFS,
    agent_tls_modes,
    choose_conf,
    covers,
    effective,
    expected_addresses,
    hostname_status,
    match_guests,
    missing_addresses,
    parse_agent_conf,
    parse_version,
    split_addresses,
    strip_port,
    tls_modes,
    unresolvable,
)


def guest(ctid="100", name="web01", address="10.0.0.51", conf=None, agent=True,
          version="7.0.9", hostname=None):
    conf = conf if conf is not None else {}
    return {"ctid": ctid, "name": name, "address": address, "agent": agent,
            "conf": conf, "files": ["/etc/zabbix/zabbix_agent2.conf"] if agent else [],
            "version": version, "service": "", "syshostname": name,
            "hostname": hostname if hostname is not None else effective(conf, "Hostname"),
            "error": ""}


def host(hostid="1", name="web01", addresses=("10.0.0.51",), proxyid="",
         tls_connect="1", tls_accept="1"):
    return {"hostid": hostid, "host": name, "enabled": True,
            "addresses": list(addresses), "agent_iface": True, "proxyid": proxyid,
            "tls_connect": tls_connect, "tls_accept": tls_accept, "groups": []}


class TestParsing(unittest.TestCase):
    CONF = """
# Zabbix agent 2 configuration
PidFile=/run/zabbix/zabbix_agent2.pid
Server=10.0.0.20,10.0.0.21
ServerActive=10.0.0.20:10051;10.0.0.21
Hostname=web01
# Server=10.0.0.99 is commented out and must not count
TLSConnect=psk
TLSAccept=unencrypted,psk
"""

    def test_directives_are_extracted(self):
        conf = parse_agent_conf(self.CONF)
        self.assertEqual(effective(conf, "Server"), "10.0.0.20,10.0.0.21")
        self.assertEqual(effective(conf, "Hostname"), "web01")

    def test_comments_are_ignored(self):
        # A commented-out Server line is the most common thing in these files
        # and counting it would report a broken allowlist as complete.
        self.assertEqual(parse_agent_conf(self.CONF)["Server"], ["10.0.0.20,10.0.0.21"])

    def test_unrelated_directives_are_dropped(self):
        self.assertNotIn("PidFile", parse_agent_conf(self.CONF))

    def test_missing_directive_is_empty_not_an_error(self):
        self.assertEqual(effective(parse_agent_conf(self.CONF), "HostMetadata"), "")

    def test_indented_directive_is_still_seen(self):
        # The agent itself rejects this, but reporting the guest as having no
        # Hostname at all would send the reader looking in the wrong place.
        self.assertEqual(effective(parse_agent_conf("   Hostname=web01"), "Hostname"),
                         "web01")

    def test_last_definition_wins(self):
        # Include= files are read where the Include appears and may redefine
        # anything above them, so the last value is the effective one.
        conf = parse_agent_conf("Hostname=old\nHostname=new")
        self.assertEqual(conf["Hostname"], ["old", "new"])
        self.assertEqual(effective(conf, "Hostname"), "new")

    def test_value_containing_equals_is_kept_whole(self):
        conf = parse_agent_conf("HostMetadata=env=prod")
        self.assertEqual(effective(conf, "HostMetadata"), "env=prod")

    def test_empty_file_parses_to_nothing(self):
        self.assertEqual(parse_agent_conf(""), {})


class TestAddressSplitting(unittest.TestCase):
    def test_server_splits_on_commas(self):
        self.assertEqual(split_addresses("10.0.0.20, 10.0.0.21", "Server"),
                         ["10.0.0.20", "10.0.0.21"])

    def test_serveractive_also_splits_on_semicolons(self):
        # In ServerActive a ';' separates HA clusters and a ',' separates the
        # nodes inside one. Both are addresses the agent talks to.
        self.assertEqual(
            split_addresses("10.0.0.20,10.0.0.21;10.0.0.30", "ServerActive"),
            ["10.0.0.20", "10.0.0.21", "10.0.0.30"])

    def test_server_does_not_split_on_semicolons(self):
        # Server has no cluster syntax, so a ';' is a typo. Keeping it embedded
        # makes it visible in the report instead of silently accepted.
        self.assertEqual(split_addresses("10.0.0.20;10.0.0.21", "Server"),
                         ["10.0.0.20;10.0.0.21"])

    def test_port_is_stripped_from_serveractive_entries(self):
        self.assertEqual(strip_port("10.0.0.20:10051"), "10.0.0.20")

    def test_bare_address_is_untouched(self):
        self.assertEqual(strip_port("10.0.0.20"), "10.0.0.20")


class TestAllowlistCoverage(unittest.TestCase):
    def test_exact_address_is_covered(self):
        self.assertTrue(covers("10.0.0.20", "10.0.0.20"))

    def test_different_address_is_not_covered(self):
        self.assertFalse(covers("10.0.0.20", "10.0.0.21"))

    def test_cidr_entry_covers_addresses_inside_it(self):
        # A fleet allowlisting the whole management network is correctly
        # configured. Comparing entries as strings would report every guest as
        # broken and bury the real gaps.
        self.assertTrue(covers("10.0.0.0/24", "10.0.0.21"))
        self.assertFalse(covers("10.0.0.0/24", "10.0.1.21"))

    def test_last_octet_range_is_understood(self):
        self.assertTrue(covers("10.0.0.16-31", "10.0.0.20"))
        self.assertFalse(covers("10.0.0.16-31", "10.0.0.40"))

    def test_dns_name_covers_nothing(self):
        # No resolution happens here, so a name can never be counted as
        # coverage. It is reported as unverified instead.
        self.assertFalse(covers("zabbix.example.com", "10.0.0.20"))

    def test_a_second_ha_node_missing_from_the_allowlist_is_found(self):
        # The failure this tool exists for: the agent answers the original node
        # and refuses the standby, so failover lands in a blind spot.
        self.assertEqual(missing_addresses(["10.0.0.20"], ["10.0.0.20", "10.0.0.21"]),
                         ["10.0.0.21"])

    def test_complete_allowlist_reports_no_gap(self):
        self.assertEqual(
            missing_addresses(["10.0.0.20", "10.0.0.21"], ["10.0.0.20", "10.0.0.21"]),
            [])

    def test_order_does_not_matter(self):
        self.assertEqual(
            missing_addresses(["10.0.0.21", "10.0.0.20"], ["10.0.0.20", "10.0.0.21"]),
            [])

    def test_empty_allowlist_is_missing_everything(self):
        self.assertEqual(missing_addresses([], ["10.0.0.20"]), ["10.0.0.20"])

    def test_names_are_reported_as_unverifiable(self):
        self.assertEqual(unresolvable(["10.0.0.20", "zbx.example.com"]),
                         ["zbx.example.com"])

    def test_cidr_and_range_entries_are_not_called_names(self):
        self.assertEqual(unresolvable(["10.0.0.0/24", "10.0.0.16-31"]), [])


class TestExpectedAddresses(unittest.TestCase):
    def test_directly_polled_host_expects_every_ha_node(self):
        self.assertEqual(
            expected_addresses(host(), ["10.0.0.20", "10.0.0.21"], {}, []),
            ["10.0.0.20", "10.0.0.21"])

    def test_proxied_host_expects_the_proxy_not_the_servers(self):
        # A host behind a proxy is polled by the proxy. Demanding the HA nodes
        # in its allowlist would be noise, and would hide the one address that
        # actually matters.
        self.assertEqual(
            expected_addresses(host(proxyid="7"), ["10.0.0.20", "10.0.0.21"],
                               {"7": "10.0.0.30"}, []),
            ["10.0.0.30"])

    def test_unknown_proxy_address_expects_nothing_rather_than_guessing(self):
        self.assertEqual(
            expected_addresses(host(proxyid="7"), ["10.0.0.20"], {}, []), [])

    def test_extra_known_addresses_are_always_expected(self):
        # A VIP that floats between HA nodes is not an HA node row, so it has to
        # be declared. Every agent still has to admit it.
        self.assertEqual(
            expected_addresses(host(), ["10.0.0.20"], {}, ["10.0.0.9"]),
            ["10.0.0.20", "10.0.0.9"])


class TestHostnameMatching(unittest.TestCase):
    def test_identical_names_match(self):
        self.assertEqual(hostname_status("web01", "web01"), "match")

    def test_different_names_are_a_mismatch(self):
        self.assertEqual(hostname_status("web01", "web02"), "mismatch")

    def test_case_difference_is_its_own_class(self):
        # Zabbix compares byte for byte. This fails exactly as hard as a wrong
        # name while looking correct to anyone reading the two side by side.
        self.assertEqual(hostname_status("Web01", "web01"), "case")

    def test_domain_suffix_difference_is_its_own_class(self):
        self.assertEqual(hostname_status("web01.lan", "web01"), "suffix")

    def test_unset_hostname_is_reported_separately(self):
        # An unset Hostname is not wrong today: the agent falls back to the
        # system hostname. It becomes wrong the day someone renames the guest.
        self.assertEqual(hostname_status("", "web01"), "unset")

    def test_surrounding_whitespace_does_not_create_a_mismatch(self):
        self.assertEqual(hostname_status(" web01 ", "web01"), "match")


class TestGuestToHostMatching(unittest.TestCase):
    def test_pairing_is_by_address_not_by_agent_hostname(self):
        # Keying on the agent's own Hostname would make every hostname mismatch
        # look like two unrelated records that simply never met, which is the
        # one result this tool must never produce.
        g = guest(name="web01", address="10.0.0.51",
                  conf=parse_agent_conf("Hostname=wrongname"))
        pairs, orphan_guests, orphan_hosts = match_guests([g], [host(name="web01")])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(orphan_guests, [])
        self.assertEqual(orphan_hosts, [])
        self.assertEqual(
            hostname_status(pairs[0]["guest"]["hostname"], pairs[0]["host"]["host"]),
            "mismatch")

    def test_name_is_the_fallback_when_the_address_is_unknown(self):
        g = guest(name="web01", address="")
        pairs, _, _ = match_guests([g], [host(name="web01", addresses=[])])
        self.assertEqual(len(pairs), 1)

    def test_guest_with_no_host_is_unmonitored(self):
        pairs, orphan_guests, _ = match_guests([guest(name="db01", address="10.0.0.60")],
                                               [host(name="web01")])
        self.assertEqual(pairs, [])
        self.assertEqual([g["name"] for g in orphan_guests], ["db01"])

    def test_host_with_no_guest_is_left_for_reconcile(self):
        _, _, orphan_hosts = match_guests([], [host(name="switch01")])
        self.assertEqual([h["host"] for h in orphan_hosts], ["switch01"])


class TestTls(unittest.TestCase):
    def test_absent_directives_mean_unencrypted(self):
        self.assertEqual(agent_tls_modes({}), ("unencrypted", {"unencrypted"}))

    def test_accept_is_a_list_and_connect_is_not(self):
        # They are compared against opposite ends of the host object, so
        # collapsing them into one value would hide a half-configured host.
        conf = parse_agent_conf("TLSConnect=psk\nTLSAccept=unencrypted,psk")
        self.assertEqual(agent_tls_modes(conf), ("psk", {"unencrypted", "psk"}))

    def test_host_bitmask_decodes_to_modes(self):
        self.assertEqual(tls_modes("1"), {"unencrypted"})
        self.assertEqual(tls_modes("2"), {"psk"})
        self.assertEqual(tls_modes("3"), {"unencrypted", "psk"})

    def test_unparseable_bitmask_yields_nothing_rather_than_a_false_mismatch(self):
        self.assertEqual(tls_modes(None), set())


class TestChoosingTheLiveConfig(unittest.TestCase):
    """A guest upgraded from the C agent keeps both files.

    Merging the two, or reading the wrong one, produces a confident report about
    a file nothing is using. That happened on the first live run of this tool:
    a stale zabbix_agentd.conf made three correctly configured guests look like
    they had lost an address from their allowlist.
    """

    AGENT2, AGENTD = AGENT_CONFS

    def test_running_service_decides(self):
        self.assertEqual(
            choose_conf({self.AGENT2: [], self.AGENTD: []},
                        {"zabbix-agent": "active"}),
            self.AGENTD)

    def test_agent2_wins_when_nothing_is_running(self):
        self.assertEqual(
            choose_conf({self.AGENT2: [], self.AGENTD: []}, {}), self.AGENT2)

    def test_an_inactive_service_does_not_win(self):
        self.assertEqual(
            choose_conf({self.AGENT2: [], self.AGENTD: []},
                        {"zabbix-agent": "inactive", "zabbix-agent2": "active"}),
            self.AGENT2)

    def test_the_only_config_present_is_used_whatever_is_running(self):
        self.assertEqual(choose_conf({self.AGENTD: []}, {}), self.AGENTD)

    def test_no_config_means_no_agent(self):
        self.assertEqual(choose_conf({}, {"zabbix-agent2": "active"}), "")


class TestVersions(unittest.TestCase):
    def test_version_is_taken_from_the_banner(self):
        self.assertEqual(parse_version("zabbix_agent2 (Zabbix) 7.0.9"), "7.0.9")

    def test_c_agent_banner_parses_too(self):
        self.assertEqual(parse_version("zabbix_agentd (daemon) (Zabbix) 6.0.14"),
                         "6.0.14")

    def test_unrecognised_banner_gives_no_version_rather_than_a_wrong_one(self):
        self.assertEqual(parse_version("command not found"), "")


if __name__ == "__main__":
    unittest.main()
