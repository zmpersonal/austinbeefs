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
!! HONESTY FLAG !!  I have NOT verified Blotato's exact REST paths / field names
from here. The structure below matches Blotato's documented tool surface, but
CONFIRM the exact endpoint, payload keys, and response shape at:
    help.blotato.com/api   (and the MCP page: help.blotato.com/api/mcp)
Treat the BLOTATO_* constants below as "fill in from the docs," not gospel.
The GRAPH fallback in strategy B is accurate and stable; lean on it.

WHAT YOU NEED:
  - Blotato PAID plan (the API is paid-only) + your API key (Settings -> API;
    keep any trailing '=' - it's base64 padding, not a typo).
  - Your Facebook accountId from Blotato (Settings -> Copy Account ID).
  - The same FB_PAGE_TOKEN + FB_PAGE_ID from proof script #1 (for strategy B).

HOW TO RUN (posts a REAL test post - use a throwaway/private page or delete after):
    export BLOTATO_API_KEY="...."
    export BLOTATO_FB_ACCOUNT_ID="...."
    export FB_PAGE_TOKEN="...."          # for the FB-post-id recovery
    export FB_PAGE_ID="...."
    python3 scripts/prove_blotato_post.py

WHAT SUCCESS LOOKS LIKE:
    It posts a tiny text test, then prints a real Facebook post-id it recovered.
    If strategy A (from Blotato's response) yields the id, great. If not,
    strategy B (match your page's newest post via Graph) will - and that tells
    you which recovery method the real pipeline should use.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

# ---- CONFIRM THESE AGAINST help.blotato.com/api ----------------------------
BLOTATO_BASE = "https://backend.blotato.com/v2"
BLOTATO_POST_PATH = "/posts"          # <-- verify exact path
BLOTATO_STATUS_PATH = "/posts/{id}"   # <-- verify exact path
# ---------------------------------------------------------------------------
GRAPH_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"


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


def blotato_post_test(api_key, account_id):
    """Strategy A source: create a post via Blotato, return the raw response
    so we can look for a native post id or a submission id to poll."""
    headers = {"Content-Type": "application/json", "blotato-api-key": api_key}
    # Payload shape per Blotato's documented fields. VERIFY key names in docs.
    payload = {
        "platform": "facebook",
        "accountId": account_id,
        "text": "Austin Beefs pipeline test - safe to delete. 🌮 (proving post + id capture)",
        "mediaUrls": [],
        # no scheduledTime = post now (or set a near-future ISO8601 UTC string)
    }
    print(f"Posting test via Blotato ({BLOTATO_BASE}{BLOTATO_POST_PATH}) ...")
    status, resp = http("POST", BLOTATO_BASE + BLOTATO_POST_PATH, headers, payload)
    print(f"  Blotato responded {status}. Body keys: {list(resp.keys())}")
    print("  full response (inspect for a post id or submission id):")
    print("  " + json.dumps(resp, indent=2).replace("\n", "\n  "))
    return resp


def try_extract_fb_id_from_blotato(resp):
    """Strategy A: hunt the Blotato response for a native FB post id.
    We don't know the exact key, so scan common candidates + anything that
    looks like an FB id ('PAGEID_POSTID')."""
    candidates = ["facebookPostId", "postId", "nativePostId", "platformPostId",
                  "post_id", "id", "postSubmissionId", "submissionId"]
    found = {}
    for k in candidates:
        if k in resp and resp[k]:
            found[k] = resp[k]
    # also scan nested dicts one level down
    for k, v in resp.items():
        if isinstance(v, dict):
            for kk in candidates:
                if kk in v and v[kk]:
                    found[f"{k}.{kk}"] = v[kk]
    return found


def newest_fb_post_id_via_graph(page_token, page_id):
    """Strategy B (accurate/stable): right after posting, ask Graph for the
    page's newest post. If it's the one we just made, that's our FB post-id.
    This is the reliable fallback and likely what production should use."""
    url = f"{GRAPH_BASE}/{page_id}/posts?" + urllib.parse.urlencode({
        "fields": "id,message,created_time",
        "limit": 1,
        "access_token": page_token,
    })
    status, resp = http("GET", url)
    posts = resp.get("data", [])
    if not posts:
        return None, None
    return posts[0]["id"], posts[0].get("created_time")


def main():
    api_key = os.environ.get("BLOTATO_API_KEY")
    account_id = os.environ.get("BLOTATO_FB_ACCOUNT_ID")
    page_token = os.environ.get("FB_PAGE_TOKEN")
    page_id = os.environ.get("FB_PAGE_ID")

    if not api_key or not account_id:
        sys.exit("Set BLOTATO_API_KEY and BLOTATO_FB_ACCOUNT_ID.")

    # --- post it ---
    resp = blotato_post_test(api_key, account_id)

    # --- Strategy A: id straight from Blotato ---
    print("\n[Strategy A] Looking for an FB post-id in Blotato's response ...")
    a = try_extract_fb_id_from_blotato(resp)
    if a:
        print("  candidate ids found:", json.dumps(a, indent=2))
    else:
        print("  none obvious. That's OK - strategy B is the reliable path.")

    # --- Strategy B: recover via Graph ---
    if page_token and page_id:
        print("\n[Strategy B] Recovering newest FB post-id via Graph (waiting a "
              "few seconds for the post to land) ...")
        time.sleep(8)  # give Blotato a moment to actually publish
        fb_id, created = newest_fb_post_id_via_graph(page_token, page_id)
        if fb_id:
            print(f"  newest FB post-id: {fb_id}")
            print(f"  created:           {created}")
            print("\nSUCCESS: you can recover an FB post-id to feed the tally. "
                  "Phase 0 link #2 is proven.")
            print("Decide which strategy production uses: A if Blotato returns a "
                  "usable native id, else B (match newest post right after posting).")
        else:
            print("  couldn't fetch a post via Graph - re-check the token/page id "
                  "(proof script #1 should have already confirmed these).")
    else:
        print("\n[Strategy B] skipped (no FB_PAGE_TOKEN/FB_PAGE_ID). Set them to "
              "prove the id-recovery path - this is the part that matters most.")

    print("\nReminder: delete the test post from your page when done.")


if __name__ == "__main__":
    main()
