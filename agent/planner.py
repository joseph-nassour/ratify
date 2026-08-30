"""Planners — turn a plain-English instruction into a TermSheet and a tool plan.

Two implementations behind one interface:

* `DeterministicPlanner` — rule-based. No network, no API key, no model. It is the
  floor: the demo cannot be taken down by a rate limit, an expired key, or a vendor
  outage, and the safety property does not depend on a model behaving.
* `LLMPlanner` — added in H4. Same interface, same output type, and it passes through
  exactly the same gate. Swapping it in changes the quality of the draft and nothing
  about what the human is asked to approve.

The deterministic planner has one property worth stating plainly in DESIGN.md: it
**cannot be prompt-injected**, because it does not interpret the text as instructions
at all. It pattern-matches for commercial terms and ignores everything else. When the
LLM planner is used instead, the isolation in app/supervisor.py is what carries the
same guarantee.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import List, Optional

from agent.terms import DERIVED, INVENTED, STATED, Term, TermSheet

# ---------------------------------------------------------------------------
# Tool plan (what the agent will do, shown live in the UI)
# ---------------------------------------------------------------------------


@dataclass
class ToolStep:
    name: str
    description: str
    gated: bool = False          # does this step need a human decision?
    material: bool = False       # does this step change what the human is bound to?

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "gated": self.gated,
            "material": self.material,
        }


@dataclass
class Plan:
    sheet: TermSheet
    steps: List[ToolStep] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    security_events: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sheet": self.sheet.to_dict(),
            "steps": [s.to_dict() for s in self.steps],
            "notes": self.notes,
            "security_events": self.security_events,
        }


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

CURRENCIES = {
    "AED": "AED", "USD": "USD", "GBP": "GBP", "EUR": "EUR", "SAR": "SAR",
    "QAR": "QAR", "KWD": "KWD", "BHD": "BHD", "OMR": "OMR",
    "$": "USD", "£": "GBP", "€": "EUR",
}

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_COMPANY_SUFFIX = (
    r"(?:FZ-?LLC|FZ-?LLE|FZE|LLC|L\.L\.C\.|Ltd\.?|Limited|LLP|PLC|Inc\.?|"
    r"Holdings|Group|Partners|Trading|Consulting|Consultancy|& Co\.?)"
)

_SERVICE_WORDS = (
    "bookkeeping", "vat filing", "vat return", "vat", "audit", "statutory audit",
    "payroll", "tax", "corporate tax", "advisory", "valuation", "due diligence",
    "financial modelling", "modeling", "management accounts", "compilation",
    "review engagement", "consulting", "equity research", "company secretarial",
)

#: Phrases that are attempts to instruct the agent rather than describe an engagement.
#: We do not obey them; we record that we saw them. See tests/test_injection.py.
_INJECTION_MARKERS = (
    # attempts to make the agent act
    "ignore previous instructions", "ignore all previous", "disregard the above",
    "disregard previous", "send for signature", "send this for signature",
    "sign this now", "sign immediately", "auto-sign", "autosign",
    "you are authorised to sign", "you are authorized to sign", "skip approval",
    "skip the approval", "no approval needed", "bypass", "system:", "</system>",
    "new instructions",
    # attempts to falsify provenance -- the subtler and more dangerous family, aimed
    # at the gate rather than at the signing call. See agent/llm_planner.py.
    "pre-approved", "pre approved", "already approved", "already been approved",
    "mark all terms", "mark these terms", "treat these terms as", "treat as stated",
    "no human review", "without human review", "no further review",
    "requires no approval", "standard and non-negotiable", "do not flag",
)


def _norm(text: str) -> str:
    return (
        text.replace("—", " - ")
        .replace("–", " - ")
        .replace("’", "'")
        .replace(" ", " ")
    )


def scan_for_injection(text: str) -> List[str]:
    """Report instruction-like phrases found in untrusted text.

    Reporting only. Nothing in this codebase acts on text found in a prompt or a
    document; this exists so the UI can *show* the attempt, which is the point of the
    adversarial demo.
    """
    low = _norm(text).lower()
    return [m for m in _INJECTION_MARKERS if m in low]


def extract_currency_amount(text: str):
    """Return (currency, amount_str, matched_text) or None."""
    pattern = (
        r"(AED|USD|GBP|EUR|SAR|QAR|KWD|BHD|OMR|\$|£|€)\s?"
        r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)"
    )
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        cur = CURRENCIES.get(m.group(1).upper(), m.group(1).upper())
        return cur, m.group(2), m.group(0)
    # trailing-currency form: "12,000 AED"
    m = re.search(
        r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)\s?"
        r"(AED|USD|GBP|EUR|SAR|QAR|KWD|BHD|OMR)\b",
        text, re.IGNORECASE,
    )
    if m:
        return m.group(2).upper(), m.group(1), m.group(0)
    return None


def extract_client(text: str) -> Optional[str]:
    text = _norm(text)
    m = re.search(
        r"(?:engagement letter|letter|agreement|sow|statement of work|contract)\s+"
        r"(?:of engagement\s+)?for\s+(.+?)(?:\s+-\s+|,|\.|\bfor\b|$)",
        text, re.IGNORECASE,
    )
    if m:
        cand = m.group(1).strip(" -,")
        if 2 <= len(cand) <= 90:
            return cand
    m = re.search(rf"\b([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){{0,4}}\s+{_COMPANY_SUFFIX})", text)
    if m:
        return m.group(1).strip()
    return None


def extract_scope(text: str, client: Optional[str]) -> Optional[str]:
    text = _norm(text)
    tail = text
    if client and client in text:
        tail = text.split(client, 1)[1]
    # prefer the clause introduced by a dash or colon
    m = re.search(r"[-:]\s*(.+)", tail)
    segment = m.group(1) if m else tail
    # cut at the first commercial clause
    segment = re.split(
        r"\s+(?:for the\b|for Q[1-4]\b|covering\b|during\b|from\b|,)", segment, 1
    )[0]
    segment = segment.strip(" -,.")
    low = segment.lower()
    if any(w in low for w in _SERVICE_WORDS) and 3 <= len(segment) <= 160:
        return segment
    # fall back: any sentence fragment containing a known service word
    for frag in re.split(r"[,.;]", text):
        if any(w in frag.lower() for w in _SERVICE_WORDS):
            frag = frag.strip(" -,.")
            if 3 <= len(frag) <= 160:
                return frag
    return None


def extract_signer(text: str):
    """Return (name, email) — either may be None."""
    text = _norm(text)
    email = None
    me = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    if me:
        email = me.group(0).rstrip(".,;")
    name = None
    # The keyword is matched case-insensitively; the name is not, because
    # capitalisation is how we tell a name from the rest of the sentence.
    mn = re.search(
        r"(?i:signer|signatory|signed by|to be signed by|counterpart)\s*(?i:is|:)?\s*"
        r"([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+){1,3})",
        text,
    )
    if mn:
        name = mn.group(1).strip()
    return name, email


def extract_cadence(text: str) -> Optional[str]:
    low = _norm(text).lower()
    for phrase, value in (
        ("paid monthly", "Monthly"),
        ("per month", "Monthly"),
        ("monthly", "Monthly"),
        ("quarterly", "Quarterly"),
        ("paid annually", "Annually"),
        ("annually", "Annually"),
        ("on completion", "On completion"),
        ("upfront", "In advance"),
        ("in advance", "In advance"),
        ("50/50", "50% in advance, 50% on completion"),
    ):
        if phrase in low:
            return value
    return None


def _fmt(d: _dt.date) -> str:
    return f"{d.day} {d.strftime('%B')} {d.year}"


def extract_period(text: str, today: _dt.date):
    """Return (end_date, end_text, start_date, start_reason) — any may be None."""
    t = _norm(text)

    # "quarter ending 31 December" / "period ending 31 Dec 2026"
    m = re.search(
        r"(quarter|month|year|period|half[- ]year)\s+end(?:ing|ed)\s+"
        r"(\d{1,2})\s+([A-Za-z]+)\.?(?:\s+(\d{4}))?",
        t, re.IGNORECASE,
    )
    if m:
        unit = m.group(1).lower()
        day = int(m.group(2))
        month = _MONTHS.get(m.group(3).lower())
        year = int(m.group(4)) if m.group(4) else None
        if month:
            if year is None:
                year = today.year
                try:
                    if _dt.date(year, month, day) < today:
                        year += 1
                except ValueError:
                    pass
            try:
                end = _dt.date(year, month, day)
            except ValueError:
                return None, m.group(0), None, None
            months_back = {"quarter": 3, "month": 1, "year": 12, "half-year": 6, "half year": 6}.get(unit)
            if months_back is None:
                return end, m.group(0), None, None
            sm = end.month - months_back + 1
            sy = end.year
            while sm <= 0:
                sm += 12
                sy -= 1
            start = _dt.date(sy, sm, 1)
            return end, m.group(0), start, (
                f"the {unit} ending {_fmt(end)} begins on {_fmt(start)}"
            )

    # "six months from 1 October" / "12 months from 1 Oct 2026"
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "nine": 9, "twelve": 12, "twenty-four": 24}
    m = re.search(
        r"\b(\d{1,2}|one|two|three|four|five|six|nine|twelve|twenty-four)\s+months?\s+"
        r"(?:from|starting|commencing)\s+(\d{1,2})\s+([A-Za-z]+)\.?(?:\s+(\d{4}))?",
        t, re.IGNORECASE,
    )
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else words.get(m.group(1).lower(), 0)
        day = int(m.group(2))
        month = _MONTHS.get(m.group(3).lower())
        year = int(m.group(4)) if m.group(4) else today.year
        if month and n:
            try:
                start = _dt.date(year, month, day)
            except ValueError:
                return None, m.group(0), None, None
            em = start.month + n
            ey = start.year
            while em > 12:
                em -= 12
                ey += 1
            last_day = (_dt.date(ey, em, 1) - _dt.timedelta(days=1))
            return last_day, m.group(0), start, (
                f"{n} months from {_fmt(start)} ends on {_fmt(last_day)}"
            )
    return None, None, None, None


def _months_between(start: _dt.date, end: _dt.date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def _money(amount: float) -> str:
    if abs(amount - round(amount)) < 0.005:
        return f"{int(round(amount)):,}"
    return f"{amount:,.2f}"


# ---------------------------------------------------------------------------
# Invented defaults — the dangerous ones
# ---------------------------------------------------------------------------

#: Terms an LLM asked to draft an engagement letter will happily supply, complete and
#: plausible, in the same typeface as the terms it was given. Every one of these is a
#: real obligation nobody in the conversation ever mentioned. They are the reason this
#: project exists, so the deterministic planner invents them too -- honestly labelled.
INVENTED_DEFAULTS = [
    dict(
        key="payment_due_days",
        label="Payment terms",
        value="Invoices payable within 14 days of issue",
        evidence="not specified. A payment period is required for the fee clause to be "
                 "enforceable, so a common commercial default was supplied.",
        removable=False,
    ),
    dict(
        key="liability_cap",
        label="Limitation of liability",
        value="Liability capped at the total fees paid under this engagement",
        evidence="not specified. A cap materially limits what the client can recover "
                 "from you and what you can recover from them. Nobody asked for this figure.",
    ),
    dict(
        key="governing_law",
        label="Governing law and jurisdiction",
        value="The laws of the Dubai International Financial Centre (DIFC)",
        evidence="not specified. Jurisdiction was inferred from the client's apparent "
                 "location. Inferring a legal forum from an address is a guess.",
    ),
    dict(
        key="auto_renewal",
        label="Automatic renewal",
        value="Renews automatically for successive 12-month terms unless cancelled "
              "in writing 30 days before expiry",
        evidence="not specified, and not implied by anything in your instruction. This "
                 "clause extends the engagement indefinitely by default.",
    ),
    dict(
        key="termination_notice",
        label="Termination",
        value="Either party may terminate on 30 days' written notice",
        evidence="not specified. A notice period was supplied so the agreement has an exit.",
    ),
]


# ---------------------------------------------------------------------------
# Deterministic planner
# ---------------------------------------------------------------------------


class DeterministicPlanner:
    """Rule-based. No model, no network, no key. The floor the demo stands on."""

    name = "deterministic"

    def __init__(self, practice_name: str = "Marlowe & Co Chartered Accountants",
                 today: Optional[_dt.date] = None) -> None:
        self.practice_name = practice_name
        self.today = today or _dt.date.today()

    # -- public API ----------------------------------------------------------

    def plan(self, prompt: str, attached_text: str = "") -> Plan:
        """Produce a term sheet from the instruction.

        `attached_text` is accepted and **deliberately unused**. This planner reads
        only the accountable human's own instruction; an attached document cannot
        change a single term it produces. That is not a limitation of the rule-based
        approach, it is the reason it is the floor: the demo's worst case is a
        planner that is impossible to influence. `LLMPlanner` does read the document,
        and pays for that with the adjudication step in agent/llm_planner.py.
        """
        sheet = TermSheet(prompt=prompt)
        security_events = [
            f"instruction-like phrase found in input and NOT acted on: {m!r}"
            for m in scan_for_injection(prompt)
        ]
        notes: List[str] = []

        # --- provider (from the practice profile, not from the prompt) -------
        sheet.add(Term(
            key="provider_name", label="Your practice", value=self.practice_name,
            provenance=STATED, evidence="from your practice profile",
            material=True, removable=False,
        ))

        # --- stated terms ----------------------------------------------------
        client = extract_client(prompt)
        sheet.add(Term(
            key="client_name", label="Client",
            value=client or "[client not identified]",
            provenance=STATED if client else INVENTED,
            evidence=(f"you wrote: {client!r}" if client else
                      "no client could be identified in your instruction; a placeholder was inserted"),
            material=True, removable=False,
        ))

        scope = extract_scope(prompt, client)
        sheet.add(Term(
            key="scope", label="Scope of services",
            value=scope or "Professional services as agreed",
            provenance=STATED if scope else INVENTED,
            evidence=(f"you wrote: {scope!r}" if scope else
                      "no scope was stated. A catch-all was supplied, which is the widest "
                      "possible obligation and almost certainly not what you meant."),
            material=True, removable=False,
        ))

        # --- period ----------------------------------------------------------
        end, end_text, start, start_reason = extract_period(prompt, self.today)
        if end is not None:
            sheet.add(Term(
                key="period_end", label="Engagement ends", value=_fmt(end),
                provenance=STATED, evidence=f"you wrote: {end_text!r}",
                material=True, removable=False,
            ))
            if start is not None:
                sheet.add(Term(
                    key="period_start", label="Engagement begins", value=_fmt(start),
                    provenance=DERIVED, evidence=start_reason,
                    material=True, removable=False,
                ))
        else:
            sheet.add(Term(
                key="period_end", label="Engagement ends",
                value=_fmt(self.today + _dt.timedelta(days=365)),
                provenance=INVENTED,
                evidence="no engagement period was stated. A 12-month term was supplied — "
                         "this commits you for a year.",
                material=True, removable=False,
            ))
            notes.append("No engagement period found in the instruction.")

        # --- fee -------------------------------------------------------------
        money = extract_currency_amount(prompt)
        cadence = extract_cadence(prompt)
        if money:
            cur, amount_str, matched = money
            sheet.add(Term(
                key="fee_amount", label="Fee", value=f"{cur} {amount_str}",
                provenance=STATED, evidence=f"you wrote: {matched!r}",
                material=True, removable=False,
            ))
        else:
            sheet.add(Term(
                key="fee_amount", label="Fee", value="[fee not stated]",
                provenance=INVENTED,
                evidence="no fee was stated. A document cannot be sent for signature "
                         "with an unresolved fee.",
                material=True, removable=False,
            ))
            notes.append("No fee found in the instruction.")

        if cadence:
            sheet.add(Term(
                key="payment_schedule", label="Payment schedule", value=cadence,
                provenance=STATED, evidence=f"you wrote a payment cadence of {cadence.lower()!r}",
                material=True, removable=False,
            ))
        else:
            sheet.add(Term(
                key="payment_schedule", label="Payment schedule",
                value="On completion", provenance=INVENTED,
                evidence="no payment schedule was stated; 'on completion' was supplied.",
                material=True, removable=False,
            ))

        # --- derived instalment ---------------------------------------------
        if money and cadence == "Monthly" and start and end:
            try:
                total = float(money[1].replace(",", ""))
                months = max(1, _months_between(start, end))
                per = total / months
                sheet.add(Term(
                    key="instalment_amount", label="Monthly instalment",
                    value=f"{money[0]} {_money(per)} per month",
                    provenance=DERIVED,
                    evidence=(f"{money[0]} {money[1]} over {months} months "
                              f"({_fmt(start)} to {_fmt(end)}) = {money[0]} {_money(per)} per month. "
                              f"Your instruction did not say whether the fee is per month or "
                              f"for the whole period; this reading treats it as the total."),
                    material=True, removable=True,
                ))
            except ValueError:
                pass

        # --- signer ----------------------------------------------------------
        signer_name, signer_email = extract_signer(prompt)
        sheet.add(Term(
            key="signer_name", label="Signer", value=signer_name or "[signer not identified]",
            provenance=STATED if signer_name else INVENTED,
            evidence=(f"you wrote: {signer_name!r}" if signer_name else
                      "no signer was named in your instruction."),
            material=True, removable=False,
        ))
        sheet.add(Term(
            key="signer_email", label="Signer email", value=signer_email or "[email not given]",
            provenance=STATED if signer_email else INVENTED,
            evidence=(f"you wrote: {signer_email!r}" if signer_email else
                      "no signer email was given. The envelope cannot be addressed without one."),
            material=True, removable=False,
        ))

        # --- the invented ones -----------------------------------------------
        for spec in INVENTED_DEFAULTS:
            sheet.add(Term(
                key=spec["key"], label=spec["label"], value=spec["value"],
                provenance=INVENTED, evidence=spec["evidence"],
                material=True, removable=spec.get("removable", True),
            ))

        return Plan(sheet=sheet, steps=self.tool_plan(), notes=notes,
                    security_events=security_events)

    # -- the tool plan the agent narrates -------------------------------------

    def tool_plan(self) -> List[ToolStep]:
        return [
            ToolStep("extract_terms", "Read the instruction and separate stated terms from gaps",
                     gated=False, material=False),
            ToolStep("select_template", "Choose the engagement-letter template", gated=False),
            ToolStep("classify_provenance",
                     "Tag every term stated / derived / invented", gated=False, material=True),
            ToolStep("docgen.GenerateDocumentBase64",
                     "Foxit Document Generation renders the draft PDF", gated=False),
            ToolStep("mcp.merge_pdf", "Merge the standard terms & conditions pages", gated=False),
            ToolStep("mcp.flatten_pdf", "Flatten the merged document", gated=False),
            ToolStep("mcp.extract_text",
                     "Read the rendered PDF back and verify the terms it actually contains",
                     gated=False, material=False),
            ToolStep("request_signature",
                     "BLOCKED until every invented term has been resolved by a human",
                     gated=True, material=True),
        ]
