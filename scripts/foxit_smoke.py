#!/usr/bin/env python3
"""Foxit live smoke test — the one thing the development sandbox cannot do.

Every Foxit host returns 403 through the development environment's egress proxy, so
`agent/docgen_client.py` and `app/esign_client.py` were both written to published
request shapes and have never met the real service. This script is the first contact.

It is deliberately minimal and read-mostly:

  1. Confirms credentials are present and reachable.
  2. Creates ONE eSign folder (envelope) with sendNow=false — a draft. Nothing is
     emailed to anyone. Cost: 5 of the free plan's 500 annual credits.
  3. Prints the *shape* of what came back — status codes and key names — so the
     clients can be corrected against reality instead of documentation.

It never prints a credential, and it never sends an envelope.

Run it from the Actions tab (workflow_dispatch), not on every push.

Context, discovered 2026-08-30 by reading the portal rather than the docs: Foxit has
unified their APIs. There is ONE credential pair, on ONE host, using plain
client_id / client_secret headers. The integration guide's claim that eSign lives on
`na1.foxitesign.foxit.com` behind OAuth2 with a separate credential set is stale.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 45


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default or "").strip()
    if not value:
        sys.exit(f"::error::{name} is not set. Add it under Settings -> Secrets and variables -> Actions.")
    return value


def post(url: str, payload: dict, headers: dict) -> tuple[int, object]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw[:2000]
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw[:2000]
    except urllib.error.URLError as exc:
        return 0, f"network error: {exc.reason}"


def describe(value: object, depth: int = 0) -> str:
    """Summarise a response's structure without dumping potentially sensitive values."""
    pad = "  " * depth
    if isinstance(value, dict):
        lines = []
        for k, v in list(value.items())[:25]:
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}:")
                lines.append(describe(v, depth + 1))
            else:
                shown = str(v)
                if len(shown) > 80:
                    shown = shown[:77] + "..."
                lines.append(f"{pad}{k}: {shown}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return f"{pad}[] (empty)"
        return f"{pad}[{len(value)} items] first:\n" + describe(value[0], depth + 1)
    return f"{pad}{value}"


def main() -> int:
    client_id = env("FOXIT_CLIENT_ID")
    client_secret = env("FOXIT_CLIENT_SECRET")
    host = env("FOXIT_API_HOST", "https://na1.fusion.foxit.com").rstrip("/")

    print(f"host          : {host}")
    print(f"client_id     : {client_id[:12]}...{client_id[-4:]}  ({len(client_id)} chars)")
    print(f"client_secret : present, {len(client_secret)} chars — not printed")
    print()

    headers = {
        "client_id": client_id,
        "client_secret": client_secret,
        "Content-Type": "application/json",
    }

    # --- eSign: create a DRAFT envelope. sendNow=false. Nobody is emailed. ---------
    url = f"{host}/esign/api/v1/folders/createfolder"
    payload = {
        "folderName": "Ratify CI smoke test — draft only, do not send",
        "inputType": "url",
        "fileUrls": ["https://app.developer-api.foxit.com/esign/foxit-esign-api-sample.pdf"],
        "fileNames": ["Ratify CI Sample.pdf"],
        "sendNow": False,
    }

    print(f"POST {url}")
    print("      sendNow=false — this creates a draft and emails nobody")
    status, body = post(url, payload, headers)
    print(f"  -> HTTP {status}")
    print(describe(body, depth=1))
    print()

    if status == 0:
        print("::error::could not reach Foxit at all. Check FOXIT_API_HOST.")
        return 1
    if status in (401, 403):
        print("::error::authentication rejected. The credentials or the header names are wrong.")
        print("         Confirm in the Foxit portal that this is the same pair the dashboard shows.")
        return 1
    if status == 404:
        print("::error::endpoint not found. The path prefix is wrong for this account's region.")
        return 1
    if status >= 400:
        print(f"::error::Foxit rejected the request with {status}. The payload shape is wrong — see above.")
        return 1

    print("::notice::eSign reachable and the draft-envelope shape is accepted.")
    print()
    print("WHAT TO DO WITH THIS OUTPUT:")
    print("  Record the exact response key names above in claude/hackathon-spec.md, then")
    print("  correct app/esign_client.py to match. Note especially whether the folder id")
    print("  comes back as folderId, id, or something else — the send step needs it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
