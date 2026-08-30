"""Tests for the signature gate.

`can_request_signature` is the entry's central claim. If it is wrong, everything built
on top of it is marketing. These tests are written first and deliberately include the
cases a naive "confirm button" implementation would pass and this design must not.
"""

import unittest

from agent.terms import (DERIVED, INVENTED, STATED, Term, TermSheet, TermError,
                         can_request_signature)


def sheet_with(*terms) -> TermSheet:
    s = TermSheet(prompt="test")
    for t in terms:
        s.add(t)
    return s


def stated(key="fee", value="AED 12,000"):
    return Term(key=key, label=key.title(), value=value, provenance=STATED,
                evidence="you wrote it")


def derived(key="start", value="1 October 2026"):
    return Term(key=key, label=key.title(), value=value, provenance=DERIVED,
                evidence="computed from the quarter end")


def invented(key="liability_cap", value="capped at fees paid", material=True):
    return Term(key=key, label=key.title(), value=value, provenance=INVENTED,
                evidence="nobody asked for this", material=material)


class TestProvenanceDefaults(unittest.TestCase):
    def test_stated_terms_are_resolved_on_arrival(self):
        self.assertTrue(stated().resolved)

    def test_derived_terms_are_resolved_on_arrival(self):
        self.assertTrue(derived().resolved)

    def test_invented_terms_are_not(self):
        self.assertFalse(invented().resolved)

    def test_invented_cannot_be_born_resolved(self):
        """No constructor argument may opt an invented term out of the gate."""
        t = Term(key="x", label="X", value="v", provenance=INVENTED,
                 evidence="e", resolved=True)
        self.assertFalse(t.resolved)

    def test_unknown_provenance_rejected(self):
        with self.assertRaises(TermError):
            Term(key="x", label="X", value="v", provenance="assumed", evidence="e")


class TestGate(unittest.TestCase):
    def test_open_when_everything_is_stated(self):
        allowed, reason = can_request_signature(sheet_with(stated(), derived()))
        self.assertTrue(allowed, reason)

    def test_shut_by_a_single_invented_term(self):
        allowed, reason = can_request_signature(sheet_with(stated(), invented()))
        self.assertFalse(allowed)
        self.assertIn("Liability_Cap", reason)

    def test_reason_names_every_blocking_term(self):
        s = sheet_with(stated(), invented("a"), invented("b"), invented("c"))
        allowed, reason = can_request_signature(s)
        self.assertFalse(allowed)
        self.assertIn("3 material terms", reason)
        for key in ("A", "B", "C"):
            self.assertIn(key, reason)

    def test_accepting_opens_the_gate(self):
        s = sheet_with(stated(), invented())
        s.get("liability_cap").accept()
        self.assertTrue(can_request_signature(s)[0])

    def test_editing_opens_the_gate_and_relabels_provenance(self):
        s = sheet_with(invented())
        s.get("liability_cap").edit("capped at AED 50,000")
        self.assertTrue(can_request_signature(s)[0])
        # a value the human typed is theirs, not the agent's
        self.assertEqual(s.get("liability_cap").provenance, STATED)
        self.assertEqual(s.get("liability_cap").proposed_value, "capped at fees paid")

    def test_removing_opens_the_gate(self):
        s = sheet_with(invented())
        s.get("liability_cap").remove()
        self.assertTrue(can_request_signature(s)[0])
        self.assertTrue(s.get("liability_cap").removed)

    def test_removed_terms_leave_the_document(self):
        s = sheet_with(stated(), invented())
        s.get("liability_cap").remove()
        self.assertNotIn("liability_cap", s.document_values())

    def test_core_terms_cannot_be_removed(self):
        t = Term(key="fee_amount", label="Fee", value="AED 1", provenance=INVENTED,
                 evidence="e", removable=False)
        with self.assertRaises(TermError):
            t.remove()
        self.assertFalse(t.resolved, "a failed removal must not resolve the term")

    def test_editing_to_empty_is_refused(self):
        t = invented()
        with self.assertRaises(TermError):
            t.edit("   ")
        self.assertFalse(t.resolved)

    def test_non_material_invented_terms_do_not_gate(self):
        """A boundary that stops everywhere is as useless as one that stops nowhere."""
        s = sheet_with(stated(), invented("letter_reference", "REF-001", material=False))
        self.assertTrue(can_request_signature(s)[0])

    def test_gate_is_false_while_any_one_term_remains(self):
        s = sheet_with(invented("a"), invented("b"))
        s.get("a").accept()
        self.assertFalse(can_request_signature(s)[0])
        s.get("b").accept()
        self.assertTrue(can_request_signature(s)[0])


class TestDisplayOrder(unittest.TestCase):
    def test_things_needing_a_decision_come_first(self):
        s = sheet_with(stated("a"), derived("b"), invented("c"), stated("d"))
        self.assertEqual(s.display_order()[0].key, "c")

    def test_resolved_terms_fall_back_in_original_order(self):
        s = sheet_with(stated("a"), invented("c"), stated("d"))
        s.get("c").accept()
        self.assertEqual([t.key for t in s.display_order()], ["a", "c", "d"])


class TestFingerprint(unittest.TestCase):
    def test_stable_across_key_order(self):
        a = sheet_with(stated("x", "1"), stated("y", "2"))
        b = sheet_with(stated("y", "2"), stated("x", "1"))
        self.assertEqual(a.fingerprint(), b.fingerprint())

    def test_changes_when_a_value_changes(self):
        s = sheet_with(stated("fee", "AED 12,000"), invented())
        s.get("liability_cap").accept()
        before = s.fingerprint()
        s.get("fee").edit("AED 20,000")
        self.assertNotEqual(before, s.fingerprint())

    def test_changes_when_a_term_is_removed(self):
        s = sheet_with(stated(), invented())
        s.get("liability_cap").accept()
        before = s.fingerprint()
        s.get("liability_cap").remove()
        self.assertNotEqual(before, s.fingerprint())


class TestRoundTrip(unittest.TestCase):
    def test_json_round_trip_preserves_resolution_state(self):
        s = sheet_with(stated(), invented("a"), invented("b"))
        s.get("a").accept()
        restored = TermSheet.from_json(s.to_json())
        self.assertTrue(restored.get("a").resolved)
        self.assertFalse(restored.get("b").resolved)
        self.assertEqual(s.fingerprint(), restored.fingerprint())

    def test_round_trip_does_not_silently_resolve_invented_terms(self):
        """The subprocess boundary is JSON. An invented term must not become resolved
        merely by crossing it."""
        s = sheet_with(invented())
        restored = TermSheet.from_json(s.to_json())
        self.assertFalse(can_request_signature(restored)[0])


if __name__ == "__main__":
    unittest.main()
