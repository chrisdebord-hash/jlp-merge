"""
jlp_common.py — shared constants and path utilities for the JLP toolkit.
"""

from pathlib import Path

SOURCE_DIR  = Path("/Users/chrisdebord/Google Drive/My Drive/JLP")
EXPORTS_DIR = Path.home() / "JLP_exports"

INSTRUMENTS = [
    "bass", "cello", "guitar1", "guitar2",
    "percussion", "viola", "violin", "piano",
]

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

# Default GM programs (1-indexed per MusicXML spec)
GM_DEFAULT = {
    "bass":       34,   # Electric Bass (finger)
    "guitar1":    27,   # Electric Guitar (clean)
    "guitar2":    27,
    "violin":     41,
    "viola":      42,
    "cello":      43,
    "percussion":  0,   # channel 10; program meaningless
    "piano":       1,   # Acoustic Grand Piano
}


def find_source_pdf(instrument: str, cue: str) -> "Path | None":
    """
    Search SOURCE_DIR/{instrument}/ for JLP.{instrument}.{cue}.*.pdf
    Returns the first match, or None.
    """
    folder = SOURCE_DIR / instrument
    if not folder.exists():
        return None
    prefix = f"JLP.{instrument}.{cue}.".lower()
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() == ".pdf" and p.name.lower().startswith(prefix):
            return p
    return None


def ensure_exports_dir() -> Path:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return EXPORTS_DIR


def resolve_output(filename: str) -> Path:
    """
    If filename has no directory component, put it under EXPORTS_DIR.
    Otherwise use the path as given.
    """
    p = Path(filename)
    if p.parent == Path("."):
        ensure_exports_dir()
        return EXPORTS_DIR / p.name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
