"""Tests for the isolation boundary — the entry's central architectural claim.

If these pass, "the agent cannot sign" is a fact about the process table rather than a
sentence in a system prompt.
"""

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from app.supervisor import (AGENT_ENV_ALLOWLIST, IsolationError, REPO_ROOT,
                            agent_env_report, build_agent_env, run_agent)

SIGNING_VARS = {
    "FOXIT_ESIGN_CLIENT_ID": "esign-id-should-never-reach-the-agent",
    "FOXIT_ESIGN_CLIENT_SECRET": "esign-secret-should-never-reach-the-agent",
    "FOXIT_ESIGN_HOST": "https://na1.foxitesign.foxit.com",
}


class TestScrubbedEnvironment(unittest.TestCase):
    def test_signing_credentials_are_absent(self):
        base = dict(os.environ)
        base.update(SIGNING_VARS)
        env = build_agent_env(base)
        for name in SIGNING_VARS:
            self.assertNotIn(name, env)
        self.assertFalse([k for k in env if "ESIGN" in k.upper()])

    def test_the_secret_value_is_absent_not_merely_the_name(self):
        base = dict(os.environ)
        base.update(SIGNING_VARS)
        env = build_agent_env(base)
        blob = json.dumps(env)
        for value in SIGNING_VARS.values():
            self.assertNotIn(value, blob)

    def test_it_is_an_allowlist_not_a_denylist(self):
        """A denylist forgets a variable. This must not copy anything it wasn't told to."""
        base = {"TOTALLY_NEW_SECRET": "x", "FOXIT_ESIGN_TOKEN_V2": "y", "PATH": "/usr/bin"}
        env = build_agent_env(base)
        self.assertNotIn("TOTALLY_NEW_SECRET", env)
        self.assertNotIn("FOXIT_ESIGN_TOKEN_V2", env)
        self.assertIn("PATH", env)

    def test_docgen_credentials_are_present(self):
        """The agent must still be able to do its actual job."""
        base = {"FOXIT_CLOUD_API_CLIENT_ID": "a", "FOXIT_CLOUD_API_CLIENT_SECRET": "b",
                "FOXIT_CLOUD_API_HOST": "https://na1.fusion.foxit.com/pdf-services"}
        env = build_agent_env(base)
        self.assertEqual(env["FOXIT_CLOUD_API_CLIENT_ID"], "a")

    def test_a_bad_allowlist_entry_raises_rather_than_leaking(self):
        base = dict(SIGNING_VARS)
        with self.assertRaises(IsolationError):
            build_agent_env(base, allowlist=tuple(AGENT_ENV_ALLOWLIST) + ("FOXIT_ESIGN_CLIENT_ID",))

    def test_env_report_shows_names_never_values(self):
        os.environ.update(SIGNING_VARS)
        try:
            report = agent_env_report()
            self.assertEqual(report["signing_variables_in_agent"], [])
            self.assertTrue(report["signing_variables_in_parent"])
            blob = json.dumps(report)
            for value in SIGNING_VARS.values():
                self.assertNotIn(value, blob)
        finally:
            for k in SIGNING_VARS:
                os.environ.pop(k, None)


class TestSpawnedProcessReality(unittest.TestCase):
    """Not a unit test of a dict — an assertion about a real OS process."""

    def test_child_process_environment_has_no_signing_credentials(self):
        parent_env = dict(os.environ)
        parent_env.update(SIGNING_VARS)
        child_env = build_agent_env(parent_env)
        proc = subprocess.run(
            [sys.executable, "-c",
             "import os,json;print(json.dumps(dict(os.environ)))"],
            capture_output=True, text=True, env=child_env, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        seen = json.loads(proc.stdout)
        self.assertFalse([k for k in seen if "ESIGN" in k.upper()],
                         f"signing credentials reached the child: {sorted(seen)}")

    def test_agent_refuses_to_run_if_signing_credentials_somehow_reach_it(self):
        """Defence in depth: if the boundary above is ever broken, the agent stops."""
        env = build_agent_env()
        env["FOXIT_ESIGN_CLIENT_SECRET"] = "leaked"
        proc = subprocess.run(
            [sys.executable, "-m", "agent.run_agent"],
            input=json.dumps({"prompt": "draft something"}),
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("refusing to run", proc.stdout)


class TestNoSecondPathToSigning(unittest.TestCase):
    """Foxit left signing out of their MCP toolset. We made sure there is no other way in."""

    def _agent_sources(self):
        return list((REPO_ROOT / "agent").glob("*.py"))

    def test_the_agent_package_never_imports_the_esign_client(self):
        for path in self._agent_sources():
            src = path.read_text()
            self.assertNotIn("esign_client", src, f"{path.name} references the eSign client")
            self.assertFalse(re.search(r"^\s*(from|import)\s+app\b", src, re.M),
                             f"{path.name} imports from the parent's app package")

    def test_the_agent_package_never_names_the_esign_host(self):
        for path in self._agent_sources():
            self.assertNotIn("foxitesign", path.read_text().lower(),
                             f"{path.name} names the eSign host")

    def test_only_one_module_can_release_an_envelope(self):
        callers = [p.name for p in (REPO_ROOT / "app").glob("*.py")
                   if "send_draft_folder" in p.read_text() and p.name != "esign_client.py"]
        self.assertEqual(callers, ["main.py"],
                         f"unexpected callers of sendDraftFolder: {callers}")

    def test_the_send_route_is_guarded_by_an_approval(self):
        src = (REPO_ROOT / "app" / "main.py").read_text()
        send_route = src.split("def send_for_signature")[1].split("\n@app")[0]
        self.assertIn("assert_may_send", send_route)
        guard_at = send_route.index("assert_may_send")
        create_at = send_route.index("create_draft_folder")
        self.assertLess(guard_at, create_at,
                        "the approval must be spent before any envelope is created")

    def test_create_folder_never_sends_immediately(self):
        src = (REPO_ROOT / "app" / "esign_client.py").read_text()
        self.assertIn('"sendNow": False', src)
        self.assertNotIn('"sendNow": True', src)


if __name__ == "__main__":
    unittest.main()
