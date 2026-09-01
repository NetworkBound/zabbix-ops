"""Tests for the shared Zabbix client's failure behaviour.

These run the script as a subprocess rather than importing it, because what is
being asserted is what a person sees in their terminal on the first run. A unit
test on the exception class would have passed throughout the period when every
script in the repository was printing a traceback.

No network is required: the addresses used are chosen to fail immediately.
"""
import pathlib
import subprocess
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


class TestConnectFailsCleanly(unittest.TestCase):
    """connect_or_exit promises a readable message rather than a traceback.

    It used to catch only environment errors, so an unreachable server raised
    from whichever script the user happened to run first. Every script here
    calls it, so the defect showed up identically in all eighteen of them.
    """

    def _run(self, env, script="zbx.py"):
        # A bare environment, so a developer's own ZBX_* variables cannot make
        # a failing case accidentally pass.
        base = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
        base.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script)],
            capture_output=True, text=True, env=base, timeout=60,
        )

    def test_missing_url_is_reported_not_raised(self):
        r = self._run({})
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("ZBX_URL is not set", r.stderr)
        self.assertEqual(r.returncode, 2)

    def test_unreachable_server_is_reported_not_raised(self):
        # Port 9 is discard: closed on a normal host, so this refuses at once.
        r = self._run({"ZBX_URL": "http://127.0.0.1:9/zabbix/api_jsonrpc.php",
                       "ZBX_TOKEN": "x"})
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("Cannot reach", r.stderr)
        self.assertNotEqual(r.returncode, 0)

    def test_unreachable_server_suggests_how_to_check(self):
        r = self._run({"ZBX_URL": "http://127.0.0.1:9/zabbix/api_jsonrpc.php",
                       "ZBX_TOKEN": "x"})
        self.assertIn("curl", r.stderr)

    def test_failure_is_clean_in_other_scripts_too(self):
        # The point of connect_or_exit is that this holds everywhere, not just
        # in zbx.py, which is the one place it is obvious.
        for script in ("audit.py", "reconcile.py", "problems.py"):
            with self.subTest(script=script):
                r = self._run({"ZBX_URL": "http://127.0.0.1:9/zabbix/api_jsonrpc.php",
                               "ZBX_TOKEN": "x"}, script=script)
                self.assertNotIn("Traceback", r.stdout + r.stderr)
                self.assertNotEqual(r.returncode, 0)

    def test_no_success_message_before_the_server_answers(self):
        # Printing "Connected to ..." before verifying would be worse than the
        # traceback: it states something untrue.
        r = self._run({"ZBX_URL": "http://127.0.0.1:9/zabbix/api_jsonrpc.php",
                       "ZBX_TOKEN": "x"})
        self.assertNotIn("Connected to", r.stdout)


if __name__ == "__main__":
    unittest.main()
