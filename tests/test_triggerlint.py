"""Tests for the trigger expression linter.

Two things are being defended here. The first is that each rule still fires on
the mistake it was written for -- the CPU trigger built on the idle percentage
is the one that started this, and it has a test of its own. The second matters
more: that each rule stays quiet on the innocent version of the same shape. A
linter is only useful for as long as people read its output, so every rule below
has a negative case sitting next to its positive one, and several of the
negatives are regressions from findings this tool produced against a real server
and had to be taught not to.

No server is contacted. The rules are pure functions of an Estate, and the
Estate here is built by hand.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import triggerlint  # noqa: E402
from triggerlint import Estate, parse_refs, render_expression, split_args  # noqa: E402

HOSTS = {
    "1": {"host": "web01", "status": "0", "groups": ["Homelab/Containers"],
          "templates": []},
    "2": {"host": "pve01", "status": "0", "groups": ["Homelab/Infrastructure"],
          "templates": ["Proxmox VE by HTTP"]},
    "9": {"host": "Homelab LXC Container", "status": "3", "groups": [],
          "templates": []},
}

_serial = iter(range(1000, 9999))


def item(key, name="", units="", value_type="0", preprocessing=(), status="0",
         state="0", error="", hostid="1"):
    return {"itemid": str(next(_serial)), "hostid": hostid, "key_": key,
            "name": name or key, "units": units, "value_type": value_type,
            "preprocessing": list(preprocessing), "status": status,
            "state": state, "error": error}


def trigger(expression, refs, description="Trigger", priority="3", hostid="1",
            **fields):
    """One trigger, with the function records its expression refers to.

    ``refs`` maps the functionid used in the expression to the function name
    and the item it reads, which is how Zabbix stores it.
    """
    t = {"triggerid": str(next(_serial)), "expression": expression,
         "description": description, "priority": priority, "manual_close": "0",
         "recovery_mode": "0", "event_name": "", "flags": "0",
         "templateid": "0", "dependencies": [],
         "hosts": [{"hostid": hostid, "host": HOSTS[hostid]["host"],
                    "status": HOSTS[hostid]["status"]}],
         "_refs": refs}
    t.update(fields)
    return t


def estate(*triggers, macros=None, availability=(), hosts=None):
    functions, items, out = {}, {}, []
    for t in triggers:
        t = dict(t)
        for fid, (fname, it) in t.pop("_refs").items():
            items[it["itemid"]] = it
            functions[fid] = {"functionid": fid, "itemid": it["itemid"],
                              "function": fname, "parameter": "$"}
        out.append(t)
    return Estate(definitions=out, host_triggers=out, items=items,
                  hosts=hosts or HOSTS, functions=functions,
                  macros=macros or {}, availability=set(availability))


def rules(name, est):
    return triggerlint.CHECKS[name](est)


class TestExpressionParsing(unittest.TestCase):
    def test_key_parameters_survive_the_argument_split(self):
        # A naive split on commas tears this key into pieces and the item is
        # then unrecognisable.
        self.assertEqual(split_args('$,"proc.num[,,,-m nginx]",5m'),
                         ["$", '"proc.num[,,,-m nginx]"', "5m"])

    def test_comparison_is_attached_to_the_reference_before_it(self):
        refs = parse_refs("{1}>85")
        self.assertEqual(refs[0]["op"], ">")
        self.assertEqual(refs[0]["num"], 85.0)

    def test_unit_suffix_is_kept_separate_from_the_number(self):
        refs = parse_refs("{1}<20M")
        self.assertEqual((refs[0]["num"], refs[0]["suffix"]), (20.0, "M"))

    def test_macro_operand_is_recognised(self):
        self.assertEqual(parse_refs('{1}>"{$MEM.PUSED.MAX:=85}"')[0]["macro"],
                         "MEM.PUSED.MAX:=85")

    def test_arithmetic_context_is_marked(self):
        # "100-free>80" is a correct utilisation trigger. Reading the sense off
        # the item alone would call it inverted.
        self.assertTrue(parse_refs("100-{1}>80")[0]["arith"])
        self.assertFalse(parse_refs("{1}>80")[0]["arith"])

    def test_constant_that_starts_a_calculation_is_not_a_threshold(self):
        # ">(90/100)*{2}" compares against another item, not against 90.
        self.assertIsNone(parse_refs("{1}>(90/100)*{2}")[0]["num"])

    def test_reversed_comparison_is_not_parsed(self):
        # A documented blind spot. Missing a finding is the safe direction;
        # this test exists so the choice stays deliberate.
        self.assertEqual(parse_refs("80<{1}")[0]["op"], "")

    def test_rendering_keeps_macros_unexpanded(self):
        functions = {"1": {"itemid": "7", "function": "last",
                           "parameter": '$,"{$PG.PASSWORD}"'}}
        items = {"7": {"itemid": "7", "hostid": "1", "key_": "pgsql.uptime"}}
        out = render_expression("{1}<10m", functions, items, HOSTS)
        self.assertEqual(out, 'last(/web01/pgsql.uptime,"{$PG.PASSWORD}")<10m')


class TestInvertedSense(unittest.TestCase):
    def test_idle_percentage_compared_upward_is_flagged(self):
        # The trigger this tool was written for.
        est = estate(trigger("{1}>85", {"1": ("avg", item("system.cpu.util[,idle]",
                                                          units="%"))},
                             description="High CPU"))
        found = rules("inverted_sense", est)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["confidence"], "high")

    def test_idle_percentage_compared_downward_is_correct(self):
        est = estate(trigger("{1}<20", {"1": ("avg", item("system.cpu.util[,idle]",
                                                          units="%"))}))
        self.assertEqual(rules("inverted_sense", est), [])

    def test_utilisation_compared_upward_is_correct(self):
        est = estate(trigger("{1}>85", {"1": ("avg", item("system.cpu.util[,user]",
                                                          units="%"))}))
        self.assertEqual(rules("inverted_sense", est), [])

    def test_free_space_inside_a_calculation_is_not_flagged(self):
        # "100 - free > 80" is the usual way to build a utilisation trigger out
        # of a free-space item, and it is right.
        est = estate(trigger("100-{1}>80", {"1": ("last", item("vfs.fs.size[/,pfree]",
                                                               units="%"))}))
        self.assertEqual(rules("inverted_sense", est), [])

    def test_absolute_threshold_is_not_judged(self):
        # "available memory < 20M" is fine, and 20 is not a percentage.
        est = estate(trigger("{1}<20M", {"1": ("last", item("vm.memory.size[available]",
                                                            units="B"))}))
        self.assertEqual(rules("inverted_sense", est), [])

    def test_availability_manager_is_not_an_inverse_metric(self):
        # Regression. "availability manager" is a Zabbix process name and the
        # item is a busy percentage; a substring match called it inverse and
        # produced a confident finding about a stock template.
        est = estate(trigger("{1}>75", {"1": ("avg", item(
            "zabbix[process,availability manager,avg,busy]",
            name="Utilization of availability manager internal processes",
            units="%"))}))
        self.assertEqual(rules("inverted_sense", est), [])

    def test_utilisation_compared_downward_is_only_a_question(self):
        est = estate(trigger("{1}<5", {"1": ("min", item("system.cpu.util[,user]",
                                                         units="%"))}))
        found = rules("inverted_sense", est)
        self.assertEqual([f["confidence"] for f in found], ["low"])


class TestNodataManualClose(unittest.TestCase):
    def test_nodata_only_trigger_without_manual_close(self):
        est = estate(trigger("{1}=1", {"1": ("nodata", item("agent.ping"))},
                             description="Host is unreachable"))
        found = rules("nodata_no_manual_close", est)
        self.assertEqual([f["confidence"] for f in found], ["high"])

    def test_manual_close_set_is_not_flagged(self):
        est = estate(trigger("{1}=1", {"1": ("nodata", item("agent.ping"))},
                             manual_close="1"))
        self.assertEqual(rules("nodata_no_manual_close", est), [])

    def test_nodata_alongside_another_term_is_less_certain(self):
        # The other term may still recover the problem on its own.
        est = estate(trigger("{1}<10m and {2}=0",
                             {"1": ("last", item("sysUpTime")),
                              "2": ("nodata", item("sysUpTime"))}))
        self.assertEqual([f["confidence"] for f in rules("nodata_no_manual_close", est)],
                         ["medium"])

    def test_trigger_without_nodata_is_not_flagged(self):
        est = estate(trigger("{1}=0", {"1": ("max", item("icmpping"))}))
        self.assertEqual(rules("nodata_no_manual_close", est), [])


class TestSeverityMismatch(unittest.TestCase):
    def test_unreachable_filed_as_warning(self):
        est = estate(trigger("{1}=0", {"1": ("max", item("icmpping"))},
                             description="web01 is unreachable", priority="2"))
        self.assertEqual([f["confidence"] for f in rules("severity_mismatch", est)],
                         ["medium"])

    def test_unreachable_filed_as_information(self):
        est = estate(trigger("{1}=0", {"1": ("max", item("icmpping"))},
                             description="web01 is unreachable", priority="1"))
        self.assertEqual([f["confidence"] for f in rules("severity_mismatch", est)],
                         ["high"])

    def test_unreachable_at_high_matches_the_convention(self):
        est = estate(trigger("{1}=0", {"1": ("max", item("icmpping"))},
                             description="web01 is unreachable", priority="4"))
        self.assertEqual(rules("severity_mismatch", est), [])

    def test_informational_wording_at_disaster(self):
        est = estate(trigger("{1}<0", {"1": ("change", item("system.uptime"))},
                             description="web01 has been restarted", priority="5"))
        self.assertEqual([f["confidence"] for f in rules("severity_mismatch", est)],
                         ["medium"])

    def test_informational_wording_at_information(self):
        est = estate(trigger("{1}<0", {"1": ("change", item("system.uptime"))},
                             description="web01 has been restarted", priority="1"))
        self.assertEqual(rules("severity_mismatch", est), [])

    def test_unclassified_severity_is_reported_quietly(self):
        est = estate(trigger("{1}>5", {"1": ("last", item("queue.length"))},
                             description="Queue is long", priority="0"))
        self.assertEqual([f["confidence"] for f in rules("severity_mismatch", est)],
                         ["low"])

    def test_no_data_in_an_event_name_is_not_an_outage_claim(self):
        # Regression. Stock templates append "(or no data for 30m)" to the
        # event name of ordinary fetch-failure triggers, and matching on it
        # produced forty findings across vendor templates in one run.
        est = estate(trigger("{1}=1", {"1": ("last", item("mysql.get_status"))},
                             description="MySQL: Failed to fetch info data",
                             priority="2",
                             event_name="MySQL: Failed to fetch info data "
                                        "(or no data for 30m)"))
        self.assertEqual(rules("severity_mismatch", est), [])


class TestMissingDependency(unittest.TestCase):
    def guest(self, **fields):
        return trigger("{1}=1", {"1": ("nodata", item("agent.ping"))},
                       description="web01 is unreachable", **fields)

    def test_guest_availability_trigger_with_no_dependency(self):
        est = estate(self.guest(), availability=["2"])
        found = rules("missing_dependency", est)
        self.assertEqual([f["confidence"] for f in found], ["medium"])
        self.assertIn("pve01", found[0]["why"])

    def test_dependency_already_present(self):
        est = estate(self.guest(dependencies=[{"triggerid": "77"}]),
                     availability=["2"])
        self.assertEqual(rules("missing_dependency", est), [])

    def test_no_hypervisor_means_nothing_to_suggest(self):
        est = estate(self.guest())
        self.assertEqual(rules("missing_dependency", est), [])

    def test_service_check_is_not_a_host_availability_trigger(self):
        # "LDAP port 389 not responding" reads like an availability trigger and
        # is not one. Hiding it behind the hypervisor would hide a real fault.
        est = estate(trigger("{1}=0",
                             {"1": ("last", item("net.tcp.service[tcp,,389]"))},
                             description="openldap: LDAP port 389 not responding"),
                     availability=["2"])
        self.assertEqual(rules("missing_dependency", est), [])


class TestCounterThreshold(unittest.TestCase):
    def test_raw_error_counter_with_a_fixed_threshold(self):
        est = estate(trigger("{1}>2", {"1": ("min", item(
            "net.if.in.errors[ifInErrors.3]", value_type="3"))}))
        found = rules("counter_threshold", est)
        self.assertEqual([f["confidence"] for f in found], ["medium"])

    def test_change_per_second_preprocessing_makes_it_a_rate(self):
        est = estate(trigger("{1}>2", {"1": ("min", item(
            "net.if.in.errors[ifInErrors.3]", value_type="3",
            preprocessing=[{"type": "10", "params": ""}]))}))
        self.assertEqual(rules("counter_threshold", est), [])

    def test_export_spelling_of_the_preprocessing_step_is_understood(self):
        est = estate(trigger("{1}>2", {"1": ("min", item(
            "net.if.in.errors[ifInErrors.3]", value_type="3",
            preprocessing=[{"type": "CHANGE_PER_SECOND"}]))}))
        self.assertEqual(rules("counter_threshold", est), [])

    def test_change_in_the_expression_differentiates_it(self):
        est = estate(trigger("{1}>2", {"1": ("change", item(
            "net.if.in.errors[ifInErrors.3]", value_type="3"))}))
        self.assertEqual(rules("counter_threshold", est), [])

    def test_a_gauge_that_happens_to_say_total_is_not_a_counter(self):
        # Regression. docker.containers.total is a current count, and matching
        # the word "total" made this rule's only live finding a wrong one.
        est = estate(trigger("{1}>0", {"1": ("last", item(
            "docker.containers.total", value_type="3"))}))
        self.assertEqual(rules("counter_threshold", est), [])


class TestHardcodedThreshold(unittest.TestCase):
    MACROS = {"9": [{"macro": "{$FS.PUSED.MAX}", "value": "80"}]}

    def test_name_promises_a_macro_the_expression_does_not_use(self):
        est = estate(trigger("{1}>80", {"1": ("last", item("vfs.fs.size[/,pused]",
                                                           hostid="9"))},
                             description="Disk space low (>{$FS.PUSED.MAX:=80}%)",
                             hostid="9"),
                     macros=self.MACROS)
        found = rules("hardcoded_threshold", est)
        self.assertEqual([f["confidence"] for f in found], ["medium"])

    def test_expression_that_uses_the_macro_is_not_flagged(self):
        est = estate(trigger('{1}>"{$FS.PUSED.MAX:=80}"',
                             {"1": ("last", item("vfs.fs.size[/,pused]", hostid="9"))},
                             description="Disk space low (>{$FS.PUSED.MAX:=80}%)",
                             hostid="9"),
                     macros=self.MACROS)
        self.assertEqual(rules("hardcoded_threshold", est), [])

    def test_sibling_trigger_using_the_macro_is_a_gentle_hint(self):
        shared = item("vfs.fs.size[/,pused]", hostid="9")
        est = estate(
            trigger('{1}>"{$FS.PUSED.MAX:=80}"', {"1": ("last", shared)},
                    description="Disk space low", hostid="9"),
            trigger("{2}>90", {"2": ("last", shared)},
                    description="Disk space critical", hostid="9"),
            macros=self.MACROS)
        found = rules("hardcoded_threshold", est)
        self.assertEqual([f["confidence"] for f in found], ["low"])

    def test_state_test_is_not_a_threshold(self):
        # Regression. "nodata(...)=1" is a boolean, and no macro belongs in it.
        est = estate(trigger("{1}=1", {"1": ("nodata", item("agent.ping",
                                                            hostid="9"))},
                             description="Zabbix agent is not available",
                             hostid="9"),
                     macros=self.MACROS)
        self.assertEqual(rules("hardcoded_threshold", est), [])

    def test_unrelated_macro_is_not_volunteered(self):
        est = estate(trigger("{1}>10", {"1": ("avg", item("icmppingloss",
                                                          hostid="9"))},
                             description="High packet loss", hostid="9"),
                     macros=self.MACROS)
        self.assertEqual(rules("hardcoded_threshold", est), [])


class TestMissingItem(unittest.TestCase):
    def test_reference_that_does_not_resolve(self):
        est = estate(trigger("{1}>0", {"1": ("last", item("agent.ping"))}))
        est.functions.clear()
        found = rules("missing_item", est)
        self.assertEqual([f["confidence"] for f in found], ["high"])

    def test_disabled_item(self):
        est = estate(trigger("{1}=0", {"1": ("last", item("net.tcp.service[tcp,,389]",
                                                          status="1"))}))
        found = rules("missing_item", est)
        self.assertEqual([f["confidence"] for f in found], ["medium"])
        self.assertIn("disabled", found[0]["why"])

    def test_unsupported_item(self):
        est = estate(trigger("{1}=0", {"1": ("last", item(
            "net.tcp.service[tcp,,53]", state="1",
            error="Check service item must have IP parameter"))}))
        found = rules("missing_item", est)
        self.assertEqual([f["confidence"] for f in found], ["medium"])
        self.assertIn("unsupported", found[0]["why"])

    def test_healthy_item_is_not_flagged(self):
        est = estate(trigger("{1}=0", {"1": ("last", item("agent.ping"))}))
        self.assertEqual(rules("missing_item", est), [])


class TestFindingShape(unittest.TestCase):
    """Every rule has to produce something a report can render."""

    def test_finding_carries_owner_severity_and_expression(self):
        est = estate(trigger("{1}>85", {"1": ("avg", item("system.cpu.util[,idle]",
                                                          units="%"))},
                             description="High CPU", priority="3"))
        f = rules("inverted_sense", est)[0]
        self.assertEqual(f["owner"], "web01")
        self.assertEqual(f["owner_kind"], "host")
        self.assertEqual(f["severity"], "AVERAGE")
        self.assertEqual(f["expression"], "avg(/web01/system.cpu.util[,idle])>85")
        self.assertIn(f["confidence"], triggerlint.CONFIDENCE)

    def test_template_owned_triggers_are_labelled_as_templates(self):
        est = estate(trigger("{1}>85", {"1": ("avg", item("system.cpu.util[,idle]",
                                                          units="%", hostid="9"))},
                             hostid="9"))
        self.assertEqual(rules("inverted_sense", est)[0]["owner_kind"], "template")

    def test_every_rule_name_has_a_check_and_a_summary(self):
        self.assertEqual(set(triggerlint.CHECKS), set(triggerlint.RULES))


if __name__ == "__main__":
    unittest.main()
