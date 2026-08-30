# Ratify

**An agent that drafts client engagement letters and cannot sign them — not because it
is told not to, but because the signing credential does not exist in the process it
runs in.**

Built for the Foxit challenge *"Your Agent Shouldn't Sign That"* at the DevNetwork
[API + Cloud + AI] Hackathon 2026.

The boundary argument — where the automated/human line is drawn and why — is in
[`DESIGN.md`](DESIGN.md). It is the point of the project; the code is the proof.

---

## The short version

1. You describe an engagement in plain English.
2. The agent drafts the letter, and separates every material term into
   **stated** (your words), **derived** (arithmetic on your words) and **invented**
   (it made this up).
3. **A signature request cannot exist while any invented term is unresolved.** You
   accept, edit or delete each one.
4. Ratifying mints an approval **bound to that exact version of the document**. Change
   a term afterwards and the approval is revoked automatically.
5. Only then does the parent process create a Foxit eSign envelope — as a `DRAFT`
   (`sendNow: false`), released by a separate call.

The agent runs in its own OS process whose environment is built from an allowlist. The
eSign credentials are not on that allowlist. Visit `/agent-env` to see it.

## Run it

Nothing below needs a Foxit account. `DRY_RUN=true` is the committed default: the
pipeline runs end to end against local fixtures and spends **zero** Foxit credits.

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
# open http://127.0.0.1:8000
```

Tests:

```bash
python -m unittest discover -s tests -t .
```

Demo video (Playwright records it; no camera, no microphone):

```bash
python scripts/record_demo.py --smoke   # ~10s pipeline check
python scripts/record_demo.py           # the full walkthrough -> build/video/*.webm
```

Docker, as deployed:

```bash
docker build -t ratify . && docker run -p 8000:8000 -e PORT=8000 ratify
```

## Going live against Foxit

Copy `.env.example` to `.env` and fill it in. **Note that Foxit needs two credential
pairs, not one** — the PDF/Document Generation APIs and the eSign API are different
products on different hosts with different auth (headers vs OAuth2), and the
credentials are not interchangeable.

| Variable | Which process can see it |
|---|---|
| `FOXIT_CLOUD_API_HOST` / `_CLIENT_ID` / `_CLIENT_SECRET` | parent **and** agent |
| `FOXIT_ESIGN_HOST` / `FOXIT_ESIGN_CLIENT_ID` / `FOXIT_ESIGN_CLIENT_SECRET` | **parent only** |
| `GEMINI_API_KEY` | agent only |
| `PUBLIC_BASE_URL` | parent — set after the first deploy; eSign fetches the PDF from it |

Then set `DRY_RUN=false`. The free Developer plan is 500 shared credits **per year**
(5 per eSign envelope), so the client refuses to create more than 25 live envelopes and
says so loudly rather than silently exhausting the allowance.

## Layout

```
app/       parent web process — holds the eSign credentials
  approval.py    approval tokens: minted only through the gate, bound to a document
  supervisor.py  builds the agent's environment from an allowlist and spawns it
  esign_client.py  ★ the only code that can cause a legally operative event
  main.py        routes; exactly one of them can release an envelope
agent/     the drafting agent — runs as a subprocess, no signing authority
  terms.py       TermSheet + provenance + the gate itself
  planner.py     deterministic planner (LLM planner plugs in behind the same interface)
  docgen_client.py, pdf_render.py
tests/     the gate, the approvals, the isolation boundary, and a full dry-run journey
```

## AI authorship

**This project was designed and written by Claude (Anthropic), working autonomously in
scheduled overnight sessions.** A human — Joseph Nassour — chose the problem, reviews
the output, holds the accounts and submits the entry. He is the accountable party; the
code is the machine's.

That is the same boundary this project argues for, which is either a good sign or a
suspiciously convenient one. Judge the code.

## Known limits

- Sessions live in memory. The free hosting tier restarts when idle, so an in-flight
  draft does not survive an idle gap. Nothing important is stored there: PDFs are
  regenerable from the term sheet and signed documents live at Foxit.
- The deterministic planner handles the engagement-letter domain and will not extract
  terms from an arbitrary contract type.
- Webhooks are not used; envelope status is polled. Deliberate — see `DESIGN.md`.
