# Austin Beefs — Build Guide

How to get this running autonomously on a cadence you can change anytime.
Claude Code + Blotato (posting) + Facebook Graph API (comment-reading) on GitHub Actions.

This is NOT a generic social-engine guide. Austin Beefs is a debate-and-tally
loop: comments are the ballot box, Claude Code counts them to pick a winner, and
the winner becomes the next post. That shape drives every choice below.

---

## The shape of the machine (read once)

- **Brain:** Claude Code runs one cycle every few days — reads last post's comments,
  tallies the winner, generates the next card, renders it, posts it, records state.
- **Posting hand:** Blotato (one integration, publishes the card).
- **Listening hand:** Facebook Graph API, directly (pulls comments for the tally).
  Blotato does NOT read comments in this project.
- **Assets that make it good, all already built:** CLAUDE.md (rules), VOICE.md,
  TALLY.md, reference_bank.json (research-verified material), templates/ (the five
  1080x1080 card designs). The cycle reads all of these every run.

One workflow, not four. Because you post every 2-3 days (not many times daily),
the whole thing is a single job that runs the full Step 1-10 sequence from CLAUDE.md.

---

## 0. What you must supply

| Thing | Where it goes | Note |
|---|---|---|
| Blotato PAID plan + API key | GitHub secret `BLOTATO_API_KEY` | API is paid-only; a trial can't drive this. Keep any trailing `=` in the key. |
| Blotato Facebook accountId | `config.json` → blotato_fb_account_id | Blotato Settings → Copy Account ID. |
| FB Page access token (pages_read_engagement) | GitHub secret `FB_PAGE_TOKEN` | This is the piece generic guides omit. It's how the tally reads comments. |
| FB Page numeric ID | GitHub secret `FB_PAGE_ID` + `config.json` | Your page. |
| Anthropic API key | GitHub secret `ANTHROPIC_API_KEY` | Meters per run; the cycle is cheap at this cadence. |
| A PRIVATE GitHub repo | — | Keeps the bank and state off the public web. |

---

## 1. PHASE 0 — prove the two load-bearing links FIRST

Do this before building any automation. If these two don't work against your real
accounts, nothing downstream matters — and you want to find that out today, not in
week three. Both scripts use only the Python standard library.

### 1a. Prove comment-reading (Graph API)

```bash
export FB_PAGE_TOKEN="EAAB...your page token..."
export FB_PAGE_ID="1234567890"
python3 scripts/prove_graph_comments.py
```

Success = it prints your latest post and lists the commenters + their text. That's
exactly what the tally ingests. If it errors, it's almost always: not a PAGE token,
missing `pages_read_engagement`, or an expired token. Fix that before moving on.
(One-time token setup is documented at the top of the script.)

### 1b. Prove posting + FB-post-id capture (Blotato → Graph)

```bash
export BLOTATO_API_KEY="...."
export BLOTATO_FB_ACCOUNT_ID="...."
export FB_PAGE_TOKEN="...."   # reused, for id recovery
export FB_PAGE_ID="...."
python3 scripts/prove_blotato_post.py
```

This posts a tiny throwaway test, then recovers the native Facebook post-id — the
bridge that lets the NEXT cycle pull THIS post's comments. It tries two strategies
and tells you which works; whichever succeeds is the one production uses. Delete the
test post afterward.

> Honesty note: the script flags that Blotato's exact REST paths/field names must be
> confirmed at help.blotato.com/api — I did not verify them blind. The Graph-based
> recovery (strategy B) is stable and will work regardless.

**Do not proceed past Phase 0 until both scripts succeed.**

---

## 2. Repo structure

You already have most of this. Add the plumbing files (this guide, scripts, workflow,
config, prompt). Final layout:

```
austin-beefs/
├── CLAUDE.md               # rules + Step 1-10 cycle + guardrails  (built)
├── VOICE.md                # voice + verdict copy + color rule     (built)
├── TALLY.md                # comment-counting rules                (built)
├── reference_bank.json     # research-verified debate material     (built)
├── templates/              # 5 card designs + README               (built)
├── config.json             # cadence + gates + ids                 (new)
├── .mcp.json               # Blotato MCP (or use REST fallback)    (new)
├── prompts/cycle.md        # the one orchestration prompt          (new)
├── scripts/                # the two Phase 0 proof scripts         (new)
├── .github/workflows/cycle.yml   # the single scheduled job        (new)
├── queue/                  # staged next-card awaiting approval     (create empty, .gitkeep)
├── published/              # archive of what posted                (create empty, .gitkeep)
├── reports/                # optional tally/failure logs           (create empty, .gitkeep)
├── current_round.json      # live-post state (pipeline writes it)  (auto)
└── canon.json              # permanent winner record (auto)        (auto)
```

Create the empty dirs with a `.gitkeep` file inside each or git won't track them:

```bash
mkdir -p queue published reports
touch queue/.gitkeep published/.gitkeep reports/.gitkeep
```

Note: your existing brand assets ARE the "brand/" folder generic guides tell you to
write from scratch. Don't rewrite them — they're tailored and research-backed.

---

## 3. Secrets

Repo → Settings → Secrets and variables → Actions → add:
- `ANTHROPIC_API_KEY`
- `BLOTATO_API_KEY`
- `FB_PAGE_TOKEN`   ← the one generic guides miss
- `FB_PAGE_ID`

Same page: set Workflow permissions to **Read and write**, and tick **Allow GitHub
Actions to create and approve pull requests** (needed for the review-phase PRs).

Token lifespan: exchange your page token for a LONG-LIVED one (~60 days) and set a
calendar reminder to refresh it, or the cycle will start failing when it expires.
A refresh step can be automated later; don't bother for launch.

---

## 4. Configure — and set your cadence (the part you asked about)

Everything about frequency lives in ONE number in `config.json`:

```json
{ "cadence_days": 3 }
```

- Every 3 days → `3`. Every other day → `2`. Weekly → `7`. That's the whole change.
- The workflow runs DAILY but exits in seconds unless it's been >= `cadence_days`
  since the last post. This is deliberately NOT a raw cron interval, because
  "every 3 days" via cron drifts across month boundaries; the day-gate never does.
- The card's voting deadline is auto-computed as publish + `cadence_days` (G3), so
  the voting window always matches the gap to the next post. Change the cadence and
  the deadlines follow automatically.

Also fill in `page_id` and `blotato_fb_account_id`, and set `auto_publish:false`
(the review gate — leave it false for ~2 weeks). Everything in config.json is
commented inline.

---

## 5. The single workflow

`.github/workflows/cycle.yml` does the whole thing: cadence-gate → install →
run `prompts/cycle.md` via Claude Code → (review phase) open a PR, or (autonomous)
commit state. It's one file; you rarely touch it. To change WHEN in the day it can
fire, edit its one `cron` line (default 22:00 UTC ≈ 17:00 Central). To change HOW
OFTEN, don't touch this file — use `cadence_days` in config.

The cron only controls the daily "is it due?" check. The day-gate controls actual
posting frequency.

---

## 6. The cycle prompt

`prompts/cycle.md` is the operational checklist Claude Code executes; CLAUDE.md is
the law it obeys. It walks Step 1-10: read state → (cold start? Round 1 taco launch)
→ Graph pull comments → tally (TALLY.md) → pick next matchup honoring the bank's
status/recurrence tags → compute deadline → generate copy (VOICE.md) → render the
template to PNG (waiting for fonts) → post via Blotato + capture FB post-id (or
stage for PR) → write state. The quality lives in the assets it reads, not in this
prompt — which is why we spent the effort on CLAUDE.md/VOICE.md/TALLY.md/the bank.

---

## 7. First run (manual, before trusting the cron)

```bash
# from the repo, with secrets set as env vars locally OR via gh:
gh workflow run cycle.yml       # Round 1: the breakfast-taco open-floor launch
```

Because `auto_publish:false`, this stages the Round 1 card and opens a PR instead of
posting. Open the PR, look at the rendered PNG and caption. If it doesn't look/sound
right, the fix is in VOICE.md or the template — not the workflow. Merge to approve
and let it post; close to discard.

Then wait `cadence_days`, let it run again (or `workflow_dispatch` to force it), and
check that it correctly: pulled Round 1's comments, tallied a defensible winner, and
staged a verdict + next matchup. That second run is the real test — it's the flywheel
turning.

---

## 8. Review phase → autonomous

For ~2 weeks, every cycle opens a PR:
- **Merge** → the staged card posts.
- **Close** → skip it.
- **Edit the caption file, then merge** → your wording ships. Every edit you keep
  making is a signal — fold recurring fixes back into VOICE.md before going auto.

To go autonomous:
1. Set `auto_publish: true` in config.json.
2. Leave `hold_verdict_gate: true` a while longer — it keeps VERDICT cards PR-gated
   even after the rest auto-post. A miscounted winner is the one error that erodes
   trust, so watch the tally longest. Flip it to false only once the tally has been
   reliably correct for weeks.

---

## 9. Costs & limits

- **GitHub Actions:** private repos get 2,000 free min/month. One ~5-min cycle every
  3 days ≈ 50 min/month. Trivial. (Even the daily gate-check that exits early is
  seconds.)
- **Anthropic:** metered per run; one cycle every few days is cheap. Watch the first
  couple runs to confirm.
- **Blotato:** publish 30 req/min, upload 10 req/min — you're nowhere near these at
  this cadence. Visual credits: N/A for us — we render our own PNGs, we don't use
  Blotato's image generation (that would break the brand's consistent look).
- **Facebook Graph:** comment reads are light. The real limit is TOKEN EXPIRY —
  keep the page token long-lived and refreshed.

---

## 10. What NOT to do

- Don't use Blotato to read comments — it's Instagram/Facebook-only and weaker than
  Graph for this. Comment-reading is Graph, always.
- Don't use Blotato's AI image generation — render the HTML templates instead, or the
  brand's recognizable look dies.
- Don't let unattended generation free-associate Austin references — it must pull from
  reference_bank.json (G1). Keep a human eye on GENERATION longer than on posting.
- Don't skip Phase 0. The two proof scripts are the difference between finding a
  broken link on day one vs. week three.

---

## Build order, one line

Phase 0 scripts pass → repo + secrets + config → manual Round 1 (PR, don't merge
blind) → review ~2 weeks → auto_publish:true (keep verdict gated) → adjust cadence
anytime via one number.
