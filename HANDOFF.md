# JLP piano-crop pipeline — handoff

Tooling that isolates the **piano grand staff** from scanned piano-vocal scores
of *Jagged Little Pill* so PlayScore (OMR) reads only the piano, not the vocal
lines. Output feeds the canned-backing-track build. Show opens **Aug 9 2026**.

Two files:

- `crop_piano_pdfs.py` — pipeline: detect staves → keep the {-**braced** piano
  grand staves → write high-res piano-only PDFs; also a QC contact-sheet mode.
- `staff_detect_v2.py` — the staff detector + deskew (read its module docstring
  for the detection rationale).

**The piano rule (ground truth):** a piano region is a `{`-braced grand staff —
exactly two adjacent staves joined by a brace. A region is emitted ONLY for a
braced pair; never for an unbraced pair. See *Brace detection* below.

---

## Diagnosis (what was broken)

The original detector (`compute_strip_density → find_clusters →
group_into_staves`) flagged a row as a staff line when enough **short** dark
runs appeared across 24 vertical strips. On dense / lyric-heavy pages the notes
fill the vertical gaps, so an entire system reads as one solid dark band and is
detected as a **single ~350 px "stave" (a blob)**. The bottom-two-staves =
piano logic then can't find the piano inside the blob, so the page keeps the
**whole system, vocals included** — the crop does nothing. Roughly half the
pages of a song cropped correctly, half passed through with vocals intact.
Validated failure: Hand In My Pocket pages 3–7.

## The fix (continuous-run staff detection)

A real staff line is the one thing on the page that is a **single continuous
horizontal run** spanning most of the score width — notes, stems, lyrics, and
slurs never are. `staff_detect_v2`:

1. Computes the **longest continuous horizontal dark run per row** (closing
   ≤2 px scan gaps first).
2. Marks a row as a staff line iff that run ≥ `LINE_RUN_FRAC × score_width`
   (skew-tolerant).
3. Groups line-rows into 5-line staves using the **measured** median line
   spacing (not gap-jump guessing).
4. Hands clean individual staves to the **unchanged** `find_piano_pairs`
   (bass-clef-via-measure-number signal), which is reliable once fed clean
   staves.

Each detected stave is now ~46 px tall; the old blobs (~350 px) are gone.

## Deskew addition (closes the 0-staves gap)

A **sub-degree** tilt is enough to break the continuous-run test: a flat pixel
row only stays inside a thin tilted line for ~350 px before drifting off, so the
longest run falls under the cutoff and the **whole page detects 0 staves**
(e.g. All I Really Want p0/p6/p8/p10/p18 — max run 389 px vs a 404 px
threshold).

Fix: before accepting a page, estimate its dominant staff-line tilt and rotate
it horizontal. The angle is scored by the **detection metric itself** — the
number of rows whose longest run clears the threshold — searched coarse (0.5°)
then fine (0.1°) at full resolution. A plain dark-pixel-variance score does
**not** work here: note/lyric content dominates the variance and peaks at 0°,
while the true skew peak can be as narrow as ±0.2°; the staff-row count instead
jumps from ~2 to ~100 at the right angle.

Deskew is applied as a **rescue**: only attempted when flat detection is weak
(`< SKEW_RETRY_STAVES` staves or a >120 px blob), adopted only if it finds more
staves, and only if it clearly beats the flat baseline (so straight pages are
never perturbed). The **same angle is re-applied to the high-DPI output render**
so the kept regions land in the right place. Anything still undetected falls
back to keeping the whole page (no worse than before).

## High-res output

Source scans are ~600 DPI (5100×6599). Detection runs at **200 DPI** (the
`find_piano_pairs` pixel windows are tuned for it), but output is built by
rendering the page at **300 DPI** (`OUTPUT_DPI`) and **redacting the vocal
bands to white**, keeping full page geometry — instead of stacking
down-rastered strips. This preserves measure alignment and keeps the piano
notation crisp for OMR. Detection rows (200 DPI) are scaled to the output by the
DPI ratio.

## Brace detection (the real piano rule)

The old way of picking the piano — "keep the bottom two staves of each system,
confirmed by a measure-number signal under the bass clef" (`find_piano_pairs` /
`group_into_systems`) — **misfired**: it kept braceless pairs (Overture p6/idx5
system 1 grabbed a vocal pair) and under-detected when a piano stave was too
faint to detect (Overture p3/idx2 found only 1 of 2 systems). The real
invariant is the engraving itself: the piano is the grand staff marked by a
curved `{` **brace**, distinct from the full-system **bracket** (spans all
staves of a system, straight) and from **barlines** (straight).

`find_piano_braces` (in `crop_piano_pdfs.py`) detects the brace by its left-edge
**cusp**:

1. In a window just left of the score — `[x0-40, x0+140]`, wide enough to absorb
   per-system **indentation** (each system can start at a different x; the brace
   is NOT a fixed offset from the global `x0`) — take the **leftmost-ink column**
   of every row across the candidate grand-staff rows. In the piano region the
   brace is the leftmost vertical structure: the full-system bracket lives in the
   vocal rows *above* the piano, never inside it.
2. A brace makes that profile dip to its **minimum at the vertical midpoint**
   (bulges LEFT) and sit further right at the top and bottom tips (it tapers to
   points). A straight bracket or barline gives a flat profile. So:
   - `taper`  = px the centre cusp sits left of the tips → **≥ 4** for a brace,
     ~0 or negative for a bracket/barline.
   - `argmin` = normalized row of the leftmost point → must be **central**
     (≈0.5), i.e. the grand-staff midpoint.
   - `cov`    = fraction of rows carrying left-edge ink → structure continuity.
3. **Primary pass:** every adjacent stave pair whose `grandstaff_cusp` clears
   those gates is a piano region. Unbraced pairs are dropped.
4. **Missing-stave rescue:** when one piano stave is too faint for the staff
   detector but the brace still proves the grand staff is there (Overture p3
   system 2 — the bass stave's longest run is 349 px vs the 405 px staff-line
   cutoff), an uncovered stave next to an empty gap is re-tested over an extended
   region (one grand-staff height) with a **stricter** cusp; if it braces, the
   region is kept. This never invents a region under a vocal stave.

**Dead ends (don't retry):** plain "any ink in the gap near `x0`" (confounded by
barlines/clefs/time-sigs); a fixed narrow band at a constant offset from the
global `x0` (breaks on per-system indentation — e.g. HIMP p1's top system starts
~100 px right of the page `x0`); vertical-opening the ink to isolate the brace
(erases the cusp, which is the whole signal). The corroborating signals
(tighter grand-staff gap, bass clef below) are NOT reliable on their own here —
the brace is primary.

`find_piano_pairs` / `group_into_systems` / `piano_regions` remain in the module
for reference but are no longer on the detection path.

---

## Validated parameters

Detection (`staff_detect_v2.py`):

| param | value | meaning |
|---|---|---|
| `DET_DPI` | 200 | detection render DPI |
| `LINE_RUN_FRAC` | 0.28 | row is a staff line if longest run ≥ this × score width |
| `DARK_THRESH` | 170 | < this (on contrast-normalized image) counts as ink |
| `STAVE_GAP_K` | 1.8 | new stave when line gap > K × median line spacing |
| `MIN_STAVE_K` | 2.0 | drop "staves" shorter than K × median line spacing |
| `SKEW_MAX_DEG` | 4.0 | deskew search range ±deg |
| `SKEW_MIN_GAIN_ROWS` | 20 | adopt a tilt only if it adds ≥ this many staff-rows |

Pipeline (`crop_piano_pdfs.py`):

| param | value | meaning |
|---|---|---|
| `ANALYSIS_DPI` | 200 | detection DPI |
| `OUTPUT_DPI` | 300 | exported page DPI |
| `SKEW_RETRY_STAVES` | 6 | below this many staves, attempt the deskew rescue |
| `BLOB_H_PX` | 120 | stave taller than this = detection-failure blob |

Brace detection (`crop_piano_pdfs.py`):

| param | value | meaning |
|---|---|---|
| `BRACE_DARK` | 170 | < this on the normalized image counts as ink |
| `BRACE_WIN_L` / `BRACE_WIN_R` | 40 / 140 | left-edge window `[x0-40, x0+140]` (absorbs per-system indent) |
| `BRACE_MIN_CUSP` | 4.0 | min cusp taper (px) for a primary braced pair |
| `BRACE_ARGMIN_LO/HI` | 0.28 / 0.72 | the cusp (leftmost) row must lie in this central band |
| `BRACE_MIN_COV` | 0.70 | min fraction of region rows carrying left-edge ink |
| `BRACE_RESCUE_CUSP` | 6.0 | stricter taper for the missing-stave rescue |
| `BRACE_RESCUE_LO/HI` | 0.30 / 0.70 | stricter central band for the rescue |

**Brace detection is the piano signal — see *Brace detection* above.**
`find_piano_pairs` (bass-clef-via-measure-number) and `group_into_systems` are
kept for reference but are OFF the detection path; the brace rule supersedes the
bottom-two-staves heuristic, which misfired.

---

## How to run

```bash
# Per-page detection smoke test for one cue
python3 staff_detect_v2.py /Users/chrisdebord/JLP.piano.03.Hand_In_My_Pocket.pdf

# Crop one cue (test): OVERTURE → writes cropped PDF + a page-1 preview PNG
python3 crop_piano_pdfs.py --test

# Crop ALL cues  (SRC_DIR → OUT_DIR, both in iCloud — see top of crop_piano_pdfs.py)
python3 crop_piano_pdfs.py

# QC contact sheets — one PNG per cue, staves blue, kept piano regions red,
# anomalies printed. Eyeball all 42 fast. Writes /tmp/qc/<cue>.png (no PDFs).
python3 crop_piano_pdfs.py --qc
```

**Source set:** 42 cue PDFs exist in two byte-different but structurally
identical copies — `/Users/chrisdebord/JLP.piano.*.pdf` (used by the smoke
tests) and the iCloud `SRC_DIR` (`…/scores/piano/`, used by the pipeline).
Same page counts and same 5100×6599 embedded images, so detection is unaffected
either way. **iCloud `SRC_DIR` is canonical** for full runs; output goes to
`OUT_DIR` (`…/scores/piano_cropped/`).

---

## Last full-run results (42 cues, all pages — brace detection)

- **Named failures fixed:** Overture p3 (idx2) now 2 regions (was 1 — the faint
  second-system grand staff is recovered by the missing-stave rescue); Overture
  p6 (idx5) now 3 regions, all on braced pairs (the braceless vocal pair the old
  heuristic grabbed is gone).
- **Hand In My Pocket:** all 8 pages, 3 braced regions each. ✓
- **No fat-regions:** no kept region encloses a 3rd (vocal) stave, across all 42
  cues. ✓ (Every emitted region sits on a braced grand staff.)
- **Recall:** only one 0-region page across all cues — You Oughta Know p8, a
  near-empty staging page (just a stage-direction arrow), correctly passed
  through whole. Spot-checked count=4 pages (Perfect, Mary Jane, Bows, All I
  Really Want, New York, Head Over Feet) and count=1 pages: all correct — the 1s
  are genuine single-system pages.

### Known residuals (upstream STAFF detection, not the brace rule)

- **No p7** keeps only the first of its two systems: the bottom system's staves
  aren't detected (only a 22 px fragment), so there is nothing for the brace
  logic to confirm. The old code missed it too. A staff-detection gap, not a
  brace misfire.
- **All I Really Want p8** keeps only 1 of its 2 piano systems for the same
  reason — the second system's bass stave isn't cleanly detected on the slightly
  warped page. Worth a manual check of these pages before final use.
  These are limits of `staff_detect_v2`; the brace rule is correct on every
  stave pair it is actually fed.

### QC verdict

Run `python3 crop_piano_pdfs.py --qc` and open `/tmp/qc/`. The contact sheets
draw every detected stave (blue) and every kept piano region (red); each red box
is a `{`-braced grand staff. A correct page shows red boxes only on the braced
piano pair of each system (region count == brace count), blue-only on the vocal
staves above. Anomalies (0-staves, blob, fat-region) are printed to stdout and
labelled red on the thumbnail.
