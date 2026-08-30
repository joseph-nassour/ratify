"""End-to-end test of the whole flow, fixture-driven, DRY_RUN=true.

No credentials, no network, no Foxit credits. This is the test that proves the entry
works before B6 lands, and it is also the storyboard for the demo video: every step
below is a beat in the recording.
"""

import os
import unittest

os.environ.setdefault("DRY_RUN", "true")
os.environ.pop("FOXIT_ESIGN_CLIENT_ID", None)
os.environ.pop("FOXIT_ESIGN_CLIENT_SECRET", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import DOCS, SESSIONS, app  # noqa: E402

PROMPT = ("Draft an engagement letter for Meridian Consulting FZ-LLC - bookkeeping and "
          "VAT filing for the quarter ending 31 December, AED 12,000, paid monthly. "
          "Signer is Layla Haddad, layla@meridian.example")


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, follow_redirects=False)
        SESSIONS.clear()
        DOCS.clear()

    # -- helpers -------------------------------------------------------------

    def draft(self, poisoned: bool = False) -> str:
        data = {"prompt": PROMPT}
        if poisoned:
            data["poisoned"] = "1"
        resp = self.client.post("/draft", data=data)
        self.assertEqual(resp.status_code, 303, resp.text[:500])
        return resp.headers["location"].rsplit("/", 1)[1]

    def page(self, session_id: str) -> str:
        resp = self.client.get(f"/s/{session_id}")
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def resolve_all(self, session_id: str):
        sheet = SESSIONS[session_id].sheet
        for term in list(sheet.unresolved()):
            self.client.post(f"/s/{session_id}/term/{term.key}", data={"action": "accept"})

    # -- the flow ------------------------------------------------------------

    def test_full_journey(self):
        sid = self.draft()
        session = SESSIONS[sid]

        # 1. the agent drafted, and the gate is shut
        self.assertGreaterEqual(len(session.sheet), 10)
        self.assertFalse(session.sheet.can_request_signature()[0])
        html = self.page(sid)
        self.assertIn("invented", html)
        self.assertIn("disabled", html, "the ratify button must be disabled while terms are open")

        # 2. ratifying is refused while any invented term is open
        resp = self.client.post(f"/s/{sid}/ratify")
        self.assertEqual(resp.status_code, 303)
        self.assertIn("error=", resp.headers["location"])
        self.assertIsNone(session.approval_token)

        # 3. and so is sending
        resp = self.client.post(f"/s/{sid}/send")
        self.assertIn("error=", resp.headers["location"])
        self.assertIsNone(session.folder_id, "no envelope may exist without a ratification")

        # 4. the human resolves the invented terms, three different ways
        self.client.post(f"/s/{sid}/term/auto_renewal", data={"action": "remove"})
        self.client.post(f"/s/{sid}/term/liability_cap",
                         data={"action": "edit", "value": "Capped at AED 50,000"})
        self.resolve_all(sid)
        self.assertTrue(session.sheet.can_request_signature()[0])
        self.assertEqual(session.sheet.get("liability_cap").provenance, "stated")
        self.assertTrue(session.sheet.get("auto_renewal").removed)

        # 5. ratify
        self.client.post(f"/s/{sid}/ratify")
        self.assertIsNotNone(session.approval_token)

        # 6. changing a term after ratifying revokes the approval
        self.client.post(f"/s/{sid}/term/fee_amount",
                         data={"action": "edit", "value": "AED 20,000"})
        self.assertIsNone(session.approval_token, "the ratification must not survive an edit")
        resp = self.client.post(f"/s/{sid}/send")
        self.assertIn("error=", resp.headers["location"])
        self.assertIsNone(session.folder_id)

        # 7. re-ratify and send
        self.client.post(f"/s/{sid}/ratify")
        resp = self.client.post(f"/s/{sid}/send")
        self.assertEqual(resp.status_code, 303)
        self.assertNotIn("error=", resp.headers["location"])
        self.assertIsNotNone(session.folder_id)
        self.assertEqual(session.envelope_status, "SENT")

        # 8. the envelope was created as a DRAFT first, then released. Two events.
        status = self.client.get(f"/s/{sid}/status").json()
        events = [h["event"] for h in status["history"]]
        self.assertEqual(events[:2], ["FOLDER_CREATED_AS_DRAFT", "FOLDER_SENT"])
        self.assertEqual(status["history"][0]["actor"], "agent (no signature authority)")
        self.assertEqual(status["history"][1]["actor"], "human (ratified)")

        # 9. the approval was single-use
        self.assertIsNone(session.approval_token)

        # 10. signer completes; the executed document comes back
        self.client.post(f"/s/{sid}/simulate-signature")
        status = self.client.get(f"/s/{sid}/status").json()
        self.assertEqual(status["status"], "COMPLETED")
        self.assertIn("FOLDER_COMPLETED", [h["event"] for h in status["history"]])

    # -- artefacts -----------------------------------------------------------

    def test_a_ratified_term_can_still_be_changed_and_the_ui_says_so(self):
        """The strongest property in this build is that an approval is bound to a
        fingerprint, not to a session -- so a term changed after ratification revokes
        it. That is only demonstrable if the UI offers a way to change a term at that
        point. Run 6 lost the demo recorder to a silent template change; this test
        exists so the affordance cannot disappear the same way."""
        sid = self.draft()
        self.resolve_all(sid)
        self.client.post(f"/s/{sid}/ratify")
        page = self.page(sid)
        self.assertIn("actions revise", page,
                      "no way to change a term after ratifying: the revocation "
                      "property is no longer visible in the UI")
        self.assertIn("Ratified.", page)

        # And using it really does revoke, rather than merely looking like it might.
        self.client.post(f"/s/{sid}/term/fee_amount",
                         data={"action": "edit", "value": "AED 99,000"})
        resp = self.client.post(f"/s/{sid}/send")
        self.assertEqual(resp.status_code, 303)
        self.assertIsNone(SESSIONS[sid].folder_id,
                          "envelope was created against a revoked approval")

    def test_the_provenance_attack_demo_runs_a_labelled_compromised_model(self):
        """The provenance attack is only meaningful against a model making claims, and
        there is no model key here, so the demo scripts one. Two things must hold, and
        both are easy to lose in a refactor: the simulation must be *labelled* as a
        simulation, and it must actually be adjudicated rather than believed."""
        resp = self.client.post("/draft", data={"prompt": PROMPT, "poisoned": "provenance"})
        sid = resp.headers["location"].rsplit("/", 1)[1]
        s = SESSIONS[sid]

        self.assertIn("simulated-compromised-model", s.planner,
                      "a scripted model must never be displayed as a real one")

        rejected = [e for e in s.security_events if "provenance claim rejected" in e]
        self.assertGreaterEqual(len(rejected), 3,
                                "the compromised model's claims were not adjudicated")

        allowed, _ = s.sheet.can_request_signature()
        self.assertFalse(allowed, "a compromised model opened the gate")

        # The nastiest single claim: redirect the signer and call it the human's word.
        signer = s.sheet.get("signer_email")
        self.assertFalse(signer.resolved,
                         "a redirected signer address was auto-resolved")

    def test_the_draft_pdf_is_served_and_is_a_real_pdf(self):
        sid = self.draft()
        doc_id = SESSIONS[sid].doc_id
        resp = self.client.get(f"/doc/{doc_id}.pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/pdf")
        self.assertTrue(resp.content.startswith(b"%PDF"))

    def test_the_pdf_is_rerendered_when_a_term_changes(self):
        sid = self.draft()
        first = SESSIONS[sid].doc_id
        self.client.post(f"/s/{sid}/term/auto_renewal", data={"action": "remove"})
        self.assertNotEqual(SESSIONS[sid].doc_id, first)
        self.assertNotIn(b"Renews automatically", SESSIONS[sid].pdf)

    def test_agent_env_endpoint_shows_no_signing_credentials(self):
        report = self.client.get("/agent-env").json()
        self.assertEqual(report["signing_variables_in_agent"], [])
        self.assertIn("DRY_RUN", report["agent_process_sees"])

    def test_healthz(self):
        self.assertTrue(self.client.get("/healthz").json()["ok"])

    # -- the adversarial coda ------------------------------------------------

    def test_a_poisoned_document_cannot_open_the_gate(self):
        sid = self.draft(poisoned=True)
        session = SESSIONS[sid]
        self.assertTrue(session.security_events, "the injection attempt must be surfaced")
        self.assertFalse(session.sheet.can_request_signature()[0])
        self.assertIsNone(session.folder_id)
        html = self.page(sid)
        self.assertIn("not acted on", html)


if __name__ == "__main__":
    unittest.main()
