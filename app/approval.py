"""Approval tokens — the only thing that lets an eSign call happen.

This module lives in the PARENT process only. The agent subprocess cannot import it,
cannot call it, and has no route to it. That is enforced structurally (see
app/supervisor.py) and asserted by tests/test_isolation.py.

Three properties, in order of importance:

1. **An approval is minted only when `can_request_signature` says so.** One gate,
   one call site, and that call site is here.

2. **An approval is bound to a document fingerprint, not to a session.** It is minted
   against the exact term sheet the human read. If any material term changes
   afterwards, the fingerprint changes, the approval no longer matches, and it cannot
   be spent. "Human approved v1, agent sent v2" is the failure mode a confirm button
   cannot see; binding the token to content is what closes it.

3. **An approval is single-use.** Spending it consumes it. A second envelope needs a
   second human decision.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from agent.terms import TermSheet, can_request_signature


class ApprovalError(Exception):
    """Raised when an approval cannot be minted or cannot be spent."""


@dataclass
class Approval:
    token: str
    session_id: str
    fingerprint: str
    granted_at: float
    granted_by: str
    spent_at: Optional[float] = None
    spent_on: Optional[str] = None  # e.g. the Foxit folderId it was spent on

    @property
    def spent(self) -> bool:
        return self.spent_at is not None

    def age_seconds(self) -> float:
        return time.time() - self.granted_at


class ApprovalStore:
    """In-memory approval registry.

    In-memory is a deliberate choice, not a shortcut: an approval that does not survive
    a process restart is strictly safer than one that does, and the deployment target
    (Render free tier) restarts on idle anyway. See hackathon-spec.md §8.
    """

    #: An approval older than this cannot be spent. A human who ratified a document an
    #: hour ago and walked away has not authorised what happens now.
    DEFAULT_TTL_SECONDS = 15 * 60

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._approvals: Dict[str, Approval] = {}
        self.ttl_seconds = ttl_seconds

    # -- minting -------------------------------------------------------------

    def mint(self, session_id: str, sheet: TermSheet, granted_by: str = "human") -> Approval:
        """Create an approval for this exact term sheet.

        Raises ApprovalError if the gate is closed. This is the ONLY place in the
        codebase that decides a signature request may exist.
        """
        allowed, reason = can_request_signature(sheet)
        if not allowed:
            raise ApprovalError(reason)

        approval = Approval(
            token=secrets.token_urlsafe(32),
            session_id=session_id,
            fingerprint=sheet.fingerprint(),
            granted_at=time.time(),
            granted_by=granted_by,
        )
        self._approvals[approval.token] = approval
        return approval

    # -- spending ------------------------------------------------------------

    def spend(self, token: str, session_id: str, sheet: TermSheet) -> Approval:
        """Consume an approval, or explain precisely why it cannot be consumed.

        Every failure mode here is a real one that a naive confirm button permits.
        """
        approval = self._approvals.get(token or "")
        if approval is None:
            raise ApprovalError("no such approval — a signature request needs a human ratification")
        if approval.spent:
            raise ApprovalError(
                "this approval has already been spent; ratify again to send another envelope"
            )
        if approval.session_id != session_id:
            raise ApprovalError("this approval belongs to a different document")
        if approval.age_seconds() > self.ttl_seconds:
            raise ApprovalError(
                f"this approval expired after {self.ttl_seconds // 60} minutes; please ratify again"
            )
        current = sheet.fingerprint()
        if current != approval.fingerprint:
            raise ApprovalError(
                "the terms changed after you ratified them — this approval covers a "
                "different version of the document. Review and ratify again."
            )
        # Re-check the gate at spend time as well as mint time. Cheap, and it means a
        # term reopened between the two cannot slip through on a stale token.
        allowed, reason = can_request_signature(sheet)
        if not allowed:
            raise ApprovalError(reason)

        approval.spent_at = time.time()
        return approval

    def mark_spent_on(self, token: str, external_id: str) -> None:
        approval = self._approvals.get(token)
        if approval is not None:
            approval.spent_on = external_id

    def get(self, token: str) -> Optional[Approval]:
        return self._approvals.get(token)

    def for_session(self, session_id: str) -> list:
        return [a for a in self._approvals.values() if a.session_id == session_id]


# ---------------------------------------------------------------------------
# The gate, restated at the boundary it protects
# ---------------------------------------------------------------------------


def assert_may_send(store: ApprovalStore, token: str, session_id: str, sheet: TermSheet) -> Approval:
    """Call this immediately before any eSign write. Nothing else may call eSign.

    Written as a separate function so that the audit trail in DESIGN.md can point at
    one line: every path that reaches Foxit eSign passes through here.
    """
    return store.spend(token=token, session_id=session_id, sheet=sheet)
