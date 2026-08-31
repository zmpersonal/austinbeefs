#!/usr/bin/env python3
"""
POST CARD - take a rendered PNG + caption, publish it to the Facebook Page via
Blotato, and capture the native Facebook post-id. This is cycle steps 8 and 9.

    from post_card import post_card
    result = post_card("queue/round_14.png", caption, page_id, account_id)
    # -> {"fb_post_id": "1283...._1783....", ...}  <- store in current_round.json

THE THREE STEPS (all confirmed against the live API, none guessed)

  1. MEDIA   POST /v2/media   {"url": "data:image/png;base64,<b64>"}
             -> {"url": "<blotato-hosted-url>", ...}
             Blotato accepts an inline data: URI and re-hosts the image on its
             own CDN. The PNG therefore NEVER leaves this machine and needs no
             public host, no bucket, no third-party image service, and no extra
             secrets. (We researched R2/catbox/imgur before probing this - all
             of it turned out to be unnecessary.)

  2. POST    POST /v2/posts   nested {"post": {accountId, content, target}}
             -> {"postSubmissionId": "<uuid>"}

  3. CAPTURE GET /v2/posts/{postSubmissionId} until status == "published",
             then parse the FB post-id from the publicUrl tail.

G6 - THE CAPTURE LOGIC IS IMPORTED, NOT COPIED
  Step 3 calls prove_blotato_post.poll_for_fb_post_id() directly. That function
  is the single source of truth for the fail-loud rules (404/5xx keep polling,
  401/403 halt, "failed" halts, unknown status never counts as published,
  timeout halts, id must match ^\\d+_\\d+$). Duplicating it here would create two
  copies that drift - and a drifted post-id capture is exactly the failure G6
  exists to prevent.

  Caveat worth knowing: that module's halt() calls sys.exit(1), so a capture
  failure terminates the process rather than raising. That IS fail-loud and is
  correct for the cron, but it means post_card() cannot be wrapped in a
  try/except by a caller. Converting halt() to raise a PostError is a small
  follow-up if you want the cycle to handle it more gracefully.

SAFETY
  This posts PUBLICLY to a live Page. There is no dry default: you must pass a
  real PNG and caption. Use --dry-run to exercise the upload half only (media
  upload is harmless - it does not appear on the Page).
"""

import argparse
import base64
import json
import mimetypes
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
sys.path.insert(0, HERE)

# Single source of truth for the Blotato surface AND the G6 capture rules.
from prove_blotato_post import (      # noqa: E402
    BLOTATO_BASE,
    BLOTATO_POST_PATH,
    http,
    halt,
    poll_for_fb_post_id,
)
from render_card import png_dimensions  # noqa: E402  (reuse the IHDR reader)

BLOTATO_MEDIA_PATH = "/media"          # POST -> {"url": "<hosted>"} (confirmed)

# base64 inflates by ~33%. A 1080x1080 card is ~100-180KB -> ~240KB encoded,
# nowhere near any sane body limit; warn well before anything could be refused.
WARN_ENCODED_BYTES = 4 * 1024 * 1024


def _read_png_as_data_uri(png_path):
    """Read the PNG and return (data_uri, info_dict). Fails loud on anything
    that is not a real, non-empty PNG - posting a broken image to a live page
    is worse than not posting."""
    if not os.path.isfile(png_path):
        halt(f"No such file: {png_path}")
    size = os.path.getsize(png_path)
    if size == 0:
        halt(f"Image is zero bytes: {png_path}")

    with open(png_path, "rb") as f:
        raw = f.read()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        halt(f"Not a PNG (bad magic bytes): {png_path}")

    w, h = png_dimensions(png_path)     # reused from render_card
    b64 = base64.b64encode(raw).decode("ascii")
    mime = mimetypes.guess_type(png_path)[0] or "image/png"
    data_uri = f"data:{mime};base64,{b64}"

    if len(data_uri) > WARN_ENCODED_BYTES:
        print(f"  WARNING: encoded payload is {len(data_uri):,} bytes - large "
              f"for a JSON body.", file=sys.stderr)

    return data_uri, {"bytes": size, "width": w, "height": h,
                      "encoded_bytes": len(data_uri)}


def upload_media(png_path, api_key):
    """STEP 1. Upload the PNG inline as a data: URI. Returns Blotato's hosted
    URL. The data URI itself is never logged - it is a ~240KB blob."""
    data_uri, info = _read_png_as_data_uri(png_path)
    print(f"Uploading media ({info['width']}x{info['height']}, "
          f"{info['bytes']:,} bytes -> {info['encoded_bytes']:,} encoded) ...")

    headers = {"Content-Type": "application/json", "blotato-api-key": api_key}
    _, resp = http("POST", BLOTATO_BASE + BLOTATO_MEDIA_PATH, headers,
                   {"url": data_uri})

    hosted = resp.get("url")
    if not hosted:
        halt("Blotato accepted the media upload but returned no hosted url.\n"
             f"  response: {json.dumps(resp, indent=2)}")
    print(f"  hosted at: {hosted}")
    return hosted


def create_post(caption, hosted_url, page_id, account_id, api_key):
    """STEP 2. Create the Facebook post. Returns the postSubmissionId."""
    payload = {
        "post": {
            "accountId": account_id,
            "content": {
                "text": caption,
                "mediaUrls": [hosted_url],
                "platform": "facebook",
            },
            "target": {
                "targetType": "facebook",
                "pageId": page_id,
            },
        }
    }
    print(f"Creating post on page {page_id} ...")
    headers = {"Content-Type": "application/json", "blotato-api-key": api_key}
    _, resp = http("POST", BLOTATO_BASE + BLOTATO_POST_PATH, headers, payload)

    submission_id = resp.get("postSubmissionId")
    if not submission_id:
        halt("Blotato accepted the post but returned no postSubmissionId. "
             "Without it the Facebook post-id cannot be captured (G6).\n"
             f"  response: {json.dumps(resp, indent=2)}")
    print(f"  postSubmissionId: {submission_id}")
    return submission_id


def post_card(png_path, caption, page_id, blotato_account_id, api_key=None):
    """Publish a rendered card and return the ids the next cycle needs.

    -> {"fb_post_id", "blotato_submission_id", "blotato_media_url",
        "public_url", "page_id"}

    fb_post_id is the value to store in current_round.json - it is what the
    Graph comment-read uses next cycle to tally THIS post's votes."""
    api_key = api_key or os.environ.get("BLOTATO_API_KEY")
    if not api_key:
        halt("BLOTATO_API_KEY is not set.")
    if not blotato_account_id:
        halt("No Blotato accountId given (config.blotato_fb_account_id).")
    if not page_id:
        halt("No Facebook page_id given (config.page_id).")

    hosted_url = upload_media(png_path, api_key)
    submission_id = create_post(caption, hosted_url, page_id,
                                blotato_account_id, api_key)
    # STEP 3 - imported, not reimplemented. See the G6 note in the docstring.
    fb_post_id, public_url = poll_for_fb_post_id(api_key, submission_id)

    return {
        "fb_post_id": fb_post_id,
        "blotato_submission_id": submission_id,
        "blotato_media_url": hosted_url,
        "public_url": public_url,
        "page_id": str(page_id),
    }


def main():
    cfg = {}
    cfg_path = os.path.join(ROOT, "config.json")
    if os.path.isfile(cfg_path):
        with open(cfg_path) as f:
            cfg = json.load(f)

    ap = argparse.ArgumentParser(
        description="Publish a rendered card to Facebook via Blotato.")
    ap.add_argument("--png", required=True)
    ap.add_argument("--caption", required=True)
    ap.add_argument("--page-id", default=cfg.get("page_id"))
    ap.add_argument("--account-id", default=cfg.get("blotato_fb_account_id"))
    ap.add_argument("--dry-run", action="store_true",
                    help="upload the media and print the post body, but do NOT "
                         "publish. Safe: media upload never appears on the Page.")
    args = ap.parse_args()

    for name, val in (("page-id", args.page_id), ("account-id", args.account_id)):
        if not val or str(val).startswith("YOUR_"):
            sys.exit(f"--{name} is unset or still a config.json placeholder "
                     f"({val!r}). Pass it explicitly or fill in config.json.")

    if args.dry_run:
        api_key = os.environ.get("BLOTATO_API_KEY")
        if not api_key:
            sys.exit("BLOTATO_API_KEY is not set.")
        hosted = upload_media(args.png, api_key)
        print("\n--dry-run: NOT publishing. The post body would be:")
        print(json.dumps({"post": {
            "accountId": args.account_id,
            "content": {"text": args.caption, "mediaUrls": [hosted],
                        "platform": "facebook"},
            "target": {"targetType": "facebook", "pageId": args.page_id},
        }}, indent=2, ensure_ascii=False))
        return

    result = post_card(args.png, args.caption, args.page_id, args.account_id)
    print("\nPUBLISHED.")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("\nStore fb_post_id in current_round.json - the next cycle reads "
          "THIS post's comments with it.")
    print("Reminder: if this was a test, delete the post from your Page.")


if __name__ == "__main__":
    main()
