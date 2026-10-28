"""Conservative local visual warnings for existing PDFs; no mutation or API calls."""
from __future__ import annotations

from pathlib import Path

import fitz


def inspect_visual(path: Path, max_pages=250):
    path = Path(path)
    result = {"pdf": path.name, "pages": [], "truncated": False}
    with fitz.open(path) as doc:
        for index in range(min(doc.page_count, max_pages)):
            page = doc[index]
            warnings = []
            text = page.get_text("text") or ""
            blocks = [fitz.Rect(block[:4]) for block in page.get_text("blocks")
                      if len(block) > 6 and block[6] == 0 and str(block[4]).strip()]
            drawings = page.get_drawings()
            if len(text.strip()) < 30 and not page.get_images(full=True):
                warnings.append("Pagina con pochissimo testo e senza immagini: verifica se è vuota.")
            bounds = page.rect
            if any(rect.x0 < bounds.x0 - 2 or rect.y0 < bounds.y0 - 2 or
                   rect.x1 > bounds.x1 + 2 or rect.y1 > bounds.y1 + 2 for rect in blocks):
                warnings.append("Testo vicino o oltre il bordo della pagina.")
            for drawing in drawings:
                rect = drawing.get("rect")
                if not rect or drawing.get("fill") is None:
                    continue
                box = fitz.Rect(rect)
                # Bounding boxes are only a suspicion; drawing order can make a valid background.
                if any(box.intersects(block) and (box & block).get_area() > block.get_area() * .65
                       and box.get_area() > block.get_area() * .8 for block in blocks):
                    warnings.append("Riquadro colorato sovrapposto al testo: verifica visivamente l'ordine dei livelli.")
                    break
            if index == doc.page_count - 1 and 0 < len(text.strip()) < 180:
                warnings.append("Ultima pagina con poco contenuto: verifica l'impaginazione.")
            result["pages"].append({"page": index + 1, "text_chars": len(text), "warnings": warnings})
        result["truncated"] = doc.page_count > max_pages
    return result
