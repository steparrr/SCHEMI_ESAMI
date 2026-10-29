"""Conservative local visual warnings for existing PDFs; no mutation or API calls."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import fitz

from common import read_json, write_json


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


def _fingerprint(root: Path, path: Path, result: dict):
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
    return root, path, hasher.hexdigest()


def export_visual_report(root: Path, path: Path, result: dict):
    """Save a local technical report with no source text or PDF alteration."""
    root, path, pdf_hash = _fingerprint(root, path, result)
    report = {"version": 1, "pdf": str(path.relative_to(root)), "pdf_sha256": pdf_hash,
              "pages_checked": len(result["pages"]), "truncated": result["truncated"],
              "pages": result["pages"]}
    folder = root / "output/reports/visual"
    destination = folder / f"visual_{pdf_hash[:16]}_{len(result['pages'])}.json"
    write_json(destination, report)
    return destination


def load_visual_reviews(root: Path, path: Path, result: dict):
    root, path, pdf_hash = _fingerprint(root, path, result)
    saved = read_json(root / "output/reports/visual" / f"review_{pdf_hash}.json", {})
    return saved["pages"] if isinstance(saved, dict) and saved.get("pdf_sha256") == pdf_hash and isinstance(saved.get("pages"), dict) else {}


def mark_visual_review(root: Path, path: Path, result: dict, page_number: int,
                       decision: str, note: str = ""):
    """Record a user observation for this exact PDF; never change pipeline state."""
    if decision not in {"verified", "false_positive"}:
        raise ValueError("Decisione di revisione visiva non valida.")
    if type(page_number) is not int or not 1 <= page_number <= len(result.get("pages") or []):
        raise ValueError("Pagina da revisionare non valida.")
    page = result["pages"][page_number - 1]
    if page.get("page") != page_number:
        raise ValueError("La numerazione delle pagine non corrisponde.")
    note = str(note).strip()
    if len(note) > 500 or (decision == "false_positive" and (not page.get("warnings") or len(note) < 5)):
        raise ValueError("Per un falso positivo indica il motivo (5–500 caratteri) e seleziona una pagina con avvisi.")
    root, path, pdf_hash = _fingerprint(root, path, result)
    destination = root / "output/reports/visual" / f"review_{pdf_hash}.json"
    saved = read_json(destination, {})
    if not isinstance(saved, dict):
        raise ValueError("Il file di revisione visiva locale non è valido.")
    if saved and (saved.get("pdf_sha256") != pdf_hash or not isinstance(saved.get("pages"), dict)):
        raise ValueError("La revisione salvata non corrisponde al PDF corrente.")
    saved = saved or {"version": 1, "pdf": str(path.relative_to(root)),
                      "pdf_sha256": pdf_hash, "pages": {}}
    saved["pages"][str(page_number)] = {"decision": decision, "note": note,
                                         "warnings": list(page.get("warnings") or []),
                                         "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    write_json(destination, saved)
    return saved["pages"][str(page_number)]
