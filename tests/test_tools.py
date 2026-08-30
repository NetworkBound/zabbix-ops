"""Tests for the DNS audit and the Zabbix client's request shaping.

No resolver and no Zabbix server are contacted — the lookup and transport layers
are separated from the logic precisely so this can run anywhere, including on a
hosted CI runner with no route to the monitored network.
"""
import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from inventory import dns_findings  # noqa: E402
from zbx import Zabbix, ZabbixError  # noqa: E402


class TestDnsFindings(unittest.TestCase):
    def test_healthy_forward_and_reverse(self):
        self.assertEqual(
            dns_findings("web01", "10.0.0.5", ["10.0.0.5"], "web01.lan"), []
        )

    def test_name_does_not_resolve(self):
        out = dns_findings("web01", "10.0.0.5", [], "web01.lan")
        self.assertIn("name does not resolve", out)

    def test_resolves_to_a_different_address(self):
        out = dns_findings("web01", "10.0.0.5", ["10.0.0.9"], "web01.lan")
        self.assertEqual(len(out), 1)
        self.assertIn("Zabbix polls 10.0.0.5", out[0])

    def test_missing_ptr(self):
        self.assertIn("no PTR", dns_findings("web01", "10.0.0.5", ["10.0.0.5"], ""))

    def test_ptr_points_elsewhere(self):
        out = dns_findings("web01", "10.0.0.5", ["10.0.0.5"], "oldname.lan")
        self.assertIn("PTR is oldname.lan", out)

    def test_ptr_match_is_case_insensitive(self):
        self.assertEqual(
            dns_findings("WEB01", "10.0.0.5", ["10.0.0.5"], "web01.lan"), []
        )

    def test_multi_homed_name_containing_the_polled_address_is_fine(self):
        self.assertEqual(
            dns_findings("web01", "10.0.0.5", ["10.0.0.5", "10.0.1.5"], "web01.lan"), []
        )

    def test_findings_accumulate(self):
        self.assertEqual(len(dns_findings("web01", "10.0.0.5", [], "")), 2)


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestZabbixClient(unittest.TestCase):
    """Zabbix 7.0 moved auth to an Authorization header. Getting this wrong
    fails with a permission error that never mentions authentication, so it is
    worth pinning down."""

    def _client(self):
        return Zabbix(url="http://zbx.example/api_jsonrpc.php", token="TOK")

    def test_authenticated_call_uses_bearer_header(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse({"result": []})) as m:
            self._client().call("host.get", {})
        req = m.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer TOK")

    def test_auth_is_not_placed_in_the_request_body(self):
        # The pre-7.0 form. Zabbix 7 ignores it rather than rejecting it, so a
        # stray "auth" key would fail silently and confusingly.
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse({"result": []})) as m:
            self._client().call("host.get", {})
        body = json.loads(m.call_args[0][0].data)
        self.assertNotIn("auth", body)
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertEqual(body["method"], "host.get")

    def test_version_call_is_unauthenticated(self):
        # apiinfo.version is the one method that must NOT carry an auth header;
        # sending one makes it fail.
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse({"result": "7.4.13"})) as m:
            self.assertEqual(self._client().version(), "7.4.13")
        self.assertIsNone(m.call_args[0][0].get_header("Authorization"))

    def test_api_error_is_raised_with_context(self):
        payload = {"error": {"message": "Not authorised", "data": "check perms"}}
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(payload)), \
                self.assertRaises(ZabbixError) as ctx:
            self._client().call("host.get", {})
        self.assertIn("host.get", str(ctx.exception))
        self.assertIn("Not authorised", str(ctx.exception))

    def test_request_ids_increment(self):
        c = self._client()
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse({"result": []})) as m:
            c.call("host.get", {})
            c.call("item.get", {})
        ids = [json.loads(call[0][0].data)["id"] for call in m.call_args_list]
        self.assertEqual(ids, [1, 2])

    def test_missing_url_is_rejected(self):
        with self.assertRaises(ZabbixError):
            Zabbix(url="", token="TOK")

    def test_missing_credentials_are_rejected(self):
        with self.assertRaises(ZabbixError):
            Zabbix(url="http://zbx.example/api_jsonrpc.php")

    def test_count_coerces_to_int(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResponse({"result": "42"})):
            self.assertEqual(self._client().count("host.get"), 42)


if __name__ == "__main__":
    unittest.main()
