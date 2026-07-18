# Letterhead document — page-anchored logo

Generate a business letter on company letterhead with the logo **pinned to a
fixed spot on the page** (typically top-left) so it does **not** drift, reflow,
or jump when the file is opened in Word, LibreOffice, Google Docs, or exported
to PDF. This is a documentation + template pack: it explains the anchoring
technique and ships a **blank generic template** you fill in with your own
company name, address, logo, and signature.

> This pack contains **no** real branding — placeholders only (`[COMPANY NAME]`,
> `[LOGO]`, `[ADDRESS]`, etc.). Drop in your own logo image and text.

## The problem it solves

If you insert a logo as a normal inline image ("in line with text") or anchor
it *to a paragraph* / *to a character*, its position is computed **relative to
surrounding text**. The moment the body text changes length, or a different
viewer lays the paragraphs out slightly differently, the logo moves. The fix is
to anchor the image **to the page** and give it an **absolute** X/Y offset from
the page corner, with **text wrapping off** so text neither pushes it nor flows
around it.

## The technique (the actual value here)

### OpenDocument (`.fodt` / `.odt`, LibreOffice)

Two pieces work together — a **graphic style** and the **frame** that holds the
image. See `src/letterhead-template.fodt` for a complete working file.

1. A graphic style whose properties make position page-relative:

```xml
<style:style style:name="LogoFixed" style:family="graphic">
  <style:graphic-properties
      style:wrap="none"
      style:vertical-pos="from-top"    style:vertical-rel="page"
      style:horizontal-pos="from-left" style:horizontal-rel="page"/>
</style:style>
```

2. The image frame that uses it, anchored to the page with an absolute offset:

```xml
<draw:frame draw:style-name="LogoFixed" draw:name="Logo"
    text:anchor-type="page" text:anchor-page-number="1"
    svg:x="1in" svg:y="0.5in"
    svg:width="4.2in" svg:height="0.6in" draw:z-index="3">
  <draw:image xlink:href="logo.png" xlink:type="simple"
      xlink:show="embed" xlink:actuate="onLoad"/>
</draw:frame>
```

The settings that matter:

| Setting | Value | Why |
|---|---|---|
| `text:anchor-type` | `page` | Anchor to the physical page, not a paragraph/char. |
| `text:anchor-page-number` | `1` | Pin to page 1 only. |
| `style:vertical-rel` / `horizontal-rel` | `page` | Measure the offset from the **page** edge. |
| `style:vertical-pos` / `horizontal-pos` | `from-top` / `from-left` | Use the absolute `svg:y` / `svg:x`, not "center/top". |
| `svg:x` / `svg:y` | e.g. `1in` / `0.5in` | Absolute distance from the top-left page corner. |
| `style:wrap` | `none` | Text is not pushed by the logo and does not flow around it. |
| `draw:z-index` | high (e.g. `3`) | Keeps the logo above the body layer. |

Then set a **top page margin** larger than the logo's bottom edge so the letter
body starts below the logo (see `pm1` / `fo:margin-top` in the template).

### OOXML (`.docx`, Microsoft Word)

The exact same idea, different vocabulary. A floating image in Word is a
`<wp:anchor>` (as opposed to inline `<wp:inline>`). Inside `word/document.xml`
the drawing must use **page-relative** positioning and **no wrap**:

```xml
<wp:anchor behindDoc="0" allowOverlap="1" ... >
  <wp:positionH relativeFrom="page">
    <wp:posOffset>914400</wp:posOffset>   <!-- 1 inch, in EMUs -->
  </wp:positionH>
  <wp:positionV relativeFrom="page">
    <wp:posOffset>457200</wp:posOffset>   <!-- 0.5 inch -->
  </wp:positionV>
  <wp:wrapNone/>
  ...
</wp:anchor>
```

The mapping from ODF to OOXML:

| ODF (`.fodt`) | OOXML (`.docx`) |
|---|---|
| `text:anchor-type="page"` | use `<wp:anchor>` (not `<wp:inline>`) |
| `horizontal-rel="page"` | `<wp:positionH relativeFrom="page">` |
| `vertical-rel="page"` | `<wp:positionV relativeFrom="page">` |
| `pos="from-left/from-top"` + `svg:x/y` | `<wp:posOffset>` (absolute) inside each |
| `style:wrap="none"` | `<wp:wrapNone/>` |

**Units:** OOXML positions/sizes are in **EMUs** — `914400 EMU = 1 inch`
(`360000 EMU = 1 cm`). So `svg:x="1in"` → `posOffset` `914400`.

### Google Docs caveat

Google Docs does not expose true page-anchored floating images in its UI, but
it **honours** the page-anchored positioning when it imports a `.docx` or `.odt`
authored as above, and preserves it on re-export and on PDF download. Author the
file in LibreOffice/Word with page anchoring; treat Google Docs as a
viewer/exporter, not the authoring tool for the anchor.

## Files

| File | Purpose |
|---|---|
| `README.md` | This guide — the anchoring technique for ODF and OOXML. |
| `src/letterhead-template.fodt` | Blank, generic, ready-to-fill flat-ODF letterhead with the logo already page-pinned. Open in LibreOffice, replace placeholders, drop in `logo.png`. |

## How to use

1. Copy `src/letterhead-template.fodt` to your project.
2. Put your logo image next to it as `logo.png` (or embed it as
   `office:binary-data` inside the frame — LibreOffice does this automatically
   when you re-insert the image and save).
3. Open in LibreOffice Writer. Replace every `[PLACEHOLDER]`:
   `[COMPANY NAME]`, `[ADDRESS ...]`, `[PHONE]`, `[EMAIL]`, `[WEBSITE]`,
   `[DATE]`, `[RECIPIENT ...]`, `[BODY PARAGRAPH ...]`, `[SIGNER NAME]`,
   `[SIGNER TITLE]`.
4. Adjust the logo: tune `svg:x` / `svg:y` (position) and
   `svg:width` / `svg:height` (size) on the `draw:frame`. Make sure the page
   `fo:margin-top` is larger than the logo's bottom edge so body text clears it.
5. Export to PDF (`File > Export as PDF`) or convert on the command line:

```sh
soffice --headless --convert-to pdf letterhead-template.fodt
```

## Prerequisites

- LibreOffice (`soffice`) for authoring/editing and headless PDF export.
  Microsoft Word works too for `.docx` using the OOXML mapping above.
- A logo image (PNG/JPG/SVG). Keep its aspect ratio consistent with
  `svg:width`:`svg:height`.

## Caveats

- **No real branding shipped.** All company name, address, contact, recipient,
  and signature fields are placeholders. There is no embedded logo image —
  `logo.png` is referenced but you supply it.
- **Set the top margin to clear the logo**, otherwise the first line of the
  letter overlaps it. The pin does not reserve space (that is the point of
  `wrap="none"`); the page margin reserves the space instead.
- **`.doc` (legacy binary Word)** is not recommended — it round-trips floating
  anchors poorly across apps. Prefer `.fodt`/`.odt`/`.docx`.
- Page anchoring pins to a **page number**. For a logo on every page (letterhead
  proper), put it in the page **header** instead, or repeat the anchored frame
  per page. This template pins page 1 (typical for a single letter).

## Gotcha: automatic-style font inheritance

If you add your own paragraph styles to the flat ODT, give every style used by
visible text an explicit `<style:text-properties>` (font + size). An automatic
style that only points at another automatic style via `parent-style-name` is
NOT resolved by LibreOffice — the text silently falls back to the default serif
font (bit us on a date line, 2026-07-17).
