#!/usr/bin/env python3
"""Warm CLIP media-search server — keeps the fastembed model + vector store in
memory so image retrieval is sub-second (the CLI pays ~1.5s of cold model load
on every call; this pays it once at startup).

Endpoints (localhost only):
  GET /find?q=<text or image-path>&k=8   -> JSON {ms, count, results:[{path,score,match,annotation,tags}]}
  GET /health                            -> {ok, images, warm}

The store is reloaded automatically when media_img_vecs.npy changes on disk, so
`./media add ...` is picked up without restarting the server.
"""
import os, sys, json, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clip_media as cm

HOST = "127.0.0.1"
PORT = int(os.environ.get("CLIP_SERVER_PORT", "8477"))
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


class _S:
    store = None
    mtime = -1


def _store():
    """Return the vector store, reloading it if the .npy changed (new images added)."""
    try:
        m = os.path.getmtime(cm.VIMG)
    except OSError:
        m = 0
    if _S.store is None or m != _S.mtime:
        _S.store = cm.MediaStore()
        _S.mtime = m
    return _S.store


def _search(q, k):
    is_img = os.path.exists(q) and q.lower().endswith(IMG_EXTS)
    qvec = cm.embed_image(q) if is_img else cm.embed_text(q)
    return _store().search(qvec, k)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            self._json(200, {"ok": True, "images": len(_store().meta), "warm": cm._img is not None})
            return
        if u.path != "/find":
            self._json(404, {"error": "use /find?q=...&k=8"})
            return
        qs = parse_qs(u.query)
        q = (qs.get("q") or [""])[0]
        k = int((qs.get("k") or ["8"])[0])
        if not q:
            self._json(400, {"error": "missing q"})
            return
        t = time.time()
        try:
            hits = _search(q, k)
        except Exception as e:
            self._json(500, {"error": str(e)})
            return
        self._json(200, {"ms": round((time.time() - t) * 1000, 1), "count": len(hits), "results": hits})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("warming CLIP model + store …", flush=True)
    cm._models()          # load fastembed image+text models once
    _store()              # load vectors once
    print(f"CLIP media server ready on {HOST}:{PORT} — {len(_store().meta)} images indexed", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
