# JLP Toolkit

Single-entry pipeline (`jlp_pipeline.py`) plus legacy per-operation scripts for the JLP pit-orchestra MusicXML workflow: PlayScore exports → merged MXL → Logic Pro multi-track session.

## Install

### 1. Install tesseract (required for jlp_next.py OCR)

```bash
brew install tesseract
```

### 2. Install Python dependencies

```bash
pip3 install -r requirements.txt
```

> If your system Python blocks pip (`PEP 668`):
> ```bash
> pip3 install -r requirements.txt --break-system-packages
> ```

---

## Scripts

### `jlp_next.py` — Find remaining PDF pages after a PlayScore cutoff

When PlayScore stops exporting mid-score, this script uses OCR to locate the
cutoff measure in the source PDF and outputs the remaining pages as a new PDF
chunk ready for the next PlayScore import.

```bash
python jlp_next.py --instrument bass --cue 01 --xml bass_01_part1.xml
```

If OCR fails to locate the measure, pass `--page N` to specify manually:

```bash
python jlp_next.py --instrument bass --cue 01 --xml bass_01_part1.xml --page 14
```

**Output:** `bass_01_part2.pdf` (auto-increments to part3, part4, …)

**Options:**
| Flag | Description |
|------|-------------|
| `--instrument` | Instrument name (bass / cello / guitar1 / guitar2 / percussion / viola / violin / piano) |
| `--cue` | Cue identifier, e.g. `01`, `01A`, `09B` |
| `--xml` | PlayScore XML export that cut off |
| `--page N` | Skip OCR; use page N as the cutoff (1-based) |
| `--output-dir` | Directory for the output PDF (default: current directory) |

---

### `jlp_merge.py` — Merge exports and assemble full score

#### Merge mode — combine XMLs for one instrument into a single MXL

```bash
python jlp_merge.py --mode merge --instrument bass --cue 01 \
    part1.xml part2.xml part3.xml --output JLP.bass.01.mxl
```

What it does:
- Renumbers measures sequentially across all inputs
- Detects octave errors (median-pitch method) and shifts if needed
- Scans text for instrument switches (`ELECTRIC`, `ACOUSTIC`, `PIZZ`, `ARCO`, etc.)
  and injects MIDI program-change directions
- Injects cue tempo at m1 from the built-in table if the source lacks one
- Flags measures with unresolved durations (`type=?`) as warnings
- Writes a compressed `.mxl` file to the exports directory

#### Assemble mode — combine per-instrument MXLs into a full score

```bash
python jlp_merge.py --mode assemble --cue 01 \
    JLP.bass.01.mxl JLP.guitar1.01.mxl JLP.piano.01.mxl \
    --output JLP.01.full.mxl
```

What it does:
- Pads shorter parts with full-measure rests to align the measure grid
- Names each part by instrument
- Embeds General MIDI program numbers (channel 10 for percussion)
- Preserves program-change directions injected during merge

**GM program numbers used:**

| Instrument | Default GM | Electric | Acoustic |
|------------|-----------|----------|---------|
| bass | 34 (E. Bass) | 34 | 33 |
| guitar1/2 | 27 (E. Guitar) | 27 | 25 |
| violin | 41 | — | — |
| viola | 42 | — | — |
| cello | 43 | — | — |
| percussion | ch. 10 | — | — |
| piano | 1 | — | — |

**Mid-cue switch patterns detected:**

| Text in score | Effect |
|---------------|--------|
| `ELECTRIC` / `ELEC` | bass → GM 34, guitar → GM 27 |
| `ACOUSTIC` / `ACOUS` / `STEEL STRING` | bass → GM 33, guitar → GM 25 |
| `PIZZ` / `PIZZICATO` | violin → 46, viola → 45, cello → 44 |
| `ARCO` | strings → restore default |
| `SUL PONT` | strings → GM 49 (string ensemble) |

---

### `jlp_status.py` — Dashboard showing export progress

```bash
python jlp_status.py                   # show all instruments and cues
python jlp_status.py --cue 01          # filter to cue 01
python jlp_status.py --instrument bass # filter to bass
```

Shows a matrix of: PDF exists / XML parts exported / MXL merged / full score assembled.
Highlights anything that has a source PDF but no merged MXL yet.

---

## Directory layout

Base: `~/Library/Mobile Documents/com~apple~CloudDocs/FYL/Jagged Little Pill/`

| Location | Purpose |
|----------|---------|
| `scores/{instrument}/` | Source PDFs (read-only) |
| `exports/raw/` | Drop PlayScore XML exports here |
| `exports/next_pdfs/` | Script writes next-chunk PDFs here |
| `exports/merged/` | Per-instrument merged MXLs |
| `exports/assembled/` | Full-score MXLs per cue |
| `exports/ready/` | Final Logic-ready MXLs |

**File naming convention:**

| File | Pattern |
|------|---------|
| Source PDF | `JLP.{inst}.{cue}.{title}.pdf` |
| First PlayScore export | `JLP.{inst}.{cue}.{title}.xml` |
| Second export (if needed) | `JLP.{inst}.{cue}.{title}.b.xml` |
| Third export | `JLP.{inst}.{cue}.{title}.c.xml` |
| Next-chunk PDF (auto-generated) | `JLP.{inst}.{cue}.{title}.b.pdf` |
| Merged per-instrument MXL | `JLP.{inst}.{cue}.{title}.mxl` |
| Full score MXL | `JLP.{cue}.{title}.full.mxl` |

---

## Cue tempo table

| Cue | BPM | Cue | BPM | Cue | BPM |
|-----|-----|-----|-----|-----|-----|
| 00 | 93 | 08 | 88 | 15 | 81 |
| 01 | 100 | 08A | 103 | 15A | 122 |
| 01A | 86 | 08B | 44 | 15B | 110 |
| 02 | 93 | 08C | 84 | 16A | 64 |
| 02A | 90 | 10 | 80 | 17 | 77 |
| 03B | 79 | 11 | 90 | 18 | 89 |
| 04 | 88 | 12 | 114 | 19 | 97 |
| 04A | 104 | 13 | 69 | 20 | 78 |
| 05 | 84 | — | — | 21 | 109 |
| 06 | 84 | — | — | 22 | 87 |
| 07 | 88 | — | — | 23 | 90 |
| 07B | 87 | — | — | — | — |

Cues 03, 03A, 05A, 07A, 09, 09A, 09B, 14, 16, 21A, 24 have no fixed tempo — a warning is printed and no tempo is injected.
