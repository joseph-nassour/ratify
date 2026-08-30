"""TermSheet — the core data structure of Ratify.

A term sheet is the set of material terms a drafted document will bind the human to.
Every term carries a *provenance*: did the human state it, did the agent derive it from
something the human stated, or did the agent invent it?

The whole safety argument of this project rests on that distinction, so it lives in the
data model rather than in a prompt or a UI convention.

This module is imported by BOTH the agent subprocess and the parent web process.
It must therefore stay dependency-free and must never import anything under `app/`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

STATED = "stated"      # the human said it, in these words
DERIVED = "derived"    # the agent computed it from something the human said
INVENTED = "invented"  # the agent supplied it; nobody asked for it

PROVENANCES = (STATED, DERIVED, INVENTED)

#: Provenances that do NOT require an explicit human resolution before a signature
#: request may exist. `stated` is the human's own words; `derived` is shown with its
#: derivation so it can be challenged, but it is arithmetic on the human's own words.
#: `invented` is deliberately absent from this set. That absence is the product.
AUTO_RESOLVED = frozenset({STATED, DERIVED})

# Resolution actions
ACCEPTED = "accepted"
EDITED = "edited"
REMOVED = "removed"


class TermError(Exception):
    """Raised when a term is mutated in a way the model forbids."""


# ---------------------------------------------------------------------------
# Term
# ---------------------------------------------------------------------------


@dataclass
class Term:
    """One material term of the engagement.

    Attributes:
        key:         stable identifier, e.g. "fee_amount"
        label:       human-facing name, e.g. "Fee"
        value:       the current value, as it will appear in the document
        provenance:  one of STATED / DERIVED / INVENTED
        evidence:    one line explaining *why* this value is here. For stated terms,
                     quote the human. For derived, show the computation. For invented,
                     say plainly that nobody asked for it.
        material:    does this term change what the human will be bound to?
                     Non-material terms (letter reference number, today's date) never gate.
        removable:   may the human delete this term outright? Core commercial terms
                     (parties, scope, fee) cannot be removed, only edited.
        resolved:    has the human dealt with this term? Set at construction for
                     STATED/DERIVED; False for INVENTED until a human acts.
        resolution:  how it was resolved: accepted / edited / removed
        proposed_value: what the agent originally proposed, retained after an edit so
                     the ratification screen can show a true diff.
    """

    key: str
    label: str
    value: str
    provenance: str
    evidence: str
    material: bool = True
    removable: bool = True
    resolved: bool = False
    resolution: Optional[str] = None
    proposed_value: Optional[str] = None

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCES:
            raise TermError(f"unknown provenance {self.provenance!r}")
        if self.proposed_value is None:
            self.proposed_value = self.value
        # Terms the human stated, and arithmetic on them, do not need ratification.
        # Anything the agent invented does, and no constructor argument can opt out of
        # that -- an invented term is created unresolved, always.
        if self.provenance == INVENTED:
            self.resolved = False
        elif not self.resolved:
            self.resolved = self.provenance in AUTO_RESOLVED

    # -- state transitions ---------------------------------------------------

    def accept(self) -> None:
        """Human accepts the agent's value as-is."""
        self.resolved = True
        self.resolution = ACCEPTED

    def edit(self, new_value: str) -> None:
        """Human supplies their own value.

        The term's provenance becomes STATED: after a human types a value, it is no
        longer something the agent made up. This is not cosmetic -- it is what makes
        the fingerprint change and the gate reopen (see approval.py).
        """
        if new_value is None or not str(new_value).strip():
            raise TermError(f"cannot set {self.key!r} to an empty value; remove it instead")
        self.value = str(new_value).strip()
        self.provenance = STATED
        self.evidence = "you supplied this value directly"
        self.resolved = True
        self.resolution = EDITED

    def remove(self) -> None:
        """Human deletes the term. Only permitted where `removable` is True."""
        if not self.removable:
            raise TermError(
                f"{self.label!r} is a core commercial term and cannot be removed; edit it instead"
            )
        self.value = ""
        self.resolved = True
        self.resolution = REMOVED

    # -- helpers -------------------------------------------------------------

    @property
    def removed(self) -> bool:
        return self.resolution == REMOVED

    @property
    def blocks_signature(self) -> bool:
        """Does this term, right now, prevent a signature request from existing?"""
        return self.material and not self.resolved

    def changed_by_human(self) -> bool:
        return self.resolution in (EDITED, REMOVED)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Term":
        return cls(**d)


# ---------------------------------------------------------------------------
# TermSheet
# ---------------------------------------------------------------------------


@dataclass
class TermSheet:
    """The full set of terms for one drafted document, plus the prompt it came from."""

    prompt: str
    terms: list = field(default_factory=list)
    document_type: str = "engagement_letter"

    # -- lookup --------------------------------------------------------------

    def __iter__(self):
        return iter(self.terms)

    def __len__(self) -> int:
        return len(self.terms)

    def get(self, key: str) -> Term:
        for t in self.terms:
            if t.key == key:
                return t
        raise KeyError(key)

    def add(self, term: Term) -> Term:
        if any(t.key == term.key for t in self.terms):
            raise TermError(f"duplicate term key {term.key!r}")
        self.terms.append(term)
        return term

    def by_provenance(self, provenance: str) -> list:
        return [t for t in self.terms if t.provenance == provenance]

    def unresolved(self) -> list:
        """Material terms still awaiting a human decision, in document order."""
        return [t for t in self.terms if t.blocks_signature]

    def display_order(self) -> list:
        """Terms ordered for the screen: whatever needs a decision, first.

        The document has its own order; the review screen should not. A human opening
        this page should be looking at the clause the agent made up, not scrolling past
        eight things they already said to find it.
        """
        # Stable sort: everything else keeps document order.
        return sorted(self.terms, key=lambda t: 0 if t.blocks_signature else 1)

    def live_terms(self) -> list:
        """Terms that will actually appear in the rendered document."""
        return [t for t in self.terms if not t.removed]

    # -- the gate ------------------------------------------------------------

    def can_request_signature(self) -> tuple:
        """The single gate. Returns (allowed, reason).

        Called in exactly one place (app/approval.py). If this function is wrong,
        every other claim this project makes is marketing.
        """
        blocking = self.unresolved()
        if not blocking:
            return True, "all material terms have been resolved by a human"
        if len(blocking) == 1:
            return False, (
                f"1 material term still needs your decision: {blocking[0].label}"
            )
        labels = ", ".join(t.label for t in blocking)
        return False, (
            f"{len(blocking)} material terms still need your decision: {labels}"
        )

    # -- identity ------------------------------------------------------------

    def fingerprint(self) -> str:
        """A content hash over everything that could change what the human is bound to.

        An approval is minted against this value. Change any material term after
        ratifying and the approval no longer matches, so it cannot be spent. This is
        what stops "human approved v1, agent sends v2" -- the classic failure of a
        confirm button that guards an action instead of a document.
        """
        canonical = [
            {
                "key": t.key,
                "value": t.value,
                "provenance": t.provenance,
                "material": t.material,
                "resolved": t.resolved,
                "removed": t.removed,
            }
            for t in sorted(self.terms, key=lambda x: x.key)
        ]
        payload = json.dumps(
            {"document_type": self.document_type, "terms": canonical},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # -- rendering / transport ----------------------------------------------

    def document_values(self) -> dict:
        """The `documentValues` payload for Foxit Document Generation."""
        return {t.key: t.value for t in self.live_terms()}

    def diff_against_prompt(self) -> list:
        """Every term that did not come verbatim from the human's prompt.

        Shown on the ratification screen so the one authorisation the human gives is
        made against a visible statement of what the agent added.
        """
        out = []
        for t in self.terms:
            if t.provenance == STATED and t.resolution is None:
                continue  # came straight from the prompt, unchanged
            out.append(
                {
                    "label": t.label,
                    "value": t.value,
                    "origin": t.provenance,
                    "proposed": t.proposed_value,
                    "resolution": t.resolution,
                    "evidence": t.evidence,
                }
            )
        return out

    def summary_counts(self) -> dict:
        return {
            "total": len(self.terms),
            "stated": len(self.by_provenance(STATED)),
            "derived": len(self.by_provenance(DERIVED)),
            "invented": len(self.by_provenance(INVENTED)),
            "unresolved": len(self.unresolved()),
        }

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "document_type": self.document_type,
            "terms": [t.to_dict() for t in self.terms],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TermSheet":
        sheet = cls(prompt=d.get("prompt", ""), document_type=d.get("document_type", "engagement_letter"))
        # bypass __post_init__ re-resolution by restoring state explicitly
        for td in d.get("terms", []):
            t = Term.from_dict(td)
            t.resolved = td.get("resolved", t.resolved)
            t.resolution = td.get("resolution")
            sheet.terms.append(t)
        return sheet

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_json(cls, s: str) -> "TermSheet":
        return cls.from_dict(json.loads(s))


def can_request_signature(sheet: TermSheet) -> tuple:
    """Module-level alias. One gate, one name, one call site."""
    return sheet.can_request_signature()
