"""Foxit eSign client — PARENT PROCESS ONLY.

★ This module is the reason the whole architecture exists. It is the only code in the
project that can cause a legally operative event, and it is deliberately unreachable
from the agent:

  * it lives under `app/`, which the agent subprocess never imports;
  * it reads `FOXIT_ESIGN_CLIENT_ID` / `FOXIT_ESIGN_CLIENT_SECRET`, which
    app/supervisor.py strips from the subprocess environment;
  * every call site is guarded by `app.approval.assert_may_send`.

tests/test_isolation.py asserts the first two mechanically. If you are editing this
file, you are editing the trust boundary.

Lifecycle verified in hackathon-spec.md §1.2:
    POST /api/oauth2/access_token          -> bearer token
    POST /api/folders/createfolder         (sendNow: false)  -> DRAFT envelope
    POST /api/folders/sendDraftFolder      -> releases it to the signer  ← the gate
    GET  /api/folders/download?folderId=   -> executed PDF
    GET  /api/folders/viewActivityHistory?folderId=  -> the audit trail

Foxit's own API models the two-phase commit we need. We are not bolting a modal onto
their product; we are using the seam they built.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Optional


class ESignError(Exception):
    pass


class CreditBudgetExceeded(ESignError):
    pass


#: Free Developer plan: 500 shared credits per YEAR, 5 credits per eSign envelope.
#: There is no way to buy more and no way to reset. 25 live envelopes = 125 credits,
#: which leaves headroom for the judging window when a judge may run the flow.
MAX_LIVE_ENVELOPES = 25


class ESignClient:
    def __init__(
        self,
        host: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        dry_run: Optional[bool] = None,
        timeout: float = 45.0,
    ) -> None:
        self.host = (host or os.environ.get("FOXIT_ESIGN_HOST", "https://na1.foxitesign.foxit.com")).rstrip("/")
        self.client_id = client_id or os.environ.get("FOXIT_ESIGN_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("FOXIT_ESIGN_CLIENT_SECRET", "")
        self.timeout = timeout
        if dry_run is None:
            dry_run = os.environ.get("DRY_RUN", "true").lower() != "false"
        self.dry_run = bool(dry_run or not (self.client_id and self.client_secret))
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self.live_envelopes_created = 0
        self._fixtures: dict = {}

    def mode(self) -> str:
        return "dry-run (no envelope leaves this process)" if self.dry_run else f"live ({self.host})"

    # -- auth ----------------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        import httpx

        url = f"{self.host}/api/oauth2/access_token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
            "scope": "read-write",
        }
        try:
            resp = httpx.post(url, data=data, timeout=self.timeout)
        except Exception as exc:
            raise ESignError(f"eSign token request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ESignError(f"eSign token returned {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        token = body.get("access_token") or body.get("accessToken")
        if not token:
            raise ESignError(f"eSign token response had no access_token: {list(body)[:8]}")
        self._token = token
        self._token_expires_at = time.time() + float(body.get("expires_in", 3600) or 3600)
        return token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}

    # -- envelope lifecycle ---------------------------------------------------

    def create_draft_folder(
        self,
        folder_name: str,
        signer_name: str,
        signer_email: str,
        file_url: str,
        file_name: str = "engagement-letter.pdf",
        embedded: bool = True,
        success_url: str = "",
    ) -> dict:
        """Create the envelope in DRAFT. `sendNow` is False, always.

        There is no parameter on this method to make it True. Releasing a draft is a
        separate method, called from a separate route, guarded by a separate approval —
        which is the entire point.
        """
        first, _, last = (signer_name or "").partition(" ")
        parties = [{
            "firstName": first or signer_name or "Signer",
            "lastName": last or "",
            "emailId": signer_email,
            "permission": "FILL_FIELDS_AND_SIGN",
            "sequence": 1,
        }]
        payload = {
            "folderName": folder_name,
            "parties": parties,
            "fileUrls": [file_url],
            "fileNames": [file_name],
            "processTextTags": True,
            "sendNow": False,  # ← never True. See docstring.
        }
        if embedded:
            payload["createEmbeddedSigningSession"] = True
            payload["embeddedSignersEmailIds"] = [signer_email]
        if success_url:
            payload["signSuccessUrl"] = success_url

        if self.dry_run:
            return self._fixture_create(payload)

        if self.live_envelopes_created >= MAX_LIVE_ENVELOPES:
            raise CreditBudgetExceeded(
                f"refusing to create envelope #{self.live_envelopes_created + 1}: the "
                f"free plan allows 500 credits/year and this project's ceiling is "
                f"{MAX_LIVE_ENVELOPES} live envelopes (5 credits each)."
            )
        import httpx

        try:
            resp = httpx.post(f"{self.host}/api/folders/createfolder",
                              headers=self._headers(), json=payload, timeout=self.timeout)
        except Exception as exc:
            raise ESignError(f"createfolder failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ESignError(f"createfolder returned {resp.status_code}: {resp.text[:400]}")
        self.live_envelopes_created += 1
        return resp.json()

    def send_draft_folder(self, folder_id: str) -> dict:
        """Release a DRAFT envelope to the signer. **This is the authorised action.**

        Callers must have spent a valid approval (app.approval.assert_may_send) before
        reaching this method. There is exactly one such call site.
        """
        if self.dry_run:
            f = self._fixtures.setdefault(folder_id, {})
            f["status"] = "SENT"
            f.setdefault("history", []).append(
                {"at": _now(), "event": "FOLDER_SENT", "actor": "human (ratified)"}
            )
            return {"folderId": folder_id, "status": "SENT", "message": "Draft released to signer"}
        import httpx

        try:
            resp = httpx.post(f"{self.host}/api/folders/sendDraftFolder",
                              headers=self._headers(), json={"folderId": folder_id},
                              timeout=self.timeout)
        except Exception as exc:
            raise ESignError(f"sendDraftFolder failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ESignError(f"sendDraftFolder returned {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    def folder_status(self, folder_id: str) -> str:
        if self.dry_run:
            return self._fixtures.get(folder_id, {}).get("status", "DRAFT")
        import httpx

        resp = httpx.get(f"{self.host}/api/folders/getfolderdetails",
                         headers=self._headers(), params={"folderId": folder_id},
                         timeout=self.timeout)
        if resp.status_code >= 400:
            raise ESignError(f"folder status returned {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        return body.get("status") or body.get("folderStatus") or "UNKNOWN"

    def download(self, folder_id: str) -> bytes:
        if self.dry_run:
            return self._fixtures.get(folder_id, {}).get("pdf", b"%PDF-1.4\n% dry-run\n")
        import httpx

        resp = httpx.get(f"{self.host}/api/folders/download",
                         headers={"Authorization": f"Bearer {self._access_token()}"},
                         params={"folderId": folder_id}, timeout=self.timeout)
        if resp.status_code >= 400:
            raise ESignError(f"download returned {resp.status_code}")
        return resp.content

    def activity_history(self, folder_id: str) -> list:
        """The audit trail. In the demo this is the evidence that the boundary held."""
        if self.dry_run:
            return self._fixtures.get(folder_id, {}).get("history", [])
        import httpx

        resp = httpx.get(f"{self.host}/api/folders/viewActivityHistory",
                         headers=self._headers(), params={"folderId": folder_id},
                         timeout=self.timeout)
        if resp.status_code >= 400:
            raise ESignError(f"viewActivityHistory returned {resp.status_code}")
        body = resp.json()
        return body if isinstance(body, list) else body.get("activities", [])

    # -- dry-run fixtures -----------------------------------------------------

    def _fixture_create(self, payload: dict) -> dict:
        folder_id = f"dry-{uuid.uuid4().hex[:12]}"
        self._fixtures[folder_id] = {
            "status": "DRAFT",
            "payload": payload,
            "history": [{"at": _now(), "event": "FOLDER_CREATED_AS_DRAFT",
                         "actor": "agent (no signature authority)"}],
        }
        out = {
            "folderId": folder_id,
            "status": "DRAFT",
            "message": "Draft envelope created. sendNow was false.",
        }
        if payload.get("createEmbeddedSigningSession"):
            out["embeddedSigningSessions"] = [{
                "emailId": payload["embeddedSignersEmailIds"][0],
                "embeddedSessionURL": f"/dry-run/sign/{folder_id}",
            }]
        return out

    def dry_run_complete(self, folder_id: str, pdf: bytes = b"") -> None:
        """Simulate the signer finishing. Dry run only — used by the demo and tests."""
        if not self.dry_run:
            raise ESignError("dry_run_complete is not available against the live API")
        f = self._fixtures.setdefault(folder_id, {})
        f["status"] = "COMPLETED"
        if pdf:
            f["pdf"] = pdf
        f.setdefault("history", []).append(
            {"at": _now(), "event": "SIGNED_BY_PARTY", "actor": "signer"}
        )
        f["history"].append({"at": _now(), "event": "FOLDER_COMPLETED", "actor": "system"})


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " UTC"
