"""Business vocabulary for the voice (#877/#878).

The voice model transcribes and spells what it hears. Left alone it turns a
customer's name, a press model or a supplier into the nearest common word. The
agent knows the business, so the agent supplies the list; the plane puts it in
every session it mints (the transcription prompt and a short instructions
section) and shows it to the app.

Generic by design: an install names its sources in config.json, and nothing
here knows any company.

    "vocabulary_sources": [
      {"path": "customers/customers.md", "kind": "customer", "headings": true,
       "contacts": true},
      {"path": "vendors/vendors.md", "kind": "supplier", "column": "Vendor",
       "contacts": true},
      {"path": "knowledge-base/equipment/equipment-park.md", "kind": "machine",
       "column": "Machine"}
    ],
    "vocabulary_terms": [{"term": "...", "kind": "term", "say_as": "..."}]

Paths are relative to the agent's workdir. A source takes `## headings`, one
table `column` by header name, and/or `contacts` (the person in "Contact: Name,"
or a table's Contact column).

Stored as vocabulary.json beside the agent's state:
    {"updated": epoch, "built": [...], "manual": [...], "removed": [...]}
`manual` (things the user said) and `removed` survive every rebuild.
"""
import json
import os
import re
import sqlite3
import time

KINDS = ("brand", "machine", "supplier", "customer", "product", "term", "person")

_HEAD = re.compile(r"^#{2,3}\s+(.+?)\s*$")
_CONTACT = re.compile(r"Contact:\s*([A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+){0,3})")


def _clean(t):
    t = re.sub(r"[*_`]", "", str(t or "")).strip(" .,:;|-")
    return re.sub(r"\s+", " ", t)


# Section headings that name a part of a document, not a thing in the business.
_GENERIC = {"notes", "note", "overview", "summary", "contacts", "contact",
            "terms", "history", "index", "details", "other", "misc",
            "miscellaneous", "general", "background", "status", "todo",
            "open items", "next steps", "references", "appendix"}


def _usable(t):
    """A term worth teaching: not empty, not a sentence, not a number."""
    return (t and 2 <= len(t) <= 60 and len(t.split()) <= 6
            and t.lower() not in _GENERIC
            and not re.fullmatch(r"[\d\s.,$%/-]+", t))


def _tables(lines):
    """Yield (header_cells, row_cells) for every markdown table row."""
    header = None
    for ln in lines:
        s = ln.strip()
        if not s.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        yield header, cells


def from_markdown(path, kind, headings=False, column=None, contacts=False):
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return []
    out = []
    if headings:
        for ln in lines:
            m = _HEAD.match(ln)
            if m:
                out.append({"term": _clean(m.group(1)), "kind": kind})
    for header, cells in _tables(lines):
        low = [h.lower() for h in header]
        if column and column.lower() in low:
            i = low.index(column.lower())
            if i < len(cells):
                out.append({"term": _clean(cells[i]), "kind": kind})
        if contacts and "contact" in low:
            i = low.index("contact")
            if i < len(cells):
                name = _clean(cells[i].split(",")[0])
                out.append({"term": name, "kind": "person"})
    if contacts:
        for ln in lines:
            for m in _CONTACT.finditer(ln):
                out.append({"term": _clean(m.group(1)), "kind": "person"})
    return [x for x in out if _usable(x["term"])]


def _weights(terms, archive_db, days=90):
    """0..1 per term: how often the conversation archive mentions it lately."""
    if not archive_db or not os.path.exists(archive_db):
        return {}
    try:
        cx = sqlite3.connect(f"file:{archive_db}?mode=ro", uri=True, timeout=3)
        rows = cx.execute("SELECT text FROM messages WHERE epoch > ?",
                          (time.time() - days * 86400,)).fetchall()
        cx.close()
    except Exception:
        return {}
    blob = "\n".join(str(r[0] or "") for r in rows).lower()
    # Whole words only: a substring count made "Valid" the top term on the
    # strength of every "invalid", and "Meta" of every "metadata".
    counts = {t: len(re.findall(r"(?<![\w-])" + re.escape(t.lower())
                                + r"(?![\w-])", blob)) for t in terms}
    top = max(counts.values() or [0]) or 1
    return {t: round(min(1.0, 0.2 + 0.8 * c / top), 3) if c else 0.1
            for t, c in counts.items()}


def build(workdir, sources, seed_terms=(), archive_db=None, extra=()):
    """The fresh list from the sources. `extra` lets an install add its own
    finder (an install can add its CRM) without this module knowing it."""
    seen, items = {}, []
    for src in sources or []:
        path = os.path.join(workdir, src.get("path", ""))
        for it in from_markdown(path, src.get("kind", "term"),
                                headings=bool(src.get("headings")),
                                column=src.get("column"),
                                contacts=bool(src.get("contacts"))):
            items.append(it)
    for it in list(seed_terms) + list(extra):
        if isinstance(it, dict) and _usable(_clean(it.get("term"))):
            items.append({**it, "term": _clean(it["term"])})
    for it in items:
        k = it["term"].lower()
        if k not in seen:
            seen[k] = {"term": it["term"],
                       "kind": it.get("kind") if it.get("kind") in KINDS else "term",
                       **({"say_as": it["say_as"]} if it.get("say_as") else {})}
    w = _weights([v["term"] for v in seen.values()], archive_db)
    for v in seen.values():
        v["weight"] = w.get(v["term"], 0.1)
    return list(seen.values())


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def view(d):
    """The answer the plane and the app see: built + manual - removed, sorted."""
    removed = {str(t).lower() for t in d.get("removed") or []}
    merged = {}
    for it in (d.get("built") or []) + [
            {**m, "weight": 1.0, "manual": True} for m in d.get("manual") or []]:
        k = str(it.get("term", "")).lower()
        if k and k not in removed:
            merged[k] = it
    items = sorted(merged.values(), key=lambda x: (-float(x.get("weight") or 0),
                                                   x["term"].lower()))
    return {"updated": d.get("updated"), "count": len(items), "items": items}


# ------------------------------------------------------------ per chat
# #884: the words one conversation uses. A group about a single customer, a
# machine or a project has its own names, codes and jargon, and the voice
# should know them while that chat is the linked one. Found in the chat's own
# archive, never configured.
_CODE = re.compile(r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b")
_CAPS = re.compile(r"\b[A-Z][a-zA-Z0-9&'.-]*(?:\s+(?:&\s+)?[A-Z][a-zA-Z0-9&'.-]*){0,3}\b")
_COMMON = set("""a an and are as at be been but by can could did do does for from had has
have he her his how i if in into is it its just me my no not of on or our please she so
than that the their them then there these they this those to up us was we were what when
where which who why will with would yes you your hi hello thanks thank ok okay also all any
both each few more most other some such only own same too very s t don should now new one two
three first last next today tomorrow yesterday monday tuesday wednesday thursday friday
saturday sunday january february march april may june july august september october november
december jan feb mar apr jun jul aug sep sept oct nov dec subject re fwd fw https http www com net org pdf png jpg mp4 utc edt est am pm id
done sure great good let here see know think need want get got make made like well right
note notes file files photo photos image voice message messages chat group claude agent""".split())


def _candidates(text):
    found = []
    for m in _CODE.finditer(text):
        t = m.group(0)
        if (3 <= len(t) <= 16
                and not re.fullmatch(r"\d+[a-z]{0,2}", t.lower())
                # "1-minute", "24-hour": a number glued to a word
                and not re.fullmatch(r"\d+(?:-[a-z]+)+", t)
                # a file id or a hash, not a word anyone says
                and not (len(t) > 10 and re.search(r"[a-z]", t)
                         and re.search(r"[A-Z]", t) and re.search(r"\d", t))):
            found.append((t, "machine"))
    for m in _CAPS.finditer(text):
        # A capital after a sentence break is grammar, not a name.
        before = text[:m.start()].rstrip(" \t*_>\"'(")
        if not before or before[-1] in ".!?:\n-#|•":
            continue
        t = m.group(0).strip(" .,'&-")
        t = re.sub(r"'s$", "", t)
        if "'" in t:
            continue                      # contractions: I'll, It's, That's
        words = t.split()
        if words and words[0].lower() in _COMMON:
            words = words[1:]
        t = " ".join(words)
        if t and t.lower() not in _COMMON and len(t) >= 3:
            found.append((t, "term"))
    return found


def chat_terms(archive_db, chat_id, exclude=(), limit=80, days=180):
    """Terms specific to one chat: frequent names, codes and jargon from its
    archive, weighted by count and recency, minus the account-wide list."""
    if not archive_db or not os.path.exists(archive_db):
        return []
    try:
        cx = sqlite3.connect(f"file:{archive_db}?mode=ro", uri=True, timeout=3)
        rows = cx.execute("SELECT epoch, text FROM messages WHERE chat_id = ? "
                          "AND epoch > ? ORDER BY epoch DESC LIMIT 4000",
                          (int(chat_id), time.time() - days * 86400)).fetchall()
        cx.close()
    except Exception:
        return []
    ex = {str(e).lower() for e in exclude}
    now, score, kinds, spelled = time.time(), {}, {}, {}
    for ep, text in rows:
        recency = 0.5 + 0.5 * max(0.0, 1 - (now - float(ep or now)) / (days * 86400))
        seen_here = set()
        for t, kind in _candidates(str(text or "")):
            k = t.lower()
            if k in ex or k in seen_here or not _usable(t):
                continue
            seen_here.add(k)
            score[k] = score.get(k, 0) + recency
            kinds.setdefault(k, kind)
            spelled.setdefault(k, t)
    # A term seen in one message only is noise more often than vocabulary.
    keep = [(k, v) for k, v in score.items() if v >= 1.5]
    keep.sort(key=lambda kv: -kv[1])
    top = keep[0][1] if keep else 1
    return [{"term": spelled[k], "kind": kinds[k],
             "weight": round(min(1.0, 0.2 + 0.8 * v / top), 3)}
            for k, v in keep[:limit]]


def _apply_overlay(d, req):
    add = req.get("add")
    if isinstance(add, dict) and _usable(_clean(add.get("term"))):
        term = _clean(add["term"])
        kind = add.get("kind") if add.get("kind") in KINDS else "term"
        d["manual"] = [m for m in d.get("manual") or []
                       if m.get("term", "").lower() != term.lower()]
        d["manual"].append({"term": term, "kind": kind,
                            **({"say_as": str(add["say_as"])[:80]}
                               if add.get("say_as") else {})})
        d["removed"] = [r for r in d.get("removed") or [] if r.lower() != term.lower()]
        d["updated"] = time.time()
    rem = req.get("remove")
    if isinstance(rem, str) and rem.strip():
        d["removed"] = sorted(set((d.get("removed") or []) + [rem.strip()]))
        d["manual"] = [m for m in d.get("manual") or []
                       if m.get("term", "").lower() != rem.strip().lower()]
        d["updated"] = time.time()


def handle(path, req, rebuild_fn, archive_db=None):
    """One relay request: {} | {"rebuild": true} | {"add": {...}} |
    {"remove": term}, each optionally with "chat_id" for that chat's list."""
    d = load(path)
    # Rebuilt on request, when empty, and once a day on its own (the next
    # request after 24 h), so no cron is needed for the list to stay fresh.
    stale = lambda e: time.time() - float(e.get("built_at") or 0) > 86400
    if req.get("chat_id") in (None, "") and (
            req.get("rebuild") or not d.get("built") or stale(d)):
        d["built"] = rebuild_fn()
        d["built_at"] = d["updated"] = time.time()
    cid = req.get("chat_id")
    if cid not in (None, ""):
        try:
            cid = str(int(cid))
        except (TypeError, ValueError):
            return {"error": "bad chat_id"}
        chats = d.setdefault("chats", {})
        e = chats.setdefault(cid, {})
        if req.get("rebuild") or not e.get("built_at") or stale(e):
            acct = view(d)["items"] if d.get("built") else []
            e["built"] = chat_terms(archive_db, cid,
                                    exclude=[i["term"] for i in acct[:300]])
            e["built_at"] = e["updated"] = time.time()
        _apply_overlay(e, req)
        save(path, d)
        return {**view(e), "chat_id": int(cid)}
    _apply_overlay(d, req)
    save(path, d)
    return view(d)
