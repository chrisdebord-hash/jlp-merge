"""
jlp_common.py — shared constants and directory paths for the JLP toolkit.
"""

import re
from pathlib import Path

# ─── Source PDFs (read-only) ─────────────────────────────────────────────────
SOURCE_DIR = Path("/Users/chrisdebord/Library/Mobile Documents/com~apple~CloudDocs/FYL/Jagged Little Pill/scores")

# ─── Working directories ─────────────────────────────────────────────────────
EXPORTS_DIR      = Path("/Users/chrisdebord/Library/Mobile Documents/com~apple~CloudDocs/FYL/Jagged Little Pill/exports")
RAW_DIR          = EXPORTS_DIR / "raw"          # user drops XMLs here
PROCESSED_DIR    = EXPORTS_DIR / "processed"    # legacy; superseded by trash/
NEXT_DIR         = EXPORTS_DIR / "next_pdfs"    # chunk PDFs awaiting PlayScore import
MERGED_DIR       = EXPORTS_DIR / "merged"       # per-instrument merged MXLs
ASSEMBLED_DIR    = EXPORTS_DIR / "assembled"    # full-score MXLs per cue
READY_DIR        = EXPORTS_DIR / "ready"        # final Logic-ready MXLs
TRASH_DIR        = EXPORTS_DIR / "trash"        # consumed XMLs and chunk PDFs
TRASH_MERGED_DIR = TRASH_DIR   / "merged"       # chunk PDFs from fully merged cues

STATE_FILE     = EXPORTS_DIR / ".jlp_state.json"
OVERRIDES_FILE = EXPORTS_DIR / "overrides.json"
PUNCHLIST_FILE = EXPORTS_DIR / ".jlp_punchlist.json"
TOTALS_FILE    = EXPORTS_DIR / ".jlp_totals.json"
ANSWERS_FILE   = EXPORTS_DIR / ".jlp_answers.json"

ALL_DIRS = [RAW_DIR, NEXT_DIR, MERGED_DIR, ASSEMBLED_DIR, READY_DIR, TRASH_DIR]

# ─── Instruments ─────────────────────────────────────────────────────────────
# INSTRUMENTS is the pipeline assembly set: every instrument that must be
# present (or accounted for) when assembling a full-score MXL, and the set
# shown in status/punchlist reports.  Percussion is intentionally excluded —
# it is handled separately in Logic Pro and is not part of pipeline assembly.
INSTRUMENTS = [
    "bass", "cello", "guitar1", "guitar2",
    "viola", "violin", "piano",
]

# KNOWN_INSTRUMENTS is the recognition set used when parsing filenames.  It is a
# superset of INSTRUMENTS that also includes percussion, so the check and merge
# phases still process percussion XMLs if they happen to exist in raw/ — they
# just never gate or appear in assembly/status/punchlist.
KNOWN_INSTRUMENTS = INSTRUMENTS + ["percussion"]

# ─── Cue tempo table ─────────────────────────────────────────────────────────
# Each value is one of:
#   None              → no tempo known for this cue
#   <int> bpm         → beats-per-minute at the quarter note (the default unit)
#   (<int> bpm, unit) → beats-per-minute at the given beat unit, e.g. (93, "half")
# Use cue_tempo() to read an entry as a normalized (bpm, beat_unit) pair.
CUE_TEMPOS = {
    "00": (93, "half"),  "01": 100, "01A": 86,  "02": 93,  "02A": 90,  "03": None,
    "03A": None,"03B": 79, "04": 88,  "04A": 104, "05": 84,  "05A": None,
    "06": 84,  "07": 88,  "07A": None,"07B": 87,  "08": 88,  "08A": 103,
    "08B": 44, "08C": 84, "09": None, "09A": None,"09B": None,
    "10": 80,  "11": 90,  "12": 114,  "13": 69,  "14": None, "15": 81,
    "15A": 122,"15B": 110,"16": None, "16A": 64,  "17": 77,  "18": 89,
    "19": 97,  "20": 78,  "21": 109,  "21A": None,"22": 87,  "23": 90,
    "24": None,
}

# ─── Default GM programs (1-indexed, MusicXML spec) ──────────────────────────
GM_DEFAULT = {
    "bass":       34,   # Electric Bass (finger)
    "guitar1":    27,   # Electric Guitar (clean)
    "guitar2":    27,
    "violin":     41,
    "viola":      42,
    "cello":      43,
    "percussion":  0,   # channel 10; program N/A
    "piano":       1,   # Acoustic Grand Piano
}


# Length of each MusicXML beat unit measured in quarter notes.  Used to convert
# a beat-unit BPM into the quarter-note tempo required by the <sound> element.
_BEAT_UNIT_QUARTERS = {
    "whole": 4.0, "half": 2.0, "quarter": 1.0, "eighth": 0.5, "16th": 0.25,
}


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────

def cue_tempo(cue):
    """Return a cue's tempo as a normalized (bpm, beat_unit) pair, or None.

    Accepts every CUE_TEMPOS value form:
      None              → returns None (no tempo known)
      <int>             → (bpm, "quarter")
      (bpm,)            → (bpm, "quarter")
      (bpm, beat_unit)  → (bpm, beat_unit)
    """
    val = CUE_TEMPOS.get(cue)
    if val is None:
        return None
    if isinstance(val, (tuple, list)):
        bpm       = val[0]
        beat_unit = val[1] if len(val) > 1 else "quarter"
    else:
        bpm, beat_unit = val, "quarter"
    return bpm, beat_unit


def sound_tempo(bpm, beat_unit="quarter"):
    """Convert a beat-unit BPM to the quarter-note tempo for <sound tempo=...>.

    MusicXML's <sound tempo> is always expressed in quarter notes, so a half-note
    tempo of 93 becomes 186.  Returns an int when the result is whole.
    """
    q = bpm * _BEAT_UNIT_QUARTERS.get(beat_unit, 1.0)
    return int(q) if q == int(q) else q


def ensure_exports_dir():
    """Create all working directories if they don't exist."""
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


def resolve_output(name: str) -> Path:
    """Route a bare filename to EXPORTS_DIR; otherwise treat as a full path."""
    p = Path(name)
    if p.parent == Path("."):
        return EXPORTS_DIR / p
    return p


def find_source_pdf(instrument: str, cue: str) -> "Path | None":
    """
    Return the first PDF matching JLP.{instrument}.{cue}.*.pdf in SOURCE_DIR/{instrument}/.
    """
    folder = SOURCE_DIR / instrument
    if not folder.exists():
        return None
    pat = re.compile(
        r"^JLP\." + re.escape(instrument) + r"\." + re.escape(cue) + r"\.",
        re.IGNORECASE,
    )
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() == ".pdf" and pat.match(f.name):
            return f
    return None
