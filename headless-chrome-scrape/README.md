# Headless Chrome scrape

Read the *real*, JavaScript-rendered content of a web page — including single-page
apps (SPAs) whose HTML shell is empty until scripts run in a browser. A plain HTTP
fetch of such a page returns only the skeleton; this technique renders the DOM with
headless Chrome first, then parses the result into plain text (and can pull out just
the lines around the labels you care about).

## What it does

- Drives **headless Chrome** to fully render a page's DOM (running its JavaScript).
- Converts the rendered HTML to readable plain text (strips `<script>`/`<style>`,
  drops tags, unescapes entities, keeps substantive lines).
- Optionally filters to only the lines mentioning labels you name
  (e.g. `Price`, `SKU`, `Total`).
- Handles flaky renders (transient TLS/network errors, or a page that only rendered
  its nav/footer on a slow pass) by **retrying with a larger virtual-time-budget**.

## How it works

The core is one Chrome invocation:

```bash
google-chrome --headless=new --no-sandbox --disable-gpu --dump-dom \
  --virtual-time-budget=20000 "<URL>" > /tmp/page.html
```

- `--headless=new` runs Chrome with no UI.
- `--dump-dom` prints the rendered DOM (post-JavaScript) to stdout.
- `--virtual-time-budget=20000` lets timers/async work run for ~20s of virtual
  time before dumping, so lazily-loaded content appears.
- `--no-sandbox --disable-gpu` keep it happy on headless servers/containers.

Then a small parser turns that HTML into text. The helper script
`src/render_and_extract.py` wraps both steps and adds retry logic.

| File | Purpose |
|------|---------|
| `src/render_and_extract.py` | Render a URL with headless Chrome + parse to text; `--label` filtering; auto-retry with growing budget. |

## Prerequisites

- **Chrome or Chromium** on `PATH` (`google-chrome`, `google-chrome-stable`,
  `chromium`, or `chromium-browser`).
- **Python 3** — standard library only, no `pip install` needed.

## Install / setup

No install step. Drop the folder anywhere and run:

```bash
python3 src/render_and_extract.py "<URL>"
```

Quick manual (no-script) version, straight from the shell:

```bash
google-chrome --headless=new --no-sandbox --disable-gpu --dump-dom \
  --virtual-time-budget=20000 "<URL>" > /tmp/page.html
# then eyeball or grep /tmp/page.html
```

## Usage

```bash
# Rendered visible text of a page
python3 src/render_and_extract.py "<URL>"

# Also keep the raw rendered HTML
python3 src/render_and_extract.py "<URL>" --html-out /tmp/page.html

# Only lines that mention any of these labels (case-insensitive)
python3 src/render_and_extract.py "<URL>" --label Price --label Total --label SKU

# Be patient with slow / heavy pages (start at 40s, up to 4 attempts)
python3 src/render_and_extract.py "<URL>" --budget 40000 --retries 4

# Point at a specific browser binary
python3 src/render_and_extract.py "<URL>" --chrome /usr/bin/chromium
```

## Config

All knobs are command-line flags:

| Flag | Default | Meaning |
|------|---------|---------|
| `--budget MS` | `20000` | Initial `--virtual-time-budget` in milliseconds. |
| `--retries N` | `3` | Max render attempts; each retry **doubles** the budget. |
| `--label LABEL` | (none) | Keep only lines mentioning this label. Repeatable. |
| `--html-out PATH` | (none) | Also write the raw rendered HTML to a file. |
| `--chrome BIN` | auto-detect | Path to a specific Chrome/Chromium binary. |

## Caveats

- **Retries by design.** Dynamic pages sometimes render only nav/footer on a slow
  pass, or hit a transient error like `ERR_CERT_VERIFIER_CHANGED`. The script detects
  these (and suspiciously empty output) and retries with a larger time budget; you can
  also just re-run it.
- **`--no-sandbox`** is convenient on headless servers but disables Chrome's sandbox.
  Prefer running as a non-root user, or drop the flag if your environment supports the
  sandbox.
- **Budget vs. speed.** Bigger `--virtual-time-budget` means more content but slower
  runs. Start at 20s and raise only if content is missing.
- **Text extraction is heuristic** (regex tag-stripping), not a full DOM parser. It is
  aimed at pulling readable text and labelled lines, not at preserving page structure.
  For structured extraction, use `--html-out` and parse the HTML with a proper library.
- **Be a good citizen.** Respect each site's terms of service and `robots.txt`, and
  don't hammer servers.
