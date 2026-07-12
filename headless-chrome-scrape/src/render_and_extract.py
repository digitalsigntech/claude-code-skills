#!/usr/bin/env python3
"""
render_and_extract.py — read JavaScript-rendered web pages.

A plain HTTP fetch of a single-page app (SPA) usually returns only the empty
shell: the real content is injected by JavaScript in the browser. This helper
drives a headless Chrome to render the DOM, then parses the resulting HTML into
plain text and (optionally) pulls out the lines around labels you care about.

Usage:
    # Dump the rendered visible text of a page
    python3 render_and_extract.py "<URL>"

    # Save the raw rendered HTML too
    python3 render_and_extract.py "<URL>" --html-out /tmp/page.html

    # Only keep lines mentioning any of these labels (case-insensitive)
    python3 render_and_extract.py "<URL>" --label Price --label Total --label SKU

    # Be patient with slow / heavy pages
    python3 render_and_extract.py "<URL>" --budget 40000 --retries 4

Requires: google-chrome (or chromium) on PATH; Python 3 standard library only.
"""

import argparse
import re
import shutil
import subprocess
import sys


# Transient failure signatures that are worth a retry (usually with a bigger
# virtual-time-budget): certificate churn, or a page that only rendered its
# navigation / footer chrome on a slow pass.
TRANSIENT_MARKERS = (
    "ERR_CERT_VERIFIER_CHANGED",
    "ERR_CERT_",
    "ERR_TIMED_OUT",
    "ERR_CONNECTION_RESET",
    "ERR_NETWORK_CHANGED",
)


def find_chrome():
    """Locate a Chrome/Chromium binary on PATH."""
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        path = shutil.which(name)
        if path:
            return path
    sys.exit(
        "error: no Chrome/Chromium binary found on PATH "
        "(tried google-chrome, chromium, ...). Install one and retry."
    )


def render_dom(url, budget, chrome_bin):
    """Render URL with headless Chrome and return the DOM as an HTML string."""
    cmd = [
        chrome_bin,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--dump-dom",
        f"--virtual-time-budget={budget}",
        url,
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.stdout, proc.stderr


def looks_transient(html, stderr):
    """True if the failure looks retryable (cert churn, timeout, etc.)."""
    blob = (html or "") + "\n" + (stderr or "")
    return any(marker in blob for marker in TRANSIENT_MARKERS)


def looks_empty(text):
    """
    Heuristic: a page that rendered only nav/footer chrome (or nothing) tends
    to produce very little substantive text. Treat that as a soft failure so
    the caller can retry with a larger time budget.
    """
    return len(text.split()) < 15


def render_with_retries(url, budget, retries, chrome_bin):
    """
    Render with retries. On a transient error or a suspiciously empty result,
    retry with a progressively larger virtual-time-budget.
    """
    current = budget
    last_text = ""
    for attempt in range(1, retries + 1):
        html, stderr = render_dom(url, current, chrome_bin)
        text = html_to_text(html)

        if looks_transient(html, stderr):
            sys.stderr.write(
                f"[attempt {attempt}] transient error; retrying "
                f"(budget {current} -> {current * 2})\n"
            )
            current *= 2
            continue

        if looks_empty(text) and attempt < retries:
            sys.stderr.write(
                f"[attempt {attempt}] page looks empty/partial; retrying "
                f"(budget {current} -> {current * 2})\n"
            )
            last_text = text
            current *= 2
            continue

        return html, text

    # Ran out of retries — return the best we managed to get.
    return html, text if text else last_text


def html_to_text(html):
    """
    Turn rendered HTML into plain, readable text:
      1. drop <script> and <style> blocks entirely,
      2. strip all remaining tags,
      3. unescape a few common HTML entities,
      4. collapse whitespace and keep only substantive (non-blank) lines.
    """
    if not html:
        return ""

    # 1. Remove script/style (and their contents).
    html = re.sub(
        r"<(script|style|noscript)\b[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Turn block-ish boundaries into newlines so text doesn't run together.
    html = re.sub(r"</(p|div|li|tr|h[1-6]|section|article|br)\s*>", "\n", html,
                  flags=re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)

    # 2. Strip remaining tags.
    text = re.sub(r"<[^>]+>", " ", html)

    # 3. Unescape common entities.
    for entity, repl in (
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
    ):
        text = text.replace(entity, repl)

    # 4. Collapse whitespace, keep substantive lines.
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def filter_labels(text, labels):
    """Keep only lines that mention any of the given labels (case-insensitive)."""
    if not labels:
        return text
    lowered = [lab.lower() for lab in labels]
    kept = [
        line
        for line in text.splitlines()
        if any(lab in line.lower() for lab in lowered)
    ]
    return "\n".join(kept)


def main():
    ap = argparse.ArgumentParser(
        description="Render a JS page with headless Chrome and extract its text."
    )
    ap.add_argument("url", help="Page URL to render, e.g. <URL>")
    ap.add_argument(
        "--budget",
        type=int,
        default=20000,
        help="Initial --virtual-time-budget in ms (default: 20000).",
    )
    ap.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Max render attempts; each retry doubles the budget (default: 3).",
    )
    ap.add_argument(
        "--label",
        action="append",
        default=[],
        dest="labels",
        help="Keep only lines mentioning this label. Repeatable.",
    )
    ap.add_argument(
        "--html-out",
        metavar="PATH",
        help="Also write the raw rendered HTML to this file.",
    )
    ap.add_argument(
        "--chrome",
        metavar="BIN",
        help="Path to a specific Chrome/Chromium binary.",
    )
    args = ap.parse_args()

    chrome_bin = args.chrome or find_chrome()
    html, text = render_with_retries(
        args.url, args.budget, max(1, args.retries), chrome_bin
    )

    if args.html_out and html:
        with open(args.html_out, "w", encoding="utf-8") as fh:
            fh.write(html)
        sys.stderr.write(f"[wrote raw HTML] {args.html_out}\n")

    out = filter_labels(text, args.labels)
    if not out.strip():
        sys.stderr.write(
            "warning: no substantive content extracted. The page may need a "
            "larger --budget, or the requested --label(s) were not present.\n"
        )
    print(out)


if __name__ == "__main__":
    main()
