#!/usr/bin/env python3
"""
RENDER CARD - fill a template's {{SLOTS}} and screenshot it to a 1080x1080 PNG.
This is cycle step 7, as a clean importable function.

    from render_card import render_card
    render_card("matchup", {...slots...}, "queue/round_14.png")

    # CLI:
    python3 scripts/render_card.py --archetype matchup \
        --slots '{"NAME_A": "Veracruz", ...}' --out /tmp/card.png
    python3 scripts/render_card.py --archetype verdict \
        --slots-file slots.json --out /tmp/verdict.png
    python3 scripts/render_card.py --archetype vs --list-slots   # what does it need?

RUNTIME NOTE - use the project venv:
    Playwright lives in ./venv (gitignored), not the system python3, so render
    calls must use ./venv/bin/python. Slot validation and --list-slots work
    under plain python3 because they never touch the browser.
        ./venv/bin/python scripts/render_card.py --archetype matchup ...
    The venv pins playwright==1.49.1 deliberately: newer releases dropped
    macOS 13 support ("Playwright does not support chromium on mac13").

HOW IT WORKS
  1. archetype -> template file, via config.json's template_map (never hardcoded).
  2. The REQUIRED slots are discovered by scanning the template for {{TOKENS}} -
     so this validator cannot drift out of sync with the templates. A missing
     slot OR an unknown/typo'd slot name is a LOUD failure, never a silent
     half-filled card.
  3. Renders per templates/README.md: headless Chromium, viewport 1080x1080,
     device_scale_factor=1, WAIT for document.fonts.ready, clip to 1080x1080.
  4. Validates the PNG on disk actually exists and is actually 1080x1080.

WHY THE VALIDATION IS LOUD (G6 spirit)
  The post-id handoff taught us the lesson: a pipeline that THINKS it produced
  an artifact but didn't is worse than one that stops. A zero-byte or
  wrong-size PNG must never reach the posting step - it would publish a broken
  card to a live audience. Every failure here raises RenderError.

FONTS - PRODUCTION CAVEAT
  The templates pull Anton / Archivo / Permanent Marker from the Google Fonts
  CDN via <link>. Waiting on document.fonts.ready makes that safe when the
  render machine has internet. For the unattended 3-day cron, templates/README.md
  recommends SELF-HOSTING the .woff2 files instead, so a slow or blocked CDN can
  never silently ship a fallback-font card. Not built here - flagged on purpose.
"""

import argparse
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
CONFIG_PATH = os.path.join(ROOT, "config.json")

CARD_W = 1080
CARD_H = 1080
FONT_SETTLE_MS = 300          # small grace period after fonts.ready (README)

TOKEN_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class RenderError(RuntimeError):
    """Anything that means 'you do not have a valid 1080x1080 card'."""


# ---------------------------------------------------------------------------
# Template resolution + slot validation (pure code, no browser needed)
# ---------------------------------------------------------------------------

def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return json.load(f)


def template_path_for(archetype, config=None):
    config = config or load_config()
    tmap = config.get("template_map", {})
    if archetype not in tmap:
        raise RenderError(
            f"Unknown archetype {archetype!r}. config.json template_map has: "
            f"{sorted(tmap)}")
    path = os.path.join(ROOT, tmap[archetype])
    if not os.path.isfile(path):
        raise RenderError(f"Template file missing for {archetype!r}: {path}")
    return path


def template_body(path):
    """Template HTML with its doc-comment stripped. The comment documents the
    slots, so leaving it in would make the token scan report tokens that the
    rendered card does not actually use."""
    with open(path, encoding="utf-8") as f:
        return HTML_COMMENT_RE.sub("", f.read())


def required_slots(archetype, config=None):
    """The authoritative slot list: whatever {{TOKENS}} the template really has."""
    return sorted(set(TOKEN_RE.findall(template_body(
        template_path_for(archetype, config)))))


def fill_template(body, slots, archetype):
    """Substitute every {{TOKEN}}. Fails loud on missing OR unknown slots."""
    needed = set(TOKEN_RE.findall(body))
    given = set(slots)

    missing = needed - given
    unknown = given - needed
    problems = []
    if missing:
        problems.append(f"MISSING slot(s) for {archetype!r}: {sorted(missing)}")
    if unknown:
        problems.append(
            f"UNKNOWN slot(s) for {archetype!r} (typo?): {sorted(unknown)}. "
            f"This template accepts: {sorted(needed)}")
    if problems:
        raise RenderError("\n  ".join(["Slot validation failed."] + problems))

    out = body
    for token, value in slots.items():
        out = out.replace("{{" + token + "}}", str(value))

    leftover = set(TOKEN_RE.findall(out))
    if leftover:                       # belt and braces
        raise RenderError(f"Unreplaced tokens remain after fill: {sorted(leftover)}")
    return out


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------

def png_dimensions(path):
    """(width, height) straight from the PNG IHDR header. Stdlib only - no
    Pillow dependency just to read 8 bytes."""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        raise RenderError(f"Not a valid PNG: {path}")
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def validate_png(path):
    if not os.path.isfile(path):
        raise RenderError(f"Render reported success but no file exists at {path}")
    size = os.path.getsize(path)
    if size == 0:
        raise RenderError(f"Rendered PNG is zero bytes: {path}")
    w, h = png_dimensions(path)
    if (w, h) != (CARD_W, CARD_H):
        raise RenderError(
            f"Rendered PNG is {w}x{h}, expected {CARD_W}x{CARD_H}: {path}")
    return {"path": path, "bytes": size, "width": w, "height": h}


# ---------------------------------------------------------------------------
# The render
# ---------------------------------------------------------------------------

def render_card(archetype, slots, out_path, config=None, keep_html=False):
    """Fill `archetype`'s template with `slots` and write a 1080x1080 PNG to
    `out_path`. Returns a dict describing the validated output.

    Raises RenderError on any failure - never returns a bad card."""
    # Validate slots BEFORE touching the browser: a typo'd slot name should
    # fail instantly and identically whether or not Playwright is installed.
    body = template_body(template_path_for(archetype, config))
    filled = fill_template(body, slots, archetype)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RenderError(
            "Playwright is not installed in this interpreter.\n"
            "  pip3 install playwright && python3 -m playwright install chromium"
        ) from e

    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    fd, tmp_html = tempfile.mkstemp(suffix=".html", prefix=f"card_{archetype}_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(filled)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                viewport={"width": CARD_W, "height": CARD_H},
                device_scale_factor=1,
            )
            page.goto(f"file://{tmp_html}", wait_until="networkidle")
            # THE critical gotcha: screenshotting before webfonts load silently
            # ships fallback fonts and destroys the brand (templates/README.md).
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(FONT_SETTLE_MS)
            # Templates that auto-fit text (vs.html) expose window.__fitNames.
            # It self-runs on fonts.ready; calling it again here removes any
            # race between font load and screenshot. Idempotent, and a no-op
            # for templates that do not define it.
            page.evaluate("window.__fitNames && window.__fitNames()")
            page.screenshot(
                path=out_path,
                clip={"x": 0, "y": 0, "width": CARD_W, "height": CARD_H},
            )
            browser.close()
    finally:
        if keep_html:
            print(f"  (filled html kept at {tmp_html})", file=sys.stderr)
        else:
            os.unlink(tmp_html)

    return validate_png(out_path)


def main():
    ap = argparse.ArgumentParser(description="Render an Austin Beefs card to PNG.")
    ap.add_argument("--archetype", required=True)
    ap.add_argument("--slots", help="inline JSON object of slot values")
    ap.add_argument("--slots-file", help="path to a JSON object of slot values")
    ap.add_argument("--out", help="output PNG path")
    ap.add_argument("--list-slots", action="store_true",
                    help="print the slots this template requires, then exit")
    ap.add_argument("--keep-html", action="store_true")
    args = ap.parse_args()

    try:
        if args.list_slots:
            for s in required_slots(args.archetype):
                print(s)
            return
        if not args.out:
            sys.exit("--out is required (or use --list-slots)")
        if args.slots_file:
            with open(args.slots_file) as f:
                slots = json.load(f)
        elif args.slots:
            slots = json.loads(args.slots)
        else:
            sys.exit("Give --slots or --slots-file (or --list-slots).")

        info = render_card(args.archetype, slots, args.out,
                           keep_html=args.keep_html)
        print(f"OK  {info['width']}x{info['height']}  "
              f"{info['bytes']:,} bytes  ->  {info['path']}")
    except RenderError as e:
        print(f"\nRENDER FAILED: {e}\n", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
