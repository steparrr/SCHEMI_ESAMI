from __future__ import annotations

import csv
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import fitz

import ingest_helpers as h
from common import read_json, write_json
from formats import IMAGES, extract_pdf, extract_pptx, image_text, normalized_image, spreadsheet_blocks

IMAGE_FIELDS = ["image_id", "document_id", "source_document", "page_or_position", "path", "filename_originale", "width", "height", "format", "sha256", "perceptual_hash", "caption", "heading", "context_before", "context_after", "status", "filtered_reason"]
SUPPORTED = {".docx", ".pdf", ".pptx", ".txt", ".md", ".xlsx", ".csv"} | IMAGES


def write_manifest(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=IMAGE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def parse_document(path, kind, doc_id):
    ext = path.suffix.lower()
    blocks, media, locations, questions = [], {}, {}, []
    pages = None
    if ext == ".docx":
        pages = h.docx_pages(path)
        blocks, raw_media, locations = h.docx_extract(path)
        for name, data in raw_media.items():
            loc = locations.get(name, [])
            media[name] = (data, None, loc[0]["seq"] if loc else None)
    elif ext == ".pdf":
        blocks, media, pages = extract_pdf(path)
    elif ext == ".pptx":
        blocks, media, pages = extract_pptx(path)
    elif ext in {".xlsx", ".csv"}:
        blocks = spreadsheet_blocks(path)
        if kind == "domande":
            questions = [block["text"] for block in blocks]
    elif ext in IMAGES:
        data = normalized_image(path)
        text = image_text(data)
        if not text and kind != "domande":
            text = "[Immagine senza testo riconoscibile; interpretare soltanto la figura fornita.]"
        blocks = [{"type": "image_ocr", "text": text, "heading": path.stem, "seq": 0}]
        media[path.stem + ".png"] = (data, None, 0)
    else:
        text = path.read_text(encoding="utf-8-sig", errors="strict")
        # Separare paragrafi prima del chunking evita di troncare testi lunghi.
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        blocks = [{"type": "text", "text": h.clean_text(p), "heading": path.stem, "seq": i} for i, p in enumerate(paragraphs)]
    text = "\n\n".join(b["text"] for b in blocks if b.get("text"))
    if kind == "domande" and not questions:
        questions = h.extract_question_units(text, "pdf" if ext == ".pdf" else "generic")
    if not text.strip() and not questions:
        raise ValueError("Nessun testo o domanda riconoscibile nel file.")
    chunks = h.chunk_blocks(blocks, doc_id, path.name) if kind in {"sbobina", "programma"} else []
    return blocks, media, questions, chunks, pages


def ingest(root: Path, log=print):
    db = root / "corpus" / "corpus.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    h.create_empty_db(db)
    state_path = root / "state" / "ingest.json"
    state = read_json(state_path, {"files": {}})
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    counts = {"updated": 0, "unchanged": 0, "failed": [], "unsupported": []}
    doc_next = h.next_numeric_id([dict(x) for x in con.execute("SELECT document_id FROM documents")], "document_id", "DOC_", 6)
    chunk_next = h.next_numeric_id([dict(x) for x in con.execute("SELECT chunk_id FROM chunks")], "chunk_id", "CH_", 6)
    q_next = h.next_numeric_id([dict(x) for x in con.execute("SELECT question_id FROM questions")], "question_id", "Q_", 6)
    files = []
    for kind, folder in (("sbobina", "sbobine"), ("domande", "domande"), ("programma", "programma")):
        for path in sorted((root / "input" / folder).rglob("*")):
            if not path.is_file() or path.name.startswith((".", "~$")):
                continue
            if path.suffix.lower() not in SUPPORTED:
                counts["unsupported"].append(str(path.relative_to(root)))
            else:
                files.append((kind, path))
    try:
        present = {str(path.relative_to(root)) for _, path in files}
        removed = [dict(row) for row in con.execute("SELECT document_id,original_path FROM documents") if row["original_path"] not in present]
        for row in removed:
            for table, column in (("chunks", "document_id"), ("questions", "source_document_id"), ("images", "document_id"), ("documents", "document_id")):
                con.execute(f"DELETE FROM {table} WHERE {column}=?", (row["document_id"],))
            state["files"].pop(row["original_path"], None)
            log("RIMOSSO DAL CORPUS: " + row["original_path"])
        con.commit()
        for kind, path in files:
            key = str(path.relative_to(root))
            checksum = h.sha256_file(path)
            old = con.execute("SELECT * FROM documents WHERE original_path=?", (key,)).fetchone()
            if old and old["sha256"] == checksum:
                counts["unchanged"] += 1
                continue
            doc_id = old["document_id"] if old else f"DOC_{doc_next:06d}"
            if not old:
                doc_next += 1
            try:
                blocks, media, questions, chunks, pages = parse_document(path, kind, doc_id)
                con.execute("BEGIN")
                for table, column in (("chunks", "document_id"), ("questions", "source_document_id"), ("images", "document_id"), ("documents", "document_id")):
                    con.execute(f"DELETE FROM {table} WHERE {column}=?", (doc_id,))
                for chunk in chunks:
                    chunk["document_name"] = key
                    chunk["chunk_id"] = f"CH_{chunk_next:06d}"
                    chunk_next += 1
                    con.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(chunk[k] for k in ("chunk_id", "document_id", "document_name", "page_start", "page_end", "heading", "text", "token_count", "content_hash", "sequence_number")))
                for normalized, frequency in Counter(h.normalize_question_key(q) for q in questions).items():
                    original = next(q for q in questions if h.normalize_question_key(q) == normalized)
                    professor = ""
                    exam = read_json(root / "esame.json", {})
                    for name in exam.get("professori", []):
                        if name.lower() in original.lower():
                            professor = name
                            break
                    qid = f"Q_{q_next:06d}"
                    q_next += 1
                    con.execute("INSERT INTO questions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (qid, original, "", key, doc_id, "", "", professor, frequency, "", h.sha256_bytes(normalized.encode()), path.suffix.lower().lstrip(".")))
                for name, (data, page, sequence) in media.items():
                    sha = h.sha256_bytes(data)
                    width, height, fmt, perceptual_hash, reason = h.inspect_image(data)
                    if reason:
                        continue
                    iid = f"IMG_{sha[:24]}"
                    extension = (fmt or Path(name).suffix.lstrip(".")).lower()
                    if extension == "jpeg":
                        extension = "jpg"
                    relative = f"images/originals/{iid}.{extension}"
                    image_path = root / relative
                    image_path.parent.mkdir(parents=True, exist_ok=True)
                    if not image_path.exists():
                        image_path.write_bytes(data)
                    before = after = heading = ""
                    if blocks and sequence is not None:
                        before, after = h.image_context(blocks, sequence)
                        heading = next((b.get("heading", "") for b in blocks if b["seq"] == sequence), "")
                    if page is not None:
                        before = " ".join(b.get("text", "") for b in blocks if b.get("page_start") == page)[:1000]
                    row = {"image_id": iid, "document_id": doc_id, "source_document": key, "page_or_position": str(page if page is not None else f"block:{sequence}"), "path": relative, "filename_originale": name, "width": width, "height": height, "format": extension, "sha256": sha, "perceptual_hash": perceptual_hash, "caption": "", "heading": heading, "context_before": before, "context_after": after, "status": "kept", "filtered_reason": ""}
                    con.execute("INSERT INTO images VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row[k] for k in IMAGE_FIELDS))
                stamp = datetime.now(timezone.utc).isoformat()
                con.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (doc_id, path.name, key, path.suffix.lower().lstrip("."), kind, path.stat().st_size, checksum, pages, 1, int(bool(media)), "processed", stamp))
                con.commit()
                state["files"][key] = {"sha256": checksum, "document_id": doc_id, "status": "processed"}
                counts["updated"] += 1
                log(f"IMPORTATO: {key}")
            except Exception as error:
                con.rollback()
                counts["failed"].append({"file": key, "error": str(error)})
                state["files"][key] = {"status": "failed", "error": str(error)}
                log(f"ERRORE IMPORTAZIONE: {key}: {error}")
        h.rebuild_fts(con)
        images = [dict(r) for r in con.execute("SELECT * FROM images ORDER BY image_id, document_id")]
        write_manifest(root / "corpus" / "images_manifest.csv", images)
    finally:
        con.close()
    write_json(state_path, state)
    write_json(root / "output" / "reports" / "ingest.json", counts)
    if counts["unsupported"]:
        log("FILE NON SUPPORTATI: " + ", ".join(counts["unsupported"]))
    if counts["failed"] or counts["unsupported"]:
        raise ValueError("Importazione incompleta: correggi i file indicati nel report prima di generare.")
    return db
