#!/usr/bin/env python3
"""
ROUND 1 OPEN-FLOOR HARVESTER - extract taco-spot nominations from an open-floor
post's comments, including spots that are NOT yet in reference_bank.json.

    # real run (needs ANTHROPIC_API_KEY):
    python3 scripts/harvest_open_floor.py --comments tests/open_floor_r1.json

    # logic test, no API call, no tokens spent:
    python3 scripts/harvest_open_floor.py --comments tests/open_floor_r1.json \
                                          --mock-response tests/mock_extraction.json

WHY THIS EXISTS (and why it is NOT part of tally.py)
    tally.py resolves votes by alias matching against the bank. That is correct
    for a steady-state matchup and it is deliberately incapable of finding a
    name nobody has added to the bank yet. Round 1 is the opposite problem:
    "name your spot" is an OPEN FLOOR whose entire purpose is surfacing spots we
    do not know about, to seed every future head-to-head. Alias matching cannot
    do open-ended entity extraction. This is the ONE justified model use in the
    pipeline.

COST RULE (same discipline as tally.py)
    ONE batched model call for the whole comment set - never one call per
    comment. If the set is too large for a single call it is chunked, with a
    hard cap of MAX_CHUNKS calls total. Round 1 runs ONCE, so this is a
    one-time cost measured in cents, not a per-comment tax. The verdict line is
    TEMPLATED and costs nothing.

!! THIS PROPOSES. IT DOES NOT WRITE. !!
    Nothing here edits reference_bank.json. Round 1 is the launch post and its
    output seeds the entire content pipeline, so every novel nomination must be
    HUMAN-REVIEWED before it enters the bank - a junk or misspelled entry would
    propagate into future matchups forever (G1: the bank is the locked source of
    truth). The script prints ready-to-review candidate entries; you approve and
    paste them in yourself.

    Extracted names are also UNVERIFIED BUSINESSES. Every candidate carries
    needs_verification:true - a spot must be confirmed open before it can ever
    appear on a card (G4/G5).
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from tally import normalize, load_bank, unsafe_as_auto_alias  # shared helpers

# ---------------------------------------------------------------------------
# TUNABLE
# ---------------------------------------------------------------------------
MIN_TOTAL_NOMINATIONS_FOR_VERDICT = 4   # below this: thin turnout, no crown
WINNER_LEAD = 1                          # top must beat 2nd by at least this

COMMENTS_PER_CHUNK = 400                 # one model call covers this many
MAX_CHUNKS = 5                           # hard ceiling on model calls, ever
MODEL = "claude-haiku-4-5-20251001"      # extraction, not reasoning - use cheap
MAX_TOKENS = 2000
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """You are extracting business names from Facebook comments on a post that asked: "What's Austin's best breakfast taco place?"

Extract every distinct Austin taco SPOT / BUSINESS that commenters name as their pick.

Rules:
- Return the business name as commonly written, cleaned up (fix obvious typos/casing).
- Count DISTINCT COMMENTERS who named each spot, not raw mentions. If the same commenter names a spot several times, that is 1.
- Ignore comments that name no spot (jokes, tagging friends, off-topic, complaints about traffic).
- Ignore generic phrases that are not a business ("the trailer by my house", "my abuela's kitchen").
- Do NOT invent spots. Only list names that actually appear in the comments.

Return STRICT JSON ONLY - a single array, no prose, no markdown fences:
[{"name": "Business Name", "distinct_mentions": 3}, ...]

COMMENTS:
"""


# ===========================================================================
# SECTION 1 - THE ONE MODEL CALL. This is the ONLY place a model is touched.
#   call_model() is invoked once per CHUNK (>=1, <= MAX_CHUNKS), never per
#   comment. Everything in Section 2 is pure local code.
# ===========================================================================

def call_model(prompt_text, api_key):
    """Single Anthropic Messages API call. Returns raw response text."""
    body = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt_text}],
    }
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    parts = [b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"]
    return "\n".join(parts)


def extract_nominations(comments, api_key, mock_response=None):
    """-> (list of {name, distinct_mentions}, n_model_calls).

    ONE call per chunk of COMMENTS_PER_CHUNK. With the default settings a
    comment set of 2,000 costs 5 calls; a set of 50 costs 1."""
    if mock_response is not None:
        return parse_model_json(mock_response), 0

    if not api_key:
        sys.exit("Set ANTHROPIC_API_KEY, or pass --mock-response to test the "
                 "reconcile/output logic without spending tokens.")

    chunks = [comments[i:i + COMMENTS_PER_CHUNK]
              for i in range(0, len(comments), COMMENTS_PER_CHUNK)][:MAX_CHUNKS]

    merged = defaultdict(int)
    calls = 0
    for chunk in chunks:
        lines = [f"- {(c.get('message') or '').strip()}"
                 for c in chunk if (c.get("message") or "").strip()]
        raw = call_model(EXTRACTION_PROMPT + "\n".join(lines), api_key)
        calls += 1
        for item in parse_model_json(raw):
            merged[item["name"]] += item["distinct_mentions"]

    return ([{"name": n, "distinct_mentions": v} for n, v in merged.items()], calls)


def parse_model_json(raw):
    """Defensive parse. Models sometimes wrap JSON in prose or fences; a bad
    response must degrade to 'nothing extracted', never crash the cycle."""
    if isinstance(raw, list):
        raw_items = raw
    else:
        text = raw if isinstance(raw, str) else json.dumps(raw)
        text = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(),
                      flags=re.MULTILINE).strip()
        m = re.search(r"\[.*\]", text, re.DOTALL)     # first JSON array
        if not m:
            print("  WARNING: model returned no parseable JSON array; "
                  "extracted nothing.", file=sys.stderr)
            return []
        try:
            raw_items = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            print(f"  WARNING: model JSON did not parse ({e}); extracted "
                  "nothing.", file=sys.stderr)
            return []

    out = []
    for it in raw_items:                               # shape-check every item
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or "").strip()
        try:
            n = int(it.get("distinct_mentions", 0))
        except (TypeError, ValueError):
            n = 0
        if name and n > 0:
            out.append({"name": name, "distinct_mentions": n})
    return out


# ===========================================================================
# SECTION 2 - PURE CODE. Reconcile, rank, verdict. No model, no network.
# ===========================================================================

def bank_lookup(bank):
    """normalized form -> canonical bank name, across names AND aliases."""
    lookup = {}
    amap = bank.get("aliases", {}).get("map", {})
    for canonical, aliases in amap.items():
        lookup[normalize(canonical)] = canonical
        for a in aliases:
            lookup.setdefault(normalize(a), canonical)
    # the R1 pool may name spots that have no alias entry yet
    for name in bank.get("round_1_launch", {}).get("verified_contender_pool", []):
        lookup.setdefault(normalize(name), name)
    return lookup


def reconcile(nominations, lookup):
    """Split extracted names into already-in-bank vs novel."""
    known, novel = [], []
    for item in nominations:
        norm = normalize(item["name"])
        canonical = lookup.get(norm)
        if canonical is None:                       # try containment both ways
            for form, canon in lookup.items():
                if form and (form in norm or norm in form) and len(form) > 4:
                    canonical = canon
                    break
        row = dict(item)
        if canonical:
            row["canonical"] = canonical
            row["status"] = "known"
            known.append(row)
        else:
            row["canonical"] = None
            row["status"] = "NEW"
            novel.append(row)
    return known, novel


# Words common enough in ordinary comment text that using them as an alias
# would generate fake votes. Not exhaustive - a human still reviews every
# candidate. Mirrors the hazard class purged from the bank (what-a, van,
# pueblo, lake austin) and catalogued in aliases.review_sensitive.
COMMON_WORD_ALIASES = {
    # Spanish
    "buenos", "buenas", "bueno", "dias", "tres", "dos", "uno", "cuatro", "casa",
    "cocina", "hermanas", "hermanos", "amigo", "amigos", "primo", "tia", "tio",
    "abuela", "mama", "papa", "don", "san", "santa", "nuevo", "nueva", "viejo",
    "vieja", "el", "la", "los", "las",
    # English
    "street", "house", "kitchen", "cafe", "coffee", "corner", "local", "daily",
    "morning", "original", "famous", "best", "good", "great", "new", "old",
    "big", "little", "north", "south", "east", "west",
}


def flag_alias(alias):
    """-> warning string, or None. Catches the OBVIOUS false-positive risks so
    they cannot slip past review at launch. Cannot judge semantics perfectly -
    it is a warning, not a filter."""
    words = alias.split()
    if unsafe_as_auto_alias(alias):
        return "numeric or <=2 chars - would fire inside ordinary text"
    if len(words) == 1 and alias in COMMON_WORD_ALIASES:
        return "common word - high false-positive risk"
    if len(words) == 1 and len(alias) <= 4:
        return "very short single word - review carefully"
    return None


def suggest_aliases(name):
    """Conservative alias suggestions for a NEW spot - human reviews these.
    Skips forms that our own hardening would reject (numeric / <=2 chars)."""
    norm = normalize(name)
    cands = {norm}
    words = norm.split()
    if len(words) > 1:
        cands.add(words[0])                          # "comal street tacos" -> "comal"
        if words[-1] in {"tacos", "taqueria", "taquería", "cafe", "kitchen",
                         "trailer", "bakery"} and len(words) > 2:
            cands.add(" ".join(words[:-1]))
    return sorted(f for f in cands if f and not unsafe_as_auto_alias(f))


def decide(ranked):
    total = sum(n for _, n in ranked)
    if total < MIN_TOTAL_NOMINATIONS_FOR_VERDICT:
        return "thin_turnout", None
    if len(ranked) == 1:
        return "winner", ranked[0][0]
    if ranked[0][1] - ranked[1][1] < WINNER_LEAD:
        return "too_close", None
    return "winner", ranked[0][0]


def verdict_line(outcome, winner):
    """G2: loud, VAGUE on arithmetic. Never a count."""
    if outcome == "winner":
        return f"Austin crowned {winner}. The taco canon has a champion."
    if outcome == "too_close":
        return "Austin couldn't agree on one — this fight's just getting started."
    return "Too quiet out there. RUN IT BACK 👇"


def main():
    ap = argparse.ArgumentParser(description="Round 1 open-floor harvester.")
    ap.add_argument("--comments", required=True)
    ap.add_argument("--mock-response",
                    help="file with a canned model response (skips the API call)")
    ap.add_argument("--bank", default=os.path.join(HERE, os.pardir,
                                                   "reference_bank.json"))
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    with open(args.comments) as f:
        data = json.load(f)
    comments = data.get("data", []) if isinstance(data, dict) else data

    mock = None
    if args.mock_response:
        with open(args.mock_response) as f:
            mock = f.read()

    bank = load_bank(args.bank)
    nominations, n_calls = extract_nominations(
        comments, os.environ.get("ANTHROPIC_API_KEY"), mock)

    known, novel = reconcile(nominations, bank_lookup(bank))
    all_rows = sorted(known + novel, key=lambda r: r["distinct_mentions"],
                      reverse=True)
    ranked = [((r["canonical"] or r["name"]), r["distinct_mentions"])
              for r in all_rows]
    outcome, winner = decide(ranked)
    total = sum(n for _, n in ranked)

    result = {
        "round": "R1 open floor",
        "outcome": outcome,
        "winner": winner,
        "verdict_line": verdict_line(outcome, winner),
        "ranked": [{"name": r["canonical"] or r["name"],
                    "as_typed": r["name"],
                    "distinct_mentions": r["distinct_mentions"],
                    "status": r["status"]} for r in all_rows],
        "new_candidates_for_bank": [
            {
                "name": r["name"],
                "aliases": suggest_aliases(r["name"]),
                "alias_warnings": {a: w for a in suggest_aliases(r["name"])
                                   if (w := flag_alias(a))},
                "status": "unconfirmed",
                "needs_verification": True,
                "recurrence": "plausible_unverified",
                "used": False,
                "_source": "R1 open-floor harvest - NOT yet verified as open (G5)",
            } for r in sorted(novel, key=lambda r: r["distinct_mentions"],
                              reverse=True)
        ],
        "turnout": {
            "total_comments": len(comments),
            "spots_named": len(all_rows),
            "total_nominations": total,
            "thin": outcome == "thin_turnout",
        },
        "review_required": (
            "PROPOSAL ONLY. Nothing was written to reference_bank.json. Verify "
            "each new spot is a real, currently-open Austin business before "
            "adding it (G5), and sanity-check the suggested aliases against the "
            "common-word hazards listed in aliases.review_sensitive."
        ),
        "debug": {
            "_WARNING": "DEBUG ONLY - never publish these numbers (G2).",
            "model_calls_made": n_calls,
            "model_calls_per_comment": 0,
            "counts": {r["name"]: r["distinct_mentions"] for r in all_rows},
        },
    }

    if not args.json_only:
        print("=" * 70)
        print(f"ROUND 1 OPEN FLOOR — {len(comments)} comments, "
              f"{n_calls} model call(s)")
        print("-" * 70)
        print(f"OUTCOME: {outcome.upper()}")
        print(f"VERDICT: {result['verdict_line']}")
        print("           ^ only this line may be published (G2)")
        print("-" * 70)
        print("NAMED SPOTS (debug — never publish):")
        for r in all_rows:
            tag = "known" if r["status"] == "known" else "** NEW **"
            print(f"  {r['distinct_mentions']:>2}  {r['canonical'] or r['name']:<32} {tag}")
        if result["new_candidates_for_bank"]:
            print("-" * 70)
            print("NEW — candidate bank entries for YOUR review (not written):")
            for c in result["new_candidates_for_bank"]:
                print(f"  {c['name']!r}   needs_verification={c['needs_verification']}")
                for a in c["aliases"]:
                    warn = c["alias_warnings"].get(a)
                    if warn:
                        print(f"      - {a!r}   ⚠️  TRIM BEFORE USE: {warn}")
                    else:
                        print(f"      - {a!r}")
        print("=" * 70)
        print()

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
