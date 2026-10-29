"""Build the bundled Italian user manual with the existing PyMuPDF dependency."""
from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/Manuale_Utente_Schemi_Esami.html"
OUTPUT = ROOT / "docs/Manuale_Utente_Schemi_Esami.pdf"


def build(source: Path = SOURCE, output: Path = OUTPUT) -> Path:
    source, output = Path(source), Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = output.with_name(output.stem + ".rendered.pdf")
    staged = output.with_name(output.stem + ".staged.pdf")
    page = pymupdf.paper_rect("a4")
    content = pymupdf.Rect(46, 51, page.width - 46, page.height - 62)
    story = pymupdf.Story(html=source.read_text(encoding="utf-8"))
    try:
        writer = pymupdf.DocumentWriter(str(rendered))
        try:
            story.write(writer, lambda number, filled: (page, content, None))
        finally:
            writer.close()
        with pymupdf.open(rendered) as document:
            for number, sheet in enumerate(document, 1):
                baseline = page.height - 35
                sheet.draw_line((46, baseline - 12), (page.width - 46, baseline - 12),
                                color=(0.85, 0.89, 0.90), width=0.7)
                sheet.insert_text((46, baseline), "SCHEMI ESAMI  /  MANUALE UTENTE",
                                  fontsize=8, fontname="helv", color=(0.35, 0.44, 0.49))
                sheet.insert_text((page.width - 77, baseline), str(number),
                                  fontsize=8, fontname="helv", color=(0.35, 0.44, 0.49))
            document.save(staged, garbage=4, deflate=True)
        staged.replace(output)
    finally:
        rendered.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crea il manuale PDF locale, senza API.")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(build(args.source, args.output))
