"""Scan reflex — a photo of a document files itself.

The owner: "if I send you a photo here, you need to detect if it contains
a document, extract the document, and save it into the KB with proper
annotation. Docs on a white paper should be saved as a PDF. Others as jpeg. A
photo may contain a white document and a non-white one."

local-ai/autoscan.py does the seeing (find every sheet in the frame, decide
whether it is a document at all, rectify it, and let the paper choose the
format). This is the other half: it runs on every inbound photo and files what
comes out, WITHOUT a model turn deciding anything — same reasoning as
kb_file_reflex, whose whole point was that a filing decision does not deserve
thirty seconds.

Three judgements live here rather than in autoscan, because they are about the
chat, not about the pixels:

  * WHEN NOT TO FIRE. A caption that asks a question ("what does this say?") is
    a request to READ the photo, not to file it — that belongs to Claude, who
    can also scan deliberately. Filing a document is not destructive, but
    swallowing a question is.
  * WHAT TO CALL IT. The caption, when there is one, is the owner naming the
    document; it becomes both the filename and the annotation. With no caption
    the vision annotator writes one, because an image filed without words is
    invisible to every text search we have.
  * WHERE IT LANDS. PDFs go to knowledge-base/from-scans with a text sidecar —
    these are PHOTOGRAPHED pages, so the PDF has no text layer and pdftotext
    finds nothing; the sidecar carrying the annotation is the only thing that
    makes the document findable by words. Both kinds also go through `media add`
    for CLIP search. Indexing runs behind the answer.
"""
import os
import re
import subprocess
import sys
import threading
import time

import tgconf as C
import projects_mode

sys.path.insert(0, os.environ.get("TG_AUTOSCAN_DIR",
                                  os.path.join(C.DST_ROOT, "local-ai")))

DEST = os.path.join(C.DST_ROOT, "knowledge-base", "from-scans")
WORK_DIR = os.path.join(C.STATE_DIR, "autoscan")
PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# A caption shaped like a question is about the photo, not a name for it.
QUESTION = re.compile(r"\?|^\s*(what|what's|whats|who|why|when|where|which|how|"
                      r"is|are|can|could|should|would|do|does|did|tell|explain|"
                      r"read|translate|check|look)\b", re.I)


def _log(m):
    print(f"[scan_reflex] {m}", flush=True)


def should_try(path, caption):
    """Is this a photo we should look for documents in?"""
    if not path or os.path.splitext(path)[1].lower() not in PHOTO_EXTS:
        return False
    cap = (caption or "").strip()
    return not (cap and (QUESTION.search(cap) or len(cap) > 120))


def _slug(caption):
    s = re.sub(r"[^a-z0-9]+", "-", (caption or "").lower()).strip("-")
    return (s[:48].rstrip("-") or "scan")


def _annotate(preview_path, caption):
    """The words that make this document findable. The owner's caption when he
    gave one; otherwise ask the vision model to write one."""
    cap = (caption or "").strip()
    if cap:
        return cap, False
    # The free VL endpoint intermittently returns nothing (same failure the
    # private describe path retries around) — and here an empty reply means a
    # document filed with no words at all, so retry before giving up.
    for i in range(3):
        ann = projects_mode.annotate_image(preview_path)
        if ann and ann.strip():
            return ann.strip(), True
        time.sleep(1.5 * (i + 1))
    return "", True


def _sidecar(pdf_path, annotation, source_photo):
    """Text next to a text-less PDF. Written even when the annotation is empty:
    the filename and date are still worth indexing, and a missing sidecar would
    make the document unfindable by anything but its own name."""
    md = os.path.splitext(pdf_path)[0] + ".md"
    with open(md, "w", encoding="utf-8") as fh:
        fh.write(f"# {os.path.basename(pdf_path)}\n\n"
                 f"Source file: {os.path.basename(pdf_path)}\n\n"
                 f"_Extracted automatically from a photo "
                 f"({os.path.basename(source_photo)}) on "
                 f"{time.strftime('%Y-%m-%d')}._\n\n"
                 + (annotation.strip() + "\n" if annotation.strip() else
                    "(no annotation — reply with one to make this searchable)\n"))
    return md


def _index_later(filed):
    """CLIP indexing and the KB index refresh, behind the answer. Nothing here
    may fail loudly: the documents are already filed, and a failed index is a
    search that misses them, not work that was lost."""
    try:
        for f in filed:
            cmd = [C.MEDIA, "add", f["path"]]
            if f["annotation"]:
                cmd += ["--annotation", f["annotation"][:600]]
            if f["kind"] == "pdf":
                cmd += ["--no-rag"]      # a photographed page has no text layer
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                _log(f"media add failed for {f['path']}: "
                     f"{(r.stderr or r.stdout).strip()[:200]}")
        if any(f["kind"] == "pdf" for f in filed):
            subprocess.run([C.KB, "index"], capture_output=True, timeout=600)
    except Exception as e:
        _log(f"background indexing failed: {e}")


def scan_and_file(path, caption=""):
    """Find every document in `path`, extract and file each one.

    THE ORIGINAL PHOTO IS NEVER TOUCHED (the owner) — not modified,
    not moved, not deleted. Extraction is lossy and one-way: the crop can take
    the wrong quad, the whitening can eat a faint pencil note, and the inbox
    photo is the only full-resolution copy of what he actually sent. Everything
    here writes to WORK_DIR and lands in DEST; `path` is opened read-only, and
    the check at the end makes that a guarantee rather than a habit.

    Returns a list of {path, kind, white, size, annotation, auto} — empty when
    the photo holds no document, which is the common case and costs ~1s.
    """
    import autoscan                        # numpy/Pillow — imported on demand

    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(DEST, exist_ok=True)
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(caption)}"
    before = (os.path.getsize(path), os.path.getmtime(path))
    found = autoscan.scan(path, WORK_DIR, stem=stem, preview_dir=WORK_DIR)
    filed = []
    for f in found:
        annotation, auto = _annotate(f["preview"], caption)
        dest = os.path.join(DEST, os.path.basename(f["path"]))
        n = 1
        while os.path.exists(dest):
            root, ext = os.path.splitext(dest)
            dest, n = f"{root}-{n}{ext}", n + 1
        os.replace(f["path"], dest)
        if f["kind"] == "pdf":
            _sidecar(dest, annotation, path)
        filed.append({"path": dest, "kind": f["kind"], "white": f["white"],
                      "size": os.path.getsize(dest), "annotation": annotation,
                      "auto": auto})
        if f["preview"] != f["path"] and os.path.exists(f["preview"]):
            os.remove(f["preview"])
    # Loud if the original ever changed: silence here would mean the one
    # full-resolution copy was damaged and nobody found out until it was needed.
    if not os.path.exists(path) or (os.path.getsize(path),
                                    os.path.getmtime(path)) != before:
        _log(f"WARNING: the source photo changed during extraction: {path}")
    if filed:
        threading.Thread(target=_index_later, args=(filed,), daemon=True).start()
    return filed


def _describe(filed):
    lines = []
    for f in filed:
        stock = "white paper" if f["white"] else "coloured/dark stock"
        lines.append(f"• `{os.path.basename(f['path'])}` — {stock} → "
                     f"{f['kind'].upper()}, {f['size'] / 1024:.0f} KB")
        ann = (f["annotation"] or "").strip().replace("\n", " ")
        if ann:
            lines.append(f"  {'auto-annotation' if f['auto'] else 'annotation'}: "
                         f"_{ann[:300]}{'…' if len(ann) > 300 else ''}_")
        else:
            lines.append("  no annotation — reply with one to make it searchable")
    return "\n".join(lines)


def try_handle(chat_id, path, caption, send):
    """The reflex. Returns a summary string if the photo was a document and has
    been filed (the gateway then stops), else None -> normal handling."""
    if not should_try(path, caption):
        return None
    try:
        filed = scan_and_file(path, caption)
    except Exception as e:
        _log(f"scan failed for {path}: {e}")
        return None                      # no document extracted -> normal handling
    if not filed:
        return None
    head = (f"📄 Found {len(filed)} document{'s' if len(filed) > 1 else ''} in that "
            f"photo and filed {'them' if len(filed) > 1 else 'it'} in "
            f"`knowledge-base/from-scans/`:")
    send(chat_id, head + "\n" + _describe(filed) +
         "\n\nIndexing for search now — searchable in a moment.")
    return ("scan reflex: filed " +
            ", ".join(f"{os.path.basename(f['path'])} ({f['kind']})" for f in filed))


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        cap = " ".join(sys.argv[2:])
        print(f"should_try -> {should_try(sys.argv[1], cap)}")
        for r in scan_and_file(sys.argv[1], cap):
            print(f"  {r['kind']}  {r['size'] / 1024:.0f}KB  {r['path']}\n"
                  f"     {r['annotation'][:200]}")
