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
- **Keep it short.** Loud type overflows fast. Rough caps that fit cleanly:
  - vs `{{NAME_A/B}}`: ~11 chars/line, up to 2 lines.
  - hot_take `{{CLAIM}}` / open_floor `{{QUESTION}}`: ~46 chars/line, up to 3 lines.
  - `{{SCRAWL}}`: keep under ~34 chars or it wraps and orphans a trailing emoji (seen in the R1 test render). Put the emoji mid-line, not at the end.
  - tier `{{ITEMS_*}}`: one short line each (~28 chars).
- Generation (cycle step 6) should cap name/claim length to these before filling.

## Rendering (the two things that break it)

1. **Wait for fonts.** Screenshotting before webfonts load = fallback fonts = broken brand.
   Always wait for `document.fonts.ready` before the shot. Python Playwright example:

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
