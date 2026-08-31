#!/usr/bin/env python3
"""
PHASE 0 - PROOF SCRIPT #2  (run this SECOND, after the Graph one works)

Goal: prove the fragile handoff (guardrail G6) - that you can (a) post a card
via Blotato, and (b) recover the native FACEBOOK post-id for that post, so the
next cycle can pull ITS comments via Graph and tally them.

Blotato is the POSTING hand only. The reason this script matters is step (b):
Blotato gives you a submission id, but the tally needs the FB post-id. This
script proves you can bridge the two.

------------------------------------------------------------------------------
CONFIRMED API SHAPE (verified against the live API by curl probing, not guessed):

  1. CREATE:  POST https://backend.blotato.com/v2/posts
     header:  blotato-api-key: <key>
     body:    {"post": {"accountId": "<blotato account id>",
                        "content": {"text": "...", "mediaUrls": [],
                                    "platform": "facebook"},
                        "target":  {"targetType": "facebook",
                                    "pageId": "<facebook page id>"}}}
     returns: {"postSubmissionId": "<uuid>"}
              ^ Blotato's INTERNAL id. This is NOT the Facebook post id.

  2. STATUS:  GET https://backend.blotato.com/v2/posts/{postSubmissionId}
     header:  blotato-api-key: <key>
     returns: {"postSubmissionId": "...", "status": "published",
               "publicUrl": "https://facebook.com/<pageid>_<postid>"}

     The native FB post-id is the "<pageid>_<postid>" tail of publicUrl, and
     that is exactly the id the Graph /comments endpoint needs.

This makes id-capture DETERMINISTIC: we poll until Blotato tells us the post is
published and hands us the URL. No guessing, no "newest post" race, and no
Graph token needed for the capture half.

WHAT YOU NEED:
  - Blotato PAID plan (the API is paid-only) + your API key (Settings -> API;
    keep any trailing '=' - it's base64 padding, not a typo).
  - Your Facebook accountId from Blotato (Settings -> Copy Account ID).
  - FB_PAGE_ID - the numeric Page id, required for target.pageId.

HOW TO RUN (posts a REAL test post - use a throwaway/private page or delete after):
    export BLOTATO_API_KEY="...."
    export BLOTATO_FB_ACCOUNT_ID="...."
    export FB_PAGE_ID="...."
    python3 scripts/prove_blotato_post.py

WHAT SUCCESS LOOKS LIKE:
    It posts a tiny text test, polls until status == "published", then prints
    the real Facebook post-id parsed from publicUrl. That id is what gets
    written to current_round.json for the next cycle's tally.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

# ---- CONFIRMED against the live API ----------------------------------------
BLOTATO_BASE = "https://backend.blotato.com/v2"
BLOTATO_POST_PATH = "/posts"          # POST -> {"postSubmissionId": ...}
BLOTATO_STATUS_PATH = "/posts/{id}"   # GET  -> {"status": ..., "publicUrl": ...}
# ---------------------------------------------------------------------------
GRAPH_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Polling behaviour for the status endpoint.
POLL_MAX_SECONDS = 120        # give up after this long
POLL_INITIAL_DELAY = 2        # first wait, seconds
POLL_MAX_DELAY = 10           # cap the backoff

# HTTP codes that mean "the submission isn't queryable YET" - keep polling.
# 404: status row not created yet. 5xx: transient server trouble. 429: throttled.
RETRYABLE_CODES = {404, 429}

# A native FB post id looks like "<pageid>_<postid>" - both all digits.
FB_POST_ID_RE = re.compile(r"^\d+_\d+$")


def http(method, url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        print(f"\nHTTP {e.code} on {method} {url}:\n{raw}\n", file=sys.stderr)
        raise


def http_no_raise(method, url, headers=None, body=None):
    """Same as http(), but RETURNS (code, body) on an HTTP error instead of
    raising. The poll loop needs to make its own decision about which codes are
    retryable, so it must not be crashed by a transient 404/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, (json.loads(raw) if raw.strip() else {})
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


class PostError(RuntimeError):
    """G6: 'you do not have a valid, recorded post.' Raised - not exited - so a
    conductor can catch it in the dangerous window between 'the post went live'
    and 'the state was written', record what happened, and reconcile. The CLI
    entry points below turn it back into a loud exit 1, so terminal behaviour is
    unchanged."""


def halt(msg):
    """Guardrail G6: fail LOUD. Never let the pipeline continue on a missing or
    uncertain post-id - a wrong id means the next cycle tallies the wrong post.

    Raises PostError. Callers that are CLIs must catch it and exit 1 (see
    _cli_guard below); callers that are the conductor catch it to record the
    half-done state before stopping."""
    raise PostError(msg)


def _cli_guard(fn):
    """Run a CLI main() so a PostError still prints the loud banner and exits 1,
    exactly as the old sys.exit-based halt() did."""
    try:
        fn()
    except PostError as e:
        print("\n" + "=" * 70, file=sys.stderr)
        print("HALT (G6): " + str(e), file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        sys.exit(1)


def blotato_post_test(api_key, account_id, page_id):
    """Create a post via Blotato. Returns the postSubmissionId.

    Body shape is the CONFIRMED nested form - see the docstring."""
    headers = {"Content-Type": "application/json", "blotato-api-key": api_key}
    payload = {
        "post": {
            "accountId": account_id,
            "content": {
                "text": "Austin Beefs pipeline test - safe to delete. 🌮 (proving post + id capture)",
                "mediaUrls": [],
                "platform": "facebook",
            },
            "target": {
                "targetType": "facebook",
                "pageId": page_id,
            },
        }
    }
    print(f"Posting test via Blotato ({BLOTATO_BASE}{BLOTATO_POST_PATH}) ...")
    status, resp = http("POST", BLOTATO_BASE + BLOTATO_POST_PATH, headers, payload)
    print(f"  Blotato responded {status}. Body keys: {list(resp.keys())}")
    print("  full response:")
    print("  " + json.dumps(resp, indent=2).replace("\n", "\n  "))

    submission_id = resp.get("postSubmissionId")
    if not submission_id:
        halt("Blotato accepted the post but returned no postSubmissionId. "
             "Without it we cannot poll for the Facebook post-id.")
    return submission_id


def poll_for_fb_post_id(api_key, submission_id):
    """PRIMARY id-capture path. Poll the status endpoint until the post is
    published, then parse the native FB post-id out of publicUrl.

    Deterministic: Blotato tells us exactly which post it created, so there is
    no race against 'whatever the newest post happens to be'.

    Only the literal status "published" is treated as success. Anything we do
    not recognise keeps polling until timeout - an unknown status must never
    masquerade as success (G6)."""
    headers = {"blotato-api-key": api_key}
    url = BLOTATO_BASE + BLOTATO_STATUS_PATH.format(id=submission_id)

    print(f"\nPolling status ({url}) ...")
    deadline = time.time() + POLL_MAX_SECONDS
    delay = POLL_INITIAL_DELAY
    last = None

    while time.time() < deadline:
        code, resp = http_no_raise("GET", url, headers)
        last = resp

        # --- HTTP-level triage -------------------------------------------
        if code in (401, 403):
            halt(f"Auth rejected by the status endpoint (HTTP {code}). Check "
                 f"BLOTATO_API_KEY (keep any trailing '=').\n"
                 f"  response: {json.dumps(resp, indent=2)}")
        if code in RETRYABLE_CODES or code >= 500:
            print(f"  HTTP {code} - not ready yet, retrying ...")
            time.sleep(delay)
            delay = min(delay * 2, POLL_MAX_DELAY)
            continue
        if code != 200:
            halt(f"Unexpected HTTP {code} from the status endpoint.\n"
                 f"  response: {json.dumps(resp, indent=2)}")

        # --- status triage (explicit; unknown != published) ---------------
        state = (resp.get("status") or "").lower()
        print(f"  status={state or '(none)'}")

        if state == "published":
            public_url = resp.get("publicUrl")
            if not public_url:
                halt("Status is 'published' but no publicUrl was returned, so "
                     "the Facebook post-id cannot be determined.")
            fb_id = public_url.rstrip("/").split("/")[-1]
            if not FB_POST_ID_RE.match(fb_id):
                halt(f"Could not parse a Facebook post-id from publicUrl.\n"
                     f"  publicUrl: {public_url}\n"
                     f"  parsed:    {fb_id!r} (expected '<pageid>_<postid>')")
            print(f"  publicUrl: {public_url}")
            return fb_id, public_url

        if state == "failed":
            halt(f"Blotato reports the post FAILED to publish.\n"
                 f"  full status response: {json.dumps(last, indent=2)}")

        # pending / processing / queued / anything unrecognised -> keep waiting
        time.sleep(delay)
        delay = min(delay * 2, POLL_MAX_DELAY)

    halt(f"Timed out after {POLL_MAX_SECONDS}s waiting for status 'published'.\n"
         f"  last status response: {json.dumps(last, indent=2)}")


# --- FALLBACK (not used) -----------------------------------------------------
# Strategy B, kept for reference only. Before the status endpoint was confirmed,
# this recovered the id by asking Graph for the page's NEWEST post right after
# posting. It is inherently racy: if Blotato publishes slowly, or anything else
# posts to the page in the interim, it silently returns the WRONG id - exactly
# the failure G6 exists to prevent. Use only if the status endpoint is
# unavailable, and treat its result as unverified.
#
# def newest_fb_post_id_via_graph(page_token, page_id):
#     url = f"{GRAPH_BASE}/{page_id}/posts?" + urllib.parse.urlencode({
#         "fields": "id,message,created_time",
#         "limit": 1,
#         "access_token": page_token,
#     })
#     status, resp = http("GET", url)
#     posts = resp.get("data", [])
#     if not posts:
#         return None, None
#     return posts[0]["id"], posts[0].get("created_time")
# -----------------------------------------------------------------------------


def main():
    api_key = os.environ.get("BLOTATO_API_KEY")
    account_id = os.environ.get("BLOTATO_FB_ACCOUNT_ID")
    page_id = os.environ.get("FB_PAGE_ID")

    if not api_key or not account_id:
        sys.exit("Set BLOTATO_API_KEY and BLOTATO_FB_ACCOUNT_ID.")
    if not page_id:
        sys.exit("Set FB_PAGE_ID (required for target.pageId in the post body).")

    # --- post it ---
    submission_id = blotato_post_test(api_key, account_id, page_id)
    print(f"\n  postSubmissionId: {submission_id}  (Blotato's internal id)")

    # --- capture the native FB post-id (deterministic) ---
    fb_id, public_url = poll_for_fb_post_id(api_key, submission_id)

    print(f"\n  FACEBOOK post-id: {fb_id}")
    print("\nSUCCESS: posted via Blotato and captured the native Facebook "
          "post-id. Phase 0 link #2 is proven.")
    print("Production capture path: POST /v2/posts -> postSubmissionId -> poll "
          "GET /v2/posts/{id} -> parse publicUrl tail. Store that id (with "
          "page_id) in current_round.json for the next cycle's tally.")

    print("\nReminder: delete the test post from your page when done.")


if __name__ == "__main__":
    _cli_guard(main)
