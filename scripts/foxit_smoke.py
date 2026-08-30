#!/usr/bin/env python3
"""Foxit live diagnostic — identifies WHY authentication is being rejected."""

from __future__ import annotations
import json, os, sys, urllib.error, urllib.request

TIMEOUT = 45

def get(name, default=""):
    return os.environ.get(name, default)

def call(url, payload, headers):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")[:1200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:1200]
    except urllib.error.URLError as e:
        return 0, f"network error: {e.reason}"

cid = get("FOXIT_CLIENT_ID")
sec = get("FOXIT_CLIENT_SECRET")
host = get("FOXIT_API_HOST", "https://na1.fusion.foxit.com")

print("=" * 60)
print("CREDENTIAL HYGIENE")
print("=" * 60)
for label, v in (("CLIENT_ID", cid), ("CLIENT_SECRET", sec), ("API_HOST", host)):
    if not v:
        print(f"{label:14}: MISSING — the secret is not set at all")
        continue
    stripped = v.strip()
    print(f"{label:14}: {len(v)} chars", end="")
    if v != stripped:
        print(f"  *** HAS SURROUNDING WHITESPACE — {len(v) - len(stripped)} extra char(s). THIS IS THE BUG. ***")
    elif "\n" in v or "\r" in v:
        print("  *** CONTAINS A NEWLINE. THIS IS THE BUG. ***")
    else:
        print("  clean")
print(f"host starts with https : {host.startswith('https://')}")
print(f"host has trailing slash: {host.endswith('/')}   (should be False)")
print(f"client_id prefix       : {cid[:10] if cid else '(none)'}")
print()

cid, sec, host = cid.strip(), sec.strip(), host.strip().rstrip("/")
H = {"client_id": cid, "client_secret": sec, "Content-Type": "application/json"}

print("=" * 60)
print("PROBE 1 — Document Generation (does the credential pair work AT ALL?)")
print("=" * 60)
s1, b1 = call(f"{host}/document-generation/api/GenerateDocumentBase64", {}, H)
print(f"HTTP {s1}\n{b1}\n")

print("=" * 60)
print("PROBE 2 — eSign createfolder (draft only, emails nobody)")
print("=" * 60)
s2, b2 = call(f"{host}/esign/api/v1/folders/createfolder", {
    "folderName": "Ratify CI diagnostic — draft only",
    "inputType": "url",
    "fileUrls": ["https://app.developer-api.foxit.com/esign/foxit-esign-api-sample.pdf"],
    "fileNames": ["Ratify Sample.pdf"],
    "sendNow": False,
}, H)
print(f"HTTP {s2}\n{b2}\n")

print("=" * 60)
print("VERDICT")
print("=" * 60)
auth_fail = {401, 403}
if s1 in auth_fail and s2 in auth_fail:
    print("BOTH rejected -> the credential pair itself is wrong, or a secret was pasted")
    print("with hidden whitespace. Check the hygiene section above first.")
elif s1 not in auth_fail and s2 in auth_fail:
    print("DocGen accepted the credentials, eSign did not -> eSign needs DIFFERENT auth")
    print("on this account. The unified-API reading is wrong for eSign specifically.")
elif s2 not in auth_fail:
    print("eSign ACCEPTED the credentials. Any 4xx above is a PAYLOAD problem, which is")
    print("exactly what we wanted to learn. Record the response shape and fix the client.")
print("\nNote: a 400/404/422 is NOT an auth failure — it means auth passed.")
sys.exit(0)
