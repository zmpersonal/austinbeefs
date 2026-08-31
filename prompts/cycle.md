# cycle.md — the one prompt Claude Code runs each cadence

You are running ONE Austin Beefs cycle. Follow CLAUDE.md exactly — it is the source
of truth for rules and the Step 1-10 sequence. This file is the operational checklist;
CLAUDE.md is the law. Read CLAUDE.md, VOICE.md, TALLY.md, reference_bank.json, and
config.json before doing anything.

## Do these in order (this ordering is the clock — do not reorder)

1. READ STATE. Load config.json, reference_bank.json, current_round.json (if it
   exists), canon.json. Note auto_publish and hold_verdict_gate.

2. COLD START? If current_round.json does not exist, this is Round 1. Skip to the
   GENERATE step and produce the FIXED launch post from reference_bank.round_1_launch
   (the breakfast-taco open_floor question). There is no verdict half on Round 1.
   Otherwise continue to TALLY.

3. PULL COMMENTS. Using current_round.json {page_id, post_id}, read the comments on
   that post via the Facebook Graph API (env: FB_PAGE_TOKEN, FB_PAGE_ID). This is the
   method proven in scripts/prove_graph_comments.py — reuse that approach. Do NOT use
   Blotato to read comments.

4. TALLY. Follow TALLY.md. Resolve nicknames via reference_bank.aliases, count real
   picks, ignore noise, harvest write-ins. Produce: winner, a loud + vague-arithmetic
   verdict line (G2, never a vote count), and any write-ins to append to the bank.
   Handle thin turnout / ties honestly (TALLY.md) — never fabricate a decisive winner.

5. PICK NEXT MATCHUP from reference_bank (guardrail G1). Obey the tags:
   - never status:closed; never status:unconfirmed without same-day verification (G1a);
   - prefer recurrence:evidenced, especially early (G1b);
   - taste only, never facts/closures/attribution (G4);
   - civic_hot_buttons sparingly + neutrally (G1c); nostalgia = fact not debate (G1d).
   Mark the chosen item used:true. Choose the archetype (matchup/tier/hot_take;
   verdict is always the reveal half except Round 1).

6. COMPUTE DEADLINE = now + config.cadence_days, formatted (e.g. "MON 6PM").
   CODE owns this (G3). Never let the model free-write the date.

7. GENERATE COPY (VOICE.md). Build the caption: last round's verdict (skip on R1) +
   the new matchup hook + how-to-vote (ask for the argument, not just the pick) + the
   computed deadline. Respect the length caps in templates/README.md so text fits.

8. RENDER IMAGE. Pick the template file from config.template_map for the archetype.
   Fill its {{SLOTS}} (see the template's header comment + templates/README.md).
   Render to a 1080x1080 PNG with headless Chromium, WAITING for document.fonts.ready
   before the screenshot (critical — see templates/README.md). Honor the color rule
   (G9): orange = a live fight, charcoal+gold = a verdict. Save the PNG under queue/.

9. POST or STAGE:
   - If auto_publish is TRUE (and, for verdict cards, hold_verdict_gate is FALSE):
     post the image + caption via Blotato (method proven in prove_blotato_post.py),
     then CAPTURE the native FB post-id (Strategy A from Blotato's response, else
     Strategy B: newest post via Graph right after). If no id is captured, HALT AND
     ALERT (G6) — do not continue blind. Move the card to published/.
   - If auto_publish is FALSE (review phase), OR it's a verdict card and
     hold_verdict_gate is TRUE: DO NOT post. Leave the card + caption + a
     proposed current_round.json in queue/ for the PR review. The workflow opens
     the PR; a human merges to approve.

10. WRITE STATE.
    - If posted: write current_round.json = {page_id, post_id, archetype, matchup,
      deadline, posted_at: <ISO date>}. Append the just-decided winner to canon.json.
    - If staged for review: write the PROPOSED current_round.json into queue/ (the
      merge promotes it). Do not set posted_at until it actually posts.
    - Persist reference_bank.json used-flags and harvested write-ins either way.

## Hard stops
- Bank exhausted → HALT, ask operator to refill (G1).
- Comment-read returns nothing when a post should have comments, or post-id capture
  fails → HALT AND ALERT (G6). Never guess.
- Anything status:closed or unverified → never post it (G1a/G4/G5).
