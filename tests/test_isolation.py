"""Tests for the isolation boundary — the entry's central architectural claim.

If these pass, "the agent cannot sign" is a fact about the process table rather than a
sentence in a system prompt.

**Amended 2026-08-30.** Foxit unified their APIs behind a single credential pair, so
the old shape of this file — signing credentials withheld, Document Generation
credentials granted — was asserting a distinction the vendor no longer makes. The
assertion that broke (`test_docgen_credentials_are_present`) was the one encoding the
mistaken model, and it has been inverted rather than deleted, because the inverted
version is the property that now matters: **no Foxit credential of any name reaches
the agent.**
"""

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from app.esign_client import (DEFAULT_HOST, ENDPOINTS, ESIGN_BASE, USER_AGENT,
                              ESignClient, normalise_created)
from app.supervisor import (AGENT_ENV_ALLOWLIST, IsolationError, REPO_ROOT,
                            agent_env_report, build_agent_env, run_agent)

#: The real variable names, post-unification. One pair, one host, both products.
SIGNING_VARS = {
    "FOXIT_CLIENT_ID": "id-should-never-reach-the-agent",
    "FOXIT_CLIENT_SECRET": "secret-should-never-reach-the-agent",
    "FOXIT_API_HOST": "https://na1.fusion.foxit.com",
}

#: The names the credential used to travel under, when the project believed Document
#: Generation and eSign were separately scoped. Kept in the suite deliberately: these
#: are exactly the variables an older deployment, an older README or an older run's
#: notes would set, and they must now be treated as signing credentials too.
LEGACY_VARS = {
    "FOXIT_CLOUD_API_CLIENT_ID": "legacy-docgen-id",
    "FOXIT_CLOUD_API_CLIENT_SECRET": "legacy-docgen-secret",
    "FOXIT_ESIGN_CLIENT_ID": "legacy-esign-id",
    "FOXIT_ESIGN_CLIENT_SECRET": "legacy-esign-secret",
}


def _code_only(path: Path) -> str:
    """Source with comments and string literals removed.

    Structural tests that grep a file are only as good as their ability to tell code
    from commentary. Without this, documenting a mistake in a docstring makes the test
    that guards against the mistake fail — which teaches the next person to stop
    documenting mistakes.
    """
    import io
    import tokenize

    kept = []
    with open(path, "rb") as fh:
        for tok in tokenize.tokenize(io.BytesIO(fh.read()).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(tok.string)
    return " ".join(kept)


class TestScrubbedEnvironment(unittest.TestCase):
    def test_signing_credentials_are_absent(self):
        base = dict(os.environ)
        base.update(SIGNING_VARS)
        env = build_agent_env(base)
        for name in SIGNING_VARS:
            self.assertNotIn(name, env)
        self.assertFalse([k for k in env if "FOXIT" in k.upper()])

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

    def test_no_foxit_credential_of_any_kind_reaches_the_agent(self):
        """★ The 2026-08-30 correction, as an assertion.

        This test replaces `test_docgen_credentials_are_present`, which asserted the
        opposite and passed for four days. Foxit's APIs are unified: the same
        `client_id` / `client_secret` pair authenticates
        `/document-generation/api/...` and `/esign/api/v1/...` on one host. A
        "Document Generation credential" can therefore create and release a signature
        envelope in a single call, so there is no version of it that is safe to put in
        a process we are claiming cannot sign.
        """
        base = dict(SIGNING_VARS)
        base.update(LEGACY_VARS)
        base["PATH"] = "/usr/bin"
        env = build_agent_env(base)
        leaked = sorted(k for k in env if "FOXIT" in k.upper())
        self.assertEqual(leaked, [], f"Foxit credentials reached the agent: {leaked}")
        blob = json.dumps(env)
        for value in list(SIGNING_VARS.values()) + list(LEGACY_VARS.values()):
            self.assertNotIn(value, blob)

    def test_the_allowlist_itself_names_no_foxit_variable(self):
        """Guards the fix, not just its effect.

        The failure mode being prevented is a later edit that re-adds a Foxit variable
        to the allowlist for a good-sounding reason ("the agent only needs to *render*").
        """
        self.assertFalse([n for n in AGENT_ENV_ALLOWLIST if "FOXIT" in n.upper()])

    def test_a_bad_allowlist_entry_raises_rather_than_leaking(self):
        base = dict(SIGNING_VARS)
        with self.assertRaises(IsolationError):
            build_agent_env(base, allowlist=tuple(AGENT_ENV_ALLOWLIST) + ("FOXIT_CLIENT_ID",))

    def test_a_legacy_docgen_variable_also_raises(self):
        """The old name must fail as loudly as the new one.

        Before the unification this was the *permitted* variable. A deployment that
        still sets it is not making a naming mistake — it is offering the agent a
        working signing credential.
        """
        base = dict(LEGACY_VARS)
        with self.assertRaises(IsolationError):
            build_agent_env(
                base, allowlist=tuple(AGENT_ENV_ALLOWLIST) + ("FOXIT_CLOUD_API_CLIENT_SECRET",))

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
        parent_env.update(LEGACY_VARS)
        child_env = build_agent_env(parent_env)
        proc = subprocess.run(
            [sys.executable, "-c",
             "import os,json;print(json.dumps(dict(os.environ)))"],
            capture_output=True, text=True, env=child_env, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        seen = json.loads(proc.stdout)
        self.assertFalse([k for k in seen if "FOXIT" in k.upper() or "ESIGN" in k.upper()],
                         f"signing credentials reached the child: {sorted(seen)}")

    def test_agent_refuses_to_run_if_signing_credentials_somehow_reach_it(self):
        """Defence in depth: if the boundary above is ever broken, the agent stops."""
        env = build_agent_env()
        env["FOXIT_CLIENT_SECRET"] = "leaked"
        proc = subprocess.run(
            [sys.executable, "-m", "agent.run_agent"],
            input=json.dumps({"prompt": "draft something"}),
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("refusing to run", proc.stdout)

    def test_the_agent_also_refuses_a_legacy_document_generation_credential(self):
        env = build_agent_env()
        env["FOXIT_CLOUD_API_CLIENT_SECRET"] = "leaked-under-the-old-name"
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
            self.assertNotIn("docgen_client", src, f"{path.name} references the DocGen client")
            self.assertFalse(re.search(r"^\s*(from|import)\s+app\b", src, re.M),
                             f"{path.name} imports from the parent's app package")

    def test_the_agent_package_contains_no_foxit_client_at_all(self):
        """★ Stronger than the test it replaces, and for a specific reason.

        The old assertion was that no agent module names the eSign *host*. Under the
        unified API both products answer on the same host, so that test could only
        ever have passed — it had quietly become unfalsifiable. What is checkable, and
        what actually matters, is that nothing in this package is capable of
        authenticating to Foxit at all: no host, no credential header name, no
        credential variable read.
        """
        banned = ("fusion.foxit.com", "foxitesign", "client_secret", "client_id",
                  "FOXIT_CLIENT", "FOXIT_CLOUD")
        for path in self._agent_sources():
            src = path.read_text()
            for needle in banned:
                self.assertNotIn(needle, src,
                                 f"{path.name} contains {needle!r} — the agent package "
                                 f"must hold no means of calling Foxit")

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

    def test_the_signed_document_is_rendered_by_the_parent_not_by_the_agent(self):
        """The agent's PDF bytes must never become the document that is signed.

        A compromised planner can return a clean term sheet and a PDF that disagrees
        with it. The human ratifies the term sheet; if the parent then served the
        agent's bytes, the signature would land on something nobody reviewed. So the
        draft route must render from the sheet and must not decode `pdf_b64`.
        """
        src = (REPO_ROOT / "app" / "main.py").read_text()
        draft_route = src.split("def draft(")[1].split("\n@app")[0]
        self.assertIn("_render_document", draft_route)
        self.assertNotIn("b64decode", draft_route)


class TestUnifiedFoxitApiShape(unittest.TestCase):
    """The 2026-08-30 rewrite, pinned.

    `hackathon-spec.md` §1.2 and run 4's log describe the old two-host, OAuth2 shape.
    They are wrong, and they are the documents a future reader is most likely to find
    first — so the corrected shape is asserted here rather than left in prose.
    """

    def test_one_host_for_both_products(self):
        self.assertEqual(DEFAULT_HOST, "https://na1.fusion.foxit.com")
        self.assertTrue(all(p.startswith(ESIGN_BASE) for p in ENDPOINTS.values()))
        self.assertEqual(ESIGN_BASE, "/esign/api/v1")

    def test_there_is_no_oauth_token_exchange(self):
        """Asserted against *code*, not prose.

        The docstrings in that module quote the old OAuth2 endpoint and the old host
        on purpose — a reader arriving from `hackathon-spec.md` §1.2 needs to see the
        thing that changed. So comments and string literals are stripped before the
        check, which is also the only version of this test that cannot be defeated by
        rewording a comment.
        """
        code = _code_only(REPO_ROOT / "app" / "esign_client.py")
        for stale in ("oauth2/access_token", "Bearer", "foxitesign.foxit.com",
                      "FOXIT_ESIGN_CLIENT_ID", "_access_token"):
            self.assertNotIn(stale, code,
                             f"{stale!r} is still live code in the eSign client")

    def test_headers_are_plain_credentials_with_an_honest_user_agent(self):
        client = ESignClient(client_id="cid", client_secret="sec", dry_run=True)
        headers = client._headers()
        self.assertEqual(headers["client_id"], "cid")
        self.assertEqual(headers["client_secret"], "sec")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers["User-Agent"], USER_AGENT)

    def test_the_user_agent_identifies_us_rather_than_impersonating_a_browser(self):
        """Cloudflare rejects the default urllib UA with a 1010 that looks like an auth
        failure. The fix is to say who we are — not to claim to be Chrome."""
        self.assertIn("Ratify", USER_AGENT)
        self.assertIn("github.com", USER_AGENT)
        for browser in ("Mozilla", "Chrome", "Safari", "AppleWebKit"):
            self.assertNotIn(browser, USER_AGENT)

    def test_credentials_are_stripped_of_whitespace(self):
        """A secret pasted into a dashboard field arrives with a trailing newline more
        often than anyone expects, and the resulting 401 is indistinguishable from a
        wrong key."""
        client = ESignClient(client_id=" cid\n", client_secret="sec ", dry_run=True)
        self.assertEqual(client.client_id, "cid")
        self.assertEqual(client.client_secret, "sec")

    def test_the_live_response_shape_is_flattened_for_callers(self):
        """The exact 200 body captured from the live service on 2026-08-30."""
        live = {"folder": {
            "folderId": 35637439,
            "folderStatus": "DRAFT",
            "folderCompanyId": 2922172,
            "folderDocumentIds": [41492365],
            "documentsList": [{"documentId": 41492365, "contractStatus": "OPEN"}],
            "folderRecipientParties": [],
            "draftFolderAccessURL": None,
        }}
        flat = normalise_created(live)
        self.assertEqual(flat["folderId"], "35637439")
        self.assertEqual(flat["status"], "DRAFT")
        self.assertIsInstance(flat["folderId"], str)

    def test_the_dry_run_and_live_shapes_agree(self):
        """The dry-run path is the one that gets exercised a hundred times; if the two
        shapes drift, the divergence surfaces live, in front of a judge."""
        client = ESignClient(dry_run=True)
        created = client.create_draft_folder(
            folder_name="f", signer_name="A B", signer_email="a@example.com",
            file_url="https://example.com/x.pdf")
        for key in ("folderId", "status"):
            self.assertIn(key, created)
        self.assertEqual(created["status"], "DRAFT")

    def test_the_verified_payload_fields_are_present(self):
        """`inputType: "url"` with `fileUrls` is what the live 200 was produced by."""
        client = ESignClient(dry_run=True)
        client.create_draft_folder(
            folder_name="f", signer_name="A B", signer_email="a@example.com",
            file_url="https://example.com/x.pdf")
        payload = list(client._fixtures.values())[0]["payload"]
        self.assertEqual(payload["inputType"], "url")
        self.assertEqual(payload["fileUrls"], ["https://example.com/x.pdf"])
        self.assertIs(payload["sendNow"], False)


if __name__ == "__main__":
    unittest.main()
