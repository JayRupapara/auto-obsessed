"""Offline tests for scripts/publish_instagram.py with a fake Instagram. Run: python tests/test_publish.py"""
import datetime as dt
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pub", root / "scripts" / "publish_instagram.py")
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)


def iso(minutes_from_now):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes_from_now)).replace(microsecond=0).isoformat()


def run_main(queue_items, fake_call=None, env_token="T", args=("x",)):
    tmp = Path(tempfile.mkdtemp())
    q = tmp / "queue.json"
    q.write_text(json.dumps({"items": queue_items}), encoding="utf-8")
    pub.QUEUE = q
    sys.argv = list(args)
    patches = [mock.patch.dict("os.environ", {"INSTAGRAM_ACCESS_TOKEN": env_token}),
               mock.patch.object(pub, "check_video", lambda u: None),
               mock.patch.object(pub.time, "sleep", lambda s: None)]
    if fake_call:
        patches.append(mock.patch.object(pub, "call", fake_call))
    for p in patches:
        p.start()
    try:
        rc = pub.main()
    finally:
        for p in patches:
            p.stop()
    data = json.loads(q.read_text(encoding="utf-8"))
    shutil.rmtree(tmp)
    return rc, data


def good_call(calls):
    def fake(token, method, path, **p):
        calls.append(path)
        if path == "me": return {"username": "obsessed.jay", "user_id": "42"}
        if path == "42/media": return {"id": "C1"}
        if path == "C1": return {"status_code": "FINISHED"}
        if path == "42/media_publish": return {"id": "M1"}
        if path == "M1": return {"permalink": "https://www.instagram.com/reel/XYZ/"}
        raise AssertionError(path)
    return fake


item = lambda id, minutes, status="pending", **kw: {"id": id, "publish_at": iso(minutes), "status": status,
                                                    "video_url": "https://x/v.mp4", "caption": "c", **kw}

# 1. nothing due, nothing overdue -> quiet success
rc, d = run_main([item("a", +60)])
assert rc == 0 and d["items"][0]["status"] == "pending"

# 2. due 5 minutes ago and posted -> success, no alarm (only 5 min late)
calls = []
rc, d = run_main([item("a", -5)], good_call(calls))
assert rc == 0 and d["items"][0]["status"] == "published" and d["items"][0]["late_alerted"] is True, d

# 3. due 90 minutes ago, posted now -> the post works but the run FAILS with a LATE POST alarm (email), once
calls = []
rc, d = run_main([item("a", -90)], good_call(calls))
assert rc == 1 and d["items"][0]["status"] == "published" and d["items"][0]["late_alerted"] is True, d
rc2, d2 = run_main(d["items"])  # next run: already reported
assert rc2 == 0

# 4. overdue and cannot post (token missing) -> run fails with OVERDUE
rc, d = run_main([item("a", -40)], good_call([]), env_token="")
assert rc == 1 and d["items"][0]["status"] == "pending"

# 5. overdue but still inside the 20-minute grace -> no alarm if it posts
rc, d = run_main([item("a", -10)], good_call([]))
assert rc == 0

# 6. dry run never fails and never posts
calls = []
rc, d = run_main([item("a", -90)], good_call(calls), args=("x", "--dry-run"))
assert rc == 0 and "42/media_publish" not in calls and d["items"][0]["status"] == "pending"

# 7. an already-published, already-reported item stays quiet forever
rc, d = run_main([item("a", -500, "published", published_at=iso(-480), late_alerted=True)])
assert rc == 0

# 8. a failed item that is old keeps alarming
rc, d = run_main([item("a", -300, "failed", error="instagram_http_400_code_9_sub_?")])
assert rc == 1

print("ALL ALARM TESTS PASSED")
