#!/usr/bin/env python3
"""Retrieve KB images by embedding search (warm CLIP server) and SEND them to the
Telegram chat — this is the 'show me pictures' path, sub-second when the server is warm.

Usage:
  show_media.py "voxeljet print head" [-k 3] [--min 0.25] [--ratio 0.8] [--chat <id>]
A query that is a path to an image does reverse (visual) search.

Only hits scoring >= ratio * top-hit-score are sent (default 0.8), so one strong
match doesn't drag loosely-related images into the chat. --ratio 0 restores
send-everything behavior.
"""
import sys, os, json, argparse, urllib.parse, urllib.request

sys.path.insert(0, os.path.expanduser(
    os.environ.get("TELEGRAM_GATEWAY_DIR", "~/workspace/telegram")))
import tg_api

CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")  # your Telegram chat id (or pass --chat)
SERVER = "http://127.0.0.1:8477/find"


def find(q, k):
    url = SERVER + "?" + urllib.parse.urlencode({"q": q, "k": k})
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("-k", type=int, default=3)
    ap.add_argument("--min", type=float, default=0.0, help="drop hits below this score")
    ap.add_argument("--ratio", type=float, default=0.8,
                    help="drop hits scoring below this fraction of the top hit")
    ap.add_argument("--chat", default=CHAT)
    a = ap.parse_args()
    d = find(a.query, a.k)
    hits = [r for r in d["results"] if r["score"] >= a.min]
    if hits:
        cutoff = hits[0]["score"] * a.ratio
        kept = [r for r in hits if r["score"] >= cutoff]
        if len(kept) < len(hits):
            print(f"dropped {len(hits) - len(kept)} low-relevance hit(s) "
                  f"(score < {cutoff:.2f})")
        hits = kept
    if not hits:
        tg_api.send_message(a.chat, f"No KB images found for: {a.query}")
        print("no hits"); return
    for r in hits:
        cap = f"{os.path.basename(r['path'])}  ({r['score']:.2f} · {r['match']})"
        is_vid = os.path.splitext(r["path"])[1].lower() in {".mp4", ".mov", ".webm", ".m4v"}
        method, field = ("sendVideo", "video") if is_vid else ("sendPhoto", "photo")
        tg_api._call(method, _files={field: open(r["path"], "rb")},
                     chat_id=a.chat, caption=cap)
        print("sent", r["path"])
    print(f"server {d['ms']}ms · sent {len(hits)}")


if __name__ == "__main__":
    main()
