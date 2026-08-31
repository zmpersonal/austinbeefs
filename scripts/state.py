#!/usr/bin/env python3
"""
STATE + CADENCE - what the cycle remembers between runs, and when it fires.

Pure stdlib, no network. Owns two files:

  current_round.json   the LIVE post. Read at step 1, overwritten at step 10.
                       Its fb_post_id is how the NEXT cycle finds this post's
                       comments to tally.
  canon.json           append-only record of finished rounds. Never rewritten.
                       (Also the future website's content.)

COLD START vs CORRUPT - the distinction that protects the flywheel (G6)
    file ABSENT   = legitimate cold start -> Round 1, the fixed taco open floor.
    file PRESENT but unreadable/missing required keys = CORRUPT -> HALT.
  Treating a corrupt file as a cold start would silently re-run Round 1 and
  throw away the round history. That is the expensive failure, so it is loud.

G3 - CODE OWNS THE DEADLINE
  compute_deadline() derives the voting deadline from publish time + cadence.
  The model never writes a date; it only receives the label this produces.

ATOMIC WRITES
  Every write goes to a temp file in the same directory and is os.replace()d
  into place, so a crash mid-write cannot leave truncated JSON behind.
"""

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    os.pardir))

CURRENT_ROUND_FILE = "current_round.json"
CANON_FILE = "canon.json"
CONFIG_FILE = "config.json"

# A live round must carry all of these or the cycle cannot safely continue.
REQUIRED_ROUND_KEYS = ("round", "archetype", "fb_post_id", "posted_at",
                       "deadline_iso", "page_id")
# A canon entry must carry these. `winner` may be None - an honest "too close"
# or "thin turnout" round is still a real round and belongs in the record (G8).
REQUIRED_CANON_KEYS = ("round", "winner", "verdict_line", "date")


class StateError(RuntimeError):
    """Corrupt or inconsistent state. Never swallow this."""


def halt(msg):
    """G6: fail loud. State problems must stop the cycle, not be guessed past."""
    raise StateError(msg)


def _path(name, root=None):
    return os.path.join(root or ROOT, name)


def _read_json(path, what):
    """Read JSON, distinguishing ABSENT (returns None) from CORRUPT (halts)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        halt(f"{what} exists at {path} but could not be read: {e}")
    if not text.strip():
        halt(f"{what} exists at {path} but is EMPTY. This is corrupt state, "
             f"not a cold start - refusing to restart the round history.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        halt(f"{what} at {path} is not valid JSON ({e}). This is corrupt "
             f"state, not a cold start - fix or delete it deliberately.")


def _write_json_atomic(path, data):
    """Write to a temp file in the same dir, then os.replace() into place."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # atomic on POSIX
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def load_config(root=None):
    cfg = _read_json(_path(CONFIG_FILE, root), "config.json")
    if cfg is None:
        halt(f"config.json not found at {_path(CONFIG_FILE, root)}")
    return cfg


def cadence_days(root=None, config=None):
    cfg = config or load_config(root)
    try:
        n = int(cfg.get("cadence_days", 3))
    except (TypeError, ValueError):
        halt(f"config.cadence_days is not an integer: {cfg.get('cadence_days')!r}")
    if n < 1:
        halt(f"config.cadence_days must be >= 1, got {n}")
    return n


# ---------------------------------------------------------------------------
# current_round.json
# ---------------------------------------------------------------------------

def read_current_round(root=None):
    """-> dict, or None if the file is legitimately ABSENT (cold start).
    A present-but-broken file HALTS rather than reporting a cold start."""
    path = _path(CURRENT_ROUND_FILE, root)
    data = _read_json(path, "current_round.json")
    if data is None:
        return None
    if not isinstance(data, dict):
        halt(f"current_round.json must be a JSON object, got {type(data).__name__}.")
    missing = [k for k in REQUIRED_ROUND_KEYS if k not in data]
    if missing:
        halt(f"current_round.json is missing required key(s): {missing}. "
             f"Corrupt state - not treating this as a cold start.")
    return data


def write_current_round(entry, root=None):
    """Validate then atomically overwrite the live-round file."""
    if not isinstance(entry, dict):
        halt("write_current_round() needs a dict.")
    missing = [k for k in REQUIRED_ROUND_KEYS if not entry.get(k)]
    if missing:
        halt(f"Refusing to write current_round.json without: {missing}")
    _write_json_atomic(_path(CURRENT_ROUND_FILE, root), entry)
    return entry


# ---------------------------------------------------------------------------
# canon.json  (append-only)
# ---------------------------------------------------------------------------

def read_canon(root=None):
    """-> list. An absent canon is a legitimate empty history."""
    data = _read_json(_path(CANON_FILE, root), "canon.json")
    if data is None:
        return []
    if not isinstance(data, list):
        halt(f"canon.json must be a JSON list, got {type(data).__name__}.")
    return data


def append_to_canon(entry, root=None):
    """Append one finished round. Never rewrites or reorders history."""
    if not isinstance(entry, dict):
        halt("append_to_canon() needs a dict.")
    missing = [k for k in REQUIRED_CANON_KEYS if k not in entry]
    if missing:
        halt(f"Refusing to append a canon entry missing: {missing}")
    if not entry.get("verdict_line"):
        halt("Refusing to append a canon entry with an empty verdict_line.")

    canon = read_canon(root)
    existing = {e.get("round") for e in canon if isinstance(e, dict)}
    if entry["round"] in existing:
        halt(f"Round {entry['round']} is already in canon.json. Canon is "
             f"append-only - refusing to record it twice.")
    canon.append(entry)
    _write_json_atomic(_path(CANON_FILE, root), canon)
    return canon


# ---------------------------------------------------------------------------
# cadence  (must agree with the gate in .github/workflows/cycle.yml)
# ---------------------------------------------------------------------------

PENDING_POST_FILE = os.path.join("queue", "PENDING_POST.json")


def assert_no_pending_post(root=None):
    """G6/D5: halt if an unreconciled post breadcrumb exists.

    The breadcrumb is written (and PUSHED) before any irreversible post. If it
    is still here, a previous run created a post and never recorded its
    fb_post_id - so current_round.json still names the PREVIOUS post and the
    cadence gate would happily post AGAIN, every run, forever. That is the
    duplicate-post-forever failure, and it is why this halts instead of warning.

    Recover by hand: the breadcrumb carries blotato_submission_id - re-poll
    GET /v2/posts/{id} for the fb_post_id, write it into current_round.json,
    then delete the breadcrumb. NEVER clear it automatically."""
    path = os.path.join(root or ROOT, PENDING_POST_FILE)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                crumb = json.load(f)
            detail = (f"round={crumb.get('round')} "
                      f"started_at={crumb.get('started_at')} "
                      f"blotato_submission_id={crumb.get('blotato_submission_id')}")
        except Exception:
            detail = "(breadcrumb unreadable)"
        halt(f"UNRECONCILED POST: {PENDING_POST_FILE} exists. A post may be LIVE "
             f"on the Page with no fb_post_id recorded. {detail}. Reconcile by "
             f"hand, then delete the breadcrumb. Refusing to post again.")


def is_post_due(root=None, today=None):
    """True if >= cadence_days have passed since posted_at, or on cold start.

    Compares DATES only, matching the workflow gate exactly:
        days = (date.today() - date.fromisoformat(posted_at[:10])).days
        due  = days >= cadence_days
    """
    cr = read_current_round(root)          # halts if corrupt
    if cr is None:
        return True                        # cold start: nothing posted yet
    posted_at = cr.get("posted_at")
    if not posted_at:                      # validated on read, belt and braces
        halt("current_round.json has no posted_at - cannot judge cadence.")
    try:
        last = date.fromisoformat(str(posted_at)[:10])
    except ValueError:
        halt(f"current_round.json posted_at is not an ISO date: {posted_at!r}")
    days = ((today or date.today()) - last).days
    return days >= cadence_days(root)


def compute_deadline(posted_at, root=None, config=None, snap=True):
    """G3: CODE owns this date. -> {"iso", "label", "days", "snapped"}.

    THE SINGLE IMPLEMENTATION. Both the autonomous conductor (which posts at
    cron time) and the review-phase publisher (which posts whenever a human
    merges) call this, so both produce identical, clean deadlines.

    SNAPPING: the raw arithmetic posted_at + cadence_days inherits the minute it
    happened to run - a merge at 14:30 would put "THU 2:30PM" on the card, which
    reads like a bug. With snap=True the deadline lands on config.post_time_local
    ("17:00" -> 5PM) on the target day.

    THE SHORT-WINDOW EDGE CASE: snapping can pull the deadline EARLIER than a
    full cadence period. Merge Monday 20:00 with a 3-day cadence and a 17:00 post
    time, and the naive target is Thursday 17:00 - only 2d21h of voting. So after
    snapping we push out a whole day at a time until the window is at least
    cadence_days long. Voters always get the full period; they never get less
    because of when someone clicked Merge.

    label is built by hand rather than with %-I/%p so it renders identically on
    macOS and on the Linux runner."""
    cfg = config or load_config(root)
    n = cadence_days(root, cfg)

    if isinstance(posted_at, datetime):
        dt = posted_at
    else:
        try:
            dt = datetime.fromisoformat(str(posted_at))
        except ValueError:
            halt(f"posted_at is not an ISO datetime: {posted_at!r}")

    earliest = dt + timedelta(days=n)      # never close voting before this
    deadline = earliest
    snapped = False

    if snap and cfg.get("post_time_local"):
        try:
            hh, mm = [int(x) for x in str(cfg["post_time_local"]).split(":")]
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
        except ValueError:
            halt(f"config.post_time_local is not HH:MM: {cfg['post_time_local']!r}")
        deadline = earliest.replace(hour=hh, minute=mm, second=0, microsecond=0)
        while deadline < earliest:         # never shorten the voting window
            deadline += timedelta(days=1)
        snapped = True

    hour12 = deadline.hour % 12 or 12
    ampm = "AM" if deadline.hour < 12 else "PM"
    minute = "" if deadline.minute == 0 else f":{deadline.minute:02d}"
    label = f"{deadline:%a}".upper() + f" {hour12}{minute}{ampm}"
    return {"iso": deadline.isoformat(), "label": label, "days": n,
            "snapped": snapped}


def next_round_number(root=None):
    """Live round + 1; else max canon round + 1; else 1 (cold start)."""
    cr = read_current_round(root)
    if cr is not None:
        try:
            return int(cr["round"]) + 1
        except (TypeError, ValueError):
            halt(f"current_round.json round is not an integer: {cr.get('round')!r}")
    canon = read_canon(root)
    rounds = [e.get("round") for e in canon
              if isinstance(e, dict) and isinstance(e.get("round"), int)]
    return (max(rounds) + 1) if rounds else 1


def main():
    """Print the current state - a quick 'where are we?' for the operator."""
    cr = read_current_round()
    print(f"cadence_days     : {cadence_days()}")
    print(f"current_round    : {'(absent - COLD START)' if cr is None else ''}")
    if cr:
        print(json.dumps(cr, indent=2, ensure_ascii=False))
    print(f"canon entries    : {len(read_canon())}")
    print(f"next round number: {next_round_number()}")
    print(f"post due now?    : {is_post_due()}")


if __name__ == "__main__":
    try:
        main()
    except StateError as e:
        print(f"\nSTATE HALT (G6): {e}\n", file=sys.stderr)
        sys.exit(1)
