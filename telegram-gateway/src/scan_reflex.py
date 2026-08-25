"""Scan reflex — a photo of a document files itself.

the owner, 2026-08-09: "if I send you a photo here, you need to detect if it contains
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
                                  os.path.join(C.WORKSPACE_ROOT, "local-ai")))

DEST = os.path.join(C.WORKSPACE_ROOT, "knowledge-base", "from-scans")
WORK_DIR = os.path.join(C.STATE_DIR, "autoscan")
PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# A caption shaped like a question is about the photo, not a name for it.
QUESTION = re.compile(r"\?|^\s*(what|what's|whats|who|why|when|where|which|how|"
                      r"is|are|can|could|should|would|do|does|did|tell|explain|"
                      r"read|translate|check|look)\b", re.I)

# Neither is a caption shaped like a SENTENCE. 2026-08-15: the owner sent a
# screenshot captioned "You did not fix it. The message show me a PHD board
# triggers, whole table printout." — a bug report, no question mark, 83
# characters — and it was filed as a document he never asked me to keep while
# the report itself was never read. A name for a file is a noun phrase; prose
# addressed to me is a message, and messages go to Claude.
PROSE = re.compile(r"[.!;](?:\s|$)|\byou\b|\bi\b|\bwe\b|\bplease\b|"
                   r"\bты\b|\bвы\b|\bя\b", re.I)


def _log(m):
    print(f"[scan_reflex] {m}", flush=True)


def should_try(path, caption):
    """Is this a photo we should look for documents in?"""
    if not path or os.path.splitext(path)[1].lower() not in PHOTO_EXTS:
        return False
    cap = (caption or "").strip()
    if not cap:
        return True
    return not (QUESTION.search(cap) or len(cap) > 120
                or PROSE.search(cap) or len(cap.split()) > 8)


def _slug(caption):
    s = re.sub(r"[^a-z0-9]+", "-", (caption or "").lower()).strip("-")
    return (s[:48].rstrip("-") or "scan")


def _annotate(preview_path):
    """Ask the vision model for the words that make this document findable.

    The free VL endpoint intermittently returns nothing (the same failure the
    private describe path retries around), and here an empty reply means a
    document filed with no words at all — so retry before giving up. Its read
    timeout is 120s, which is why this runs BEHIND the answer and never in the
    handler: three slow retries on two documents would otherwise hold the chat
    reply for minutes over something the owner is not waiting for."""
    # SIX attempts, not three: measured 2026-08-11, the free tier answered 1
    # time in 3 on one image and instantly on the next call. Each attempt is
    # now capped at 45s (see projects_mode._or_chat), so six of them cost less
    # in the worst case than the old three did, and the odds of ending with
    # nothing drop from about one in three to roughly one in seven hundred.
    for i in range(6):
        ann = projects_mode.annotate_image(preview_path)
        if ann and ann.strip():
            return ann.strip()
        time.sleep(min(1.5 * (i + 1), 5))
    return ""


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


def _finish_later(filed, source_photo):
    """Auto-annotation, CLIP indexing and the KB index refresh, behind the
    answer. Nothing here may fail loudly: the documents are already filed, and a
    failed index is a search that misses them, not work that was lost."""
    try:
        for f in filed:
            if f["auto"]:                # no caption -> the model writes one
                f["annotation"] = _annotate(f["preview"])
                if f["kind"] == "pdf":
                    _sidecar(f["path"], f["annotation"], source_photo)
            if f["preview"] != f["path"] and os.path.exists(f["preview"]):
                os.remove(f["preview"])
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

    THE ORIGINAL PHOTO IS NEVER TOUCHED (the owner, 2026-08-09) — not modified,
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
    cap = (caption or "").strip()
    filed = []
    for f in found:
        dest = os.path.join(DEST, os.path.basename(f["path"]))
        n = 1
        while os.path.exists(dest):
            root, ext = os.path.splitext(dest)
            dest, n = f"{root}-{n}{ext}", n + 1
        os.replace(f["path"], dest)
        if f["kind"] == "pdf":
            _sidecar(dest, cap, path)     # rewritten once the model annotates
        filed.append({"path": dest, "kind": f["kind"], "white": f["white"],
                      "size": os.path.getsize(dest), "annotation": cap,
                      "auto": not cap,
                      "preview": dest if f["preview"] == f["path"] else f["preview"]})
    # Loud if the original ever changed: silence here would mean the one
    # full-resolution copy was damaged and nobody found out until it was needed.
    if not os.path.exists(path) or (os.path.getsize(path),
                                    os.path.getmtime(path)) != before:
        _log(f"WARNING: the source photo changed during extraction: {path}")
    if filed:
        threading.Thread(target=_finish_later, args=(filed, path),
                         daemon=True).start()
    return filed


def file_extracted(path, kind=None, caption="", source=None):
    """File a document that has ALREADY been extracted — by the app, not here.

    2026-08-09: the iOS app now runs the same geometry on the phone, at capture,
    where it has the real focal length and the user's aim. What it produces is
    better evidence than anything the box could recover from a re-uploaded JPEG,
    so the box must not extract it a second time — it must file what arrived.

    Everything after the pixels is identical to `scan_and_file`: the same
    destination, the same sidecar for a text-less PDF, the same annotate-and-
    index behind the answer. Only the seeing is skipped.

    The source photo is never read here and never touched.
    """
    os.makedirs(DEST, exist_ok=True)
    ext = os.path.splitext(path)[1].lower()
    kind = kind or ("pdf" if ext == ".pdf" else "image")
    cap = (caption or "").strip()
    dest = os.path.join(DEST, f"{time.strftime('%Y%m%d-%H%M%S')}-"
                              f"{_slug(cap) if cap else 'extract'}{ext}")
    n = 1
    while os.path.exists(dest):
        root, e = os.path.splitext(dest)
        dest, n = f"{root}-{n}{e}", n + 1
    # COPY, never move: the upload lives in the camera directory, which is the
    # app's own record of what it sent and what /api/file/<token> resolves to.
    # Emptying it would break every thumbnail already drawn on the phone.
    with open(path, "rb") as src, open(dest, "wb") as out:
        out.write(src.read())
    if kind == "pdf":
        _sidecar(dest, cap, source or path)
    # THE ANNOTATOR NEEDS AN IMAGE. Handing it the PDF is a 400 from the vision
    # endpoint, three times over the retry loop, and the document lands with
    # "(no annotation — reply with one to make this searchable)" — filed, and
    # findable by nothing but its date. That is what happened to the first real
    # extraction to arrive (2026-08-11, the app developer's agent's two-document desk photo):
    # the header worked, the filing worked, and the document was invisible.
    #
    # The Telegram path never hit this because autoscan renders a preview for
    # every PDF it makes; this path had no such step, so it passed the PDF
    # itself and called it a preview.
    preview = dest
    if kind == "pdf":
        try:
            stem = os.path.splitext(dest)[0] + "-preview"
            subprocess.run(["pdftoppm", "-jpeg", "-r", "110", "-f", "1",
                            "-l", "1", dest, stem],
                           capture_output=True, timeout=60)
            for cand in (f"{stem}-1.jpg", f"{stem}-01.jpg", f"{stem}.jpg"):
                if os.path.exists(cand):
                    preview = cand
                    break
        except Exception as e:
            _log(f"preview render failed for {dest}: {e}")
    filed = [{"path": dest, "kind": kind, "white": kind == "pdf",
              "size": os.path.getsize(dest), "annotation": cap,
              "auto": not cap, "preview": preview}]
    threading.Thread(target=_finish_later, args=(filed, source or path),
                     daemon=True).start()
    _log(f"filed an app-extracted {kind}: {os.path.basename(dest)} "
         f"({os.path.getsize(dest) / 1024:.0f} KB)")
    return filed[0]


def _describe(filed):
    lines = []
    for f in filed:
        stock = "white paper" if f["white"] else "coloured/dark stock"
        lines.append(f"• `{os.path.basename(f['path'])}` — {stock} → "
                     f"{f['kind'].upper()}, {f['size'] / 1024:.0f} KB")
        ann = (f["annotation"] or "").strip().replace("\n", " ")
        if ann:
            lines.append(f"  annotation: _{ann[:300]}{'…' if len(ann) > 300 else ''}_")
        else:
            lines.append("  annotation: being written now (no caption)")
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
         "\n\nAnnotating and indexing now — searchable in a moment. "
         "The original photo is untouched.")
    return ("scan reflex: filed " +
            ", ".join(f"{os.path.basename(f['path'])} ({f['kind']})" for f in filed))


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        cap = " ".join(sys.argv[2:])
        print(f"should_try -> {should_try(sys.argv[1], cap)}")
        out = scan_and_file(sys.argv[1], cap)
        for r in out:
            print(f"  {r['kind']}  {r['size'] / 1024:.0f}KB  {r['path']}")
        # In the gateway the annotate+index thread outlives the answer; on the
        # command line there is nothing to outlive, so wait and show the result.
        for t in threading.enumerate():
            if t is not threading.current_thread():
                t.join()
        for r in out:
            print(f"  annotation ({os.path.basename(r['path'])}): "
                  f"{(r['annotation'] or '(none)')[:200]}")
