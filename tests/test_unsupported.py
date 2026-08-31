"""Tests for unsupported-item triage: signatures, classification, grouping.

Classification is the part worth testing. Everything the tool advises follows
from which class an error lands in, so a pattern that matches too eagerly does
not produce a slightly wrong report — it tells an operator to disable an item
that was reporting a real fault. The ordering tests exist for that reason.

Signature stability matters almost as much. If a signature varies with the OID
or the index that failed, four hundred items that share one cause become four
hundred groups and the tool stops being useful at exactly the scale it was
written for.

No network. Every error string here was taken from a real Zabbix 7.4 server.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from unsupported import BUCKETS, CLASSES, classify, group, matches, signature  # noqa: E402

WALK_COUNTER32 = (
    "Preprocessing failed for: .1.3.6.1.2.1.2.2.1.13.1 = Counter32: 6.."
    ".1.3.6.1.2.1.2.2.1.13.2 = Counter32: 0..1.3.6.1.2.1.2.2.1....\n"
    "1. Failed: unable to extract value for given OID: no data was found"
)
WALK_INTEGER = (
    "Preprocessing failed for: .1.3.6.1.2.1.10.7.2.1.19.2 = INTEGER: 1.."
    ".1.3.6.1.2.1.10.7.2.1.19.3 = INTEGER: 1..1.3.6.1.2.1.10.7...\n"
    "1. Failed: unable to extract value for given OID: no data was found"
)


def rec(error, host="host-a", template="", key="k", itemid="1"):
    """The subset of a fetched record that the pure functions actually read."""
    return {"itemid": itemid, "error": error, "host": host, "template": template,
            "key": key, "signature": signature(error)}


class TestSignature(unittest.TestCase):
    """A signature has to be the same for two items that share a cause."""

    def test_snmp_walk_payload_does_not_leak_into_the_signature(self):
        # The error text is the whole walk response, so it differs per item.
        # Grouping on it raw produced one group per item, which is the bug this
        # normalisation exists to prevent.
        other = WALK_COUNTER32.replace("2.2.1.13.1", "2.2.1.13.99").replace(": 6", ": 41231")
        self.assertEqual(signature(WALK_COUNTER32), signature(other))

    def test_snmp_walk_signature_is_short(self):
        self.assertLess(len(signature(WALK_COUNTER32)), 120)

    def test_different_snmp_types_stay_separate(self):
        # Counter32 and INTEGER walks fail for different reasons often enough
        # that merging them would send someone to the wrong master item.
        self.assertNotEqual(signature(WALK_COUNTER32), signature(WALK_INTEGER))
        self.assertIn("Counter32", signature(WALK_COUNTER32))
        self.assertIn("INTEGER", signature(WALK_INTEGER))

    def test_repeated_identical_lines_collapse(self):
        one = "No Such Instance currently exists at this OID"
        many = "\n".join([one] * 40)
        self.assertEqual(signature(many), signature(one))

    def test_oids_and_indexes_are_replaced(self):
        a = signature("Cannot read .1.3.6.1.2.1.2.2.1.10.4 from device")
        b = signature("Cannot read .1.3.6.1.2.1.2.2.1.10.9 from device")
        self.assertEqual(a, b)

    def test_quoted_values_are_replaced(self):
        a = signature('No "ipmi poller" processes started.')
        b = signature('No "java poller" processes started.')
        self.assertEqual(a, b)

    def test_unknown_metric_groups_by_cause_not_by_key(self):
        # One missing plugin produces one error per key it should have provided.
        a = signature("Unknown metric nvidia.gpu.temp")
        b = signature("Unknown metric nvidia.gpu.fan")
        self.assertEqual(a, b)
        self.assertIn("Unknown metric", a)

    def test_distinct_causes_do_not_collapse(self):
        self.assertNotEqual(signature("Invalid second parameter."),
                            signature("Invalid first parameter."))

    def test_empty_error_is_handled(self):
        self.assertTrue(signature(""))
        self.assertTrue(signature(None))

    def test_signature_is_bounded(self):
        self.assertLessEqual(len(signature("x " * 4000)), 150)

    def test_signature_is_stable_across_calls(self):
        self.assertEqual(signature(WALK_COUNTER32), signature(WALK_COUNTER32))


class TestClassification(unittest.TestCase):
    def test_snmp_walk_failure(self):
        self.assertEqual(classify(WALK_COUNTER32)["id"], "snmp-walk-missing-oid")
        self.assertEqual(classify(WALK_INTEGER)["id"], "snmp-walk-missing-oid")

    def test_invalid_second_parameter(self):
        c = classify("Invalid second parameter.")
        self.assertEqual(c["id"], "bad-key-parameter-2")
        self.assertEqual(c["bucket"], "params")

    def test_invalid_first_parameter_is_not_confused_with_the_second(self):
        self.assertEqual(classify("Invalid first parameter.")["id"], "bad-key-parameter-1")

    def test_block_device_missing(self):
        c = classify("Cannot obtain device name used internally by the kernel.")
        self.assertEqual(c["id"], "block-device-missing")
        self.assertEqual(c["bucket"], "macro")

    def test_network_interface_missing(self):
        c = classify("Cannot find information for this network interface in /proc/net/dev.")
        self.assertEqual(c["bucket"], "macro")

    def test_no_such_object_is_the_hardware_case(self):
        # This is the only class disable exists for, so it must not be reached
        # by anything else and must not itself fall through to another pattern.
        c = classify("No Such Object available on this agent at this OID")
        self.assertEqual(c["id"], "oid-not-implemented")
        self.assertEqual(c["bucket"], "hardware")

    def test_no_such_instance_is_not_the_hardware_case(self):
        # The device does support the MIB; the row is gone. Disabling by hand
        # here leaves an object discovery would have removed.
        c = classify("No Such Instance currently exists at this OID")
        self.assertEqual(c["id"], "oid-instance-missing")
        self.assertNotEqual(c["bucket"], "hardware")

    def test_unknown_metric_is_a_host_problem(self):
        self.assertEqual(classify("Unknown metric docker.images.total")["bucket"], "host")

    def test_process_not_started(self):
        c = classify('No "vmware collector" processes started.')
        self.assertEqual(c["id"], "server-process-not-started")

    def test_calculated_item(self):
        c = classify('Cannot evaluate expression: division by zero at '
                     '"/last(//system.swap.total[memTotalSwap.0])*100"')
        self.assertEqual(c["id"], "calculated-formula")

    def test_simple_check_without_interface(self):
        c = classify("Check service item must have IP parameter or host "
                     "interface specified.")
        self.assertEqual(c["bucket"], "host")

    def test_timeout_is_transient_and_never_hardware(self):
        c = classify("Timeout while executing a shell script.")
        self.assertEqual(c["bucket"], "transient")

    def test_permission_denied_is_not_disableable(self):
        # The metric is real; only the account is wrong. Classifying this as
        # hardware would advise disabling a check that should be repaired.
        self.assertEqual(classify("/proc/1/io: Permission denied")["bucket"], "host")

    def test_unrecognised_error_is_marked_unknown_not_guessed(self):
        c = classify("Some error nobody has written a pattern for yet")
        self.assertEqual(c["id"], "unknown")
        self.assertEqual(c["bucket"], "unknown")

    def test_empty_error_is_unknown(self):
        self.assertEqual(classify("")["id"], "unknown")
        self.assertEqual(classify(None)["id"], "unknown")

    def test_every_class_uses_a_documented_bucket(self):
        # A typo in a bucket name would silently drop the class out of the
        # summary and out of the disable safety note.
        for entry in CLASSES:
            self.assertIn(entry["bucket"], BUCKETS, entry["id"])

    def test_class_ids_are_unique(self):
        ids = [e["id"] for e in CLASSES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_class_carries_a_cause_and_an_action(self):
        for entry in CLASSES:
            self.assertTrue(entry["cause"].strip(), entry["id"])
            self.assertTrue(entry["action"].strip(), entry["id"])


class TestGrouping(unittest.TestCase):
    def test_groups_are_ordered_most_common_first(self):
        records = ([rec("Invalid second parameter.")] * 3 +
                   [rec("Cannot obtain device name used internally by the kernel.")] * 7)
        groups = group(records)
        self.assertEqual([g["count"] for g in groups], [7, 3])

    def test_equal_counts_sort_deterministically(self):
        # Output gets diffed between runs, so ties must not reorder.
        records = [rec("Invalid second parameter."), rec("Invalid first parameter.")]
        first = [g["signature"] for g in group(records)]
        second = [g["signature"] for g in group(list(reversed(records)))]
        self.assertEqual(first, second)

    def test_one_cause_across_many_hosts_is_one_group(self):
        records = [rec(WALK_COUNTER32.replace(": 6", f": {n}"), host=f"sw-{n}")
                   for n in range(20)]
        groups = group(records)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 20)
        self.assertEqual(len(groups[0]["hosts"]), 20)

    def test_group_counts_distinct_hosts_templates_and_keys(self):
        records = [
            rec("Invalid second parameter.", host="a", template="T1", key="k1"),
            rec("Invalid second parameter.", host="a", template="T1", key="k2"),
            rec("Invalid second parameter.", host="b", template="T2", key="k1"),
        ]
        g = group(records)[0]
        self.assertEqual(g["count"], 3)
        self.assertEqual(g["hosts"], ["a", "b"])
        self.assertEqual(g["templates"], ["T1", "T2"])
        self.assertEqual(g["keys"], ["k1", "k2"])

    def test_untemplated_items_do_not_produce_an_empty_template_name(self):
        g = group([rec("Invalid second parameter.", template="")])[0]
        self.assertEqual(g["templates"], [])

    def test_group_carries_the_classification(self):
        g = group([rec("No Such Object available on this agent at this OID")])[0]
        self.assertEqual(g["class"]["bucket"], "hardware")

    def test_no_records_produces_no_groups(self):
        self.assertEqual(group([]), [])


class TestFilters(unittest.TestCase):
    def test_no_filter_matches_everything(self):
        self.assertTrue(matches(rec("e", host="anything")))

    def test_host_filter_is_a_case_insensitive_substring(self):
        r = rec("e", host="Core-SW-01")
        self.assertTrue(matches(r, host="core-sw"))
        self.assertTrue(matches(r, host="SW"))
        self.assertFalse(matches(r, host="edge"))

    def test_template_filter_is_a_case_insensitive_substring(self):
        r = rec("e", template="Linux by SNMP")
        self.assertTrue(matches(r, template="snmp"))
        self.assertFalse(matches(r, template="Windows"))

    def test_template_filter_excludes_items_with_no_template(self):
        self.assertFalse(matches(rec("e", template=""), template="Linux"))

    def test_filters_combine(self):
        r = rec("e", host="core-sw", template="Linux by SNMP")
        self.assertTrue(matches(r, host="core", template="Linux"))
        self.assertFalse(matches(r, host="core", template="Windows"))


if __name__ == "__main__":
    unittest.main()
