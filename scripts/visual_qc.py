"""Conservative local visual warnings for existing PDFs; no mutation or API calls."""
from __future__ import annotations

import hashlib
from pathlib import Path

import fitz

from common import write_json


def inspect_visual(path: Path, max_pages=250):
    path = Path(path)
    before = path.stat()
    result = {"pdf": path.name, "pages": [], "truncated": False,
              "file_size": before.st_size, "file_mtime_ns": before.st_mtime_ns}
    with fitz.open(path) as doc:
        for index in range(min(doc.page_count, max_pages)):
            page = doc[index]
            warnings = []
            text = page.get_text("text") or ""
            blocks = [fitz.Rect(block[:4]) for block in page.get_text("blocks")
                      if len(block) > 6 and block[6] == 0 and str(block[4]).strip()]
            visible_text = [(fitz.Rect(span["bbox"]), span["seqno"])
                            for span in page.get_texttrace()
                            if span.get("type") == 0 and span.get("chars")]
            drawings = page.get_drawings()
            if len(text.strip()) < 30 and not page.get_images(full=True):
                warnings.append("Pagina con pochissimo testo e senza immagini: verifica se è vuota.")
            bounds = page.rect
            if any(rect.x0 < bounds.x0 - 2 or rect.y0 < bounds.y0 - 2 or
                   rect.x1 > bounds.x1 + 2 or rect.y1 > bounds.y1 + 2 for rect in blocks):
                warnings.append("Testo vicino o oltre il bordo della pagina.")
            for drawing in drawings:
                rect = drawing.get("rect")
                opacity = drawing.get("fill_opacity")
                if not rect or drawing.get("fill") is None or (opacity is not None and opacity < .85):
                    continue
                box = fitz.Rect(rect)
                # A solid shape drawn after text may cover it. Geometric overlap is still a suspicion.
                if any(drawing.get("seqno", -1) > text_order and box.intersects(text_box)
                       and (box & text_box).get_area() > text_box.get_area() * .65
                       for text_box, text_order in visible_text):
                    warnings.append("Riquadro opaco disegnato sopra il testo: verifica visivamente la pagina.")
                    break
            if index == doc.page_count - 1 and 0 < len(text.strip()) < 180:
                warnings.append("Ultima pagina con poco contenuto: verifica l'impaginazione.")
            result["pages"].append({"page": index + 1, "text_chars": len(text), "warnings": warnings})
        result["truncated"] = doc.page_count > max_pages
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise ValueError("Il PDF è cambiato durante il controllo visivo; ripeti l'analisi.")
    return result


def export_visual_report(root: Path, path: Path, result: dict):
    """Save a local technical report with no source text or PDF alteration."""
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to((root / "output/pdf").resolve()) or path.suffix.lower() != ".pdf":
        raise ValueError("Seleziona un PDF della materia.")
    before = path.stat()
    if (before.st_size, before.st_mtime_ns) != (result.get("file_size"), result.get("file_mtime_ns")) or result.get("pdf") != path.name:
        raise ValueError("Il PDF è cambiato dopo il controllo; ripeti l'analisi.")
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise ValueError("Il PDF è cambiato prima di esportare il report.")
    report = {"version": 1, "pdf": str(path.relative_to(root)), "pdf_sha256": hasher.hexdigest(),
              "pages_checked": len(result["pages"]), "truncated": result["truncated"],
              "pages": result["pages"]}
    folder = root / "output/reports/visual"
    destination = folder / f"visual_{hasher.hexdigest()[:16]}_{len(result['pages'])}.json"
    write_json(destination, report)
    return destination
