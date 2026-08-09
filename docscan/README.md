# docscan

Turn a phone photo of a sheet of paper into a small, square, clean document —
cropped, perspective-corrected, upright, whitened, and compressed to roughly the
size a commercial scanner app produces.

Pillow + numpy only. No OpenCV, no cloud, nothing leaves the machine.

```bash
python3 src/docscan.py photo.jpg               # -> photo-doc.pdf, original kept
python3 src/docscan.py a.jpg b.jpg -o out.pdf  # one multi-page PDF
python3 -c "import docscan; docscan.to_image('photo.jpg')"   # JPEG instead
```

`autoscan.py` is the pointed-at-anything layer on top: docscan handles ONE known
page, autoscan finds however many documents are in a photo, decides whether there
is a document there at all, and lets each one's paper choose its format.

```bash
python3 src/autoscan.py photo.jpg -o out/     # every document in the photo
```

Two things it adds:

* **Find all of them, not one.** docscan flood-fills from the centre of frame
  because "the document is what you aimed at". That is exactly wrong with two
  sheets side by side, so autoscan labels every connected region across a LADDER
  of thresholds in both polarities — one global split assumes the scene has two
  levels, and a white sheet plus a dark card on a mid-tone desk has three — then
  collapses the repeats by overlap, ranking by how RECTANGULAR each reading is.
* **Decide whether there is a document at all.** A photo of a flowerbed must
  produce nothing rather than a confident crop of some leaves. Two independent
  tests have to agree: the region fills its own corner quad, and it carries
  ink structure (real pages score 11-17 on the line-organisation measure, a
  photographed flowerbed 2.3).

Routing follows the paper, which docscan already classifies: white stock is a
document and becomes a **PDF**; coloured or dark stock is artwork and becomes a
**JPEG**, because wrapping a picture in a PDF adds a container and nothing else.
`preview_dir=` also writes a small JPEG of each page — a vision model cannot look
at a PDF, so that is what a caller captions it from.

## Why it exists

A phone photo of a page is a photo of a *scene*: a skewed sheet seen at an angle,
uneven lighting, grey paper, sensor noise — and 12 MB of JPEG spent on all of it.
A scanner app keeps only what is on the page. This does the same, and was tuned
by measuring against a commercial app's output on the same sheet rather than by
eye.

**Result on the reference sheet: 12.4 MB → 233 KB (~50×)**, with a background
statistically indistinguishable from the reference app's.

| | reference app | first attempt | tuned |
|---|---|---|---|
| pixels at pure white (≥250) | 93.1% | 72.8% | 92.7% |
| mean brightness | 248.6 | 246.2 | 249.6 |
| ink coverage (<128) | 2.26% | 1.66% | 1.74% |

## The pipeline

Order matters — each step depends on the previous one having run.

1. **Find the page.** Otsu mask, **both polarities**. "Paper is the bright
   region" is exactly backwards for a dark brochure on pale wood, where the
   bright mask is the *desk*. Then flood-fill from the centre of frame to drop
   everything the camera was not pointed at, and refine the corners by fitting
   the four **edges** and intersecting them.
2. **Rectify at the right aspect.** The projected edges are not the page's
   proportions — a page tilted away is foreshortened, so sizing the output from
   measured edges squashes it vertically. Four corners of a known rectangle
   determine the focal length and hence the true aspect.
3. **Rotate upright.** Text is periodic *perpendicular* to its reading
   direction, so whichever projection of the ink varies more says which way the
   text runs — no OCR needed.
4. **Is the paper white?** Everything below assumes paper should end up white.
   Applied to coloured or dark stock it erases the design, so it is skipped and
   such a document gets geometry, resize and compression only.
5. **Flatten the illumination** — divide by a heavy blur of the image, which is
   an estimate of the lighting across the sheet. Ink is far too fine to survive
   the blur, so it is untouched.
6. **Set the white point by measuring it** — the 80th percentile of the page
   *centre* is the paper, whatever the paper is. Then a soft knee ramps the top
   of the range to pure white.
7. **Keep the colour.** Not a bilevel scan: an engineering drawing is
   colour-coded and binarising destroys its meaning, not just its looks.
8. **Downscale + JPEG**, sized by long edge.

## Five findings worth keeping

Each of these was a bug first. They are the parts most likely to be
rediscovered the hard way by anyone reimplementing this.

- **Measure the white point, never assume it.** A fixed constant left 73% of
  pixels at true white where the reference reached 93%, and the residue read as
  grey mottling. A measured percentile maps paper to white *by construction*.
  Sample the page centre: even after cropping there is a shadow rim where the
  paper curls, and including it drags the estimate down.
- **Size by long edge, not by DPI.** A photographed page has no meaningful DPI.
  A source PDF claimed a 34-inch-wide page because that was what the camera
  resolution worked out to, so "150 dpi" left it 5040 px wide and 1.2 MB.
- **Target the reference app's bytes, not its pixel count.** Matching its
  resolution at half its file size means throwing away detail it kept.
- **The aspect-ratio solve is stated for corner order (tl, tr, bl, br).**
  Feeding it clockwise order yields a negative f² and a silent "no solution".
- **When the page is tilted about only one axis** — you tilt the phone forward
  but not sideways — one vanishing point goes to infinity and the focal length
  is *unrecoverable from the corners*. EXIF `FocalLengthIn35mmFilm` supplies it
  (`f_px = f35 / 36 × long_edge`). Without EXIF and with degenerate geometry the
  aspect genuinely cannot be recovered from a single image, and the code keeps
  the measured edges rather than inventing a number.

Also: `Image.fromarray` wraps the numpy buffer **read-only**, and
`ImageDraw.floodfill` then silently fills nothing. `.copy()` is load-bearing.

## Refusals

The code returns "no" rather than a best guess in three places, because a
plausible wrong answer is worse than none:

- A page whose corners touch the frame border is **clipped** — its real corners
  are not in the image, so it cannot be rectified. Warping anyway distorts the
  content instead of straightening it. Falls back to an axis-aligned crop.
- A page with no clear line structure (a drawing, a diagram) is **not rotated**;
  the orientation test requires the column score to beat the row score
  decisively.
- `build()` **refuses to write over any input**. Whitening and downscaling are
  lossy and one-way, so consuming the original would destroy the ability to
  reprocess later. `process()` writes `<name>-doc.pdf` beside the original and
  never moves, renames or rewrites it.

## Notes for a mobile port

On iOS, steps 1–2 are better served by the platform:
`VNDetectDocumentSegmentationRequest` returns the page quad and
`CIPerspectiveCorrection` squares it up, which beats the Otsu/flood-fill path
here. Steps 4–8 port directly — `CIGaussianBlur` + `CIDivideBlendMode` for the
flatten, `CIColorControls` for saturation.
