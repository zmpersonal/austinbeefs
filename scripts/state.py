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


def compute_deadline(posted_at, root=None, config=None):
    """G3: CODE owns this date. -> {"iso", "label", "days"}.

    label is what goes on the card, e.g. "MON 6PM". Built by hand rather than
    with %-I/%p so it renders identically on macOS and on the Linux runner."""
    cfg = config or load_config(root)
    n = cadence_days(root, cfg)

    if isinstance(posted_at, datetime):
        dt = posted_at
    else:
        try:
            dt = datetime.fromisoformat(str(posted_at))
        except ValueError:
            halt(f"posted_at is not an ISO datetime: {posted_at!r}")

    deadline = dt + timedelta(days=n)
    hour12 = deadline.hour % 12 or 12
    ampm = "AM" if deadline.hour < 12 else "PM"
    minute = "" if deadline.minute == 0 else f":{deadline.minute:02d}"
    label = f"{deadline:%a}".upper() + f" {hour12}{minute}{ampm}"
    return {"iso": deadline.isoformat(), "label": label, "days": n}


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
