"""Validation for the guided project flow; no disk writes or API calls."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from ingest import SUPPORTED
from local_app import KINDS, MAX_FILE_BYTES, filename


def validate_wizard(values, files, start=False):
    name = str(values.get("name") or "").strip()
    if not name or len(name) > 160:
        raise ValueError("Inserisci il nome della materia (massimo 160 caratteri).")
    short = str(values.get("short_title") or "").strip()
    if len(short) > 80:
        raise ValueError("Il titolo breve non può superare 80 caratteri.")
    exam_date = str(values.get("exam_date") or "").strip()
    if exam_date:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", exam_date):
                raise ValueError("Formato non valido")
            date.fromisoformat(exam_date)
        except ValueError as error:
            raise ValueError("La data dell'esame deve essere nel formato AAAA-MM-GG.") from error
    mode = values.get("mode", "completa")
    if mode not in {"completa", "veloce"}:
        raise ValueError("Scegli Completa o Veloce.")
    try:
        max_pages = int(values.get("max_pages") or 0) if mode == "veloce" else None
    except (TypeError, ValueError) as error:
        raise ValueError("Indica un numero intero di pagine.") from error
    if mode == "veloce" and not 1 <= max_pages <= 1000:
        raise ValueError("Nella modalità Veloce indica da 1 a 1000 pagine totali.")
    execution = values.get("execution", "batch")
    if execution not in {"batch", "urgent", "night"}:
        raise ValueError("Modalità di produzione non valida.")
    selected = {}
    for kind in KINDS:
        paths = []
        seen_names = set()
        for raw in files.get(kind, []):
            path = Path(raw).resolve()
            filename(path.name)
            if not path.is_file() or path.suffix.lower() not in SUPPORTED:
                raise ValueError("File non supportato: " + path.name)
            if not 0 < path.stat().st_size <= MAX_FILE_BYTES:
                raise ValueError("File vuoto o superiore a 512 MB: " + path.name)
            if path.name.casefold() in seen_names:
                raise ValueError("Due file hanno lo stesso nome nella stessa area: " + path.name)
            seen_names.add(path.name.casefold())
            paths.append(path)
        selected[kind] = paths
    if start and not selected["materiali"]:
        raise ValueError("Aggiungi almeno una sbobina o un appunto prima di avviare. Puoi salvare una bozza senza file.")
    return ({"name": name, "short_title": short, "exam_date": exam_date or None,
             "mode": mode, "max_pages": max_pages,
             "instructions": str(values.get("instructions") or "")[:20000],
             "illustrator_enabled": bool(values.get("illustrator_enabled")),
             "execution": execution}, selected)
