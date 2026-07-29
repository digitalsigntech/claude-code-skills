"""On-box PDF text editing (PyMuPDF) for the private agent.

Built 2026-07-27 (owner request: "we need to give Nemotron pdf editing capabilities" — after
it refused to bump a quantity on a customer proforma invoice in the Private group).

Approach: value-level find/replace. Each match's exact glyph area is redacted (white
fill) and the replacement text re-inserted at the same baseline with the original
font size and color. Right for editing quantities, prices, dates, names in generated
PDFs; NOT for scanned images (no text layer) or edits that need layout reflow.

Targeting a repeated string ("US$1,590.00" as unit price AND amount AND total):
  • near   — preferred: a unique anchor string on the SAME VISUAL ROW (matched by
    y-coordinate, since table cells are separate lines in the text layer). First
    e2e test showed Nemotron miscounts occurrences (edited the unit price instead
    of the total) but names rows correctly.
  • occurrence — 1-based index in document order (same order extract_text returns).
Every applied edit is reported with its row context so the model can self-verify.
"""
import os

import fitz

MAX_TEXT = 8000          # cap on extracted text returned to the model
ROW_TOL = 4              # pt: lines whose y-centers differ less are the same row
NEAR_MAX_DY = 30         # pt: a 'near' anchor farther than this is a wrong row


def _int_color(c):
    """PyMuPDF span color (int 0xRRGGBB) -> (r, g, b) floats."""
    c = int(c or 0)
    return ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)


def _page_lines(page):
    """Flatten rawdict: [{'x0', 'y', 'text', 'chars'}] per line, reading order.
    chars = [(char, bbox, origin, span), ...]."""
    out = []
    for block in page.get_text("rawdict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            chars = []
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    chars.append((ch["c"], ch["bbox"], ch.get("origin"), span))
            if not chars:
                continue
            bbox = line.get("bbox", chars[0][1])
            out.append({"x0": bbox[0], "y": (bbox[1] + bbox[3]) / 2,
                        "text": "".join(c[0] for c in chars), "chars": chars})
    return out


_WIDE = str.maketrans(":;()", "：；（）")   # CJK-generated invoices use full-width
_NARROW = str.maketrans("：；（）", ":;()")  # punctuation the model retypes as ASCII


def _needle_variants(needle):
    """The needle plus whitespace/width-tolerant fallbacks, first match wins."""
    seen, out = set(), []
    for n in (needle, needle.strip(), needle.translate(_WIDE),
              needle.strip().translate(_WIDE), needle.translate(_NARROW),
              needle.strip().translate(_NARROW)):
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _find_matches(pages, needle):
    """All occurrences of needle (within a single line), document order.
    pages = [(pno, lines)]. Returns match dicts with geometry + row context."""
    matches = []
    for pno, lines in pages:
        for line in lines:
            start = 0
            while True:
                i = line["text"].find(needle, start)
                if i < 0:
                    break
                seg = line["chars"][i:i + len(needle)]
                rect = fitz.Rect(seg[0][1])
                for _, bbox, _, _ in seg[1:]:
                    rect |= fitz.Rect(bbox)
                span = seg[0][3]
                origin = seg[0][2] or (rect.x0, rect.y1)
                matches.append({"page": pno, "rect": rect, "y": line["y"],
                                "size": span.get("size", 10),
                                "color": span.get("color", 0),
                                "font": span.get("font", ""),
                                "baseline": (rect.x0, origin[1])})
                start = i + len(needle)
    return matches


def _match_any(pages, needle):
    """First needle variant with hits: (variant_used, matches)."""
    for n in _needle_variants(needle):
        hits = _find_matches(pages, n)
        if hits:
            return n, hits
    return needle, []


def _near_misses(pages, needle, limit=3):
    """Lines that ALMOST contain needle — shown when a find string misses, so the
    caller can copy the exact text instead of guessing. Two passes: loose
    containment (case/space-insensitive), then difflib similarity."""
    import difflib
    norm = lambda s: " ".join(s.split()).casefold().translate(_NARROW)
    want = norm(needle)
    lines, seen = [], set()
    for pno, pls in pages:
        for l in pls:
            t = l["text"].strip()
            if t and t not in seen:
                seen.add(t)
                lines.append((pno, t))
    out = [f"p{p + 1}: {t}" for p, t in lines if want and want in norm(t)]
    if not out:
        ranked = sorted(
            ((difflib.SequenceMatcher(None, want, norm(t)).ratio(), p, t)
             for p, t in lines), reverse=True)
        out = [f"p{p + 1}: {t}" for r, p, t in ranked[:limit] if r >= 0.5]
    return out[:limit]


def _row_text(pages, pno, y):
    """All line texts on page pno sharing the visual row at y, left-to-right."""
    lines = dict(pages)[pno]
    row = sorted((l for l in lines if abs(l["y"] - y) < ROW_TOL),
                 key=lambda l: l["x0"])
    return " | ".join(l["text"].strip() for l in row if l["text"].strip())


def extract_text(path):
    """Plain text of the PDF, page-tagged, capped at MAX_TEXT chars."""
    doc = fitz.open(path)
    parts = []
    for page in doc:
        # Visual-row layout: raw get_text() emits table cells as a vertical stream
        # ("1 / GEN5 / 1.00 / US$1,590.00 / US$1,590.00"), which made Nemotron
        # misjudge which column a value sits in (it edited UNIT PRICE meaning
        # AMOUNT). Joining same-row lines with ' | ' shows the table structure —
        # and keeps freshly inserted text in place on verification reads.
        rows, cur = [], []
        for l in sorted(_page_lines(page), key=lambda l: (round(l["y"], 1), l["x0"])):
            if cur and abs(l["y"] - cur[0]["y"]) >= ROW_TOL:
                rows.append(cur)
                cur = []
            cur.append(l)
        if cur:
            rows.append(cur)
        body = "\n".join(
            " | ".join(l["text"].strip() for l in sorted(r, key=lambda l: l["x0"])
                       if l["text"].strip())
            for r in rows)
        parts.append(f"--- page {page.number + 1} ---\n{body}")
    doc.close()
    text = "\n".join(parts)
    return text[:MAX_TEXT] + ("\n[... truncated]" if len(text) > MAX_TEXT else "")


def apply_edits(path, edits, out_path):
    """Apply [{find, replace, near?, occurrence?}] to the PDF at path, save to
    out_path. near: anchor string on the same visual row picks the occurrence
    (preferred). occurrence: 1-based document-order index. Neither = replace ALL.
    Returns a report dict (with row context per edit); raises ValueError on any
    unresolvable edit — nothing is written in that case."""
    doc = fitz.open(path)
    try:
        pages = [(p.number, _page_lines(p)) for p in doc]
        selected = []      # (match, replacement)
        report = []
        for e in edits:
            find = str(e.get("find") or "")
            repl = " ".join(str(e.get("replace") or "").split())
            if not find:
                raise ValueError("an edit is missing its 'find' string")
            if "\n" in find or "\r" in find:
                raise ValueError(
                    "find strings must be a SINGLE LINE — the PDF is edited "
                    "value-by-value, one cell at a time. Make a separate edit per "
                    "value, e.g. {'find': '1.00', 'replace': '2.00', 'near': 'GEN5'}")
            find, hits = _match_any(pages, find)
            if not hits:
                miss = _near_misses(pages, find)
                hint = ("; near-misses (copy the exact value from these lines): "
                        + " | ".join(miss)) if miss else ""
                raise ValueError(f"'{find}' not found in the PDF — copy the exact "
                                 "text from read_pdf output (spacing matters), one "
                                 f"single-line value per edit{hint}")
            near = str(e.get("near") or "").strip()
            column = str(e.get("column") or "").strip()
            occ = int(e.get("occurrence") or 0)
            if near:
                near, anchors = _match_any(pages, near)
                if not anchors:
                    raise ValueError(f"near-anchor '{near}' not found in the PDF — "
                                     "use exact text from read_pdf output")
                def _dy(h):
                    return min((abs(h["y"] - a["y"]) for a in anchors
                                if a["page"] == h["page"]), default=1e9)
                row_hits = [h for h in hits if _dy(h) <= NEAR_MAX_DY]
                if not row_hits:
                    raise ValueError(f"no occurrence of '{find}' shares a row with "
                                     f"'{near}' — pick an anchor on the same line")
                row_hits.sort(key=_dy)
                tight = [h for h in row_hits if _dy(h) <= ROW_TOL] or row_hits[:1]
                hits = tight
            if column and len(hits) > 1:
                column, col_anchors = _match_any(pages, column)
                if not col_anchors:
                    raise ValueError(f"column header '{column}' not found in the PDF "
                                     "— use exact text from read_pdf output")
                def _dx(h):
                    hx = (h["rect"].x0 + h["rect"].x1) / 2
                    return min(abs(hx - (a["rect"].x0 + a["rect"].x1) / 2)
                               for a in col_anchors)
                hits = [min(hits, key=_dx)]
            if near or (column and len(hits) == 1):
                if len(hits) > 1:
                    spots = "; ".join(
                        f"p{h['page'] + 1} row: {_row_text(pages, h['page'], h['y'])}"
                        for h in hits[:4])
                    raise ValueError(
                        f"'{find}' appears {len(hits)} times on that row ({spots}) — "
                        "add 'column' with the header of the ONE column whose value "
                        "must CHANGE (for a line total that is 'AMOUNT'; never a "
                        "column the request says stays the same, like a unit price)")
            elif occ:
                if occ > len(hits):
                    raise ValueError(f"'{find}' has only {len(hits)} occurrence(s); "
                                     f"occurrence {occ} does not exist")
                hits = [hits[occ - 1]]
            elif len(hits) > 1 and not e.get("all"):
                # Silent replace-all bit us 2026-07-27: a bare {find, replace} for a
                # leftover TOTAL also rewrote the unit price two rows up.
                spots = "; ".join(
                    f"p{h['page'] + 1} row: {_row_text(pages, h['page'], h['y'])}"
                    for h in hits[:4])
                raise ValueError(
                    f"'{find}' appears {len(hits)} times ({spots}) — ambiguous. "
                    "Add 'near' (unique text on the intended row, e.g. 'TOTAL' or "
                    "the item name) plus 'column' if needed, or 'occurrence', or "
                    "set \"all\": true ONLY if every single occurrence really must "
                    "change.")
            selected += [(m, repl) for m in hits]
            report.append({"find": find, "replace": repl, "replaced": len(hits),
                           "_locs": [(m["page"], m["y"]) for m in hits[:6]]})

        # Geometry was captured above from the ORIGINAL doc; now mutate per page:
        # all redactions first, then all insertions.
        by_page = {}
        for m, repl in selected:
            by_page.setdefault(m["page"], []).append((m, repl))
        for pno, items in by_page.items():
            page = doc[pno]
            for m, _ in items:
                page.add_redact_annot(m["rect"], fill=(1, 1, 1))
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            for m, repl in items:
                bold = "bold" in (m["font"] or "").lower()
                page.insert_text(m["baseline"], repl,
                                 fontsize=m["size"],
                                 fontname="hebo" if bold else "helv",
                                 color=_int_color(m["color"]))
        if os.path.dirname(out_path):
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
        doc.save(out_path, garbage=3, deflate=True)

        # Post-edit self-check data. rows_after_edit is the load-bearing part; the
        # unchanged-rows list is deliberately NEUTRAL — an earlier version phrased
        # it as a warning ("still appears — fix before sending") and Nemotron twice
        # over-corrected by editing the unit price the request said to keep.
        out = fitz.open(out_path)
        try:
            out_pages = [(p.number, _page_lines(p)) for p in out]
            for r in report:
                # Show each changed row as it NOW reads, so a wrong-column pick is
                # visible in the tool result itself (second e2e run: the model set
                # column 'UNIT PRICE' for a value meant for AMOUNT and only a
                # human eyeballing the render caught it).
                r["rows_after_edit"] = [
                    f"p{pno + 1}: {_row_text(out_pages, pno, y)}"
                    for pno, y in r.pop("_locs")]
                left = _find_matches(out_pages, r["find"])
                if left:
                    r["same_value_elsewhere_left_unchanged"] = [
                        f"p{m['page'] + 1}: {_row_text(out_pages, m['page'], m['y'])}"
                        for m in left[:6]]
        finally:
            out.close()
        return {"saved": out_path, "edits": report}
    finally:
        doc.close()


def _main(argv=None):
    """CLI. Extract text, or apply edits and save a copy (never in-place):
        pdf_edit.py invoice.pdf                              # print text
        pdf_edit.py invoice.pdf -f "1.00" -r "2.00" [--near GEN5] [--column AMOUNT]
                                 [--occurrence 2 | --all] [-o out.pdf]
        pdf_edit.py invoice.pdf -e '[{"find":"1.00","replace":"2.00","near":"GEN5"}]'
    """
    import argparse, json, sys
    ap = argparse.ArgumentParser(
        description="Edit text values in a PDF (redact + reinsert, PyMuPDF). "
                    "Always writes a NEW file; the original is never touched.")
    ap.add_argument("pdf", help="path to the source PDF")
    ap.add_argument("-e", "--edits", help='JSON array of edits: '
                    '[{"find","replace","near"?,"column"?,"occurrence"?,"all"?}]')
    ap.add_argument("-f", "--find", help="single edit: text to find")
    ap.add_argument("-r", "--replace", help="single edit: replacement text")
    ap.add_argument("--near", help="anchor text on the same visual row")
    ap.add_argument("--column", help="column header above the intended value")
    ap.add_argument("--occurrence", type=int, help="1-based occurrence index")
    ap.add_argument("--all", action="store_true",
                    help="replace every occurrence (otherwise a repeated find "
                         "with no near/occurrence is rejected as ambiguous)")
    ap.add_argument("-o", "--out", help="output path (default <name>-edited.pdf)")
    a = ap.parse_args(argv)
    if not os.path.isfile(a.pdf):
        sys.exit(f"error: {a.pdf} does not exist")

    if not a.edits and not a.find:            # extract mode
        print(extract_text(a.pdf))
        return

    if a.edits:
        try:
            edits = json.loads(a.edits)
        except Exception as e:
            sys.exit(f"error: --edits is not valid JSON ({e})")
        if isinstance(edits, dict):
            edits = [edits]
    else:
        if a.replace is None:
            sys.exit("error: -f/--find needs -r/--replace")
        e = {"find": a.find, "replace": a.replace}
        if a.near:
            e["near"] = a.near
        if a.column:
            e["column"] = a.column
        if a.occurrence:
            e["occurrence"] = a.occurrence
        if a.all:
            e["all"] = True
        edits = [e]

    stem, _ = os.path.splitext(a.pdf)
    out = a.out or stem + "-edited.pdf"
    if os.path.realpath(out) == os.path.realpath(a.pdf):
        sys.exit("error: output path equals the input — the original is never "
                 "overwritten; pick a different -o")
    try:
        rep = apply_edits(a.pdf, edits, out)
    except ValueError as e:
        sys.exit(f"error: {e}")
    print(json.dumps(rep, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _main()
