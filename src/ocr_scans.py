#!/usr/bin/env python3
"""Vision-transcribe every OCR-needed page (from the ingest manifest) and
splice the text into parsed_docs/ so downstream stages see complete documents.

Fully generic: no hardcoded document hashes or page lists - any dataset's
manifest drives the work. Pages are rendered with pdftoppm on the fly.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import STRONG, generate  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "case-related-docs" / "documents"
PAGES = REPO / "artifacts" / "scan_pages"
OUT = REPO / "artifacts" / "ocr"
PARSED = REPO / "artifacts" / "parsed_docs"
MARKER = "===== OCR OF SCANNED PAGE"

PROMPT = (
    "Transcribe this scanned document page verbatim into markdown. "
    "Preserve every table exactly (use markdown tables), every number, "
    "percentage and company name exactly as printed. Do not summarize, "
    "do not translate. Output only the transcription."
)


def render_page(doc: str, page: int) -> Path:
    PAGES.mkdir(parents=True, exist_ok=True)
    prefix = PAGES / f"{doc}-p{page}"
    out = Path(f"{prefix}-{page}.png")
    if not out.exists():
        subprocess.run(
            ["pdftoppm", "-png", "-r", "200", "-f", str(page), "-l", str(page),
             str(DOCS / f"{doc}.pdf"), str(prefix)],
            check=True, capture_output=True,
        )
        candidates = sorted(PAGES.glob(f"{doc}-p{page}-*.png"))
        if not candidates:
            raise RuntimeError(f"pdftoppm produced no image for {doc} p{page}")
        out = candidates[0]
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((REPO / "artifacts" / "doc_manifest.json").read_text())
    n_docs = n_pages = 0
    for doc, meta in sorted(manifest.items()):
        pages = meta.get("ocr_needed_pages", [])
        if not pages:
            continue
        parsed_path = PARSED / f"{doc}.txt"
        base = parsed_path.read_text()
        if MARKER in base:
            continue  # already spliced on a previous run
        chunks = []
        for page in pages:
            png = render_page(doc, page)
            try:
                text = generate(PROMPT, model=STRONG, images=[str(png)])
            except Exception as e:  # noqa: BLE001
                print(f"  !! vision failed for {doc} p{page} ({str(e)[:80]}) "
                      "-> tesseract fallback")
                r = subprocess.run(
                    ["tesseract", str(png), "-", "-l", "rus+kaz+eng"],
                    capture_output=True, text=True)
                text = f"[tesseract fallback]\n{r.stdout}"
            (OUT / f"{doc}-p{page}.md").write_text(text)
            chunks.append((page, text))
            n_pages += 1
            print(f"{doc} p{page}: {len(text)} chars")
        addition = "\n\n".join(
            f"\n{MARKER} {p} =====\n{t}" for p, t in chunks)
        parsed_path.write_text(base + addition)
        n_docs += 1
    print(f"OCR done: {n_pages} pages across {n_docs} docs spliced into parsed_docs/")


if __name__ == "__main__":
    main()
