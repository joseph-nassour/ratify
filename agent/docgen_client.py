"""Foxit Document Generation client.

Runs inside the AGENT subprocess. It holds the PDF/DocGen credentials
(`FOXIT_CLOUD_API_*`) and nothing else — see app/supervisor.py for how that is
enforced.

API shape verified in hackathon-spec.md §7:

    POST {host}/document-generation/api/GenerateDocumentBase64
    headers: client_id, client_secret, Content-Type: application/json
    body:    {"base64FileString": ..., "documentValues": {...}, "outputFormat": "pdf"}
    -> {"base64FileString": ..., "fileExtension": "pdf", "message": ...}

Note the auth style: **plain headers**, no OAuth. The eSign API on the other host does
OAuth2 client-credentials instead. Two products, two hosts, two credential sets, not
interchangeable — this is why blocked.md B6 was amended.

DRY_RUN=true is the committed default. In dry run nothing leaves the process and no
Foxit credits are spent; the local renderer in pdf_render.py produces the PDF.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Optional

from agent import pdf_render


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
        self.host = (host or os.environ.get("FOXIT_CLOUD_API_HOST", "")).rstrip("/")
        self.client_id = client_id or os.environ.get("FOXIT_CLOUD_API_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("FOXIT_CLOUD_API_CLIENT_SECRET", "")
        self.timeout = timeout
        if dry_run is None:
            dry_run = os.environ.get("DRY_RUN", "true").lower() != "false"
        # No credentials means dry run, always. A missing key must degrade to the
        # fixture path, never to a 401 in front of a judge.
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
        }
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
        except Exception as exc:  # network failure must not be fatal to the demo
            raise DocGenError(f"DocGen request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise DocGenError(f"DocGen returned {resp.status_code}: {resp.text[:400]}")
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
