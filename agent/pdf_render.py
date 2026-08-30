"""A tiny, dependency-free PDF writer.

Used only when `DRY_RUN=true`, to stand in for Foxit Document Generation so the whole
pipeline — render, serve, envelope, sign — can be exercised end to end with **no
credentials and no credits spent**. Foxit's free Developer plan is 500 shared credits
per year for the entire project (hackathon-spec.md §7.2); burning them on development
runs would be the easiest way to lose the live demo.

It writes a genuine PDF (parseable, openable, text-selectable in Helvetica), not a
placeholder blob, because the eSign path needs a real file at /doc/{id}.pdf and a
judge clicking the draft link should see a document.
"""

from __future__ import annotations

from typing import List, Tuple

PAGE_W, PAGE_H = 595, 842  # A4 points
MARGIN_X, TOP_Y = 56, 786
LEADING = 15


def _esc(s: str) -> str:
    out = []
    for ch in s:
        if ch in "()\\":
            out.append("\\" + ch)
        elif ord(ch) < 32:
            out.append(" ")
        elif ord(ch) > 126:
            # Helvetica/WinAnsi: transliterate the few characters we actually emit
            out.append({"£": "\\243", "€": "\\200", "’": "'", "—": "-", "–": "-"}.get(ch, "?"))
        else:
            out.append(ch)
    return "".join(out)


def _wrap(text: str, width: int) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if len(trial) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def render_lines(lines: List[Tuple[str, str]]) -> bytes:
    """Render (style, text) pairs to PDF bytes.

    Styles: "h1", "h2", "body", "small", "gap".
    Paginates automatically.
    """
    pages: List[List[str]] = []
    ops: List[str] = []
    y = TOP_Y

    def newpage():
        nonlocal ops, y
        if ops:
            pages.append(ops)
        ops, y = [], TOP_Y

    for style, text in lines:
        if style == "gap":
            y -= LEADING // 2
            continue
        font, size, width = {
            "h1": ("F2", 17, 60),
            "h2": ("F2", 11, 88),
            "body": ("F1", 10.5, 92),
            "small": ("F1", 8.5, 112),
        }.get(style, ("F1", 10.5, 92))
        for line in _wrap(text, width):
            if y < 60:
                newpage()
            ops.append(
                f"BT /{font} {size} Tf {MARGIN_X} {y:.0f} Td ({_esc(line)}) Tj ET"
            )
            y -= LEADING if style != "small" else LEADING - 3
        y -= 4
    pages.append(ops)

    # --- assemble the file ---------------------------------------------------
    objects: List[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)  # 1-indexed object number

    font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")

    pages_obj_num = len(objects) + 1 + 2 * len(pages)  # reserve page + content objects
    page_nums: List[int] = []
    for page_ops in pages:
        stream = "\n".join(page_ops).encode("latin-1", "replace")
        content_num = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_num = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_obj_num, PAGE_W, PAGE_H, font_regular, font_bold, content_num)
        )
        page_nums.append(page_num)

    kids = b" ".join(b"%d 0 R" % n for n in page_nums)
    pages_num = add(b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_nums), kids))
    assert pages_num == pages_obj_num, "page-tree object number reservation drifted"
    catalog_num = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_num)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += (
        b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, catalog_num, xref_at)
    )
    return bytes(out)


def render_engagement_letter(values: dict, flags: dict = None) -> bytes:
    """Render a term sheet's values as an engagement letter.

    `flags` maps term key -> provenance, so the dry-run PDF can carry the same
    provenance marks the UI shows. A judge downloading the draft sees exactly what the
    screen said.
    """
    flags = flags or {}
    g = lambda k, d="—": values.get(k, d)  # noqa: E731

    def mark(key: str) -> str:
        p = flags.get(key)
        return "   [AGENT-SUPPLIED]" if p == "invented" else ""

    lines: List[Tuple[str, str]] = [
        ("h1", "Engagement Letter"),
        ("small", "DRAFT — rendered by Ratify. Not executed until signed."),
        ("gap", ""),
        ("body", f"From: {g('provider_name')}"),
        ("body", f"To: {g('client_name')}"),
        ("body", f"Attention: {g('signer_name')} ({g('signer_email')})"),
        ("gap", ""),
        ("h2", "1. Scope of services"),
        ("body", g("scope")),
        ("gap", ""),
        ("h2", "2. Period of engagement"),
        ("body", f"Commencing {g('period_start', 'on execution')} and ending {g('period_end')}."),
        ("gap", ""),
        ("h2", "3. Fees"),
        ("body", f"Total fee: {g('fee_amount')}. Payment schedule: {g('payment_schedule')}."),
    ]
    if "instalment_amount" in values:
        lines.append(("body", f"Instalments: {g('instalment_amount')}."))
    if "payment_due_days" in values:
        lines.append(("body", g("payment_due_days") + mark("payment_due_days")))

    section = 4
    for key, heading in (
        ("liability_cap", "Limitation of liability"),
        ("termination_notice", "Termination"),
        ("auto_renewal", "Renewal"),
        ("governing_law", "Governing law"),
    ):
        if key in values and values[key]:
            lines += [("gap", ""), ("h2", f"{section}. {heading}"),
                      ("body", values[key] + mark(key))]
            section += 1

    lines += [
        ("gap", ""),
        ("h2", f"{section}. Acceptance"),
        ("body", "Signed for and on behalf of the client:"),
        ("gap", ""),
        ("body", "[[SIGNATURE]]"),
        ("body", f"{g('signer_name')}"),
        ("gap", ""),
        ("small",
         "Terms marked [AGENT-SUPPLIED] were not stated by the instructing party. "
         "They were supplied by an automated drafting agent and must be individually "
         "resolved by a human before this document can be sent for signature."),
    ]
    return render_lines(lines)
