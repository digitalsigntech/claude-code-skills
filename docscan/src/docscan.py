#!/usr/bin/env python3
"""docscan — turn a photographed page into a small, square, clean document.

The owner: "our iOS app can now process images into PDF documents,
with the paper whitening and compression, so the 13Mb PDF becomes only 300Kb."
Tuned since against a Genius Scan render of the same sheet, then against a
brochure printed on dark stock.

A phone photo of a sheet of paper is a photo of a *scene*: a skewed page seen at
an angle, uneven lighting, grey paper, sensor noise, and 12 MB of JPEG spent on
all of it. A scanner app keeps only what is on the page. The order below matters
— each step depends on the previous one having run.

  1. FIND THE PAGE. Otsu mask, BOTH polarities: "paper is the bright region" is
     exactly backwards for a dark brochure on pale wood, where the bright mask is
     the desk. Flood-fill from the centre of frame to drop everything the camera
     was not pointed at (a keyboard at the edge otherwise poisons the mask), then
     refine the corners by fitting the four EDGES and intersecting them — one
     extreme pixel per corner is not enough evidence for step 2.
  2. RECTIFY, AT THE RIGHT ASPECT. The projected edges are NOT the page's
     proportions: a page tilted away is foreshortened, so sizing the output from
     measured edges squashes it vertically. Four corners of a known rectangle
     determine the focal length and hence the true aspect — and when the page is
     tilted about only one axis, that solve is degenerate and EXIF's
     FocalLengthIn35mmFilm supplies the focal length instead.
  3. ROTATE UPRIGHT. Text is periodic perpendicular to its reading direction, so
     whichever projection of the ink varies more says which way the text runs.
  4. IS THE PAPER WHITE? Everything below assumes paper should end up white.
     Applied to coloured or dark stock it erases the design, so it is skipped —
     such a document gets geometry, resize and compression only.
  5. FLATTEN the illumination: divide by a heavy blur of the image, which is an
     estimate of the lighting. Ink is far too fine to survive the blur.
  6. WHITE POINT, MEASURED not assumed — the 80th percentile of the page centre
     IS the paper. Then a soft knee ramps the top of the range to pure white.
     This step is the whole difference between a 73%-white background and the
     93% Genius Scan achieves.
  7. KEEP THE COLOUR. Not a bilevel scan: an engineering drawing is colour-coded
     (red framing, green LEDs, blue jumpers) and binarising destroys its meaning.
  8. DOWNSCALE + JPEG. Sized by LONG EDGE, never dpi — a photographed page has
     no meaningful dpi. Target the reference app's BYTES (~200 KB/page), not its
     pixel count: matching pixels at half the size means throwing away detail it
     kept.

Usage:
  docscan.py photo.jpg                 # -> photo-doc.pdf beside it, original kept
  docscan.py a.jpg b.jpg -o out.pdf    # one multi-page PDF
  docscan.py in.pdf -o out.pdf [--max-px 2800] [--quality 78] [--gray]
                                       [--no-crop] [--keep-size]
  python3 -c "import docscan; docscan.to_image('photo.jpg')"   # JPEG, not PDF

Stdlib + Pillow/numpy only. Nothing leaves the box.
"""
import argparse
import io
import os
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _pages_from_pdf(path, render_dpi=200):
    """Rasterize each PDF page. 200 dpi in, downscaled later — rendering at the
    output resolution directly would alias the thin lines on a drawing."""
    out = []
    with tempfile.TemporaryDirectory() as td:
        stem = os.path.join(td, "p")
        r = subprocess.run(["pdftoppm", "-r", str(render_dpi), "-jpeg",
                            "-jpegopt", "quality=95", path, stem],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"pdftoppm: {(r.stderr or '')[:200]}")
        for f in sorted(os.listdir(td)):
            out.append(Image.open(os.path.join(td, f)).convert("RGB").copy())
    return out


def _page_box(gray, pad=0.004):
    """Bounding box of the sheet inside the photo.

    Genius Scan crops to the paper and throws the desk away; keeping the desk
    costs pixels AND drags the statistics, because a dark border is what stops
    the white point from reaching true white. Axis-aligned only — full
    perspective rectification needs corner detection this does not have, and
    the crop is where nearly all of the benefit is.

    The sheet is the bright connected mass in the middle: take rows/columns
    whose brightness is at least 80% of the brightest row/column, and keep the
    span between the first and last of them.
    """
    h, w = gray.shape
    rows, cols = gray.mean(axis=1), gray.mean(axis=0)

    def span(v, n):
        thr = v.max() * 0.80
        idx = np.flatnonzero(v >= thr)
        if idx.size < n * 0.15:          # no clear page — do not crop
            return 0, n
        lo, hi = int(idx[0]), int(idx[-1]) + 1
        m = int(n * pad)
        return max(0, lo - m), min(n, hi + m)

    t, b = span(rows, h)
    l, r = span(cols, w)
    if (b - t) < h * 0.4 or (r - l) < w * 0.4:
        return 0, 0, w, h                # implausible crop — keep everything
    return l, t, r, b


def _otsu(v):
    """Threshold separating paper from background. Otsu because the split is
    genuinely bimodal here — a bright sheet on a darker desk — and a fixed
    threshold fails the moment the desk is pale."""
    hist, _ = np.histogram(v, bins=256, range=(0, 256))
    tot = float(hist.sum())
    s = float(np.dot(np.arange(256), hist))
    sB = wB = 0.0
    best = (0.0, 128)
    for i in range(256):
        wB += hist[i]
        if wB == 0:
            continue
        wF = tot - wB
        if wF <= 0:
            break
        sB += i * hist[i]
        var = wB * wF * ((sB / wB) - ((s - sB) / wF)) ** 2
        if var > best[0]:
            best = (var, i)
    return best[1]


def _page_quad(img, work=900, margin=0.01):
    """The four corners of the sheet, or None when the photo does not contain a
    whole page.

    Corners from the extremes of (x+y) and (x−y) over the paper mask: the
    top-left minimises the sum, the bottom-right maximises it, and the other two
    diagonal fall out of the difference. Cheap, and exact for a convex quad.

    Returns None — deliberately, rather than a best guess — when:
      * the mask is not a plausible page (under 15% or over 95% of the frame),
      * any corner sits on the frame border, meaning the sheet runs out of shot
        and its real corner is not in the image at all, or
      * the quad is too small or too close to the full frame to be worth warping.
    A page that is clipped cannot be rectified; pretending otherwise warps the
    content instead of straightening it. The caller falls back to a plain crop.
    """
    g = img.convert("L")
    f = work / float(max(g.size))
    if f < 1.0:
        g = g.resize((max(1, int(g.width * f)), max(1, int(g.height * f))),
                     Image.BILINEAR)
    else:
        f = 1.0
    a = np.asarray(g.filter(ImageFilter.GaussianBlur(2)), dtype=np.float32)
    h, w = a.shape
    thr = _otsu(a)
    # BOTH polarities. "Paper is the bright region" holds for a white sheet on a
    # dark desk and is exactly backwards for a dark brochure on pale wood — there
    # the bright mask is the DESK, and cropping to it would keep the desk and
    # throw the document away. Whichever mask yields a quad that clears the
    # guards is the document; the other one is the surface it is lying on.
    for mask in (a > thr, a <= thr):
        q = _quad_from_mask(mask, w, h, margin)
        if q is not None:
            return q / f
    return None


def _centre_region(mask):
    """Keep only the blob the camera was pointed at.

    A polarity mask is not one object: photographing a dark brochure on a pale
    desk also selects the keyboard at the edge of frame, and its corners sit on
    the border, so the whole mask gets rejected as "clipped". The document is
    what is under the centre of the frame — that is what aiming a camera means —
    so flood-fill from the middle and discard everything not connected to it.
    """
    h, w = mask.shape
    cy, cx = h // 2, w // 2
    if not mask[cy, cx]:
        return None                      # centre is background — wrong polarity
    # .copy() is load-bearing: fromarray wraps the numpy buffer read-only, and
    # floodfill then silently fills nothing.
    img = Image.fromarray((mask.astype(np.uint8) * 255), "L").copy()
    ImageDraw.floodfill(img, (cx, cy), 128)
    return np.array(img) == 128


def _quad_from_mask(mask, w, h, margin):
    mask = _centre_region(mask)
    if mask is None:
        return None
    cov = mask.mean()
    if not (0.15 <= cov <= 0.95):
        return None
    ys, xs = np.nonzero(mask)
    pts = np.stack([xs, ys], axis=1).astype(np.float32)
    ssum, sdif = xs + ys, xs - ys
    tl, br = pts[np.argmin(ssum)], pts[np.argmax(ssum)]
    tr, bl = pts[np.argmax(sdif)], pts[np.argmin(sdif)]
    quad = np.array([tl, tr, br, bl], dtype=np.float32)

    mx, my = w * margin, h * margin
    for x, y in quad:
        if x <= mx or y <= my or x >= w - 1 - mx or y >= h - 1 - my:
            return None                       # sheet is clipped by the frame
    area = 0.5 * abs(
        (quad[0, 0] * quad[1, 1] - quad[1, 0] * quad[0, 1]) +
        (quad[1, 0] * quad[2, 1] - quad[2, 0] * quad[1, 1]) +
        (quad[2, 0] * quad[3, 1] - quad[3, 0] * quad[2, 1]) +
        (quad[3, 0] * quad[0, 1] - quad[0, 0] * quad[3, 1]))
    if not (0.25 * w * h <= area <= 0.98 * w * h):
        return None
    refined = _refine_quad(mask, quad)
    return refined if refined is not None else quad


def _refine_quad(mask, quad):
    """Sharpen the corners by fitting the four EDGES and intersecting them.

    Extreme-point corners are only as good as one pixel each, and a card with
    rounded corners or a soft shadow moves that pixel. The aspect-ratio solve is
    very sensitive to it — page 1 of the brochure gave a negative f² (i.e. "no
    valid rectangle") from corners that looked fine — while an edge is hundreds
    of pixels of evidence and fits robustly. Corners near the ends are excluded
    so a rounded corner cannot bend its own line.
    """
    ys, xs = np.nonzero(mask)
    pts = np.stack([xs, ys], axis=1).astype(np.float64)
    lines = []
    for i in range(4):
        a, b = quad[i], quad[(i + 1) % 4]
        d = b - a
        L = np.linalg.norm(d)
        if L < 10:
            return None
        d = d / L
        n = np.array([-d[1], d[0]])
        rel = pts - a
        t = rel @ d                       # position along the edge
        dist = np.abs(rel @ n)            # distance from the edge line
        sel = (t > 0.15 * L) & (t < 0.85 * L) & (dist < 0.02 * L)
        if sel.sum() < 30:
            return None
        P = pts[sel]
        c = P.mean(axis=0)
        # total least squares: the edge direction is the principal axis
        u, _, _ = np.linalg.svd(P - c)
        _, _, vt = np.linalg.svd(P - c, full_matrices=False)
        dirv = vt[0]
        lines.append((c, np.array([-dirv[1], dirv[0]])))   # point + normal

    out = []
    for i in range(4):
        (c1, n1), (c2, n2) = lines[i - 1], lines[i]
        A = np.stack([n1, n2])
        b = np.array([n1 @ c1, n2 @ c2])
        if abs(np.linalg.det(A)) < 1e-6:
            return None
        out.append(np.linalg.solve(A, b))
    return np.array(out, dtype=np.float32)


def _quad_from_mask_raw(mask, w, h):
    """Corners of an ALREADY-ISOLATED region, refined by edge fit.

    _quad_from_mask picks the region for you (flood-fill from the centre) and
    rejects clipped pages. autoscan has already chosen the region and wants the
    corners for it, nothing more — keeping that split stops the multi-document
    path from silently inheriting the single-document policy.
    """
    ys, xs = np.nonzero(mask)
    if xs.size < 50:
        return None
    pts = np.stack([xs, ys], axis=1).astype(np.float32)
    ssum, sdif = xs + ys, xs - ys
    quad = np.array([pts[np.argmin(ssum)], pts[np.argmax(sdif)],
                     pts[np.argmax(ssum)], pts[np.argmin(sdif)]], dtype=np.float32)
    refined = _refine_quad(mask, quad)
    return refined if refined is not None else quad


def rectify_quad(img, quad):
    """Warp a known quad to an upright rectangle at the page's TRUE aspect."""
    tl, tr, br, bl = quad
    wid = int(round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))))
    hei = int(round(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))))
    if wid < 120 or hei < 120:
        return None
    ar = _true_aspect(quad, img.size, img)
    if ar:
        if wid >= hei:
            hei = int(round(wid / ar))
        else:
            wid = int(round(hei * ar))
    coeffs = _perspective_coeffs([(0, 0), (wid, 0), (wid, hei), (0, hei)],
                                 [tuple(p) for p in quad])
    return img.transform((wid, hei), Image.PERSPECTIVE, coeffs, Image.BICUBIC)


def _perspective_coeffs(dst, src):
    """PIL's 8 transform coefficients: it maps OUTPUT -> INPUT, so `dst` here is
    the rectangle we are producing and `src` the quad in the photo."""
    A, b = [], []
    for (ox, oy), (ix, iy) in zip(dst, src):
        A.append([ox, oy, 1, 0, 0, 0, -ix * ox, -ix * oy])
        A.append([0, 0, 0, ox, oy, 1, -iy * ox, -iy * oy])
        b += [ix, iy]
    return np.linalg.solve(np.asarray(A, np.float64),
                           np.asarray(b, np.float64)).tolist()


def paper_is_white(img, lum_min=150.0, sat_max=0.20, frac_min=0.35):
    """True when the sheet is white/near-white paper, i.e. whitening is safe.

    The owner: a document printed on NON-white stock must not be
    whitened — only resized and compressed. Whitening works by declaring the
    paper to be pure white; do that to a navy brochure and you erase the design.

    Measured on the page itself (call after cropping): the background is the
    modal region, so ask what fraction of the page is simultaneously bright and
    unsaturated. A white sheet is mostly both. A coloured or dark stock fails on
    one or the other, even when it carries white text.
    """
    a = np.asarray(img.convert("RGB").resize((256, 256)), dtype=np.float32)
    lum = a.mean(axis=2)
    mx, mn = a.max(axis=2), a.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1.0), 0.0)
    paperish = float(np.mean((lum >= lum_min) & (sat <= sat_max)))
    return paperish >= frac_min, paperish


def _text_axis_score(img, work=700):
    """(row_score, col_score) — how strongly the ink is organised into lines.

    Text is periodic PERPENDICULAR to its reading direction: upright lines make
    the row-projection of the ink swing between line and gap, while the column
    projection stays flat. So whichever projection varies more tells you which
    way the text runs, without reading a single character.
    """
    g = img.convert("L")
    f = work / float(max(g.size))
    if f < 1.0:
        g = g.resize((max(1, int(g.width * f)), max(1, int(g.height * f))),
                     Image.BILINEAR)
    a = np.asarray(g, dtype=np.float32)
    # ink = local deviation from the blurred page, so it works on dark stock too
    bg = np.asarray(Image.fromarray(a.astype(np.uint8), "L").filter(
        ImageFilter.GaussianBlur(6)), dtype=np.float32)
    ink = np.abs(a - bg)
    r, c = ink.mean(axis=1), ink.mean(axis=0)
    return float(r.std()), float(c.std())


def upright(img, margin=1.25):
    """Rotate a rectified page so its text reads horizontally.

    Cropping and warping fix the geometry but not the orientation: a card
    photographed sideways comes out sideways, which is not "straightened" in any
    sense the reader cares about. `margin` keeps it honest — the column score
    has to beat the row score decisively before anything is rotated, so a page
    with no clear line structure (a drawing, a diagram) is left alone.
    """
    r, c = _text_axis_score(img)
    if c > r * margin:
        return img.rotate(-90, expand=True), True
    return img, False


# Assumed 35 mm-equivalent focal length when the file carries no EXIF. Every
# phone main camera of the last decade sits in 24-28 mm; 26 is the middle of
# that range and the whole range only moves the recovered aspect by ~3%, which
# is far less than the error from having no focal length at all. Telegram (and
# most chat apps) strip EXIF on upload, so for a photo that arrives through chat
# this is the ONLY focal length available.
F35_DEFAULT = float(os.environ.get("DOCSCAN_F35_DEFAULT", "26"))


def _focal_px(img, default=True):
    """Focal length in pixels from EXIF, falling back to a typical phone lens.

    The geometric solve needs two finite vanishing points. A page tilted about
    only ONE axis — very common, you tilt the phone forward and not sideways —
    puts the second vanishing point at infinity and the focal length becomes
    unrecoverable from the corners alone. Brochure page 1 is exactly that case:
    its left and right edges are parallel to within 0.8 degrees.

    The camera knows what the geometry cannot say. FocalLengthIn35mmFilm is
    referenced to a 36 mm frame width, so f_px = f35 / 36 * image_width. When
    the EXIF is gone, assuming F35_DEFAULT beats giving up: measured on the
    2026-08-09 Telegram photos, a US Letter page came out at 0.96 (18% too wide)
    keeping the measured edges and 0.77 assuming the lens — and 4x6 cards landed
    within 1% of 0.667 across four separate shots.
    """
    f35 = None
    try:
        ex = img._getexif() or {}
        f35 = ex.get(41989)                   # FocalLengthIn35mmFilm
    except Exception:
        f35 = None
    try:
        f35 = float(f35) if f35 else None
    except Exception:
        f35 = None
    if not f35 or f35 <= 0:
        f35 = F35_DEFAULT if default else None
    if not f35:
        return None
    return f35 / 36.0 * float(max(img.size))


def _aspect_from_focal(quad, size, f):
    """True aspect with the focal length known — no vanishing points needed."""
    w, h = size
    cx, cy = w / 2.0, h / 2.0
    tl, tr, br, bl = [np.array([p[0] - cx, p[1] - cy, 1.0]) for p in quad]
    m1, m2, m3, m4 = tl, tr, bl, br
    d2 = float(np.dot(np.cross(m2, m4), m3))
    d3 = float(np.dot(np.cross(m3, m4), m2))
    if abs(d2) < 1e-9 or abs(d3) < 1e-9:
        return None
    k2 = float(np.dot(np.cross(m1, m4), m3)) / d2
    k3 = float(np.dot(np.cross(m1, m4), m2)) / d3
    n2 = k2 * m2 - m1
    n3 = k3 * m3 - m1
    Ainv = np.diag([1.0 / f, 1.0 / f, 1.0])
    d_2, d_3 = np.linalg.norm(Ainv @ n2), np.linalg.norm(Ainv @ n3)
    if d_3 < 1e-9:
        return None
    ar = float(d_2 / d_3)
    return ar if np.isfinite(ar) and 0.1 < ar < 10.0 else None


def _edge_convergence(quad):
    """How strongly each pair of opposite edges converges — the perspective
    evidence the focal-length solve lives on. 1.0 means parallel (no evidence)."""
    tl, tr, br, bl = quad
    top, bot = np.linalg.norm(tr - tl), np.linalg.norm(br - bl)
    lef, rig = np.linalg.norm(bl - tl), np.linalg.norm(br - tr)
    return min(max(top, bot) / max(min(top, bot), 1e-6),
               max(lef, rig) / max(min(lef, rig), 1e-6))


# Below this, one axis is effectively parallel and self-calibration is guessing.
MIN_CONVERGENCE = 1.05


def _true_aspect(quad, size, img=None):
    """Width/height of the real sheet, recovered from its perspective image.

    The projected edges are NOT the page's proportions: a page tilted away from
    the camera is foreshortened, so sizing the output from the measured edge
    lengths squashes it — the brochure came out vertically compressed, and a
    synthetic page of known 1.412 aspect rectified to 1.28.

    With the principal point assumed at the image centre, four corners of a
    known-rectangular object are enough to solve for the focal length and hence
    the true aspect (Zhang & He's whiteboard rectification). Returns None when
    the geometry is near-affine — the camera is square-on, f² comes out
    non-positive, and the measured edges are already right.
    """
    w, h = size
    cx, cy = w / 2.0, h / 2.0
    # SELF-CALIBRATION NEEDS PERSPECTIVE TO WORK ON. When one pair of edges is
    # nearly parallel the recovered focal length is noise, and the aspect that
    # comes out of it is confidently wrong rather than merely unknown — on the
    # 2026-08-09 photos it squashed a 4x6 card to 0.60 (it is 0.667) while the
    # edges converged by only 2%. Measure the evidence first and take the
    # camera's focal length instead when there isn't enough of it.
    if _edge_convergence(quad) < MIN_CONVERGENCE:
        fpx = _focal_px(img) if img is not None else None
        return _aspect_from_focal(quad, size, fpx) if fpx else None
    # quad arrives as tl, tr, br, bl. The formula is stated for
    # (tl, tr, bl, br) — feeding it our order silently yields a negative f².
    tl, tr, br, bl = [np.array([p[0] - cx, p[1] - cy, 1.0]) for p in quad]
    m1, m2, m3, m4 = tl, tr, bl, br
    d2 = float(np.dot(np.cross(m2, m4), m3))
    d3 = float(np.dot(np.cross(m3, m4), m2))
    if abs(d2) < 1e-9 or abs(d3) < 1e-9:
        return None
    k2 = float(np.dot(np.cross(m1, m4), m3)) / d2
    k3 = float(np.dot(np.cross(m1, m4), m2)) / d3
    n2 = k2 * m2 - m1
    n3 = k3 * m3 - m1
    if abs(n2[2]) < 1e-9 or abs(n3[2]) < 1e-9:
        return None
    f2 = -(n2[0] * n3[0] + n2[1] * n3[1]) / (n2[2] * n3[2])
    if f2 <= 0:
        # Degenerate for focal recovery. Fall back to what the camera reported.
        fpx = _focal_px(img) if img is not None else None
        return _aspect_from_focal(quad, size, fpx) if fpx else None
    f = np.sqrt(f2)
    Ainv = np.diag([1.0 / f, 1.0 / f, 1.0])
    ar = float(np.linalg.norm(Ainv @ n2) / np.linalg.norm(Ainv @ n3))
    if not np.isfinite(ar) or not (0.1 < ar < 10.0):
        return None
    return ar


def rectify(img):
    """Square the page up. Returns the image unchanged when no whole page is
    found — see _page_quad for why that is a refusal and not a fallback guess."""
    q = _page_quad(img)
    if q is None:
        return img, False
    tl, tr, br, bl = q
    wid = int(round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))))
    hei = int(round(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))))
    # Restore the sheet's real proportions. Keep the longer measured side and
    # derive the other from the recovered aspect, so nothing is upscaled.
    ar = _true_aspect(q, img.size, img)
    if ar:
        if wid >= hei:
            hei = int(round(wid / ar))
        else:
            wid = int(round(hei * ar))
    if wid < 200 or hei < 200:
        return img, False
    dst = [(0, 0), (wid, 0), (wid, hei), (0, hei)]
    coeffs = _perspective_coeffs(dst, [tuple(p) for p in q])
    out = img.transform((wid, hei), Image.PERSPECTIVE, coeffs, Image.BICUBIC)
    return out, True


def whiten(img, gray=False, sat=1.35, black_point=0.10, paper_pct=80.0,
           knee=0.80, autocrop=True):
    """Flatten the lighting, set the white point from the paper itself, keep ink.

    2026-08-08, tuned against a Genius Scan render of the same sheet. The first
    version left 73% of pixels at true white where Genius Scan reaches 93%, and
    the residue read as grey mottling across the page. Two causes:

      * The white point was a fixed constant. Now it is measured: the 88th
        percentile of the flattened image IS the paper, whatever the paper is,
        so it maps to 1.0 by construction.
      * Nothing forced paper to be uniform. A soft knee above `knee` ramps the
        top of the range to pure white, so paper texture and sensor noise
        collapse to 255 while ink edges keep their gradation — a hard clip
        here would eat the thin lines on a drawing.
    """
    # Geometry runs on the ORIGINAL object, not a converted copy: .convert()
    # drops the EXIF, and the focal length in there is what rescues the aspect
    # ratio when the corners alone cannot give it.
    src = img
    if autocrop:
        # Perspective first: it both crops AND squares up. The axis-aligned box
        # is the fallback for a sheet that runs out of frame, where there is no
        # quad to warp.
        src, warped = rectify(src)
        if warped:
            src, _ = upright(src)
        if not warped:
            g = np.asarray(src.convert("L"), dtype=np.float32)
            l, t, r, b = _page_box(g)
            if (r - l, b - t) != (src.width, src.height):
                src = src.crop((l, t, r, b))
    src = src.convert("RGB")
    a = np.asarray(src, dtype=np.float32)

    # Non-white stock: crop and straighten, but leave the tone alone. Every step
    # below assumes the paper should end up white.
    white, frac = paper_is_white(src)
    if not white:
        return src, False

    # 1. illumination estimate: blur radius scaled to the page, so it tracks
    # lighting (centimetres) and never ink (millimetres).
    radius = max(src.size) / 20.0
    bg = np.asarray(src.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)
    flat = a / np.maximum(bg, 1.0)          # ~1.0 on paper, <1 on ink

    # 2. white point MEASURED from the paper, not assumed.
    # Percentile over the MIDDLE of the sheet: after cropping there is still a
    # shadow rim where the paper curls, and including it drags the estimate
    # down so the true paper never reaches white.
    lum = flat.mean(axis=2)
    h, w = lum.shape
    core = lum[int(h * 0.10):int(h * 0.90), int(w * 0.10):int(w * 0.90)]
    paper = float(np.percentile(core if core.size else lum, paper_pct))
    if paper > 0.2:
        flat = flat / paper
    flat = np.clip((flat - black_point) / (1.0 - black_point), 0.0, 1.0)

    # 3. soft knee: everything above `knee` ramps to pure white.
    hi = flat >= knee
    flat[hi] = 1.0
    band = (flat < knee) & (flat > knee - 0.10)
    flat[band] = knee - 0.10 + (flat[band] - (knee - 0.10)) * (
        (1.0 - (knee - 0.10)) / 0.10)
    flat = np.clip(flat, 0.0, 1.0)

    out = Image.fromarray((flat * 255.0).astype(np.uint8), "RGB")

    # 4. colour. Boost AFTER flattening: the division desaturates slightly, and
    # a drawing's colour coding is information, not decoration.
    if gray:
        out = out.convert("L").convert("RGB")
    elif sat != 1.0:
        out = ImageEnhance.Color(out).enhance(sat)
    return out, True


def scale(img, max_px, keep=False):
    """Cap the long edge. Everything above ~2000 px on a page-sized sheet is
    sensor resolution, not information."""
    if keep or max(img.size) <= max_px:
        return img
    f = max_px / float(max(img.size))
    return img.resize((max(1, int(img.width * f)), max(1, int(img.height * f))),
                      Image.LANCZOS)


def build(inputs, out_path, max_px=2800, quality=78, gray=False,
          keep_size=False, autocrop=True, verbose=True):
    pages = []
    for p in inputs:
        ext = os.path.splitext(p)[1].lower()
        if ext == ".pdf":
            pages += _pages_from_pdf(p)
        elif ext in IMG_EXTS:
            pages.append(Image.open(p).convert("RGB"))
        else:
            raise ValueError(f"unsupported input: {p}")
    if not pages:
        raise ValueError("no pages")
    # The original capture is the only full-resolution copy and this transform
    # is lossy and one-way. Writing over an input would make reprocessing
    # impossible, so it is refused rather than warned about.
    if any(os.path.abspath(p) == os.path.abspath(out_path) for p in inputs):
        raise ValueError(f"refusing to overwrite the original: {out_path}")

    done = []
    for im in pages:
        im, _ = whiten(im, gray=gray, autocrop=autocrop)
        im = scale(im, max_px, keep=keep_size)
        # Round-trip through JPEG explicitly so the PDF embeds the compressed
        # bytes we chose, not Pillow's default for the container.
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        buf.seek(0)
        done.append(Image.open(buf).convert("RGB"))

    # 150 dpi nominal: a ~2200 px long edge over a ~14 in sheet.
    done[0].save(out_path, "PDF", save_all=True, append_images=done[1:],
                 resolution=150.0, quality=quality, optimize=True)
    if verbose:
        src = sum(os.path.getsize(p) for p in inputs)
        dst = os.path.getsize(out_path)
        print(f"{len(done)} page(s)  {src/1e6:.1f}MB -> {dst/1e3:.0f}KB "
              f"({src/max(dst,1):.0f}x smaller)  {max_px}px q{quality}")
    return out_path


def to_image(src_path, out_dir=None, suffix="-scan", max_px=2800, quality=78,
             **kw):
    """One cropped, straightened JPEG. Same geometry work as the PDF path.

    The owner: a non-white document "can be saved as a photo (jpg)
    instead of pdf, unless prompted by user" — a brochure is a picture, and
    wrapping a picture in a PDF adds a container without adding anything.
    Whitening is skipped automatically for non-white stock (see paper_is_white),
    so this is the natural output for those.
    """
    src_path = os.path.abspath(src_path)
    out_dir = os.path.abspath(out_dir or os.path.dirname(src_path))
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src_path))[0]
    out = os.path.join(out_dir, f"{stem}{suffix}.jpg")
    im = Image.open(src_path)
    im, whitened = whiten(im, **kw)
    im = scale(im, max_px)
    im.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    return out, whitened


def process(src_path, out_dir=None, suffix="-doc", **kw):
    """Make a document PDF from a photo, WITHOUT touching the photo.

    The owner: "when processing an image into a document, keep the
    original image and create a document (PDF)." The original is the only copy
    of the full-resolution capture — whitening and downscaling are lossy and
    one-way, so a pipeline that consumed its input would destroy the ability to
    reprocess later (better parameters, a different crop, a colour question the
    flattened version can no longer answer).

    Returns (original_path, pdf_path). The original is never moved, renamed or
    rewritten, and the output refuses to land on top of any input.
    """
    src_path = os.path.abspath(src_path)
    out_dir = os.path.abspath(out_dir or os.path.dirname(src_path))
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src_path))[0]
    out = os.path.join(out_dir, f"{stem}{suffix}.pdf")
    n = 1
    while os.path.exists(out) and os.path.abspath(out) != src_path:
        out = os.path.join(out_dir, f"{stem}{suffix}-{n}.pdf")
        n += 1
    build([src_path], out, **kw)
    return src_path, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--out",
                    help="output PDF; omit to write <input>-doc.pdf beside each "
                         "input, keeping every original untouched")
    ap.add_argument("--max-px", type=int, default=2800,
                    help="cap on the long edge in pixels")
    ap.add_argument("--quality", type=int, default=78)
    ap.add_argument("--no-crop", action="store_true",
                    help="keep the whole photo; do not crop to the sheet")
    ap.add_argument("--gray", action="store_true",
                    help="greyscale — smaller, but destroys colour coding")
    ap.add_argument("--keep-size", action="store_true")
    a = ap.parse_args()
    kw = dict(max_px=a.max_px, quality=a.quality, gray=a.gray,
              keep_size=a.keep_size, autocrop=not a.no_crop)
    if a.out:
        build(a.inputs, a.out, **kw)
    else:
        for src in a.inputs:
            orig, pdf = process(src, **kw)
            print(f"  kept {os.path.basename(orig)}  ->  {os.path.basename(pdf)}")


if __name__ == "__main__":
    main()
