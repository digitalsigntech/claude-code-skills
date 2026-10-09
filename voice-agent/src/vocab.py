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


def handle(path, req, rebuild_fn):
    """One relay request: {} | {"rebuild": true} | {"add": {...}} | {"remove": term}."""
    d = load(path)
    # Rebuilt on request, when empty, and once a day on its own (the next
    # request after 24 h), so no cron is needed for the list to stay fresh.
    if (req.get("rebuild") or not d.get("built")
            or time.time() - float(d.get("built_at") or 0) > 86400):
        d["built"] = rebuild_fn()
        d["built_at"] = d["updated"] = time.time()
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
    save(path, d)
    return view(d)
