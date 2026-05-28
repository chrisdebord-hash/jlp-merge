#!/usr/bin/env python3
"""
jlp_merge.py — assemble multi-instrument MusicXML from PlayScore exports.

Stage 1  --mode merge    merge XMLs for one instrument → single-instrument .mxl
Stage 2  --mode assemble combine per-instrument .mxl files → full score .mxl
"""

import argparse
import io
import sys
import zipfile
import xml.etree.ElementTree as ET
from copy import deepcopy
from statistics import median as _median

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

CUE_TEMPOS = {
    '00': 93,  '01': 100, '01A': 86,  '02': 93,  '02A': 90,  '03': None,
    '03A': None,'03B': 79, '04': 88,  '04A': 104, '05': 84,  '05A': None,
    '06': 84,  '07': 88,  '07A': None,'07B': 87,  '08': 88,  '08A': 103,
    '08B': 44, '08C': 84, '09': None, '09A': None,'09B': None,
    '10': 80,  '11': 90,  '12': 114,  '13': 69,  '14': None, '15': 81,
    '15A': 122,'15B': 110,'16': None, '16A': 64,  '17': 77,  '18': 89,
    '19': 97,  '20': 78,  '21': 109,  '21A': None,'22': 87,  '23': 90,
    '24': None,
}

# Known mid-cue tempo changes: cue → [(measure_number, bpm), ...]
CUE_MID_TEMPOS = {
    '01': [(20, 84)],
}

# Default GM programs (1-indexed, as MusicXML spec requires)
GM_DEFAULT = {
    'bass':       34,   # Electric Bass (finger)
    'guitar1':    27,   # Electric Guitar (clean)
    'guitar2':    27,
    'violin':     41,
    'viola':      42,
    'cello':      43,
    'percussion':  0,   # channel 10; program not meaningful
    'piano':       1,   # Acoustic Grand Piano
}

# Switch text patterns → {instrument: gm_program}
# Listed longest-first so substring checks don't short-circuit on a prefix.
SWITCH_PATTERNS = [
    ('PIZZICATO', {'violin': 46, 'viola': 45, 'cello': 44}),
    ('SUL PONT',  {'violin': 49, 'viola': 49, 'cello': 49}),
    ('ACOUSTIC',  {'bass': 33,  'guitar1': 25, 'guitar2': 25}),
    ('ELECTRIC',  {'bass': 34,  'guitar1': 27, 'guitar2': 27}),
    ('MUTED',     {'guitar1': 29,'guitar2': 29,'bass': 29}),
    ('ACOUS',     {'bass': 33,  'guitar1': 25, 'guitar2': 25}),
    ('ELEC',      {'bass': 34,  'guitar1': 27, 'guitar2': 27}),
    ('ARCO',      {'violin': 41,'viola': 42,  'cello': 43}),
    ('PIZZ',      {'violin': 46,'viola': 45,  'cello': 44}),
    ('MUTE',      {'guitar1': 29,'guitar2': 29,'bass': 29}),
]

INSTRUMENTS = ['bass', 'cello', 'guitar1', 'guitar2',
               'percussion', 'viola', 'violin', 'piano']

# Octave ceiling MIDI values: if median pitch of part exceeds this, shift all
# notes down one octave.  E3 = MIDI 52, C5 = MIDI 72.
_STEP_MIDI = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
OCTAVE_CEILINGS_MIDI = {
    'bass':   (3 + 1) * 12 + _STEP_MIDI['E'],   # 52  (E3)
    'cello':  (5 + 1) * 12 + _STEP_MIDI['C'],   # 72  (C5)
    'viola':  (5 + 1) * 12 + _STEP_MIDI['C'],
    'violin': (5 + 1) * 12 + _STEP_MIDI['C'],
}


# ─────────────────────────────────────────────────────────────────────────────
# Pitch utilities
# ─────────────────────────────────────────────────────────────────────────────

def note_to_midi(step, octave, alter=0):
    """Convert MusicXML pitch fields to MIDI note number (C4=60 convention)."""
    return (octave + 1) * 12 + _STEP_MIDI.get(step.upper(), 0) + int(alter)


def collect_midi_pitches(part_el):
    """Return a list of MIDI note numbers for every pitched note in a <part>."""
    result = []
    for pitch in part_el.iter('pitch'):
        step = (pitch.findtext('step') or '').strip()
        oct_t = (pitch.findtext('octave') or '').strip()
        alt_t = (pitch.findtext('alter') or '0').strip()
        if step and oct_t:
            try:
                result.append(note_to_midi(step, int(oct_t), float(alt_t or 0)))
            except (ValueError, TypeError):
                pass
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Octave correction  (median-based)
# ─────────────────────────────────────────────────────────────────────────────

def apply_octave_correction(part_el, instrument):
    """
    If the median MIDI pitch of the part exceeds the instrument ceiling, shift
    every pitch element down one octave.  Returns (median, shifted=True/False).
    """
    ceiling = OCTAVE_CEILINGS_MIDI.get(instrument)
    if ceiling is None:
        return None, False
    pitches = collect_midi_pitches(part_el)
    if not pitches:
        return None, False
    med = _median(pitches)
    if med <= ceiling:
        return med, False
    for pitch in part_el.iter('pitch'):
        oct_el = pitch.find('octave')
        if oct_el is not None and oct_el.text:
            try:
                oct_el.text = str(int(oct_el.text) - 1)
            except ValueError:
                pass
    return med, True


# ─────────────────────────────────────────────────────────────────────────────
# Unknown-duration warnings
# ─────────────────────────────────────────────────────────────────────────────

def find_unknown_duration_measures(part_el):
    """Return list of measure numbers that contain a <type> whose text has '?'."""
    bad = []
    for m in part_el.findall('measure'):
        for type_el in m.iter('type'):
            if type_el.text and '?' in type_el.text:
                bad.append(m.get('number', '?'))
                break
    return bad


# ─────────────────────────────────────────────────────────────────────────────
# Mid-cue switch detection & program-change injection
# ─────────────────────────────────────────────────────────────────────────────

def _measure_texts(measure_el):
    """Return all <words> and <rehearsal> text values in a measure, uppercased."""
    texts = []
    for el in measure_el.iter():
        if el.tag in ('words', 'rehearsal') and el.text:
            texts.append(el.text.strip().upper())
    return ' '.join(texts)


def _inject_program_change(measure_el, program):
    """Prepend a minimal <direction><sound midi-program="N"/></direction>."""
    direction = ET.Element('direction')
    dt = ET.SubElement(direction, 'direction-type')
    # Invisible placeholder so the direction is structurally valid
    words = ET.SubElement(dt, 'words')
    words.text = ''
    sound = ET.SubElement(direction, 'sound')
    sound.set('midi-program', str(program))
    measure_el.insert(0, direction)


def detect_and_inject_switches(part_el, instrument):
    """
    Scan every measure for switch text, inject program-change directions, and
    return a list of (measure_number_str, pattern, gm_program) tuples.
    """
    detected = []
    for m in part_el.findall('measure'):
        combined = _measure_texts(m)
        if not combined:
            continue
        for pattern, programs in SWITCH_PATTERNS:
            if pattern in combined:
                prog = programs.get(instrument)
                if prog is not None:
                    _inject_program_change(m, prog)
                    detected.append((m.get('number', '?'), pattern, prog))
                    break   # one program-change per measure
    return detected


# ─────────────────────────────────────────────────────────────────────────────
# Tempo
# ─────────────────────────────────────────────────────────────────────────────

def has_tempo(root):
    return root.find('.//metronome') is not None


def _inject_tempo_direction(measure_el, bpm):
    direction = ET.Element('direction', placement='above')
    dt = ET.SubElement(direction, 'direction-type')
    metro = ET.SubElement(dt, 'metronome')
    bu = ET.SubElement(metro, 'beat-unit')
    bu.text = 'quarter'
    pm = ET.SubElement(metro, 'per-minute')
    pm.text = str(bpm)
    sound = ET.SubElement(direction, 'sound')
    sound.set('tempo', str(bpm))
    # Insert before the first note/rest/barline
    insert_pos = 0
    for i, child in enumerate(list(measure_el)):
        if child.tag in ('note', 'harmony', 'barline'):
            insert_pos = i
            break
    measure_el.insert(insert_pos, direction)


def inject_tempo_at_measure(part_el, measure_number, bpm):
    """Inject a tempo direction at the given measure number (string or int)."""
    for m in part_el.findall('measure'):
        if m.get('number') == str(measure_number):
            _inject_tempo_direction(m, bpm)
            return
    # Fallback: first measure
    measures = part_el.findall('measure')
    if measures:
        _inject_tempo_direction(measures[0], bpm)


# ─────────────────────────────────────────────────────────────────────────────
# Measure renumbering
# ─────────────────────────────────────────────────────────────────────────────

def renumber_part_measures(part_el, start):
    """
    Renumber every measure in the part starting from start.
    Warns if a measure had number '?'.  Returns next available number.
    """
    measures = part_el.findall('measure')
    for i, m in enumerate(measures):
        if (m.get('number') or '').strip() == '?':
            print(f'  [warning] measure with number "?" → assigned {start + i}')
        m.set('number', str(start + i))
    return start + len(measures)


# ─────────────────────────────────────────────────────────────────────────────
# MusicXML I/O
# ─────────────────────────────────────────────────────────────────────────────

def load_xml(path):
    """Parse a plain .xml or compressed .mxl file and return an ElementTree."""
    if path.lower().endswith('.mxl'):
        with zipfile.ZipFile(path) as zf:
            root_path = None
            try:
                cdata = zf.read('META-INF/container.xml')
                croot = ET.fromstring(cdata)
                for rf in croot.iter('rootfile'):
                    root_path = rf.get('full-path')
                    break
            except (KeyError, ET.ParseError):
                pass
            if root_path is None:
                for name in zf.namelist():
                    if name.endswith('.xml') and 'META-INF' not in name:
                        root_path = name
                        break
            if root_path is None:
                raise ValueError(f'No MusicXML root found in {path}')
            return ET.parse(io.BytesIO(zf.read(root_path)))
    return ET.parse(path)


def write_mxl(tree, output_path):
    """Write the ElementTree as a compressed .mxl file.  Returns XML byte count."""
    buf = io.BytesIO()
    tree.write(buf, encoding='utf-8', xml_declaration=True)
    xml_bytes = buf.getvalue()
    inner = (output_path[:-4] + '.xml'
             if output_path.lower().endswith('.mxl')
             else output_path + '.xml')
    container = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container>\n  <rootfiles>\n'
        f'    <rootfile full-path="{inner}"\n'
        '      media-type="application/vnd.recordare.musicxml+xml"/>\n'
        '  </rootfiles>\n</container>\n'
    ).encode('utf-8')
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('META-INF/container.xml', container)
        zf.writestr(inner, xml_bytes)
    return len(xml_bytes)


# ─────────────────────────────────────────────────────────────────────────────
# Part extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_primary_part(root, instrument):
    """
    Return the <part> element best matching instrument.
    Tries part-name substring match, falls back to first part.
    Returns (part_element, part_name_str).
    """
    parts = root.findall('part')
    if not parts:
        return None, None

    name_map = {}
    for sp in root.findall('.//score-part'):
        pid = sp.get('id')
        ne = sp.find('part-name')
        if pid and ne is not None and ne.text:
            name_map[pid] = ne.text.strip()

    keyword = instrument.lower().rstrip('12')   # guitar1 → guitar
    for part in parts:
        pid = part.get('id', '')
        if keyword in name_map.get(pid, '').lower():
            return part, name_map.get(pid, instrument)

    first = parts[0]
    return first, name_map.get(first.get('id', ''), instrument)


# ─────────────────────────────────────────────────────────────────────────────
# Score-part element builder (with GM MIDI info)
# ─────────────────────────────────────────────────────────────────────────────

def make_score_part_el(part_id, instrument, channel=1):
    """Build a <score-part> element complete with MIDI program assignment."""
    sp = ET.Element('score-part', id=part_id)
    pname = ET.SubElement(sp, 'part-name')
    pname.text = instrument.capitalize()
    inst_id = f'{part_id}-I1'
    si = ET.SubElement(sp, 'score-instrument', id=inst_id)
    iname = ET.SubElement(si, 'instrument-name')
    iname.text = instrument
    ET.SubElement(sp, 'midi-device', id=inst_id, port='1')
    mi = ET.SubElement(sp, 'midi-instrument', id=inst_id)
    ET.SubElement(mi, 'midi-channel').text = str(channel)
    ET.SubElement(mi, 'midi-program').text = str(GM_DEFAULT.get(instrument, 1))
    ET.SubElement(mi, 'volume').text = '80'
    ET.SubElement(mi, 'pan').text = '0'
    return sp


# ─────────────────────────────────────────────────────────────────────────────
# Time-signature tracking for rest padding
# ─────────────────────────────────────────────────────────────────────────────

def get_time_sig_at_end(part_el):
    """Return (beats, beat_type, divisions) using the last seen attribute values."""
    beats, beat_type, divisions = 4, 4, 1
    for m in part_el.findall('measure'):
        for attr in m.findall('attributes'):
            d = attr.findtext('divisions')
            if d:
                try:
                    divisions = int(d)
                except ValueError:
                    pass
            time_el = attr.find('time')
            if time_el is not None:
                b = time_el.findtext('beats')
                bt = time_el.findtext('beat-type')
                try:
                    if b:
                        beats = int(b)
                    if bt:
                        beat_type = int(bt)
                except ValueError:
                    pass
    return beats, beat_type, divisions


def make_rest_measure(number, beats, beat_type, divisions):
    """Create a single full-measure rest element."""
    m = ET.Element('measure', number=str(number))
    note = ET.SubElement(m, 'note')
    rest = ET.SubElement(note, 'rest')
    rest.set('measure', 'yes')
    # Duration in quarter-note divisions = divisions * beats * (4 / beat_type)
    dur = ET.SubElement(note, 'duration')
    dur.text = str(int(divisions * beats * 4 / beat_type))
    return m


# ─────────────────────────────────────────────────────────────────────────────
# Instrument detection from filename (for assemble mode)
# ─────────────────────────────────────────────────────────────────────────────

def detect_instrument_from_path(path):
    low = path.lower()
    # Match longest instrument name first to avoid 'guitar' matching 'guitar1'
    for inst in sorted(INSTRUMENTS, key=len, reverse=True):
        if inst in low:
            return inst
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: merge
# ─────────────────────────────────────────────────────────────────────────────

def mode_merge(args):
    instrument = args.instrument
    cue        = args.cue
    output     = args.output

    summary_switches          = []
    summary_unknown_durations = []
    src_has_tempo             = False

    # ── Load and extract the relevant part from each input file ──────────────
    raw_parts = []   # list of (part_element_copy, source_path)

    for file_idx, path in enumerate(args.inputs):
        print(f'Loading {path} ...')
        try:
            tree = load_xml(path)
        except Exception as exc:
            print(f'  [error] {exc}', file=sys.stderr)
            sys.exit(1)
        root = tree.getroot()

        part, pname = extract_primary_part(root, instrument)
        if part is None:
            print(f'  [error] no <part> elements in {path}', file=sys.stderr)
            sys.exit(1)
        print(f'  Part: "{pname}"')

        if file_idx == 0:
            src_has_tempo = has_tempo(root)

        raw_parts.append((deepcopy(part), path))

    # ── Assemble merged <part> with sequential measure numbers ───────────────
    merged_part = ET.Element('part', id='P1')
    next_num = 1

    for part, path in raw_parts:
        measures = part.findall('measure')
        # Strip new-system/new-page from first measure of non-first files
        if next_num > 1 and measures:
            for print_el in measures[0].findall('print'):
                print_el.attrib.pop('new-system', None)
                print_el.attrib.pop('new-page', None)
        for i, m in enumerate(measures):
            if (m.get('number') or '').strip() == '?':
                print(f'  [warning] measure "?" in {path} → assigned {next_num + i}')
            m.set('number', str(next_num + i))
            merged_part.append(deepcopy(m))
        next_num += len(measures)

    total_measures = next_num - 1

    # ── Octave correction (median-based) ─────────────────────────────────────
    med, shifted = apply_octave_correction(merged_part, instrument)
    if shifted:
        ceil = OCTAVE_CEILINGS_MIDI[instrument]
        print(f'  [octave-fix] median MIDI {med:.1f} > ceiling {ceil}; '
              f'shifted all pitches down 1 octave')
    elif med is not None and not shifted:
        pass   # within range, no action needed

    # ── Unknown duration warnings (after final renumbering) ──────────────────
    unknown = find_unknown_duration_measures(merged_part)
    for m_num in unknown:
        print(f'  [warning] unresolved duration in measure {m_num}')
    summary_unknown_durations = unknown

    # ── Switch detection ──────────────────────────────────────────────────────
    switches = detect_and_inject_switches(merged_part, instrument)
    for m_num, pattern, prog in switches:
        print(f'  Switch: {instrument} m{m_num} → {pattern} (GM {prog})')
        summary_switches.append(f'{instrument} m{m_num} → {pattern} (GM {prog})')

    # ── Tempo handling ────────────────────────────────────────────────────────
    if src_has_tempo:
        tempo_info = 'found in source'
    else:
        cue_bpm = CUE_TEMPOS.get(cue)
        if cue_bpm is not None:
            inject_tempo_at_measure(merged_part, 1, cue_bpm)
            print(f'  [WARNING] Tempo not in source; injected {cue_bpm} BPM '
                  f'from cue table (cue {cue})')
            tempo_info = f'injected {cue_bpm} BPM (WARNING: not found in source)'
        else:
            print(f'  [WARNING] No tempo in source and cue {cue!r} has no '
                  f'table entry — no tempo injected')
            tempo_info = f'none (cue {cue} not in tempo table)'

        # Mid-cue tempo changes
        for m_num, bpm in CUE_MID_TEMPOS.get(cue, []):
            nums = {m.get('number') for m in merged_part.findall('measure')}
            if str(m_num) in nums:
                inject_tempo_at_measure(merged_part, m_num, bpm)
                print(f'  [WARNING] Mid-cue tempo: m{m_num} → {bpm} BPM (cue {cue})')
                summary_switches.append(f'tempo m{m_num} → {bpm} BPM')
            else:
                print(f'  [info] Cue {cue} mid-cue tempo at m{m_num} skipped '
                      f'(measure not present — score may be shorter)')

    # ── Build score document and write ───────────────────────────────────────
    score = ET.Element('score-partwise', version='3.1')
    pl    = ET.SubElement(score, 'part-list')
    channel = 10 if instrument == 'percussion' else 1
    pl.append(make_score_part_el('P1', instrument, channel))
    score.append(merged_part)

    size = write_mxl(ET.ElementTree(score), output)
    print(f'\nWrote {output}  ({size:,} bytes XML)')

    # ── Summary ───────────────────────────────────────────────────────────────
    _print_summary({
        'Mode':              'merge',
        'Instrument':        instrument,
        'Cue':               cue,
        'Input files':       str(len(args.inputs)),
        'Total measures':    str(total_measures),
        'Tempo':             tempo_info,
        'Switches detected': summary_switches or ['none'],
        'Unknown durations': (summary_unknown_durations
                              if summary_unknown_durations else ['none']),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: assemble
# ─────────────────────────────────────────────────────────────────────────────

def mode_assemble(args):
    cue    = args.cue
    output = args.output

    loaded = []   # list of (instrument, part_element)

    for path in args.inputs:
        print(f'Loading {path} ...')
        try:
            tree = load_xml(path)
        except Exception as exc:
            print(f'  [error] {exc}', file=sys.stderr)
            sys.exit(1)
        root = tree.getroot()

        # Determine instrument from filename, then part name, then warn
        inst = detect_instrument_from_path(path)
        if inst is None:
            part, pname = extract_primary_part(root, '')
            if pname:
                pname_low = pname.lower()
                for candidate in sorted(INSTRUMENTS, key=len, reverse=True):
                    if candidate.rstrip('12') in pname_low or candidate in pname_low:
                        inst = candidate
                        break
        if inst is None:
            inst = 'unknown'
            print(f'  [warning] cannot determine instrument for {path}')

        print(f'  Instrument: {inst}')

        parts = root.findall('part')
        if not parts:
            print(f'  [error] no <part> in {path}', file=sys.stderr)
            sys.exit(1)

        loaded.append((inst, deepcopy(parts[0])))

    # ── Align to the longest part ─────────────────────────────────────────────
    max_measures = max(len(p.findall('measure')) for _, p in loaded)
    print(f'\nAligning all parts to {max_measures} measures ...')

    score = ET.Element('score-partwise', version='3.1')
    pl    = ET.SubElement(score, 'part-list')

    instruments_included = []
    for idx, (inst, part) in enumerate(loaded):
        part_id = f'P{idx + 1}'
        channel = 10 if inst == 'percussion' else 1
        pl.append(make_score_part_el(part_id, inst, channel))

        current = len(part.findall('measure'))
        if current < max_measures:
            beats, beat_type, divisions = get_time_sig_at_end(part)
            for m_num in range(current + 1, max_measures + 1):
                part.append(make_rest_measure(m_num, beats, beat_type, divisions))
            print(f'  Padded {inst}: added {max_measures - current} rest measure(s)')

        part.set('id', part_id)
        score.append(part)
        instruments_included.append(inst)

    size = write_mxl(ET.ElementTree(score), output)
    print(f'\nWrote {output}  ({size:,} bytes XML)')

    cue_bpm = CUE_TEMPOS.get(cue, 'not in table')
    _print_summary({
        'Mode':           'assemble',
        'Cue':            cue,
        'Instruments':    instruments_included,
        'Total measures': str(max_measures),
        'Cue tempo (ref)': (f'{cue_bpm} BPM' if cue_bpm else
                            f'none (cue {cue} not in table)'),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Summary printer
# ─────────────────────────────────────────────────────────────────────────────

def _print_summary(fields):
    print(f'\n{"─" * 52}')
    for key, val in fields.items():
        if isinstance(val, list):
            print(f'  {key}:')
            for item in val:
                print(f'    {item}')
        else:
            print(f'  {key}: {val}')
    print(f'{"─" * 52}')


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Merge and assemble PlayScore MusicXML exports.'
    )
    p.add_argument('--mode', required=True, choices=['merge', 'assemble'],
                   help='merge: combine files for one instrument; '
                        'assemble: build full score from per-instrument files')
    p.add_argument('--instrument', '-i', choices=INSTRUMENTS,
                   help='Instrument (required for --mode merge)')
    p.add_argument('--cue', required=True,
                   help='Cue identifier, e.g. 01  01A  09B')
    p.add_argument('--output', '-o', required=True,
                   help='Output .mxl filename')
    p.add_argument('inputs', nargs='+', metavar='FILE',
                   help='Input .xml or .mxl files in order')
    return p.parse_args()


def main():
    args = parse_args()
    if args.mode == 'merge':
        if not args.instrument:
            print('error: --instrument is required for --mode merge',
                  file=sys.stderr)
            sys.exit(1)
        mode_merge(args)
    else:
        mode_assemble(args)


if __name__ == '__main__':
    main()
