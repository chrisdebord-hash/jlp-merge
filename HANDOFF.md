# JLP piano-crop pipeline — handoff

Tooling that isolates the **piano grand staff** from scanned piano-vocal scores
of *Jagged Little Pill* so PlayScore (OMR) reads only the piano, not the vocal
lines. Output feeds the canned-backing-track build. Show opens **Aug 9 2026**.

Two files:

- `crop_piano_pdfs.py` — pipeline: detect staves → keep the piano grand staves →
  write high-res piano-only PDFs; also a QC contact-sheet mode.
- `staff_detect_v2.py` — the staff detector + deskew + the **structural** piano
  rule (`find_piano_systems`).

**The piano rule (ground truth, from the music director):** every system is
vocal stave(s) on top + a 2-stave piano grand staff on the bottom, and the
bottom 2 staves of any page are always piano. Systems are split by the LEFT-EDGE
connector (vertical brace/barline ink bridges staves within a system; the margin
is blank between systems). So: segment staves into systems by the left edge, then
the bottom 2 staves of each system are the piano region. See *Structural piano
detection* below. (This superseded an earlier brace-pixel detector and a
measure-number bass detector, both kept for reference only.)

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

## Structural piano detection (the piano rule)

The piano is picked by the score's STRUCTURE, not by reading any one glyph.
`find_piano_systems` (in `staff_detect_v2.py`):

1. **Segment staves into systems by the LEFT-EDGE connector**
   (`group_into_systems_by_edge`). For each adjacent stave gap, look in a band at
   the score's left edge — `[x0-20, x0+150]`, wide on the right so an indented
   system's connector still falls inside it — and take the **max fraction of the
   gap's rows inked by any single column**. A brace/barline bridging the gap
   gives ~**1.0** (a full vertical line) ⇒ *same system*; a clean between-system
   margin gives ~**0.1–0.35** (only stray measure-number / tempo marks, which
   never span the gap vertically) ⇒ *boundary*. A gap larger than `200 px` is a
   boundary regardless (a faint missing stave can blow a within-system gap past
   any plausible size; the bass is recovered in step 3).
   - This is a system-**BOUNDARY** test, not a piano-pair test. Measuring "ink in
     the gap" as a piano-PAIR signal is a dead end (every within-system gap —
     vocal-vocal, vocal-piano, piano-piano — is barline-bridged). The tighter
     grand-staff gap (~93–95 px on HIMP) is only a weak corroborator and does NOT
     generalise (Perfect's grand-staff gaps are ~105–108).
2. **Bottom 2 staves of each system = the piano region.** Guaranteed by the
   layout: vocal(s) on top, piano grand staff on the bottom.
3. **Faint-bass rescue.** A piano bass whose staff lines fall just under the
   `0.28·w` staff-detection cutoff (Overture p3, Ironic p11, AIRW p9, Wake Up
   p16 — 4 pages) leaves the detected bottom stave as a lone treble. If a
   sub-threshold staff-line cluster (longest run in `[0.16·w, 0.28·w)`, ≥10 rows)
   sits within one grand-staff drop below a system's bottom stave, that stave is
   the treble and the region is extended down to cover the recovered bass.
4. **Sanity flags** (logged for QC, not fatal): `rescued-bass`, `lone-stave`
   (a 1-stave "system" — a stray cue line or a system whose other staves are
   undetected), `bottom-anchor-miss` (the page's bottom stave didn't land in any
   region — the left-edge segmentation or staff detection failed). These surface
   the handful of pages worth an eyeball; they do NOT force a wrong crop.

**Instrumental / irregular cues** (e.g. the Overture) were the worry, but with
the faint-bass rescue they now segment correctly; anything that still doesn't fit
trips a sanity flag for manual review rather than being force-cropped.

**Dead ends (don't retry):** a fixed narrow band at a constant offset from the
global `x0` (breaks on per-system **indentation** — each system can start at a
different x; HIMP p1's top system starts ~100 px right of the page `x0`); reading
the brace pixels themselves (the earlier `find_piano_braces` — works, but
over-detected vocal pairs on e.g. Forgiven p5 / Mary Jane p0 and under-detected
on So Unsexy p4 where the structural rule is correct).

`find_piano_braces` (brace-pixel cusp), `find_piano_pairs` (measure-number bass)
and `group_into_systems` remain in the modules for reference but are OFF the
detection path.

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

Structural piano detection (`staff_detect_v2.py`):

| param | value | meaning |
|---|---|---|
| `LEFTEDGE_BAND_L` / `LEFTEDGE_BAND_R` | 20 / 150 | left-edge connector band `[x0-20, x0+150]` (absorbs per-system indent) |
| `LEFTEDGE_CONN_THR` | 0.55 | gap-column inked ≥ this fraction ⇒ staves bridged (same system) |
| `LEFTEDGE_BIG_GAP` | 200 | a gap this large is a system boundary regardless of connector |
| `FAINTBASS_RUN_LO` | 0.16 | faint bass staff line: longest run in `[0.16·w, 0.28·w)` |
| `FAINTBASS_MIN_ROWS` | 10 | this many sub-threshold staff-line rows below a stave ⇒ a faint stave |
| `FAINTBASS_MAX_K` | 3.0 | search at most this many stave-heights below the treble |

**The structural rule is the piano signal — see *Structural piano detection*
above.** `find_piano_braces`, `find_piano_pairs` and `group_into_systems` are
kept for reference but are OFF the detection path.

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

## Last full-run results (42 cues, all pages — structural rule)

- **Named failures fixed:** Overture p3 (idx2) → 2 regions (system 2's faint
  bass is recovered by the faint-bass rescue, so the crop covers treble+bass, not
  vocal+treble); Overture p6 (idx5) → 3 regions, each the bottom-2 of a system.
- **Hand In My Pocket:** all 8 pages, 3 piano regions each. ✓
- **No fat-regions:** no kept region encloses a 3rd (vocal) stave, across all 42
  cues. ✓
- **No silent merges:** every within-system gap >155 px is barline-bridged
  (conn ≈ 1.0) — i.e. genuinely within a system, not two systems joined by a
  stray column. No system boundary was missed without a flag.
- **Recall:** every page with ≥2 staves yields ≥1 region (the only 0-region page
  is You Oughta Know p8, a near-empty staging page — correct pass-through).
- **Faint-bass rescues (4):** Overture idx2, Ironic idx10, All I Really Want
  idx8, Wake Up idx15 — each recovers a sub-threshold piano bass so the grand
  staff is whole (page indices are 0-based, as labelled on the QC thumbnails).
  This also closes the old All I Really Want idx8 residual.
- **More accurate than the prior brace-pixel detector** on the pages where they
  disagree: Forgiven p5 (structural 2 vs brace's 4 — brace over-fired on vocal
  pairs), Mary Jane p0 (3 vs 4), So Unsexy p4 (3 vs 2 — brace missed a system).

### Flagged for manual review (8 pages — logged by `--qc`, not forced)

- **`rescued-bass`** (Overture p3, Ironic p11, AIRW p9, Wake Up p16): a faint
  bass was recovered — eyeball that the crop bottom lands below the bass.
- **`lone-stave`** (Dear God idx1, Unprodigal Daughter idx5, You Oughta Know
  idx9): a 1-stave "system" isolated by a clear blank gap (a stray cue line /
  intro stave) — correctly produces no region, flagged so you can confirm nothing
  real was dropped.
- **`lone-stave` + `bottom-anchor-miss`** (No idx7): the page's bottom system is
  largely undetected (only a 22 px fragment), so its piano isn't cropped. An
  upstream `staff_detect_v2` gap, surfaced for a manual crop.

### QC verdict

Run `python3 crop_piano_pdfs.py --qc` and open `/tmp/qc/`. The contact sheets
draw every detected stave (blue) and every kept piano region (red). A correct
page shows red boxes only on the bottom (piano) pair of each system, blue-only on
the vocal staves above; region count == system count. Anomalies and structural
flags (`rescued-bass`, `lone-stave`, `bottom-anchor-miss`, `0-staves`,
`fat-region`) are printed to stdout and labelled red on the thumbnail.
