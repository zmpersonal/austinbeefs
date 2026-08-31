# TALLY.md — counting the comment votes

Read this at cycle step 3. The comment section is the ballot box; this is how the
brain reads the room. The output is a WINNER + a verdict line — never a spreadsheet.

## Inputs

- The raw comments pulled from the last post (via Graph API, cycle step 2).
- The matchup + its known aliases from reference_bank.json (this is why the bank
  stores nicknames — they're the key for resolving votes).

## How to count

1. RESOLVE NAMES & NICKNAMES. Map every mention to a canonical contender using
   the bank's aliases. "VT", "the trailer on Rosewood", "vaquero" → Vaquero
   Taquero. Misspellings count if intent is clear.
2. HANDLE MESS.
   - Multi-vote ("both, but Veracruz edges it") → count the stated preference.
   - No-pick rants, off-topic, tagging a friend with no opinion → ignore.
   - Vote for something NOT in the matchup (a write-in) → don't count toward this
     round, BUT log it: frequent write-ins are future matchup material. Harvest them.
3. WEIGH ROUGHLY, NOT PRECISELY. You're reading sentiment and rough counts to
   pick a clear winner. You are NOT producing an auditable number.

## Output (feeds the next post's verdict)

- `winner`: the canonical contender name.
- `verdict_line`: loud, confident, VAGUE on arithmetic (G2).
  GOOD: "Austin has spoken — Vaquero takes it."
  BAD:  "Vaquero won with 1,247 votes." (never state counts)
- `write_ins`: any frequently-named spots not in the matchup → append to
  reference_bank as candidates.

## Edge cases (build these branches — early posts WILL be thin)

- THIN TURNOUT (few or no clear votes): don't invent a winner. Use an honest,
  in-voice out: "Too quiet out there. RUN IT BACK 👇" and re-run the same
  matchup next cycle, OR crown on the little signal there is if defensible.
- GENUINE TIE / TOO CLOSE: "Austin couldn't decide — this one goes to overtime."
  Either extend or declare a friendly draw and move on. Never fake a decisive win.
- BRIGADING / SPAM (one account flooding, obvious coordination): weight toward
  distinct commenters, not raw mention count. Don't let one person swing it.
- OFF-THE-RAILS (comments went to a different argument entirely): count what
  you can; if there's genuinely no verdict, use the thin-turnout out.

## Hard rules (from CLAUDE.md, restated because they live here)

- G2: loud verdict, NEVER exact counts.
- G8: real votes only — never fabricate comments or engagement.
- Honesty over drama: a fake decisive winner is worse than an honest "too close."
