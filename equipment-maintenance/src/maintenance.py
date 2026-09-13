#!/usr/bin/env python3
"""Equipment maintenance records — every machine, every job, every invoice,
from the day it was installed. One SQLite file; questions are commands.

    maintenance.py assets [--category press] [--tag digital]
    maintenance.py cost --annual-average                        # avg cost per machine per year
    maintenance.py cost --asset P-01 --by year,quarter --pivot # quarterly breakdown, one machine
    maintenance.py cost --category press --tag digital --by asset
    maintenance.py history P-01 [--since 2025-01-01] [--type corrective]
    maintenance.py show WO-2024-0003
    maintenance.py invoice EX-1001
    maintenance.py search "bearing" [--asset F-01]
    maintenance.py add-wo --asset F-01 --type corrective --summary "..." --hours 2 --rate 40
    maintenance.py import records.json
    maintenance.py render-invoices [--number N] [--missing]

Output is a GFM table (renders in chat apps) or --json. Stdlib only.

WHAT A COST IS. A work order's cost is in-house labour (hours x rate) + parts taken
from the shop's own stock + the total of every vendor invoice attached to it. Parts
bought for a job live on the invoice, never also on the work order, so nothing counts
twice. A cost is dated by the work order's close date (open date while it is open).

WHERE THE DATABASE IS, first match wins: --db, $MAINTENANCE_DB, the `maintenance`
capability's `db` in the agent profile (agent-profile.json, found via $AGENT_PROFILE
or by walking up from the working directory and from this file), else
maintenance.db next to this file.
"""
from __future__ import annotations

import argparse, datetime as dt, html, json, os, re, shutil, sqlite3, subprocess, sys
from collections import OrderedDict, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = "equipment-maintenance/1"
WO_TYPES = ("preventive", "corrective", "inspection", "calibration", "replacement",
            "service-contract", "installation", "upgrade")
DIMENSIONS = ("asset", "category", "year", "quarter", "month", "type", "system", "vendor")


# ------------------------------------------------------------------ location ---
def _find_profile():
    env = os.environ.get("AGENT_PROFILE")
    if env:
        return env if os.path.exists(env) else None
    for start in (os.getcwd(), HERE):
        d = os.path.abspath(start)
        while True:
            p = os.path.join(d, "agent-profile.json")
            if os.path.exists(p):
                return p
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return None


def db_path(explicit=None):
    if explicit:
        return explicit
    if os.environ.get("MAINTENANCE_DB"):
        return os.environ["MAINTENANCE_DB"]
    prof = _find_profile()
    if prof:
        try:
            data = json.load(open(prof))
            cap = (data.get("capabilities") or {}).get("maintenance") or {}
            if isinstance(cap, dict) and cap.get("db"):
                root = (data.get("workspace") or {}).get("root") or os.path.dirname(prof)
                p = cap["db"]
                return p if os.path.isabs(p) else os.path.join(root, p)
        except (OSError, ValueError):
            pass
    return os.path.join(HERE, "maintenance.db")


# -------------------------------------------------------------------- schema ---
DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS assets(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, manufacturer TEXT, model TEXT, serial TEXT,
  category TEXT, tags TEXT NOT NULL DEFAULT '', location TEXT,
  installed TEXT, retired TEXT, purchase_cost REAL, notes TEXT);
CREATE TABLE IF NOT EXISTS vendors(
  name TEXT PRIMARY KEY, address TEXT, phone TEXT, email TEXT, remit_to TEXT);
CREATE TABLE IF NOT EXISTS technicians(
  name TEXT PRIMARY KEY, role TEXT, hourly_rate REAL);
CREATE TABLE IF NOT EXISTS invoices(
  number TEXT PRIMARY KEY, vendor TEXT, date TEXT, due TEXT, terms TEXT, po TEXT,
  status TEXT, paid_on TEXT, field_tech TEXT, subtotal REAL NOT NULL DEFAULT 0,
  freight REAL NOT NULL DEFAULT 0, tax REAL NOT NULL DEFAULT 0,
  total REAL NOT NULL DEFAULT 0, notes TEXT, file TEXT);
CREATE TABLE IF NOT EXISTS invoice_lines(
  invoice TEXT NOT NULL REFERENCES invoices(number) ON DELETE CASCADE,
  line INTEGER NOT NULL, part_no TEXT, description TEXT, qty REAL, unit_price REAL,
  amount REAL, PRIMARY KEY(invoice, line));
CREATE TABLE IF NOT EXISTS work_orders(
  id TEXT PRIMARY KEY, asset TEXT NOT NULL REFERENCES assets(id),
  opened TEXT NOT NULL, closed TEXT, type TEXT, system TEXT, summary TEXT,
  description TEXT, performed_by TEXT, technicians TEXT NOT NULL DEFAULT '',
  vendor TEXT, labor_hours REAL NOT NULL DEFAULT 0, labor_rate REAL NOT NULL DEFAULT 0,
  downtime_hours REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'closed',
  labor_cost REAL NOT NULL DEFAULT 0, parts_cost REAL NOT NULL DEFAULT 0,
  invoiced REAL NOT NULL DEFAULT 0, total_cost REAL NOT NULL DEFAULT 0,
  cost_date TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wo_parts(
  wo TEXT NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
  line INTEGER NOT NULL, part_no TEXT, description TEXT, qty REAL, unit_cost REAL,
  amount REAL, PRIMARY KEY(wo, line));
CREATE TABLE IF NOT EXISTS wo_invoices(
  wo TEXT NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
  invoice TEXT NOT NULL REFERENCES invoices(number),
  PRIMARY KEY(wo, invoice));
CREATE INDEX IF NOT EXISTS idx_wo_asset_date ON work_orders(asset, cost_date);
CREATE INDEX IF NOT EXISTS idx_wo_date ON work_orders(cost_date);
CREATE INDEX IF NOT EXISTS idx_wi_invoice ON wo_invoices(invoice);
"""


def connect(path):
    new = not os.path.exists(path)
    if new:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    cx = sqlite3.connect(path)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA foreign_keys=ON")
    cx.executescript(DDL)
    try:
        cx.execute("CREATE VIRTUAL TABLE IF NOT EXISTS wo_fts USING fts5("
                   "id UNINDEXED, asset, summary, description, system, type, vendor, "
                   "technicians, parts, tokenize='porter unicode61')")
    except sqlite3.OperationalError:
        pass                                    # no FTS5 in this build: search uses LIKE
    return cx


def has_fts(cx):
    return bool(cx.execute("SELECT 1 FROM sqlite_master WHERE name='wo_fts'").fetchone())


def r2(x):
    return round(float(x or 0) + 1e-9, 2)


# -------------------------------------------------------------------- import ---
def _tags(v):
    if isinstance(v, str):
        v = re.split(r"[,\s]+", v)
    return "," + ",".join(sorted({t.strip().lower() for t in (v or []) if t.strip()})) + ","


def upsert_invoice(cx, inv):
    lines = inv.get("lines") or []
    sub = r2(sum(r2(float(l.get("qty") or 0) * float(l.get("unit_price") or 0)) for l in lines))
    freight, tax = r2(inv.get("freight")), r2(inv.get("tax"))
    total = r2(sub + freight + tax)
    old = cx.execute("SELECT file FROM invoices WHERE number=?", (inv["number"],)).fetchone()
    cx.execute("INSERT OR REPLACE INTO invoices(number, vendor, date, due, terms, po, status, "
               "paid_on, field_tech, subtotal, freight, tax, total, notes, file) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (inv["number"], inv.get("vendor"), inv.get("date"), inv.get("due"),
                inv.get("terms"), inv.get("po"), inv.get("status"), inv.get("paid_on"),
                inv.get("field_tech"), sub, freight, tax, total, inv.get("notes"),
                inv.get("file") or (old["file"] if old else None)))
    cx.execute("DELETE FROM invoice_lines WHERE invoice=?", (inv["number"],))
    for i, l in enumerate(lines, 1):
        q, u = float(l.get("qty") or 0), float(l.get("unit_price") or 0)
        cx.execute("INSERT INTO invoice_lines VALUES(?,?,?,?,?,?,?)",
                   (inv["number"], i, l.get("part_no"), l.get("description"), q, u, r2(q * u)))


def upsert_wo(cx, wo):
    if wo.get("type") and wo["type"] not in WO_TYPES:
        raise ValueError(f"{wo['id']}: type must be one of {', '.join(WO_TYPES)}")
    parts = wo.get("parts") or []
    labor = r2(float(wo.get("labor_hours") or 0) * float(wo.get("labor_rate") or 0))
    parts_cost = r2(sum(r2(float(p.get("qty") or 0) * float(p.get("unit_cost") or 0)) for p in parts))
    cx.execute("INSERT OR REPLACE INTO work_orders(id, asset, opened, closed, type, system, "
               "summary, description, performed_by, technicians, vendor, labor_hours, "
               "labor_rate, downtime_hours, status, labor_cost, parts_cost, invoiced, "
               "total_cost, cost_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?)",
               (wo["id"], wo["asset"], wo["opened"], wo.get("closed"), wo.get("type"),
                wo.get("system"), wo.get("summary"), wo.get("description"),
                wo.get("performed_by"), "; ".join(wo.get("technicians") or []),
                wo.get("vendor"), float(wo.get("labor_hours") or 0),
                float(wo.get("labor_rate") or 0), float(wo.get("downtime_hours") or 0),
                wo.get("status") or ("closed" if wo.get("closed") else "open"),
                labor, parts_cost, wo.get("closed") or wo["opened"]))
    cx.execute("DELETE FROM wo_parts WHERE wo=?", (wo["id"],))
    for i, p in enumerate(parts, 1):
        q, u = float(p.get("qty") or 0), float(p.get("unit_cost") or 0)
        cx.execute("INSERT INTO wo_parts VALUES(?,?,?,?,?,?,?)",
                   (wo["id"], i, p.get("part_no"), p.get("description"), q, u, r2(q * u)))
    cx.execute("DELETE FROM wo_invoices WHERE wo=?", (wo["id"],))
    for n in wo.get("invoices") or []:
        cx.execute("INSERT INTO wo_invoices VALUES(?,?)", (wo["id"], n))


def recompute(cx, ids=None):
    """Roll invoice totals into work-order costs and refresh the search index."""
    where, args = ("", ()) if ids is None else (
        f"WHERE w.id IN ({','.join('?' * len(ids))})", tuple(ids))
    cx.execute(f"""UPDATE work_orders AS w SET
        invoiced = COALESCE((SELECT ROUND(SUM(i.total), 2) FROM wo_invoices wi
                             JOIN invoices i ON i.number = wi.invoice WHERE wi.wo = w.id), 0)
        {where}""", args)
    cx.execute(f"UPDATE work_orders AS w SET total_cost = ROUND(labor_cost + parts_cost + invoiced, 2) {where}", args)
    if not has_fts(cx):
        return
    rows = cx.execute(f"SELECT * FROM work_orders w {where}", args).fetchall()
    for w in rows:
        parts = " ".join(f"{r['part_no'] or ''} {r['description'] or ''}" for r in cx.execute(
            "SELECT part_no, description FROM wo_parts WHERE wo=? UNION ALL "
            "SELECT l.part_no, l.description FROM wo_invoices wi JOIN invoice_lines l "
            "ON l.invoice = wi.invoice WHERE wi.wo=?", (w["id"], w["id"])))
        invs = " ".join(r[0] for r in cx.execute("SELECT invoice FROM wo_invoices WHERE wo=?", (w["id"],)))
        cx.execute("DELETE FROM wo_fts WHERE id=?", (w["id"],))
        cx.execute("INSERT INTO wo_fts VALUES(?,?,?,?,?,?,?,?,?)",
                   (w["id"], w["asset"], w["summary"], w["description"], w["system"],
                    w["type"], w["vendor"], w["technicians"], f"{parts} {invs}"))


def cmd_import(cx, a):
    data = json.load(open(a.file))
    if data.get("schema") not in (None, SCHEMA):
        sys.exit(f"unsupported schema {data.get('schema')!r} (expected {SCHEMA})")
    if a.replace:
        for t in ("wo_invoices", "wo_parts", "work_orders", "invoice_lines", "invoices",
                  "technicians", "vendors", "assets"):
            cx.execute(f"DELETE FROM {t}")
        if has_fts(cx):
            cx.execute("DELETE FROM wo_fts")
    for k in ("currency", "bill_to_name", "bill_to_address"):
        if data.get(k):
            cx.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, data[k]))
    for s in data.get("assets") or []:
        cx.execute("INSERT OR REPLACE INTO assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (s["id"], s["name"], s.get("manufacturer"), s.get("model"), s.get("serial"),
                    (s.get("category") or "").lower(), _tags(s.get("tags")), s.get("location"),
                    s.get("installed"), s.get("retired"), s.get("purchase_cost"), s.get("notes")))
    for v in data.get("vendors") or []:
        cx.execute("INSERT OR REPLACE INTO vendors VALUES(?,?,?,?,?)",
                   (v["name"], v.get("address"), v.get("phone"), v.get("email"), v.get("remit_to")))
    for t in data.get("technicians") or []:
        cx.execute("INSERT OR REPLACE INTO technicians VALUES(?,?,?)",
                   (t["name"], t.get("role"), t.get("hourly_rate")))
    for inv in data.get("invoices") or []:
        upsert_invoice(cx, inv)
    wos = data.get("work_orders") or []
    for wo in wos:
        upsert_wo(cx, wo)
    touched = {w["id"] for w in wos}
    for inv in data.get("invoices") or []:            # an invoice re-imported alone still re-rolls its WO
        touched |= {r[0] for r in cx.execute("SELECT wo FROM wo_invoices WHERE invoice=?", (inv["number"],))}
    recompute(cx, sorted(touched) if touched else [])
    cx.commit()
    problems = [f"work order {r[0]} names missing invoice {r[1]}" for r in cx.execute(
        "SELECT wi.wo, wi.invoice FROM wo_invoices wi LEFT JOIN invoices i ON i.number = wi.invoice "
        "WHERE i.number IS NULL")]
    problems += [f"invoice {r[0]} is attached to no work order (its cost is counted nowhere)"
                 for r in cx.execute("SELECT number FROM invoices WHERE number NOT IN (SELECT invoice FROM wo_invoices)")]
    problems += [f"work order {r[0]} names unknown asset {r[1]}" for r in cx.execute(
        "SELECT id, asset FROM work_orders WHERE asset NOT IN (SELECT id FROM assets)")]
    counts = {t: cx.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("assets", "work_orders", "invoices", "vendors")}
    print("imported:", ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items()))
    for p in problems[:20]:
        print("WARNING:", p)
    if len(problems) > 20:
        print(f"WARNING: … and {len(problems) - 20} more")
    return 1 if problems else 0


# ------------------------------------------------------------------- filters ---
def asset_filter(a):
    """SQL + args selecting assets by --asset / --category / --tag (all AND)."""
    where, args = [], []
    ids = [x.strip().upper() for v in (getattr(a, "asset", None) or []) for x in v.split(",") if x.strip()]
    if ids:
        where.append(f"s.id IN ({','.join('?' * len(ids))})"); args += ids
    if getattr(a, "category", None):
        where.append("s.category = ?"); args.append(a.category.lower())
    for t in getattr(a, "tag", None) or []:
        for x in t.split(","):
            if x.strip():
                where.append("s.tags LIKE ?"); args.append(f"%,{x.strip().lower()},%")
    return where, args


def wo_filter(a):
    where, args = asset_filter(a)
    if getattr(a, "since", None):
        where.append("w.cost_date >= ?"); args.append(a.since)
    if getattr(a, "until", None):
        where.append("w.cost_date <= ?"); args.append(a.until)
    if getattr(a, "type", None):
        where.append("w.type = ?"); args.append(a.type)
    if getattr(a, "system", None):
        where.append("LOWER(w.system) = ?"); args.append(a.system.lower())
    if getattr(a, "vendor", None):
        where.append("LOWER(w.vendor) LIKE ?"); args.append(f"%{a.vendor.lower()}%")
    if getattr(a, "status", None):
        where.append("w.status = ?"); args.append(a.status)
    return where, args


def _where(parts):
    return (" WHERE " + " AND ".join(parts)) if parts else ""


# ------------------------------------------------------------------- output ---
def money(x, cents=False):
    x = float(x or 0)
    s = f"{abs(x):,.2f}" if cents else f"{abs(round(x)):,.0f}"
    return ("-$" if x < 0 else "$") + s


def table(headers, rows, right=()):
    esc = lambda v: str("" if v is None else v).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---:" if i in right else "---" for i in range(len(headers))) + "|"]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def emit(a, obj, text):
    if a.json:
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(text)


def _matched_assets(cx, a):
    where, args = asset_filter(a)
    return [dict(r) for r in cx.execute(f"SELECT * FROM assets s{_where(where)} ORDER BY s.id", args)]


# ---------------------------------------------------------------- commands ---
def cmd_assets(cx, a):
    where, args = asset_filter(a)
    if a.search:
        where.append("(s.name LIKE ? OR s.manufacturer LIKE ? OR s.model LIKE ? OR s.notes LIKE ?)")
        args += [f"%{a.search}%"] * 4
    rows = [dict(r) for r in cx.execute(
        f"""SELECT s.*, COUNT(w.id) AS work_orders, COALESCE(SUM(w.total_cost),0) AS lifetime_cost,
            MAX(w.cost_date) AS last_work FROM assets s LEFT JOIN work_orders w ON w.asset = s.id
            {_where(where)} GROUP BY s.id ORDER BY s.id""", args)]
    for r in rows:
        r["tags"] = [t for t in (r["tags"] or "").split(",") if t]
    text = table(["ID", "Machine", "Category", "Tags", "Installed", "Jobs", "Lifetime cost", "Last work"],
                 [[r["id"], r["name"], r["category"], ", ".join(r["tags"]), r["installed"],
                   r["work_orders"], money(r["lifetime_cost"]), r["last_work"]] for r in rows],
                 right=(5, 6))
    emit(a, rows, text)


def cmd_history(cx, a):
    a.asset = [a.asset_id]
    where, args = wo_filter(a)
    lim = f" LIMIT {int(a.limit)}" if a.limit else ""
    rows = [dict(r) for r in cx.execute(
        f"""SELECT w.id, w.cost_date AS date, w.asset, w.type, w.system, w.summary, w.performed_by,
            w.vendor, w.downtime_hours, w.total_cost,
            (SELECT GROUP_CONCAT(invoice, ', ') FROM wo_invoices WHERE wo = w.id) AS invoices
            FROM work_orders w JOIN assets s ON s.id = w.asset{_where(where)}
            ORDER BY w.cost_date {'ASC' if a.oldest_first else 'DESC'}, w.id{lim}""", args)]
    total = sum(r["total_cost"] for r in rows)
    text = table(["WO", "Date", "Type", "System", "Summary", "By", "Down h", "Cost", "Invoices"],
                 [[r["id"], r["date"], r["type"], r["system"], r["summary"], r["performed_by"],
                   f"{r['downtime_hours']:g}", money(r["total_cost"], True), r["invoices"] or "—"]
                  for r in rows], right=(6, 7))
    text += f"\n\n{len(rows)} work orders, {money(total, True)}"
    emit(a, rows, text)


def cmd_show(cx, a):
    w = cx.execute("SELECT w.*, s.name AS asset_name, s.serial FROM work_orders w "
                   "JOIN assets s ON s.id = w.asset WHERE w.id = ?", (a.wo.upper(),)).fetchone()
    if not w:
        sys.exit(f"no work order {a.wo}")
    w = dict(w)
    w["parts"] = [dict(r) for r in cx.execute("SELECT part_no, description, qty, unit_cost, amount FROM wo_parts WHERE wo=? ORDER BY line", (w["id"],))]
    w["invoices"] = [dict(r) for r in cx.execute(
        "SELECT i.number, i.vendor, i.date, i.total, i.status, i.file FROM wo_invoices wi "
        "JOIN invoices i ON i.number = wi.invoice WHERE wi.wo=? ORDER BY i.date", (w["id"],))]
    L = [f"**{w['id']}** — {w['asset']} {w['asset_name']} (S/N {w['serial'] or '—'})",
         f"{w['summary']}", "",
         f"- Opened {w['opened']}, closed {w['closed'] or 'OPEN'} · {w['type']} · {w['system'] or '—'}",
         f"- Performed by {w['performed_by'] or '—'}: {w['technicians'] or '—'}" + (f" · vendor {w['vendor']}" if w["vendor"] else ""),
         f"- Downtime {w['downtime_hours']:g} h · in-house labour {w['labor_hours']:g} h × {money(w['labor_rate'], True)} = {money(w['labor_cost'], True)}",
         "", w["description"] or ""]
    if w["parts"]:
        L += ["", "Parts from stock:", table(["Part", "Description", "Qty", "Unit", "Amount"],
              [[p["part_no"], p["description"], f"{p['qty']:g}", money(p["unit_cost"], True), money(p["amount"], True)] for p in w["parts"]], right=(2, 3, 4))]
    if w["invoices"]:
        L += ["", "Invoices:", table(["Invoice", "Vendor", "Date", "Total", "Status", "PDF"],
              [[i["number"], i["vendor"], i["date"], money(i["total"], True), i["status"], i["file"] or "—"] for i in w["invoices"]], right=(3,))]
    L += ["", f"**Total cost {money(w['total_cost'], True)}** = labour {money(w['labor_cost'], True)} + stock parts {money(w['parts_cost'], True)} + invoiced {money(w['invoiced'], True)}"]
    emit(a, w, "\n".join(L))


def cmd_invoice(cx, a):
    i = cx.execute("SELECT * FROM invoices WHERE number = ?", (a.number,)).fetchone()
    if not i:
        sys.exit(f"no invoice {a.number}")
    i = dict(i)
    i["lines"] = [dict(r) for r in cx.execute("SELECT part_no, description, qty, unit_price, amount FROM invoice_lines WHERE invoice=? ORDER BY line", (a.number,))]
    i["work_orders"] = [r[0] for r in cx.execute("SELECT wo FROM wo_invoices WHERE invoice=?", (a.number,))]
    i["pdf"] = _abs_file(cx, i["file"])
    L = [f"**{i['number']}** — {i['vendor']}, {i['date']} (due {i['due'] or '—'}, {i['terms'] or '—'})",
         f"- PO {i['po'] or '—'} · {i['status'] or '—'}" + (f" {i['paid_on']}" if i["paid_on"] else "") + f" · work order {', '.join(i['work_orders']) or '—'}",
         "", table(["Part", "Description", "Qty", "Unit", "Amount"],
                   [[l["part_no"] or "", l["description"], f"{l['qty']:g}", money(l["unit_price"], True), money(l["amount"], True)] for l in i["lines"]], right=(2, 3, 4)),
         "", f"Subtotal {money(i['subtotal'], True)} · freight {money(i['freight'], True)} · tax {money(i['tax'], True)} · **total {money(i['total'], True)}**"]
    if i["notes"]:
        L += ["", i["notes"]]
    L += ["", f"PDF: {i['pdf'] or 'not rendered — run render-invoices'}"]
    emit(a, i, "\n".join(L))


def _fts_query(terms):
    toks = re.findall(r"[\w\-./]+", terms)
    return " ".join('"' + t.replace('"', "") + '"' for t in toks)


def cmd_search(cx, a):
    where, args = wo_filter(a)
    lim = int(a.limit or 25)
    if has_fts(cx):
        q = _fts_query(a.terms)
        if not q:
            sys.exit("nothing to search for")
        sql = (f"""SELECT w.id, w.cost_date AS date, w.asset, w.type, w.summary, w.total_cost,
                   snippet(wo_fts, 3, '**', '**', '…', 12) AS hit
                   FROM wo_fts JOIN work_orders w ON w.id = wo_fts.id JOIN assets s ON s.id = w.asset
                   WHERE wo_fts MATCH ?{(' AND ' + ' AND '.join(where)) if where else ''}
                   ORDER BY bm25(wo_fts), w.cost_date DESC LIMIT {lim}""")
        rows = [dict(r) for r in cx.execute(sql, [q] + args)]
    else:
        like = f"%{a.terms}%"
        where2 = where + ["(w.summary LIKE ? OR w.description LIKE ? OR w.system LIKE ? OR w.vendor LIKE ?)"]
        rows = [dict(r) for r in cx.execute(
            f"""SELECT w.id, w.cost_date AS date, w.asset, w.type, w.summary, w.total_cost, '' AS hit
                FROM work_orders w JOIN assets s ON s.id = w.asset{_where(where2)}
                ORDER BY w.cost_date DESC LIMIT {lim}""", args + [like] * 4)]
    text = table(["WO", "Date", "Asset", "Type", "Summary", "Cost"],
                 [[r["id"], r["date"], r["asset"], r["type"], r["summary"], money(r["total_cost"], True)] for r in rows],
                 right=(5,)) if rows else f"No work orders match {a.terms!r}."
    emit(a, rows, text)


def _key(dim, row, dims):
    d = row["cost_date"]
    if dim == "year":
        return d[:4]
    if dim == "quarter":
        q = f"Q{(int(d[5:7]) - 1) // 3 + 1}"
        return q if "year" in dims else f"{d[:4]}-{q}"
    if dim == "month":
        return d[5:7] if "year" in dims else d[:7]
    return row[dim] or "—"


def _axis(dim, keys, dims):
    """Every value a time axis should show between its first and last key — a
    quarterly breakdown with the empty quarters left out reads as if they never
    happened. Non-time dimensions just sort."""
    keys = sorted(set(keys))
    if not keys or dim not in ("year", "quarter", "month"):
        return keys
    if dim == "quarter" and "year" in dims:
        return ["Q1", "Q2", "Q3", "Q4"]
    if dim == "month" and "year" in dims:
        return [f"{m:02d}" for m in range(1, 13)]
    if dim == "year":
        return [str(y) for y in range(int(keys[0]), int(keys[-1]) + 1)]
    step = 3 if dim == "quarter" else 1
    idx = lambda k: int(k[:4]) * 12 + ((int(k[-1]) - 1) * 3 if dim == "quarter" else int(k[5:7]) - 1)
    lab = lambda i: (f"{i // 12}-Q{i % 12 // 3 + 1}" if dim == "quarter" else f"{i // 12}-{i % 12 + 1:02d}")
    return [lab(i) for i in range(idx(keys[0]), idx(keys[-1]) + 1, step)]


def cmd_cost(cx, a):
    dims = [x.strip() for x in (a.by or "").split(",") if x.strip()]
    bad = [x for x in dims if x not in DIMENSIONS]
    if bad or len(dims) > 2:
        sys.exit(f"--by takes one or two of: {', '.join(DIMENSIONS)}")
    if a.annual_average:
        return annual_average(cx, a)
    where, args = wo_filter(a)
    rows = [dict(r) for r in cx.execute(
        f"""SELECT w.*, s.category FROM work_orders w JOIN assets s ON s.id = w.asset
            {_where(where)} ORDER BY w.cost_date""", args)]
    assets = _matched_assets(cx, a)
    scope = _scope_line(a, assets, rows)
    fields = ("total_cost", "labor_cost", "parts_cost", "invoiced", "downtime_hours")

    if not dims:
        agg = {f: r2(sum(r[f] for r in rows)) for f in fields}
        agg["work_orders"] = len(rows)
        text = scope + "\n\n" + table(
            ["Work orders", "Downtime h", "Labour", "Stock parts", "Invoiced", "Total"],
            [[len(rows), f"{agg['downtime_hours']:,.1f}", money(agg["labor_cost"], a.cents), money(agg["parts_cost"], a.cents),
              money(agg["invoiced"], a.cents), money(agg["total_cost"], a.cents)]], right=range(6))
        return emit(a, {"scope": scope, "assets": [x["id"] for x in assets], **agg}, text)

    groups = OrderedDict()
    for r in rows:
        k = tuple(_key(d, r, dims) for d in dims)
        g = groups.setdefault(k, dict.fromkeys(fields, 0.0) | {"work_orders": 0})
        g["work_orders"] += 1
        for f in fields:
            g[f] += r[f]
    label = {"asset": "Asset", "category": "Category", "year": "Year", "quarter": "Quarter",
             "month": "Month", "type": "Type", "system": "System", "vendor": "Vendor"}
    names = {x["id"]: x["name"] for x in assets}

    if a.pivot and len(dims) == 2:
        cols = _axis(dims[1], [k[1] for k in groups], dims)
        rows_k = _axis(dims[0], [k[0] for k in groups], dims)
        body, col_tot = [], defaultdict(float)
        for rk in rows_k:
            cells, tot = [], 0.0
            for ck in cols:
                v = groups.get((rk, ck), {}).get("total_cost", 0.0)
                cells.append(money(v, a.cents) if v else "—"); tot += v; col_tot[ck] += v
            head = f"{rk} {names[rk]}" if dims[0] == "asset" and rk in names else rk
            body.append([head] + cells + [money(tot, a.cents)])
        body.append(["**Total**"] + [f"**{money(col_tot[c], a.cents)}**" for c in cols]
                    + [f"**{money(sum(col_tot.values()), a.cents)}**"])
        text = scope + "\n\n" + table([label[dims[0]]] + cols + ["Total"], body, right=range(1, len(cols) + 2))
        data = [{dims[0]: k[0], dims[1]: k[1], **{f: r2(v) if f != "work_orders" else v for f, v in g.items()}}
                for k, g in groups.items()]
        return emit(a, {"scope": scope, "rows": data}, text)

    if len(dims) == 1 and dims[0] in ("year", "quarter", "month") and a.sort != "cost":
        for k in _axis(dims[0], [k[0] for k in groups], dims):
            groups.setdefault((k,), dict.fromkeys(fields, 0.0) | {"work_orders": 0})
    ordered = sorted(groups.items(), key=(lambda kv: -kv[1]["total_cost"]) if a.sort == "cost" else (lambda kv: kv[0]))
    body = []
    for k, g in ordered:
        keys = [f"{k[i]} {names[k[i]]}" if d == "asset" and k[i] in names else k[i] for i, d in enumerate(dims)]
        body.append(keys + [g["work_orders"], f"{g['downtime_hours']:,.1f}", money(g["labor_cost"], a.cents),
                            money(g["parts_cost"], a.cents), money(g["invoiced"], a.cents), money(g["total_cost"], a.cents)])
    tot = {f: sum(g[f] for g in groups.values()) for f in fields}
    body.append(["**Total**"] + [""] * (len(dims) - 1) + [sum(g["work_orders"] for g in groups.values()),
                f"{tot['downtime_hours']:,.1f}", money(tot["labor_cost"], a.cents), money(tot["parts_cost"], a.cents),
                money(tot["invoiced"], a.cents), f"**{money(tot['total_cost'], a.cents)}**"])
    n = len(dims)
    text = scope + "\n\n" + table([label[d] for d in dims] + ["Jobs", "Down h", "Labour", "Stock parts", "Invoiced", "Total"],
                                  body, right=range(n, n + 6))
    data = [{**dict(zip(dims, k)), **{f: r2(v) if f != "work_orders" else v for f, v in g.items()}} for k, g in ordered]
    emit(a, {"scope": scope, "rows": data}, text)


def _scope_line(a, assets, rows):
    ids = ", ".join(x["id"] for x in assets) or "none"
    lo = a.since or (min((r["cost_date"] for r in rows), default=None))
    hi = a.until or (max((r["cost_date"] for r in rows), default=None))
    extra = [f"{k} {getattr(a, k)}" for k in ("type", "system", "vendor") if getattr(a, k, None)]
    return (f"Machines: {ids} · period {lo or '—'} to {hi or '—'}"
            + (f" · {'; '.join(extra)}" if extra else ""))


def annual_average(cx, a):
    today = a.until or dt.date.today().isoformat()
    assets = _matched_assets(cx, a)
    where, args = wo_filter(a)
    per = {r["asset"]: dict(r) for r in cx.execute(
        f"""SELECT w.asset, COUNT(*) AS jobs, SUM(w.total_cost) AS total, SUM(w.downtime_hours) AS down
            FROM work_orders w JOIN assets s ON s.id = w.asset{_where(where)} GROUP BY w.asset""", args)}
    body, data, tot_cost, tot_avg = [], [], 0.0, 0.0
    for s in assets:
        start = max(filter(None, [s["installed"], a.since])) if (s["installed"] or a.since) else None
        end = min(filter(None, [s["retired"], today]))
        if not start or start > end:
            continue
        years = max((dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days / 365.25, 1 / 12)
        g = per.get(s["id"], {"jobs": 0, "total": 0.0, "down": 0.0})
        avg = (g["total"] or 0) / years
        tot_cost += g["total"] or 0; tot_avg += avg
        body.append([f"{s['id']} {s['name']}", s["installed"], f"{years:.1f}", g["jobs"],
                     money(g["total"], a.cents), money(avg, a.cents), f"{(g['down'] or 0) / years:,.0f}"])
        data.append({"asset": s["id"], "name": s["name"], "installed": s["installed"], "years": round(years, 2),
                     "work_orders": g["jobs"], "total_cost": r2(g["total"]), "avg_per_year": r2(avg),
                     "downtime_hours_per_year": round((g["down"] or 0) / years, 1)})
    body.sort(key=lambda b: -float(re.sub(r"[^\d.\-]", "", b[5]) or 0))
    body.append(["**All machines**", "", "", sum(d["work_orders"] for d in data),
                 f"**{money(tot_cost, a.cents)}**", f"**{money(tot_avg, a.cents)}**", ""])
    text = (f"Average maintenance cost per year in service, through {today}"
            + (f", from {a.since}" if a.since else "") + ".\n\n"
            + table(["Machine", "Installed", "Years", "Jobs", "Total", "Avg / year", "Down h / yr"],
                    body, right=(2, 3, 4, 5, 6)))
    emit(a, data, text)


def _next_wo_id(cx, date):
    y = date[:4]
    last = cx.execute("SELECT id FROM work_orders WHERE id LIKE ? ORDER BY id DESC LIMIT 1", (f"WO-{y}-%",)).fetchone()
    n = int(last[0].rsplit("-", 1)[1]) + 1 if last else 1
    return f"WO-{y}-{n:04d}"


def cmd_add_wo(cx, a):
    if not cx.execute("SELECT 1 FROM assets WHERE id=?", (a.asset.upper(),)).fetchone():
        sys.exit(f"no asset {a.asset}")
    opened = a.opened or dt.date.today().isoformat()
    parts = []
    for p in a.part or []:
        pn, desc, qty, unit = (p.split("|") + ["", "", "1", "0"])[:4]
        parts.append({"part_no": pn or None, "description": desc, "qty": float(qty or 1), "unit_cost": float(unit or 0)})
    wo = {"id": a.id or _next_wo_id(cx, opened), "asset": a.asset.upper(), "opened": opened,
          "closed": None if a.open else (a.closed or opened), "type": a.type, "system": a.system,
          "summary": a.summary, "description": a.description, "performed_by": a.performed_by,
          "technicians": a.tech or [], "vendor": a.vendor_name, "labor_hours": a.hours,
          "labor_rate": a.rate, "downtime_hours": a.downtime, "parts": parts,
          "invoices": a.invoice or [], "status": "open" if a.open else "closed"}
    missing = [n for n in wo["invoices"] if not cx.execute("SELECT 1 FROM invoices WHERE number=?", (n,)).fetchone()]
    if missing:
        sys.exit(f"unknown invoice(s): {', '.join(missing)} — import them first")
    upsert_wo(cx, wo)
    recompute(cx, [wo["id"]])
    cx.commit()
    a.wo = wo["id"]
    cmd_show(cx, a)


def cmd_meta(cx, a):
    if a.value is not None:
        cx.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (a.key, a.value)); cx.commit()
    for r in cx.execute("SELECT key, value FROM meta" + (" WHERE key=?" if a.key else ""), ((a.key,) if a.key else ())):
        print(f"{r[0]} = {r[1]}")


# ---------------------------------------------------------------- invoices ---
def _abs_file(cx, rel):
    if not rel:
        return None
    base = os.path.dirname(os.path.abspath(cx.execute("PRAGMA database_list").fetchone()[2]))
    p = rel if os.path.isabs(rel) else os.path.join(base, rel)
    return p if os.path.exists(p) else None


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "vendor").lower()).strip("-")


INVOICE_CSS = """
body{font-family:Helvetica,Arial,sans-serif;color:#222;font-size:10.5pt;margin:0}
.page{padding:36px 44px}
.top{display:table;width:100%}.top>div{display:table-cell;vertical-align:top}
.vendor .name{font-size:20pt;font-weight:bold;color:#1d3557;letter-spacing:.3px}
.vendor .addr{white-space:pre-line;color:#555;margin-top:4px;line-height:1.35}
.doc{text-align:right}.doc h1{margin:0;font-size:24pt;color:#1d3557;letter-spacing:3px}
.doc table{margin-left:auto;margin-top:8px;border-collapse:collapse}
.doc td{padding:2px 0 2px 14px;text-align:right}.doc td.k{color:#777}
.bill{display:table;width:100%;margin-top:26px}.bill>div{display:table-cell;width:50%;vertical-align:top}
.lbl{font-size:8pt;color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:3px}
.bill .v{white-space:pre-line;line-height:1.35}
table.lines{width:100%;border-collapse:collapse;margin-top:24px}
table.lines th{background:#1d3557;color:#fff;font-weight:normal;text-align:left;padding:6px 8px;font-size:9pt}
table.lines td{padding:6px 8px;border-bottom:1px solid #e3e3e3;vertical-align:top}
.num{text-align:right;white-space:nowrap}
.tot{width:42%;margin-left:auto;border-collapse:collapse;margin-top:10px}
.tot td{padding:4px 8px}.tot tr.g td{border-top:2px solid #1d3557;font-weight:bold;font-size:12pt}
.notes{margin-top:26px;color:#555;font-size:9pt;white-space:pre-line}
.stamp{position:absolute;top:210px;right:70px;border:3px solid #2a9d8f;color:#2a9d8f;font-size:22pt;
 font-weight:bold;padding:4px 16px;transform:rotate(-12deg);opacity:.75;letter-spacing:2px}
.foot{margin-top:34px;border-top:1px solid #ddd;padding-top:8px;color:#888;font-size:8.5pt}
"""


def invoice_html(cx, number):
    i = dict(cx.execute("SELECT * FROM invoices WHERE number=?", (number,)).fetchone())
    v = cx.execute("SELECT * FROM vendors WHERE name=?", (i["vendor"],)).fetchone()
    v = dict(v) if v else {"name": i["vendor"], "address": "", "phone": "", "email": "", "remit_to": ""}
    meta = dict(cx.execute("SELECT key, value FROM meta").fetchall())
    lines = cx.execute("SELECT * FROM invoice_lines WHERE invoice=? ORDER BY line", (number,)).fetchall()
    eq = cx.execute("SELECT s.id, s.name, s.serial, w.id AS wo FROM wo_invoices wi JOIN work_orders w ON w.id = wi.wo "
                    "JOIN assets s ON s.id = w.asset WHERE wi.invoice=? LIMIT 1", (number,)).fetchone()
    e = lambda s: html.escape(str(s or ""))
    m = lambda x: f"${float(x or 0):,.2f}"
    rows = "".join(f"<tr><td>{e(l['part_no'])}</td><td>{e(l['description'])}</td>"
                   f"<td class=num>{float(l['qty']):g}</td><td class=num>{m(l['unit_price'])}</td>"
                   f"<td class=num>{m(l['amount'])}</td></tr>" for l in lines)
    contact = " · ".join(x for x in (v.get("phone"), v.get("email")) if x)
    equip = (f"{e(eq['name'])}\nSerial {e(eq['serial'] or '—')}" if eq else "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{INVOICE_CSS}</style></head><body><div class=page>
{'<div class=stamp>PAID ' + e(i['paid_on'] or '') + '</div>' if (i['status'] or '').lower() == 'paid' else ''}
<div class=top><div class=vendor><div class=name>{e(v['name'])}</div><div class=addr>{e(v.get('address'))}\n{e(contact)}</div></div>
<div class=doc><h1>INVOICE</h1><table>
<tr><td class=k>Invoice #</td><td><b>{e(i['number'])}</b></td></tr>
<tr><td class=k>Date</td><td>{e(i['date'])}</td></tr>
<tr><td class=k>Due</td><td>{e(i['due'])}</td></tr>
<tr><td class=k>Terms</td><td>{e(i['terms'])}</td></tr>
<tr><td class=k>Customer PO</td><td>{e(i['po'] or '—')}</td></tr></table></div></div>
<div class=bill><div><div class=lbl>Bill to</div><div class=v><b>{e(meta.get('bill_to_name', ''))}</b>\n{e(meta.get('bill_to_address', ''))}</div></div>
<div><div class=lbl>Equipment serviced</div><div class=v>{equip}</div>
{('<div class=lbl style="margin-top:10px">Field technician</div><div class=v>' + e(i['field_tech']) + '</div>') if i['field_tech'] else ''}</div></div>
<table class=lines><tr><th style="width:17%">Part #</th><th>Description</th><th class=num>Qty</th><th class=num>Unit price</th><th class=num>Amount</th></tr>{rows}</table>
<table class=tot><tr><td>Subtotal</td><td class=num>{m(i['subtotal'])}</td></tr>
<tr><td>Freight</td><td class=num>{m(i['freight'])}</td></tr>
<tr><td>Sales tax</td><td class=num>{m(i['tax'])}</td></tr>
<tr class=g><td>Total due ({e(meta.get('currency', 'USD'))})</td><td class=num>{m(i['total'])}</td></tr></table>
<div class=notes>{e(i['notes'])}</div>
<div class=foot>{('Remit to: ' + e(v.get('remit_to'))) if v.get('remit_to') else ''}</div>
</div></body></html>"""


def cmd_render(cx, a):
    base = os.path.dirname(os.path.abspath(cx.execute("PRAGMA database_list").fetchone()[2]))
    out_root = a.out or os.path.join(base, "invoices")
    engine = shutil.which("wkhtmltopdf")
    sql, args = "SELECT number, vendor, date, file FROM invoices", []
    if a.number:
        sql += " WHERE number IN (%s)" % ",".join("?" * len(a.number)); args = a.number
    todo = [dict(r) for r in cx.execute(sql + " ORDER BY date", args)]
    if a.missing:
        todo = [r for r in todo if not _abs_file(cx, r["file"])]
    done = 0
    for r in todo:
        d = os.path.join(out_root, _slug(r["vendor"]), r["date"][:4])
        os.makedirs(d, exist_ok=True)
        stem = os.path.join(d, re.sub(r"[^\w.\-]", "_", r["number"]))
        page = invoice_html(cx, r["number"])
        if engine:
            src = stem + ".html"
            open(src, "w").write(page)
            p = subprocess.run([engine, "--quiet", "--page-size", "Letter", "--margin-top", "8mm",
                                "--margin-bottom", "8mm", "--margin-left", "6mm", "--margin-right", "6mm",
                                src, stem + ".pdf"], capture_output=True, text=True)
            os.remove(src)
            if p.returncode != 0 or not os.path.exists(stem + ".pdf"):
                print(f"WARNING: {r['number']}: {p.stderr.strip()[:200]}")
                continue
            path = stem + ".pdf"
        else:
            path = stem + ".html"
            open(path, "w").write(page)
        cx.execute("UPDATE invoices SET file=? WHERE number=?", (os.path.relpath(path, base), r["number"]))
        done += 1
        if done % 50 == 0:
            cx.commit(); print(f"… {done}/{len(todo)}", flush=True)
    cx.commit()
    print(f"rendered {done} invoice{'s' if done != 1 else ''} as {'PDF' if engine else 'HTML (install wkhtmltopdf for PDF)'} under {out_root}")


# --------------------------------------------------------------------- main ---
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--db", help="database file (see WHERE THE DATABASE IS)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def filters(p, dates=True, wo=True):
        p.add_argument("--asset", action="append", help="asset id(s), repeat or comma-separate")
        p.add_argument("--category", help="e.g. press, finishing")
        p.add_argument("--tag", action="append", help="all must match: --tag digital --tag inkjet")
        if dates:
            p.add_argument("--since", help="YYYY-MM-DD, inclusive")
            p.add_argument("--until", help="YYYY-MM-DD, inclusive")
        if wo:
            p.add_argument("--type", choices=WO_TYPES)
            p.add_argument("--system", help="e.g. printheads, UV curing")
            p.add_argument("--vendor", help="substring of vendor name")
            p.add_argument("--status", choices=("open", "closed"))

    p = sub.add_parser("assets", help="list machines with lifetime cost"); filters(p, dates=False, wo=False)
    p.add_argument("--search"); p.set_defaults(fn=cmd_assets)

    p = sub.add_parser("history", help="work orders for one machine, newest first")
    p.add_argument("asset_id"); filters(p)
    p.add_argument("--limit", type=int); p.add_argument("--oldest-first", action="store_true")
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("show", help="one work order in full"); p.add_argument("wo"); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("invoice", help="one invoice with lines and PDF path"); p.add_argument("number"); p.set_defaults(fn=cmd_invoice)

    p = sub.add_parser("search", help="full-text search over jobs, parts and invoice lines")
    p.add_argument("terms"); filters(p); p.add_argument("--limit", type=int); p.set_defaults(fn=cmd_search)

    p = sub.add_parser("cost", help="cost totals, grouped, pivoted or averaged per year")
    filters(p)
    p.add_argument("--by", help=f"one or two of {', '.join(DIMENSIONS)} (e.g. asset,year)")
    p.add_argument("--pivot", action="store_true", help="with two --by dimensions: second becomes columns")
    p.add_argument("--annual-average", action="store_true", help="per machine: total / years in service")
    p.add_argument("--sort", choices=("key", "cost"), default="key")
    p.add_argument("--cents", action="store_true", help="show cents (default whole dollars)")
    p.set_defaults(fn=cmd_cost)

    p = sub.add_parser("add-wo", help="record a new work order")
    p.add_argument("--asset", required=True); p.add_argument("--id")
    p.add_argument("--opened"); p.add_argument("--closed"); p.add_argument("--open", action="store_true")
    p.add_argument("--type", choices=WO_TYPES, required=True); p.add_argument("--system")
    p.add_argument("--summary", required=True); p.add_argument("--description")
    p.add_argument("--performed-by", default="in-house"); p.add_argument("--tech", action="append")
    p.add_argument("--vendor", dest="vendor_name"); p.add_argument("--hours", type=float, default=0)
    p.add_argument("--rate", type=float, default=0); p.add_argument("--downtime", type=float, default=0)
    p.add_argument("--part", action="append", help='"PART#|description|qty|unit cost" (stock parts)')
    p.add_argument("--invoice", action="append", help="attach an already-imported invoice")
    p.set_defaults(fn=cmd_add_wo)

    p = sub.add_parser("import", help=f"load a {SCHEMA} JSON file (upsert)")
    p.add_argument("file"); p.add_argument("--replace", action="store_true", help="wipe records first")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("render-invoices", help="write each invoice as a PDF (wkhtmltopdf) and link it")
    p.add_argument("--number", action="append"); p.add_argument("--missing", action="store_true")
    p.add_argument("--out"); p.set_defaults(fn=cmd_render)

    p = sub.add_parser("meta", help="show or set bill_to_name, bill_to_address, currency")
    p.add_argument("key", nargs="?"); p.add_argument("value", nargs="?"); p.set_defaults(fn=cmd_meta)

    a = ap.parse_args(argv)
    cx = connect(db_path(a.db))
    return a.fn(cx, a) or 0


if __name__ == "__main__":
    sys.exit(main())
