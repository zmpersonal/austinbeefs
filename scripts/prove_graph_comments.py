#!/usr/bin/env python3
"""
PHASE 0 - PROOF SCRIPT #1  (run this FIRST, before building anything)

Goal: prove you can read the comments off one of your own Facebook Page's posts
via the Graph API. If this works, the whole vote-tally mechanic is possible.
If it doesn't, the Austin Beefs concept needs rethinking - so find out now.

This uses the Facebook GRAPH API directly (NOT Blotato). Comment-reading is
always Graph in this project - see CLAUDE.md (LISTENING HAND).

------------------------------------------------------------------------------
WHAT YOU NEED (one-time setup at developers.facebook.com):
  1. A Facebook App (any type; "Business" is fine).
  2. A PAGE access token for the Page that runs Austin Beefs, with the
     `pages_read_engagement` permission (and `pages_show_list`).
     - Quickest path to test: Graph API Explorer -> select your app ->
       "Get Page Access Token" -> grant pages_read_engagement.
     - For production, exchange it for a LONG-LIVED page token (they last ~60
       days and can be refreshed). Short-lived tokens expire in ~1 hour, which
       is fine just to prove this works today.
  3. Your Page's numeric ID (Graph API Explorer: GET /me?fields=id,name while
     on a page token, or it's in your Page settings).

HOW TO RUN:
    export FB_PAGE_TOKEN="EAAB...your-page-token..."
    export FB_PAGE_ID="1234567890"          # your page's numeric id
    python3 scripts/prove_graph_comments.py

    # optional: test a specific post instead of the latest one
    export FB_POST_ID="1234567890_9876543210"
    python3 scripts/prove_graph_comments.py

WHAT SUCCESS LOOKS LIKE:
    It prints your latest post, then lists the commenters and their comment
    text. That's the raw material the tally step (TALLY.md) will count.

NOTE: only the standard library is used - no pip install needed.
"""

import json
import os
import sys
import urllib.parse
import urllib.request

# Graph API version. Bump this when Meta deprecates it (they do, ~yearly).
GRAPH_VERSION = "v21.0"
BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"


def get(path, params):
    """GET {BASE}/{path}?{params} and return parsed JSON, or raise with the
    Graph error body (Graph errors are informative - read them)."""
    url = f"{BASE}/{path}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"\nGRAPH ERROR {e.code} on /{path}:\n{body}\n", file=sys.stderr)
        raise


def main():
    token = os.environ.get("FB_PAGE_TOKEN")
    page_id = os.environ.get("FB_PAGE_ID")
    post_id = os.environ.get("FB_POST_ID")  # optional

    if not token:
        sys.exit("Set FB_PAGE_TOKEN (a page access token with pages_read_engagement).")

    # 1) Resolve which post to read. If FB_POST_ID given, use it; else grab the
    #    Page's most recent post.
    if not post_id:
        if not page_id:
            sys.exit("Set FB_PAGE_ID, or set FB_POST_ID to target a specific post.")
        print(f"Fetching latest post on page {page_id} ...")
        feed = get(f"{page_id}/posts", {
            "fields": "id,message,created_time",
            "limit": 1,
            "access_token": token,
        })
        posts = feed.get("data", [])
        if not posts:
            sys.exit("No posts found on this page. Post something first, then re-run.")
        post = posts[0]
        post_id = post["id"]
        print(f"  latest post: {post_id}")
        print(f"  posted:      {post.get('created_time')}")
        msg = (post.get("message") or "").strip().replace("\n", " ")
        print(f"  text:        {msg[:120]}{'...' if len(msg) > 120 else ''}")
    else:
        print(f"Reading comments on specified post {post_id} ...")

    # 2) Pull comments (paginated). This is the load-bearing call.
    print("\n--- COMMENTS ---")
    count = 0
    params = {
        "fields": "id,from{name,id},message,created_time,like_count",
        "filter": "stream",     # all comments, not just top-level "toplevel"
        "limit": 100,
        "access_token": token,
    }
    next_url = f"{BASE}/{post_id}/comments?" + urllib.parse.urlencode(params)

    while next_url:
        try:
            with urllib.request.urlopen(next_url, timeout=30) as r:
                page = json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            print(f"\nGRAPH ERROR {e.code} reading comments:\n{body}", file=sys.stderr)
            print("\nCommon causes: token lacks pages_read_engagement, token "
                  "expired, or it's a user token not a PAGE token.", file=sys.stderr)
            sys.exit(1)

        for c in page.get("data", []):
            count += 1
            who = (c.get("from") or {}).get("name", "[name hidden]")
            # NOTE: 'from' is often null now unless the commenter granted access;
            # the tally can still count the message text even without a name.
            text = (c.get("message") or "").replace("\n", " ")
            print(f"{count:>3}. {who}: {text}")

        next_url = (page.get("paging") or {}).get("next")

    print(f"\n--- {count} comment(s) read. ---")
    if count == 0:
        print("Zero comments. Either the post has none yet, or (if you expected "
              "some) the token/permission is wrong. Test on a post you know has "
              "comments.")
    else:
        print("SUCCESS: comment-read works. This is exactly what the tally step "
              "will ingest. Phase 0 link #1 is proven.")


if __name__ == "__main__":
    main()
