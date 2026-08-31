# cycle.md — the one prompt Claude Code runs each cadence

You are running ONE Austin Beefs cycle. **CLAUDE.md is the law**; this file is the
runbook. Read CLAUDE.md, VOICE.md, TALLY.md, templates/README.md, config.json and
reference_bank.json before acting.

## Philosophy: THIN CONDUCTOR over proven scripts

Almost all of this cycle is deterministic Python that is already written and already
tested. **Do not reimplement any of it.** Import the helpers and call them. Your job
is to sequence them and to supply the three things a program genuinely cannot.

**The model does exactly three things. Nothing else.**

| # | The model's job | Where |
|---|---|---|
| M1 | **Select the next matchup** from `reference_bank.json`, obeying the tag rules | Step 5 |
| M2 | **Write the caption copy** per VOICE.md | Step 7 |
| M3 | *(optional)* Adjudicate the **bounded** ambiguous-comment sample `tally.py` already collects — one batched call, capped at `MAX_AMBIGUOUS_FOR_MODEL`, never per comment | Step 4 |

**The model NEVER:** counts votes (code does), writes a date (G3 — code does),
invents an Austin reference outside the bank (G1), states a vote count (G2),
fabricates engagement (G8), or decides whether to post (config does).

### The tested building blocks — import, do not rewrite

```python
import sys; sys.path.insert(0, "scripts")
import state, tally, render_card, post_card, harvest_open_floor
```

| Need | Call |
|---|---|
| state + cadence | `state.is_post_due()`, `read_current_round()`, `write_current_round()`, `read_canon()`, `append_to_canon()`, `next_round_number()`, `compute_deadline()` |
| comment read | Graph API exactly as `scripts/prove_graph_comments.py` does it (`FB_PAGE_TOKEN`; `filter=stream`, paginate) — **never Blotato** |
| steady-state count | `tally.tally(comments, contenders, alias_index)` + `tally.decide()` + `tally.verdict_line()` |
| Round-1 harvest | `harvest_open_floor` — **open-floor rounds only** |
| render | `render_card.render_card(archetype, slots, out_path)` |
| post + capture | `post_card.post_card(png, caption, page_id, blotato_account_id)` |

---

## The runbook — this ordering IS the clock. Do not reorder.

### 0. RECONCILE BEFORE ANYTHING ELSE
Read `queue/PENDING_POST.json` (Step 8a writes it).
- **Absent** → normal, continue.
- **Present** → a previous run began an irreversible post and never finished
  recording it. **HALT AND ALERT (G6).** A post may be live on the Page with no
  `fb_post_id` recorded. Do not post anything. Tell the operator to find the post,
  put its `<pageid>_<postid>` into `current_round.json`, and delete the breadcrumb.
  **Never** clear this file automatically — that is the one guard against posting a
  duplicate every day forever.

### 1. READ STATE
`state.is_post_due()` — if **False**, print why and **exit 0**. Nothing is due.
If it raises `StateError`, let it fail loudly: corrupt state is not a cold start.
Load `config.json` (`auto_publish`, `hold_verdict_gate`, `cadence_days`, `page_id`,
`blotato_fb_account_id`, `template_map`), `reference_bank.json`, `state.read_canon()`.
`round_no = state.next_round_number()`.

### 2. COLD START?
`state.read_current_round()` returns `None` **only** when the file is genuinely absent.
- **None → ROUND 1.** Skip Steps 3–4 entirely (nothing to tally). The post is the
  FIXED launch from `reference_bank.round_1_launch`: archetype `open_floor`, the
  breakfast-taco question, `no_verdict_half: true`. Do not invent a Round 1.
- **Otherwise** → normal cycle, continue to Step 3.

### 3. PULL COMMENTS (code)
Read comments on `current_round["fb_post_id"]` via Graph. If the read **errors**,
HALT (G6). Zero comments is *not* an error — it is a thin-turnout signal for Step 4.

### 4. TALLY or HARVEST (code)
- If the last round's `archetype == "open_floor"` → run **`harvest_open_floor`**.
  It **PROPOSES** novel spots; write them to `reports/harvest_<round>.json`.
  **NEVER auto-apply harvested names to `reference_bank.json`** — a junk or
  misspelled entry propagates into every future matchup (G1). Human review only.
- Otherwise → `tally.tally(...)` → `tally.decide(...)` → `tally.verdict_line(...)`.
  Contenders come from `current_round["matchup"]`; aliases from the bank.
  Honour the outcomes as returned: `winner` / `too_close` / `thin_turnout`.
  **Never upgrade a thin or tied result into a decisive win** (TALLY.md, G8).
  M3 (optional) may adjudicate only the bounded `ambiguous_sample`.

Vote counts from `debug` are **for your eyes only** and must never reach the caption (G2).

### 5. SELECT THE NEXT MATCHUP — **M1, the model's job**
From `reference_bank.json` only (G1). Never free-associate a spot.
- `status: closed` → **never** (G1a).
- `status: unconfirmed` → only with same-day verification; otherwise skip it (G1a/G5).
- Prefer `recurrence: evidenced`, especially early (G1b).
- `civic_hot_buttons` → rare, two-sided, never take a side (G1c).
- `closures_for_nostalgia` → state as fact using the given `safe_phrasing`; never
  turn a closure into a debate (G1d).
- Taste only — never provoke with a factual claim (G4).
- Skip anything already `used: true`.
- **Bank exhausted → HALT** and ask the operator to refill. Do not improvise (G1).
Choose the archetype: `matchup` / `tier_list` / `hot_take`, plus `verdict` when a
result is being revealed (every round except Round 1).
Check `aliases.review_sensitive` — if a contender's alias is collision-prone, eyeball
the tally debug before trusting the verdict.
**Do not mark `used: true` yet.** That happens in Step 10, after the outcome is known.

### 6. COMPUTE THE DEADLINE (code — G3)
`state.compute_deadline(posted_at)` → `{"iso", "label"}`. Use `label` verbatim
("MON 6PM"). **The model never writes, reformats, or guesses this string.** A wrong
deadline is a factual error, the one kind that wounds credibility.

### 7. WRITE THE CAPTION — **M2, the model's job**
Per VOICE.md's caption anatomy, in order:
1. **Verdict** of the last round — loud, vague arithmetic (skip on Round 1).
2. **New matchup hook.**
3. **How to vote** — ask for the *argument*, not just the pick.
4. **Deadline** — the injected `label` from Step 6.

Loud but never mean (G7). No counts (G2). No facts as provocation (G4).
Respect the slot caps in templates/README.md: `CLAIM`/`QUESTION` ~46 chars/line,
`SCRAWL` <34 chars, tier items ~28 chars. **Contender names need no length capping**
— `vs.html` auto-fits them. Never end a slot with a lone emoji after `<br>`.

### 8. RENDER (code)
`render_card.render_card(archetype, slots, "queue/round_<N>.png")`.
It validates the PNG is really 1080×1080 and raises `RenderError` otherwise.
Colour law (G9): orange = a fight is live; charcoal+gold = results are in. Never
recolour across that boundary.

### 8a. DECIDE THE GATE, then write the breadcrumb
```
post_for_real = config.auto_publish is True
                and not (archetype == "verdict" and config.hold_verdict_gate is True)
```
- **`post_for_real` False → STAGE.** Write the PNG, the caption, and a *proposed*
  `current_round.json` into `queue/`. **Do not post. Do not write breadcrumb. Stop
  at Step 10-staged.**
- **`post_for_real` True →** write `queue/PENDING_POST.json` **BEFORE** calling
  `post_card()`: `{round, archetype, matchup, caption, png, started_at}`.
  This is the only record that survives a crash between "post went live" and
  "state written". Step 0 refuses to run again while it exists.

### 9. POST + CAPTURE (code)
`post_card.post_card(png, caption, page_id, blotato_account_id)`
→ `{fb_post_id, blotato_submission_id, blotato_media_url, public_url}`.
The FB post-id capture is deterministic (poll Blotato's status endpoint, parse
`publicUrl`, validate `^\d+_\d+$`). No id → it halts. **Never invent or guess an id (G6).**

### 10. WRITE STATE — **strict order, most-important first**
**If posted:**
1. `state.write_current_round({round, archetype, matchup, fb_post_id,
   blotato_submission_id, deadline_iso, posted_at, page_id})` — **FIRST**. This is
   the operating state; losing it breaks the next cycle.
2. `state.append_to_canon({round: <previous>, matchup, winner, verdict_line, date})`
   — second. Archive, not operating state. `winner: None` is correct and honest for a
   `too_close` / `thin_turnout` round.
3. Mark the chosen bank item `used: true`; save the harvest proposal to `reports/`.
4. Move the PNG `queue/ → published/`.
5. **Delete `queue/PENDING_POST.json` LAST** — only once everything above succeeded.

**If staged:** write the proposed state into `queue/` only. No `posted_at`, no canon
append, no `used: true` — none of it happened yet. The PR merge promotes it.

---

## Hard stops (HALT AND ALERT — never continue blind)
- `queue/PENDING_POST.json` exists at Step 0 (unreconciled post).
- Corrupt `current_round.json` / `canon.json` — corrupt is **not** cold start.
- Graph comment-read errors.
- Bank exhausted, or every candidate is `closed` / `unconfirmed` (G1/G1a/G5).
- No `fb_post_id` captured after posting (G6).
- Render produced a PNG that is not 1080×1080.

## Never
- Never post a `status: closed` or unverified business (G1a/G4/G5).
- Never state a vote count (G2). Never fabricate comments or engagement (G8).
- Never write the deadline yourself (G3).
- Never auto-apply harvested spots to the bank — propose for human review (G1).
- Never clear `PENDING_POST.json` automatically.
