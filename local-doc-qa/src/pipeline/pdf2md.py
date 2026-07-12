#!/usr/bin/env python3
"""Convert a PDF to Markdown ONCE, on receipt. The .md becomes the source of truth.

Tables are extracted as real Markdown tables (PyMuPDF table detection) so structured
numeric queries work; surrounding prose is kept for RAG. After this, nothing re-reads
the PDF — both the structured engine and RAG operate on the .md.
"""
import os, re, sys, datetime
import fitz  # PyMuPDF
from config import KB_DIR

OUT_DIR = os.path.join(KB_DIR, "from-pdfs")


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", os.path.splitext(name)[0].lower()).strip("-")


def convert(pdf_path, out_dir=OUT_DIR, force=False):
    """Convert pdf_path -> <out_dir>/<slug>.md (once). Returns the .md path."""
    os.makedirs(out_dir, exist_ok=True)
    out_md = os.path.join(out_dir, _slug(os.path.basename(pdf_path)) + ".md")
    if os.path.exists(out_md) and not force and \
       os.path.getmtime(out_md) >= os.path.getmtime(pdf_path):
        return out_md  # already converted and up to date

    doc = fitz.open(pdf_path)
    today = datetime.date.today().isoformat()
    parts = [f"# {os.path.basename(pdf_path)}",
             f"\n_Converted from PDF on {today}. This .md is the source of truth; "
             f"the PDF is not re-read._\n"]
    for i, page in enumerate(doc, start=1):
        parts.append(f"\n## Page {i}\n")
        table_rects = []
        try:
            finder = page.find_tables()
            tables = list(finder.tables)
        except Exception:
            tables = []
        for t in tables:
            try:
                md = t.to_markdown()
            except Exception:
                md = None
            if md and md.strip():
                parts.append(md.strip() + "\n")
                table_rects.append(fitz.Rect(t.bbox))
        # prose: text not covered by a detected table
        prose = []
        for block in page.get_text("blocks"):
            x0, y0, x1, y1, txt = block[0], block[1], block[2], block[3], block[4]
            if not txt.strip():
                continue
            r = fitz.Rect(x0, y0, x1, y1)
            if any(r.intersects(tr) for tr in table_rects):
                continue  # already captured in a table
            prose.append(txt.strip())
        if prose:
            parts.append("\n".join(prose) + "\n")
    doc.close()
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    return out_md


if __name__ == "__main__":
    for p in sys.argv[1:]:
        out = convert(p, force="--force" in sys.argv)
        print(f"{p} -> {out}")
