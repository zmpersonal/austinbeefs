# templates/ — how the render step works

Five standalone 1080×1080 HTML cards. The engine picks one by archetype, replaces the
`{{TOKENS}}`, opens it in a headless browser, and screenshots it to a 1080×1080 PNG
(cycle step 7). What you approved in the brand mockup IS these files — no redraw.

## Archetype → file

| archetype   | file             | when                                              |
|-------------|------------------|---------------------------------------------------|
| open_floor  | open_floor.html  | Round 1 (breakfast tacos) + any open nomination   |
| matchup     | vs.html          | the workhorse head-to-head                         |
| tier_list   | tier.html        | S–F ranking; rage by placement                    |
| hot_take    | hot_take.html    | one gloriously wrong take (TASTE only, G4)         |
| verdict     | verdict.html     | reveal last round's winner (LOUD, no counts, G2)  |

Each file's top comment block is the authoritative slot list for that template.

## Slot fill (all templates)

- `{{DEADLINE}}` is computed by code from publish time (guardrail G3) — the model never writes it.
- Slots may contain `<br>` to control line breaks. They must NOT contain raw `{` `}` or unescaped quotes.
- **vs `{{NAME_A}}` / `{{NAME_B}}`: no length limit — the card auto-fits.**
  `vs.html` measures each name after the fonts load and adjusts it to clear both the VS
  badge and the sloping edge of its own triangle. It **wraps at spaces first** (so
  "Vaquero Taquero" becomes two full-size lines) and only shrinks the font when a single
  unbreakable word is still too wide. Range 104px → 40px floor.
  Verified renders: `Veracruz` (1 line, barely shrunk), `Vaquero Taquero` /
  `Juan in a Million` (2 lines, near-full size), `Terry Black's` / `La Barbecue` (2 lines).
  Use `<br>` only to force a break you specifically want; you do not need it to fit.

  > The old "~11 chars/line" guidance in this file was **wrong** and caused a real
  > collision: the usable width beside the badge is only ~324px, which at 104px Anton is
  > about **6 characters** — `VERACRUZ` (8) already overlapped the badge. Auto-fit exists
  > because capping names to ~6 chars would make most real contenders unusable.

- **The other slots have no auto-fit — these caps are real:**
  - hot_take `{{CLAIM}}` / open_floor `{{QUESTION}}`: ~46 chars/line, up to 3 lines.
  - `{{SCRAWL}}`: under ~34 chars, or it wraps and orphans a trailing emoji.
  - tier `{{ITEMS_*}}`: one short line each (~28 chars).
- **Never end a slot with a lone trailing emoji after a `<br>`.** It orphans onto its own
  line. Applies to `{{SCRAWL}}` *and* `{{VOTE_VALUE}}` — `"Name your spot<br>👇"` put the
  👇 alone on a second footer line; `"Name your<br>spot 👇"` renders correctly.
- Generation (cycle step 6) should respect the caps above. It does **not** need to
  measure or cap contender names.

## Rendering (the two things that break it)

1. **Wait for fonts.** Screenshotting before webfonts load = fallback fonts = broken brand.
   Always wait for `document.fonts.ready` before the shot. This is also why auto-fit runs
   after `fonts.ready` — measuring in a fallback font produces the wrong size.
   `scripts/render_card.py` does all of this for you; call it rather than re-implementing.
   Python Playwright example:

   ```python
   from playwright.sync_api import sync_playwright
   import pathlib
   url = pathlib.Path("filled.html").resolve().as_uri()
   with sync_playwright() as p:
       b = p.chromium.launch()
       pg = b.new_page(viewport={"width":1080,"height":1080}, device_scale_factor=1)
       pg.goto(url, wait_until="networkidle")
       pg.evaluate("document.fonts.ready")
       pg.wait_for_timeout(300)
       pg.evaluate("window.__fitNames && window.__fitNames()")   # auto-fit (vs.html)
       pg.screenshot(path="out.png", clip={"x":0,"y":0,"width":1080,"height":1080})
       b.close()
   ```

2. **Native size.** viewport 1080×1080, `device_scale_factor=1`, clip to 1080×1080. The card
   fills the body at 1:1 — do NOT scale.

## Fonts: CDN vs local (production reliability)

These files load Anton / Archivo / Permanent Marker from Google Fonts via `<link>`. That works
when the render machine has internet AND you wait for `document.fonts.ready`. For an unattended
production pipeline, prefer **self-hosting the fonts** (download the .woff2 once, swap the
`<link>` for a local `@font-face`) so a slow/blocked CDN can never silently ship a fallback-font
post. CDN is fine for testing; local is safer for the 3-day-forever automation.

## Fonts used
- Display / scoreboard: **Anton**
- Body / labels: **Archivo** (700, 900)
- Friendly scrawl: **Permanent Marker**
