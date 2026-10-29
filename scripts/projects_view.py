"""Fast, read-only project cards derived from authoritative local state."""
from __future__ import annotations

from common import read_json


def project_cards(app, rows=None):
    rows = app.list_exams() if rows is None else rows
    cards = []
    for row in rows:
        root = app.root(row["slug"])
        state = read_json(root / "state/pipeline.json", {})
        batches = state.get("batches") or []
        jobs = state.get("jobs") or {}
        latest = state.get("latest") or {}
        pilot = state.get("pilot_review") or {}
        current = [jobs[key] for key in latest.values() if key in jobs]
        total = len(latest) if latest else row["total"]
        ready = sum(bool(job.get("status") == "PDF_OK" and job.get("public_pdf") and
                         (root / job["public_pdf"]).is_file()) for job in current) if latest else row["ready"]
        if row.get("archived"):
            phase = "Archiviata · riattivabile"
        elif any(b.get("stage") in {"creating", "uncertain"} and not (b.get("batch_id") or b.get("response_id")) for b in batches):
            phase = "Invio incerto · riconciliare"
        elif any((b.get("batch_id") or b.get("response_id")) and not b.get("processed") for b in batches):
            phase = "OpenAI · stato osservato"
        elif pilot and pilot.get("approved_hash") != latest.get("1") and jobs.get(latest.get("1", ""), {}).get("status") == "PDF_OK":
            phase = "Primo PDF da approvare"
        elif ready == total and total:
            phase = "PDF pronti"
        elif row.get("busy"):
            phase = "Lavoro locale attivo"
        elif row.get("scheduled"):
            phase = "In attesa di avvio o materiali"
        else:
            phase = "Bozza o in pausa"
        checked = max((b.get("remote_checked_at") for b in batches if b.get("remote_checked_at")), default=None)
        cards.append({"slug": row["slug"], "name": row["name"], "ready": ready,
                      "total": total, "phase": phase, "last_remote_check": checked,
                      "archived": row.get("archived", False), "favorite": row.get("favorite", False),
                      "exam_date": row.get("exam_date"), "mode": row.get("mode"),
                      "pilot_approved": bool(pilot.get("approved_hash") == latest.get("1") and latest.get("1")),
                      "active_batches": sum(bool(b.get("batch_id") or b.get("response_id")) and not b.get("processed") for b in batches)})
    return cards


def arrange_cards(cards, visibility="Tutte", order="Nome"):
    if visibility not in {"Tutte", "Attive", "Archiviate", "Preferite"}:
        raise ValueError("Filtro progetti non valido.")
    if order not in {"Nome", "Data esame", "PDF pronti"}:
        raise ValueError("Ordine progetti non valido.")
    filtered = [card for card in cards if
                visibility == "Tutte" or
                (visibility == "Attive" and not card["archived"]) or
                (visibility == "Archiviate" and card["archived"]) or
                (visibility == "Preferite" and card["favorite"])]
    if order == "Data esame":
        return sorted(filtered, key=lambda card: (card["exam_date"] is None, card["exam_date"] or "", card["name"].casefold(), card["slug"]))
    if order == "PDF pronti":
        return sorted(filtered, key=lambda card: (-card["ready"], card["name"].casefold(), card["slug"]))
    return sorted(filtered, key=lambda card: (card["name"].casefold(), card["slug"]))


def pdf_versions(root):
    """Read PDF versions already on disk; never infer that an old version is current."""
    root = root.resolve()
    state = read_json(root / "state/pipeline.json", {})
    latest = state.get("latest") or {}
    jobs = state.get("jobs") or {}
    folder = (root / "output/pdf").resolve()
    rows = []
    for key, job in jobs.items():
        if job.get("kind") != "schema" or not isinstance(job.get("numero"), int):
            continue
        relative = job.get("pdf")
        if not isinstance(relative, str) or not relative:
            continue
        path = (root / relative).resolve()
        if not path.is_relative_to(folder) or path.suffix.lower() != ".pdf" or not path.is_file():
            continue
        current = latest.get(str(job["numero"])) == key
        rows.append({"number": job["numero"], "title": str(job.get("title") or "Schema")[:160],
                     "request_hash": str(key)[:12], "current": current,
                     "status": job.get("status") or "sconosciuto",
                     "path": path, "bytes": path.stat().st_size,
                     "modified_at": path.stat().st_mtime})
    return sorted(rows, key=lambda row: (row["number"], not row["current"], -row["modified_at"], row["request_hash"]))
