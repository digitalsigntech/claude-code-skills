"""Data privacy labels — the source of truth for public vs private.

DEFAULT PRIVATE. A source is public only if it is explicitly listed in the manifest's
`public_paths`, or it came from the open web (rule web_is_public). Everything else is
private and must be processed on-box / by the private model.

Public data can be processed by the cloud LLM; private data must not be. Routing
consults `label_for()` / `is_public()`. Fail closed: anything unknown is private.

Config: set DATA_CLASSIFICATION to the manifest path (default: data-classification.json
next to this file's parent project root).
"""
import os
import json

ROOT = os.environ.get("PRIVACY_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.environ.get("DATA_CLASSIFICATION", os.path.join(ROOT, "data-classification.json"))


def _load():
    try:
        with open(MANIFEST) as f:
            return json.load(f)
    except Exception:
        # Fail closed: if the manifest can't be read, treat everything as private.
        return {"default": "private", "public_paths": [], "rules": {}}


def _rel(path):
    ap = os.path.abspath(path)
    try:
        return os.path.relpath(ap, ROOT)
    except ValueError:
        return ap


def label_for(path, origin=None):
    """Return 'public' or 'private' for a source. `origin='web'` marks web-scraped
    content public (rule web_is_public). Unknown/unlisted -> private (fail closed)."""
    m = _load()
    if origin == "web" and m.get("rules", {}).get("web_is_public"):
        return "public"
    rel = _rel(path)
    pub = set(m.get("public_paths", []))
    if rel in pub or os.path.basename(rel) in {os.path.basename(p) for p in pub}:
        return "public"
    return "private" if m.get("default", "private") == "private" else "public"


def is_public(path, origin=None):
    return label_for(path, origin) == "public"


def mark_public(path):
    """Persist a source as public (when the owner drops a doc and says 'public')."""
    m = _load()
    rel = _rel(path)
    paths = m.setdefault("public_paths", [])
    if rel not in paths:
        paths.append(rel)
        with open(MANIFEST, "w") as f:
            json.dump(m, f, indent=2)
    return rel


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        print(f"{label_for(p):8}  {p}")
