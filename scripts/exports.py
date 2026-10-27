from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

import fitz

from common import digest, pdf_filename, read_json, safe_title, write_json


def publish(root, job):
    relative = Path("output/pdf") / pdf_filename(job["numero"], job["title"])
    source, destination = root / job["pdf"], root / relative
    if not destination.exists() or digest(source) != digest(destination):
        temporary = destination.with_suffix(".tmp.pdf")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    job["public_pdf"] = str(relative)


def export_all(root, cfg, jobs, latest):
    rows = []
    for number, key in sorted(latest.items(), key=lambda pair: int(pair[0])):
        job = jobs[key]
        rows.append({"numero": int(number), "title": job["title"], "status": job["status"], "pages": (job.get("qc") or {}).get("pages"), "file": job.get("public_pdf") if job["status"] == "PDF_OK" else None, "hash": key})
    path = root / "output/reports/exports.json"
    old = read_json(path, {})
    result = {"rows": rows, "combined": None, "archive": None, "pages": None,
              "max_pages": cfg.get("max_pages") if cfg.get("mode") == "veloce" else cfg.get("total_page_limit")}
    if not rows or any(not row["file"] for row in rows):
        write_json(path, result)
        return result
    signature = digest([(row["hash"], row["title"], digest(root / row["file"])) for row in rows])
    final_name = safe_title(cfg["exam_name"]) + " - tutti gli schemi.pdf"
    combined = root / "output/pdf" / final_name
    archive = root / "output/pdf" / (safe_title(cfg["exam_name"]) + " - schemi.zip")
    if old.get("signature") == signature and combined.is_file() and archive.is_file():
        return old
    assembled = fitz.open()
    toc = []
    try:
        for row in rows:
            toc.append([1, f"{row['numero']} - {row['title']}", assembled.page_count + 1])
            with fitz.open(root / row["file"]) as chapter:
                assembled.insert_pdf(chapter)
        pages = assembled.page_count
        if result["max_pages"] is not None and pages > result["max_pages"]:
            raise ValueError(f"Il PDF unico supera il limite: {pages}/{result['max_pages']} pagine.")
        assembled.set_toc(toc)
        assembled.set_metadata({"title": cfg["exam_name"], "creator": "Schemi Esami V2"})
        version = root / "output/pdf/versioni" / ("raccolta_" + signature + ".pdf")
        version.parent.mkdir(parents=True, exist_ok=True)
        if not version.exists():
            assembled.save(version, garbage=3, deflate=True)
    finally:
        assembled.close()
    temp = combined.with_suffix(".tmp.pdf")
    shutil.copy2(version, temp)
    os.replace(temp, combined)
    temp_zip = archive.with_suffix(".tmp.zip")
    with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as package:
        for row in rows:
            package.write(root / row["file"], Path(row["file"]).name)
        package.write(combined, combined.name)
    os.replace(temp_zip, archive)
    result.update(signature=signature, combined=str(combined.relative_to(root)), archive=str(archive.relative_to(root)), pages=pages)
    write_json(path, result)
    return result
