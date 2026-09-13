# equipment-maintenance

Maintenance records for every machine in a plant — from the day it was installed —
kept so that an agent can answer cost and history questions with one command and
hand back a table.

- **Work orders**: date, machine, type (preventive, corrective, inspection,
  calibration, replacement, service-contract, installation, upgrade), system,
  one-line summary, full narrative, technicians, vendor, labour hours and rate,
  downtime, parts taken from stock.
- **Vendor invoices**: number, date, terms, PO, line items, freight, tax, paid status —
  each rendered to a PDF and linked from its work order.
- **Machines**: id, name, manufacturer, model, serial, category, tags, install date.
- **Search**: full-text over job narratives, parts and invoice lines (SQLite FTS5).

Stdlib Python 3.9+. One file, `src/maintenance.py`. PDFs need `wkhtmltopdf`; without it
invoices are written as HTML.

## The questions it is built for

| Question | Command |
|---|---|
| Average maintenance cost of each machine per year | `maintenance.py cost --annual-average` |
| Quarterly breakdown for one machine | `maintenance.py cost --asset P-01 --by year,quarter --pivot` |
| What does it cost to maintain all our digital presses? | `maintenance.py cost --category press --tag digital --by asset` |
| … per year, side by side | `maintenance.py cost --category press --tag digital --by asset,year --pivot` |
| Where does the money go on the inkjets? | `maintenance.py cost --tag inkjet --by system --sort cost` |
| What did we spend with one vendor last year? | `maintenance.py cost --vendor exampleoem --since 2025-01-01 --until 2025-12-31 --by asset` |
| Every job on a machine | `maintenance.py history P-01 [--since 2025-01-01] [--type corrective]` |
| One job in full, with its invoices | `maintenance.py show WO-2024-0003` |
| One invoice, with lines and PDF path | `maintenance.py invoice EX-1001` |
| When did we last replace a damper? | `maintenance.py search "damper"` |
| Record a job | `maintenance.py add-wo --asset F-01 --type corrective --summary "…" --hours 2 --rate 40 --tech "…"` |

Filters combine (AND) on every query command: `--asset` (repeat or comma-separate),
`--category`, `--tag` (repeat: all must match), `--since` / `--until`, `--type`,
`--system`, `--vendor` (substring), `--status`. `--by` takes one or two of `asset,
category, year, quarter, month, type, system, vendor`; `--pivot` spreads the second
across columns. Time axes show every period in range, empty ones included. `--json`
on anything for programmatic use; `--cents` for exact money in cost tables.

Every cost table opens with the machines the filter matched and the period, so an
answer about "digital presses" shows exactly which presses it counted.

## What a cost is

A work order's cost = in-house labour (hours × rate) + parts from the shop's own stock
+ the totals of the vendor invoices attached to it. Parts bought for a job live only on
the invoice, so nothing is counted twice. A cost is dated by the work order's close date.

This is **maintenance** cost. "What does it cost to run the presses" in the full sense
also includes ink, substrate, operators and power, which are not maintenance records —
an agent answering that question should say what the number covers.

`--annual-average` divides each machine's total by its years in service (install date,
or `--since` if later, to `--until` or today), so a machine installed last year is not
flattered against one installed seven years ago.

## Data model

One SQLite file: `assets`, `work_orders`, `wo_parts`, `invoices`, `invoice_lines`,
`wo_invoices` (a work order may carry several invoices; each invoice belongs to one work
order), `vendors`, `technicians`, `meta` (bill-to name/address, currency) and the
`wo_fts` search index. Rolled-up costs on each work order are recomputed on every write.

Records load from a JSON file (`schema: "equipment-maintenance/1"`, see
`src/example-records.json`) with `import`, which upserts — re-import a corrected file,
or a file holding only new invoices, and the affected work orders re-total. Import
warns about invoices attached to no work order (their cost would count nowhere) and
work orders naming unknown invoices or machines.

## Install

See [`AGENT-INSTALL.md`](AGENT-INSTALL.md) — written for an agent to follow.

## Files

| File | |
|---|---|
| `src/maintenance.py` | Store, importer, queries, invoice renderer. `--help` lists everything. |
| `src/example-records.json` | A three-machine example in the import format. |
