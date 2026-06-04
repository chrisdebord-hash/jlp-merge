#!/usr/bin/env python3
"""
crop_piano_pdfs.py

Strip vocal staves from piano-vocal score PDFs, keeping only the piano grand
staff (treble + bass, the two staves marked by the { brace).

Strategy
--------
Rather than trying to detect the curved { brace pixel-by-pixel (unreliable on
mixed-quality scans), we exploit the invariant that piano staves are always the
BOTTOM two staves in every system of these vocal-piano scores.

For each page:
  1. Render at DPI (200 for analysis).
  2. Normalize contrast so faint scans work the same as dark ones.
  3. Auto-detect the horizontal extent of the score area (some PDFs have the
     score occupying only the left ~30 % of the page width).
  4. Find "staff-line rows" — rows where a high fraction of equal-width
     vertical strips across the score area each contain at least one dark pixel.
  5. Cluster those rows into individual staff lines, then group lines into
     staves (5 lines each) using the gap-percentile heuristic.
  6. Group staves into systems (inter-system gaps are larger than intra-system).
  7. For each system, keep the bottom two staves (piano treble + bass).
     If a system already has only two staves it is piano-only — keep it whole.
  8. Stack the piano strips from all systems and write a new PDF page.

Usage
-----
  # Test on one file, write a preview PNG of page 1:
  python crop_piano_pdfs.py --test

  # Process all 42 PDFs:
  python crop_piano_pdfs.py
"""

import io
import sys
import argparse
import numpy as np
import fitz
from pathlib import Path
from PIL import Image, ImageDraw

# ── Paths ─────────────────────────────────────────────────────────────────────
SRC_DIR = Path("/Users/chrisdebord/Library/Mobile Documents/com~apple~CloudDocs"
               "/FYL/Jagged Little Pill/scores/piano")
OUT_DIR = Path("/Users/chrisdebord/Library/Mobile Documents/com~apple~CloudDocs"
               "/FYL/Jagged Little Pill/scores/piano_cropped")

# ── Parameters ────────────────────────────────────────────────────────────────
ANALYSIS_DPI  = 200   # DPI used for staff detection (find_piano_pairs windows are tuned for it)
OUTPUT_DPI    = 300   # DPI for the exported page; scans are ~600, 300 is crisp for PlayScore OMR
SKEW_RETRY_STAVES = 6 # below this many detected staves, attempt the deskew rescue (see detect_page)
N_STRIPS      = 24    # number of equal-width horizontal strips for staff detection
MIN_STRIP_FRAC = 0.42 # fraction of strips that must be dark for a "staff row"
CLUSTER_GAP   = 8     # max pixel gap to merge adjacent rows into one cluster
INTRA_STAVE_PERCENTILE = 82  # gaps below this percentile are within-stave
SYSTEM_SEP_FACTOR = 1.75     # inter-system gap > factor × median intra-system gap
TOP_PAD_PX    = 14    # pixels of padding above the piano treble stave
BOT_PAD_PX    = 20    # pixels of padding below the piano bass stave

# Bass-clef detection via measure-number signal
BASS_BELOW_LOOK_H   = 32   # px to scan below the stave bottom for measure numbers
BASS_BELOW_LOOK_W   = 80   # px wide from x0 (measure numbers live here)
BASS_BELOW_OFFSET   =  8   # skip the first few px (avoids stave bottom bar-line blur)
BASS_DARK_THRESHOLD = 120  # local-min below_dark < this → bass clef stave
MAX_PIANO_SPAN      = 350  # max px between top of treble and bottom of bass stave

# Output quality / PlayScore compatibility
MIN_STAVE_H_PX      =  6   # reject "staves" shorter than this (spurious lines, arrows)
MAX_STAVE_H_PX      = 9999 # effectively disabled — tall merged groups are handled downstream
MIN_PAGE_HEIGHT_PT  = 300  # minimum output page height in points; shorter pages are padded


# ── Image utilities ───────────────────────────────────────────────────────────

def render_gray(page: fitz.Page, dpi: int) -> np.ndarray:
    """Render a page to a grayscale numpy array at the given DPI."""
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)


def normalize_contrast(img: np.ndarray) -> np.ndarray:
    """Stretch the 1st–99th percentile of pixel values to the 0–255 range."""
    lo = float(np.percentile(img, 1))
    hi = float(np.percentile(img, 99))
    return np.clip((img.astype(float) - lo) / max(hi - lo, 1.0) * 255,
                   0, 255).astype(np.uint8)


def find_score_x_bounds(norm: np.ndarray) -> tuple[int, int]:
    """
    Return (x_start, x_end) of the active staff-line area.

    Staff lines appear in many rows at a given x position.  We count dark
    pixels per column but ONLY in the middle 60 % of rows (excluding the
    top and bottom 20 % which carry headers, footers, and tempo markings).
    This filters out isolated title/page-number content that would otherwise
    inflate the x bounds, causing strips to be too wide for narrow scores.
    """
    H, W = norm.shape
    y0 = H // 5           # skip top 20 %
    y1 = H - H // 5      # skip bottom 20 %
    col_dark = (norm[y0:y1, :] < 100).sum(axis=0).astype(float)

    # Require a column to have dark pixels in many rows — staff lines are
    # consistent; a lone bar-line or bracket appears in far fewer rows.
    # Use 5 % of the central height as the minimum row-count per column.
    min_rows = max((y1 - y0) * 0.05, 3)
    active_mask = col_dark >= min_rows

    # Smooth to merge close gaps
    k = np.ones(15) / 15
    smoothed = np.convolve(active_mask.astype(float), k, mode='same')
    active = np.where(smoothed > 0.2)[0]

    if len(active) < 10:
        # Fallback: use full-width scan
        return int(W * 0.05), int(W * 0.95)
    return max(int(active[0]) - 8, 0), min(int(active[-1]) + 8, W)


# ── Staff-line detection ──────────────────────────────────────────────────────

def _strip_has_long_run(strip: np.ndarray, min_run: int) -> np.ndarray:
    """
    Vectorised: for each row in strip (shape H×W), return a boolean array
    indicating whether that row contains a run of at least `min_run` True values.

    Uses the cumulative-sum sliding-window trick — O(H × (W - min_run + 1)).
    """
    H, W = strip.shape
    if min_run > W:
        return np.zeros(H, dtype=bool)
    cs = np.cumsum(strip.astype(np.int16), axis=1)          # (H, W)
    cs_pad = np.concatenate([np.zeros((H, 1), dtype=np.int16), cs], axis=1)
    result  = np.zeros(H, dtype=bool)
    for j in range(W - min_run + 1):
        result |= (cs_pad[:, j + min_run] - cs_pad[:, j]) == min_run
    return result


def compute_strip_density(norm: np.ndarray, x0: int, x1: int,
                          min_run_divisor: int = 4) -> np.ndarray:
    """
    For each row, compute the fraction of N_STRIPS equal-width vertical strips
    (within [x0, x1]) whose LONGEST dark-pixel run meets a minimum length.

    min_run = max(strip_width // min_run_divisor, 3).  Pass a larger divisor
    (e.g. 8) for a looser second-pass on pages where staff lines are faint.

    Returns a 1-D float array of shape (H,).
    """
    H   = norm.shape[0]
    region = (norm[:, x0:x1] < 100)
    width  = x1 - x0
    sw     = max(width // N_STRIPS, 1)
    min_run = max(sw // min_run_divisor, 3)

    density = np.zeros(H, dtype=float)
    for s in range(N_STRIPS):
        col_s = s * sw
        col_e = min((s + 1) * sw, width)
        strip = region[:, col_s:col_e]
        density += _strip_has_long_run(strip, min_run).astype(float)
    density /= N_STRIPS
    return density


def find_clusters(density: np.ndarray, H: int) -> list[tuple[int, int]]:
    """
    Find contiguous bands of rows whose strip-density ≥ MIN_STRIP_FRAC.
    Adjacent bands within CLUSTER_GAP rows of each other are merged.

    Returns list of (top_y, bot_y) tuples.
    """
    above = density >= MIN_STRIP_FRAC

    raw = []
    in_c = False
    start = 0
    for y in range(H):
        if above[y] and not in_c:
            start, in_c = y, True
        elif not above[y] and in_c:
            raw.append((start, y - 1))
            in_c = False
    if in_c:
        raw.append((start, H - 1))

    # Merge nearby clusters
    if not raw:
        return []
    merged = [raw[0]]
    for c in raw[1:]:
        if c[0] - merged[-1][1] <= CLUSTER_GAP:
            merged[-1] = (merged[-1][0], c[1])
        else:
            merged.append(c)
    return merged


# ── Stave / system grouping ───────────────────────────────────────────────────

def group_into_staves(clusters: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """
    Given staff-line clusters, merge them into complete staves.

    Finds the intra-stave threshold as the midpoint of the LARGEST consecutive
    jump in the sorted gap sequence — the natural break between "staff-line
    spacing" (~11 px at 200 DPI) and "stave-to-stave spacing" (~100 px).
    This is more robust than a fixed percentile when the inter-stave gap
    happens to be close to the percentile boundary.
    """
    if len(clusters) <= 1:
        return list(clusters)

    mids = [(t + b) // 2 for t, b in clusters]
    gaps = [mids[i] - mids[i - 1] for i in range(1, len(mids))]

    sorted_g = sorted(gaps)
    if len(sorted_g) == 1:
        # Single gap: assume it's intra-stave (one system of 2 clusters)
        intra_thr = sorted_g[0] + 1
    else:
        jumps = [sorted_g[i + 1] - sorted_g[i] for i in range(len(sorted_g) - 1)]
        max_j   = max(range(len(jumps)), key=lambda i: jumps[i])
        intra_thr = (sorted_g[max_j] + sorted_g[max_j + 1]) / 2.0

    staves: list[tuple[int, int]] = []
    cur_top, cur_bot = clusters[0]
    for i in range(1, len(clusters)):
        gap = mids[i] - mids[i - 1]
        if gap <= intra_thr:
            cur_bot = clusters[i][1]
        else:
            staves.append((cur_top, cur_bot))
            cur_top, cur_bot = clusters[i]
    staves.append((cur_top, cur_bot))
    return staves


def group_into_systems(
        staves: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """
    Group staves into systems using three complementary passes that together
    cover the full range of layouts found in these vocal-piano PDFs.

    Pass 1 — Absolute threshold (ABS_SYSTEM_GAP):
        Any gap > 180 px is unconditionally an inter-system boundary.
        Handles pages where stave detection was sparse and gaps are large.

    Pass 2 — Largest-jump threshold + local-max filter (MIN_SYSTEM_JUMP=15):
        Find the midpoint of the largest consecutive jump in the sorted gap
        sequence; only flag a gap as inter-system if it is above that midpoint
        AND is a local maximum relative to its neighbours (NEIGHBOR_FACTOR).
        Handles Overture-style pages where the inter-system gap (139 px) is
        clearly the largest and well separated from intra-system gaps (89–107).

    Pass 3 — "Large following small" (tight-layout heuristic):
        After finding the small/large threshold via the largest-jump method,
        classify each gap as small (≤ threshold) or large (> threshold).
        A large gap that IMMEDIATELY FOLLOWS a small gap is inter-system.
        This works because, in 3-stave systems (1 vocal + 2 piano), the
        inter-system gap always follows the smaller piano-treble→bass gap.
        Only activated when 20–50 % of gaps are "small" (the piano-treble→
        bass gaps), which prevents spurious splits on dense Overture pages
        where the threshold does not cleanly separate any sub-group.
    """
    ABS_SYSTEM_GAP  = 180   # Pass 1: unconditional split
    MIN_SYSTEM_JUMP = 15    # Pass 2: minimum sorted-gap jump to apply threshold
    NEIGHBOR_FACTOR = 1.10  # Pass 2: local-max neighbour ratio
    MIN_JUMP_P3     = 5     # Pass 3: minimum jump to compute a useful threshold
    MIN_SMALL_FRAC  = 0.20  # Pass 3: at least this fraction must be "small"
    MAX_SMALL_FRAC  = 0.50  # Pass 3: at most this fraction must be "small"

    if len(staves) <= 1:
        return [list(staves)]

    inter = [staves[i][0] - staves[i - 1][1] for i in range(1, len(staves))]
    if not inter:
        return [list(staves)]

    sep: set[int] = set()

    # Pass 1 — absolute threshold
    for idx, g in enumerate(inter):
        if g > ABS_SYSTEM_GAP:
            sep.add(idx)

    # Compute the shared "small vs large" threshold (used by Passes 2 and 3)
    if len(inter) >= 2:
        sorted_g   = sorted(inter)
        jumps      = [sorted_g[i + 1] - sorted_g[i] for i in range(len(sorted_g) - 1)]
        max_j      = max(range(len(jumps)), key=lambda i: jumps[i])
        best_jump  = jumps[max_j]
        sep_thr    = (sorted_g[max_j] + sorted_g[max_j + 1]) / 2.0

        # Pass 2 — threshold + local-max filter
        if best_jump >= MIN_SYSTEM_JUMP:
            for idx, g in enumerate(inter):
                if g <= sep_thr:
                    continue
                left_ok  = (idx == 0) or (g >= inter[idx - 1] * NEIGHBOR_FACTOR)
                right_ok = (idx == len(inter) - 1) or (g >= inter[idx + 1] * NEIGHBOR_FACTOR)
                if left_ok and right_ok:
                    sep.add(idx)

        # Pass 3 — large-following-small (tight-layout heuristic)
        if best_jump >= MIN_JUMP_P3:
            n_small    = sum(1 for g in inter if g <= sep_thr)
            small_frac = n_small / len(inter)
            if MIN_SMALL_FRAC <= small_frac <= MAX_SMALL_FRAC:
                for idx in range(1, len(inter)):
                    if inter[idx] > sep_thr and inter[idx - 1] <= sep_thr:
                        sep.add(idx)

    systems: list[list[tuple[int, int]]] = []
    curr: list[tuple[int, int]] = [staves[0]]
    for i in range(1, len(staves)):
        if (i - 1) in sep:
            systems.append(curr)
            curr = [staves[i]]
        else:
            curr.append(staves[i])
    systems.append(curr)

    # Pass 4 — recursive local-maximum split for the "all-in-one" failure mode.
    # Applied ONLY when the earlier passes left every stave in a single system
    # (i.e. Passes 1–3 found no system boundary at all).  In that case, use
    # non-edge strict local maxima above the sub-system's median gap to split.
    # This correctly handles Perfect, where all inter-stave gaps are 104–118 px:
    # the true inter-system gaps (112 and 118) are the only non-edge local maxima.
    # We do NOT apply this pass when earlier passes already found boundaries,
    # because that can over-split valid large systems (e.g. Overture page 4).
    MAX_STAVES = 5

    def _split_local_max(grp: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
        if len(grp) <= MAX_STAVES:
            return [grp]
        g = [grp[i][0] - grp[i - 1][1] for i in range(1, len(grp))]
        med = float(np.median(g))
        inner_sep = {i for i in range(1, len(g) - 1)
                     if g[i] > med and g[i] > g[i - 1] and g[i] > g[i + 1]}
        if not inner_sep:
            return [grp]
        sub: list[list[tuple[int, int]]] = []
        cur: list[tuple[int, int]] = [grp[0]]
        for i in range(1, len(grp)):
            if (i - 1) in inner_sep:
                sub.append(cur)
                cur = [grp[i]]
            else:
                cur.append(grp[i])
        sub.append(cur)
        return [s for g2 in sub for s in _split_local_max(g2)]

    if len(systems) == 1 and len(systems[0]) > MAX_STAVES:
        systems = _split_local_max(systems[0])

    return systems


def piano_regions(
        systems: list[list[tuple[int, int]]],
        img_h: int) -> list[tuple[int, int]]:
    """
    For each system return the y-range (with padding) of the piano portion.
    Piano = bottom 2 staves; if a system has ≤ 2 staves it is piano-only.
    """
    regions: list[tuple[int, int]] = []
    for sys_staves in systems:
        if not sys_staves:
            continue
        piano = sys_staves[-2:] if len(sys_staves) > 2 else sys_staves
        y_top = max(piano[0][0] - TOP_PAD_PX, 0)
        y_bot = min(piano[-1][1] + BOT_PAD_PX, img_h - 1)
        regions.append((y_top, y_bot))
    return regions


# ── Bass-clef detection via measure numbers ───────────────────────────────────

def find_piano_pairs(
        norm: np.ndarray,
        staves: list[tuple[int, int]],
        x0: int) -> list[tuple[int, int]]:
    """
    Identify piano grand-staff stave pairs using the measure-number signal.

    Measure numbers always appear directly below the bass-clef (piano left-hand)
    stave and near the left edge of the score.  Treble-clef staves have much
    more dark content directly below them (notes, lyrics, chord symbols).
    The bass stave therefore shows up as a *local minimum* in the per-stave
    "below_dark" count.

    Algorithm
    ---------
    1. For each stave compute below_dark: dark pixels in the rectangle
       [x0, x0+BASS_BELOW_LOOK_W) × [bot+BASS_BELOW_OFFSET, bot+BASS_BELOW_OFFSET+BASS_BELOW_LOOK_H)
    2. Find staves that are local minima AND below_dark < BASS_DARK_THRESHOLD.
       A stave at index i is a local minimum if it is strictly less than its
       right neighbour (i+1) and non-strictly ≤ its left neighbour (i-1).
       For the final stave, "strictly less than left" is the criterion.
    3. Each detected bass stave at index i>0 forms a piano pair with stave[i-1].
       Pairs where the top-to-bottom span exceeds MAX_PIANO_SPAN are discarded
       (they would span multiple systems, which cannot be a piano grand staff).

    Returns a list of (y_top, y_bot) crop regions with padding applied.
    If no valid pairs are found, returns an empty list (caller falls back to
    the system-detection approach).
    """
    if len(staves) < 2:
        return []

    H, W = norm.shape
    x1 = min(x0 + BASS_BELOW_LOOK_W, W)

    def _below_dark(stave_bot: int) -> int:
        y0 = min(stave_bot + BASS_BELOW_OFFSET, H - 1)
        y1 = min(y0 + BASS_BELOW_LOOK_H, H)
        if y0 >= y1 or x0 >= x1:
            return 0
        return int((norm[y0:y1, x0:x1] < 100).sum())

    bd = [_below_dark(b) for _, b in staves]
    n  = len(bd)

    bass_indices: list[int] = []
    for i in range(n):
        v = bd[i]
        if v >= BASS_DARK_THRESHOLD:
            continue
        if i < n - 1:
            # non-last: must be strictly less than right neighbour
            if v < bd[i + 1] and (i == 0 or v <= bd[i - 1]):
                bass_indices.append(i)
        else:
            # last stave: must be strictly less than left neighbour
            if i > 0 and v < bd[i - 1]:
                bass_indices.append(i)

    regions: list[tuple[int, int]] = []
    for i in bass_indices:
        if i == 0:
            continue          # no stave above — can't form a pair
        treble_top, _ = staves[i - 1]
        _, bass_bot   = staves[i]
        if bass_bot - treble_top > MAX_PIANO_SPAN:
            continue          # too far apart — wrong system
        y_top = max(treble_top - TOP_PAD_PX, 0)
        y_bot = min(bass_bot   + BOT_PAD_PX, H - 1)
        regions.append((y_top, y_bot))

    return regions


# ── Per-page processing ───────────────────────────────────────────────────────

def detect_page(page: fitz.Page, dpi: int = ANALYSIS_DPI):
    """
    Run the full detection pipeline for one page.

    Returns (img, (x0, x1), staves, regions, n_systems, piano_only, angle) where
    img is the deskewed detection-DPI render, staves are individual (top, bot)
    bands, regions are the kept piano (top, bot) crop bands (all in detection-DPI
    rows of the deskewed frame), and angle is the deskew rotation in degrees.
    Shared by process_page (output) and the QC contact sheet so both report
    exactly the same kept regions; process_page re-applies `angle` to the
    high-DPI output render so the regions line up.
    """
    # Staff detection (continuous-run detector — see staff_detect_v2 docstring).
    # We render here (not via detect_staves) so the SAME deskew angle drives both
    # detection and the high-DPI output.  Lazy import avoids a circular import
    # (staff_detect_v2 imports this module).
    import staff_detect_v2
    img = render_gray(page, dpi)
    (x0, x1), staves = staff_detect_v2.detect_on_image(img)
    angle = 0.0

    # Deskew rescue: a sub-degree tilt can drop a whole page to 0/blobby staves.
    # Only pay for the angle search when angle-0 detection looks weak (few staves
    # or a merged blob); a clearly-detected page is already horizontal.  Adopt
    # the rotated result only if it actually finds more staves.
    weak = len(staves) < SKEW_RETRY_STAVES or \
        any((b - t) > BLOB_H_PX for t, b in staves)
    if weak:
        a = staff_detect_v2.estimate_skew_angle(img)
        if abs(a) >= staff_detect_v2.SKEW_MIN_APPLY:
            img2 = staff_detect_v2.deskew_image(img, a)
            (x0b, x1b), staves2 = staff_detect_v2.detect_on_image(img2)
            if len(staves2) > len(staves):
                img, x0, x1, staves, angle = img2, x0b, x1b, staves2, a

    norm = normalize_contrast(img)

    if len(staves) < 2:
        return img, (x0, x1), staves, [], 0, True, angle

    # Primary: bass-clef detection via measure-number signal.
    # Bass clef staves are local minima in the "dark pixels just below the stave"
    # profile — measure numbers are small; treble-stave content (notes, lyrics,
    # chord symbols) is dense.  This works regardless of system count or stave
    # spacing and is the most direct implementation of the invariant:
    # "piano grand staff = bass clef stave + the one immediately above it."
    regions = find_piano_pairs(norm, staves, x0)

    # Fallback: if the measure-number signal found nothing (e.g. the piano staves
    # are too faint to detect, or the page has only piano-only systems where the
    # "bottom of the page" has no measure numbers below the last system), fall
    # back to the system-detection approach.
    n_systems  = len(regions)   # reported count comes from pairs found
    piano_only = False
    if not regions:
        systems    = group_into_systems(staves)
        n_systems  = len(systems)
        max_staves = max(len(s) for s in systems) if systems else 0
        piano_only = (max_staves <= 2)
        regions    = piano_regions(systems, img.shape[0])

    return img, (x0, x1), staves, regions, n_systems, piano_only, angle


def process_page(page: fitz.Page, dpi: int = ANALYSIS_DPI):
    """
    Analyse a PDF page and return the cropped piano-only image as a PIL Image.

    Returns
    -------
    pil_out      : PIL.Image  — the (possibly cropped) page image
    removed_pct  : float      — percentage of page height removed
    warnings     : list[str]
    n_systems    : int
    piano_only   : bool       — True when no vocal staves were detected
    """
    warnings: list[str] = []

    import staff_detect_v2
    img, (x0, x1), staves, regions, n_systems, piano_only, angle = detect_page(page, dpi)

    # Output is rendered fresh at OUTPUT_DPI; detection coordinates (200 DPI)
    # are scaled to it by this ratio.  Keeping the page whole means re-rendering
    # the full page at OUTPUT_DPI so even pass-through pages are crisp.  Pages
    # that pass through (no staves/regions) keep their ORIGINAL orientation —
    # the deskew angle is only trustworthy once staves were actually detected.
    out_dpi = OUTPUT_DPI
    scale   = out_dpi / dpi

    def _whole_page():
        return Image.fromarray(render_gray(page, out_dpi), mode='L')

    if len(staves) < 2:
        warnings.append(f"Only {len(staves)} stave(s) detected — keeping full page")
        return _whole_page(), 0.0, warnings, 0, True

    if not regions:
        warnings.append("No piano regions determined — keeping full page")
        return _whole_page(), 0.0, warnings, 0, True

    # Build the output by REDACTING the vocal bands to white on a high-DPI
    # render of the page, instead of stacking down-rastered strips.  The page
    # geometry (and therefore measure alignment) is preserved, and the kept
    # piano grand staves stay at their native scan resolution — crisper notation
    # for PlayScore.  The output is deskewed by the SAME angle as detection so
    # the region rows (200 DPI) scale straight to OUTPUT_DPI.
    out_img = staff_detect_v2.deskew_image(render_gray(page, out_dpi), angle)
    oH, oW  = out_img.shape
    redacted = np.full_like(out_img, 255)          # start all-white
    kept_rows = 0
    for t, b in regions:
        ot = max(int(round(t * scale)), 0)
        ob = min(int(round((b + 1) * scale)), oH)
        if ob > ot:
            redacted[ot:ob, :] = out_img[ot:ob, :]  # copy piano band through
            kept_rows += ob - ot

    if kept_rows == 0:
        warnings.append("All crop regions were empty — keeping full page")
        return _whole_page(), 0.0, warnings, 0, True

    removed_pct = (1.0 - kept_rows / oH) * 100.0
    return Image.fromarray(redacted, mode='L'), removed_pct, warnings, n_systems, piano_only


# ── QC contact sheet ──────────────────────────────────────────────────────────

BLOB_H_PX = 120   # a stave taller than this at detection DPI is a detection-failure "blob"


def page_anomalies(staves: list[tuple[int, int]],
                   regions: list[tuple[int, int]]) -> list[str]:
    """
    Flag detection problems on one page (coordinates at detection DPI):
      • '0-staves'    — nothing detected (skewed scan or title page)
      • 'blob'        — a merged stave taller than BLOB_H_PX (detection failure)
      • 'fat-region'  — a kept region enclosing >2 staves, i.e. a vocal stave
                        slipped into the piano crop (proxy for visual check (c))
    """
    out: list[str] = []
    if len(staves) == 0:
        out.append("0-staves")
    tall = [b - t for t, b in staves if (b - t) > BLOB_H_PX]
    if tall:
        out.append(f"blob(maxH={max(tall)})")
    for rt, rb in regions:
        enclosed = sum(1 for t, b in staves if t >= rt - 2 and b <= rb + 2)
        if enclosed > 2:
            out.append(f"fat-region({enclosed}-staves)")
    return out


def qc_contact_sheet(src: Path, out_png: Path, dpi: int = ANALYSIS_DPI,
                     thumb_w: int = 380, cols: int = 4) -> list[tuple[int, list[str]]]:
    """
    Render every page of `src` as a thumbnail in a grid, drawing detected staves
    in blue and kept piano regions in red, and write one PNG to `out_png`.

    Returns a list of (page_index, anomalies) for pages with any anomaly, so the
    batch runner can report problems without re-detecting.
    """
    doc = fitz.open(str(src))
    thumbs: list[Image.Image] = []
    found: list[tuple[int, list[str]]] = []

    for i in range(len(doc)):
        img, (x0, x1), staves, regions, _, _, _ = detect_page(doc[i], dpi)
        H, W = img.shape
        sc = thumb_w / W
        th = max(int(H * sc), 1)
        t = Image.fromarray(img, mode='L').convert('RGB').resize((thumb_w, th))
        d = ImageDraw.Draw(t)
        xa, xb = x0 * sc, x1 * sc
        for s_t, s_b in staves:                       # detected staves → blue
            d.rectangle([xa, s_t * sc, xb, s_b * sc], outline=(0, 90, 255), width=1)
        for r_t, r_b in regions:                      # kept piano regions → red
            d.rectangle([xa, r_t * sc, xb, r_b * sc], outline=(225, 0, 0), width=2)

        anom = page_anomalies(staves, regions)
        if anom:
            found.append((i, anom))
        label = f"p{i}  staves={len(staves)} regions={len(regions)}"
        if anom:
            label += "  !" + ",".join(anom)
        d.rectangle([0, 0, thumb_w - 1, 14], fill=(0, 0, 0))
        d.text((3, 3), label, fill=(255, 100, 100) if anom else (120, 255, 120))
        thumbs.append(t)
    doc.close()

    if not thumbs:
        return found
    cell_w = thumb_w + 8
    cell_h = max(t.height for t in thumbs) + 8
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * cell_w, rows * cell_h), (235, 235, 235))
    for idx, t in enumerate(thumbs):
        r, c = divmod(idx, cols)
        sheet.paste(t, (c * cell_w + 4, r * cell_h + 4))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(str(out_png))
    return found


# ── File-level processing ─────────────────────────────────────────────────────

def process_file(src: Path, out: Path, dpi: int = ANALYSIS_DPI,
                 preview_png: Path | None = None) -> bool:
    """
    Crop all pages of src and write the result to out.
    Returns True on success.
    """
    doc      = fitz.open(str(src))
    out_doc  = fitz.open()
    any_warn = False

    for pg_idx in range(len(doc)):
        page = doc[pg_idx]
        pil_out, removed_pct, warns, n_sys, piano_only = process_page(page, dpi)

        for w in warns:
            print(f"    [page {pg_idx + 1}] WARNING: {w}", file=sys.stderr)
            any_warn = True

        if pg_idx == 0:
            if piano_only and not warns:
                print(f"  → piano-only page, no cropping ({n_sys} system(s))")
            elif not warns:
                print(f"  → removed top {removed_pct:.0f}% "
                      f"({n_sys} system(s) processed)")

        # Enforce minimum page height so PlayScore can load every page.
        # Short cues (01A, 02A, etc.) and pages where detection produced a tiny
        # sliver both end up well below PlayScore's minimum; pad with whitespace.
        W_px, H_px = pil_out.size          # PIL: (width, height)
        # pil_out is rendered at OUTPUT_DPI (not the detection dpi), so convert
        # pixels→points and size the minimum-height guard against OUTPUT_DPI.
        min_h_px = int(MIN_PAGE_HEIGHT_PT * OUTPUT_DPI / 72)
        if H_px < min_h_px:
            padded = Image.new('L', (W_px, min_h_px), 255)
            padded.paste(pil_out, (0, 0))
            pil_out = padded
            H_px = min_h_px

        W_pt = W_px * 72.0 / OUTPUT_DPI
        H_pt = H_px * 72.0 / OUTPUT_DPI
        out_page = out_doc.new_page(width=W_pt, height=H_pt)

        buf = io.BytesIO()
        pil_out.save(buf, format="PNG", optimize=False)
        buf.seek(0)
        out_page.insert_image(fitz.Rect(0, 0, W_pt, H_pt), stream=buf.getvalue())

        # Save page-1 preview if requested
        if pg_idx == 0 and preview_png is not None:
            pil_out.save(str(preview_png))
            print(f"  → Preview saved: {preview_png}")

    out_doc.save(str(out), garbage=4, deflate=True)
    out_doc.close()
    doc.close()
    return not any_warn


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Crop vocal staves from piano-vocal PDF scores")
    ap.add_argument("--test", action="store_true",
                    help="Process only JLP.piano.00.OVERTURE.pdf and save a "
                         "preview PNG of page 1")
    ap.add_argument("--preview", default="/tmp/overture_cropped_preview.png",
                    metavar="PATH",
                    help="Path for the page-1 preview PNG (test mode only)")
    ap.add_argument("--qc", action="store_true",
                    help="QC mode: write one contact-sheet PNG per cue to "
                         "--qc-dir (staves in blue, kept regions in red) and "
                         "report anomalies. Does NOT write cropped PDFs.")
    ap.add_argument("--qc-dir", default="/tmp/qc", metavar="DIR",
                    help="Directory for QC contact sheets (default /tmp/qc)")
    args = ap.parse_args()

    if args.test:
        files = [SRC_DIR / "JLP.piano.00.OVERTURE.pdf"]
    else:
        files = sorted(SRC_DIR.glob("*.pdf"))

    if args.qc:
        qc_dir = Path(args.qc_dir)
        qc_dir.mkdir(parents=True, exist_ok=True)
        total_anom = 0
        for src in files:
            cue = src.stem
            out_png = qc_dir / f"{cue}.png"
            anomalies = qc_contact_sheet(src, out_png)
            tag = "" if not anomalies else \
                "  ⚠ " + "; ".join(f"p{p}:{','.join(a)}" for p, a in anomalies)
            total_anom += len(anomalies)
            print(f"QC: {cue}.png{tag}")
        print(f"\nDone — {len(files)} contact sheets in {qc_dir}. "
              f"{total_anom} page(s) flagged.")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    for src in files:
        out     = OUT_DIR / src.name
        preview = Path(args.preview) if args.test else None
        print(f"Cropping: {src.name}")
        success = process_file(src, out, preview_png=preview)
        if success:
            ok += 1

    total = len(files)
    print(f"\nDone — {ok}/{total} files processed cleanly. "
          f"Output: {OUT_DIR}")


if __name__ == "__main__":
    main()
