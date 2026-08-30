"""Tests for approval tokens — minting, binding, expiry, single use.

The failure these exist to prevent: a human approves version 1 of a document and the
system sends version 2. A confirm button cannot see that happen. Binding the approval
to a content fingerprint can.
"""

import time
import unittest

from agent.terms import INVENTED, STATED, Term, TermSheet
from app.approval import Approval, ApprovalError, ApprovalStore, assert_may_send


def make_sheet(resolved=True) -> TermSheet:
    s = TermSheet(prompt="test")
    s.add(Term(key="fee", label="Fee", value="AED 12,000", provenance=STATED,
               evidence="you wrote it"))
    t = Term(key="cap", label="Liability cap", value="fees paid", provenance=INVENTED,
             evidence="nobody asked")
    s.add(t)
    if resolved:
        t.accept()
    return s


class TestMinting(unittest.TestCase):
    def test_mint_refused_while_the_gate_is_shut(self):
        store = ApprovalStore()
        with self.assertRaises(ApprovalError) as ctx:
            store.mint("s1", make_sheet(resolved=False))
        self.assertIn("Liability cap", str(ctx.exception))

    def test_mint_succeeds_when_resolved(self):
        store = ApprovalStore()
        approval = store.mint("s1", make_sheet())
        self.assertTrue(approval.token)
        self.assertFalse(approval.spent)

    def test_tokens_are_unguessable_and_unique(self):
        store = ApprovalStore()
        tokens = {store.mint("s1", make_sheet()).token for _ in range(25)}
        self.assertEqual(len(tokens), 25)
        self.assertTrue(all(len(t) >= 32 for t in tokens))


class TestSpending(unittest.TestCase):
    def setUp(self):
        self.store = ApprovalStore()
        self.sheet = make_sheet()
        self.approval = self.store.mint("s1", self.sheet)

    def test_happy_path(self):
        spent = assert_may_send(self.store, self.approval.token, "s1", self.sheet)
        self.assertTrue(spent.spent)

    def test_no_token_no_send(self):
        with self.assertRaises(ApprovalError):
            assert_may_send(self.store, "", "s1", self.sheet)

    def test_invented_token_rejected(self):
        with self.assertRaises(ApprovalError):
            assert_may_send(self.store, "not-a-real-token", "s1", self.sheet)

    def test_single_use(self):
        assert_may_send(self.store, self.approval.token, "s1", self.sheet)
        with self.assertRaises(ApprovalError) as ctx:
            assert_may_send(self.store, self.approval.token, "s1", self.sheet)
        self.assertIn("already been spent", str(ctx.exception))

    def test_bound_to_its_session(self):
        with self.assertRaises(ApprovalError):
            assert_may_send(self.store, self.approval.token, "another-session", self.sheet)

    def test_terms_changed_after_ratification_invalidates_the_approval(self):
        """The headline case. Human ratifies; something edits a term; the token dies."""
        self.sheet.get("fee").edit("AED 120,000")
        with self.assertRaises(ApprovalError) as ctx:
            assert_may_send(self.store, self.approval.token, "s1", self.sheet)
        self.assertIn("terms changed", str(ctx.exception))

    def test_a_reopened_term_shuts_the_gate_at_spend_time(self):
        new_term = Term(key="renewal", label="Auto-renewal", value="12 months",
                        provenance=INVENTED, evidence="nobody asked")
        self.sheet.add(new_term)
        with self.assertRaises(ApprovalError):
            assert_may_send(self.store, self.approval.token, "s1", self.sheet)

    def test_expiry(self):
        store = ApprovalStore(ttl_seconds=0)
        sheet = make_sheet()
        approval = store.mint("s1", sheet)
        time.sleep(0.01)
        with self.assertRaises(ApprovalError) as ctx:
            assert_may_send(store, approval.token, "s1", sheet)
        self.assertIn("expired", str(ctx.exception))

    def test_re_ratifying_after_a_change_works(self):
        self.sheet.get("fee").edit("AED 120,000")
        fresh = self.store.mint("s1", self.sheet)
        spent = assert_may_send(self.store, fresh.token, "s1", self.sheet)
        self.assertTrue(spent.spent)


if __name__ == "__main__":
    unittest.main()
