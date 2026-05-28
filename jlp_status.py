#!/usr/bin/env python3
"""
jlp_status.py — show at a glance what has been exported and what needs work.

Usage:
    python jlp_status.py                        # full matrix
    python jlp_status.py --cue 01               # filter to one cue
    python jlp_status.py --instrument bass      # filter to one instrument
    python jlp_status.py --cue 01 --instrument bass
    python jlp_status.py --exports /path/to/dir # custom exports dir
"""

import argparse
import re
import sys
from pathlib import Path

from jlp_common import (
    SOURCE_DIR, EXPORTS_DIR, INSTRUMENTS, CUE_TEMPOS, ensure_exports_dir,
)

# ─────────────────────────────────────────────────────────────────────────────
# Discover source PDFs
# ─────────────────────────────────────────────────────────────────────────────

def discover_source_pdfs() -> dict[str, dict[str, Path]]:
    """
    Return {instrument: {cue: pdf_path}} for every PDF in SOURCE_DIR.
    PDF names must match: JLP.{instrument}.{cue}.*.pdf
    """
    result: dict[str, dict[str, Path]] = {inst: {} for inst in INSTRUMENTS}

    if not SOURCE_DIR.exists():
        print(f"[warning] SOURCE_DIR not found: {SOURCE_DIR}", file=sys.stderr)
        return result

    pattern = re.compile(
        r"^JLP\.(?P<inst>[^.]+)\.(?P<cue>[^.]+)\.",
        re.IGNORECASE,
    )
    for inst in INSTRUMENTS:
        folder = SOURCE_DIR / inst
        if not folder.exists():
            continue
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() != ".pdf":
                continue
            m = pattern.match(f.name)
            if not m:
                continue
            cue = m.group("cue").upper()
            result[inst][cue] = f

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Discover export artefacts
# ─────────────────────────────────────────────────────────────────────────────

def discover_exports(exports_dir: Path) -> dict[tuple[str, str], dict]:
    """
    Return {(instrument, cue): {'xml_parts': [paths], 'merged': bool, 'assembled': bool}}.

    - xml_parts:  any .xml files whose name contains both the instrument and cue tokens
    - merged:     JLP.{instrument}.{cue}.mxl exists
    - assembled:  JLP.{cue}.full.mxl exists
    """
    status: dict[tuple[str, str], dict] = {}

    if not exports_dir.exists():
        return status

    all_files = list(exports_dir.iterdir())

    def _key(inst, cue):
        k = (inst, cue.upper())
        if k not in status:
            status[k] = {"xml_parts": [], "merged": False, "assembled": False}
        return status[k]

    # Merged per-instrument MXLs: JLP.{instrument}.{cue}.mxl
    merged_re = re.compile(
        r"^JLP\.(?P<inst>[^.]+)\.(?P<cue>[^.]+)\.mxl$",
        re.IGNORECASE,
    )
    # Full scores: JLP.{cue}.full.mxl
    full_re = re.compile(
        r"^JLP\.(?P<cue>[^.]+)\.full\.mxl$",
        re.IGNORECASE,
    )
    # Raw XML exports: any .xml containing instrument + cue in the filename
    # Pattern: e.g. bass_01_part1.xml  or  bass.01.part1.xml
    xml_inst_re = re.compile(
        r"^(?P<inst>" + "|".join(re.escape(i) for i in
                                 sorted(INSTRUMENTS, key=len, reverse=True))
        + r")[._-](?P<cue>[A-Z0-9]+)[._-].*\.xml$",
        re.IGNORECASE,
    )

    assembled_cues: set[str] = set()

    for f in all_files:
        name = f.name

        m = merged_re.match(name)
        if m:
            inst = m.group("inst").lower()
            cue  = m.group("cue").upper()
            if inst in INSTRUMENTS:
                _key(inst, cue)["merged"] = True
            continue

        m = full_re.match(name)
        if m:
            assembled_cues.add(m.group("cue").upper())
            continue

        m = xml_inst_re.match(name)
        if m and f.suffix.lower() == ".xml":
            inst = m.group("inst").lower()
            cue  = m.group("cue").upper()
            if inst in INSTRUMENTS:
                _key(inst, cue)["xml_parts"].append(f)

    # Back-fill assembled flag onto all (inst, cue) pairs for matching cues
    for cue in assembled_cues:
        for inst in INSTRUMENTS:
            k = (inst, cue)
            if k in status:
                status[k]["assembled"] = True
            # Don't create phantom entries — only mark assembled on known pairs

    return status, assembled_cues


# ─────────────────────────────────────────────────────────────────────────────
# Build the combined status matrix
# ─────────────────────────────────────────────────────────────────────────────

def build_matrix(
    source: dict[str, dict[str, Path]],
    exports: dict,
    assembled_cues: set[str],
    filter_cue: str | None,
    filter_instrument: str | None,
) -> list[dict]:
    """
    Return one row dict per (instrument, cue) combination that passes the
    active filters.  Rows include instruments with no PDF if exports exist.
    """
    # Collect all cues seen in either source or exports
    all_cues: set[str] = set()
    for cues in source.values():
        all_cues |= set(cues)
    for inst, cue in exports:
        all_cues.add(cue)

    # Sort cues: numeric part first, then suffix
    def _cue_sort(c):
        m = re.match(r"^(\d+)([A-Z]*)$", c)
        if m:
            return (int(m.group(1)), m.group(2))
        return (9999, c)

    sorted_cues  = sorted(all_cues, key=_cue_sort)
    sorted_insts = [i for i in INSTRUMENTS
                    if filter_instrument is None or i == filter_instrument]

    rows = []
    for cue in sorted_cues:
        if filter_cue and cue != filter_cue.upper():
            continue
        for inst in sorted_insts:
            pdf_exists = cue in source.get(inst, {})
            exp        = exports.get((inst, cue), {})
            xml_parts  = exp.get("xml_parts", [])
            merged     = exp.get("merged", False)
            full_score = cue in assembled_cues

            # Skip rows where nothing exists at all
            if not pdf_exists and not xml_parts and not merged and not full_score:
                continue

            rows.append({
                "instrument": inst,
                "cue":        cue,
                "pdf":        pdf_exists,
                "xml_parts":  len(xml_parts),
                "merged":     merged,
                "full_score": full_score,
                "attention":  (pdf_exists and not merged),
            })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Display
# ─────────────────────────────────────────────────────────────────────────────

_CHECK = "✓"
_CROSS = "·"
_WARN  = "!"


def _cell(flag: bool, warn: bool = False) -> str:
    if flag:
        return _CHECK
    return _WARN if warn else _CROSS


def print_table(rows: list[dict], exports_dir: Path):
    if not rows:
        print("  (no data found)")
        return

    col_inst = max(len(r["instrument"]) for r in rows)
    col_inst = max(col_inst, 10)

    header = (
        f"  {'Instrument':<{col_inst}}  "
        f"{'Cue':<6}  {'PDF':<4}  {'XML':>5}  {'Merged':<7}  Full"
    )
    sep = "  " + "─" * (len(header) - 2)

    print(f"\n{header}")
    print(sep)

    prev_cue = None
    needs_attention: list[str] = []

    for r in rows:
        if r["cue"] != prev_cue and prev_cue is not None:
            print()
        prev_cue = r["cue"]

        xml_str = str(r["xml_parts"]) if r["xml_parts"] else _CROSS
        pdf_c   = _cell(r["pdf"])
        merged_c = _cell(r["merged"], warn=r["attention"])
        full_c  = _cell(r["full_score"])

        flag = " ←" if r["attention"] else ""
        print(
            f"  {r['instrument']:<{col_inst}}  "
            f"{r['cue']:<6}  {pdf_c:<4}  {xml_str:>5}  "
            f"{merged_c:<7}  {full_c}{flag}"
        )

        if r["attention"]:
            needs_attention.append(f"{r['instrument']} cue {r['cue']}")

    print(sep)

    # Totals
    total_pdfs   = sum(1 for r in rows if r["pdf"])
    total_merged = sum(1 for r in rows if r["merged"])

    print(f"\n  Exports dir : {exports_dir}")
    print(f"  PDF total   : {total_pdfs}")
    print(f"  Merged      : {total_merged} / {total_pdfs}"
          + (" ✓ all done!" if total_merged == total_pdfs and total_pdfs else ""))

    if needs_attention:
        print(f"\n  Needs attention ({len(needs_attention)}):")
        for item in needs_attention[:20]:
            print(f"    {item}")
        if len(needs_attention) > 20:
            print(f"    … and {len(needs_attention) - 20} more")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Show export status for all JLP cues and instruments."
    )
    p.add_argument("--cue", default=None,
                   help="Filter to a single cue, e.g. 01  01A")
    p.add_argument("--instrument", default=None, choices=INSTRUMENTS,
                   help="Filter to a single instrument")
    p.add_argument("--exports", default=None, metavar="DIR",
                   help="Exports directory (default: ~/JLP_exports)")
    return p.parse_args()


def main():
    args = parse_args()

    exports_dir = Path(args.exports) if args.exports else EXPORTS_DIR
    ensure_exports_dir()

    print(f"Source PDFs : {SOURCE_DIR}")
    print(f"Exports dir : {exports_dir}")

    source = discover_source_pdfs()
    exports_data, assembled_cues = discover_exports(exports_dir)

    rows = build_matrix(
        source,
        exports_data,
        assembled_cues,
        filter_cue=args.cue,
        filter_instrument=args.instrument,
    )

    print_table(rows, exports_dir)


if __name__ == "__main__":
    main()
