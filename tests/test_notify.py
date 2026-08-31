"""Tests for the notification-path checks.

The classification is the part worth guarding. A media type that has never
delivered is broken and someone has to go and fix it; one that fails sometimes
is a reliability problem. Collapsing those two into "failing" is how a silent
alerting outage gets filed alongside a rate limit and ignored.

Everything here is pure, so it runs with no Zabbix and no network.
"""
import pathlib
import sys
import time
import unittest
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from notify import (  # noqa: E402
    classify,
    group_errors,
    mask_sendto,
    medium_status,
    period_covers,
    summarise,
    targeted_userids,
    user_status,
)

MONDAY_1000 = "2026-08-31 10:00"
SATURDAY_1000 = "2026-09-05 10:00"


def moment(text):
    return time.strptime(text, "%Y-%m-%d %H:%M")


def alert(mediatypeid="1", status="1", clock=1000, error="", userid="5"):
    return {"mediatypeid": mediatypeid, "status": status, "clock": str(clock),
            "error": error, "userid": userid, "alerttype": "0"}


def mediatype(mediatypeid="1", name="Email", status="0", kind="0"):
    return {"mediatypeid": mediatypeid, "name": name, "status": status, "type": kind}


def medium(mediatypeid="1", sendto="ops@example.com", active="0", severity="63",
           period="1-7,00:00-24:00"):
    return {"mediatypeid": mediatypeid, "sendto": sendto, "active": active,
            "severity": severity, "period": period}


def user(username="ops", userid="5", medias=None, group_disabled=False):
    return {"userid": userid, "username": username, "medias": medias or [],
            "usrgrps": [{"usrgrpid": "7", "name": "Zabbix administrators",
                         "users_status": "1" if group_disabled else "0"}]}


class TestClassification(unittest.TestCase):
    """Never-succeeded is a different failure from sometimes-fails."""

    def test_no_success_ever_is_broken(self):
        stats = summarise([alert(status="2", error="MAIL failed: 530") for _ in range(9)])
        self.assertEqual(classify(stats["1"]), "broken")

    def test_one_success_among_failures_is_flaky(self):
        alerts = [alert(status="2", error="rate limited") for _ in range(99)]
        alerts.append(alert(status="1"))
        self.assertEqual(classify(summarise(alerts)["1"]), "flaky")

    def test_all_delivered_is_healthy(self):
        self.assertEqual(classify(summarise([alert(status="1")])["1"]), "healthy")

    def test_no_attempts_is_idle_not_broken(self):
        # Nothing was tried, so nothing has been disproven. Reporting this as a
        # failure would make a nightly job cry wolf about an unused media type.
        empty = {"attempts": 0, "delivered": 0, "failed": 0, "pending": 0}
        self.assertEqual(classify(empty), "idle")

    def test_queued_attempts_have_no_verdict_yet(self):
        self.assertEqual(classify(summarise([alert(status="0")])["1"]), "pending")


class TestSummarise(unittest.TestCase):
    def test_counts_split_by_media_type(self):
        stats = summarise([alert("1", "1"), alert("1", "2", error="x"), alert("2", "1")],
                          {"1": "Email", "2": "Discord"})
        self.assertEqual(stats["1"]["attempts"], 2)
        self.assertEqual(stats["1"]["delivered"], 1)
        self.assertEqual(stats["1"]["failed"], 1)
        self.assertEqual(stats["2"]["name"], "Discord")

    def test_keeps_the_latest_of_each_outcome(self):
        stats = summarise([alert(status="1", clock=10), alert(status="1", clock=90),
                           alert(status="2", clock=50, error="e"),
                           alert(status="2", clock=20, error="e")])
        self.assertEqual(stats["1"]["last_success"], 90)
        self.assertEqual(stats["1"]["last_failure"], 50)

    def test_distinct_errors_are_kept_with_counts(self):
        stats = summarise([alert(status="2", error="a"), alert(status="2", error="a"),
                           alert(status="2", error="b")])
        self.assertEqual(stats["1"]["errors"]["a"], 2)
        self.assertEqual(stats["1"]["errors"]["b"], 1)

    def test_attempts_with_no_media_type_are_kept_apart(self):
        # "No media defined for user" never reaches a media type at all, so
        # counting it against one would blame the wrong thing.
        stats = summarise([alert(mediatypeid="0", status="2",
                                 error="No media defined for user.")],
                          {"1": "Email"})
        self.assertIn("0", stats)
        self.assertNotIn("1", stats)


class TestGroupErrors(unittest.TestCase):
    def test_errors_differing_only_in_a_number_are_one_cause(self):
        errors = Counter({"cannot connect to 10.0.0.38 port 3000 after 0 ms": 900,
                          "cannot connect to 10.0.0.38 port 3000 after 1 ms": 100})
        grouped = group_errors(errors)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0][1], 1000)

    def test_genuinely_different_causes_stay_separate(self):
        grouped = group_errors(Counter({"Unknown Webhook": 3, "Invalid Form Body": 1}))
        self.assertEqual([count for _, count in grouped], [3, 1])

    def test_embedded_stack_trace_is_flattened_to_one_line(self):
        grouped = group_errors(Counter({"cannot get URL\n  at [anon] (httprequest.c)": 1}))
        self.assertNotIn("\n", grouped[0][0])


class TestPeriod(unittest.TestCase):
    def test_the_default_period_always_covers(self):
        self.assertTrue(period_covers("1-7,00:00-24:00", moment(MONDAY_1000)))

    def test_outside_the_hours_is_not_covered(self):
        self.assertFalse(period_covers("1-5,18:00-23:00", moment(MONDAY_1000)))

    def test_outside_the_days_is_not_covered(self):
        self.assertFalse(period_covers("1-5,09:00-18:00", moment(SATURDAY_1000)))

    def test_a_later_entry_can_cover_the_moment(self):
        self.assertTrue(period_covers("1-5,18:00-23:00;6-7,09:00-18:00",
                                      moment(SATURDAY_1000)))

    def test_a_single_day_entry_is_understood(self):
        self.assertTrue(period_covers("6,09:00-18:00", moment(SATURDAY_1000)))

    def test_an_unparseable_period_is_treated_as_covering(self):
        # A gap in this parser is not evidence that nobody can be reached.
        self.assertTrue(period_covers("whenever", moment(MONDAY_1000)))
        self.assertTrue(period_covers("", moment(MONDAY_1000)))


class TestMaskSendto(unittest.TestCase):
    def test_a_webhook_url_loses_its_path(self):
        # The path is the credential. Printing it turns a report into a leak.
        masked = mask_sendto("https://example.com/api/webhooks/1427425/iF3K0w1Fe-oD")
        self.assertEqual(masked, "https://example.com/…")

    def test_an_email_address_is_left_alone(self):
        self.assertEqual(mask_sendto("ops@example.com"), "ops@example.com")

    def test_a_bare_name_is_left_alone(self):
        self.assertEqual(mask_sendto("ops-team"), "ops-team")


class TestMediumStatus(unittest.TestCase):
    def test_an_enabled_medium_on_an_enabled_type_is_usable(self):
        self.assertTrue(medium_status(medium(), mediatype(), moment(MONDAY_1000))["usable"])

    def test_a_disabled_media_type_makes_the_medium_useless(self):
        s = medium_status(medium(), mediatype(status="1"), moment(MONDAY_1000))
        self.assertFalse(s["usable"])
        self.assertIn("media type disabled", s["reason"])

    def test_a_medium_disabled_on_the_user_is_useless(self):
        s = medium_status(medium(active="1"), mediatype(), moment(MONDAY_1000))
        self.assertFalse(s["usable"])
        self.assertIn("medium disabled", s["reason"])

    def test_no_severities_selected_means_nothing_ever_matches(self):
        s = medium_status(medium(severity="0"), mediatype(), moment(MONDAY_1000))
        self.assertFalse(s["usable"])
        self.assertIn("severit", s["reason"])

    def test_outside_the_time_period_is_not_reachable_now(self):
        s = medium_status(medium(period="1-5,18:00-23:00"), mediatype(),
                          moment(MONDAY_1000))
        self.assertFalse(s["usable"])
        self.assertIn("time period", s["reason"])

    def test_a_deleted_media_type_is_reported_rather_than_crashing(self):
        s = medium_status(medium(mediatypeid="99"), None, moment(MONDAY_1000))
        self.assertFalse(s["usable"])
        self.assertIn("no longer exists", s["reason"])

    def test_several_email_recipients_are_shown_as_one_target(self):
        s = medium_status(medium(sendto=["a@example.com", "b@example.com"]),
                          mediatype(), moment(MONDAY_1000))
        self.assertEqual(s["sendto"], "a@example.com, b@example.com")


class TestUserReachability(unittest.TestCase):
    types = {"1": mediatype(), "2": mediatype("2", "Discord", status="1", kind="4")}

    def test_a_user_with_one_working_medium_is_reachable(self):
        r = user_status(user(medias=[medium()]), self.types, moment(MONDAY_1000))
        self.assertTrue(r["reachable"])

    def test_a_user_with_no_media_cannot_be_reached(self):
        r = user_status(user(), self.types, moment(MONDAY_1000))
        self.assertFalse(r["reachable"])
        self.assertEqual(r["reason"], "no media configured")

    def test_media_on_disabled_types_only_cannot_be_reached(self):
        r = user_status(user(medias=[medium(mediatypeid="2")]), self.types,
                        moment(MONDAY_1000))
        self.assertFalse(r["reachable"])
        self.assertIn("unusable", r["reason"])

    def test_a_disabled_user_is_unreachable_however_good_the_media_look(self):
        # Zabbix does not notify a user in a disabled group. The media entry
        # still reads as healthy in the frontend, which is the trap.
        r = user_status(user(medias=[medium()], group_disabled=True), self.types,
                        moment(MONDAY_1000))
        self.assertFalse(r["reachable"])
        self.assertIn("disabled", r["reason"])
        self.assertTrue(r["media"][0]["usable"])

    def test_one_working_medium_is_enough(self):
        r = user_status(user(medias=[medium(mediatypeid="2"), medium()]), self.types,
                        moment(MONDAY_1000))
        self.assertTrue(r["reachable"])


class TestTargetedUsers(unittest.TestCase):
    members = {"7": ["1", "5"], "9": ["6"]}

    def message_op(self, users=(), groups=()):
        return {"operationtype": "0",
                "opmessage_usr": [{"userid": u} for u in users],
                "opmessage_grp": [{"usrgrpid": g} for g in groups]}

    def test_users_named_directly_are_targeted(self):
        actions = [{"status": "0", "operations": [self.message_op(users=["3"])]}]
        self.assertEqual(targeted_userids(actions, self.members), {"3"})

    def test_group_membership_is_expanded(self):
        actions = [{"status": "0", "operations": [self.message_op(groups=["7"])]}]
        self.assertEqual(targeted_userids(actions, self.members), {"1", "5"})

    def test_a_disabled_action_targets_nobody(self):
        actions = [{"status": "1", "operations": [self.message_op(users=["3"])]}]
        self.assertEqual(targeted_userids(actions, self.members), set())

    def test_non_message_operations_are_ignored(self):
        # Operation type 4 adds a host to a group. It notifies nobody, and
        # counting it would invent recipients that do not exist.
        actions = [{"status": "0", "operations": [
            {"operationtype": "4", "opgroup": [{"groupid": "5"}]}]}]
        self.assertEqual(targeted_userids(actions, self.members), set())

    def test_recovery_and_update_operations_count_too(self):
        actions = [{"status": "0", "operations": [],
                    "recovery_operations": [self.message_op(users=["8"])],
                    "update_operations": [self.message_op(groups=["9"])]}]
        self.assertEqual(targeted_userids(actions, self.members), {"8", "6"})

    def test_an_unknown_group_contributes_nothing(self):
        actions = [{"status": "0", "operations": [self.message_op(groups=["99"])]}]
        self.assertEqual(targeted_userids(actions, self.members), set())


if __name__ == "__main__":
    unittest.main()
