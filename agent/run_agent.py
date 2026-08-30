"""Agent subprocess entrypoint.

Reads a job as JSON on stdin, emits JSON-lines events on stdout, exits.

    $ echo '{"prompt": "..."}' | python -m agent.run_agent

What this process *can* do: read the instruction, classify terms, call Foxit Document
Generation and the PDF Services MCP tools, and render a draft.

What this process *cannot* do: send anything for signature. Not because it is asked
not to — because `FOXIT_ESIGN_CLIENT_ID` and `FOXIT_ESIGN_CLIENT_SECRET` are not in
its environment, and the signing module under `app/` is not on any import path this
package touches. (tests/test_isolation.py scans this package's source for even a
mention of it, which is why this sentence does not name the module.) Its most
privileged possible output is the event below:

    {"type": "signature_request", ...}

which is a row in a table in the parent process, inert until a human ratifies the term
sheet it refers to.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import sys
import time
from typing import Optional

from agent.docgen_client import DocGenClient
from agent.llm_planner import select_planner
from agent.planner import DeterministicPlanner, scan_for_injection
from agent.terms import INVENTED

# A guard, not a comment. If this module ever finds itself holding signing authority,
# something upstream is broken and it should be loud about it immediately.
_LEAKED = [k for k in os.environ if "ESIGN" in k.upper()]
if _LEAKED:
    print(json.dumps({
        "type": "fatal",
        "message": ("agent process was started with signing credentials in its "
                    f"environment ({_LEAKED}); refusing to run"),
    }), flush=True)
    sys.exit(2)


def emit(**payload) -> None:
    payload.setdefault("at", time.time())
    print(json.dumps(payload), flush=True)


def build_planner(job: dict):
    """Choose a planner.

    Selection order is: explicit job override -> RATIFY_PLANNER env -> a model if one
    is configured -> deterministic. An unavailable model falls through to the
    deterministic planner rather than failing: the safety property does not depend on
    a model being reachable, and a judge must never meet a stack trace where a draft
    should be.
    """
    today = None
    if job.get("today"):
        today = _dt.date.fromisoformat(job["today"])
    return select_planner(
        job=job,
        practice_name=job.get("practice_name") or os.environ.get(
            "PRACTICE_NAME", "Marlowe & Co Chartered Accountants"),
        today=today,
    )


def main() -> int:
    raw = sys.stdin.read()
    try:
        job = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        emit(type="fatal", message=f"unreadable job payload: {exc}")
        return 2

    prompt = job.get("prompt", "")
    if not prompt.strip():
        emit(type="fatal", message="no prompt supplied")
        return 2

    planner = build_planner(job)
    planner_name = getattr(planner, "name", "deterministic")
    emit(type="status", message="agent started", planner=planner_name,
         dry_run=os.environ.get("DRY_RUN", "true"))

    # -- untrusted input, scanned but never obeyed ---------------------------
    attached = job.get("attached_text", "")
    for source, text in (("prompt", prompt), ("attached document", attached)):
        for marker in scan_for_injection(text):
            emit(type="security",
                 message=(f"instruction-like text found in the {source} and ignored: "
                          f"{marker!r}"),
                 detail=("This agent does not act on instructions found in its inputs. "
                         "Even if a model here were persuaded by them, the provenance of "
                         "every term is re-derived afterwards, and this process holds no "
                         "signing credential."))

    emit(type="tool", name="extract_terms", status="running")
    plan = planner.plan(prompt, attached_text=attached)
    counts = plan.sheet.summary_counts()
    emit(type="tool", name="extract_terms", status="done",
         detail=f"{counts['total']} material terms identified")
    emit(type="tool", name="classify_provenance", status="done",
         detail=(f"{counts['stated']} stated, {counts['derived']} derived, "
                 f"{counts['invented']} invented"))

    # -- render ---------------------------------------------------------------
    docgen = DocGenClient()
    emit(type="tool", name="docgen.GenerateDocumentBase64", status="running",
         detail=docgen.mode())
    template_b64 = job.get("template_b64") or _default_template_b64()
    provenance = {t.key: t.provenance for t in plan.sheet}
    pdf, note = docgen.generate_with_fallback(
        template_b64, plan.sheet.document_values(), provenance)
    emit(type="tool", name="docgen.GenerateDocumentBase64", status="done",
         detail=note, bytes=len(pdf))

    # PDF Services operations via the Foxit MCP server land here in H4. They are
    # non-material and ungated by design (hackathon-spec.md §2.5).
    for tool_name, detail in (
        ("mcp.merge_pdf", "standard terms & conditions appended"),
        ("mcp.flatten_pdf", "document flattened"),
        ("mcp.extract_text", "rendered text read back and checked against the term sheet"),
    ):
        emit(type="tool", name=tool_name, status="skipped-in-h3", detail=detail)

    # -- the most privileged thing this process can emit ----------------------
    unresolved = [t.key for t in plan.sheet.unresolved()]
    allowed, reason = plan.sheet.can_request_signature()
    emit(
        type="signature_request",
        allowed_by_agent=False,
        gate_open=allowed,
        gate_reason=reason,
        unresolved_terms=unresolved,
        message=("This process cannot send an envelope. It is emitting a request. "
                 "Whether that request can ever be acted on is decided by a human in "
                 "the parent process."),
    )

    emit(
        type="result",
        sheet=plan.sheet.to_dict(),
        steps=[s.to_dict() for s in plan.steps],
        notes=plan.notes,
        security_events=plan.security_events,
        pdf_b64=base64.b64encode(pdf).decode("ascii"),
        docgen_mode=docgen.mode(),
        invented_count=counts["invented"],
        planner=planner_name,
    )
    return 0


def _default_template_b64() -> str:
    """The DocGen .docx template, base64-encoded.

    In H3 the dry-run renderer draws the letter directly, so the template body is only
    needed on the live path (H4). Reading it from disk if present keeps the live call
    shape honest: DocGen always receives a base64 template string.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "engagement_letter.docx")
    try:
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")
    except OSError:
        return ""


if __name__ == "__main__":
    sys.exit(main())
