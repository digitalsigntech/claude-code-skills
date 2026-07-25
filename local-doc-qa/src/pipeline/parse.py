"""Local document parsing → text chunks. No network, no cloud.

Supports: PDF (PyMuPDF), CSV/TSV (pandas), and plain text/markdown/json/code.
Each chunk carries a locator (page / row-range) so answers can cite sources.
"""
import os, json, csv
import fitz  # PyMuPDF
import pandas as pd
from config import CHUNK_CHARS, CHUNK_OVERLAP

TEXT_EXTS = {".txt", ".md", ".markdown", ".json", ".log", ".py", ".js",
             ".html", ".xml", ".yaml", ".yml", ".rtf"}

# Binary formats we can't extract text from locally — skip instead of falling
# through to the text parser (a multi-MB image read as text becomes thousands
# of garbage chunks and pegs the embedding server for minutes). Images/videos/
# audio are here as a backstop: docpipe routes them to the CLIP media index +
# whisper transcription BEFORE parse_file. .doc is the legacy pre-2007 Word
# binary format (no dependency-free converter); .docx is parsed below.
SKIP_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif",
             ".tif", ".tiff", ".svg", ".ico", ".mp4", ".mov", ".avi", ".mkv",
             ".webm", ".m4v", ".mp3", ".wav", ".m4a", ".ogg", ".opus", ".flac",
             ".zip", ".gz", ".tgz", ".tar", ".7z", ".rar", ".exe", ".dll",
             ".so", ".bin", ".iso", ".dmg", ".doc"}


def _looks_binary(path, probe=8192):
    try:
        with open(path, "rb") as f:
            head = f.read(probe)
    except OSError:
        return True
    if b"\x00" in head:
        return True
    # mostly non-text bytes -> binary
    textish = sum(1 for b in head if 32 <= b < 127 or b in (9, 10, 13))
    return bool(head) and textish / len(head) < 0.7


def _chunk(text, max_chars=CHUNK_CHARS, overlap=CHUNK_OVERLAP):
    """Split text into overlapping chunks on paragraph/word boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        # try to break on a newline or space near the end
        if end < len(text):
            brk = text.rfind("\n", start + max_chars // 2, end)
            if brk == -1:
                brk = text.rfind(" ", start + max_chars // 2, end)
            if brk != -1:
                end = brk
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def _chunk_price_records(text):
    """For value/price-table pages: make each chunk end at a currency token so an
    item's name/description and its value stay together (fixes column misalignment)."""
    import re
    prices = list(re.finditer(r"\$\s?[\d,]+(?:\.\d{2})?", text))
    if len(prices) < 2:
        return _chunk(text)
    records, start = [], 0
    for m in prices:
        rec = text[start:m.end()].strip()
        if rec:
            records.append(rec)
        start = m.end()
    tail = text[start:].strip()
    if tail and records:
        records[-1] = records[-1] + "\n" + tail
    elif tail:
        records.append(tail)
    return records


def _parse_pdf(path):
    items = []
    doc = fitz.open(path)
    for i, page in enumerate(doc, start=1):
        ptext = page.get_text("text")
        # value-table page (many $ tokens) -> record-aware chunking; else normal
        chunks = _chunk_price_records(ptext) if ptext.count("$") >= 4 else _chunk(ptext)
        for c in chunks:
            items.append({"locator": f"p.{i}", "text": c})
    doc.close()
    return items


def _parse_csv(path, sep=None):
    # sniff delimiter for .tsv vs .csv
    if sep is None:
        sep = "\t" if path.lower().endswith(".tsv") else ","
    df = pd.read_csv(path, sep=sep, dtype=str, keep_default_na=False)
    items = []
    cols = list(df.columns)
    # 1) a schema/summary chunk so the model knows the table shape
    summary = (f"CSV table '{os.path.basename(path)}': {len(df)} rows, "
               f"{len(cols)} columns.\nColumns: {', '.join(cols)}")
    items.append({"locator": "schema", "text": summary})
    # 2) row chunks (grouped) rendered as readable key:value records
    rows_per_chunk = max(1, CHUNK_CHARS // max(60, len(cols) * 18))
    block, first = [], 1
    for idx, row in df.iterrows():
        rec = "; ".join(f"{c}={row[c]}" for c in cols)
        block.append(f"[row {idx + 1}] {rec}")
        if len(block) >= rows_per_chunk:
            items.append({"locator": f"rows {first}-{idx + 1}",
                          "text": "\n".join(block)})
            block, first = [], idx + 2
    if block:
        items.append({"locator": f"rows {first}-{len(df)}", "text": "\n".join(block)})
    return items


def _parse_docx(path):
    """Word .docx -> text via stdlib (a .docx is a zip; paragraphs live in
    word/document.xml as <w:p>/<w:t> elements). No python-docx dependency."""
    import zipfile
    from xml.etree import ElementTree as ET
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(W + "p"):
        t = "".join(n.text or "" for n in p.iter(W + "t"))
        if t.strip():
            paras.append(t.strip())
    text = "\n".join(paras)
    return [{"locator": f"chunk {i+1}", "text": c}
            for i, c in enumerate(_chunk(text))]


def _parse_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    if path.lower().endswith(".json"):
        try:
            raw = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except Exception:
            pass
    return [{"locator": f"chunk {i+1}", "text": c} for i, c in enumerate(_chunk(raw))]


def parse_file(path):
    """Return list of {source, locator, text} chunks for a single file."""
    ext = os.path.splitext(path)[1].lower()
    if ext in SKIP_EXTS:
        print(f"  skipping media/binary file (no text extraction): {path}")
        return []
    if ext == ".pdf":
        items = _parse_pdf(path)
    elif ext == ".docx":
        items = _parse_docx(path)
    elif ext in (".csv", ".tsv"):
        items = _parse_csv(path)
    elif ext in TEXT_EXTS:
        items = _parse_text(path)
    elif _looks_binary(path):
        print(f"  skipping binary file (no text extraction): {path}")
        return []
    else:
        # best-effort: try as text
        items = _parse_text(path)
    src = os.path.abspath(path)
    return [{"source": src, "locator": it["locator"], "text": it["text"]}
            for it in items if it["text"].strip()]


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        ch = parse_file(p)
        print(f"{p}: {len(ch)} chunks")
        if ch:
            print("  first:", ch[0]["locator"], "->", ch[0]["text"][:120].replace("\n", " "))
