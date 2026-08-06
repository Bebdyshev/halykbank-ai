#!/usr/bin/env python3
"""S1 INGEST: extract text from every PDF and flag pages that need OCR/vision.

Outputs:
    artifacts/parsed_docs/{hash}.txt   pdftotext -layout output per PDF
    artifacts/doc_manifest.json        per-doc: pages, text chars, OCR-needed pages

A page needs OCR when it carries an embedded raster image OR has almost no
extractable text (covers fully-scanned pages, image-tables inside text docs,
and vector-rendered scans with a stub of text).
"""
import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "case-related-docs" / "documents"
OUT_TXT = REPO / "artifacts" / "parsed_docs"
MANIFEST = REPO / "artifacts" / "doc_manifest.json"

MIN_TEXT_CHARS = 200  # below this a page is suspicious even without a raster image


def pdf_page_count(pdf: Path) -> int:
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
    m = re.search(r"^Pages:\s+(\d+)", info, re.M)
    return int(m.group(1)) if m else 0


def image_pages(pdf: Path) -> list:
    out = subprocess.run(["pdfimages", "-list", str(pdf)], capture_output=True, text=True).stdout
    pages = set()
    for line in out.splitlines()[2:]:
        parts = line.split()
        if parts and parts[0].isdigit():
            pages.add(int(parts[0]))
    return sorted(pages)


def text_per_page(pdf: Path, n_pages: int) -> list:
    counts = []
    for p in range(1, n_pages + 1):
        r = subprocess.run(
            ["pdftotext", "-layout", "-f", str(p), "-l", str(p), str(pdf), "-"],
            capture_output=True, text=True,
        )
        counts.append(len(r.stdout.strip()))
    return counts


def main() -> None:
    OUT_TXT.mkdir(parents=True, exist_ok=True)
    manifest = {}
    pdfs = sorted(DOCS.glob("*.pdf"))
    for pdf in pdfs:
        name = pdf.stem
        r = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True
        )
        (OUT_TXT / f"{name}.txt").write_text(r.stdout)

        n_pages = pdf_page_count(pdf)
        img_pages = set(image_pages(pdf))
        per_page = text_per_page(pdf, n_pages)
        ocr_pages = sorted(
            p for p in range(1, n_pages + 1)
            if (p in img_pages and per_page[p - 1] < MIN_TEXT_CHARS)
            or per_page[p - 1] < 50
        )

        manifest[name] = {
            "pages": n_pages,
            "text_chars": len(r.stdout.strip()),
            "image_pages": sorted(img_pages),
            "ocr_needed_pages": ocr_pages,
        }
        if ocr_pages:
            print(f"OCR NEEDED: {name}.pdf pages {ocr_pages}")

    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    total_ocr = sum(1 for m in manifest.values() if m["ocr_needed_pages"])
    print(f"ingested {len(pdfs)} PDFs -> {OUT_TXT}")
    print(f"docs with OCR-needed pages: {total_ocr}")


if __name__ == "__main__":
    main()
