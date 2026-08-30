"""Tests for export canonicalisation and the semantic diff.

The ordering tests matter more than they look. Canonicalisation exists to make
diffs trustworthy, so a bug that silently reorders a positional list turns the
tool into one that corrupts configuration while claiming to tidy it.
"""
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from canon import canonicalise, classify, diff, dumps, normalise_document  # noqa: E402


class TestOrderingIsPreserved(unittest.TestCase):
    """Regression tests. The first version sorted these and broke them."""

    def test_preprocessing_parameters_keep_their_order(self):
        # SNMP_WALK_TO_JSON takes (macro, oid, format) triplets. Sorted
        # alphabetically the OID leads and the step means something else
        # entirely — this is the bug that shipped and had to be found by an
        # import rejecting the file.
        step = {"type": "SNMP_WALK_TO_JSON",
                "parameters": ["{#ENTRY}", "1.3.6.1.2.1.2.2.1.2", "0"]}
        out = canonicalise({"preprocessing": [step]})
        self.assertEqual(out["preprocessing"][0]["parameters"],
                         ["{#ENTRY}", "1.3.6.1.2.1.2.2.1.2", "0"])

    def test_preprocessing_steps_keep_their_order(self):
        # Steps form a pipeline: each consumes the previous result.
        steps = [{"type": "JSONPATH", "parameters": ["$.b"]},
                 {"type": "MULTIPLIER", "parameters": ["8"]},
                 {"type": "CHANGE_PER_SECOND"}]
        out = canonicalise({"preprocessing": steps})
        self.assertEqual([s["type"] for s in out["preprocessing"]],
                         ["JSONPATH", "MULTIPLIER", "CHANGE_PER_SECOND"])

    def test_any_scalar_list_keeps_its_order(self):
        # A list of scalars has no identity to sort by and is nearly always
        # positional, whatever the key is called.
        out = canonicalise({"anything": ["z", "a", "m"]})
        self.assertEqual(out["anything"], ["z", "a", "m"])

    def test_dashboard_widgets_keep_their_order(self):
        widgets = [{"type": "clock"}, {"type": "problems"}, {"type": "graph"}]
        out = canonicalise({"widgets": widgets})
        self.assertEqual([w["type"] for w in out["widgets"]],
                         ["clock", "problems", "graph"])


class TestCanonicalisation(unittest.TestCase):
    def test_items_are_sorted_by_key(self):
        out = canonicalise({"items": [{"key": "b"}, {"key": "a"}]})
        self.assertEqual([i["key"] for i in out["items"]], ["a", "b"])

    def test_triggers_sort_by_name_then_expression(self):
        out = canonicalise({"triggers": [
            {"name": "x", "expression": "b"},
            {"name": "x", "expression": "a"},
        ]})
        self.assertEqual([t["expression"] for t in out["triggers"]], ["a", "b"])

    def test_empty_values_are_dropped(self):
        out = canonicalise({"a": "", "b": [], "c": {}, "d": None, "e": "keep"})
        self.assertEqual(out, {"e": "keep"})

    def test_zero_is_not_treated_as_empty(self):
        # 0 is a real value; dropping it would change a delay or a format flag.
        self.assertEqual(canonicalise({"n": 0}), {"n": 0})

    def test_version_header_is_removed(self):
        doc = normalise_document(json.dumps(
            {"zabbix_export": {"version": "7.4", "templates": [{"template": "T"}]}}))
        self.assertNotIn("version", doc)
        self.assertIn("templates", doc)

    def test_same_content_different_order_normalises_identically(self):
        a = json.dumps({"zabbix_export": {"version": "7.0", "items": [
            {"key": "b", "name": "B"}, {"key": "a", "name": "A"}]}})
        b = json.dumps({"zabbix_export": {"version": "7.4", "items": [
            {"key": "a", "name": "A"}, {"key": "b", "name": "B"}]}})
        self.assertEqual(dumps(normalise_document(a)), dumps(normalise_document(b)))

    def test_uuids_are_preserved(self):
        # Import matches on UUID first. Stripping or regenerating one turns an
        # update into a duplicate.
        u = "0123456789abcdef0123456789abcdef"
        out = canonicalise({"templates": [{"template": "T", "uuid": u}]})
        self.assertEqual(out["templates"][0]["uuid"], u)


class TestDiff(unittest.TestCase):
    def test_identical_documents_have_no_changes(self):
        d = {"items": [{"key": "a", "delay": "1m"}]}
        self.assertEqual(diff(d, d), [])

    def test_changed_field_is_mutating(self):
        c = classify(diff({"items": [{"key": "a", "delay": "1m"}]},
                          {"items": [{"key": "a", "delay": "5m"}]}))
        self.assertEqual(len(c["mutating"]), 1)
        self.assertEqual(len(c["destructive"]), 0)

    def test_removed_item_is_destructive_and_loses_data(self):
        c = classify(diff({"items": [{"key": "a"}, {"key": "b"}]},
                          {"items": [{"key": "a"}]}))
        self.assertEqual(len(c["destructive"]), 1)
        self.assertEqual(len(c["data_loss"]), 1)
        self.assertIn("history", c["data_loss"][0]["detail"])

    def test_added_item_is_additive_only(self):
        c = classify(diff({"items": [{"key": "a"}]},
                          {"items": [{"key": "a"}, {"key": "b"}]}))
        self.assertEqual(len(c["additive"]), 1)
        self.assertEqual(len(c["destructive"]), 0)

    def test_removed_trigger_reports_open_problems(self):
        c = classify(diff({"triggers": [{"name": "t", "expression": "e"}]},
                          {"triggers": []}))
        self.assertEqual(len(c["data_loss"]), 1)
        self.assertIn("problem", c["data_loss"][0]["detail"])

    def test_reordering_alone_produces_no_diff(self):
        a = canonicalise({"items": [{"key": "b"}, {"key": "a"}]})
        b = canonicalise({"items": [{"key": "a"}, {"key": "b"}]})
        self.assertEqual(diff(a, b), [])


if __name__ == "__main__":
    unittest.main()
