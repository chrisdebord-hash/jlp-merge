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


def detect_staves(page: fitz.Page, dpi: int = DET_DPI):
    """Return individual staff regions [(top, bot), ...] (empty if none found)."""
    img = _render_gray(page, dpi)
    H, W = img.shape
    norm = C.normalize_contrast(img)
    x0, x1 = C.find_score_x_bounds(norm)
    w = x1 - x0
    if w < 20:
        return img, (x0, x1), []
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
        return img, (x0, x1), []          # title / near-empty page → caller keeps whole page

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
    return img, (x0, x1), staves


def detect(page: fitz.Page, dpi: int = DET_DPI):
    """Full detection → (img, (x0,x1), staves, piano_regions, method)."""
    img, (x0, x1), staves = detect_staves(page, dpi)
    if len(staves) < 2:
        return img, (x0, x1), staves, [], "too-few-staves"

    regions = C.find_piano_pairs(C.normalize_contrast(img), staves, x0)
    method = "measure-num pairs"
    if not regions:                                   # fallback: system grouping + bottom-2
        norm = C.normalize_contrast(img)
        x0b, x1b = C.find_score_x_bounds(norm)
        centers = [(t + b) // 2 for t, b in staves]
        line_sp = float(np.median(np.diff(centers))) if len(centers) > 1 else 30.0
        systems: list[list[tuple[int, int]]] = []
        cur = [staves[0]]
        for i in range(1, len(staves)):
            if staves[i][0] - staves[i - 1][1] > SYS_GAP_K * line_sp:
                systems.append(cur); cur = [staves[i]]
            else:
                cur.append(staves[i])
        systems.append(cur)
        regions = []
        for s in systems:
            p = s[-2:] if len(s) > 2 else s
            regions.append((max(p[0][0] - int(0.5 * line_sp), 0),
                            min(p[-1][1] + int(line_sp), img.shape[0] - 1)))
        method = "fallback systems"
    return img, (x0, x1), staves, regions, method


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
