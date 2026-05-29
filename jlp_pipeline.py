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
import sys
import zipfile
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path
from statistics import median as _median

from jlp_common import (
    SOURCE_DIR, EXPORTS_DIR, RAW_DIR, NEXT_DIR, MERGED_DIR, ASSEMBLED_DIR,
    STATE_FILE, ALL_DIRS, INSTRUMENTS, CUE_TEMPOS, GM_DEFAULT,
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

OCTAVE_CEILINGS = {
    "bass":   52,   # E3
    "cello":  72,   # C5
    "viola":  72,
    "violin": 72,
}

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
    # Last part is a suffix if it's a single lowercase letter >= 'b'
    if (
        len(parts) >= 5
        and len(parts[-1]) == 1
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

    # Match cue — longest first so "01A" is tried before "01"
    cue = None
    for candidate in sorted(CUE_TEMPOS.keys(), key=len, reverse=True):
        if rest.upper().startswith(candidate.upper()):
            cue = candidate
            rest = rest[len(candidate):]
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
    """None → 'b', 'b' → 'c', 'c' → 'd', ..."""
    return "b" if suffix is None else chr(ord(suffix) + 1)


def group_raw_xmls(raw_dir: Path, filter_inst=None, filter_cue=None) -> dict:
    """
    Scan raw_dir for XML exports, group by (inst, cue, canonical_title).

    Accepts both naming conventions:
    - Standard JLP:  JLP.bass.00.OVERTURE.xml  (dots preserved)
    - PlayScore:     JLPbass00OVERTURE.xml      (dots stripped by PlayScore on save)

    For PlayScore names the canonical title is resolved from the matching source
    PDF so all downstream naming uses the correct base name.

    Returns {(inst, cue, title): [(path, suffix), ...]} sorted by suffix.
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

        if filter_inst and inst != filter_inst:
            continue
        if filter_cue and cue != filter_cue.upper():
            continue
        groups.setdefault((inst, cue, title), []).append((f, suffix))

    for key in groups:
        groups[key].sort(key=lambda x: suffix_sort_key(x[1]))
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

def load_xml(path) -> ET.ElementTree:
    """Parse a plain .xml or compressed .mxl file."""
    p = str(path)
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
                for name in zf.namelist():
                    if name.endswith(".xml") and "META-INF" not in name:
                        root_entry = name
                        break
            if root_entry is None:
                raise ValueError(f"No MusicXML content in {p}")
            return ET.parse(io.BytesIO(zf.read(root_entry)))
    return ET.parse(p)


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
    """Return the highest measure/@number in the file."""
    root = ET.parse(str(xml_path)).getroot()
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


def _ocr_page_max_measure(page) -> "int | None":
    """
    OCR the bottom 20% and top 15% of a page (fitz auto-corrects /Rotate).
    Returns the highest standalone integer 1-999 found — the last measure number
    visible on that page.  Used to read total measure count from the last PDF page.
    """
    mat = fitz.Matrix(OCR_DPI / 72, OCR_DPI / 72)
    pix = page.get_pixmap(matrix=mat)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    w, h = img.size

    regions = [
        img.crop((0, int(h * 0.80), w, h)),   # bottom 20%
        img.crop((0, 0, w, int(h * 0.15))),    # top 15% (rehearsal numbers)
    ]
    nums = []
    for region in regions:
        text = pytesseract.image_to_string(region, config="--psm 11")
        for m in re.finditer(r"(?<!\d)\d{1,3}(?!\d)", text):
            n = int(m.group())
            if 1 <= n <= 999:
                nums.append(n)
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


def extract_pages_fixed(src_path: Path, from_page_0: int, output_path: Path) -> int:
    """
    Extract pages [from_page_0 .. end] to output_path.
    Pages with /Rotate metadata are re-rendered (rotation baked in) so PlayScore
    receives upright pages with no rotation flag.
    Returns the number of pages written.
    """
    src   = fitz.open(str(src_path))
    total = len(src)
    out   = fitz.open()

    for idx in range(from_page_0, total):
        src_page = src[idx]
        if src_page.rotation == 0:
            out.insert_pdf(src, from_page=idx, to_page=idx)
        else:
            # fitz.get_pixmap auto-corrects /Rotate → bake result into plain image page
            mat     = fitz.Matrix(2.0, 2.0)   # render at 2× for quality
            pix     = src_page.get_pixmap(matrix=mat)
            w, h    = pix.width / 2.0, pix.height / 2.0
            new_pg  = out.new_page(width=w, height=h)
            new_pg.insert_image(fitz.Rect(0, 0, w, h), pixmap=pix)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(output_path))
    src.close()
    out.close()
    return total - from_page_0


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

    groups = group_raw_xmls(RAW_DIR, filter_inst=args.instrument, filter_cue=args.cue)

    if not groups:
        print(f"No JLP-named XML files found in: {RAW_DIR}")
        print("Drop your PlayScore XML exports there and re-run.")
        return

    state            = load_state()
    complete_count   = 0
    incomplete_count = 0

    print(f"\nFound {len(groups)} instrument/cue group(s) in {RAW_DIR}\n")

    for (inst, cue, title), parts in sorted(groups.items()):
        latest_path, latest_suffix = parts[-1]
        print(f"── {inst} / cue {cue}  ({len(parts)} part(s))")

        try:
            last_m = last_measure_number(latest_path)
        except ValueError as exc:
            print(f"   [error] {exc}")
            print()
            incomplete_count += 1
            continue

        print(f"   Latest : {latest_path.name}  (last measure: m{last_m})")

        state_key = f"{inst}.{cue}"
        cached    = state.setdefault(state_key, {})

        pdf_path = _find_source_pdf(inst, cue, title)
        if pdf_path is None:
            print(f"   [warning] Source PDF not found.")
            print(f"   Expected: {SOURCE_DIR}/{inst}/JLP.{inst}.{cue}.{title}.pdf")
            print()
            incomplete_count += 1
            continue
        print(f"   Source PDF : {pdf_path.name}")

        # ── Step 1: OCR last page to get total measure count (cheap, cached) ───
        last_score_m = None
        if (
            not args.force
            and cached.get("pdf_path") == str(pdf_path)
            and "last_score_measure" in cached
        ):
            last_score_m = cached["last_score_measure"]
            print(f"   (cached) score total: ~m{last_score_m}")
        else:
            doc = fitz.open(str(pdf_path))
            total_pages = len(doc)
            last_score_m = _ocr_page_max_measure(doc[-1])
            doc.close()
            if last_score_m is not None:
                print(f"   Score total: ~m{last_score_m} (last-page OCR)")
                cached["last_score_measure"] = last_score_m
                cached["pdf_path"]           = str(pdf_path)
                cached["total_pages"]        = total_pages
                save_state(state)
            else:
                print(f"   [warning] Could not read measure count from last page of "
                      f"{pdf_path.name} — will scan forward.")

        # ── Step 2: Determine completeness ────────────────────────────────────
        if last_score_m is not None and last_m >= last_score_m:
            print(f"   ✓ Complete — m{last_m} >= m{last_score_m} (score total). Ready to merge.")
            cached["complete"]     = True
            cached["last_measure"] = last_m
            save_state(state)
            complete_count += 1
            print()
            continue

        # ── Step 3: Incomplete — find cutoff page, extract remaining pages ────
        # Use cached cutoff-page result if last_m hasn't changed
        if (
            not args.force
            and cached.get("last_measure") == last_m
            and "last_measure_page_0" in cached
        ):
            page_0      = cached["last_measure_page_0"]
            total_pages = cached.get("total_pages", cached["last_measure_page_0"] + 1)
            print(f"   (cached) m{last_m} on page {page_0 + 1}/{total_pages}")
        else:
            page_0, total_pages = scan_pdf_for_measure(pdf_path, last_m)
            if page_0 is None:
                print(f"   [warning] Could not locate m{last_m} in {pdf_path.name}.")
                print()
                incomplete_count += 1
                continue
            cached["last_measure"]        = last_m
            cached["last_measure_page_0"] = page_0
            cached["total_pages"]         = total_pages
            cached["complete"]            = False
            save_state(state)

        # If the cutoff page is the last page, nothing remains — mark complete.
        # (Happens when last-page OCR returns a number slightly above last_m due to
        # other numbers visible on the page, but the XML actually covers everything.)
        if page_0 + 1 >= total_pages:
            print(f"   ✓ Complete — m{last_m} is on the last page. Ready to merge.")
            cached["complete"]     = True
            cached["last_measure"] = last_m
            save_state(state)
            complete_count += 1
            print()
            continue

        # Extract remaining pages after the cutoff page
        next_suf   = next_suffix(latest_suffix)
        chunk_name = f"JLP.{inst}.{cue}.{title}.{next_suf}.pdf"
        chunk_path = NEXT_DIR / chunk_name
        n_pages    = extract_pages_fixed(pdf_path, page_0 + 1, chunk_path)

        # PlayScore strips dots when naming the XML after the imported PDF
        ps_name = chunk_path.stem.replace(".", "") + ".xml"
        print(f"   → Chunk PDF : {chunk_path.name}")
        print(f"      Pages {page_0 + 2}–{total_pages} ({n_pages} page(s))")
        print(f"   → Import that PDF into PlayScore and export the XML.")
        print(f"      PlayScore will name it: {ps_name}")
        print(f"      Drop it in: {RAW_DIR}")
        print(f"      (Any name PlayScore gives is fine — script matches by instrument+cue)")
        incomplete_count += 1
        print()

    print(f"Summary: {complete_count} complete, {incomplete_count} incomplete")
    if complete_count:
        print("Run --phase merge to process complete groups.")


# ─────────────────────────────────────────────────────────────────────────────
# Phase: merge
# ─────────────────────────────────────────────────────────────────────────────

def _merge_group(inst: str, cue: str, title: str, xml_paths: list) -> "Path | None":
    """Merge xml_paths into a single MXL in MERGED_DIR. Returns output path or None."""
    out_path = MERGED_DIR / f"JLP.{inst}.{cue}.{title}.mxl"
    src_has_tempo = False
    raw_parts = []

    for idx, path in enumerate(xml_paths):
        try:
            tree = load_xml(path)
        except Exception as exc:
            print(f"   [error] loading {path.name}: {exc}", file=sys.stderr)
            return None
        root = tree.getroot()
        part, pname = extract_primary_part(root, inst)
        if part is None:
            print(f"   [error] no <part> in {path.name}", file=sys.stderr)
            return None
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

    # Unknown durations
    for mn in find_unknown_duration_measures(merged):
        print(f"   [warning] unresolved duration in measure {mn}")

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
    return out_path


def phase_merge(args):
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)

    groups = group_raw_xmls(RAW_DIR, filter_inst=args.instrument, filter_cue=args.cue)
    if not groups:
        print(f"No JLP-named XML files found in: {RAW_DIR}")
        return

    state = load_state()
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
        result = _merge_group(inst, cue, title, xml_paths)
        if result:
            merged_count += 1
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
# Phase: status
# ─────────────────────────────────────────────────────────────────────────────

def _cue_sort(c: str):
    m = re.match(r"^(\d+)([A-Z]*)$", c.upper())
    return (int(m.group(1)), m.group(2)) if m else (9999, c)


def phase_status(args):
    state = load_state()

    raw_groups    = group_raw_xmls(RAW_DIR)
    merged_groups = group_merged_mxls(MERGED_DIR)

    merged_set: set = set()
    for (cue, _), inst_files in merged_groups.items():
        for inst, _ in inst_files:
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
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="JLP pit-orchestra MusicXML pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
phases:
  check     scan raw/ vs source PDFs; generate next-chunk PDFs
  merge     merge complete XML sets into per-instrument MXLs
  assemble  combine per-instrument MXLs into full-score MXLs
  status    print progress table (cues × instruments)
        """,
    )
    p.add_argument("--phase", required=True,
                   choices=["check", "merge", "assemble", "status"])
    p.add_argument("--cue",        default=None, help="Filter to one cue, e.g. 01  01A")
    p.add_argument("--instrument", default=None, choices=INSTRUMENTS,
                   help="Filter to one instrument")
    p.add_argument("--force", action="store_true",
                   help="Re-process even if output already exists / result cached")
    return p.parse_args()


def main():
    args = parse_args()
    dispatch = {
        "check":    phase_check,
        "merge":    phase_merge,
        "assemble": phase_assemble,
        "status":   phase_status,
    }
    dispatch[args.phase](args)


if __name__ == "__main__":
    main()
