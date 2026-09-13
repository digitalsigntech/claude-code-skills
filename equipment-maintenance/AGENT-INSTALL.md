# Agent install brief — equipment-maintenance

You are an agent installing maintenance records for the machines of the business you
work for. At the end, any question about maintenance history or cost is one command
whose output you can show as-is.

## 1. Place it

```bash
mkdir -p <WORKSPACE>/operations/maintenance
cp equipment-maintenance/src/maintenance.py <WORKSPACE>/operations/maintenance/
command -v wkhtmltopdf || sudo apt-get install -y wkhtmltopdf   # invoice PDFs; optional
```

## 2. Declare it in the agent profile

In `<WORKSPACE>/agent-profile.json`, under `capabilities`:

```json
"maintenance": {"enabled": true, "db": "operations/maintenance/maintenance.db",
                "cli": "operations/maintenance/maintenance.py"}
```

The CLI finds its database from this entry, and the rendered identity block lists it
under "What this machine provides", so every session on every road knows it exists.
Then add one line to the deployment's persona file (`agent.system_prompt_file`), in
the deployment's own words — something like:

> Equipment maintenance history and cost questions are answered with
> `python3 operations/maintenance/maintenance.py` (`--help` lists the queries). Show
> its tables as they are, and say that the figures are maintenance cost only.

## 3. Load the records

Build a JSON file in the `equipment-maintenance/1` format (`src/example-records.json`
shows every field) from whatever the business has — a maintenance log, AP bills, OEM
service reports — and import it:

```bash
cd <WORKSPACE>/operations/maintenance
python3 maintenance.py import records.json          # prints counts and WARNINGs
python3 maintenance.py meta bill_to_name "<company legal name>"
python3 maintenance.py meta bill_to_address "<street>
<city, state zip>"
python3 maintenance.py render-invoices --missing    # PDFs under ./invoices/<vendor>/<year>/
```

Resolve every WARNING before calling it done: an invoice attached to no work order is
money the cost queries cannot see.

Tags are how people will ask. Tag each machine with the words someone would use to
group it — process (`digital`, `flexo`, `inkjet`, `offset`), features (`printheads`,
`uv`), function (`die-cut`, `inspection`) — and use `category` for the kind of machine
(`press`, `finishing`). A hybrid machine is a judgement call: decide whether it belongs
under `digital`, and say so where the owner will see it.

## 4. Verify

```bash
python3 maintenance.py assets
python3 maintenance.py cost --annual-average
python3 maintenance.py cost --asset <ID> --by year,quarter --pivot
python3 maintenance.py search "<a part you know was replaced>"
```

Then ask yourself, from outside the workspace directory, "what's the average
maintenance cost of our machines per year?" — the answer should be that table.
