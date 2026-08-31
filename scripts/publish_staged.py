#!/usr/bin/env python3
"""
PUBLISH A STAGED CARD - the review-phase publish path (auto_publish: false).

    python3 scripts/publish_staged.py --prepare   # validate, re-render, breadcrumb
    python3 scripts/publish_staged.py --post      # the irreversible half

WHY THIS EXISTS
  In review phase the conductor only STAGES a card; merging the PR used to do
  nothing at all - no trigger fired and no step consumed queue/, so a staged
  card was inert and the cycle re-staged it daily forever. This closes that loop.

THE DEADLINE PROBLEM IT SOLVES (G3)
  A staged card is reviewed now and merged later. A deadline baked in at render
  time is therefore wrong by the time it posts. So the conductor stages the
  INPUTS (slots + a caption template with {{DEADLINE}}) alongside a preview PNG
  whose deadline reads "SET ON POST", and this script recomputes the deadline at
  the actual moment of publishing and re-renders.

"WHAT I REVIEWED IS WHAT SHIPS"
  Step P5 diffs the staged slots against the render slots and REFUSES to post if
  any key other than DEADLINE differs, or if the key set changed at all. That is
  a mechanical assertion, not a promise.

TWO PHASES, because a breadcrumb only protects you if it is PUSHED
  The runner is ephemeral: a breadcrumb written to its local disk dies with it.
  --prepare writes queue/PENDING_POST.json and stops so the workflow can COMMIT
  AND PUSH it; --post then does the irreversible work. A merge-then-crash is
  therefore recoverable, and the next run halts at P1 instead of double-posting.
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
sys.path.insert(0, HERE)

import state                                    # noqa: E402
import render_card                              # noqa: E402
from post_card import upload_media, create_post  # noqa: E402
from prove_blotato_post import poll_for_fb_post_id, PostError  # noqa: E402

QUEUE = os.path.join(ROOT, "queue")
PUBLISHED = os.path.join(ROOT, "published")
BREADCRUMB = os.path.join(QUEUE, "PENDING_POST.json")
DEADLINE_PLACEHOLDER = "SET ON POST"
DEADLINE_KEY = "DEADLINE"


class PublishError(RuntimeError):
    """Refusing to publish. Never swallow this."""


def halt(msg):
    raise PublishError(msg)


def _emit(key, value):
    """Expose a value to later workflow steps."""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"{key}={value}\n")
    print(f"  [output] {key}={value}")


def find_staged():
    """-> dict of staged paths, or None if nothing is staged."""
    hits = sorted(glob.glob(os.path.join(QUEUE, "round_*_slots.json")))
    if not hits:
        return None
    if len(hits) > 1:
        halt(f"More than one staged card in queue/: {[os.path.basename(h) for h in hits]}. "
             f"Refusing to guess which one to publish.")
    slots_path = hits[0]
    m = re.search(r"round_(\d+)_slots\.json$", slots_path)
    if not m:
        halt(f"Cannot read a round number from {slots_path}")
    n = int(m.group(1))
    return {
        "round": n,
        "slots": slots_path,
        "caption_tmpl": os.path.join(QUEUE, f"round_{n}_caption.tmpl"),
        "preview_png": os.path.join(QUEUE, f"round_{n}.png"),
        "final_png": os.path.join(QUEUE, f"round_{n}.final.png"),
        "final_caption": os.path.join(QUEUE, f"round_{n}.final.txt"),
        "proposed": os.path.join(QUEUE, "current_round.proposed.json"),
        "canon": os.path.join(QUEUE, f"round_{n}_canon.json"),   # optional
    }


def load_json(path, what):
    if not os.path.isfile(path):
        halt(f"Staged {what} missing: {path}")
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            halt(f"Staged {what} is not valid JSON ({e}): {path}")


# ---------------------------------------------------------------------------
# P5 - the assertion that makes "review == ship" mechanical
# ---------------------------------------------------------------------------

def assert_only_deadline_changed(staged, rendered):
    staged_keys, rendered_keys = set(staged), set(rendered)
    if staged_keys != rendered_keys:
        halt("Slot KEY SET changed between review and publish.\n"
             f"  added:   {sorted(rendered_keys - staged_keys)}\n"
             f"  removed: {sorted(staged_keys - rendered_keys)}\n"
             "  Refusing to post something other than what was reviewed.")
    changed = [k for k in staged_keys if staged[k] != rendered[k]]
    if changed != [DEADLINE_KEY]:
        offenders = {k: {"reviewed": staged[k], "would_post": rendered[k]}
                     for k in changed if k != DEADLINE_KEY}
        halt("Slots other than DEADLINE changed between review and publish:\n"
             f"{json.dumps(offenders, indent=2, ensure_ascii=False)}\n"
             "  Refusing to post something other than what was reviewed.")
    print(f"  P5 OK: exactly one slot changed - {DEADLINE_KEY} "
          f"{staged[DEADLINE_KEY]!r} -> {rendered[DEADLINE_KEY]!r}")


# ---------------------------------------------------------------------------
# phases
# ---------------------------------------------------------------------------

def prepare():
    cfg = state.load_config()

    # P0 - the double-post guard. Autonomous mode posts inside the conductor;
    # this job must never also post. Exit 0: "nothing to do", not an error.
    if cfg.get("auto_publish") is True:
        print("P0 auto_publish is TRUE -> the conductor posts directly. "
              "Nothing for this job to do.")
        _emit("should_publish", "false")
        return
    staged = find_staged()
    if staged is None:
        print("P0 no staged card in queue/ -> nothing to publish.")
        _emit("should_publish", "false")
        return
    print(f"P0 guard passed: review phase, round {staged['round']} staged.")

    # P1 - reconcile. Shared with the cycle workflow's gate: ONE implementation
    # of the D5 rule, so the two paths cannot drift.
    state.assert_no_pending_post()
    print("P1 reconcile: no breadcrumb.")

    # P2 - load
    slots_staged = load_json(staged["slots"], "slots")
    if DEADLINE_KEY not in slots_staged:
        halt(f"Staged slots have no {DEADLINE_KEY} key: {staged['slots']}")
    if not os.path.isfile(staged["caption_tmpl"]):
        halt(f"Staged caption template missing: {staged['caption_tmpl']}")
    caption_tmpl = open(staged["caption_tmpl"], encoding="utf-8").read()
    if "{{DEADLINE}}" not in caption_tmpl:
        halt("Staged caption template has no {{DEADLINE}} placeholder - it would "
             "ship a stale deadline in the copy even if the card is correct.")
    proposed = load_json(staged["proposed"], "proposed current_round")
    archetype = proposed.get("archetype")
    if not archetype:
        halt("Staged proposed current_round has no archetype.")
    print(f"P2 loaded slots({len(slots_staged)}), caption template, proposal.")

    # P3 - THE POINT: deadline computed NOW, at publish time (G3)
    now = datetime.now()
    dl = state.compute_deadline(now)
    print(f"P3 deadline computed at publish time: {dl['label']!r} ({dl['iso']})")

    # P4 - re-render with ONLY the deadline substituted
    slots_render = dict(slots_staged)
    slots_render[DEADLINE_KEY] = dl["label"]

    # P5 - prove it
    assert_only_deadline_changed(slots_staged, slots_render)

    info = render_card.render_card(archetype, slots_render, staged["final_png"])
    print(f"P4 re-rendered {info['width']}x{info['height']} -> {staged['final_png']}")

    # P6 - caption
    caption = caption_tmpl.replace("{{DEADLINE}}", dl["label"])
    with open(staged["final_caption"], "w", encoding="utf-8") as f:
        f.write(caption)
    print("P6 caption filled from template.")

    # P7 - breadcrumb (the WORKFLOW pushes this before --post runs)
    with open(BREADCRUMB, "w", encoding="utf-8") as f:
        json.dump({
            "round": staged["round"],
            "archetype": archetype,
            "matchup": proposed.get("matchup"),
            "png": staged["final_png"],
            "deadline_iso": dl["iso"],
            "deadline_label": dl["label"],
            "started_at": now.isoformat(),
            "blotato_submission_id": None,   # filled by --post the moment it exists
            "_note": "An irreversible post is about to be attempted. If this file "
                     "survives, the post may be LIVE but unrecorded. Recover with "
                     "blotato_submission_id (re-poll GET /v2/posts/{id}).",
        }, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"P7 breadcrumb written -> {BREADCRUMB} (workflow must PUSH it now)")
    _emit("should_publish", "true")
    _emit("round", staged["round"])


def post():
    cfg = state.load_config()
    if cfg.get("auto_publish") is True:
        halt("auto_publish is TRUE - refusing to run the review-phase publisher.")
    staged = find_staged()
    if staged is None:
        halt("--post called with nothing staged.")
    if not os.path.exists(BREADCRUMB):
        halt("--post called with no breadcrumb. Run --prepare first (and push it).")

    crumb = load_json(BREADCRUMB, "breadcrumb")
    caption = open(staged["final_caption"], encoding="utf-8").read()
    api_key = os.environ.get("BLOTATO_API_KEY")
    if not api_key:
        halt("BLOTATO_API_KEY is not set.")
    page_id = str(cfg["page_id"])
    account_id = str(cfg["blotato_fb_account_id"])

    # P8 - three-call orchestration, so the submission id lands in the
    # breadcrumb the instant it exists. Same functions post_card() itself uses.
    hosted = upload_media(staged["final_png"], api_key)      # reversible
    submission_id = create_post(caption, hosted, page_id,    # IRREVERSIBLE
                                account_id, api_key)

    crumb["blotato_submission_id"] = submission_id
    crumb["blotato_media_url"] = hosted
    with open(BREADCRUMB, "w", encoding="utf-8") as f:
        json.dump(crumb, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"\n  ***** RECOVERY KEY - blotato_submission_id={submission_id} *****")
    print("  If the next step fails, re-poll GET /v2/posts/{id} with this id.\n")

    fb_post_id, public_url = poll_for_fb_post_id(api_key, submission_id)
    print(f"P8 published. fb_post_id={fb_post_id}")

    # P9 - state. current_round FIRST (operating state), canon second (archive).
    posted_at = datetime.now().isoformat()
    state.write_current_round({
        "round": crumb["round"],
        "archetype": crumb["archetype"],
        "matchup": crumb.get("matchup"),
        "fb_post_id": fb_post_id,
        "blotato_submission_id": submission_id,
        "deadline_iso": crumb["deadline_iso"],
        "posted_at": posted_at,
        "page_id": page_id,
    })
    print(f"P9 current_round.json written (posted_at={posted_at})")

    if os.path.isfile(staged["canon"]):
        entry = load_json(staged["canon"], "canon entry")
        state.append_to_canon(entry)
        print(f"P9 canon: appended round {entry.get('round')}")
    else:
        print("P9 canon: no staged entry (Round 1 has no prior result) - skipped.")

    # P10 - clear the queue
    os.makedirs(PUBLISHED, exist_ok=True)
    os.replace(staged["final_png"],
               os.path.join(PUBLISHED, f"round_{crumb['round']}.png"))
    for p in (staged["slots"], staged["caption_tmpl"], staged["preview_png"],
              staged["final_caption"], staged["proposed"], staged["canon"],
              BREADCRUMB):
        if os.path.exists(p):
            os.remove(p)
    print("P10 queue cleared; breadcrumb deleted LAST.")
    _emit("fb_post_id", fb_post_id)
    print(f"\nPUBLISHED {public_url}")


def main():
    ap = argparse.ArgumentParser(description="Publish a staged (review-phase) card.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--prepare", action="store_true")
    g.add_argument("--post", action="store_true")
    args = ap.parse_args()
    (prepare if args.prepare else post)()


if __name__ == "__main__":
    try:
        main()
    except (PublishError, PostError, state.StateError,
            render_card.RenderError) as e:
        print("\n" + "=" * 70, file=sys.stderr)
        print(f"PUBLISH HALT: {e}", file=sys.stderr)
        print("=" * 70 + "\n", file=sys.stderr)
        sys.exit(1)
