"""Foxit eSign client — PARENT PROCESS ONLY.

★ This module is the reason the whole architecture exists. It is the only code in the
project that can cause a legally operative event, and it is deliberately unreachable
from the agent:

  * it lives under `app/`, which the agent subprocess never imports;
  * it reads `FOXIT_CLIENT_ID` / `FOXIT_CLIENT_SECRET`, which app/supervisor.py
    refuses to place in the subprocess environment;
  * every call site is guarded by `app.approval.assert_may_send`.

tests/test_isolation.py asserts all three mechanically. If you are editing this file,
you are editing the trust boundary.

────────────────────────────────────────────────────────────────────────────────────
CORRECTION, 2026-08-30 — Foxit unified their APIs, and it matters more than an endpoint
────────────────────────────────────────────────────────────────────────────────────
`hackathon-spec.md` §1.2 and the vendor's older integration guide both describe eSign
as a separate host (`na1.foxitesign.foxit.com`) behind OAuth2 client-credentials, with
a second credential pair that is "not interchangeable" with the Document Generation
one. **That is no longer true.** Verified against the live service on 2026-08-30:

    ONE credential pair. ONE host. Plain `client_id` / `client_secret` headers.
    Document Generation : {host}/document-generation/api/...
    eSign               : {host}/esign/api/v1/...

The security consequence is the important half, and it is why this project's boundary
is drawn where it is: **under a unified credential there is no such thing as a
document-only Foxit key.** Any credential that can render a PDF can also create and
release a signature envelope. An architecture that hands its agent "just the document
tools" is, on this vendor, handing it signing authority. See DESIGN.md §"One key".

Lifecycle:

    POST {host}/esign/api/v1/folders/createfolder   (sendNow: false)  -> DRAFT
    POST {host}/esign/api/v1/folders/sendDraftFolder -> releases it to the signer ← gate
    GET  {host}/esign/api/v1/folders/download?folderId=
    GET  {host}/esign/api/v1/folders/viewActivityHistory?folderId=

Foxit's own API models the two-phase commit we need. We are not bolting a modal onto
their product; we are using the seam they built.
"""

from __future__ import annotations

import os
import uuid
from typing import Optional

#: Identify ourselves honestly. Foxit sits behind Cloudflare, which rejects Python's
#: default `User-Agent: python-urllib/3.11` at the edge and returns HTTP 403 with body
#: `{"error code": 1010}` — a Cloudflare browser-signature ban, NOT a Foxit auth
#: failure. Four runs of this project were spent misdiagnosing it as bad credentials.
#: The tell is the Foxit console's Usage tab showing 0 requests while CI has fired six:
#: the calls never arrived. Foxit sells API access and publishes curl examples, so a
#: blanket bot rule catching an anonymous default is an accident rather than a policy —
#: and we conform to it by saying who we are, never by impersonating a browser.
USER_AGENT = "Ratify/1.0 (+https://github.com/joseph-nassour/ratify)"

DEFAULT_HOST = "https://na1.fusion.foxit.com"
ESIGN_BASE = "/esign/api/v1"

#: Paths, with their evidence. `createfolder` was exercised live on 2026-08-30 and
#: returned HTTP 200 with a real DRAFT envelope; the rest are read off the same API
#: family and have not yet met the service. Keeping the distinction in the source
#: rather than in a log means the next person to get a 404 knows immediately whether
#: they are looking at a bug or at an unverified guess.
ENDPOINTS = {
    "createfolder": f"{ESIGN_BASE}/folders/createfolder",              # VERIFIED live
    "sendDraftFolder": f"{ESIGN_BASE}/folders/sendDraftFolder",        # unverified
    "getFolderDetails": f"{ESIGN_BASE}/folders/getfolderdetails",      # unverified
    "download": f"{ESIGN_BASE}/folders/download",                      # unverified
    "viewActivityHistory": f"{ESIGN_BASE}/folders/viewActivityHistory",  # unverified
}


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
        self.host = (host or os.environ.get("FOXIT_API_HOST", DEFAULT_HOST)).strip().rstrip("/")
        self.client_id = (client_id or os.environ.get("FOXIT_CLIENT_ID", "")).strip()
        self.client_secret = (client_secret or os.environ.get("FOXIT_CLIENT_SECRET", "")).strip()
        self.timeout = timeout
        if dry_run is None:
            dry_run = os.environ.get("DRY_RUN", "true").lower() != "false"
        self.dry_run = bool(dry_run or not (self.client_id and self.client_secret))
        self.live_envelopes_created = 0
        self._fixtures: dict = {}

    def mode(self) -> str:
        return "dry-run (no envelope leaves this process)" if self.dry_run else f"live ({self.host})"

    # -- auth ----------------------------------------------------------------

    def _headers(self) -> dict:
        """Plain headers. There is no token exchange on the unified API.

        Note what is *absent*: no OAuth round trip, no bearer token, no refresh. The
        previous version of this file maintained a token cache against
        `POST /api/oauth2/access_token` on a host that no longer serves this account.
        """
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _url(self, name: str) -> str:
        return f"{self.host}{ENDPOINTS[name]}"

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
            # VERIFIED 2026-08-30: `inputType: "url"` with `fileUrls` is accepted and
            # returns a DRAFT envelope. The base64-body question raised in spec §1.2
            # is settled — no base64 upload is required.
            "inputType": "url",
            "fileUrls": [file_url],
            "fileNames": [file_name],
            "parties": parties,
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
        body = self._post("createfolder", payload)
        self.live_envelopes_created += 1
        return normalise_created(body)

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
        body = self._post("sendDraftFolder", {"folderId": folder_id})
        return normalise_created(body)

    def folder_status(self, folder_id: str) -> str:
        if self.dry_run:
            return self._fixtures.get(folder_id, {}).get("status", "DRAFT")
        body = self._get("getFolderDetails", {"folderId": folder_id})
        flat = normalise_created(body)
        return flat.get("status") or "UNKNOWN"

    def download(self, folder_id: str) -> bytes:
        if self.dry_run:
            return self._fixtures.get(folder_id, {}).get("pdf", b"%PDF-1.4\n% dry-run\n")
        import httpx

        resp = httpx.get(self._url("download"), headers=self._headers(),
                         params={"folderId": folder_id}, timeout=self.timeout)
        if resp.status_code >= 400:
            raise ESignError(f"download returned {resp.status_code}: {_explain(resp)}")
        return resp.content

    def activity_history(self, folder_id: str) -> list:
        """The audit trail. In the demo this is the evidence that the boundary held."""
        if self.dry_run:
            return self._fixtures.get(folder_id, {}).get("history", [])
        body = self._get("viewActivityHistory", {"folderId": folder_id})
        if isinstance(body, list):
            return body
        for key in ("activities", "activityHistory", "history"):
            if isinstance(body.get(key), list):
                return body[key]
        return []

    # -- transport ------------------------------------------------------------

    def _post(self, endpoint: str, payload: dict) -> dict:
        import httpx  # imported lazily so dry-run needs no network stack

        try:
            resp = httpx.post(self._url(endpoint), headers=self._headers(),
                              json=payload, timeout=self.timeout)
        except Exception as exc:
            raise ESignError(f"{endpoint} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ESignError(f"{endpoint} returned {resp.status_code}: {_explain(resp)}")
        return _json_or_raise(resp, endpoint)

    def _get(self, endpoint: str, params: dict) -> dict:
        import httpx

        try:
            resp = httpx.get(self._url(endpoint), headers=self._headers(),
                             params=params, timeout=self.timeout)
        except Exception as exc:
            raise ESignError(f"{endpoint} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ESignError(f"{endpoint} returned {resp.status_code}: {_explain(resp)}")
        return _json_or_raise(resp, endpoint)

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


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


def normalise_created(body: dict) -> dict:
    """Flatten a live eSign response into the shape the rest of the app expects.

    The live 200 from `createfolder`, captured 2026-08-30, nests everything one level
    down and names the id `folderId` as an **integer**:

        {"folder": {"folderId": 35637439, "folderStatus": "DRAFT",
                    "folderCompanyId": 2922172, "folderDocumentIds": [41492365],
                    "documentsList": [...], "folderRecipientParties": [],
                    "draftFolderAccessURL": null}}

    Callers in app/main.py read `folderId` and `status` off a flat dict, and the
    dry-run fixtures already produce that shape. Doing the flattening here rather
    than at the call site means the dry-run and live paths cannot drift apart —
    which matters, because the dry-run path is the one that gets exercised.

    The id is stringified deliberately: it travels through URLs and session state, and
    an int that is sometimes a str is a bug waiting for the one day it is live.
    """
    if not isinstance(body, dict):
        return {"folderId": None, "status": "UNKNOWN", "raw": body}
    folder = body.get("folder") if isinstance(body.get("folder"), dict) else body
    folder_id = folder.get("folderId", folder.get("id"))
    out = {
        "folderId": str(folder_id) if folder_id is not None else None,
        "status": folder.get("folderStatus") or folder.get("status") or "DRAFT",
        "raw": body,
    }
    sessions = folder.get("embeddedSigningSessions") or body.get("embeddedSigningSessions")
    if sessions:
        out["embeddedSigningSessions"] = sessions
    if folder.get("draftFolderAccessURL"):
        out["draftFolderAccessURL"] = folder["draftFolderAccessURL"]
    return out


def _json_or_raise(resp, endpoint: str) -> dict:
    try:
        return resp.json()
    except Exception as exc:
        raise ESignError(f"{endpoint} returned a non-JSON body: {resp.text[:200]!r}") from exc


def _explain(resp) -> str:
    """Turn a rejection into something a 3am reader can act on.

    Cloudflare's 1010 is the specific failure that cost this project four runs, so it
    gets named rather than left as an opaque 403 body.
    """
    text = (resp.text or "")[:400]
    if "1010" in text:
        return (f"{text}  ← this is CLOUDFLARE, not Foxit: error 1010 is a "
                f"browser-signature ban at the edge. The request never reached the "
                f"API. Check the User-Agent header, not the credentials.")
    return text


def _now() -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " UTC"
