#!/usr/bin/env python3
"""Demonstration of scripts/state.py against a throwaway root directory.
Never touches the real current_round.json / canon.json."""
import json, os, shutil, sys, tempfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "scripts"))
import state
from state import StateError

ROOT = tempfile.mkdtemp(prefix="abstate_")
shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         os.pardir, "config.json"), ROOT)

def show(n, title): print(f"\n{'='*66}\n{n}. {title}\n{'='*66}")
def ok(msg):  print(f"   PASS  {msg}")
def bad(msg): print(f"   FAIL  {msg}")

# ---------------------------------------------------------------- 1
show(1, "COLD START - no current_round.json, no canon.json")
assert state.read_current_round(ROOT) is None
ok("read_current_round() -> None (absent = legitimate cold start)")
assert state.read_canon(ROOT) == []
ok("read_canon() -> [] (empty history)")
assert state.is_post_due(ROOT) is True
ok("is_post_due() -> True (nothing posted yet)")
assert state.next_round_number(ROOT) == 1
ok("next_round_number() -> 1 (Round 1 = the fixed taco open floor)")

# ---------------------------------------------------------------- 2
show(2, "COMPUTE DEADLINE (G3 - code owns the date)")
posted = datetime(2026, 8, 31, 18, 0, 0)          # Monday 6pm - AFTER post_time
dl = state.compute_deadline(posted, ROOT)
print(f"   posted_at {posted.isoformat()}  + cadence {dl['days']}d")
print(f"   -> iso   {dl['iso']}")
print(f"   -> label {dl['label']!r}   <- this is what goes on the card")
assert dl["label"] == "FRI 5PM", dl["label"]
ok("snapped to post_time_local (17:00), not the raw 18:00 minute")
ok("posted AFTER 17:00 -> pushed to FRI, so the window is never < cadence")
assert (datetime.fromisoformat(dl["iso"]) - posted) >= timedelta(days=3)
ok("voting window is a FULL cadence period")

morning = datetime(2026, 8, 31, 9, 0, 0)          # Monday 9am - BEFORE post_time
dlm = state.compute_deadline(morning, ROOT)
print(f"   morning merge {morning:%a %H:%M} -> {dlm['label']!r}")
assert dlm["label"] == "THU 5PM", dlm["label"]
ok("morning merge lands on the natural day (THU), also a clean 5PM")
assert (datetime.fromisoformat(dlm["iso"]) - morning) >= timedelta(days=3)
ok("clean label built without %-I/%p, so macOS and Linux agree")

# ---------------------------------------------------------------- 3
show(3, "FRESH POST - write current_round, append canon")
rnd = {
    "round": 1,
    "archetype": "open_floor",
    "matchup": {"question": "What's Austin's best breakfast taco place?"},
    "fb_post_id": "1283217111543170_9876543210",
    "blotato_submission_id": "b1f2c3d4-0000-0000-0000-000000000001",
    "deadline_iso": dl["iso"],
    "posted_at": posted.isoformat(),
    "page_id": "1283217111543170",
}
state.write_current_round(rnd, ROOT)
back = state.read_current_round(ROOT)
assert back == rnd
ok("write -> read round-trips exactly (atomic temp+rename)")
assert state.next_round_number(ROOT) == 2
ok("next_round_number() -> 2 while Round 1 is live")

state.append_to_canon({
    "round": 1, "matchup": {"question": "best breakfast taco"},
    "winner": "Veracruz All Natural",
    "verdict_line": "Austin has spoken - Veracruz All Natural takes it.",
    "date": "2026-09-03",
}, ROOT)
assert len(state.read_canon(ROOT)) == 1
ok("append_to_canon() recorded round 1")

state.append_to_canon({
    "round": 2, "matchup": {"a": "Franklin Barbecue", "b": "la Barbecue"},
    "winner": None,                                  # honest non-result
    "verdict_line": "Austin couldn't decide - this one goes to overtime.",
    "date": "2026-09-06",
}, ROOT)
ok("append_to_canon() accepts winner:None (honest too-close round, G8)")

try:
    state.append_to_canon({"round": 1, "winner": "x", "verdict_line": "y",
                           "date": "2026-09-03"}, ROOT)
    bad("duplicate round was appended!")
except StateError as e:
    ok(f"duplicate round refused - {str(e).splitlines()[0][:58]}...")

# ---------------------------------------------------------------- 4
show(4, "CADENCE GATE")
d0 = date.fromisoformat(posted.date().isoformat())
for offset, expect in ((0, False), (1, False), (2, False), (3, True), (5, True)):
    got = state.is_post_due(ROOT, today=d0 + timedelta(days=offset))
    tag = "due" if got else "not due"
    (ok if got == expect else bad)(f"{offset}d after posting -> {tag}")

# ---------------------------------------------------------------- 5
show(5, "CORRUPT STATE MUST HALT, NOT LOOK LIKE A COLD START")
for label, payload in (("invalid JSON", "{not json at all"),
                       ("empty file", ""),
                       ("missing required keys", '{"round": 4}')):
    with open(os.path.join(ROOT, "current_round.json"), "w") as f:
        f.write(payload)
    try:
        state.read_current_round(ROOT)
        bad(f"{label}: was accepted (would silently restart at Round 1!)")
    except StateError as e:
        ok(f"{label}: HALTED - {str(e).splitlines()[0][:52]}...")

try:
    state.is_post_due(ROOT)
    bad("is_post_due() ignored corrupt state")
except StateError:
    ok("is_post_due() halts on corrupt state too (no silent re-post)")

print(f"\n{'='*66}\nAll checks done. Temp root: {ROOT}\n{'='*66}")
shutil.rmtree(ROOT, ignore_errors=True)
