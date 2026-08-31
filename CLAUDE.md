# CLAUDE.md — Austin Beefs operating manual

This file is loaded every run. It is the source of truth for how the automated
posting engine behaves. If any instruction here conflicts with improvisation,
this file wins.

## What this project is

Austin Beefs is an automated social account that posts loud, friendly DEBATE
content about hyperlocal Austin things and lets locals argue in the comments.
Each post puts an Austin matchup in the ring; the comment section is the ballot
box; Claude Code tallies the winner and starts the next fight. The goal is
organic growth of an Austin audience with zero ongoing ad spend.

Division of labor:
- BRAIN (Claude Code): generate matchups, render images, tally comments, decide
  next round, hold state. Everything intelligent.
- POSTING HAND (Blotato): publishes a finished image + caption on schedule, across
  all pages. Chosen over per-page schedulers because one integration fans out to
  5-10 pages. NOTE: Blotato is the posting hand ONLY — it does not read comments.
- LISTENING HAND (Facebook Graph API): pulls comments off a live post so the
  brain can count them. Blotato is NOT in this path — comment-reading is always Graph.

## File map

Authored (you curate — the model only reads these):
- `CLAUDE.md`          — this file. Rules + cycle + guardrails.
- `VOICE.md`           — brand voice, copy rules, caption anatomy, example lines.
- `TALLY.md`           — how to count comment votes. Read at the tally step.
- `reference_bank.json`— the debate material. Data, never invented at runtime.

Generated (the pipeline writes these — do not hand-edit unless fixing a break):
- `current_round.json` — the live post: {page_id, post_id, matchup, deadline}.
                         Read → overwritten each cycle. (page_id is stored so the Graph
                         comment-pull is unambiguous about which page's post it reads.)
- `canon.json`         — permanent record of past winners. Append-only. (Also = future website content.)
- `config.json`        — cadence, vote-window, page id, template mappings, Blotato target id.

Assets:
- `/templates/*.html`  — the five APPROVED card templates (vs, tier, hot_take, verdict, open_floor) with fill-in slots.

Scope note: this is a single-page project (Austin Beefs). Other pages you run are
separate projects/codebases — this one does not manage them and needs no multi-page machinery.

## The cadence

One post every 3 days. Voting window = the 3 days between posts. Each post does
DOUBLE DUTY: it reveals the last round's winner AND opens the next fight.

## The cycle (this ordering IS the clock — do not reorder)

Run every 3 days:

1.  READ STATE — load reference_bank, current_round, canon.
2.  PULL COMMENTS (Graph API) — using current_round.post_id, fetch all comments.
3.  TALLY (see TALLY.md) — produce winner + a loud, vague verdict line.
4.  PICK NEXT MATCHUP — select the next fight from reference_bank; mark it used;
    choose archetype (VS / tier / hot_take; a verdict is always included except Round 1).
5.  COMPUTE DEADLINE — deadline = now + 3 days, formatted (e.g. "MON 6PM").
    CODE owns this date. The model NEVER writes the deadline. See guardrail G3.
6.  GENERATE COPY (see VOICE.md) — caption = last verdict + new matchup hook +
    how-to-vote + computed deadline. In voice, from locked references only.
7.  RENDER IMAGE — fill the chosen /templates/*.html, screenshot to 1080x1080 PNG.
8.  POST (Blotato) — push image + caption to the page.
9.  CAPTURE NEW POST-ID — from Blotato's response, or query the page's recent
    posts via Graph immediately after and match. Store {page_id, post_id} in state.
    THIS is the most fragile link — if no id is captured, HALT AND ALERT (guardrail G6).
10. WRITE STATE — save new post-id/matchup/deadline to current_round; append last
    round's winner to canon; persist reference_bank used-flags.

## COLD START — Round 1 is special (READ THIS)

Round 1 has no prior round to reveal, so it has NO verdict half. The first debate
is fixed by the operator:

    ROUND 1 = "What's Austin's best breakfast taco place?"

Run it as an OPEN-FLOOR post, not a two-way VS:
- Use the hot_take/statement template layout, but the big text is the QUESTION,
  not a take: "AUSTIN'S BEST BREAKFAST TACO?"
- How-to-vote: "Name your spot in the comments." Deadline as normal.
- WHY open-floor for launch: everyone has a pick (max participation = max reach),
  and the crowd's nominations SEED future VS matchups. Harvest the top-named
  spots from Round 1's comments into reference_bank for later head-to-heads.
- Round 2 onward returns to the normal cycle (verdict of R1 + new matchup).

## Hard rules / guardrails (non-negotiable)

- G1  LOCKED BANK. Matchups, tier items, and takes come ONLY from
      reference_bank.json. Never free-associate Austin references at runtime —
      that is how the account drifts into generic slop and dies. If the bank is
      exhausted, HALT and ask the operator to refill it. Do not improvise.
- G1a STATUS TAGS ARE LAW. Every bank entry has a `status` tag.
      status:closed → NEVER post it. status:unconfirmed → do NOT post without
      verifying the place is open TODAY. Only status:confirmed_open posts freely.
      (Enforces G4/G5. The research caught real closures — Valentina's is closed,
      P. Terry's Cap Plaza is gone; the tags carry those corrections.)
- G1b RECURRENCE TAGS SET PRIORITY. Prefer recurrence:evidenced entries for
      early rounds — those are fights locals demonstrably have. recurrence:
      plausible_unverified entries ARE usable, but treat their first outing as a
      test of whether the fight is real; if a plausible matchup gets thin
      engagement, don't force it again. Harvest what lands.
- G1c CIVIC HOT-BUTTONS = SPARING + NEUTRAL. Items in civic_hot_buttons edge
      toward politics. Use rarely, frame as the genuine two-sided debate, never
      take a side, never state a contested fact as settled (G7).
- G1d NOSTALGIA = FACT, NOT DEBATE. closures_for_nostalgia entries state what's
      gone using the exact safe_phrasing given. Never turn a closure into a
      debate on a false premise, and never state a closure not listed there or
      confirmed in the bank.
- G9  COLOR = SIGNAL. The feed is readable at a glance because color encodes
      card type. This is a brand rule, not decoration — do not break it:
      * ORANGE field (vs, tier, hot_take, open_floor) = "a fight is LIVE, go vote."
      * CHARCOAL + GOLD (verdict) = "results are in." Verdict cards are NEVER orange.
      Each template already bakes in its colorway; the engine must not recolor a
      card in a way that crosses these two meanings. Within a verdict card, the
      one orange element is the bottom scoreboard, because that's the single
      "go vote on the NEXT fight" call — that's intentional, keep it.
- G2  LOUD VERDICT, VAGUE ARITHMETIC. Announce winners confidently but NEVER
      state exact vote counts. "Austin has spoken" — not "1,247 votes." A stated
      number invites a recount you'll lose. See TALLY.md.
- G3  CODE OWNS DATES. The deadline is computed from publish time by code and
      injected. The model writes voice, never the date. A wrong deadline is a
      factual error — the one kind of mistake that wounds credibility.
- G4  TASTE, NOT FACT. Provocation lives in OPINION (rankings, takes). Never
      provoke with a factual claim. "P. Terry's beats Whataburger" = fine.
      A wrong address, wrong hours, or a closed business stated as open = fatal.
- G5  VERIFY BUSINESS STATUS. Any spot flagged needs_verification in the bank
      must be confirmed still-open before it appears in a post. Do not matchup a
      place that may have closed.
- G6  FAIL LOUD. If comment-read returns nothing, or no post-id is captured, or
      the bank is empty — HALT and alert the operator. Never continue blindly
      into a next cycle on missing state.
- G7  FRIENDLY, NEVER CRUEL. Loud trash-talk is the brand; punching down,
      slurs, or genuine hostility is not. When unsure, dial toward playful.
- G8  REAL VOTES ONLY. Never fabricate comments, votes, or engagement. The
      votes are real people — that is the entire point.

## Build order (de-risk in this sequence)

1. Prove Graph comment-read (fetch + print comments off one post).
2. Prove render (fill one template, screenshot, eyeball the 1080 PNG).
3. Prove Blotato post + post-id capture IN ISOLATION (the fragile handoff, G6).
4. Prove tally (feed real messy comments to the tally step; check it's defensible).
5. Wire the full cycle WITH a human approval gate before each post.
6. Remove the gate on posting once trusted. Keep a longer human eye on
   GENERATION (reference-drift, G1) than on tally.

## The two things most likely to break this

- Reference drift (generation goes generic/wrong) → defense: G1 + human spot-check
  on generation that outlives the one on tally.
- The post-id handoff (step 9) → defense: prove in isolation (build step 3) + G6.
