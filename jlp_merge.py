#!/usr/bin/env python3
"""
jlp_merge.py — merge multiple PlayScore MusicXML exports into one .mxl file.

Usage:
    python3 jlp_merge.py file1.xml file2.xml ... [options]

Options:
    --output FILE       Output .mxl filename (default: merged.mxl)
    --instrument NAME   Instrument for octave-error correction
                        Choices: bass, cello, guitar1, guitar2, percussion,
                                 viola, violin, piano, trombone
    --tempo BPM         Inject a tempo marking at m1 if none exists
"""

import argparse
import io
import sys
import zipfile
import xml.etree.ElementTree as ET
from copy import deepcopy

# ---------------------------------------------------------------------------
# Octave-ceiling rules: (step, octave) — notes ABOVE this are shifted down.
# E3  = step E, octave 3  (MIDI ~52)
# C5  = step C, octave 5  (MIDI ~72)
# ---------------------------------------------------------------------------
OCTAVE_CEILINGS = {
    "bass":     ("E", 3),
    "trombone": ("E", 3),
    "cello":    ("C", 5),
    "viola":    ("C", 5),
    "violin":   ("C", 5),
}

STEP_ORDER = ["C", "D", "E", "F", "G", "A", "B"]


def step_octave_gt(step, octave, ceil_step, ceil_octave):
    """Return True if (step, octave) is strictly above (ceil_step, ceil_octave)."""
    if octave != ceil_octave:
        return octave > ceil_octave
    return STEP_ORDER.index(step) > STEP_ORDER.index(ceil_step)


def _correct_pitches_in_element(element, ceil_step, ceil_octave):
    """Return count of pitches shifted down one octave in element."""
    count = 0
    for pitch in element.iter("pitch"):
        step_el = pitch.find("step")
        octave_el = pitch.find("octave")
        if step_el is None or octave_el is None:
            continue
        step = step_el.text.strip()
        try:
            octave = int(octave_el.text.strip())
        except (ValueError, TypeError):
            continue
        if step_octave_gt(step, octave, ceil_step, ceil_octave):
            octave_el.text = str(octave - 1)
            count += 1
    return count


def fix_octave_errors(root, instrument):
    """
    Shift notes down one octave where they exceed the ceiling for the instrument.
    Only corrects parts whose <part-name> matches the instrument (case-insensitive).
    Falls back to correcting all parts if no name match is found.
    """
    ceiling = OCTAVE_CEILINGS.get(instrument)
    if ceiling is None:
        return  # no correction defined for this instrument
    ceil_step, ceil_octave = ceiling

    # Build a map of part id → part-name from <part-list>
    part_names = {}
    for sp in root.findall(".//score-part"):
        pid = sp.get("id")
        name_el = sp.find("part-name")
        if pid and name_el is not None and name_el.text:
            part_names[pid] = name_el.text.strip().lower()

    # Find parts whose name contains the instrument keyword
    instrument_key = instrument.lower().rstrip("12")  # strip guitar1→guitar
    matched_ids = {
        pid for pid, name in part_names.items()
        if instrument_key in name
    }

    parts = root.findall("part")
    # Determine which parts to correct
    if matched_ids:
        target_parts = [p for p in parts if p.get("id") in matched_ids]
        scope_note = f"matched part(s) {sorted(matched_ids)}"
    else:
        target_parts = parts
        scope_note = "all parts (no name match found)"

    total = sum(_correct_pitches_in_element(p, ceil_step, ceil_octave)
                for p in target_parts)
    if total:
        print(f"  [octave-fix] shifted {total} note(s) down one octave "
              f"(instrument={instrument}, ceiling={ceil_step}{ceil_octave}, {scope_note})")


# ---------------------------------------------------------------------------
# Tempo injection
# ---------------------------------------------------------------------------

def has_tempo(root):
    """Return True if any direction contains a metronome element."""
    return bool(root.find(".//metronome"))


def inject_tempo(root, bpm):
    """Insert a <direction> with a <metronome> into the first measure."""
    first_measure = root.find(".//measure")
    if first_measure is None:
        return
    # Build the direction element
    direction = ET.Element("direction", placement="above")
    dt = ET.SubElement(direction, "direction-type")
    metro = ET.SubElement(dt, "metronome")
    beat_unit = ET.SubElement(metro, "beat-unit")
    beat_unit.text = "quarter"
    per_minute = ET.SubElement(metro, "per-minute")
    per_minute.text = str(bpm)
    sound = ET.Element("sound", tempo=str(bpm))
    direction.append(sound)
    # Insert before the first note/rest
    insert_pos = 0
    for i, child in enumerate(list(first_measure)):
        if child.tag in ("note", "harmony", "barline"):
            insert_pos = i
            break
    first_measure.insert(insert_pos, direction)
    print(f"  [tempo] injected {bpm} BPM at measure 1")


# ---------------------------------------------------------------------------
# MusicXML loading — handles both plain .xml and .mxl (zipped)
# ---------------------------------------------------------------------------

def load_xml(path):
    """Return an ElementTree parsed from a plain or compressed MusicXML file."""
    if path.lower().endswith(".mxl"):
        with zipfile.ZipFile(path) as zf:
            # Find the rootfile entry from META-INF/container.xml
            container_data = None
            try:
                container_data = zf.read("META-INF/container.xml")
            except KeyError:
                pass
            rootfile_path = None
            if container_data:
                croot = ET.fromstring(container_data)
                for rf in croot.iter("rootfile"):
                    rootfile_path = rf.get("full-path")
                    break
            if rootfile_path is None:
                # Fall back: first .xml file
                for name in zf.namelist():
                    if name.endswith(".xml") and "META-INF" not in name:
                        rootfile_path = name
                        break
            if rootfile_path is None:
                raise ValueError(f"Cannot find MusicXML root in {path}")
            data = zf.read(rootfile_path)
        return ET.parse(io.BytesIO(data))
    else:
        return ET.parse(path)


# ---------------------------------------------------------------------------
# Measure renumbering
# ---------------------------------------------------------------------------

def renumber_measures(root, start_number):
    """
    Renumber measures starting from start_number.

    In partwise MusicXML all parts are parallel in time, so every part must
    carry the same set of measure numbers.  We renumber each part's measures
    independently with the same ascending sequence.  The count of measures per
    part is taken from the first part (all parts are assumed to be the same
    length).  Returns the next available measure number (start + count).
    """
    parts = root.findall("part")
    if not parts:
        # Fallback: no <part> wrapper, renumber all measures linearly
        measures = root.findall(".//measure")
        for i, m in enumerate(measures):
            _warn_unknown_number(m, start_number + i)
            m.set("number", str(start_number + i))
        return start_number + len(measures)

    # Use first part's count as authoritative length
    count = len(parts[0].findall("measure"))

    for part in parts:
        for i, m in enumerate(part.findall("measure")):
            _warn_unknown_number(m, start_number + i)
            m.set("number", str(start_number + i))

    return start_number + count


def _warn_unknown_number(measure_el, new_number):
    orig = measure_el.get("number", "?")
    if orig.strip() == "?":
        print(f"  [warning] measure with unknown number '?' encountered; "
              f"assigning number {new_number}")


# ---------------------------------------------------------------------------
# Merging logic
# ---------------------------------------------------------------------------

def merge_files(input_paths, instrument, tempo_bpm):
    """
    Parse all input files, renumber measures sequentially, apply octave
    correction, and return a merged ElementTree.

    Strategy: treat the first file as the base document and append the
    measures from subsequent files into each matching <part>.  Parts are
    matched by <part id="..."> then by position if IDs differ.
    """
    trees = []
    for path in input_paths:
        try:
            tree = load_xml(path)
            trees.append((path, tree))
        except Exception as exc:
            print(f"[error] could not parse {path}: {exc}", file=sys.stderr)
            sys.exit(1)

    # ---- Apply octave correction to every file first ----
    if instrument:
        for path, tree in trees:
            print(f"  Checking octave errors in {path} ...")
            fix_octave_errors(tree.getroot(), instrument)

    # ---- Use first file as the skeleton ----
    base_path, base_tree = trees[0]
    base_root = base_tree.getroot()

    # Register namespaces so round-trip preserves them
    ns_map = {}
    for _, (path, tree) in enumerate(trees):
        for event, (prefix, uri) in ET.iterparse(path, events=["start-ns"]):
            ns_map[prefix] = uri
    for prefix, uri in ns_map.items():
        try:
            ET.register_namespace(prefix, uri)
        except Exception:
            pass

    # ---- Collect parts from base ----
    base_parts = base_root.findall("part")
    if not base_parts:
        print("[error] No <part> elements found in base file.", file=sys.stderr)
        sys.exit(1)

    # Renumber base file starting at 1
    next_measure_num = renumber_measures(base_root, 1)

    # ---- Append measures from subsequent files ----
    for path, tree in trees[1:]:
        root = tree.getroot()
        parts = root.findall("part")
        # Match parts by id, fallback to position
        for i, base_part in enumerate(base_parts):
            base_id = base_part.get("id")
            donor_part = None
            # Try id match first
            for p in parts:
                if p.get("id") == base_id:
                    donor_part = p
                    break
            # Fallback: positional
            if donor_part is None and i < len(parts):
                donor_part = parts[i]
            if donor_part is None:
                print(f"  [warning] {path} has no part to match "
                      f"base part id='{base_id}' (position {i}); skipping")
                continue

            # Renumber this donor's measures continuing from where base left off
            donor_next = renumber_measures(root, next_measure_num)

            # Strip print/layout new-system from first appended measure
            donor_measures = donor_part.findall("measure")
            if donor_measures:
                first_m = donor_measures[0]
                for print_el in first_m.findall("print"):
                    # Remove new-system/new-page attributes that cause page breaks
                    print_el.attrib.pop("new-system", None)
                    print_el.attrib.pop("new-page", None)

            for m in donor_measures:
                base_part.append(deepcopy(m))

        next_measure_num = donor_next  # advance global counter

    # ---- Tempo injection ----
    if tempo_bpm is not None and not has_tempo(base_root):
        inject_tempo(base_root, tempo_bpm)
    elif tempo_bpm is not None:
        print("  [tempo] existing tempo marking found; --tempo flag ignored")

    return base_tree


# ---------------------------------------------------------------------------
# .mxl packaging
# ---------------------------------------------------------------------------

def write_mxl(tree, output_path):
    """Write the ElementTree as a compressed .mxl file."""
    xml_bytes = io.BytesIO()
    tree.write(xml_bytes, encoding="utf-8", xml_declaration=True)
    xml_content = xml_bytes.getvalue()

    # Derive an inner filename from the output path
    inner_name = output_path
    if inner_name.lower().endswith(".mxl"):
        inner_name = inner_name[:-4] + ".xml"

    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container>\n'
        '  <rootfiles>\n'
        f'    <rootfile full-path="{inner_name}"\n'
        '      media-type="application/vnd.recordare.musicxml+xml"/>\n'
        '  </rootfiles>\n'
        '</container>\n'
    ).encode("utf-8")

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr(inner_name, xml_content)

    print(f"\nWrote {output_path}  ({len(xml_content):,} bytes XML compressed into .mxl)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Merge multiple PlayScore MusicXML exports into one .mxl file."
    )
    parser.add_argument(
        "inputs", nargs="+", metavar="FILE",
        help="Input .xml or .mxl files in order"
    )
    parser.add_argument(
        "--output", "-o", default="merged.mxl", metavar="FILE",
        help="Output .mxl filename (default: merged.mxl)"
    )
    parser.add_argument(
        "--instrument", "-i",
        choices=["bass", "cello", "guitar1", "guitar2", "percussion",
                 "viola", "violin", "piano", "trombone"],
        default=None,
        help="Instrument type for octave-error correction"
    )
    parser.add_argument(
        "--tempo", "-t", type=int, default=None, metavar="BPM",
        help="Inject a tempo marking at m1 if none exists"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print(f"Merging {len(args.inputs)} file(s):")
    for f in args.inputs:
        print(f"  {f}")

    merged_tree = merge_files(args.inputs, args.instrument, args.tempo)
    write_mxl(merged_tree, args.output)


if __name__ == "__main__":
    main()
