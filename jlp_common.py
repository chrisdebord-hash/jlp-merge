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
INSTRUMENTS = [
    "bass", "cello", "guitar1", "guitar2",
    "percussion", "viola", "violin", "piano",
]

# ─── Cue tempo table ─────────────────────────────────────────────────────────
CUE_TEMPOS = {
    "00": 93,  "01": 100, "01A": 86,  "02": 93,  "02A": 90,  "03": None,
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


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────

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
