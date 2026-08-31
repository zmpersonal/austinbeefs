#!/usr/bin/env python3
"""
TALLY - turn a post's comments into a winner (cycle step 3).

Implements TALLY.md. Pure local logic: no network, no API, no posting. Reads
comments from a FILE so it is testable without a live post.

    python3 scripts/tally.py --comments tests/clear_winner.json \
                             --matchup  tests/matchup_tacos.json

    # or name the contenders inline:
    python3 scripts/tally.py --comments tests/messy.json \
                             --contenders "Veracruz All Natural" "Pueblo Viejo"

WHAT IT DOES
  1. Resolves each comment to a contender using reference_bank.json's alias map
     (the bank is the ONLY source of nicknames - guardrail G1; this script never
     invents an alias).
  2. Counts by DISTINCT COMMENTER, not raw mentions, so one loud account cannot
     swing a round (TALLY.md, brigading).
  3. Logs write-ins (bank spots named but not in this matchup) as future matchup
     material.
  4. Decides: winner / too_close / thin_turnout - and NEVER fabricates a
     decisive win on thin signal.

GUARDRAIL G2 - the verdict line is loud and VAGUE ON ARITHMETIC. Vote counts
live only in the "debug" block and MUST NEVER reach a caption.

SCOPE - STEADY-STATE MATCHUPS ONLY:
    This tallies a known two-(or more)-contender matchup. It CANNOT harvest
    novel nominations from an open-floor post (Round 1's "name your spot"),
    because alias matching can only recognise names ALREADY in
    reference_bank.json. A spot nobody has added to the bank is invisible here.
    That is by design - do not bolt open-floor handling onto this file. Round 1
    harvesting is a SEPARATE tool.

COST / ARCHITECTURE RULE (non-negotiable):
    "Counting is code. The model is touched at most once per cycle (verdict
     line + optional bounded ambiguity batch). Never one model call per
     comment. Cost must not scale with comment volume."

    Every vote in this file is resolved by deterministic alias matching in
    plain Python. There is NO per-comment model call and no network call of
    any kind - this module imports no HTTP client at all. Tallying 20,000
    comments costs exactly what tallying 20 costs.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
BANK_PATH = os.path.join(HERE, os.pardir, "reference_bank.json")

# ---------------------------------------------------------------------------
# TUNABLE THRESHOLDS - the honest-verdict dials (TALLY.md edge cases).
# ---------------------------------------------------------------------------
# TURNOUT and CLOSENESS are separate questions - do not conflate them.
# Early rounds are low-turnout by nature; a decisive result on small numbers is
# still a real result, and refusing to crown it stalls the flywheel.
MIN_TOTAL_VOTES_FOR_VERDICT = 4   # below this there is no credible signal at all
WINNER_MARGIN = 0.60              # top contender needs this SHARE of votes cast

# Hard cap on comments that may EVER be shown to a model. If more than this
# many are ambiguous, the extras stay uncounted rather than escalating cost -
# an uncounted comment is cheap, a per-comment model loop is not.
MAX_AMBIGUOUS_FOR_MODEL = 10
# ---------------------------------------------------------------------------

# Phrases that signal a stated preference when a comment names BOTH contenders.
PREF_OVER = re.compile(r"\bover\b")          # "A over B"      -> A
PREF_BUT = re.compile(r"\b(but|although|though|however)\b")   # "both, but B" -> B
PREF_VERB = re.compile(r"\b(edges|edge|wins|win|takes it|beats|better|best|"
                       r"by a mile|all day|hands down|no contest)\b")


# ===========================================================================
# SECTION 1 - PURE CODE. ALL COUNTING HAPPENS HERE.
#   No model call. No network call. No API key. Deterministic and O(comments).
#   normalize / find_mentions / resolve_preference / tally / decide
# ===========================================================================


def normalize(text):
    """Lowercase and strip punctuation so "P. Terry's" and "p terrys" match."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def load_bank(path=BANK_PATH):
    with open(path) as f:
        return json.load(f)


def build_alias_index(bank, contenders):
    """canonical name -> [normalized alias, ...], for every bank entry.

    The canonical name is always its own alias. Names on the bank's
    `low_confidence_do_not_assume` list are NEVER used as match keys."""
    amap = bank.get("aliases", {}).get("map", {})
    do_not_assume = {normalize(x) for x in
                     bank.get("aliases", {}).get("low_confidence_do_not_assume", [])}

    index = {}
    for canonical, aliases in amap.items():
        forms = {normalize(canonical)} | {normalize(a) for a in aliases}
        forms = {f for f in forms if f and f not in do_not_assume}
        if forms:
            index[canonical] = sorted(forms, key=len, reverse=True)

    # A contender might not be in the alias map at all; still match its own name.
    for c in contenders:
        if c not in index:
            index[c] = [normalize(c)]
    return index


def find_mentions(norm_text, alias_index):
    """-> {canonical: earliest match position}. Word-boundary matched so "35"
    does not fire inside "1350" and "vt" does not fire inside "vtx"."""
    hits = {}
    for canonical, forms in alias_index.items():
        for form in forms:
            m = re.search(r"\b" + re.escape(form) + r"\b", norm_text)
            if m:
                hits[canonical] = min(hits.get(canonical, m.start()), m.start())
                break
    return hits


def resolve_preference(norm_text, mentioned):
    """A comment naming 2+ contenders: return the stated preference, or None.

    TALLY.md: "both, but Veracruz edges it" -> count the stated preference.
    No clear preference -> None (ignored, never guessed)."""
    ordered = sorted(mentioned.items(), key=lambda kv: kv[1])  # by position

    m = PREF_OVER.search(norm_text)
    if m:  # "A over B" -> the one BEFORE "over"
        before = [c for c, pos in ordered if pos < m.start()]
        if before:
            return before[-1]

    m = PREF_BUT.search(norm_text)
    if m:  # "both, but B ..." -> the one AFTER the pivot
        after = [c for c, pos in ordered if pos > m.start()]
        if after:
            return after[0]

    m = PREF_VERB.search(norm_text)
    if m:  # "B edges it" -> nearest contender BEFORE the verb
        before = [c for c, pos in ordered if pos < m.start()]
        if before:
            return before[-1]

    return None


def commenter_key(comment, idx):
    """Identity for distinct-commenter weighting. Graph usually nulls `from`,
    so fall back to a per-comment key and report that we could not dedupe."""
    frm = comment.get("from") or {}
    return frm.get("id") or frm.get("name") or f"anon:{comment.get('id', idx)}"


def load_comments(path):
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict):          # tolerate raw Graph shape {"data": [...]}
        data = data.get("data", [])
    if not isinstance(data, list):
        sys.exit(f"{path}: expected a list of comments (or {{'data': [...]}}).")
    return data


def tally(comments, contenders, alias_index):
    votes = defaultdict(set)            # canonical -> {commenter keys}
    raw_mentions = defaultdict(int)     # canonical -> raw comment count
    write_ins = defaultdict(set)
    write_in_raw = defaultdict(int)
    ignored = 0
    ambiguous = 0
    ambiguous_sample = []      # capped at MAX_AMBIGUOUS_FOR_MODEL
    identified_commenters = 0

    contender_set = set(contenders)

    for i, c in enumerate(comments):
        norm = normalize(c.get("message", ""))
        if not norm:
            ignored += 1
            continue
        who = commenter_key(c, i)
        if (c.get("from") or {}).get("id") or (c.get("from") or {}).get("name"):
            identified_commenters += 1

        mentioned = find_mentions(norm, alias_index)
        in_play = {k: v for k, v in mentioned.items() if k in contender_set}
        outside = {k: v for k, v in mentioned.items() if k not in contender_set}

        # write-ins: bank spots named that are not in this matchup
        for name in outside:
            write_ins[name].add(who)
            write_in_raw[name] += 1

        if not in_play:
            ignored += 1
            continue

        if len(in_play) == 1:
            pick = next(iter(in_play))
        else:
            pick = resolve_preference(norm, in_play)
            if pick is None:
                ambiguous += 1
                # Collect a BOUNDED sample only. Past the cap we stop
                # collecting entirely - extras stay uncounted by design.
                if len(ambiguous_sample) < MAX_AMBIGUOUS_FOR_MODEL:
                    ambiguous_sample.append({
                        "id": c.get("id"),
                        "message": c.get("message", ""),
                        "mentions": sorted(in_play),
                    })
                continue

        votes[pick].add(who)
        raw_mentions[pick] += 1

    return (votes, raw_mentions, write_ins, write_in_raw, ignored, ambiguous,
            ambiguous_sample, identified_commenters)


def decide(votes, contenders):
    """-> (outcome, winner_or_None, ranked). Decision uses DISTINCT commenters."""
    ranked = sorted(((c, len(votes.get(c, ()))) for c in contenders),
                    key=lambda kv: kv[1], reverse=True)
    total = sum(n for _, n in ranked)

    # (1) TURNOUT gate: is there any credible signal at all?
    if total < MIN_TOTAL_VOTES_FOR_VERDICT:
        return "thin_turnout", None, ranked

    # (2) CLOSENESS gate: did anyone actually pull clear? A strong majority
    #     wins even on low turnout - 2-of-3 is a decisive result, not a fluke.
    top, top_n = ranked[0]
    if top_n / total >= WINNER_MARGIN:
        return "winner", top, ranked

    return "too_close", None, ranked


# ===========================================================================
# SECTION 2 - THE MODEL BOUNDARY. Nothing above this line touches a model.
#
#   (a) verdict_line() below is TEMPLATED - it makes NO model call at all.
#       If you ever want the model to write it with more flair, that is ONE
#       call per CYCLE, taking only (outcome, winner) - never the comments.
#
#   (b) Optional bounded adjudication: `ambiguous_sample` in the output holds
#       at most MAX_AMBIGUOUS_FOR_MODEL comments. If you adjudicate them, send
#       that whole list in a SINGLE batched call. Never loop per comment, and
#       never send the full comment set. Over the cap, they stay uncounted.
#
#   Neither path is wired up here: this script is offline by construction.
# ===========================================================================


def verdict_line(outcome, winner):
    """G2: loud, confident, VAGUE on arithmetic. Never a number."""
    if outcome == "winner":
        return f"Austin has spoken — {winner} takes it."
    if outcome == "too_close":
        return "Austin couldn't decide — this one goes to overtime."
    return "Too quiet out there. RUN IT BACK 👇"


def main():
    ap = argparse.ArgumentParser(description="Tally Austin Beefs comment votes.")
    ap.add_argument("--comments", required=True, help="path to comments JSON")
    ap.add_argument("--matchup", help="JSON file with {\"a\": ..., \"b\": ...}")
    ap.add_argument("--contenders", nargs="+", help="canonical contender names")
    ap.add_argument("--bank", default=BANK_PATH)
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    if args.contenders:
        contenders = args.contenders
    elif args.matchup:
        with open(args.matchup) as f:
            m = json.load(f)
        contenders = [m[k] for k in ("a", "b", "c", "d") if m.get(k)]
    else:
        sys.exit("Give --matchup or --contenders.")

    bank = load_bank(args.bank)
    alias_index = build_alias_index(bank, contenders)
    comments = load_comments(args.comments)

    (votes, raw_mentions, write_ins, write_in_raw, ignored, ambiguous,
     ambiguous_sample, identified) = tally(comments, contenders, alias_index)

    outcome, winner, ranked = decide(votes, contenders)
    total_votes = sum(n for _, n in ranked)

    result = {
        "outcome": outcome,
        "winner": winner,
        "verdict_line": verdict_line(outcome, winner),
        "matchup": contenders,
        "write_ins": [
            {"name": n, "distinct_commenters": len(w), "raw_mentions": write_in_raw[n]}
            for n, w in sorted(write_ins.items(),
                               key=lambda kv: len(kv[1]), reverse=True)
        ],
        "turnout": {
            "total_comments": len(comments),
            "clear_votes": total_votes,
            "thin": outcome == "thin_turnout",
            "note": ("Thin turnout - not enough clear signal to crown anyone."
                     if outcome == "thin_turnout" else
                     "Too close to call honestly." if outcome == "too_close" else
                     "Enough clear signal for a verdict."),
        },
        "ambiguous_sample": {
            "_note": "OPTIONAL, BOUNDED. At most MAX_AMBIGUOUS_FOR_MODEL items. "
                     "If adjudicated by a model, send this list in ONE batched "
                     "call - never one call per comment.",
            "cap": MAX_AMBIGUOUS_FOR_MODEL,
            "total_ambiguous": ambiguous,
            "uncounted_over_cap": max(0, ambiguous - MAX_AMBIGUOUS_FOR_MODEL),
            "items": ambiguous_sample,
        },
        "debug": {
            "_WARNING": "DEBUG ONLY. These numbers must NEVER appear in a caption "
                        "or verdict line (G2 - a stated count invites a recount).",
            "distinct_commenter_votes": {c: n for c, n in ranked},
            "raw_comment_mentions": dict(raw_mentions),
            "top_share": round(ranked[0][1] / total_votes, 3) if total_votes else 0.0,
            "margin": (ranked[0][1] - ranked[1][1]) if len(ranked) > 1 else ranked[0][1],
            "ignored_no_clear_vote": ignored,
            "ambiguous_multi_mention": ambiguous,
            "commenters_with_names": identified,
            "dedupe_possible": identified > 0,
            "model_calls_made": 0,
            "cost_scales_with_comment_volume": False,
            "thresholds": {
                "MIN_TOTAL_VOTES_FOR_VERDICT": MIN_TOTAL_VOTES_FOR_VERDICT,
                "WINNER_MARGIN": WINNER_MARGIN,
            },
        },
    }

    if not args.json_only:
        print("=" * 68)
        print(f"MATCHUP: {' vs '.join(contenders)}")
        print(f"COMMENTS READ: {len(comments)}")
        print("-" * 68)
        print(f"OUTCOME:  {outcome.upper()}")
        print(f"VERDICT:  {result['verdict_line']}")
        print("            ^ this is the only line that may be published (G2)")
        if result["write_ins"]:
            print("-" * 68)
            print("WRITE-INS (harvest into reference_bank):")
            for w in result["write_ins"]:
                print(f"  - {w['name']}  ({w['distinct_commenters']} commenter(s))")
        print("-" * 68)
        print("DEBUG (never publish):")
        for c, n in ranked:
            print(f"  {c}: {n}")
        print(f"  ignored: {ignored} | ambiguous: {ambiguous}")
        print("=" * 68)
        print()

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
