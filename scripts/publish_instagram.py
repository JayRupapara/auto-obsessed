"""
Publish due Instagram Reels from schedule/queue.json. Runs on GitHub Actions.

- Needs the secret INSTAGRAM_ACCESS_TOKEN (Instagram API with Instagram Login, for @obsessed.jay).
- Publishes an item only when status is "pending" (or "container_created") and publish_at has passed.
- Never creates a second container for the same item; a failed publish retries the same container.
- Checks the video link first (HTTP 200, video content type) and the account name.
- Prints no token, no response body. Errors are short codes only.

    python scripts/publish_instagram.py            # publish what is due
    python scripts/publish_instagram.py --dry-run  # show what is due, send nothing
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

API = "https://graph.instagram.com/v25.0"
EXPECTED_USER = "obsessed.jay"
QUEUE = Path(__file__).resolve().parents[1] / "schedule" / "queue.json"
MAX_ATTEMPTS = 3


def now():
    return dt.datetime.now(dt.timezone.utc)


def parse(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def save(data):
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(QUEUE)


class Failure(Exception):
    pass


def call(token, method, path, **params):
    try:
        r = requests.request(method, f"{API}/{path}", headers={"Authorization": "Bearer " + token},
                             params=params if method == "GET" else None, data=params if method != "GET" else None,
                             timeout=(15, 90))
        body = r.json()
    except (requests.RequestException, ValueError):
        raise Failure("network_or_parse_error") from None
    if not r.ok or "error" in body:
        e = body.get("error", {})
        raise Failure(f"instagram_http_{r.status_code}_code_{e.get('code', '?')}_sub_{e.get('error_subcode', '?')}")
    return body


def check_video(url):
    if not url.startswith("https://"):
        raise Failure("video_url_not_https")
    try:
        r = requests.head(url, allow_redirects=True, timeout=30)
    except requests.RequestException:
        raise Failure("video_url_unreachable") from None
    if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("video/"):
        raise Failure(f"video_url_bad_response_{r.status_code}")


def publish_item(token, item):
    check_video(item["video_url"])
    me = call(token, "GET", "me", fields="user_id,username")
    if me.get("username", "").lower() != EXPECTED_USER:
        raise Failure("wrong_instagram_account")
    account = str(me.get("user_id") or me.get("id"))
    if not item.get("container_id"):
        item["container_id"] = call(token, "POST", f"{account}/media", media_type="REELS", video_url=item["video_url"],
                                    caption=item["caption"], share_to_feed="true")["id"]
        item["status"] = "container_created"
        save_state()
    deadline = time.monotonic() + 600
    while True:
        status = call(token, "GET", item["container_id"], fields="status_code").get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise Failure("container_" + status.lower())
        if time.monotonic() > deadline:
            raise Failure("still_processing")
        time.sleep(15)
    result = call(token, "POST", f"{account}/media_publish", creation_id=item["container_id"])
    item["media_id"] = result["id"]
    item["status"] = "published"
    item["published_at"] = now().replace(microsecond=0).isoformat()
    try:
        item["permalink"] = call(token, "GET", result["id"], fields="permalink")["permalink"]
    except Failure:
        pass
    item.pop("error", None)


STATE = {}


def save_state():
    save(STATE["data"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check-token", action="store_true", help="Verify the secret works for @obsessed.jay; posts nothing")
    args = ap.parse_args()
    if args.check_token:
        token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
        if not token:
            print("Token check FAILED: the INSTAGRAM_ACCESS_TOKEN secret is missing or empty.")
            return 1
        try:
            me = call(token, "GET", "me", fields="user_id,username")
        except Failure as e:
            print(f"Token check FAILED: {e}")
            return 1
        if me.get("username", "").lower() != EXPECTED_USER:
            print("Token check FAILED: the token works but belongs to a different account.")
            return 1
        print(f"Token OK for @{EXPECTED_USER}. Nothing was posted.")
        return 0
    data = json.loads(QUEUE.read_text(encoding="utf-8"))
    STATE["data"] = data
    due = [i for i in data["items"] if i["status"] in ("pending", "container_created") and parse(i["publish_at"]) <= now()
           and i.get("attempts", 0) < MAX_ATTEMPTS]
    print(f"{len(due)} item(s) due of {len(data['items'])}")
    for i in data["items"]:
        print(f"  {i['id']}: {i['status']} at {i['publish_at']}")
    if args.dry_run or not due:
        return 0
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    if not token:
        print("INSTAGRAM_ACCESS_TOKEN secret is missing.")
        return 1
    failed = False
    for item in due:
        item["attempts"] = item.get("attempts", 0) + 1
        try:
            publish_item(token, item)
            print(f"published {item['id']} -> {item.get('permalink', item.get('media_id'))}")
        except Failure as e:
            item["error"] = str(e)
            if item["attempts"] >= MAX_ATTEMPTS and item["status"] != "published":
                item["status"] = "failed"
            failed = True
            print(f"FAILED {item['id']}: {e}")
        save_state()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
