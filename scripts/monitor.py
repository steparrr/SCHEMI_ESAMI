"""A dated, honest view of the local -> OpenAI -> local pipeline."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from common import read_json


LOCAL_NAMES = {
    "MATERIALI_MANCANTI": "Materiali mancanti",
    "LETTURA_MATERIALI": "Analisi dei materiali",
    "ORGANIZZAZIONE_SCHEMI": "Organizzazione degli schemi",
    "OK": "Materiali e piano pronti",
}
REMOTE_NAMES = {"validating": "verifica dell'invio", "in_progress": "elaborazione sul server",
                "queued": "in coda sul server", "finalizing": "raccolta dei risultati",
                "completed": "completato", "failed": "non riuscito", "expired": "scaduto",
                "cancelled": "annullato", "incomplete": "risposta incompleta"}


def _remote_status(value):
    return (REMOTE_NAMES[value] + f" ({value})") if value in REMOTE_NAMES else (value or "stato non disponibile")


def _time(value):
    if not value:
        return "mai"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%d/%m %H:%M:%S")
    except (TypeError, ValueError):
        return str(value)


def overview(root: Path, worker_active=False):
    project = root.parent.parent
    state = read_json(root / "state/pipeline.json", {})
    report = read_json(root / "output/reports/pipeline_report.json", {})
    jobs = list((state.get("jobs") or {}).values())
    batches = state.get("batches") or []
    planning = [job for job in jobs if job.get("kind") in {"analysis", "plan"}]
    schemes = [job for job in jobs if job.get("kind") == "schema" and job.get("status") != "SUPERSEDED"]
    local_phase = state.get("input_status") or "Materiali salvati"
    heartbeat = read_json(project / "state/worker-heartbeat.json", {})
    try:
        last_beat = datetime.fromisoformat(heartbeat.get("checked_at", "").replace("Z", "+00:00"))
        background_alive = (datetime.now(timezone.utc) - last_beat).total_seconds() < 150
    except ValueError:
        background_alive = False
    if local_phase.startswith("ERRORE"):
        local = local_phase
    else:
        local = LOCAL_NAMES.get(local_phase, local_phase)
    local += " · motore locale attivo" if worker_active or background_alive else " · motore locale fermo"
    if heartbeat.get("errors", {}).get(root.name):
        local += " · errore: " + str(heartbeat["errors"][root.name])[:180]
    local += f" · analisi/piano completati {sum(j.get('status') == 'DATA_OK' for j in planning)}/{len(planning)}"
    backups = sorted((project / "backups").glob(root.name + "-20*")) if (project / "backups").is_dir() else []
    if backups:
        local += " · copia di sicurezza " + backups[-1].name[-10:]
    agent_error = project / "state/worker-agent-error.txt"
    if agent_error.exists():
        local += " · ATTENZIONE: avvio automatico non riuscito"

    active = [b for b in batches if (b.get("batch_id") or b.get("response_id")) and not b.get("processed")]
    uncertain = [b for b in batches if b.get("stage") in {"creating", "uncertain"} and not (b.get("batch_id") or b.get("response_id"))]
    waiting_local = [b for b in batches if b.get("stage") in {"prepared", "uploaded"} and not b.get("processed")]
    checked = [b.get("remote_checked_at") for b in active if b.get("remote_checked_at")]
    last_check = max(checked) if checked else None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_check.replace("Z", "+00:00"))).total_seconds() if last_check else None
    except ValueError:
        age = None
    stale = bool(active and (age is None or age > 180))
    if active:
        parts = []
        for b in active:
            counts = b.get("request_counts") or {}
            progress = f" · {counts.get('completed', 0)}/{counts['total']} richieste completate" if counts.get("total") else ""
            parts.append(f"{_remote_status(b.get('status'))}{progress}")
        server = f"{len(active)} lavori accettati da OpenAI: " + "; ".join(parts)
        server += (" · ultimo controllo " + _time(last_check)) if last_check else " · controllo non ancora registrato"
        if stale:
            server += " · dato da aggiornare"
    elif uncertain:
        server = "Invio da riconciliare: l'accettazione dal server non è ancora confermata"
    elif waiting_local:
        server = "Richieste preparate sul Mac: non ancora accettate da OpenAI"
    elif batches:
        server = "Nessun batch attivo · " + str(sum(bool(b.get("processed")) for b in batches)) + " batch conclusi"
    else:
        server = "Nessuna richiesta API inviata"

    ready = sum(j.get("status") == "PDF_OK" for j in schemes)
    rendering = sum(j.get("status") == "RENDERING" for j in schemes)
    local_finish = f"PDF pronti {ready}/{len(schemes)}"
    if rendering:
        local_finish += f" · {rendering} in impaginazione locale"
    elif any(b.get("status") == "completed" and not b.get("processed") for b in batches):
        local_finish += " · risultati server da scaricare"
    elif active:
        local_finish += " · in attesa dei risultati server"
    elif waiting_local:
        local_finish += " · in attesa dell'invio"
    elif ready == len(schemes) and schemes:
        local_finish += " · completo"
    else:
        local_finish += " · da completare"
    return {"local": local, "server": server, "finish": local_finish,
            "active_batch_ids": [b.get("batch_id") or b.get("response_id") for b in active],
            "stale": stale, "report_generated_at": report.get("generated_at"),
            "worker_alive": background_alive or worker_active}


def batch_lines(state):
    lines = []
    for batch in state.get("batches", []):
        batch_id = batch.get("batch_id") or batch.get("response_id") or "ID non ancora assegnato"
        counts = batch.get("request_counts") or {}
        progress = (f" · completate {counts.get('completed', 0)}/{counts['total']}, "
                    f"fallite {counts.get('failed', 0)}" if counts.get("total") else "")
        label = "Urgente" if batch.get("mode") == "urgent" else "Batch"
        lines.append(f"{label} {batch_id} · locale {batch.get('stage')} · OpenAI {_remote_status(batch.get('status'))}{progress}")
        lines.append("  ultimo controllo server: " + (_time(batch["remote_checked_at"]) if batch.get("remote_checked_at") else "non registrato"))
        if batch.get("error"):
            lines.append("  ultimo errore: " + str(batch["error"])[:1000])
    return lines
