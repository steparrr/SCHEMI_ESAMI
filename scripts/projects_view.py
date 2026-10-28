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
        if any(b.get("stage") in {"creating", "uncertain"} and not (b.get("batch_id") or b.get("response_id")) for b in batches):
            phase = "Invio incerto · riconciliare"
        elif any((b.get("batch_id") or b.get("response_id")) and not b.get("processed") for b in batches):
            phase = "OpenAI · stato osservato"
        elif pilot and not pilot.get("approved_hash") and jobs.get(latest.get("1", ""), {}).get("status") == "PDF_OK":
            phase = "Primo PDF da approvare"
        elif row["ready"] == row["total"] and row["total"]:
            phase = "PDF pronti"
        elif row.get("busy"):
            phase = "Lavoro locale attivo"
        elif row.get("scheduled"):
            phase = "In attesa di avvio o materiali"
        else:
            phase = "Bozza o in pausa"
        checked = max((b.get("remote_checked_at") for b in batches if b.get("remote_checked_at")), default=None)
        cards.append({"slug": row["slug"], "name": row["name"], "ready": row["ready"],
                      "total": row["total"], "phase": phase, "last_remote_check": checked,
                      "pilot_approved": bool(pilot.get("approved_hash") == latest.get("1") and latest.get("1")),
                      "active_batches": sum(bool(b.get("batch_id") or b.get("response_id")) and not b.get("processed") for b in batches)})
    return cards
