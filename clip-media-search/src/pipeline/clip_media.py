#!/usr/bin/env python3
"""CLIP image search for a media/knowledge-base folder — neural, annotation-aware.

Each image is stored with TWO vectors in the joint CLIP ViT-B/32 space (fastembed/
ONNX, CPU):
  1. the image embedding             -> visual neural search
  2. the user's ANNOTATION embedding -> human-curated text search
A text query (embedded into the same space) is matched against BOTH and the better
score wins — so an image is found by what it looks like OR by what you said about it.
The annotation text is also saved verbatim. PDFs (optional) are handed off to a
separate text-RAG pipeline via the DOCPIPE hook. Everything stays local.

The CLIP backend is swappable: point _models() at any fastembed-compatible image +
text embedding pair that share one vector space (see README).

Usage:
  clip_media.py add  <image|pdf|dir> --annotation "what this shows" [--tags "a,b"]
  clip_media.py find "natural-language query" [-k 8]
  clip_media.py list
"""
import os, sys, json, shutil, argparse, subprocess, datetime
import numpy as np

# Paths derive from this script's location by default; override via env vars.
BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # skill src root
MEDIA_DIR = os.environ.get("MEDIA_DIR", os.path.join(BASE_DIR, "media"))
STORE_DIR = os.environ.get("CLIP_STORE_DIR", os.path.join(BASE_DIR, "store"))
VIMG = os.path.join(STORE_DIR, "media_img_vecs.npy")
VANN = os.path.join(STORE_DIR, "media_ann_vecs.npy")
META = os.path.join(STORE_DIR, "media_meta.json")
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VID_EXTS = {".mp4", ".mov", ".webm", ".m4v"}  # indexed by mid-frame (requires ffmpeg)
# Optional: path to an external doc-RAG pipeline that ingests PDFs. If unset, PDFs
# are skipped (this skill only handles images).
DOCPIPE = os.environ.get("DOCPIPE")
# CLIP model backend (swappable). Any fastembed image/text pair in a shared space.
IMG_MODEL = os.environ.get("CLIP_IMG_MODEL", "Qdrant/clip-ViT-B-32-vision")
TXT_MODEL = os.environ.get("CLIP_TXT_MODEL", "Qdrant/clip-ViT-B-32-text")
DIM = int(os.environ.get("CLIP_DIM", "512"))

_img = _txt = None


def _models():
    global _img, _txt
    if _img is None:
        from fastembed import ImageEmbedding, TextEmbedding
        _img = ImageEmbedding(IMG_MODEL)
        _txt = TextEmbedding(TXT_MODEL)
    return _img, _txt


def _norm(v):
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


def embed_image(path):
    img, _ = _models()
    return _norm(list(img.embed([path]))[0])


def embed_text(text):
    _, txt = _models()
    return _norm(list(txt.embed([text]))[0])


class MediaStore:
    def __init__(self):
        os.makedirs(STORE_DIR, exist_ok=True)
        if os.path.exists(VIMG) and os.path.exists(META):
            self.img = np.load(VIMG)
            self.ann = np.load(VANN)
            self.meta = json.load(open(META))
        else:
            self.img = np.zeros((0, DIM), np.float32)
            self.ann = np.zeros((0, DIM), np.float32)
            self.meta = []

    def _remove(self, path):
        keep = [i for i, m in enumerate(self.meta) if m["path"] != path]
        self.img = self.img[keep] if keep else np.zeros((0, DIM), np.float32)
        self.ann = self.ann[keep] if keep else np.zeros((0, DIM), np.float32)
        self.meta = [self.meta[i] for i in keep]

    def add(self, path, img_vec, ann_vec, annotation, tags):
        self._remove(path)  # idempotent re-index
        has_ann = ann_vec is not None
        self.meta.append({"path": path, "annotation": annotation, "tags": tags,
                          "has_ann": has_ann,
                          "added": datetime.datetime.now().isoformat(timespec="seconds")})
        self.img = np.vstack([self.img, img_vec])
        self.ann = np.vstack([self.ann, ann_vec if has_ann else np.zeros(DIM, np.float32)])

    def search(self, qvec, k):
        if not self.meta:
            return []
        q = _norm(qvec)
        sim_img = self.img @ q
        sim_ann = self.ann @ q
        has = np.array([m["has_ann"] for m in self.meta])
        final = np.where(has, np.maximum(sim_img, sim_ann), sim_img)
        out = []
        for i in np.argsort(-final)[:k]:
            why = "annotation" if (has[i] and sim_ann[i] >= sim_img[i]) else "visual"
            out.append({**self.meta[i], "score": float(final[i]), "match": why})
        return out

    def save(self):
        np.save(VIMG, self.img)
        np.save(VANN, self.ann)
        json.dump(self.meta, open(META, "w"), indent=2)


def index_image(path, annotation="", tags=""):
    os.makedirs(MEDIA_DIR, exist_ok=True)
    dest = os.path.join(MEDIA_DIR, os.path.basename(path))
    if os.path.abspath(path) != os.path.abspath(dest):
        shutil.copy2(path, dest)
    img_vec = embed_image(dest)
    ann_vec = embed_text(annotation) if annotation.strip() else None
    s = MediaStore()
    s.add(dest, img_vec, ann_vec, annotation, tags)
    s.save()
    return dest


def index_video(path, annotation="", tags=""):
    """Index a video by its middle frame (CLIP sees one representative frame; the
    annotation carries the rest). Meta path points at the VIDEO, so search hits
    return the clip itself. Requires ffmpeg/ffprobe on PATH."""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    dest = os.path.join(MEDIA_DIR, os.path.basename(path))
    if os.path.abspath(path) != os.path.abspath(dest):
        shutil.copy2(path, dest)
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", dest], capture_output=True, text=True)
    mid = max(float(dur.stdout.strip() or 0) / 2, 0.1)
    frame = os.path.join(STORE_DIR, "_vid_frame.jpg")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(mid), "-i", dest,
                    "-frames:v", "1", frame], check=True)
    img_vec = embed_image(frame)
    os.remove(frame)
    ann_vec = embed_text(annotation) if annotation.strip() else None
    s = MediaStore()
    s.add(dest, img_vec, ann_vec, annotation, tags)
    s.save()
    return dest


def cmd_add(a):
    files = []
    for p in a.paths:
        if os.path.isdir(p):
            for root, _, fs in os.walk(p):
                files += [os.path.join(root, f) for f in fs]
        else:
            files.append(p)
    for p in files:
        ext = os.path.splitext(p)[1].lower()
        if ext in IMG_EXTS:
            d = index_image(p, a.annotation or "", a.tags or "")
            note = "with annotation" if a.annotation else "NO annotation (visual-only)"
            print(f"  image indexed (CLIP, {note}): {os.path.basename(d)}")
        elif ext in VID_EXTS:
            d = index_video(p, a.annotation or "", a.tags or "")
            note = "with annotation" if a.annotation else "NO annotation (visual-only)"
            print(f"  video indexed (CLIP mid-frame, {note}): {os.path.basename(d)}")
        elif ext == ".pdf":
            if not DOCPIPE:
                print(f"  skip pdf (no DOCPIPE configured): {p}")
                continue
            os.makedirs(MEDIA_DIR, exist_ok=True)
            dest = os.path.join(MEDIA_DIR, os.path.basename(p))
            if os.path.abspath(p) != os.path.abspath(dest):
                shutil.copy2(p, dest)
            subprocess.run([DOCPIPE, "ingest", dest])
            print(f"  pdf indexed (text RAG): {os.path.basename(dest)}")
        else:
            print(f"  skip (unsupported): {p}")


def cmd_find(a):
    # Visual search ("reverse image" mode): if the query is a path to an image file,
    # embed it as an IMAGE and search by what it looks like. Otherwise it's text.
    is_img = os.path.isfile(a.query) and os.path.splitext(a.query)[1].lower() in IMG_EXTS
    qvec = embed_image(a.query) if is_img else embed_text(a.query)
    hits = MediaStore().search(qvec, a.k)
    if not hits:
        print("(no images indexed yet)"); return
    label = f"image {os.path.basename(a.query)}" if is_img else a.query
    print(f'Top {len(hits)} for: "{label}"')
    for h in hits:
        ann = f"  — {h['annotation']}" if h.get("annotation") else ""
        print(f"  {h['score']:.3f} [{h['match']:>10}]  {h['path']}{ann}")


def cmd_list(a):
    s = MediaStore()
    print(f"{len(s.meta)} images in the CLIP index:")
    for m in s.meta:
        print(f"  {os.path.basename(m['path'])}  ann={m.get('annotation','')!r}  tags={m.get('tags','')!r}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("add"); g.add_argument("paths", nargs="+"); g.add_argument("--annotation", default=""); g.add_argument("--tags", default=""); g.set_defaults(func=cmd_add)
    g = sub.add_parser("find"); g.add_argument("query"); g.add_argument("-k", type=int, default=8); g.set_defaults(func=cmd_find)
    g = sub.add_parser("list"); g.set_defaults(func=cmd_list)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
