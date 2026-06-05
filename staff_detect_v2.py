#!/usr/bin/env python3
"""
staff_detect_v2.py  —  robust staff detection for crop_piano_pdfs.py

WHY THIS EXISTS
---------------
The original detector in crop_piano_pdfs.py (compute_strip_density →
find_clusters → group_into_staves) flags a row as a staff line when enough
SHORT dark runs appear across 24 strips. On dense / lyric-heavy pages the notes
fill the vertical gaps, so an entire system reads as one solid band and is
detected as a single ~350px "stave." Result: those pages keep the whole system
(vocals included) — the crop does nothing. Half the pages of a song crop right,
half pass through. Validated failure: Hand In My Pocket pages 3–7.

THE FIX
-------
A real staff line is the ONE thing on the page that is a single CONTINUOUS
horizontal run spanning most of the score width. Notes, stems, lyrics, slurs
never are. So:
  1. longest continuous horizontal dark run per row  (<=2px scan-gaps closed)
  2. row is a staff line iff that run >= 0.28 * score_width (skew-tolerant)
  3. group line-rows into 5-line staves using the MEASURED line spacing
     (median consecutive-line gap), not gap-jump guessing
  4. hand clean staves to crop_piano_pdfs.find_piano_pairs (bass-clef via
     measure-number signal) — which works reliably once staves are clean
Validated: HIMP all 8 pages now resolve 9-10 individual staves (~46px each)
and 3 piano regions each, zero blobs; Overture p3/p4 resolve 12 clean staves.

KNOWN LIMIT — SKEW: heavily skewed scans break the continuous-run test (a flat
pixel row crosses a tilted staff line only part-way). The 2px gap-close + 0.28
cutoff recovers most pages, but a few badly skewed pages still find 0 staves
(All I Really Want p8/p10). When detect_staves returns [], the caller must keep
the WHOLE page (safe: a title page or an undetected page just passes through to
PlayScore as before — no worse than today). NEXT STEP to close the gap: deskew
each page (estimate dominant staff-line angle via Hough/projection, rotate to
horizontal) before detection. The QC contact sheet (TODO 3) will show exactly
which pages need it.

INTEGRATION TODO (do in Claude Code, in the JLP_merge repo)
-----------------------------------------------------------
1. In crop_piano_pdfs.process_page, REPLACE the two-pass strip-density block
   (the `for run_div in (4,8)` loop) with a call to detect_staves() below.
   Keep everything downstream (find_piano_pairs, piano_regions, output).
2. OUTPUT QUALITY: source scans are ~600 DPI (5100x6599). The tool currently
   renders/analyzes/exports at 200 DPI. Detect at 200 (these params are tuned
   for it), but build OUTPUT at >=300 DPI — better, redact the vocal bands to
   white directly on a 300–400 DPI render and keep it, instead of down-
   rastering. Crisper notation → better PlayScore OMR.
3. QC CONTACT SHEET: add a mode that renders every page of every file as a
   thumbnail with detected staves (blue) and kept regions (red) drawn on, laid
   out in a grid, one PNG per cue. This catches bad crops in seconds. Use it to
   resolve the known edge cases: 'too-few-lines' pages (title/near-empty pages —
   keep whole page) and any page that falls to the fallback path.

PUBLIC API
----------
    detect_staves(page, dpi=200) -> list[(top, bot)]        # individual staves
    detect(page, dpi=200) -> (img, (x0,x1), staves, regions, method)
"""
import sys
sys.path.insert(0, "/Users/chrisdebord/JLP_merge")
import numpy as np
import fitz
from PIL import Image
import crop_piano_pdfs as C

DET_DPI       = 200
LINE_RUN_FRAC = 0.28   # row is a staff line if its longest dark run >= this * score width (skew-tolerant)
DARK_THRESH   = 170    # < this (on contrast-normalized image) counts as ink
STAVE_GAP_K   = 1.8    # new stave when line gap > K * median line spacing
SYS_GAP_K     = 2.6    # new system when stave gap > K * median line spacing (fallback only)
MIN_STAVE_K   = 2.0    # drop "staves" shorter than K * median line spacing (stray lines)


def _longest_run_rows(dark: np.ndarray) -> np.ndarray:
    """Longest continuous horizontal True-run per row; closes small breaks first."""
    d = dark.copy()
    d[:, 2:] |= dark[:, :-2]      # close up to 2px breaks (scan skew / faint staff lines)
    d[:, 1:] |= d[:, :-1]
    _, W = d.shape
    cur = d[:, 0].astype(np.int32)
    longest = cur.copy()
    for c in range(1, W):
        cur = (cur + 1) * d[:, c]
        longest = np.maximum(longest, cur)
    return longest


def _bands(mask: np.ndarray) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    inb = False; s = 0
    for y in range(len(mask)):
        if mask[y] and not inb:
            s, inb = y, True
        elif not mask[y] and inb:
            out.append((s, y - 1)); inb = False
    if inb:
        out.append((s, len(mask) - 1))
    return out


def _render_gray(page: fitz.Page, dpi: int):
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return img


# ── Deskew (closes the heavy-skew gap) ────────────────────────────────────────
# A flat pixel row only crosses a tilted staff line part-way, so even a sub-degree
# tilt drops the longest continuous run below the cutoff and detection returns 0
# staves (e.g. All I Really Want p8/p10: max run 389px vs a 404px threshold — the
# line drifts out of the flat row after ~350px). Fix: rotate the page so staff
# lines are horizontal BEFORE detection.
#
# The angle is scored by the DETECTION METRIC ITSELF — the number of rows whose
# longest dark run clears LINE_RUN_FRAC*width.  A plain dark-pixel-variance score
# fails here because note/lyric content dominates the variance and peaks at 0°;
# the staff-row count instead jumps from ~0 to >100 at the true angle (a tilted
# line snaps to full width).  We only adopt a tilt when it CLEARLY beats the flat
# baseline, so already-horizontal pages stay at 0° untouched.  The SAME angle is
# re-applied to the output render so kept regions land correctly (see
# crop_piano_pdfs.detect_page).
SKEW_MAX_DEG       = 4.0   # search range ±deg (these scans are never worse than this)
SKEW_MIN_APPLY     = 0.10  # don't rotate below this (avoids needless resampling)
SKEW_MIN_GAIN_ROWS = 20    # adopt a tilt only if it adds at least this many staff-rows


def _staffline_row_count(small: np.ndarray, angle: float) -> int:
    """How many rows look like staff lines after rotating `small` by `angle`.

    This is the same continuous-run test detection uses, so maximizing it is
    exactly maximizing detector success.
    """
    if angle == 0.0:
        rot = small
    else:
        rot = np.asarray(Image.fromarray(small, mode='L').rotate(
            angle, resample=Image.BILINEAR, expand=False, fillcolor=255))
    norm = C.normalize_contrast(rot)
    x0, x1 = C.find_score_x_bounds(norm)
    w = x1 - x0
    if w < 20:
        return 0
    dark = norm[:, x0:x1] < DARK_THRESH
    return int((_longest_run_rows(dark) >= LINE_RUN_FRAC * w).sum())


def estimate_skew_angle(img: np.ndarray, max_deg: float = SKEW_MAX_DEG) -> float:
    """Estimate the dominant staff-line tilt (deg) via coarse→fine search.

    The whole search runs at FULL resolution: the staff-row peak can be as narrow
    as ±0.2° (e.g. All I Really Want p0), and downsampling washes it out — a flat
    row only stays inside a thin tilted line for a limited span, and that span
    shrinks with resolution.  Cost is bounded because the caller only invokes
    this on pages that already failed flat detection.  Returns 0.0 unless a
    non-zero angle clearly beats the flat baseline, so already-horizontal pages
    are never perturbed.
    """
    H, W = img.shape
    if W < 40 or H < 40:
        return 0.0

    base = _staffline_row_count(img, 0.0)
    # Coarse: 0.5° steps to bracket the tilt.
    coarse_a, coarse_s = 0.0, base
    for i in range(int(-max_deg * 2), int(max_deg * 2) + 1):
        a = i * 0.5
        if a == 0.0:
            continue
        s = _staffline_row_count(img, a)
        if s > coarse_s:
            coarse_s, coarse_a = s, a
    if coarse_a == 0.0:
        return 0.0          # nothing beats horizontal → page is straight

    # Fine: 0.1° steps around the coarse estimate.
    best_a, best_s = 0.0, base
    for d in range(-6, 7):
        a = round(coarse_a + d * 0.1, 2)
        if abs(a) > max_deg + 0.5:
            continue
        s = _staffline_row_count(img, a)
        if s > best_s:
            best_s, best_a = s, a

    # Adopt the tilt only if it clearly beats horizontal — guards good pages.
    if best_a != 0.0 and best_s >= base + SKEW_MIN_GAIN_ROWS and best_s > base * 1.10:
        return best_a
    return 0.0


def deskew_image(img: np.ndarray, angle: float) -> np.ndarray:
    """Rotate a grayscale image by `angle` degrees (white fill, same size)."""
    if abs(angle) < SKEW_MIN_APPLY:
        return img
    im = Image.fromarray(img, mode='L').rotate(
        angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
    return np.asarray(im)


def detect_on_image(img: np.ndarray):
    """Core detection on an already-rendered (and deskewed) gray image.

    Returns ((x0, x1), staves). Used directly by crop_piano_pdfs.detect_page so
    it can drive deskew once and reuse the angle for the output render.
    """
    H, W = img.shape
    norm = C.normalize_contrast(img)
    x0, x1 = C.find_score_x_bounds(norm)
    w = x1 - x0
    if w < 20:
        return (x0, x1), []
    dark = norm[:, x0:x1] < DARK_THRESH
    isline = _longest_run_rows(dark) >= LINE_RUN_FRAC * w

    lb = _bands(isline)
    merged: list[tuple[int, int]] = []
    for b in lb:
        if merged and b[0] - merged[-1][1] <= 3:
            merged[-1] = (merged[-1][0], b[1])
        else:
            merged.append(b)
    lb = merged
    if len(lb) < 2:
        return (x0, x1), []               # title / near-empty page → caller keeps whole page

    centers = [(t + b) // 2 for t, b in lb]
    line_sp = float(np.median(np.diff(centers))) or 1.0

    staves: list[tuple[int, int]] = []
    cur = [lb[0]]
    for i in range(1, len(lb)):
        if centers[i] - centers[i - 1] > STAVE_GAP_K * line_sp:
            staves.append((cur[0][0], cur[-1][1])); cur = [lb[i]]
        else:
            cur.append(lb[i])
    staves.append((cur[0][0], cur[-1][1]))
    staves = [(t, b) for t, b in staves if (b - t) >= MIN_STAVE_K * line_sp]
    return (x0, x1), staves


def detect_staves(page: fitz.Page, dpi: int = DET_DPI, deskew: bool = True):
    """Render a page, deskew it if needed, and return (img, (x0,x1), staves).

    img is the (possibly deskewed) detection-DPI render the staves index into.
    Deskew is a RESCUE: only attempted when flat detection looks weak (few staves
    or a merged blob), and adopted only if it finds more staves — mirroring
    crop_piano_pdfs.detect_page (which additionally re-uses the angle for the
    high-DPI output).  This wrapper is for the smoke test / standalone callers.
    """
    img = _render_gray(page, dpi)
    (x0, x1), staves = detect_on_image(img)
    if deskew and (len(staves) < 6 or any((b - t) > 120 for t, b in staves)):
        a = estimate_skew_angle(img)
        if abs(a) >= SKEW_MIN_APPLY:
            img2 = deskew_image(img, a)
            (x0b, x1b), staves2 = detect_on_image(img2)
            if len(staves2) > len(staves):
                img, x0, x1, staves = img2, x0b, x1b, staves2
    return img, (x0, x1), staves


def detect(page: fitz.Page, dpi: int = DET_DPI):
    """Full detection → (img, (x0,x1), staves, piano_regions, method)."""
    img, (x0, x1), staves = detect_staves(page, dpi)
    if len(staves) < 2:
        return img, (x0, x1), staves, [], "too-few-staves"

    # Piano regions come from brace detection (the { grand-staff marker) — see
    # crop_piano_pdfs.find_piano_braces.  A region is kept only for a braced pair.
    regions = C.find_piano_braces(C.normalize_contrast(img), staves, x0, x1)
    return img, (x0, x1), staves, regions, "braced grand staves"


if __name__ == "__main__":
    # Smoke test / per-page report:  python staff_detect_v2.py <pdf>
    path = sys.argv[1] if len(sys.argv) > 1 else "/Users/chrisdebord/JLP.piano.03.Hand_In_My_Pocket.pdf"
    doc = fitz.open(path)
    print(f"{path}  ({len(doc)} pages)")
    for i in range(len(doc)):
        _, _, staves, regions, m = detect(doc[i])
        hh = [b - t for t, b in staves]
        blob = " <-- BLOB" if any(h > 120 for h in hh) else ""
        print(f"  p{i}: {len(staves):2d} staves  {len(regions):2d} regions  {m:18s} maxH={max(hh) if hh else 0}{blob}")
    doc.close()
