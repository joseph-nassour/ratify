"""Verify docs/index.html renders correctly in a real browser.

Run from the repository root:  python scripts/verify_frontdoor.py

Checks, in both the unfilled (submission-pending) and filled states:
  * no console errors
  * no third-party network requests before the video is played
  * no horizontal overflow at 1280 / 768 / 390 px
  * the placeholder / degradation behaviour is what the comment claims
  * screenshots for a human to look at
"""
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]  # repo root; this file lives in scripts/
SRC = ROOT / "docs" / "index.html"
OUT = ROOT / "build" / "frontdoor"
OUT.mkdir(parents=True, exist_ok=True)

#: The three values that arrive from outside the codebase, in the one RATIFY block.
#: Amended 2026-08-31: this script used to assume all three were still empty, which
#: stopped being true the moment two of them were filled in — so it started reporting
#: the *success* as three failures. Both states are now synthesised from whatever the
#: source currently holds, so the check keeps working at every stage of the hand-off.
KEYS = ("youtubeId", "liveAppUrl", "repoUrl")
PLACEHOLDERS = {
    "youtubeId": "aaaaaaaaaaa",
    "liveAppUrl": "https://ratify.example.onrender.com",
    "repoUrl": "https://github.com/example/ratify",
}

html = SRC.read_text(encoding="utf-8")


def _assign(src: str, key: str, value: str) -> str:
    """Rewrite one `key: "..."` assignment, insisting it appears exactly once."""
    pattern = re.compile(rf'({re.escape(key)}:\s*)"[^"]*"')
    out, n = pattern.subn(lambda m: f'{m.group(1)}"{value}"', src)
    if n != 1:
        raise SystemExit(
            f"{key!r} appears {n} times in docs/index.html, expected exactly 1 — the "
            f"RATIFY block has changed shape, and claude/submission-checklist.md tells "
            f"a human to search for these exact names."
        )
    return out


def _current(key: str) -> str:
    m = re.search(rf'{re.escape(key)}:\s*"([^"]*)"', html)
    return m.group(1) if m else ""


print("current RATIFY values:")
for k in KEYS:
    v = _current(k)
    print(f"  {k:<11} {v or '(empty — still pending)'}")

# Both states are synthesised, so neither depends on how far the hand-off has got.
EMPTIED = OUT / "index-unfilled.html"
FILLED = OUT / "index-filled.html"
emptied = html
filled = html
for key in KEYS:
    emptied = _assign(emptied, key, "")
    filled = _assign(filled, key, _current(key) or PLACEHOLDERS[key])
EMPTIED.write_text(emptied, encoding="utf-8")
FILLED.write_text(filled, encoding="utf-8")

failures = []
WIDTHS = [(1280, 900), (768, 1024), (390, 844)]


def check(page, label, expect_iframe):
    for w, h in WIDTHS:
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(150)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        if overflow > 1:
            failures.append(f"{label} @{w}px: horizontal overflow of {overflow}px")
        # any element wider than the viewport that is not inside a designated scroller
        bad = page.evaluate(
            """() => [...document.querySelectorAll('body *')]
                 .filter(el => !el.closest('.scroll') && !el.closest('pre')
                            && el.getBoundingClientRect().right > window.innerWidth + 1)
                 .map(el => el.tagName + '.' + el.className).slice(0, 5)"""
        )
        if bad:
            failures.append(f"{label} @{w}px: elements past the right edge: {bad}")

    has_iframe = page.locator("#video-slot iframe").count() == 1
    if has_iframe != expect_iframe:
        failures.append(f"{label}: iframe present={has_iframe}, expected {expect_iframe}")

    app = page.locator("#link-app")
    disabled = app.get_attribute("aria-disabled") == "true"
    if disabled == expect_iframe:  # filled => enabled, unfilled => disabled
        failures.append(f"{label}: 'Open the live app' aria-disabled={disabled}, unexpected")


with sync_playwright() as p:
    browser = p.chromium.launch()
    for label, path, expect_iframe in (
        ("unfilled", EMPTIED, False),
        ("filled", FILLED, True),
    ):
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors, requests = [], []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("request", lambda r: requests.append(r.url))
        page.goto(path.as_uri(), wait_until="networkidle")

        check(page, label, expect_iframe)
        if errors:
            failures.append(f"{label}: console errors {errors}")
        external = [u for u in requests if not u.startswith("file://")]
        if label == "unfilled" and external:
            failures.append(f"{label}: made third-party requests {external}")

        if label == "unfilled":
            page.set_viewport_size({"width": 1280, "height": 900})
            page.screenshot(path=str(OUT / "hero-1280.png"))
            page.locator("#boundary").scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            page.screenshot(path=str(OUT / "boundary-1280.png"))
            page.locator("#attacks").scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            page.screenshot(path=str(OUT / "attacks-1280.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(200)
            page.screenshot(path=str(OUT / "hero-390.png"))
            ctx2 = browser.new_context(
                viewport={"width": 1280, "height": 900}, color_scheme="dark"
            )
            pd = ctx2.new_page()
            pd.goto(path.as_uri(), wait_until="networkidle")
            pd.screenshot(path=str(OUT / "hero-1280-dark.png"))
            ctx2.close()
        ctx.close()
    browser.close()

# The three names must stay greppable, because the hand-off document tells a human to
# search for them. Their *values* are none of this check's business.
for key in KEYS:
    if len(re.findall(rf'{re.escape(key)}:\s*"[^"]*"', html)) != 1:
        failures.append(f"{key} is not a single findable assignment in docs/index.html")

still_pending = [k for k in KEYS if not _current(k)]
if still_pending:
    print("\nstill pending before submission:", ", ".join(still_pending))

print("screenshots:", ", ".join(sorted(f.name for f in OUT.glob("*.png"))))
if failures:
    print("\nFAILURES:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nAll front-door checks passed.")
