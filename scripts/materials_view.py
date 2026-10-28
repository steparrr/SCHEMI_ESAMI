"""Read-only input status and extracted-text preview from the existing corpus."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from common import read_json
from local_app import KINDS


def material_detail(root: Path, kind: str, name: str):
    root = Path(root)
    if kind not in KINDS:
        raise ValueError("Categoria non valida.")
    folder = (root / "input" / KINDS[kind]).resolve()
    path = (folder / name).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        raise ValueError("File non disponibile.")
    key = str(path.relative_to(root))
    ingest = read_json(root / "state/ingest.json", {"files": {}})["files"].get(key, {})
    result = {"name": name, "format": path.suffix.lower(), "bytes": path.stat().st_size,
              "status": ingest.get("status", "copiato; estrazione non ancora eseguita"),
              "error": ingest.get("error"), "pages": None, "images": None,
              "sha256": ingest.get("sha256"), "text_preview": None,
              "ocr_quality": "non disponibile: il programma non misura una confidenza OCR"}
    db_path = root / "corpus/corpus.db"
    if not db_path.is_file():
        return result
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM documents WHERE original_path=?", (key,)).fetchone()
            if row is None:
                return result
            result.update(status=row["status"], pages=row["pages"], sha256=row["sha256"])
            result["images"] = db.execute("SELECT COUNT(*) FROM images WHERE document_id=?", (row["document_id"],)).fetchone()[0]
            if kind == "domande":
                snippets = [item[0] for item in db.execute(
                    "SELECT question_text FROM questions WHERE source_document_id=? LIMIT 5", (row["document_id"],))]
            else:
                snippets = [item[0] for item in db.execute(
                    "SELECT text FROM chunks WHERE document_id=? ORDER BY sequence_number LIMIT 2", (row["document_id"],))]
            result["text_preview"] = "\n\n".join(snippets)[:5000] if snippets else None
        finally:
            db.close()
    except sqlite3.Error:
        result["status"] = "corpus non leggibile; verifica il database locale"
    return result
