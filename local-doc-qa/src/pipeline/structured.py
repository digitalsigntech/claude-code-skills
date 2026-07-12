"""Exact numeric/value queries over Markdown tables — no LLM, no embeddings.

The .md files in your knowledge base are the source of truth. This parses their
tables and answers "which X cost more than $N", "cheapest", etc. deterministically
(100% correct), so we never rely on the model for arithmetic over a table.

Currency examples below use "$"; adapt PRICE_RE for other currencies if needed.
"""
import os, re, glob
from config import KB_DIR

KB_ROOT = KB_DIR
PRICE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")

# Optional category routing: map phrases in a question to a substring of the
# authoritative source .md filename, so a category question is answered from the
# right file. CUSTOMIZE these to your own document/product categories, or leave the
# list empty to search across every .md. Examples:
CATEGORY_MAP = [
    # (("phrase", "synonym", ...),                "substring-of-.md-filename"),
    (("widget", "widgets"),                       "widgets"),
    (("accessory", "accessories", "adapter"),     "accessories"),
    (("service", "subscription", "plan"),         "services"),
]


def _price(cell):
    m = PRICE_RE.search(cell or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_md_tables(text):
    """Yield (heading, header_cells, [row_cells,...]) for each Markdown table."""
    lines = text.split("\n")
    heading, i = "", 0
    while i < len(lines):
        line = lines[i]
        h = re.match(r"^#{1,6}\s+(.*)", line.strip())
        if h:
            heading = h.group(1).strip()
        is_row = line.strip().startswith("|")
        sep = (i + 1 < len(lines)
               and re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", lines[i + 1])
               and "-" in lines[i + 1])
        if is_row and sep:
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            rows, j = [], i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            yield heading, header, rows
            i = j
        else:
            i += 1


def load_priced_items(root=KB_ROOT):
    """All (name, price, source, section, context) priced rows across the KB .md."""
    items = []
    for path in glob.glob(os.path.join(root, "**", "*.md"), recursive=True):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        src = os.path.basename(path)
        for heading, header, rows in parse_md_tables(text):
            pcol = next((k for k, h in enumerate(header) if "price" in h.lower()), None)
            for cells in rows:
                price = _price(cells[pcol]) if (pcol is not None and pcol < len(cells)) else None
                if price is None:
                    price = next((p for p in (_price(c) for c in cells) if p is not None), None)
                if price is None:
                    continue
                name = re.sub(r"<br\s*/?>", " ", cells[0]) if cells else ""
                name = re.sub(r"\s+", " ", name).strip()
                if not name or set(name) <= {"-", " "}:
                    continue
                ctx = re.sub(r"<br\s*/?>", " ", " | ".join(cells))
                items.append({"name": name, "price": price, "source": src,
                              "section": heading, "context": re.sub(r"\s+", " ", ctx).strip()})
    return items


def _dedup(items):
    seen, out = set(), []
    for it in items:
        key = (it["name"].lower(), round(it["price"], 2))
        if key not in seen:
            seen.add(key); out.append(it)
    return out


def _category_filter(items, question):
    ql = question.lower()
    wanted = [srcsub for phrases, srcsub in CATEGORY_MAP if any(p in ql for p in phrases)]
    if not wanted:
        return items
    def norm(s):
        return s.lower().replace("-", " ")
    pool = [it for it in items if any(norm(w) in norm(it["source"]) for w in wanted)]
    return pool or items


# ---- numeric intent + answering ---------------------------------------------
_NUM = r"\$?\s?([\d,]+(?:\.\d+)?)"
_OPS = [
    (re.compile(r"(?:more than|greater than|over|above|exceed(?:s|ing)?|\>)\s*" + _NUM, re.I), "gt"),
    (re.compile(r"at least\s*" + _NUM, re.I), "gte"),
    (re.compile(r"(?:less than|cheaper than|under|below|\<)\s*" + _NUM, re.I), "lt"),
    (re.compile(r"at most\s*" + _NUM, re.I), "lte"),
]
_BETWEEN = re.compile(r"between\s*" + _NUM + r"\s*(?:and|-|to)\s*" + _NUM, re.I)
_EXTREME = re.compile(r"\b(cheapest|least expensive|lowest priced?|most expensive|"
                      r"highest priced?|dearest|priciest)\b", re.I)


def is_numeric_query(question):
    ql = question.lower()
    return bool(_BETWEEN.search(ql) or _EXTREME.search(ql)
                or any(rx.search(ql) for rx, _ in _OPS))


def answer(question):
    """Return a formatted exact answer, or None if not a numeric/price query."""
    # category-filter on raw items FIRST (preserves the category-named source),
    # then dedup within the filtered pool.
    items = _dedup(_category_filter(load_priced_items(), question))
    if not items:
        return None
    ql = question.lower()

    m = _BETWEEN.search(ql)
    if m:
        lo, hi = float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
        sel = [it for it in items if lo <= it["price"] <= hi]
        return _fmt(sel, f"priced between ${lo:,.0f} and ${hi:,.0f}")

    me = _EXTREME.search(ql)
    if me:
        cheapest = "cheap" in me.group(0).lower() or "lowest" in me.group(0).lower() or "least" in me.group(0).lower()
        it = min(items, key=lambda x: x["price"]) if cheapest else max(items, key=lambda x: x["price"])
        kind = "cheapest" if cheapest else "most expensive"
        return f"The {kind} is **{it['name']}** at ${it['price']:,.2f} [{it['source']}]."

    for rx, op in _OPS:
        m = rx.search(ql)
        if not m:
            continue
        v = float(m.group(1).replace(",", ""))
        pred = {"gt": lambda p: p > v, "gte": lambda p: p >= v,
                "lt": lambda p: p < v, "lte": lambda p: p <= v}[op]
        sel = [it for it in items if pred(it["price"])]
        word = {"gt": f"more than ${v:,.0f}", "gte": f"at least ${v:,.0f}",
                "lt": f"less than ${v:,.0f}", "lte": f"at most ${v:,.0f}"}[op]
        return _fmt(sel, word)
    return None


def _fmt(items, desc):
    if not items:
        return f"No items found {desc}."
    items = sorted(items, key=lambda x: -x["price"])
    lines = [f"Items {desc} ({len(items)}):"]
    for it in items:
        lines.append(f"- {it['name']}: ${it['price']:,.2f} [{it['source']}]")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "items more than $300"
    print(f"numeric? {is_numeric_query(q)}\n")
    print(answer(q))
