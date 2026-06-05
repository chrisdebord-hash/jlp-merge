# JLP piano-crop pipeline — handoff

Tooling that isolates the **piano grand staff** from scanned piano-vocal scores
of *Jagged Little Pill* so PlayScore (OMR) reads only the piano, not the vocal
lines. Output feeds the canned-backing-track build. Show opens **Aug 9 2026**.

Two files:

- `crop_piano_pdfs.py` — pipeline: detect → keep piano regions → write
  high-res piano-only PDFs; also a QC contact-sheet mode.
- `staff_detect_v2.py` — the staff detector + deskew (read its module docstring
  for the detection rationale).

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

**Do NOT modify** `find_piano_pairs` / the bass-clef-via-measure-number logic —
it is correct once fed clean staves.

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

## Last full-run results (42 cues, all pages)

- **Hand In My Pocket:** all 8 pages, 9–10 staves each, 3 regions each, no blobs. ✓
- **No blobs anywhere:** no stave > 120 px at 200 DPI across all 42 cues. ✓
- **No fat-regions:** no kept region encloses a 3rd (vocal) stave. ✓
- **Deskew rescues:** All I Really Want p0/p6/p8/p10/p18 (was 0 staves) now
  detect; HIMP and other straight pages unchanged (angle 0°).
- **One legitimate 0-staves page:** You Oughta Know p8 is a near-empty staging
  page (just a stage-direction arrow) — correctly passes through whole.

### Known residual

- **All I Really Want p8** keeps only 1 of its 2 piano systems: the second
  system's bass stave isn't cleanly detected after deskew (page is slightly
  warped, not a uniform tilt), so `find_piano_pairs` can't form the second pair.
  Detection succeeds (9 staves, no blob), but one piano system is dropped rather
  than kept. Not fixable without touching the (intentionally frozen) pairing
  logic. Worth a manual check of that single page before final use.

### QC verdict

Run `python3 crop_piano_pdfs.py --qc` and open `/tmp/qc/`. The contact sheets
draw every detected stave (blue) and every kept piano region (red). A correct
page shows red boxes only on the bottom pair of each system, blue-only on the
vocal staves above. Anomalies (0-staves, blob, fat-region) are printed to stdout
and labelled red on the thumbnail.
