#!/usr/bin/env python3
"""
jlp_merge.py — merge PlayScore XML exports into .mxl files for Logic Pro.

Stage 1  --mode merge    merge XMLs for one instrument → JLP.{inst}.{cue}.mxl
Stage 2  --mode assemble combine per-instrument MXLs → JLP.{cue}.full.mxl
"""

import argparse
import io
import sys
import zipfile
import xml.etree.ElementTree as ET
from copy import deepcopy
from statistics import median as _median

from jlp_common import (
    EXPORTS_DIR, INSTRUMENTS, CUE_TEMPOS, GM_DEFAULT, cue_tempo, sound_tempo,
    ensure_exports_dir, resolve_output,
)

# ─────────────────────────────────────────────────────────────────────────────
# Pitch constants
# ─────────────────────────────────────────────────────────────────────────────

_STEP_MIDI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# Octave correction ceilings (MIDI note numbers).
# E3 = 52, C5 = 72.  Correction fires when median of non-rest pitches > ceiling.
OCTAVE_CEILINGS = {
    "bass":   52,   # E3
    "cello":  72,   # C5
    "viola":  72,
    "violin": 72,
}

# Switch text patterns → {instrument: gm_program}
# Listed longest-first to prevent shorter substrings from matching first.
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
# Pitch utilities
# ─────────────────────────────────────────────────────────────────────────────

def _note_to_midi(step: str, octave: int, alter: float = 0.0) -> int:
    return (octave + 1) * 12 + _STEP_MIDI.get(step.upper(), 0) + round(alter)


def _collect_pitches(part_el) -> list[int]:
    """MIDI values of every non-rest pitched note in the part."""
    result = []
    for note in part_el.iter("note"):
        if note.find("rest") is not None:
            continue                    # skip rests
        pitch = note.find("pitch")
        if pitch is None:
            continue
        step  = (pitch.findtext("step")  or "").strip()
        oct_t = (pitch.findtext("octave") or "").strip()
        alt_t = (pitch.findtext("alter")  or "0").strip()
        if step and oct_t:
            try:
                result.append(_note_to_midi(step, int(oct_t), float(alt_t)))
            except (ValueError, TypeError):
                pass
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Octave correction  (median-based)
# ─────────────────────────────────────────────────────────────────────────────

def apply_octave_correction(part_el, instrument: str) -> tuple[float | None, bool]:
    """
    If the median MIDI pitch of non-rest notes exceeds the ceiling, shift every
    pitch element down one octave.
    Returns (median_value, shifted: bool).
    """
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

def find_unknown_duration_measures(part_el) -> list[str]:
    """Return measure numbers where a <type> element contains '?'."""
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
    """Prepend a <direction><sound midi-program="N"/></direction>."""
    d = ET.Element("direction")
    dt = ET.SubElement(d, "direction-type")
    w  = ET.SubElement(dt, "words")
    w.text = ""
    s = ET.SubElement(d, "sound")
    s.set("midi-program", str(program))
    measure_el.insert(0, d)


def detect_and_inject_switches(part_el, instrument: str) -> list[tuple]:
    """
    Scan every measure's text elements.  Inject a MIDI program-change direction
    where a switch is detected.
    Returns list of (measure_number_str, pattern_name, gm_program).
    """
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
# Tempo
# ─────────────────────────────────────────────────────────────────────────────

def _has_tempo(root) -> bool:
    return root.find(".//metronome") is not None


def _inject_tempo_direction(measure_el, bpm: int, beat_unit: str = "quarter"):
    d  = ET.Element("direction", placement="above")
    dt = ET.SubElement(d, "direction-type")
    mt = ET.SubElement(dt, "metronome")
    bu = ET.SubElement(mt, "beat-unit")
    bu.text = beat_unit
    pm = ET.SubElement(mt, "per-minute")
    pm.text = str(bpm)
    s  = ET.SubElement(d, "sound")
    # <sound tempo> is always in quarter notes (half-note 93 → quarter-note 186).
    s.set("tempo", str(sound_tempo(bpm, beat_unit)))
    insert_pos = 0
    for i, child in enumerate(list(measure_el)):
        if child.tag in ("note", "harmony", "barline"):
            insert_pos = i
            break
    measure_el.insert(insert_pos, d)


def inject_tempo(part_el, measure_number: int, bpm: int, beat_unit: str = "quarter"):
    for m in part_el.findall("measure"):
        if m.get("number") == str(measure_number):
            _inject_tempo_direction(m, bpm, beat_unit)
            return
    measures = part_el.findall("measure")
    if measures:
        _inject_tempo_direction(measures[0], bpm, beat_unit)


# ─────────────────────────────────────────────────────────────────────────────
# Measure renumbering
# ─────────────────────────────────────────────────────────────────────────────

def _renumber_part(part_el, start: int) -> int:
    measures = part_el.findall("measure")
    for i, m in enumerate(measures):
        if (m.get("number") or "").strip() == "?":
            print(f"    [warning] measure '?' → assigned {start + i}")
        m.set("number", str(start + i))
    return start + len(measures)


# ─────────────────────────────────────────────────────────────────────────────
# MusicXML I/O
# ─────────────────────────────────────────────────────────────────────────────

def load_xml(path: str) -> ET.ElementTree:
    """Parse a plain .xml or compressed .mxl file."""
    p = str(path)
    if p.lower().endswith(".mxl"):
        with zipfile.ZipFile(p) as zf:
            root_entry = None
            try:
                cdata = zf.read("META-INF/container.xml")
                croot = ET.fromstring(cdata)
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
                raise ValueError(f"No MusicXML content found in {p}")
            return ET.parse(io.BytesIO(zf.read(root_entry)))
    return ET.parse(p)


def write_mxl(tree: ET.ElementTree, output_path) -> int:
    """Write ElementTree as a compressed .mxl.  Returns XML byte count."""
    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    xml_bytes = buf.getvalue()
    op = str(output_path)
    inner = op[:-4] + ".xml" if op.lower().endswith(".mxl") else op + ".xml"
    inner = str(Path(inner).name)   # strip path — only bare filename inside zip
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
# Part extraction
# ─────────────────────────────────────────────────────────────────────────────

def _name_map(root) -> dict[str, str]:
    m = {}
    for sp in root.findall(".//score-part"):
        pid = sp.get("id")
        ne  = sp.find("part-name")
        if pid and ne is not None and ne.text:
            m[pid] = ne.text.strip()
    return m


def extract_primary_part(root, instrument: str):
    """Return (part_element, part_name_str) best matching instrument."""
    parts = root.findall("part")
    if not parts:
        return None, None
    nm  = _name_map(root)
    kw  = instrument.lower().rstrip("12")   # guitar1 → guitar
    for part in parts:
        pid = part.get("id", "")
        if kw in nm.get(pid, "").lower():
            return part, nm.get(pid, instrument)
    return parts[0], nm.get(parts[0].get("id", ""), instrument)


# ─────────────────────────────────────────────────────────────────────────────
# Score-part builder (with GM MIDI)
# ─────────────────────────────────────────────────────────────────────────────

def make_score_part_el(part_id: str, instrument: str, channel: int = 1):
    sp = ET.Element("score-part", id=part_id)
    ET.SubElement(sp, "part-name").text = instrument.capitalize()
    inst_id = f"{part_id}-I1"
    si = ET.SubElement(sp, "score-instrument", id=inst_id)
    ET.SubElement(si, "instrument-name").text = instrument
    ET.SubElement(sp, "midi-device", id=inst_id, port="1")
    mi = ET.SubElement(sp, "midi-instrument", id=inst_id)
    ET.SubElement(mi, "midi-channel").text = str(channel)
    ET.SubElement(mi, "midi-program").text = str(GM_DEFAULT.get(instrument, 1))
    ET.SubElement(mi, "volume").text = "80"
    ET.SubElement(mi, "pan").text = "0"
    return sp


# ─────────────────────────────────────────────────────────────────────────────
# Time-signature tracking (for rest padding)
# ─────────────────────────────────────────────────────────────────────────────

def _last_time_sig(part_el) -> tuple[int, int, int]:
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
                    if b:
                        beats = int(b)
                    if bt:
                        beat_type = int(bt)
                except ValueError:
                    pass
    return beats, beat_type, divisions


def _make_rest_measure(number: int, beats: int, beat_type: int, divisions: int):
    m    = ET.Element("measure", number=str(number))
    note = ET.SubElement(m, "note")
    rest = ET.SubElement(note, "rest")
    rest.set("measure", "yes")
    dur  = ET.SubElement(note, "duration")
    dur.text = str(int(divisions * beats * 4 / beat_type))
    return m


# ─────────────────────────────────────────────────────────────────────────────
# Summary printer
# ─────────────────────────────────────────────────────────────────────────────

def _print_summary(fields: dict):
    print(f"\n{'─' * 54}")
    for key, val in fields.items():
        if isinstance(val, list):
            if val:
                print(f"  {key}:")
                for item in val:
                    print(f"    {item}")
            else:
                print(f"  {key}: none")
        else:
            print(f"  {key}: {val}")
    print(f"{'─' * 54}")


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: merge
# ─────────────────────────────────────────────────────────────────────────────

def mode_merge(args):
    instrument = args.instrument
    cue        = args.cue
    out_path   = resolve_output(args.output)

    summary_switches  = []
    summary_unknown   = []
    src_has_tempo     = False

    # ── Load each input and extract the relevant part ─────────────────────────
    raw_parts: list[tuple] = []
    for idx, path in enumerate(args.inputs):
        print(f"Loading {path} ...")
        try:
            tree = load_xml(path)
        except Exception as exc:
            print(f"  [error] {exc}", file=sys.stderr)
            sys.exit(1)
        root = tree.getroot()
        part, pname = extract_primary_part(root, instrument)
        if part is None:
            print(f"  [error] no <part> elements in {path}", file=sys.stderr)
            sys.exit(1)
        print(f"  Part: \"{pname}\"")
        if idx == 0:
            src_has_tempo = _has_tempo(root)
        raw_parts.append((deepcopy(part), path))

    # ── Build merged part with sequential measure numbering ───────────────────
    merged = ET.Element("part", id="P1")
    next_num = 1
    for part, path in raw_parts:
        measures = part.findall("measure")
        if next_num > 1 and measures:
            for pr in measures[0].findall("print"):
                pr.attrib.pop("new-system", None)
                pr.attrib.pop("new-page", None)
        for i, m in enumerate(measures):
            if (m.get("number") or "").strip() == "?":
                print(f"  [warning] measure '?' in {path} → {next_num + i}")
            m.set("number", str(next_num + i))
            merged.append(deepcopy(m))
        next_num += len(measures)
    total_measures = next_num - 1

    # ── Octave correction ─────────────────────────────────────────────────────
    med, shifted = apply_octave_correction(merged, instrument)
    if shifted:
        print(f"  [octave-fix] median MIDI {med:.1f} > ceiling "
              f"{OCTAVE_CEILINGS[instrument]}; all pitches shifted down 1 octave")

    # ── Unknown durations ─────────────────────────────────────────────────────
    unknown = find_unknown_duration_measures(merged)
    for mn in unknown:
        print(f"  [warning] unresolved duration in measure {mn}")
    summary_unknown = unknown

    # ── Switch detection ──────────────────────────────────────────────────────
    switches = detect_and_inject_switches(merged, instrument)
    for mn, pat, prog in switches:
        print(f"  Switch detected: {instrument} m{mn} → {pat} (GM {prog})")
        summary_switches.append(f"{instrument} m{mn} → {pat} (GM {prog})")

    # ── Tempo ─────────────────────────────────────────────────────────────────
    if src_has_tempo:
        tempo_info = "found in source"
    else:
        tempo = cue_tempo(cue)
        if tempo is not None:
            bpm, beat_unit = tempo
            inject_tempo(merged, 1, bpm, beat_unit)
            unit_note = "" if beat_unit == "quarter" else f" ({beat_unit} note)"
            print(f"  [WARNING] Tempo not in source; injected {bpm} BPM{unit_note} "
                  f"from cue table (cue {cue})")
            tempo_info = f"injected {bpm} BPM{unit_note} (WARNING: verify against score)"
        else:
            print(f"  [WARNING] Cue {cue!r} not in tempo table and no tempo "
                  f"found in source — no tempo injected")
            tempo_info = f"none (cue {cue} absent from table)"

    # ── Build and write score document ────────────────────────────────────────
    score = ET.Element("score-partwise", version="3.1")
    pl    = ET.SubElement(score, "part-list")
    ch    = 10 if instrument == "percussion" else 1
    pl.append(make_score_part_el("P1", instrument, ch))
    score.append(merged)
    size = write_mxl(ET.ElementTree(score), out_path)
    print(f"\nWrote {out_path}  ({size:,} bytes XML)")

    _print_summary({
        "Mode":              "merge",
        "Instrument":        instrument,
        "Cue":               cue,
        "Input files":       str(len(args.inputs)),
        "Total measures":    str(total_measures),
        "Tempo":             tempo_info,
        "Switches detected": summary_switches,
        "Unknown durations": summary_unknown,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: assemble
# ─────────────────────────────────────────────────────────────────────────────

def _detect_instrument_from_path(path: str) -> str | None:
    low = Path(path).name.lower()
    for inst in sorted(INSTRUMENTS, key=len, reverse=True):
        if inst in low:
            return inst
    return None


def mode_assemble(args):
    cue      = args.cue
    out_path = resolve_output(args.output)

    loaded: list[tuple[str, ET.Element]] = []
    for path in args.inputs:
        print(f"Loading {path} ...")
        try:
            tree = load_xml(path)
        except Exception as exc:
            print(f"  [error] {exc}", file=sys.stderr)
            sys.exit(1)
        root = tree.getroot()

        inst = _detect_instrument_from_path(path)
        if inst is None:
            part, pname = extract_primary_part(root, "")
            if pname:
                for candidate in sorted(INSTRUMENTS, key=len, reverse=True):
                    if candidate.rstrip("12") in pname.lower():
                        inst = candidate
                        break
        if inst is None:
            inst = "unknown"
            print(f"  [warning] cannot determine instrument for {path}")
        print(f"  Instrument: {inst}")

        parts = root.findall("part")
        if not parts:
            print(f"  [error] no <part> in {path}", file=sys.stderr)
            sys.exit(1)
        loaded.append((inst, deepcopy(parts[0])))

    max_m = max(len(p.findall("measure")) for _, p in loaded)
    print(f"\nAligning to {max_m} measures ...")

    score = ET.Element("score-partwise", version="3.1")
    pl    = ET.SubElement(score, "part-list")
    instruments_included = []

    for idx, (inst, part) in enumerate(loaded):
        pid = f"P{idx + 1}"
        ch  = 10 if inst == "percussion" else 1
        pl.append(make_score_part_el(pid, inst, ch))

        current = len(part.findall("measure"))
        if current < max_m:
            beats, beat_type, divisions = _last_time_sig(part)
            for mn in range(current + 1, max_m + 1):
                part.append(_make_rest_measure(mn, beats, beat_type, divisions))
            print(f"  Padded {inst}: +{max_m - current} rest measure(s)")

        part.set("id", pid)
        score.append(part)
        instruments_included.append(inst)

    size = write_mxl(ET.ElementTree(score), out_path)
    print(f"\nWrote {out_path}  ({size:,} bytes XML)")

    cue_t = cue_tempo(cue)
    if cue_t is None:
        cue_tempo_ref = f"none (cue {cue})"
    else:
        _bpm, _unit = cue_t
        _unit_note = "" if _unit == "quarter" else f" ({_unit} note)"
        cue_tempo_ref = f"{_bpm} BPM{_unit_note}"
    _print_summary({
        "Mode":             "assemble",
        "Cue":              cue,
        "Instruments":      instruments_included,
        "Total measures":   str(max_m),
        "Cue tempo (ref)":  cue_tempo_ref,
        "Output":           str(out_path),
    })


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Merge and assemble PlayScore MusicXML exports."
    )
    p.add_argument("--mode", required=True, choices=["merge", "assemble"])
    p.add_argument("--instrument", "-i", choices=INSTRUMENTS,
                   help="Required for --mode merge")
    p.add_argument("--cue", required=True,
                   help="Cue identifier, e.g. 01  01A  09B")
    p.add_argument("--output", "-o", required=True,
                   help="Output .mxl filename (bare name → ~/JLP_exports/)")
    p.add_argument("inputs", nargs="+", metavar="FILE",
                   help="Input .xml or .mxl files in order")
    return p.parse_args()


def main():
    args = parse_args()
    if args.mode == "merge":
        if not args.instrument:
            print("error: --instrument required for --mode merge", file=sys.stderr)
            sys.exit(1)
        mode_merge(args)
    else:
        mode_assemble(args)


# ─── needed so Path is available in write_mxl ───
from pathlib import Path

if __name__ == "__main__":
    main()
