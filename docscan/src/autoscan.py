#!/usr/bin/env python3
"""autoscan — find the documents in a photo, extract each one, file them.

The owner: "if I send you a photo, you need to detect if it contains
a document, extract it, and save it into the KB. Docs on white paper as PDF,
others as JPEG. A photo may contain a white document AND a non-white one."

docscan handles ONE known page. This adds the two things that turns into a
pipeline you can point at any photo:

  * FIND ALL of them, not one. docscan flood-fills from the centre of frame
    because "the document is what you aimed at". That is exactly wrong when
    there are two sheets side by side — so this labels every connected region in
    both polarity masks and keeps each one that looks like a sheet.
  * DECIDE WHETHER THERE IS A DOCUMENT AT ALL. A photo of a flowerbed must
    produce nothing rather than a confident crop of some leaves. Two independent
    tests have to agree: the region has to be RECTANGULAR (its mask fills its
    own corner quad), and it has to carry INK STRUCTURE (line-like organisation
    a natural scene does not have).

Routing follows the paper, which docscan already classifies: white stock is a
document and becomes a PDF; coloured or dark stock is artwork and becomes a
JPEG, because wrapping a picture in a PDF adds a container and nothing else.
"""
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import docscan

WORK = 900
MIN_AREA = 0.04           # of the frame — smaller than this is clutter, not a page
MAX_AREA = 0.95
MIN_SOLIDITY = 0.80       # mask area / quad area: how rectangular the region is.
                          # 0.86 rejected a real card at 0.84 — a dark sheet's
                          # edge blends into a dark desk and frays the mask. The
                          # ink-structure test is the real discriminator, so this
                          # only has to exclude obviously ragged blobs.
MIN_INK = 4.0             # ink-projection std. Measured: real pages score 11-17,
                          # a photographed flowerbed 2.3 — the gap is wide, so the
                          # threshold sits in the middle rather than at either edge.
MAX_ELONGATION = 5.0      # w/h or h/w. A 2603x231 sliver of desk edge passed every
                          # other test on the brochure shot; no page is 11:1.


def _components(mask, min_px):
    """Every connected region of `mask`, largest first.

    Flood-fill labelling rather than scipy: one dependency less, and at 900 px
    the cost is irrelevant. PIL's floodfill is C, so this is a few ms.
    """
    h, w = mask.shape
    work = Image.fromarray((mask.astype(np.uint8) * 255), "L").copy()
    arr = np.array(work)
    out = []
    label = 1
    while True:
        ys, xs = np.nonzero(arr == 255)
        if xs.size == 0 or label > 12:
            break
        ImageDraw.floodfill(work, (int(xs[0]), int(ys[0])), label)
        arr = np.array(work)
        comp = arr == label
        if comp.sum() >= min_px:
            out.append(comp)
        label += 1
        if label == 255:
            break
    out.sort(key=lambda m: -m.sum())
    return out


def _ink_structure(img):
    """How line-organised the content is. Text and drawings score high; foliage,
    fabric and other natural texture score low, because their energy is spread
    evenly instead of collecting into rows or columns."""
    r, c = docscan._text_axis_score(img)
    return max(r, c)


def find_documents(img, work=WORK):
    """Quads of every document-looking region, in full-image coordinates."""
    g = img.convert("L")
    f = work / float(max(g.size))
    if f < 1.0:
        g = g.resize((max(1, int(g.width * f)), max(1, int(g.height * f))),
                     Image.BILINEAR)
    else:
        f = 1.0
    a = np.asarray(g.filter(ImageFilter.GaussianBlur(2)), dtype=np.float32)
    h, w = a.shape
    frame = float(h * w)

    # A LADDER of thresholds, not just Otsu. One global split assumes the scene
    # has two levels; a photo with a white sheet AND a dark card on a mid-tone
    # desk has three, and whichever pair Otsu picks, the third merges into a
    # neighbour. (Built exactly that case as a test: the dark card fused with
    # the desk into one 0.82-of-frame blob and was thrown out for being ragged.)
    # Sweeping percentiles finds each object at whatever level separates IT, and
    # the overlap dedupe below collapses the repeats.
    # Spaced across the luminance RANGE, not by percentile. Percentiles follow
    # histogram mass, and on a photo where the desk is most of the frame the
    # 25th, 45th and 65th all landed on the same desk value — the ladder
    # collapsed to one rung and the dark card (well below it) was never isolated.
    lo, hi = float(a.min()), float(a.max())
    levels = [docscan._otsu(a)] + [lo + (hi - lo) * k / 6.0 for k in range(1, 6)]
    found = []
    # Both polarities, because a photo can hold a white sheet AND a dark card:
    # neither mask alone contains both.
    masks = []
    for thr in levels:
        masks.append(a > thr)
        masks.append(a <= thr)
    for mask in masks:
        cov = mask.mean()
        if cov < 0.02 or cov > 0.98:
            continue                          # nothing to segment at this level
        for comp in _components(mask, int(frame * MIN_AREA)):
            area = comp.sum()
            if not (frame * MIN_AREA <= area <= frame * MAX_AREA):
                continue
            quad = docscan._quad_from_mask_raw(comp, w, h)
            if quad is None:
                continue
            qarea = _quad_area(quad)
            if qarea <= 0 or area / qarea < MIN_SOLIDITY:
                continue                      # ragged blob, not a sheet
            side = _quad_sides(quad)
            if max(side) / max(min(side), 1e-6) > MAX_ELONGATION:
                continue                      # a strip of something, not a page
            # A region touching the frame border is the SURFACE, not a sheet on
            # it. Relaxing solidity let the desk itself win the ranking and the
            # brochure came back as a photo of a desk with a card on it. A whole
            # document has all four corners inside the picture — that is what
            # makes it whole.
            m = 0.01
            if (quad[:, 0].min() <= w * m or quad[:, 1].min() <= h * m or
                    quad[:, 0].max() >= w * (1 - m) or
                    quad[:, 1].max() >= h * (1 - m)):
                continue
            found.append((quad / f, area / qarea, area / frame))

    # Several thresholds describe the same sheet, and one bad threshold describes
    # TWO sheets as a single blob. Rank by SOLIDITY, not size: the most
    # rectangular reading of a region is the right one, and it is what stops a
    # sprawling both-documents-and-the-desk blob from suppressing the two real
    # pages underneath it (it did, on the two-document test).
    found.sort(key=lambda t: -t[1])
    kept = []
    for quad, _sol, _area in found:
        if not any(_overlaps(quad, k) for k in kept):
            kept.append(quad)
    return kept


def _quad_sides(q):
    w = max(np.linalg.norm(q[1] - q[0]), np.linalg.norm(q[2] - q[3]))
    h = max(np.linalg.norm(q[3] - q[0]), np.linalg.norm(q[2] - q[1]))
    return (float(w), float(h))


def _quad_area(q):
    return 0.5 * abs(sum(q[i][0] * q[(i + 1) % 4][1] - q[(i + 1) % 4][0] * q[i][1]
                         for i in range(4)))


def _overlaps(a, b, thresh=0.5):
    ax0, ay0 = a[:, 0].min(), a[:, 1].min()
    ax1, ay1 = a[:, 0].max(), a[:, 1].max()
    bx0, by0 = b[:, 0].min(), b[:, 1].min()
    bx1, by1 = b[:, 0].max(), b[:, 1].max()
    iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    ih = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    if inter <= 0:
        return False
    small = min((ax1 - ax0) * (ay1 - ay0), (bx1 - bx0) * (by1 - by0))
    return small > 0 and inter / small > thresh


def extract(img, quad):
    """Rectify one region to an upright page. Returns (image, is_white_paper)."""
    page = docscan.rectify_quad(img, quad)
    if page is None:
        return None, None
    page, _ = docscan.upright(page)
    white, _ = docscan.paper_is_white(page)
    return page, white


def scan(path, out_dir, stem=None, max_px=2800, quality=78, preview_dir=None):
    """Find, extract and write every document in `path`.

    Returns a list of dicts: {path, kind, white, size, preview}. The source photo
    is never modified — same reasoning as docscan.process: this transform is lossy
    and one-way, and the original is the only full-resolution copy.

    `preview_dir` asks for a small JPEG of each extracted page as well. A page
    that became a PDF is otherwise unreadable to anything that only takes images
    — which is exactly what the caller needs to caption it (a vision model
    cannot look at a PDF). A JPEG result is already its own preview.
    """
    img = Image.open(path)
    stem = stem or os.path.splitext(os.path.basename(path))[0]
    os.makedirs(out_dir, exist_ok=True)
    results = []
    quads = find_documents(img)
    for i, quad in enumerate(quads, 1):
        page, white = extract(img, quad)
        if page is None:
            continue
        if _ink_structure(page) < MIN_INK:
            continue                      # rectangular, but no document on it
        suffix = "" if len(quads) == 1 else f"-{i}"
        if white:
            page, _ = docscan.whiten(page, autocrop=False)
            page = docscan.scale(page, max_px)
            out = os.path.join(out_dir, f"{stem}{suffix}.pdf")
            page.save(out, "PDF", resolution=150.0, quality=quality, optimize=True)
        else:
            page = docscan.scale(page, max_px)
            out = os.path.join(out_dir, f"{stem}{suffix}.jpg")
            page.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
        preview = out
        if white and preview_dir:
            os.makedirs(preview_dir, exist_ok=True)
            preview = os.path.join(preview_dir, f"{stem}{suffix}-preview.jpg")
            docscan.scale(page, 1400).convert("RGB").save(
                preview, "JPEG", quality=70, optimize=True)
        results.append({"path": out, "kind": "pdf" if white else "jpg",
                        "white": bool(white), "size": os.path.getsize(out),
                        "preview": preview})
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("photo")
    ap.add_argument("-o", "--out-dir", default=".")
    ap.add_argument("--stem")
    a = ap.parse_args()
    res = scan(a.photo, a.out_dir, stem=a.stem)
    if not res:
        print("no document found")
    for r in res:
        print(f"  {r['kind']}  {'white paper' if r['white'] else 'non-white stock'}"
              f"  {r['size']/1024:.0f}KB  {r['path']}")
