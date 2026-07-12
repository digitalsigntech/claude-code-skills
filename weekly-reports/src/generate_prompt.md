You are generating a WEEKLY competitive-analysis report as an unattended job. Work autonomously; do not ask questions. Produce a polished PDF and nothing else that requires a human.

<!--
  This is a GENERIC EXAMPLE prompt. Copy it to generate_prompt_<key>.md and fill in
  YOUR own product, market, and sources. The generator picks generate_prompt_<key>.md
  by key, falling back to this file. Keep the 5-step structure below — the last two
  steps (render to the exact OUT path, print the OK sentinel) are what the generator
  and sender rely on.
-->

CONTEXT — describe what you sell / build here:
- <YOUR_PRODUCT>: a one-line description of what it is and who it's for.
  Public site(s): <YOUR_SITE>. Internal notes / price list: <path-to-your-KB-file>.

TASK:
1. Fetch <YOUR_SITE> and read your own live pricing. Note any change vs your internal
   knowledge-base / price list at <path-to-your-KB-file>.
2. Research the TOP 10 competitors for <YOUR_TOPIC>. For each: company + product,
   exact verified website URL, one line on what it is vs your product, and price in USD
   if public (else "Quote only" with any sourced secondary-market anchor). Use WebSearch
   + WebFetch to verify every URL and price. You may launch up to 2 subagents in parallel
   to gather this, then synthesize. Be honest where pricing is not public; cite a source
   URL for every price figure.
3. Build a styled A4 HTML report. Use <path-to-your-style-template>.html as the STYLE
   TEMPLATE (reuse its CSS and section layout: Executive Summary; Your Product & Pricing
   with a price-reconciliation callout if live != KB; Top 10 Competitors table; Pricing
   Landscape; Positioning & Recommendations; Methodology & Sources). Update all content
   with THIS week's findings and date it for the current week.
4. Render to PDF with headless Chrome to the EXACT path given in OUT below:
   google-chrome --headless=new --disable-gpu --no-sandbox --print-to-pdf="$OUT" --no-pdf-header-footer file://<your-html-path>
5. Confirm the PDF exists and is >20KB. Print the final line: WEEKLY_REPORT_OK <OUT>

Keep it factual and sourced. Accuracy of URLs and prices matters more than volume.
