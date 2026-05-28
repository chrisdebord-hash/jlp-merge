#!/usr/bin/env python3
"""
jlp_next.py — after PlayScore cuts off mid-export, figure out which pages
remain and produce the next PDF chunk ready for PlayScore import.

Usage:
    python jlp_next.py --instrument bass --cue 01 --xml bass_01_part1.xml
    python jlp_next.py --instrument bass --cue 01 --xml bass_01_part2.xml --page 14
"""

import argparse
import io
import re
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

import fitz                          # pymupdf
from PIL import Image
import pytesseract

from jlp_common import (
    SOURCE_DIR, find_source_pdf, INSTRUMENTS,
)

# DPI used for rasterising pages before OCR.
# 200 DPI gives good accuracy with moderate speed on A4/letter sheet music.
OCR_DPI = 200

# How many pages to scan forward past the found page before giving up.
# (Handles cases where the measure number appears on multiple pages.)
MAX_SCAN_PAGES = 200


# ─────────────────────────────────────────────────────────────────────────────
# XML: find the last exported measure number
# ─────────────────────────────────────────────────────────────────────────────

def last_measure_number(xml_path: Path) -> int:
    """Return the highest measure/@number found in the XML file."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    nums = []
    for m in root.iter("measure"):
        raw = (m.get("number") or "").strip()
        try:
            nums.append(int(raw))
        except ValueError:
            pass
    if not nums:
        raise ValueError(f"No numeric measure numbers found in {xml_path}")
    return max(nums)


# ─────────────────────────────────────────────────────────────────────────────
# PDF: text extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

def _number_regex(n: int, fuzz: int = 2) -> re.Pattern:
    """Build a regex that matches any integer in [n-fuzz, n+fuzz] as a word."""
    alts = "|".join(str(n + d) for d in range(-fuzz, fuzz + 1) if n + d > 0)
    return re.compile(r"(?<!\d)(?:" + alts + r")(?!\d)")


def _page_contains_measure(page: "fitz.Page", pattern: re.Pattern) -> bool:
    """
    Two-layer check: (1) fitz text extraction (fast, works for vector PDFs),
    (2) pytesseract OCR (for scanned/image PDFs).
    Returns True if the measure number was found.
    """
    # Layer 1 — fitz text (near-instant for vector PDFs)
    text = page.get_text("text")
    if pattern.search(text):
        return True

    # Layer 2 — pytesseract OCR
    mat = fitz.Matrix(OCR_DPI / 72, OCR_DPI / 72)
    pix = page.get_pixmap(matrix=mat)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    ocr_text = pytesseract.image_to_string(img, config="--psm 11")
    return bool(pattern.search(ocr_text))


def scan_pdf_for_measure(
    pdf_path: Path,
    measure_num: int,
    hint_page: int | None = None,
) -> tuple[int | None, int]:
    """
    Scan PDF pages to find the first page containing measure_num (±2).

    hint_page: 1-based page number to start scanning from (skips earlier pages).
    Returns (page_index_0based | None, total_pages).
    """
    doc = fitz.open(str(pdf_path))
    total = len(doc)
    pattern = _number_regex(measure_num)

    start = max(0, (hint_page - 1) if hint_page else 0)

    print(f"  Scanning {total} pages of {pdf_path.name} "
          f"for measure {measure_num} ...", flush=True)

    for page_idx in range(start, min(start + MAX_SCAN_PAGES, total)):
        page = doc[page_idx]
        sys.stdout.write(f"\r  Page {page_idx + 1}/{total} ...   ")
        sys.stdout.flush()
        if _page_contains_measure(page, pattern):
            sys.stdout.write("\n")
            doc.close()
            return page_idx, total

    sys.stdout.write("\n")
    doc.close()
    return None, total


# ─────────────────────────────────────────────────────────────────────────────
# PDF: extract a page range to a new file
# ─────────────────────────────────────────────────────────────────────────────

def extract_pages(
    src_path: Path,
    from_page_0based: int,
    output_path: Path,
) -> int:
    """
    Write pages [from_page_0based .. end] of src_path to output_path.
    Returns the number of pages written.
    """
    doc = fitz.open(str(src_path))
    total = len(doc)
    out = fitz.open()
    out.insert_pdf(doc, from_page=from_page_0based)
    out.save(str(output_path))
    doc.close()
    out.close()
    return total - from_page_0based


# ─────────────────────────────────────────────────────────────────────────────
# Output filename: auto-increment part number
# ─────────────────────────────────────────────────────────────────────────────

def next_part_pdf_path(instrument: str, cue: str, output_dir: Path) -> Path:
    """
    Scan output_dir for existing {instrument}_{cue}_part*.pdf files and
    return the next one in sequence (part2, part3, …).
    """
    existing = list(output_dir.glob(f"{instrument}_{cue}_part*.pdf"))
    nums = []
    for p in existing:
        m = re.search(r"part(\d+)", p.stem)
        if m:
            nums.append(int(m.group(1)))
    next_num = max(nums, default=1) + 1
    return output_dir / f"{instrument}_{cue}_part{next_num}.pdf"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Find the remaining PDF pages after a PlayScore export cutoff."
    )
    p.add_argument("--instrument", "-i", required=True, choices=INSTRUMENTS)
    p.add_argument("--cue", "-c", required=True,
                   help="Cue identifier, e.g. 01  01A  09B")
    p.add_argument("--xml", required=True, metavar="FILE",
                   help="PlayScore XML export that cut off")
    p.add_argument("--page", type=int, default=None, metavar="N",
                   help="Manually specify page number (1-based) of last measure "
                        "(skips OCR scan, useful if scan fails)")
    p.add_argument("--output-dir", default=".", metavar="DIR",
                   help="Directory for output PDF (default: current directory)")
    return p.parse_args()


def main():
    args = parse_args()
    xml_path    = Path(args.xml)
    output_dir  = Path(args.output_dir)
    instrument  = args.instrument
    cue         = args.cue

    # ── 1. Find last exported measure ────────────────────────────────────────
    if not xml_path.exists():
        print(f"[error] XML not found: {xml_path}", file=sys.stderr)
        sys.exit(1)

    last_m = last_measure_number(xml_path)
    print(f"Last measure in {xml_path.name}: m{last_m}")

    # ── 2. Locate source PDF ──────────────────────────────────────────────────
    pdf_path = find_source_pdf(instrument, cue)
    if pdf_path is None:
        expected = SOURCE_DIR / instrument / f"JLP.{instrument}.{cue}.*.pdf"
        print(f"[error] Source PDF not found.\n"
              f"  Expected: {expected}\n"
              f"  Check that the file exists and SOURCE_DIR is correct.",
              file=sys.stderr)
        sys.exit(1)
    print(f"Source PDF : {pdf_path}")

    # ── 3. Find the page containing last measure ──────────────────────────────
    if args.page is not None:
        cutoff_page_0 = args.page - 1   # convert 1-based to 0-based
        doc = fitz.open(str(pdf_path))
        total_pages = len(doc)
        doc.close()
        print(f"  Using manually specified page {args.page} of {total_pages}")
    else:
        cutoff_page_0, total_pages = scan_pdf_for_measure(pdf_path, last_m)
        if cutoff_page_0 is None:
            print(
                f"\n[warning] Could not locate m{last_m} via OCR in "
                f"{pdf_path.name}.\n"
                f"  Re-run with --page N to specify manually.",
                file=sys.stderr,
            )
            sys.exit(1)

    # ── 4. Extract remaining pages ────────────────────────────────────────────
    next_page_0 = cutoff_page_0 + 1      # pages AFTER the found page
    if next_page_0 >= total_pages:
        print(f"\n[info] m{last_m} is on the last page ({total_pages}) — "
              f"nothing to extract.")
        sys.exit(0)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path   = next_part_pdf_path(instrument, cue, output_dir)
    page_count = extract_pages(pdf_path, next_page_0, out_path)

    start_1based = cutoff_page_0 + 2    # first exported page, 1-based
    end_1based   = total_pages

    print(
        f"\nPlayScore cut off at m{last_m} "
        f"(page {cutoff_page_0 + 1} of {total_pages}). "
        f"Next chunk: pages {start_1based}–{end_1based} "
        f"({page_count} page{'s' if page_count != 1 else ''}) "
        f"→ {out_path.name}"
    )


if __name__ == "__main__":
    main()
