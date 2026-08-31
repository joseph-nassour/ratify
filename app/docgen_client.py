"""Foxit Document Generation client — PARENT PROCESS ONLY.

**This module used to live in `agent/`. Moving it is the substance of the 2026-08-30
correction, not a tidy-up.**

The original design gave the agent subprocess the Document Generation credential and
withheld the eSign one: rendering a PDF is reversible and non-material, releasing an
envelope is not, so the agent got the first and not the second. That reasoning is
sound and the implementation was correct — against the API Foxit used to have.

Foxit has since unified their APIs behind **one credential pair on one host**:

    Document Generation : {host}/document-generation/api/...
    eSign               : {host}/esign/api/v1/...

Both authenticate with the same `client_id` / `client_secret` headers. So the
credential this module needs is, byte for byte, the credential that can create and
release a signature envelope. **There is no document-only Foxit key to give an agent.**
A process holding this pair could `POST /esign/api/v1/folders/createfolder` with
`sendNow: true` in a single call, and no amount of tool-scoping in the agent's harness
would stop it — the credential is the capability.

Hence: the credential never enters the agent process, and neither does this module.
The agent composes the document *content*; the supervisor renders it. What crosses the
process boundary is a term sheet — data, not authority.

    POST {host}/document-generation/api/GenerateDocumentBase64
    headers: client_id, client_secret, Content-Type, Accept, User-Agent
    body:    {"base64FileString": ..., "documentValues": {...}, "outputFormat": "pdf"}
    -> {"base64FileString": ..., "fileExtension": "pdf", "message": ...}

Live status, 2026-08-30: authentication passes. A deliberately empty payload returned
HTTP 500 *"An error occurred while analyzing the template: Invalid output format.
Supported formats are 'docx' and 'pdf'."* — an ugly status code for a bad request, but
not an auth failure. The call has never been made with a real template.

DRY_RUN=true is the committed default. In dry run nothing leaves the process, no Foxit
credits are spent, and the local renderer in agent/pdf_render.py produces the PDF.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Optional

from agent import pdf_render
from app.esign_client import USER_AGENT


class DocGenError(Exception):
    pass


class DocGenClient:
    def __init__(
        self,
        host: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        dry_run: Optional[bool] = None,
        timeout: float = 60.0,
    ) -> None:
        self.host = (host or os.environ.get("FOXIT_API_HOST", "")).strip().rstrip("/")
        self.client_id = (client_id or os.environ.get("FOXIT_CLIENT_ID", "")).strip()
        self.client_secret = (client_secret or os.environ.get("FOXIT_CLIENT_SECRET", "")).strip()
        self.timeout = timeout
        if dry_run is None:
            dry_run = os.environ.get("DRY_RUN", "true").lower() != "false"
        # No credentials means dry run, always. A missing key must degrade to the
        # local renderer, never to a 401 in front of a judge.
        self.dry_run = bool(dry_run or not (self.client_id and self.client_secret and self.host))
        self.calls = 0

    # -- public --------------------------------------------------------------

    def generate(self, template_b64: str, document_values: dict,
                 provenance: dict = None, output_format: str = "pdf") -> bytes:
        """Render a document. Returns raw PDF bytes."""
        self.calls += 1
        if self.dry_run:
            return pdf_render.render_engagement_letter(document_values, provenance or {})
        return self._live_generate(template_b64, document_values, output_format)

    def mode(self) -> str:
        return "dry-run (local renderer, 0 Foxit credits)" if self.dry_run else f"live ({self.host})"

    # -- live path -----------------------------------------------------------

    def _live_generate(self, template_b64: str, document_values: dict, output_format: str) -> bytes:
        import httpx  # imported lazily so dry-run needs no network stack

        url = f"{self.host}/document-generation/api/GenerateDocumentBase64"
        payload = {
            "base64FileString": template_b64,
            "documentValues": document_values,
            "outputFormat": output_format,
        }
        headers = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
            # See app/esign_client.USER_AGENT: without this, Cloudflare rejects the
            # request at the edge with a 1010 that reads exactly like an auth failure.
            "User-Agent": USER_AGENT,
        }
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
        except Exception as exc:  # network failure must not be fatal to the demo
            raise DocGenError(f"DocGen request failed: {exc}") from exc
        if resp.status_code >= 400:
            body = (resp.text or "")[:400]
            if "1010" in body:
                body += ("  ← CLOUDFLARE 1010, not a Foxit rejection: the request "
                         "never reached the API. Check the User-Agent header.")
            raise DocGenError(f"DocGen returned {resp.status_code}: {body}")
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise DocGenError("DocGen returned a non-JSON body") from exc
        b64 = data.get("base64FileString")
        if not b64:
            raise DocGenError(f"DocGen returned no document: {data.get('message')!r}")
        return base64.b64decode(b64)

    def generate_with_fallback(self, template_b64: str, document_values: dict,
                               provenance: dict = None) -> tuple:
        """Render live if configured, but never let a vendor failure kill the demo.

        Returns (pdf_bytes, note). The note is shown in the tool log, so a fallback is
        visible rather than silent.
        """
        if self.dry_run:
            return self.generate(template_b64, document_values, provenance), "rendered locally (dry run)"
        try:
            return self.generate(template_b64, document_values, provenance), "rendered by Foxit Document Generation"
        except DocGenError as exc:
            pdf = pdf_render.render_engagement_letter(document_values, provenance or {})
            return pdf, f"Foxit DocGen unavailable ({exc}); fell back to the local renderer"


def default_template_b64() -> str:
    """The DocGen .docx template, base64-encoded.

    In dry run the local renderer draws the letter directly, so the template body is
    only needed on the live path. Reading it from disk if present keeps the live call
    shape honest: DocGen always receives a base64 template string.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "engagement_letter.docx")
    try:
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")
    except OSError:
        return ""
