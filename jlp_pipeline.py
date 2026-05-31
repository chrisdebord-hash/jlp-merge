#!/usr/bin/env python3
"""
jlp_pipeline.py — single entry point for the JLP pit-orchestra MusicXML pipeline.

Phases:
  check     Scan raw/ XMLs vs source PDFs; generate next-chunk PDFs
  merge     Merge complete raw/ XML sets into per-instrument MXLs
  assemble  Combine per-instrument MXLs into full-score MXLs
  status    Print progress table (44 cues × 8 instruments)

Usage:
  python jlp_pipeline.py --phase check
  python jlp_pipeline.py --phase merge
  python jlp_pipeline.py --phase assemble
  python jlp_pipeline.py --phase status
  python jlp_pipeline.py --phase check --cue 01 --instrument bass
  python jlp_pipeline.py --phase check --force   # re-scan even if cached
"""

import argparse
import io
import json
import re
import shutil
import sys
import zipfile
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path
from statistics import median as _median

from jlp_common import (
    SOURCE_DIR, EXPORTS_DIR, RAW_DIR, NEXT_DIR,
    MERGED_DIR, ASSEMBLED_DIR, TRASH_DIR, TRASH_MERGED_DIR,
    STATE_FILE, OVERRIDES_FILE, PUNCHLIST_FILE, TOTALS_FILE, ANSWERS_FILE,
    ALL_DIRS, INSTRUMENTS, CUE_TEMPOS, GM_DEFAULT,
)

try:
    import fitz
    from PIL import Image
    import pytesseract
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False

OCR_DPI = 200


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_STEP_MIDI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# Navigation/marker pages that carry no musical content and should be skipped
# when building chunk PDFs.  Matched against fitz-extracted text (case-insensitive).
_NAV_PATTERN = re.compile(
    r'\bv\.?s\.?\b'         # V.S. / VS  (Volti Subito — turn page quickly)
    r'|\bvolti\s+subito\b'  # full spelling
    r'|\btacet\b',          # tacet — instrument is silent for the movement
    re.IGNORECASE,
)

OCTAVE_CEILINGS = {
    "bass":   52,   # E3
    "cello":  72,   # C5
    "viola":  72,
    "violin": 72,
}

_SINGLE_STAFF = {"bass", "cello", "guitar1", "guitar2", "viola", "violin", "percussion"}

SWITCH_PATTERNS = [
    ("STEEL STRING", {"guitar1": 25, "guitar2": 25}),
    ("PIZZICATO",    {"violin": 46, "viola": 45, "cello": 44}),
    ("SUL PONT",     {"violin": 49, "viola": 49, "cello": 49}),
    ("ACOUSTIC",     {"bass": 33, "guitar1": 25, "guitar2": 25}),
    ("ELECTRIC",     {"bass": 34, "guitar1": 27, "guitar2": 27}),
    ("ACOUS",        {"bass": 33, "guitar1": 25, "guitar2": 25}),
    ("ELEC",         {"bass": 34, "guitar1": 27, "guitar2": 27}),
    ("ARCO",         {"violin": 41, "viola": 42, "cello": 43}),
    ("PIZZ",         {"violin": 46, "viola": 45, "cello": 44}),
]


# ─────────────────────────────────────────────────────────────────────────────
# Naming convention
# ─────────────────────────────────────────────────────────────────────────────

def parse_xml_name(path: Path):
    """
    Parse JLP naming convention for raw XML exports.

    JLP.bass.01.Right_Through_You.xml    → ("bass", "01", "Right_Through_You", None)
    JLP.bass.01.Right_Through_You.b.xml  → ("bass", "01", "Right_Through_You", "b")

    Returns (instrument, cue, title, suffix) or None.
    Suffix is a single lowercase letter >= 'b'; first export has no suffix.
    """
    if path.suffix.lower() != ".xml":
        return None
    stem = path.stem
    parts = stem.split(".")
    if len(parts) < 4 or parts[0].upper() != "JLP":
        return None
    inst = parts[1].lower()
    if inst not in INSTRUMENTS:
        return None
    cue = parts[2].upper()
    # Last part is a suffix if it's an all-lowercase string >= 'b'
    if (
        len(parts) >= 5
        and parts[-1].islower()
        and parts[-1] >= "b"
    ):
        suffix = parts[-1]
        title = ".".join(parts[3:-1])
    else:
        suffix = None
        title = ".".join(parts[3:])
    return inst, cue, title, suffix


def parse_playscorename(path: Path):
    """
    Parse PlayScore-style filenames where dots are stripped on save.

    PlayScore imports a PDF named JLP.bass.00.OVERTURE.pdf and exports the
    XML with all dots removed from the base name:
      JLPbass00OVERTURE.xml       → ("bass", "00", "OVERTURE", None)
      JLPbass00OVERTUREb.xml      → ("bass", "00", "OVERTURE", "b")
      JLPguitar100OVERTURE.xml    → ("guitar1", "00", "OVERTURE", None)
      JLPpercussion00OVERTURE.xml → ("percussion", "00", "OVERTURE", None)

    Returns (instrument, cue, raw_title, suffix) or None.
    Suffix: single lowercase letter >= 'b' appended after the title.
    """
    if path.suffix.lower() != ".xml":
        return None
    stem = path.stem   # e.g. "JLPbass00OVERTURE"

    if not stem.upper().startswith("JLP"):
        return None
    rest = stem[3:]    # strip leading "JLP"

    # Match instrument — longest first to avoid "guitar" matching before "guitar1"
    inst = None
    for candidate in sorted(INSTRUMENTS, key=len, reverse=True):
        if rest.lower().startswith(candidate.lower()):
            inst = candidate
            rest = rest[len(candidate):]
            break
    if inst is None:
        return None

    # Match cue — longest first so "01A" is tried before "01".
    # After a candidate matches, check the character immediately following:
    # if it is a lowercase letter the suffix letter was the start of a title
    # word (e.g. "02All…" → 'A' belongs to "All", not cue "02A"), so skip
    # that candidate and continue to shorter ones.
    cue = None
    for candidate in sorted(CUE_TEMPOS.keys(), key=len, reverse=True):
        if rest.upper().startswith(candidate.upper()):
            after = rest[len(candidate):]
            if after and after[0].islower():
                continue   # suffix letter is part of the title word
            cue  = candidate
            rest = after
            break
    if cue is None:
        return None

    # Suffix: single lowercase letter >= 'b' at end (title itself is uppercase)
    if rest and rest[-1].islower() and rest[-1] >= "b":
        suffix    = rest[-1]
        raw_title = rest[:-1]
    else:
        suffix    = None
        raw_title = rest

    return inst, cue, raw_title, suffix


def parse_merged_name(path: Path):
    """
    Parse JLP.{instrument}.{cue}.{title}.mxl
    Returns (instrument, cue, title) or None.
    """
    if path.suffix.lower() != ".mxl":
        return None
    stem = path.stem
    parts = stem.split(".")
    if len(parts) < 4 or parts[0].upper() != "JLP":
        return None
    inst = parts[1].lower()
    if inst not in INSTRUMENTS:
        return None
    cue = parts[2].upper()
    title = ".".join(parts[3:])
    return inst, cue, title


def parse_assembled_name(path: Path):
    """
    Parse JLP.{cue}.{title}.full.mxl
    Returns (cue, title) or None.
    """
    if path.suffix.lower() != ".mxl":
        return None
    stem = path.stem
    parts = stem.split(".")
    # ['JLP', '01', 'Right_Through_You', 'full']
    if len(parts) < 4 or parts[0].upper() != "JLP" or parts[-1].lower() != "full":
        return None
    cue = parts[1].upper()
    title = ".".join(parts[2:-1])
    return cue, title


def suffix_sort_key(suffix) -> int:
    """None → 0 (first export), 'b' → 1, 'c' → 2, ..."""
    return 0 if suffix is None else ord(suffix) - ord("a")


def next_suffix(suffix) -> str:
    """None → 'b', 'b' → 'c', ..., 'z' → 'ba', 'ba' → 'bb', ..."""
    if suffix is None:
        return "b"
    # Single letter: increment; wrap at 'z' by extending to two-letter suffix
    if len(suffix) == 1:
        if suffix < "z":
            return chr(ord(suffix) + 1)
        return "ba"
    # Two-letter suffix: increment last character, carry if needed
    head, last = suffix[:-1], suffix[-1]
    if last < "z":
        return head + chr(ord(last) + 1)
    return head + "a"  # unlikely to ever hit this depth


def _parse_chunk_pdf_name(path: Path):
    """
    Parse a chunk PDF created by this script: JLP.{inst}.{cue}.{title}.{suffix}.pdf
    Returns (inst, cue, title, suffix) or None.
    suffix is a lowercase string >= 'b' (b, c, ..., z, ba, bb, ...).
    """
    if path.suffix.lower() != ".pdf":
        return None
    stem  = path.stem
    parts = stem.split(".")
    if len(parts) < 5 or parts[0].upper() != "JLP":
        return None
    inst = parts[1].lower()
    if inst not in INSTRUMENTS:
        return None
    cue  = parts[2].upper()
    last = parts[-1]
    if not (last.islower() and last >= "b"):
        return None
    return inst, cue, ".".join(parts[3:-1]), last


def group_raw_xmls(raw_dir: Path, filter_inst=None, filter_cue=None) -> dict:
    """
    Scan raw_dir for XML exports, group by (inst, cue, canonical_title).

    Accepts both naming conventions:
    - Standard JLP:  JLP.bass.00.OVERTURE.xml  (dots preserved)
    - PlayScore:     JLPbass00OVERTURE.xml      (dots stripped by PlayScore on save)

    For PlayScore names the canonical title is resolved from the matching source
    PDF so all downstream naming uses the correct base name.

    Returns {(inst, cue, title): [(path, suffix), ...]} sorted by modification
    time ascending (oldest first, most recent last).  Sorting by mtime rather
    than suffix order ensures iCloud ghost entries (stale directory listings for
    files that have already been moved) are always treated as older than the real
    current file.
    """
    groups: dict = {}
    if not raw_dir.exists():
        return groups
    for f in sorted(raw_dir.iterdir()):
        if f.suffix.lower() != ".xml":
            continue

        # Try standard JLP naming first
        parsed = parse_xml_name(f)
        if parsed is not None:
            inst, cue, title, suffix = parsed
        else:
            # Try PlayScore naming (dots stripped)
            parsed = parse_playscorename(f)
            if parsed is None:
                continue
            inst, cue, raw_title, suffix = parsed
            # Resolve canonical title from the source PDF for this inst+cue
            pdf = _find_source_pdf(inst, cue, raw_title)
            if pdf is not None:
                pdf_parts = pdf.stem.split(".")
                title = ".".join(pdf_parts[3:]) if len(pdf_parts) > 3 else raw_title
            else:
                title = raw_title
            # Correct suffix against the canonical title.  parse_playscorename
            # strips a single trailing lowercase char as a potential suffix, but
            # titles can end with lowercase letters (e.g. "You" → raw="Yo" suf="u").
            # Case 1: title == raw_title + suffix  →  the char was part of the title
            # Case 2: raw_title starts with title  →  multi-char suffix was absorbed
            if raw_title != title:
                if title == raw_title + (suffix or ""):
                    suffix = None
                elif raw_title.startswith(title):
                    extra  = raw_title[len(title):]
                    suffix = extra if (extra.islower() and extra >= "b") else None

        if filter_inst and inst != filter_inst:
            continue
        if filter_cue and cue != filter_cue.upper():
            continue
        groups.setdefault((inst, cue, title), []).append((f, suffix))

    for key in groups:
        groups[key].sort(key=lambda x: x[0].stat().st_mtime)
    return groups


def group_merged_mxls(merged_dir: Path, filter_cue=None) -> dict:
    """
    Scan merged_dir for per-instrument MXLs.
    Returns {(cue, title): [(inst, path), ...]} grouped by cue.
    """
    groups: dict = {}
    if not merged_dir.exists():
        return groups
    for f in sorted(merged_dir.iterdir()):
        parsed = parse_merged_name(f)
        if parsed is None:
            continue
        inst, cue, title = parsed
        if filter_cue and cue != filter_cue.upper():
            continue
        groups.setdefault((cue, title), []).append((inst, f))
    return groups


# ─────────────────────────────────────────────────────────────────────────────
# State file  (~/.jlp_state.json)
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_overrides() -> set:
    if OVERRIDES_FILE.exists():
        try:
            return set(json.loads(OVERRIDES_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def save_overrides(overrides: set):
    OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES_FILE.write_text(json.dumps(sorted(overrides), indent=2))


def load_punchlist() -> dict:
    if PUNCHLIST_FILE.exists():
        try:
            return json.loads(PUNCHLIST_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_punchlist(punchlist: dict):
    PUNCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    PUNCHLIST_FILE.write_text(json.dumps(punchlist, indent=2))


def load_totals() -> dict:
    if TOTALS_FILE.exists():
        try:
            return json.loads(TOTALS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_totals(totals: dict):
    TOTALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOTALS_FILE.write_text(json.dumps(totals, indent=2))


def load_answers() -> dict:
    if ANSWERS_FILE.exists():
        try:
            return json.loads(ANSWERS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_answers(answers: dict):
    ANSWERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ANSWERS_FILE.write_text(json.dumps(answers, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# Interactive clarification
# ─────────────────────────────────────────────────────────────────────────────

def _render_question_box(context: str, description: list, question: str, hint: str) -> str:
    """
    Render the question box to stdout, collect user input, and return it.
    description: list of body strings (empty string = blank separator line).
    Returns the raw stripped input string (possibly empty if user pressed Enter).
    """
    header = f"QUESTION NEEDED — {context}"
    body   = [header, ""] + description + ["", question]
    if hint:
        body.append(f"({hint})")
    body.append("")
    prompt = "Enter answer (or press Enter to skip)"

    inner_w = max(len(l) for l in body + [prompt + ": "]) + 2  # 1-char padding each side
    bar     = "─" * inner_w

    print(f"\n  ┌{bar}┐")
    for line in body:
        print(f"  │ {line:<{inner_w - 1}}│")
    # Prompt line — input() puts the cursor right after it; bottom border follows
    sys.stdout.write(f"  │ {prompt + ': ':<{inner_w - 1}}│\n  └{bar}┘\n  > ")
    sys.stdout.flush()
    try:
        return input().strip()
    except EOFError:
        return ""


def _ask_question(
    key:         str,
    context:     str,
    description: list,
    question:    str,
    hint:        str,
    answers:     dict,
    args,
) -> "str | None":
    """
    Return a stored answer for *key* without prompting, or prompt the user and
    store their answer, or return None when running non-interactively / skipped.

    Never asks the same question twice.  The answer is persisted to
    .jlp_answers.json immediately after the user responds.
    """
    if key in answers:
        stored = answers[key].get("answer", "")
        if stored:
            print(f"   (stored answer for {key!r}: {stored!r})")
            return stored
        return None   # previously skipped

    if getattr(args, "no_interactive", False):
        return None

    if not sys.stdin.isatty():
        return None

    answer = _render_question_box(context, description, question, hint)
    # Store regardless of content so we don't ask again on the next run
    answers[key] = {"answer": answer, "question": question}
    save_answers(answers)
    return answer if answer else None


# ─────────────────────────────────────────────────────────────────────────────
# Pitch / octave correction
# ─────────────────────────────────────────────────────────────────────────────

def _note_to_midi(step: str, octave: int, alter: float = 0.0) -> int:
    return (octave + 1) * 12 + _STEP_MIDI.get(step.upper(), 0) + round(alter)


def _collect_pitches(part_el) -> list:
    result = []
    for note in part_el.iter("note"):
        if note.find("rest") is not None:
            continue
        pitch = note.find("pitch")
        if pitch is None:
            continue
        step  = (pitch.findtext("step")   or "").strip()
        oct_t = (pitch.findtext("octave") or "").strip()
        alt_t = (pitch.findtext("alter")  or "0").strip()
        if step and oct_t:
            try:
                result.append(_note_to_midi(step, int(oct_t), float(alt_t)))
            except (ValueError, TypeError):
                pass
    return result


def apply_octave_correction(part_el, instrument: str):
    """Shift all pitches down one octave if median MIDI pitch exceeds ceiling."""
    ceiling = OCTAVE_CEILINGS.get(instrument)
    if ceiling is None:
        return None, False
    pitches = _collect_pitches(part_el)
    if not pitches:
        return None, False
    med = _median(pitches)
    if med <= ceiling:
        return med, False
    for note in part_el.iter("note"):
        if note.find("rest") is not None:
            continue
        pitch = note.find("pitch")
        if pitch is None:
            continue
        oct_el = pitch.find("octave")
        if oct_el is not None and oct_el.text:
            try:
                oct_el.text = str(int(oct_el.text) - 1)
            except ValueError:
                pass
    return med, True


# ─────────────────────────────────────────────────────────────────────────────
# Unknown-duration warnings
# ─────────────────────────────────────────────────────────────────────────────

def find_unknown_duration_measures(part_el) -> list:
    bad = []
    for m in part_el.findall("measure"):
        for t in m.iter("type"):
            if t.text and "?" in t.text:
                bad.append(m.get("number", "?"))
                break
    return bad


# ─────────────────────────────────────────────────────────────────────────────
# Switch detection & program-change injection
# ─────────────────────────────────────────────────────────────────────────────

def _measure_text(measure_el) -> str:
    texts = []
    for el in measure_el.iter():
        if el.tag in ("words", "rehearsal") and el.text:
            texts.append(el.text.strip())
    return " ".join(texts).upper()


def _inject_pc_direction(measure_el, program: int):
    d  = ET.Element("direction")
    dt = ET.SubElement(d, "direction-type")
    w  = ET.SubElement(dt, "words")
    w.text = ""
    s  = ET.SubElement(d, "sound")
    s.set("midi-program", str(program))
    measure_el.insert(0, d)


def detect_and_inject_switches(part_el, instrument: str) -> list:
    detected = []
    for m in part_el.findall("measure"):
        combined = _measure_text(m)
        if not combined:
            continue
        for pattern, programs in SWITCH_PATTERNS:
            if pattern in combined:
                prog = programs.get(instrument)
                if prog is not None:
                    _inject_pc_direction(m, prog)
                    detected.append((m.get("number", "?"), pattern, prog))
                    break
    return detected


# ─────────────────────────────────────────────────────────────────────────────
# Tempo injection
# ─────────────────────────────────────────────────────────────────────────────

def _has_tempo(root) -> bool:
    return root.find(".//metronome") is not None


def _inject_tempo_direction(measure_el, bpm: int):
    d  = ET.Element("direction", placement="above")
    dt = ET.SubElement(d, "direction-type")
    mt = ET.SubElement(dt, "metronome")
    ET.SubElement(mt, "beat-unit").text  = "quarter"
    ET.SubElement(mt, "per-minute").text = str(bpm)
    s  = ET.SubElement(d, "sound")
    s.set("tempo", str(bpm))
    insert_pos = 0
    for i, child in enumerate(list(measure_el)):
        if child.tag in ("note", "harmony", "barline"):
            insert_pos = i
            break
    measure_el.insert(insert_pos, d)


def inject_tempo(part_el, measure_number: int, bpm: int):
    for m in part_el.findall("measure"):
        if m.get("number") == str(measure_number):
            _inject_tempo_direction(m, bpm)
            return
    measures = part_el.findall("measure")
    if measures:
        _inject_tempo_direction(measures[0], bpm)


# ─────────────────────────────────────────────────────────────────────────────
# MusicXML I/O
# ─────────────────────────────────────────────────────────────────────────────

def _repair_xml(raw: bytes, filename: str) -> "bytes | None":
    """
    Repair common PlayScore XML corruption: a <time> element whose opening tag
    is missing, leaving orphaned <beats>/<beat-type> tags and an unmatched
    </time> closing tag (which causes ET.parse to raise ParseError).

    Algorithm (line-by-line):
    1. When a <beats> line is found that is NOT already inside an open <time>
       (i.e. the preceding non-blank result line and the current line do not
       contain <time>), treat it as an orphaned block.
    2. Collect the block: this line plus any directly following <beat-type>
       lines.
    3. Look ahead past optional blank lines for an orphaned </time> — if found,
       consume it as the block's closing tag (avoids a duplicate </time>).
       If not found, synthesise a closing </time>.
    4. Emit: <time> opening + block lines + </time>.

    Returns repaired bytes when at least one fix was applied, else None.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")

    lines    = text.splitlines(keepends=True)
    result: list = []
    i        = 0
    modified = False

    while i < len(lines):
        line = lines[i]

        if re.search(r"<beats\b", line):
            # Already inside <time> if <time> appears before <beats> on this line
            beats_pos = re.search(r"<beats\b", line).start()
            if re.search(r"<time\b", line[:beats_pos]):
                result.append(line)
                i += 1
                continue

            # Check the last non-blank result line for an open <time>
            j = len(result) - 1
            while j >= 0 and not result[j].strip():
                j -= 1
            prev = result[j].strip() if j >= 0 else ""

            if not re.search(r"<time\b", prev):
                indent = " " * (len(line) - len(line.lstrip()))

                # Collect <beats> and any following <beat-type> lines
                block = [line]
                i += 1
                while i < len(lines) and re.search(r"<beats\b|<beat-type\b", lines[i]):
                    block.append(lines[i])
                    i += 1

                # Look ahead for an orphaned </time> (possibly after blanks)
                look      = i
                gap_lines = []
                while look < len(lines) and not lines[look].strip():
                    gap_lines.append(lines[look])
                    look += 1
                if look < len(lines) and re.search(r"</time\b", lines[look]):
                    # Orphaned </time> found — use it as the closing tag and
                    # skip it from further processing so it isn't emitted twice.
                    close_line = lines[look]
                    i = look + 1
                else:
                    close_line = f"{indent}</time>\n"
                    # gap_lines go back into the stream normally
                    gap_lines  = []

                result.append(f"{indent}<time>\n")
                result.extend(block)
                result.extend(gap_lines)
                result.append(close_line)
                modified = True
                continue

        result.append(line)
        i += 1

    return "".join(result).encode("utf-8") if modified else None


def load_xml(path) -> ET.ElementTree:
    """
    Parse a plain .xml or compressed .mxl file.
    On ParseError in a plain .xml, attempts automatic repair of the common
    PlayScore corruption where <beats>/<beat-type> tags lack a <time> wrapper.
    Raises ValueError if the file cannot be parsed even after repair.
    """
    p    = str(path)
    name = Path(p).name

    if p.lower().endswith(".mxl"):
        with zipfile.ZipFile(p) as zf:
            root_entry = None
            try:
                cdata  = zf.read("META-INF/container.xml")
                croot  = ET.fromstring(cdata)
                for rf in croot.iter("rootfile"):
                    root_entry = rf.get("full-path")
                    break
            except (KeyError, ET.ParseError):
                pass
            if root_entry is None:
                for n in zf.namelist():
                    if n.endswith(".xml") and "META-INF" not in n:
                        root_entry = n
                        break
            if root_entry is None:
                raise ValueError(f"No MusicXML content in {p}")
            return ET.parse(io.BytesIO(zf.read(root_entry)))

    # Plain .xml — try direct parse, then repair on failure
    raw = Path(p).read_bytes()
    try:
        return ET.parse(io.BytesIO(raw))
    except ET.ParseError:
        repaired = _repair_xml(raw, name)
        if repaired is not None:
            try:
                tree = ET.parse(io.BytesIO(repaired))
                print(f"   [repaired] XML parse error in {name} — "
                      f"auto-fixed malformed <time> element")
                return tree
            except ET.ParseError:
                pass
        raise ValueError(f"XML parse error in {name} (repair failed)")


def write_mxl(tree: ET.ElementTree, output_path) -> int:
    """Write ElementTree as a compressed .mxl. Returns XML byte count."""
    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    xml_bytes = buf.getvalue()
    op    = str(output_path)
    inner = Path(op).stem + ".xml"
    container = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<container>\n  <rootfiles>\n"
        f'    <rootfile full-path="{inner}"\n'
        '      media-type="application/vnd.recordare.musicxml+xml"/>\n'
        "  </rootfiles>\n</container>\n"
    ).encode("utf-8")
    with zipfile.ZipFile(op, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr(inner, xml_bytes)
    return len(xml_bytes)


# ─────────────────────────────────────────────────────────────────────────────
# Part utilities
# ─────────────────────────────────────────────────────────────────────────────

def _name_map(root) -> dict:
    m = {}
    for sp in root.findall(".//score-part"):
        pid = sp.get("id")
        ne  = sp.find("part-name")
        if pid and ne is not None and ne.text:
            m[pid] = ne.text.strip()
    return m


def extract_primary_part(root, instrument: str):
    """Return (part_element, part_name) best matching instrument."""
    parts = root.findall("part")
    if not parts:
        return None, None
    nm  = _name_map(root)
    kw  = instrument.lower().rstrip("12")
    for part in parts:
        pid = part.get("id", "")
        if kw in nm.get(pid, "").lower():
            return part, nm.get(pid, instrument)
    return parts[0], nm.get(parts[0].get("id", ""), instrument)


def make_score_part_el(part_id: str, instrument: str, channel: int = 1):
    sp = ET.Element("score-part", id=part_id)
    ET.SubElement(sp, "part-name").text = instrument.capitalize()
    inst_id = f"{part_id}-I1"
    si = ET.SubElement(sp, "score-instrument", id=inst_id)
    ET.SubElement(si, "instrument-name").text = instrument
    ET.SubElement(sp, "midi-device", id=inst_id, port="1")
    mi = ET.SubElement(sp, "midi-instrument", id=inst_id)
    ET.SubElement(mi, "midi-channel").text  = str(channel)
    ET.SubElement(mi, "midi-program").text  = str(GM_DEFAULT.get(instrument, 1))
    ET.SubElement(mi, "volume").text        = "80"
    ET.SubElement(mi, "pan").text           = "0"
    return sp


def _last_time_sig(part_el):
    beats, beat_type, divisions = 4, 4, 1
    for m in part_el.findall("measure"):
        for attr in m.findall("attributes"):
            d = attr.findtext("divisions")
            if d:
                try:
                    divisions = int(d)
                except ValueError:
                    pass
            time_el = attr.find("time")
            if time_el is not None:
                b  = time_el.findtext("beats")
                bt = time_el.findtext("beat-type")
                try:
                    if b:  beats     = int(b)
                    if bt: beat_type = int(bt)
                except ValueError:
                    pass
    return beats, beat_type, divisions


def _make_rest_measure(number: int, beats: int, beat_type: int, divisions: int):
    m    = ET.Element("measure", number=str(number))
    note = ET.SubElement(m, "note")
    rest = ET.SubElement(note, "rest")
    rest.set("measure", "yes")
    ET.SubElement(note, "duration").text = str(int(divisions * beats * 4 / beat_type))
    return m


def last_measure_number(xml_path: Path) -> int:
    """Return the highest measure/@number in the file.
    Uses load_xml so corrupt files are auto-repaired before parsing.
    Raises ValueError if the file cannot be read or contains no measures."""
    root = load_xml(xml_path).getroot()
    nums = []
    for m in root.iter("measure"):
        raw = (m.get("number") or "").strip()
        try:
            nums.append(int(raw))
        except ValueError:
            pass
    if not nums:
        raise ValueError(f"No numeric measure numbers in {xml_path}")
    return max(nums)


# ─────────────────────────────────────────────────────────────────────────────
# PDF / OCR utilities
# ─────────────────────────────────────────────────────────────────────────────

def _measure_pattern(n: int, fuzz: int = 2) -> re.Pattern:
    """Regex matching any integer in [n-fuzz, n+fuzz] as a standalone word."""
    alts = "|".join(str(n + d) for d in range(-fuzz, fuzz + 1) if n + d > 0)
    return re.compile(r"(?<!\d)(?:" + alts + r")(?!\d)")


def _ocr_page(page) -> str:
    """Rasterise a fitz page and OCR it. fitz auto-corrects /Rotate so no PIL rotation needed."""
    mat = fitz.Matrix(OCR_DPI / 72, OCR_DPI / 72)
    pix = page.get_pixmap(matrix=mat)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    return pytesseract.image_to_string(img, config="--psm 11")


def _ocr_page_max_measure(page, full_page: bool = False) -> "int | None":
    """
    Return the highest standalone integer 1-999 on a page — the last measure
    number visible on that page.  fitz auto-corrects /Rotate so no PIL rotation needed.

    full_page=False (default, last-page mode):
        Try bottom 20% + top 15% strips first (fast path — measure numbers
        normally cluster there on the final page).  Fall back to full-page OCR
        if strips return nothing, e.g. when numbers live inside rehearsal-mark
        boxes in the middle of the page.

    full_page=True (penultimate/earlier-page mode):
        Skip strips and OCR the whole page directly, because on non-final pages
        the highest-numbered measures appear in the middle, not the bottom strip.
    """
    mat = fitz.Matrix(OCR_DPI / 72, OCR_DPI / 72)
    pix = page.get_pixmap(matrix=mat)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    w, h = img.size

    def _nums_from_text(text):
        result = []
        for m in re.finditer(r"(?<!\d)\d{1,3}(?!\d)", text):
            n = int(m.group())
            if 1 <= n <= 999:
                result.append(n)
        return result

    if not full_page:
        # Fast path: strips where measure numbers normally appear on a final page
        regions = [
            img.crop((0, int(h * 0.80), w, h)),   # bottom 20%
            img.crop((0, 0, w, int(h * 0.15))),    # top 15%
        ]
        nums = []
        for region in regions:
            nums.extend(_nums_from_text(
                pytesseract.image_to_string(region, config="--psm 11")
            ))
        if nums:
            return max(nums)

    # Full-page scan (fallback for last page; default for earlier pages)
    nums = _nums_from_text(pytesseract.image_to_string(img, config="--psm 11"))
    return max(nums) if nums else None


def scan_pdf_for_measure(pdf_path: Path, measure_num: int, hint_page: int | None = None):
    """
    Find the first page of pdf_path containing measure_num (±2).
    hint_page: 1-based page to start scanning from.
    Returns (page_index_0based | None, total_pages).
    """
    doc     = fitz.open(str(pdf_path))
    total   = len(doc)
    pattern = _measure_pattern(measure_num)
    start   = max(0, (hint_page - 1) if hint_page else 0)

    print(f"   Scanning {pdf_path.name} ({total} pages) for m{measure_num} ...",
          flush=True)

    for idx in range(start, total):
        page = doc[idx]
        sys.stdout.write(f"\r   page {idx + 1}/{total}  ")
        sys.stdout.flush()
        if pattern.search(page.get_text("text")):
            sys.stdout.write(f"\r   Found on page {idx + 1}/{total}          \n")
            doc.close()
            return idx, total
        if pattern.search(_ocr_page(page)):
            sys.stdout.write(f"\r   Found on page {idx + 1}/{total} (OCR)    \n")
            doc.close()
            return idx, total

    sys.stdout.write("\n")
    doc.close()
    return None, total


def _is_vs_page(page) -> bool:
    """
    Return True if a page appears to be a V.S. (Volti Subito) navigation-only
    page containing no musical content.

    Uses the same text-based heuristic as _is_navigation_page but applies a
    5% ink-coverage threshold (rather than 2%) because V.S. last pages in
    Concord Theatricals scores often carry a visible arrow graphic that pushes
    coverage above 2% even though there is no music.

    Detection order:
    1. Text layer present → V.S./tacet pattern found AND no standalone integers
       (measure numbers) on the same page.
    2. No text layer → ink coverage < 5% at 72 DPI.
    """
    text = page.get_text("text").strip()
    if text:
        if _NAV_PATTERN.search(text) and not re.search(r"(?<!\d)\d{1,3}(?!\d)", text):
            return True
        return False
    mat = fitz.Matrix(1.0, 1.0)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
    non_white = sum(1 for b in pix.samples if b < 240)
    coverage  = non_white / len(pix.samples) if pix.samples else 0
    return coverage < 0.05


def _last_music_page_0(doc, pdf_name: str) -> int:
    """
    Return the 0-indexed position of the last page in *doc* that contains
    musical content.  If the final page is a V.S.-only navigation page it is
    skipped and the previous page index is returned, with an [info] log line.
    Only inspects the final page — multi-page V.S. tails are unusual.
    """
    last_idx = len(doc) - 1
    if last_idx >= 1:
        try:
            if _is_vs_page(doc[last_idx]):
                print(f"   [info] Last page of {pdf_name} appears to be a V.S. page — "
                      f"using page {last_idx} as last music page.")
                return last_idx - 1
        except Exception:
            pass
    return last_idx


def _is_navigation_page(page) -> bool:
    """
    Return True if this page carries no musical content and should be skipped
    when building a chunk PDF.

    Two detection paths:
    - Text-based PDF: fitz text contains a known navigation marker (V.S., tacet,
      etc.) and has no measure numbers (integers 1-999).  A real music page that
      merely mentions "V.S." in a direction still has measure numbers, so it won't
      be filtered.
    - Rasterised PDF (no text layer): render at 72 DPI and measure ink coverage.
      Real music pages are ≥ 8% ink; blank/V.S.-arrow pages are < 2%.
    """
    text = page.get_text("text").strip()
    if text:
        if _NAV_PATTERN.search(text):
            # Only skip if no measure numbers are present on the same page
            if not re.search(r"(?<!\d)\d{1,3}(?!\d)", text):
                return True
        return False

    # No text layer — fall back to pixel coverage check
    mat = fitz.Matrix(1.0, 1.0)   # 72 DPI; fast and enough for coverage estimate
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
    non_white = sum(1 for b in pix.samples if b < 240)
    coverage = non_white / len(pix.samples) if pix.samples else 0
    return coverage < 0.02        # < 2% ink → blank or near-blank


def extract_pages_fixed(src_path: Path, from_page_0: int, output_path: Path) -> int:
    """
    Extract pages [from_page_0 .. end] to output_path, skipping navigation/
    marker-only pages (V.S. indicators, blank spacer pages, etc.).
    Pages with rotation==180 are corrected via PIL so PlayScore doesn't silently
    re-read page 1 when it encounters an upside-down page.
    Returns the number of music pages written (0 if only nav pages remained).
    """
    src   = fitz.open(str(src_path))
    total = len(src)
    out   = fitz.open()

    for idx in range(from_page_0, total):
        src_page = src[idx]
        if _is_navigation_page(src_page):
            print(f"   (skipping navigation/marker page {idx + 1}/{total})")
            continue
        if src_page.rotation == 180:
            # Extract the raw embedded image, rotate 180° with PIL, and write a
            # fresh page so PlayScore receives a clean upright image with no
            # rotation metadata.
            images = src_page.get_images()
            xref   = images[0][0]
            pix    = fitz.Pixmap(src, xref)
            img    = Image.open(io.BytesIO(pix.tobytes("png")))
            img    = img.rotate(180)
            img_bytes = io.BytesIO()
            img.save(img_bytes, format="PNG")
            rotated_pix = fitz.Pixmap(img_bytes.getvalue())
            new_page = out.new_page(width=src_page.rect.width,
                                    height=src_page.rect.height)
            new_page.insert_image(new_page.rect, pixmap=rotated_pix)
        else:
            out.insert_pdf(src, from_page=idx, to_page=idx)

    n_written = len(out)
    if n_written > 0:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        out.save(str(output_path))
    src.close()
    out.close()
    return n_written


def _find_source_pdf(inst: str, cue: str, title: str) -> "Path | None":
    """Find source PDF, preferring exact title match then any cue match."""
    folder = SOURCE_DIR / inst
    if not folder.exists():
        return None
    exact = folder / f"JLP.{inst}.{cue}.{title}.pdf"
    if exact.exists():
        return exact
    pat = re.compile(
        r"^JLP\." + re.escape(inst) + r"\." + re.escape(cue) + r"\.",
        re.IGNORECASE,
    )
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() == ".pdf" and pat.match(f.name):
            return f
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Phase: check
# ─────────────────────────────────────────────────────────────────────────────

def phase_check(args):
    if not _OCR_AVAILABLE:
        print(
            "[error] OCR dependencies not installed.\n"
            "  Run: pip install pymupdf pytesseract Pillow",
            file=sys.stderr,
        )
        sys.exit(1)

    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)
    TRASH_MERGED_DIR.mkdir(parents=True, exist_ok=True)

    if getattr(args, "no_interactive", False):
        print("[info] Non-interactive mode — ambiguous cases will use best-guess fallbacks")

    state     = load_state()
    overrides = load_overrides()
    answers   = load_answers()
    punchlist = load_punchlist()
    totals    = load_totals()

    # Belt-and-suspenders: if --reset-state was given alongside --phase check,
    # scrub per-instrument stale keys even if the file deletion raced with a
    # cloud-sync daemon that restored the old state file.
    if getattr(args, "reset_state", False) and state:
        _scrub_stale_keys(state)
        save_state(state)

    # Process --mark-complete flags before scanning
    if args.mark_complete:
        for token in args.mark_complete:
            if ":" not in token:
                print(f"[warning] --mark-complete: ignoring {token!r} (expected inst:cue)")
                continue
            inst_mc, cue_mc = token.split(":", 1)
            inst_mc = inst_mc.lower()
            cue_mc  = cue_mc.upper()
            if inst_mc not in INSTRUMENTS:
                print(f"[warning] --mark-complete: unknown instrument {inst_mc!r}")
                continue
            key = f"{inst_mc}.{cue_mc}"
            overrides.add(key)
            state.setdefault(key, {})["complete"] = True
            print(f"[override] Marked complete: {inst_mc} cue {cue_mc}")
        save_overrides(overrides)
        save_state(state)

    # ── Startup: archive chunk PDFs already imported into PlayScore ──────────
    # Match every PDF in next_pdfs/ against XMLs in raw/ by (inst, cue, suffix).
    # If a match is found the user has already imported that PDF, so move it to
    # trash/ to keep next_pdfs/ clean.
    if NEXT_DIR.exists():
        all_raw = group_raw_xmls(RAW_DIR)
        imported_sigs: set = set()
        for (i, c, _t), parts in all_raw.items():
            for _p, suf in parts:
                if suf is not None:
                    imported_sigs.add((i, c, suf))
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        for f in sorted(NEXT_DIR.iterdir()):
            parsed = _parse_chunk_pdf_name(f)
            if parsed is None:
                continue
            p_inst, p_cue, _p_title, p_suf = parsed
            if (p_inst, p_cue, p_suf) in imported_sigs:
                shutil.move(str(f), str(TRASH_DIR / f.name))
                print(f"Archived to trash/: {f.name}  (XML already in raw/)")

    groups = group_raw_xmls(RAW_DIR, filter_inst=args.instrument, filter_cue=args.cue)

    if not groups:
        print(f"No JLP-named XML files found in: {RAW_DIR}")
        print("Drop your PlayScore XML exports there and re-run.")
        return

    complete_entries: list = []   # "inst cue XX" for complete instruments
    chunk_pdfs:      list = []    # Path objects for generated chunk PDFs

    print(f"\nFound {len(groups)} instrument/cue group(s) in {RAW_DIR}\n")

    for (inst, cue, title), parts in sorted(groups.items()):
        latest_path, latest_suffix = parts[-1]
        print(f"── {inst} / cue {cue}  ({len(parts)} part(s))")
        print(f"   Latest : {latest_path.name}")

        state_key    = f"{inst}.{cue}"
        cached       = state.setdefault(state_key, {})
        prev_seen    = cached.get("last_seen_xml")
        prev_covered = set(cached.get("covered_pages", []))

        cached["last_seen_xml"] = latest_path.name
        save_state(state)

        # Check manual override
        if state_key in overrides:
            print(f"   ✓ Complete (manual override). Ready to merge.")
            cached["complete"] = True
            save_state(state)
            complete_entries.append(f"{inst} cue {cue}")
            print()
            continue

        pdf_path = _find_source_pdf(inst, cue, title)
        if pdf_path is None:
            print(f"   [warning] Source PDF not found.")
            print(f"   Expected: {SOURCE_DIR}/{inst}/JLP.{inst}.{cue}.{title}.pdf")
            print()
            continue
        print(f"   Source PDF : {pdf_path.name}")

        # ── Step 1: Get total pages (no OCR needed) ──────────────────────────
        # Always run the V.S. last-page check so that state reflects the true
        # music-only page count even when the PDF was cached before V.S.
        # detection was added.  Saving only when the value actually changes
        # keeps disk writes minimal.
        doc         = fitz.open(str(pdf_path))
        total_pages = _last_music_page_0(doc, pdf_path.name) + 1
        has_vs_last = (total_pages < len(doc))
        doc.close()
        if (args.force
                or cached.get("pdf_path") != str(pdf_path)
                or cached.get("total_pages") != total_pages
                or cached.get("vs_last_page") != has_vs_last):
            cached["pdf_path"]     = str(pdf_path)
            cached["total_pages"]  = total_pages
            cached["vs_last_page"] = has_vs_last
            save_state(state)
        print(f"   Total pages: {total_pages}")

        # Flag short cues (≤2 pages, known total <10 measures) so the punchlist
        # can suggest manual entry instead of a difficult re-export.
        known_total = totals.get(cue, {}).get("total")
        if total_pages <= 2 and known_total and known_total < 10:
            punchlist.setdefault(cue, {})["_short_cue"] = True
            save_punchlist(punchlist)

        # ── Step 2: Build page coverage for every XML in the group ───────────
        # For the no-suffix (first) XML: measure numbers printed in the score
        # match the source PDF, so we can scan for the last measure to find
        # which page it falls on.  For chunk XMLs, PlayScore renumbers from m1
        # so measure numbers cannot be used to locate pages in the source PDF —
        # instead we use the pending_chunk record written when the chunk was
        # generated, or infer remaining pages when no next chunk PDF is pending.

        cached_parts_by_name = {p["filename"]: p for p in cached.get("parts", [])}
        new_parts: list = []
        prev_end  = 0   # highest 1-based source page covered so far
        xml_parse_failed = False  # set True when a file cannot be parsed even after repair

        for xml_path, xml_suffix in parts:   # oldest → newest by mtime
            fname = xml_path.name

            # Use cache only when pages is non-empty — an empty cached list
            # means coverage was deferred in a previous run and must be recomputed.
            if not args.force and fname in cached_parts_by_name:
                cached_entry = cached_parts_by_name[fname]
                if cached_entry.get("pages"):
                    entry     = cached_entry.copy()
                    xml_pages = entry["pages"]
                    prev_end  = max(xml_pages)
                    new_parts.append(entry)
                    continue
                # pages missing or empty → fall through to recompute

            # Scan source PDF for this XML's last measure.
            # For the no-suffix XML, PlayScore reads measure numbers from the
            # printed score, so the scan locates the exact coverage end page.
            # For suffix XMLs the same applies when PlayScore preserved source
            # numbering; if it renumbered from m1 the scan returns a page that
            # is ≤ prev_end, which we treat as a scan failure and fall back to
            # covering all remaining pages.
            try:
                last_m = last_measure_number(xml_path)
            except ValueError as exc:
                if "repair failed" in str(exc) or "XML parse error" in str(exc):
                    # Structural parse failure — cannot read this file at all.
                    # Log the skip, record the whole inst+cue as malformed in
                    # the punchlist, and abandon the rest of this group.
                    print(f"   [skipped] {fname} — malformed XML, could not repair. "
                          f"Added to punchlist as fully missing.")
                    est_total = total_pages * 30
                    punchlist.setdefault(cue, {})["_title"] = title
                    punchlist[cue][inst] = {
                        "malformed":        True,
                        "captured_through": 0,
                        "missing":          [[1, est_total]],
                        "uncertain":        [],
                        "total":            est_total,
                        "total_source":     "estimated_pages",
                    }
                    save_punchlist(punchlist)
                    xml_parse_failed = True
                    break   # exits the inner `for xml_path` loop
                # Semantic failure (e.g. no numeric measures found) — use fallback
                print(f"   [error] {exc}")
                fallback_pages = [1] if xml_suffix is None else list(range(prev_end + 1, total_pages + 1))
                new_parts.append({"filename": fname, "pages": fallback_pages})
                prev_end = max(fallback_pages) if fallback_pages else prev_end
                continue

            cached_p0 = cached_parts_by_name.get(fname, {}).get("page_0")
            if cached_p0 is not None and not args.force:
                page_0 = cached_p0
                print(f"   (cached) {fname}: last m{last_m} on source page {page_0 + 1}/{total_pages}")
            else:
                page_0, _ = scan_pdf_for_measure(pdf_path, last_m)

            if xml_suffix is None:
                # No-suffix XML always starts at page 1
                if page_0 is not None:
                    xml_pages = list(range(1, page_0 + 2))   # [1..page_0+1] in 1-based
                    prev_end  = page_0 + 1
                    new_parts.append({"filename": fname, "pages": xml_pages, "page_0": page_0})
                else:
                    # Trigger 2: OCR cannot locate the measure — ask user for the
                    # last measure number on the last page so we can retry the scan.
                    resolved_p0 = None
                    q_key = f"{inst}:{cue}:last_measure_page:{total_pages}"
                    ans = _ask_question(
                        key=q_key,
                        context=f"{inst} cue {cue}",
                        description=[
                            f"OCR could not locate measure m{last_m} in",
                            f"{pdf_path.name}",
                        ],
                        question=f"What is the last measure number on page {total_pages}"
                                 f" of {pdf_path.name}?",
                        hint="Look at the bottom-right of the last staff",
                        answers=answers,
                        args=args,
                    )
                    if ans and ans.strip().lstrip("-").isdigit():
                        hint_m     = int(ans.strip())
                        retry_p0, _ = scan_pdf_for_measure(pdf_path, hint_m)
                        if retry_p0 is not None:
                            resolved_p0 = retry_p0

                    if resolved_p0 is not None:
                        xml_pages = list(range(1, resolved_p0 + 2))
                        prev_end  = resolved_p0 + 1
                        new_parts.append({"filename": fname, "pages": xml_pages,
                                          "page_0": resolved_p0})
                    else:
                        print(f"   [warning] Could not locate m{last_m} in source — "
                              f"assuming page 1 only")
                        new_parts.append({"filename": fname, "pages": [1]})
                        prev_end = 1
            else:
                # Suffix XML: use the scan result if it lands past where the
                # previous XML ended; otherwise assume it covers all remaining pages.
                if page_0 is not None and (page_0 + 1) > prev_end:
                    xml_pages = list(range(prev_end + 1, page_0 + 2))
                    prev_end  = page_0 + 1
                    new_parts.append({"filename": fname, "pages": xml_pages, "page_0": page_0})
                else:
                    xml_pages = list(range(prev_end + 1, total_pages + 1))
                    prev_end  = total_pages
                    new_parts.append({"filename": fname, "pages": xml_pages})

        if xml_parse_failed:
            print()
            continue   # skip coverage / completion steps for this group

        # Compute overall coverage and update state
        covered  = sorted(set(p for e in new_parts for p in e.get("pages", [])))
        expected = list(range(1, total_pages + 1))

        cached["covered_pages"] = covered
        cached["parts"]         = new_parts
        save_state(state)

        # ── Step 3: Completion check ─────────────────────────────────────────
        if covered == expected:
            n = len(covered)
            print(f"   {inst} cue {cue}: pages 1-{total_pages} covered ({n}/{total_pages}) ✓ Complete")
            print(f"   ✓ Complete. Ready to merge.")
            cached["complete"] = True
            save_state(state)
            complete_entries.append(f"{inst} cue {cue}")
            print()
            continue

        n_cov = len(covered)
        print(f"   {inst} cue {cue}: {n_cov}/{total_pages} pages covered — incomplete")
        cached["complete"] = False
        save_state(state)

        # ── Step 4: Check for a pending chunk PDF ────────────────────────────
        next_suf   = next_suffix(latest_suffix)
        chunk_name = f"JLP.{inst}.{cue}.{title}.{next_suf}.pdf"
        chunk_path = NEXT_DIR / chunk_name

        if chunk_path.exists():
            ps_name = chunk_path.stem.replace(".", "") + ".xml"
            print(f"   → Waiting for import: {chunk_path.name}")
            print(f"      PlayScore will name the export: {ps_name}")
            chunk_pdfs.append(chunk_path)
            print()
            continue

        # ── Step 5: Loop detection ───────────────────────────────────────────
        # Only warn when the SAME latest filename appears in two consecutive runs
        # AND page coverage has not advanced since last run.
        coverage_advanced = set(covered) > prev_covered
        if prev_seen is not None and prev_seen == latest_path.name and not coverage_advanced:
            # If the source PDF has a V.S. last page, the stall is expected:
            # PlayScore finished the music pages and stopped before the
            # navigation page.  Mark complete without asking.
            if cached.get("vs_last_page"):
                print(f"   ✓ Complete — export stalled at V.S. last page (no music there). "
                      f"Ready to merge.")
                cached["complete"] = True
                save_state(state)
                complete_entries.append(f"{inst} cue {cue}")
                print()
                continue

            try:
                last_m_loop = last_measure_number(latest_path)
            except Exception:
                last_m_loop = "?"
            pages_str = (f"pages {min(covered)}-{max(covered)}"
                         if covered else "unknown pages")

            # Trigger 3: ask whether PlayScore captured everything
            q_loop = f"{inst}:{cue}:loop_confirmed:{latest_path.name}"
            ans_loop = _ask_question(
                key=q_loop,
                context=f"{inst} cue {cue}",
                description=[
                    f"PlayScore exported m{last_m_loop} from {pages_str}",
                    f"twice in a row without advancing page coverage.",
                ],
                question=f"Did PlayScore capture all the music? (yes/no)",
                hint="Check the exported XML against the score; enter 'yes' if complete",
                answers=answers,
                args=args,
            )
            if ans_loop and ans_loop.strip().lower() in ("yes", "y"):
                print(f"   ✓ Complete (user confirmed). Ready to merge.")
                cached["complete"] = True
                save_state(state)
                complete_entries.append(f"{inst} cue {cue}")
                print()
                continue

            # Trigger 4: ask whether the instrument is TACET in this cue
            q_tacet = f"{inst}:{cue}:tacet"
            ans_tacet = _ask_question(
                key=q_tacet,
                context=f"{inst} cue {cue}",
                description=[
                    f"Page coverage has not advanced for {inst} cue {cue}.",
                    f"The instrument may be TACET (silent) for this cue.",
                ],
                question=f"Does {inst} play in cue {cue}, or is it TACET?",
                hint="Enter 'tacet' if silent for the whole cue",
                answers=answers,
                args=args,
            )
            if ans_tacet and "tacet" in ans_tacet.strip().lower():
                print(f"   ✓ Complete — {inst} is TACET in cue {cue}. Ready to merge.")
                cached["complete"] = True
                save_state(state)
                complete_entries.append(f"{inst} cue {cue}")
                print()
                continue

            print(
                f"\n[warning] {inst} cue {cue}: PlayScore has exported the same content "
                f"twice in a row without advancing page coverage. PlayScore may have hit "
                f"its recognition limit on this chunk.\n"
                f"   Generating the next chunk PDF for the remaining pages.\n"
                f"   Options for the next import:\n"
                f"   a) Try importing a smaller chunk (1-2 pages at a time)\n"
                f"   b) Mark as complete if you believe all music is captured: "
                f"--mark-complete {inst}:{cue}\n"
                f"   c) Skip for now and continue with other instruments"
            )
            # Fall through to Step 6 — generate the chunk PDF so the user has
            # it ready to try, even when loop detection fires.

        # ── Step 6: Determine chunk start page and generate chunk PDF ────────
        # from_page_0: 0-indexed source page where the new chunk starts.
        # The latest XML's pages tell us the last covered page (1-based); the
        # chunk starts at the 0-indexed equivalent of the next 1-based page,
        # which equals max(latest_pages) in 0-indexed terms.
        latest_entry = new_parts[-1] if new_parts else {}
        latest_pages = latest_entry.get("pages", [])

        if latest_pages:
            from_page_0 = max(latest_pages)   # 1-based last covered = 0-indexed chunk start
        else:
            # Safety fallback: scan source for latest XML's last measure
            try:
                last_m_fb  = last_measure_number(latest_path)
                p0_fb, _   = scan_pdf_for_measure(pdf_path, last_m_fb)
                from_page_0 = (p0_fb + 1) if p0_fb is not None else 1
            except Exception:
                from_page_0 = 1

        if from_page_0 >= total_pages:
            print(f"   ✓ Complete — last covered page is the final source page. Ready to merge.")
            cached["complete"] = True
            save_state(state)
            complete_entries.append(f"{inst} cue {cue}")
            print()
            continue

        n_pages = extract_pages_fixed(pdf_path, from_page_0, chunk_path)

        if n_pages == 0:
            print(f"   ✓ Complete — remaining pages are navigation markers only. Ready to merge.")
            cached["complete"] = True
            save_state(state)
            complete_entries.append(f"{inst} cue {cue}")
            print()
            continue

        # Move the consumed chunk PDF (the one that produced latest_path) to trash.
        # XMLs are NOT moved here — all parts stay in raw/ until --phase merge
        # runs so that every XML can contribute to page coverage on re-checks.
        if latest_suffix is not None:
            consumed_name = f"JLP.{inst}.{cue}.{title}.{latest_suffix}.pdf"
            consumed_pdf  = NEXT_DIR / consumed_name
            if consumed_pdf.exists():
                TRASH_DIR.mkdir(parents=True, exist_ok=True)
                shutil.move(str(consumed_pdf), str(TRASH_DIR / consumed_name))
                print(f"   Moved to trash/: {consumed_name}")

        # Record which source pages this new chunk covers so that when the
        # corresponding XML arrives we can mark those pages as covered.
        chunk_pages = list(range(from_page_0 + 1, total_pages + 1))
        cached["pending_chunk"] = {
            "suffix":   next_suf,
            "pages":    chunk_pages,
            "pdf_name": chunk_name,
        }
        save_state(state)

        ps_name = chunk_path.stem.replace(".", "") + ".xml"
        print(f"   → Chunk PDF : {chunk_path.name}")
        print(f"      Pages {from_page_0 + 1}–{total_pages} ({n_pages} page(s))")
        print(f"      PlayScore will name the export: {ps_name}")
        chunk_pdfs.append(chunk_path)
        print()

    # ── Actionable summary ────────────────────────────────────────────────────
    print()
    if chunk_pdfs and complete_entries:
        print("═══ ACTION REQUIRED ═══")
        print("Import these PDFs into PlayScore and export XMLs to the raw folder:")
        for i, cp in enumerate(chunk_pdfs, 1):
            print(f"  {i}. {cp.name}  →  {cp.parent}")
        print(f"When done, re-run: python3 jlp_pipeline.py --phase check")
        print()
        print("═══ READY TO MERGE ═══")
        print("The following are complete and ready to merge:")
        print("  " + ", ".join(complete_entries))
        print("Run: python3 jlp_pipeline.py --phase merge")
    elif chunk_pdfs:
        print("═══ ACTION REQUIRED ═══")
        print("Import these PDFs into PlayScore and export XMLs to the raw folder:")
        for i, cp in enumerate(chunk_pdfs, 1):
            print(f"  {i}. {cp.name}  →  {cp.parent}")
        print(f"When done, re-run: python3 jlp_pipeline.py --phase check")
    elif complete_entries:
        print("═══ ALL COMPLETE ═══")
        print("All exports done. Run: python3 jlp_pipeline.py --phase merge")

    # ── Directory counts ──────────────────────────────────────────────────────
    n_next  = sum(1 for f in NEXT_DIR.iterdir()  if f.suffix.lower() == ".pdf") \
              if NEXT_DIR.exists() else 0
    n_trash = sum(1 for f in TRASH_DIR.rglob("*") if f.is_file()) \
              if TRASH_DIR.exists() else 0
    print(f"\nnext_pdfs/: {n_next} PDF(s) waiting to be imported")
    print(f"trash/:     {n_trash} file(s) archived")


# ─────────────────────────────────────────────────────────────────────────────
# Master measure-count detection
# ─────────────────────────────────────────────────────────────────────────────

def _piano_export_total(cue: str) -> "int | None":
    """
    Count measures from the merged piano MXL (if it exists) or from complete
    raw piano XMLs.  Returns None when no piano data is available yet.
    """
    # Prefer the merged MXL — scan MERGED_DIR for any file matching the cue
    if MERGED_DIR.exists():
        pat = re.compile(r"^JLP\.piano\." + re.escape(cue) + r"\.", re.IGNORECASE)
        for f in MERGED_DIR.iterdir():
            if f.suffix.lower() == ".mxl" and pat.match(f.name):
                try:
                    tree  = load_xml(f)
                    root  = tree.getroot()
                    parts = root.findall("part")
                    if parts:
                        return len(parts[0].findall("measure"))
                except Exception:
                    pass

    # Fall back to raw XMLs when piano is marked complete in check state
    state = load_state()
    if not state.get(f"piano.{cue}", {}).get("complete"):
        return None
    raw_piano = group_raw_xmls(RAW_DIR, filter_inst="piano", filter_cue=cue)
    for (pi, pc, _), parts_list in raw_piano.items():
        if pi == "piano" and pc == cue:
            total = 0
            for xml_path, _ in parts_list:
                try:
                    root = ET.parse(str(xml_path)).getroot()
                    part, _ = extract_primary_part(root, "piano")
                    if part is None:
                        all_parts = root.findall("part")
                        part = all_parts[0] if all_parts else None
                    if part is not None and len(part):
                        total += len(part.findall("measure"))
                except Exception:
                    pass
            return total or None
    return None


def _piano_pdf_ocr_total(cue: str) -> "int | None":
    """OCR the piano source PDF last page for the highest measure number. Cap at 400."""
    if not _OCR_AVAILABLE:
        return None
    piano_pdf = _find_source_pdf("piano", cue, "")
    if piano_pdf is None:
        return None
    try:
        doc        = fitz.open(str(piano_pdf))
        last_0     = _last_music_page_0(doc, piano_pdf.name)
        for back in range(min(3, last_0 + 1)):
            candidate = _ocr_page_max_measure(doc[last_0 - back], full_page=(back > 0))
            if candidate is not None:
                doc.close()
                return min(candidate, 400)
        doc.close()
    except Exception:
        pass
    return None


def _resolve_cue_total(
    cue:               str,
    inst:              str,
    inst_total_measures: int,
    totals:            dict,
    state:             dict,
) -> "tuple[int | None, str]":
    """
    Determine total measures for a cue using the four-level priority chain.
    Updates totals in-place for discovered values (caller saves).

    Priority:
      1. manual         — set via --set-total; never overwritten here
      2. piano_export   — merged or complete raw piano XMLs; or the piano being merged now
      3. piano_ocr      — OCR of piano source PDF
      4. ocr_estimate   — per-instrument OCR from check-phase state
      (estimated_pages  — total_pages × 30 last-resort fallback, not persisted)

    Returns (total_int_or_None, source_string).
    """
    existing = totals.get(cue, {})

    # 1. Manual — highest priority, immutable
    if existing.get("source") == "manual" and "total" in existing:
        return existing["total"], "manual"

    # 2. Piano export — if we're merging piano right now, this IS the count
    if inst == "piano":
        totals[cue] = {"total": inst_total_measures, "source": "piano_export"}
        print(f"   Cue {cue} total: {inst_total_measures} measures (from piano export)")
        return inst_total_measures, "piano_export"

    if existing.get("source") == "piano_export" and "total" in existing:
        return existing["total"], "piano_export"

    piano_exp = _piano_export_total(cue)
    if piano_exp is not None:
        totals[cue] = {"total": piano_exp, "source": "piano_export"}
        print(f"   Cue {cue} total: {piano_exp} measures (from piano export)")
        return piano_exp, "piano_export"

    # 3. Piano PDF OCR
    if existing.get("source") == "piano_ocr" and "total" in existing:
        return existing["total"], "piano_ocr"

    piano_ocr = _piano_pdf_ocr_total(cue)
    if piano_ocr is not None:
        totals[cue] = {"total": piano_ocr, "source": "piano_ocr"}
        print(f"   Cue {cue} total: ~{piano_ocr} measures (OCR from piano PDF)")
        return piano_ocr, "piano_ocr"

    # 4. Per-instrument OCR cached from check phase
    score_m = state.get(f"{inst}.{cue}", {}).get("last_score_measure")
    if score_m:
        return score_m, "ocr_estimate"

    # Last-resort page estimate (not persisted — too coarse)
    total_pages = state.get(f"{inst}.{cue}", {}).get("total_pages")
    if total_pages:
        return total_pages * 30, "estimated_pages"

    return None, "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Phase: merge
# ─────────────────────────────────────────────────────────────────────────────

def _merge_group(inst: str, cue: str, title: str, xml_paths: list):
    """
    Merge xml_paths into a single MXL in MERGED_DIR.
    Returns (out_path, total_measures, uncertain_measure_nums) on success,
    or (None, 0, []) on error.
    uncertain_measure_nums is a sorted list of integer measure numbers that
    contain at least one note with a duration type containing '?'.
    """
    out_path = MERGED_DIR / f"JLP.{inst}.{cue}.{title}.mxl"
    src_has_tempo = False
    raw_parts = []

    for idx, path in enumerate(xml_paths):
        try:
            tree = load_xml(path)
        except Exception as exc:
            print(f"   [error] loading {path.name}: {exc}", file=sys.stderr)
            return None, 0, []
        root = tree.getroot()
        part, pname = extract_primary_part(root, inst)
        if part is None:
            print(f"   [error] no <part> in {path.name}", file=sys.stderr)
            return None, 0, []
        if idx == 0:
            src_has_tempo = _has_tempo(root)
        raw_parts.append((deepcopy(part), path))

    # Merge parts into sequential measure stream
    merged   = ET.Element("part", id="P1")
    next_num = 1
    for part, path in raw_parts:
        measures = part.findall("measure")
        if next_num > 1 and measures:
            for pr in measures[0].findall("print"):
                pr.attrib.pop("new-system", None)
                pr.attrib.pop("new-page",   None)
        for i, m in enumerate(measures):
            if (m.get("number") or "").strip() == "?":
                print(f"   [warning] measure '?' in {path.name} → {next_num + i}")
            m.set("number", str(next_num + i))
            merged.append(deepcopy(m))
        next_num += len(measures)
    total_measures = next_num - 1

    # Octave correction
    med, shifted = apply_octave_correction(merged, inst)
    if shifted:
        print(f"   [octave-fix] median MIDI {med:.1f} > {OCTAVE_CEILINGS[inst]}; "
              f"shifted down 1 octave")

    # Collect uncertain measure numbers (duration type contains '?')
    uncertain = []
    for raw_mn in find_unknown_duration_measures(merged):
        print(f"   [warning] unresolved duration in measure {raw_mn}")
        try:
            uncertain.append(int(raw_mn))
        except (ValueError, TypeError):
            pass
    uncertain.sort()

    # Switch detection
    for mn, pat, prog in detect_and_inject_switches(merged, inst):
        print(f"   Switch: {inst} cue {cue} m{mn} → {pat} (GM {prog})")

    # Tempo
    if not src_has_tempo:
        bpm = CUE_TEMPOS.get(cue)
        if bpm is not None:
            inject_tempo(merged, 1, bpm)
            print(f"   [tempo] injected {bpm} BPM from cue table")
        else:
            print(f"   [warning] no tempo for cue {cue}; none injected")

    score = ET.Element("score-partwise", version="3.1")
    pl    = ET.SubElement(score, "part-list")
    ch    = 10 if inst == "percussion" else 1
    pl.append(make_score_part_el("P1", inst, ch))
    score.append(merged)

    size = write_mxl(ET.ElementTree(score), out_path)
    print(f"   → {out_path.name}  ({total_measures} measures, {size:,} bytes)")
    return out_path, total_measures, uncertain


def phase_merge(args):
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)

    groups = group_raw_xmls(RAW_DIR, filter_inst=args.instrument, filter_cue=args.cue)
    if not groups:
        print(f"No JLP-named XML files found in: {RAW_DIR}")
        return

    state     = load_state()
    punchlist = load_punchlist()
    totals    = load_totals()
    answers   = load_answers()
    merged_count  = 0
    skipped_count = 0

    print(f"\nMerging {RAW_DIR} → {MERGED_DIR}\n")

    for (inst, cue, title), parts in sorted(groups.items()):
        out_path = MERGED_DIR / f"JLP.{inst}.{cue}.{title}.mxl"

        if out_path.exists() and not args.force:
            print(f"── {inst}/{cue}  skipped (already exists: {out_path.name})")
            skipped_count += 1
            continue

        cached = state.get(f"{inst}.{cue}", {})
        if not cached.get("complete") and not args.force:
            print(f"── {inst}/{cue}  skipped (not marked complete — run --phase check first)")
            skipped_count += 1
            continue

        xml_paths = [p for p, _ in parts]
        print(f"── {inst} / cue {cue}  ({len(xml_paths)} part(s))")
        out_path_result, total_measures, uncertain = _merge_group(inst, cue, title, xml_paths)
        if not out_path_result:
            # Merge failed (unrecoverable parse error) — record as malformed
            est_total = state.get(f"{inst}.{cue}", {}).get("total_pages", 1) * 30
            punchlist.setdefault(cue, {})["_title"] = title
            punchlist[cue][inst] = {
                "malformed":        True,
                "captured_through": 0,
                "missing":          [[1, est_total]],
                "uncertain":        [],
                "total":            est_total,
                "total_source":     "estimated_pages",
            }
            save_punchlist(punchlist)
        if out_path_result:
            merged_count += 1
            TRASH_DIR.mkdir(parents=True, exist_ok=True)
            for xml_path in xml_paths:
                dest = TRASH_DIR / xml_path.name
                shutil.move(str(xml_path), str(dest))
                print(f"   Moved to trash/: {xml_path.name}")
            # Move matching chunk PDFs from trash/ into trash/merged/
            TRASH_MERGED_DIR.mkdir(parents=True, exist_ok=True)
            chunk_pdf_pat = re.compile(
                r"^JLP\." + re.escape(inst) + r"\." + re.escape(cue) + r"\.",
                re.IGNORECASE,
            )
            for f in sorted(TRASH_DIR.iterdir()):
                if f.suffix.lower() == ".pdf" and chunk_pdf_pat.match(f.name):
                    shutil.move(str(f), str(TRASH_MERGED_DIR / f.name))
                    print(f"   Moved to trash/merged/: {f.name}")

            # ── Punchlist: resolve total and compute missing ranges ──────────
            score_total, total_source = _resolve_cue_total(
                cue, inst, total_measures, totals, state
            )
            save_totals(totals)   # persist any newly discovered total

            # Trigger 1: total still unknown — ask user
            if score_total is None:
                q_key = f"cue:{cue}:total_measures"
                ans = _ask_question(
                    key=q_key,
                    context=f"cue {cue}",
                    description=[
                        f"Could not determine total measure count for cue {cue}.",
                        f"No piano export, piano PDF OCR, or per-instrument estimate",
                        f"is available.",
                    ],
                    question=f"How many total measures does cue {cue} have?",
                    hint="Count the measures in the full score or check the conductor score",
                    answers=answers,
                    args=args,
                )
                if ans and ans.strip().lstrip("-").isdigit():
                    score_total  = int(ans.strip())
                    total_source = "user_answer"
                    totals[cue]  = {"total": score_total, "source": "user_answer"}
                    save_totals(totals)
                    print(f"   Cue {cue} total: {score_total} measures (user answer)")

            missing = []
            if score_total and score_total > total_measures:
                missing = [[total_measures + 1, score_total]]

            # Flag short cues so the punchlist can suggest manual MIDI entry
            total_pages_cached = state.get(f"{inst}.{cue}", {}).get("total_pages", 0)
            if total_pages_cached <= 2 and score_total and score_total < 10:
                punchlist.setdefault(cue, {})["_short_cue"] = True

            punchlist.setdefault(cue, {})["_title"] = title
            punchlist[cue][inst] = {
                "captured_through": total_measures,
                "missing":          missing,
                "uncertain":        uncertain,
                "total":            score_total or total_measures,
                "total_source":     total_source,
            }
            save_punchlist(punchlist)
        print()

    print(f"Summary: {merged_count} merged, {skipped_count} skipped")
    if merged_count:
        print("Run --phase assemble to build full scores.")


# ─────────────────────────────────────────────────────────────────────────────
# Phase: assemble
# ─────────────────────────────────────────────────────────────────────────────

def phase_assemble(args):
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)

    cue_groups = group_merged_mxls(MERGED_DIR, filter_cue=args.cue)
    if not cue_groups:
        print(f"No merged MXLs found in: {MERGED_DIR}")
        return

    assembled_count = 0
    skipped_count   = 0

    print(f"\nAssembling {MERGED_DIR} → {ASSEMBLED_DIR}\n")

    for (cue, title), inst_files in sorted(cue_groups.items()):
        out_path = ASSEMBLED_DIR / f"JLP.{cue}.{title}.full.mxl"

        if out_path.exists() and not args.force:
            print(f"── cue {cue}  skipped ({out_path.name} already exists)")
            skipped_count += 1
            continue

        inst_list = [i for i, _ in inst_files]
        missing   = [i for i in INSTRUMENTS if i not in inst_list]
        if missing and not args.force:
            print(f"── cue {cue}  skipped "
                  f"(missing {len(missing)} instrument(s): {', '.join(missing)})")
            print(f"      Use --force to assemble with available instruments only.")
            skipped_count += 1
            continue
        print(f"── cue {cue}  ({len(inst_files)} instrument(s): {', '.join(inst_list)})")

        loaded = []
        error  = False
        for inst, path in inst_files:
            try:
                tree = load_xml(path)
            except Exception as exc:
                print(f"   [error] loading {path.name}: {exc}", file=sys.stderr)
                error = True
                break
            root  = tree.getroot()
            parts = root.findall("part")
            if not parts:
                print(f"   [error] no <part> in {path.name}", file=sys.stderr)
                error = True
                break
            loaded.append((inst, deepcopy(parts[0])))

        if error:
            print()
            continue

        max_m = max(len(p.findall("measure")) for _, p in loaded)
        score = ET.Element("score-partwise", version="3.1")
        pl    = ET.SubElement(score, "part-list")

        for idx, (inst, part) in enumerate(loaded):
            pid = f"P{idx + 1}"
            ch  = 10 if inst == "percussion" else 1
            pl.append(make_score_part_el(pid, inst, ch))
            current = len(part.findall("measure"))
            if current < max_m:
                beats, beat_type, divisions = _last_time_sig(part)
                for mn in range(current + 1, max_m + 1):
                    part.append(_make_rest_measure(mn, beats, beat_type, divisions))
                print(f"   padded {inst}: +{max_m - current} rest measure(s)")
            part.set("id", pid)
            score.append(part)

        size = write_mxl(ET.ElementTree(score), out_path)
        print(f"   → {out_path.name}  ({max_m} measures, {size:,} bytes)")
        assembled_count += 1
        print()

    print(f"Summary: {assembled_count} assembled, {skipped_count} skipped")


# ─────────────────────────────────────────────────────────────────────────────
# Phase: punchlist
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_title(raw: str) -> str:
    return raw.replace("_", " ").upper()


def _parse_measure_range(s: str) -> "tuple[int,int] | None":
    """Parse '107' → (107,107) or '100-120' → (100,120). Returns None on error."""
    s = s.strip()
    if "-" in s:
        parts = s.split("-", 1)
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None
    try:
        n = int(s)
        return n, n
    except ValueError:
        return None


def _inst_overlaps_range(entry: dict, lo: int, hi: int) -> "str | None":
    """
    Return 'missing' if a missing range overlaps [lo,hi],
    'uncertain' if an uncertain measure falls in [lo,hi], else None.
    Malformed entries are treated as fully missing.
    """
    if entry.get("malformed"):
        return "missing"
    for start, end in entry.get("missing", []):
        if start <= hi and end >= lo:
            return "missing"
    for m in entry.get("uncertain", []):
        if lo <= m <= hi:
            return "uncertain"
    return None


def _render_cue_punchlist(cue: str, cue_data: dict, out: list, inst_filter: "str | None" = None):
    """Append formatted punchlist lines for one cue to out.
    If inst_filter is given only that instrument is shown."""
    title_raw = cue_data.get("_title", "")
    header = f"CUE {cue}"
    if title_raw:
        header += f" - {_fmt_title(title_raw)}"
    header += " — PUNCH LIST"
    if inst_filter:
        header += f" ({inst_filter})"

    bar = "═" * (len(header) + 8)
    out.append(bar)
    out.append(f"═══ {header} ═══")
    out.append(bar)
    out.append("")

    if cue_data.get("_short_cue"):
        out.append("[note] Short cue — may be faster to enter all measures manually "
                   "in MuseScore than to re-export from PlayScore")
        out.append("")

    needs_work:     list = []
    malformed_insts: list = []
    complete_insts: list = []
    not_exported:   list = []

    visible = [inst_filter] if inst_filter else INSTRUMENTS
    for inst in visible:
        if inst not in cue_data:
            not_exported.append(inst)
            continue
        entry = cue_data[inst]
        if entry.get("malformed"):
            malformed_insts.append(inst)
            continue
        n_missing   = sum(end - start + 1 for start, end in entry.get("missing",   []))
        n_uncertain = len(entry.get("uncertain", []))
        if n_missing == 0 and n_uncertain == 0:
            complete_insts.append(inst)
        else:
            needs_work.append((inst, entry, n_missing, n_uncertain))

    if malformed_insts:
        out.append("Malformed exports — re-export required:")
        out.append("")
        for inst in malformed_insts:
            out.append(f"  {inst.upper()}")
            out.append(f"    MALFORMED EXPORT — all measures missing, re-export required")
            out.append("")

    if needs_work:
        out.append("Instruments requiring manual MIDI entry:")
        out.append("")
        for inst, entry, n_missing, n_uncertain in needs_work:
            out.append(f"  {inst.upper()}")
            captured = entry.get("captured_through", 0)
            out.append(f"    Captured:   m1–m{captured}")
            for start, end in entry.get("missing", []):
                n = end - start + 1
                out.append(f"    Missing:    m{start}–m{end}  ({n} measures — end of score not exported)")
            uncertain = entry.get("uncertain", [])
            if uncertain:
                unc_str = ", ".join(f"m{n}" for n in uncertain)
                out.append(f"    Uncertain:  {unc_str}  (rhythm unresolved — verify)")
            out.append("")

    if not malformed_insts and not needs_work:
        out.append("No instruments require manual MIDI entry.")
        out.append("")

    if complete_insts:
        out.append("Instruments complete:")
        out.append(f"  {', '.join(complete_insts)} ✓")
        out.append("")

    if not_exported:
        out.append("Not yet exported:")
        out.append(f"  {', '.join(not_exported)}")
        out.append("")

    if needs_work or malformed_insts:
        parts_str: list = []
        total_manual = 0
        if needs_work:
            total_miss = sum(m for _, _, m, _ in needs_work)
            total_unc  = sum(u for _, _, _, u in needs_work)
            if total_miss:
                parts_str.append(f"{total_miss} missing")
            if total_unc:
                parts_str.append(f"{total_unc} uncertain")
            total_manual = total_miss + total_unc
        if malformed_insts:
            parts_str.append(f"{len(malformed_insts)} instrument(s) fully missing (malformed)")
        if total_manual:
            out.append(f"Total manual measures needed: {' + '.join(parts_str)} = {total_manual} measures")
        else:
            out.append(f"Total manual measures needed: {' + '.join(parts_str)}")


def _render_measure_view(cue: str, cue_data: dict, lo: int, hi: int, out: list,
                         inst_filter: "str | None" = None):
    """Append measure-centric view for one cue showing per-instrument status in [lo,hi]."""
    title_raw = cue_data.get("_title", "")
    range_str = f"m{lo}" if lo == hi else f"m{lo}-{hi}"
    cue_label = f"CUE {cue}"
    if title_raw:
        cue_label += f" {_fmt_title(title_raw)}"
    header = f"{cue_label} — {range_str.upper()} — ALL INSTRUMENTS"

    bar = "═" * (len(header) + 8)
    out.append(bar)
    out.append(f"═══ {header} ═══")
    out.append(bar)
    out.append("")
    out.append(f"  {range_str}:")

    visible   = [inst_filter] if inst_filter else INSTRUMENTS
    name_w    = max(len(inst.capitalize()) for inst in visible) + 1   # +1 for ":"
    for inst in visible:
        name = inst.capitalize() + ":"
        if inst not in cue_data:
            status = "not exported"
        else:
            overlap = _inst_overlaps_range(cue_data[inst], lo, hi)
            if overlap == "missing":
                status = "MISSING (not exported)"
            elif overlap == "uncertain":
                status = "UNCERTAIN (verify)"
            else:
                status = "complete ✓"
        out.append(f"    {name:<{name_w}}  {status}")

    out.append("")
    out.append("  Useful when working in MuseScore on the assembled score —")
    out.append("  look up any measure range to see which instruments need attention there.")


def _render_summary(punchlist: dict, out: list, cues: list, inst_filter: "str | None" = None):
    """Append compact one-line-per-cue summary to out."""
    header = "═══ PUNCH LIST SUMMARY ═══"
    if inst_filter:
        header = f"═══ PUNCH LIST SUMMARY — {inst_filter.upper()} ═══"
    out.append(header)
    out.append("")

    # Pre-compute label widths for alignment
    labels = []
    for cue in cues:
        title_raw = punchlist[cue].get("_title", "")
        label = f"Cue {cue}"
        if title_raw:
            label += f" {_fmt_title(title_raw)}"
        labels.append(label)
    col_w = max(len(lb) for lb in labels) + 1   # +1 for ":"

    show_miss = 0
    show_unc  = 0

    for label, cue in zip(labels, cues):
        cue_data = punchlist[cue]
        visible  = [inst_filter] if inst_filter else INSTRUMENTS

        inst_issues: list = []
        has_data = False
        cue_miss = 0
        cue_unc  = 0

        for inst in visible:
            if inst not in cue_data:
                continue
            has_data = True
            entry    = cue_data[inst]
            n_miss   = sum(end - start + 1 for start, end in entry.get("missing", []))
            n_unc    = len(entry.get("uncertain", []))
            cue_miss += n_miss
            cue_unc  += n_unc
            if n_miss or n_unc:
                issue_parts: list = []
                for start, end in entry.get("missing", []):
                    issue_parts.append(f"m{start}-{end}")
                if n_unc and not entry.get("missing"):
                    issue_parts.append(f"({n_unc} uncertain)")
                elif n_unc:
                    issue_parts.append(f"+{n_unc} uncertain")
                inst_issues.append(f"{inst} {' '.join(issue_parts)}")

        show_miss += cue_miss
        show_unc  += cue_unc
        total_manual = cue_miss + cue_unc

        col = (label + ":").ljust(col_w)
        if inst_issues:
            suffix = f" ({total_manual} measures total)" if total_manual else ""
            out.append(f"  {col}  {', '.join(inst_issues)}{suffix}")
        elif has_data:
            out.append(f"  {col}  all complete ✓")
        else:
            out.append(f"  {col}  pending export")

    out.append("")
    out.append(f"Total across show: {show_miss} missing, {show_unc} uncertain")


def phase_punchlist(args):
    punchlist = load_punchlist()

    if not punchlist:
        print("No punchlist data yet. Run --phase merge to generate it.")
        return

    out: list = []

    # Parse --measure if provided
    measure_range: "tuple[int,int] | None" = None
    if getattr(args, "measure", None):
        measure_range = _parse_measure_range(args.measure)
        if measure_range is None:
            print(f"[error] --measure: expected N or N-M, got {args.measure!r}", file=sys.stderr)
            sys.exit(1)

    cue_filter  = args.cue.upper() if args.cue else None
    inst_filter = args.instrument   # str or None; already validated by argparse

    # Determine which cues to process
    if cue_filter:
        if cue_filter not in punchlist:
            print(f"No punchlist data for cue {cue_filter}. "
                  f"Run --phase merge --cue {cue_filter} first.")
            return
        cues = [cue_filter]
    else:
        cues = sorted(punchlist.keys(), key=_cue_sort)

    # Choose rendering mode
    if measure_range:
        lo, hi = measure_range
        for i, cue in enumerate(cues):
            if i:
                out.append("")
            _render_measure_view(cue, punchlist[cue], lo, hi, out, inst_filter)

    elif getattr(args, "summary", False) or (not cue_filter and not inst_filter):
        # Compact summary: explicit --summary flag OR bare invocation with no filters
        _render_summary(punchlist, out, cues, inst_filter)

    else:
        # Detailed view filtered by cue and/or instrument
        for i, cue in enumerate(cues):
            if i:
                out.append("")
            _render_cue_punchlist(cue, punchlist[cue], out, inst_filter)

    text = "\n".join(out).rstrip()
    print(text)

    txt_path = EXPORTS_DIR / "punchlist.txt"
    try:
        txt_path.write_text(text + "\n")
        print(f"\n(Saved to {txt_path.name})")
    except OSError as exc:
        print(f"\n[warning] Could not save punchlist.txt: {exc}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# Phase: status
# ─────────────────────────────────────────────────────────────────────────────

def _cue_sort(c: str):
    m = re.match(r"^(\d+)([A-Z]*)$", c.upper())
    return (int(m.group(1)), m.group(2)) if m else (9999, c)


def phase_status(args):
    state = load_state()

    raw_groups   = group_raw_xmls(RAW_DIR)
    trash_groups = group_raw_xmls(TRASH_DIR) if TRASH_DIR.exists() else {}
    merged_groups = group_merged_mxls(MERGED_DIR)

    merged_set: set = set()
    for (cue, _), inst_files in merged_groups.items():
        for inst, _ in inst_files:
            merged_set.add((inst, cue))
    # XMLs in trash/ were successfully merged — count them even if the
    # merged MXL was manually removed.
    for inst, cue, _title in trash_groups:
        merged_set.add((inst, cue))

    assembled_cues: set = set()
    if ASSEMBLED_DIR.exists():
        for f in ASSEMBLED_DIR.iterdir():
            parsed = parse_assembled_name(f)
            if parsed:
                assembled_cues.add(parsed[0])

    # Always show all 44 defined cues; add any extra from actual data
    all_cues: set = set(CUE_TEMPOS.keys())
    for inst, cue, title in raw_groups:
        all_cues.add(cue)
    for inst, cue, title in trash_groups:
        all_cues.add(cue)
    for cue, title in merged_groups:
        all_cues.add(cue)
    all_cues.update(assembled_cues)
    sorted_cues = sorted(all_cues, key=_cue_sort)

    if args.cue:
        sorted_cues = [c for c in sorted_cues if c == args.cue.upper()]

    def cell(inst: str, cue: str) -> str:
        """Return display value for one (instrument, cue) cell."""
        if (inst, cue) in merged_set and cue in assembled_cues:
            return "DONE"
        if (inst, cue) in merged_set:
            return "MERGED"
        raw_parts = [
            s for (i, c, _t), parts in raw_groups.items()
            if i == inst and c == cue
            for _p, s in parts
        ]
        if raw_parts:
            complete = state.get(f"{inst}.{cue}", {}).get("complete", False)
            if complete:
                return f"READY({len(raw_parts)})"
            return f"PARTIAL({len(raw_parts)})"
        return "—"

    def full_cell(cue: str) -> str:
        return "DONE" if cue in assembled_cues else "—"

    # Pre-compute
    table = {(inst, cue): cell(inst, cue) for cue in sorted_cues for inst in INSTRUMENTS}

    abbrevs = ["bass", "cello", "gtr1", "gtr2", "perc", "viola", "violin", "piano", "Full"]
    cue_w   = 6
    col_w   = 11   # wide enough for PARTIAL(N)

    header = f"  {'Cue':<{cue_w}}" + "".join(f"  {a:<{col_w}}" for a in abbrevs)
    sep    = "  " + "─" * (len(header) - 2)

    print(f"\n{header}")
    print(sep)

    for cue in sorted_cues:
        cells = [table[(inst, cue)] for inst in INSTRUMENTS] + [full_cell(cue)]
        print(f"  {cue:<{cue_w}}" + "".join(f"  {c:<{col_w}}" for c in cells))

    # Totals row: count of MERGED+DONE per instrument column
    print(sep)
    totals = []
    for inst in INSTRUMENTS:
        n = sum(1 for cue in sorted_cues
                if table[(inst, cue)] in ("MERGED", "DONE"))
        totals.append(str(n))
    totals.append(str(len(assembled_cues)))   # Full column
    print(f"  {'':>{cue_w}}" + "".join(f"  {t:<{col_w}}" for t in totals))
    print(sep)

    complete_cells = sum(
        1 for v in table.values() if v in ("MERGED", "DONE")
    )
    total_cells = len(INSTRUMENTS) * len(CUE_TEMPOS)
    print(f"\n  {complete_cells}/{total_cells} instrument-cues complete "
          f"({len(CUE_TEMPOS)} cues × {len(INSTRUMENTS)} instruments)")
    print(f"  Dirs: raw={RAW_DIR}")
    print(f"        merged={MERGED_DIR}")
    print(f"        assembled={ASSEMBLED_DIR}")


# ─────────────────────────────────────────────────────────────────────────────
# Cache management
# ─────────────────────────────────────────────────────────────────────────────

def _do_set_total(tokens: list):
    """Process 'CUE:N ...' tokens from --set-total and persist to .jlp_totals.json."""
    totals = load_totals()
    for token in tokens:
        if ":" not in token:
            print(f"[warning] --set-total: expected CUE:N, got {token!r}")
            continue
        cue_raw, n_raw = token.split(":", 1)
        cue = cue_raw.upper()
        try:
            n = int(n_raw)
        except ValueError:
            print(f"[warning] --set-total: {n_raw!r} is not a number, skipping {token!r}")
            continue
        totals[cue] = {"total": n, "source": "manual"}
        print(f"Set cue {cue} total: {n} measures (manual)")
    save_totals(totals)


def _do_show_totals():
    """Print all cue totals with their source."""
    totals    = load_totals()
    punchlist = load_punchlist()

    all_cues = sorted(set(CUE_TEMPOS.keys()) | set(totals.keys()), key=_cue_sort)

    def _cue_title(cue: str) -> str:
        raw = punchlist.get(cue, {}).get("_title", "")
        return _fmt_title(raw) if raw else ""

    _SOURCE_LABELS = {
        "manual":         ("", "manual ✓"),
        "piano_export":   ("", "piano export ✓"),
        "piano_ocr":      ("~", "OCR estimate — piano PDF"),
        "ocr_estimate":   ("~", "OCR estimate"),
        "estimated_pages":("~", "page estimate"),
    }

    rows = []
    for cue in all_cues:
        label = f"Cue {cue}"
        title = _cue_title(cue)
        if title:
            label += f" {title}"
        label += ":"

        entry = totals.get(cue, {})
        if "total" in entry:
            n   = entry["total"]
            src = entry.get("source", "unknown")
            prefix, detail = _SOURCE_LABELS.get(src, ("~", src))
            measure_str = f"{prefix}{n}"
            rows.append((label, measure_str, detail))
        else:
            rows.append((label, "unknown", ""))

    if not rows:
        print("No cue totals recorded yet.")
        return

    col1 = max(len(r[0]) for r in rows)
    col2 = max(len(r[1]) for r in rows)
    print()
    for label, measure_str, detail in rows:
        if detail:
            print(f"  {label:<{col1}}  {measure_str:>{col2}} measures  ({detail})")
        else:
            print(f"  {label:<{col1}}  {measure_str}")
    print()


def _do_show_answers():
    """Print all stored Q&A pairs."""
    answers = load_answers()
    if not answers:
        print("No stored answers yet.")
        return
    print()
    for key in sorted(answers.keys()):
        entry = answers[key]
        q = entry.get("question", "")
        a = entry.get("answer", "")
        print(f"  {key}")
        if q:
            print(f"    Q: {q}")
        print(f"    A: {a!r}")
    print()


def _do_clear_answer(key: str):
    """Remove a specific stored answer so the question is asked again."""
    answers = load_answers()
    if key in answers:
        del answers[key]
        save_answers(answers)
        print(f"Cleared answer for {key!r}. It will be asked again next run.")
    else:
        print(f"No stored answer for {key!r}.")


_PROTECTED_JSONS = frozenset({".jlp_totals.json", ".jlp_answers.json", ".jlp_punchlist.json"})

# Keys stored per instrument+cue inside .jlp_state.json that become stale
# across sessions and must be wiped on --reset-state.
_STATE_STALE_KEYS = frozenset({
    "last_measure_page_0",
    "covered_pages",
    "parts",
    "pending_chunk",
    "last_seen_xml",
    "complete",
    "last_measure",
    "last_score_measure",
    "vs_last_page",
    "pdf_path",
    "total_pages",
})


def _scrub_stale_keys(state: dict) -> dict:
    """Remove per-instrument stale cache keys from every entry in *state* in-place."""
    for entry in state.values():
        if isinstance(entry, dict):
            for k in _STATE_STALE_KEYS:
                entry.pop(k, None)
    return state


def _do_reset_state():
    """
    Clear all persistent state in exports/.

    Two-phase approach for reliability on iCloud Drive where a deleted file
    can be restored by the sync daemon before the next read:

    Phase 1: Delete every non-protected .json file found in EXPORTS_DIR.
             Each deletion is attempted individually so one failure does not
             abort the rest.
    Phase 2: Write an empty {} to STATE_FILE.  Even if the old file reappears
             via cloud sync, the next load_state() call will read {}.
    """
    deleted  = []
    warnings = []
    if EXPORTS_DIR.exists():
        for f in sorted(EXPORTS_DIR.iterdir()):
            if f.is_file() and f.suffix == ".json" and f.name not in _PROTECTED_JSONS:
                try:
                    f.unlink()
                    deleted.append(f.name)
                except OSError as exc:
                    warnings.append(f"{f.name}: {exc}")

    # Guarantee clean state regardless of file-system race conditions.
    try:
        save_state({})
    except OSError:
        pass

    if deleted:
        print(f"State cleared: {', '.join(deleted)}")
    else:
        print("No state files found (nothing to reset).")
    for w in warnings:
        print(f"[warning] Could not delete {w}", file=sys.stderr)


def _do_clear_cache(target: str):
    """Delete cached OCR/measure data for one inst:cue, or wipe the state file for 'all'."""
    if target.strip().lower() == "all":
        _do_reset_state()
        return
    if ":" not in target:
        print(f"[error] --clear-cache expects inst:cue or 'all', got {target!r}",
              file=sys.stderr)
        return
    state = load_state()
    inst, cue = target.split(":", 1)
    key = f"{inst.lower()}.{cue.upper()}"
    if key in state:
        del state[key]
        save_state(state)
        print(f"Cleared cache for {inst.lower()} cue {cue.upper()}.")
    else:
        print(f"No cache entry for {inst.lower()} cue {cue.upper()} (nothing to clear).")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="JLP pit-orchestra MusicXML pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
phases:
  check      scan raw/ vs source PDFs; generate next-chunk PDFs
  merge      merge complete XML sets into per-instrument MXLs
  assemble   combine per-instrument MXLs into full-score MXLs
  status     print progress table (cues × instruments)
  punchlist  show missing/uncertain measures per cue
        """,
    )
    p.add_argument("--phase", default=None,
                   choices=["check", "merge", "assemble", "status", "punchlist"])
    p.add_argument("--cue",        default=None, help="Filter to one cue, e.g. 01  01A")
    p.add_argument("--instrument", default=None, choices=INSTRUMENTS,
                   help="Filter to one instrument")
    p.add_argument("--force", action="store_true",
                   help="Re-process even if output already exists / result cached")
    p.add_argument("--mark-complete", nargs="+", default=[], metavar="INST:CUE",
                   help="Force inst:cue pairs complete, e.g. bass:01 violin:01")
    p.add_argument("--clear-cache", default=None, metavar="INST:CUE|all",
                   help="Clear cached OCR/measure data: 'bass:01' or 'all'")
    p.add_argument("--reset-state", action="store_true",
                   help="Delete the state file completely for a clean start")
    p.add_argument("--measure", default=None, metavar="N|N-M",
                   help="punchlist: show measure-centric view for measure N or range N-M")
    p.add_argument("--summary", action="store_true",
                   help="punchlist: compact one-line-per-cue overview")
    p.add_argument("--set-total", nargs="+", default=[], metavar="CUE:N",
                   help="Set manual total measures: --set-total 01:123 02:210")
    p.add_argument("--show-totals", action="store_true",
                   help="Print all known cue totals and their sources")
    p.add_argument("--no-interactive", action="store_true",
                   help="Skip all interactive questions; use best-guess fallbacks")
    p.add_argument("--show-answers", action="store_true",
                   help="Print all stored Q&A answers")
    p.add_argument("--clear-answer", default=None, metavar="KEY",
                   help="Remove a stored answer so it is asked again, e.g. bass:01:last_measure_page:3")
    return p.parse_args()


def main():
    args = parse_args()

    if args.reset_state:
        _do_reset_state()
        if args.phase is None and args.clear_cache is None and not args.set_total and not args.show_totals:
            return

    if args.set_total:
        _do_set_total(args.set_total)
        if args.phase is None and not args.show_totals:
            return

    if args.show_totals:
        _do_show_totals()
        if args.phase is None:
            return

    if args.show_answers:
        _do_show_answers()
        if args.phase is None:
            return

    if args.clear_answer is not None:
        _do_clear_answer(args.clear_answer)
        if args.phase is None:
            return

    if args.clear_cache is not None:
        _do_clear_cache(args.clear_cache)
        if args.phase is None:
            return

    if args.phase is None:
        print("error: --phase is required", file=sys.stderr)
        sys.exit(1)

    dispatch = {
        "check":     phase_check,
        "merge":     phase_merge,
        "assemble":  phase_assemble,
        "status":    phase_status,
        "punchlist": phase_punchlist,
    }
    dispatch[args.phase](args)


if __name__ == "__main__":
    main()
