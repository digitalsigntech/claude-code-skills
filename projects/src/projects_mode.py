"""R&D project chats — every post in a project group is FILED into the project.

A "project chat" is a Telegram group bound to a project directory under
DST/projects/<slug>/ (e.g. "PHD R&D with Claude" → projects/phd-rd/). Everything
the owner posts there — text, voice notes, photos, documents — is filed
deterministically into the project, organized for quick retrieval:

    projects/<slug>/
      PROJECT.md          wiki-style overview: goals, decisions, links (LLM-editable)
      REGISTRY.md         one table row per filed item — THE quick-retrieval index
      files/YYYY-MM-DD/   raw files, timestamped names
      notes/YYYY-MM.md    chronological lab-notebook of text posts + voice transcripts

Processing policy: image/document analysis is done by the LOCAL-policy LLM only
(Nemotron — on OpenRouter until local hardware lands, same interim the owner accepted
for email extraction; never cloud Claude). Voice transcription is whisper.cpp on-box.

Privacy switch per chat: "wisdom" = the conversational turn runs on cloud Claude;
"privacy" = it runs on the Nemotron private path. The current mode is shown as a
suffix on the GROUP TITLE (needs the bot to be a group admin with "change info").
"""
import os, re, json, base64, datetime, subprocess, threading

import requests

import tgconf as C
import tg_api as TG

PROJECTS_DIR = os.path.join(C.DST_ROOT, "projects")
STATE_FILE = os.path.join(C.STATE_DIR, "projects.json")
_LOCK = threading.Lock()

# Interim until the DGX Spark: "local-policy" models run on OpenRouter, matching the
# email-KB pipeline. Text = the standard Nemotron; vision = NVIDIA's Nemotron VL.
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
OR_TEXT_MODEL = os.environ.get("OR_MODEL", "nvidia/nemotron-3-super-120b-a12b")
OR_VISION_MODEL = os.environ.get("DST_PROJECTS_VISION_MODEL",
                                 "nvidia/nemotron-nano-12b-v2-vl:free")

TITLE_SUFFIX = {"wisdom": "💡 Wisdom", "privacy": "🔒 Privacy"}
# retired suffixes still stripped from titles so toggling doesn't stack them
OLD_SUFFIXES = ["🧠 Wisdom"]


def _or_key():
    try:
        return next((l.split("=", 1)[1].strip()
                     for l in open(os.path.expanduser("~/.config/dst/secrets.env"))
                     if l.startswith("OPENROUTER_API_KEY=")), None)
    except Exception:
        return None


# ---- per-chat state ---------------------------------------------------------
def _load():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def _save(d):
    tmp = STATE_FILE + ".tmp"
    json.dump(d, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE_FILE)


def get(chat_id):
    """Chat's project binding: {'project': slug, 'privacy': mode} or None."""
    with _LOCK:
        st = _load().get(str(chat_id))
    if st:
        return st
    slug = C.PROJECT_CHATS.get(chat_id)
    return {"project": slug, "privacy": "wisdom"} if slug else None


def is_project_chat(chat_id):
    return get(chat_id) is not None


def _update(chat_id, **kv):
    with _LOCK:
        d = _load()
        st = d.get(str(chat_id)) or dict(get(chat_id) or {})
        st.update(kv)
        d[str(chat_id)] = st
        _save(d)
    return st


def set_privacy(chat_id, mode):
    return _update(chat_id, privacy=mode)


def set_project(chat_id, slug):
    slug = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")
    if not slug:
        return None
    _update(chat_id, project=slug)
    scaffold(slug)
    return slug


def privacy_of(chat_id):
    st = get(chat_id)
    return (st or {}).get("privacy", "wisdom")


def apply_title(chat_id, base_title=None):
    """Show the privacy mode on the group name. Remembers the base title (stripping
    any previous mode suffix) so toggling doesn't stack suffixes. Returns an error
    string if Telegram refused (usually: bot is not an admin), else None."""
    st = get(chat_id) or {}
    base = base_title or st.get("base_title") or ""
    for suf in list(TITLE_SUFFIX.values()) + OLD_SUFFIXES:
        base = base.replace(suf, "")
    base = base.strip(" -—·|").strip()
    if not base:
        return "no base title known yet"
    _update(chat_id, base_title=base)
    title = f"{base} · {TITLE_SUFFIX[privacy_of(chat_id)]}"
    r = TG._call("setChatTitle", chat_id=chat_id, title=title[:128])
    if not r.get("ok"):
        return str(r.get("error"))[:200]
    return None


# ---- project directories ----------------------------------------------------
def root(slug):
    return os.path.join(PROJECTS_DIR, slug)


def scaffold(slug):
    r = root(slug)
    os.makedirs(os.path.join(r, "files"), exist_ok=True)
    os.makedirs(os.path.join(r, "notes"), exist_ok=True)
    pj = os.path.join(r, "PROJECT.md")
    if not os.path.exists(pj):
        open(pj, "w").write(
            f"# Project: {slug}\n\n"
            "Wiki-style overview — keep this current: goals, current state, key "
            "decisions, links to important files (paths relative to this directory).\n\n"
            "## Goals\n\n## Current state\n\n## Decisions\n\n## Key files\n")
    rg = os.path.join(r, "REGISTRY.md")
    if not os.path.exists(rg):
        open(rg, "w").write(
            f"# Registry — {slug}\n\n"
            "Every filed item, one row each. Newest last. Grep me first.\n\n"
            "| when | item | kind | by | summary/annotation |\n"
            "|------|------|------|----|--------------------|\n")
    return r


def _registry_add(slug, relpath, kind, sender, summary):
    row = "| {} | {} | {} | {} | {} |\n".format(
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        relpath, kind, sender,
        (summary or "").replace("\n", " ").replace("|", "/").strip()[:500])
    with _LOCK:
        with open(os.path.join(root(slug), "REGISTRY.md"), "a") as f:
            f.write(row)


def add_note(slug, text, sender, kind="note"):
    """Append a text post / voice transcript to the monthly lab-notebook."""
    scaffold(slug)
    now = datetime.datetime.now()
    p = os.path.join(root(slug), "notes", f"{now:%Y-%m}.md")
    entry = f"\n### {now:%Y-%m-%d %H:%M} — {sender}" + \
            (f" ({kind})" if kind != "note" else "") + f"\n{text.strip()}\n"
    with _LOCK:
        new = not os.path.exists(p)
        with open(p, "a") as f:
            if new:
                f.write(f"# {slug} — notes {now:%Y-%m}\n")
            f.write(entry)
    return p


def file_file(slug, src_path, sender, annotation, kind):
    """Move an inbox download into the project tree + registry. Returns dest path."""
    scaffold(slug)
    now = datetime.datetime.now()
    day_dir = os.path.join(root(slug), "files", f"{now:%Y-%m-%d}")
    os.makedirs(day_dir, exist_ok=True)
    dest = os.path.join(day_dir, os.path.basename(src_path))
    if os.path.abspath(src_path) != os.path.abspath(dest):
        os.replace(src_path, dest)
    rel = os.path.relpath(dest, root(slug))
    _registry_add(slug, rel, kind, sender, annotation)
    return dest


# ---- local-policy analysis (Nemotron; NEVER cloud Claude) -------------------
def _or_chat(messages, model, max_tokens=400):
    key = _or_key()
    if not key:
        raise RuntimeError("no OpenRouter key")
    r = requests.post(OR_URL, json={
        "model": model, "temperature": 0.2, "max_tokens": max_tokens,
        "reasoning": {"enabled": False}, "messages": messages},
        headers={"Authorization": f"Bearer {key}"}, timeout=(10, 120))
    r.raise_for_status()
    out = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
    if not out.strip():
        raise RuntimeError("empty model reply")
    return out.strip()


def annotate_image(path):
    """Auto-annotation for a photo the owner didn't caption. Local-policy model only.
    Returns the annotation, or None (caller marks the item unannotated)."""
    try:
        b64 = base64.b64encode(open(path, "rb").read()).decode()
        ext = os.path.splitext(path)[1].lstrip(".").lower() or "jpeg"
        ext = {"jpg": "jpeg"}.get(ext, ext)
        return _or_chat([
            {"role": "system", "content":
             "You annotate R&D lab photos for a printhead-maintenance equipment "
             "company (inkjet printheads, cleaning fluids, testing rigs). Describe "
             "what the image shows in 1-3 factual sentences, then 'Keywords:' with "
             "5-12 search keywords. No speculation beyond what is visible."},
            {"role": "user", "content": [
                {"type": "text", "text": "Annotate this project photo."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/{ext};base64,{b64}"}}]}],
            OR_VISION_MODEL, max_tokens=300)
    except Exception as e:
        print(f"[projects] image annotation failed for {path}: {e}", flush=True)
        return None


def _doc_text(path, max_chars=12000):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            r = subprocess.run(["pdftotext", "-l", "20", path, "-"],
                               capture_output=True, text=True, timeout=120)
            return (r.stdout or "")[:max_chars]
        if ext in (".txt", ".md", ".csv", ".tsv", ".log"):
            return open(path, errors="replace").read()[:max_chars]
    except Exception:
        pass
    return ""


def summarize_doc(path):
    """2-4 sentence summary + keywords for a filed document (local-policy model).
    Returns None when the doc has no extractable text or the model call fails."""
    text = _doc_text(path)
    if not text.strip():
        return None
    try:
        return _or_chat([
            {"role": "system", "content":
             "You index R&D documents for a printhead-maintenance company. Summarize "
             "the document in 2-4 factual sentences, then 'Keywords:' with 5-15 "
             "search keywords."},
            {"role": "user", "content":
             f"Document filename: {os.path.basename(path)}\n\n{text}"}],
            OR_TEXT_MODEL, max_tokens=350)
    except Exception as e:
        print(f"[projects] doc summary failed for {path}: {e}", flush=True)
        return None


def write_sidecar(dest, annotation, sender, caption):
    """<file>.meta.md next to a filed file — makes it findable via ug / kb search."""
    try:
        with open(dest + ".meta.md", "w") as f:
            f.write(f"# {os.path.basename(dest)}\n\n"
                    f"- filed: {datetime.datetime.now():%Y-%m-%d %H:%M}\n"
                    f"- by: {sender}\n"
                    f"- caption: {caption or '(none)'}\n\n{annotation or ''}\n")
    except Exception:
        pass


def clip_index(path, annotation):
    """Index a filed image into the local CLIP media search (best-effort)."""
    try:
        cmd = [C.MEDIA, "add", path]
        if annotation:
            cmd += ["--annotation", annotation[:600]]
        subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:
        print(f"[projects] clip index failed for {path}: {e}", flush=True)


def ingest_file(chat_id, path, caption, sender):
    """Full filing pipeline for one downloaded file in a project chat.
    Returns (dest, kind, annotation, auto) — auto=True when the annotation was
    machine-generated rather than the owner's caption."""
    slug = get(chat_id)["project"]
    ext = os.path.splitext(path)[1].lower()
    caption = (caption or "").strip()
    auto = False
    if ext in C.IMG_EXTS:
        kind = "photo"
        annotation = caption
        if not annotation:
            annotation = annotate_image(path) or "(unannotated — auto-annotation unavailable)"
            auto = True
    elif ext in (".ogg", ".oga", ".mp3", ".m4a", ".wav"):
        kind = "audio"
        annotation = caption or "(voice note — transcript in notes/)"
    elif ext in C.VID_EXTS:
        kind = "video"
        annotation = caption or "(video — no auto-annotation yet)"
    else:
        kind = "document"
        summary = summarize_doc(path)
        auto = bool(summary) and not caption
        annotation = " — ".join(x for x in (caption, summary) if x) or "(no extractable text)"
    dest = file_file(slug, path, sender, annotation, kind)
    write_sidecar(dest, annotation, sender, caption)
    if kind == "photo":
        clip_index(dest, annotation if annotation and "unannotated" not in annotation else "")
    return dest, kind, annotation, auto


# ---- prompt context for the conversational turn -----------------------------
def turn_context(chat_id, filed_note=""):
    st = get(chat_id)
    slug = st["project"]
    r = root(slug)
    return (
        f"[PROJECT CHAT — project '{slug}'. This group is a lab notebook for this "
        f"project; everything the user posts is auto-filed under {r}/ "
        f"(PROJECT.md = overview, REGISTRY.md = index of all filed items, notes/ = "
        f"chronological text/voice notes, files/ = raw files by date).{filed_note} "
        f"FIRST SOURCE of truth for any question here is the project directory — "
        f"read PROJECT.md / REGISTRY.md / notes/, and `ug <term> {r}` to search — "
        f"BEFORE the general KB or generic knowledge. If the user's message states "
        f"results/decisions/facts worth keeping, also update PROJECT.md (Goals/"
        f"Current state/Decisions/Key files) so the overview stays current. If it "
        f"is just a note with nothing to answer, confirm filing in ONE short line.]")
