"""Conservative, read-only incident rules over local state and observed remote checks."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from common import read_json


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def detect(root: Path, *, now=None, state=None):
    root = Path(root)
    current_time = now or datetime.now(timezone.utc)
    state = state if state is not None else read_json(root / "state/pipeline.json", {})
    jobs = state.get("jobs") or {}
    latest = state.get("latest") or {}
    incidents = []

    def add(code, severity, detail, suggestion, *, number=None, request_hash=None):
        incidents.append({"code": code, "severity": severity, "detail": detail,
                          "suggestion": suggestion, "number": number,
                          "request_hash": request_hash})

    for number, key in latest.items():
        job = jobs.get(key)
        if not job:
            add("CURRENT_JOB_MISSING", "error", "Lo schema corrente non è nello stato dei job.",
                "Non avviare nuovi invii; controlla lo stato locale e i backup.", number=int(number), request_hash=key)
            continue
        status = job.get("status")
        if status in {"HTML_OK", "RENDERING", "RENDER_FAILED"}:
            html = root / (job.get("html") or "__missing__")
            if not html.is_file():
                add("CURRENT_HTML_MISSING", "error", "HTML corrente non trovato sul disco.",
                    "Controlla Search and Rescue sui risultati già salvati; nessun nuovo invio automatico.",
                    number=int(number), request_hash=key)
        if status == "PDF_OK":
            version = root / (job.get("pdf") or "__missing__")
            if not version.is_file():
                add("CURRENT_PDF_MISSING", "error", "Il PDF versionato corrente non è sul disco.",
                    "Controlla le copie locali; non usare il solo stato PDF_OK come prova del file.",
                    number=int(number), request_hash=key)
            published = root / (job.get("public_pdf") or "__missing__")
            if not published.is_file():
                add("CURRENT_PUBLIC_PDF_MISSING", "error", "Il PDF pubblico dello schema corrente non è sul disco.",
                    "Verifica il PDF versionato e usa solo il recupero locale controllato.",
                    number=int(number), request_hash=key)
        if status == "RENDERING":
            started = _parse_time(job.get("render_started_at"))
            if started and (current_time - started).total_seconds() > 7200:
                add("RENDER_OBSERVATION_OLD", "warning", "Il rendering non registra un progresso da oltre due ore.",
                    "Verifica worker e lock prima di tentare un recupero locale.",
                    number=int(number), request_hash=key)

    for batch in state.get("batches") or []:
        if batch.get("processed"):
            continue
        stage = batch.get("stage")
        remote_id = batch.get("batch_id") or batch.get("response_id")
        if stage in {"creating", "uncertain"} and not remote_id:
            add("REMOTE_ACCEPTANCE_UNCERTAIN", "warning", "Accettazione dell'invio non confermata; possibile costo già sostenuto.",
                "Riconcilia con OpenAI. Non inviare una nuova richiesta per tentativo.")
        elif remote_id:
            checked = _parse_time(batch.get("remote_checked_at"))
            if checked is None or (current_time - checked).total_seconds() > 6 * 3600:
                add("REMOTE_OBSERVATION_OLD", "info", "L'ultimo stato remoto osservato è vecchio o assente.",
                    "Controlla lo stato remoto con l'azione esplicita; l'attesa non prova uno stallo.")

    pilot = state.get("pilot_review") or {}
    first = jobs.get(latest.get("1", ""), {})
    if first.get("status") == "PDF_OK" and pilot and pilot.get("approved_hash") != latest.get("1"):
        add("PILOT_WAITING_FOR_USER", "info", "Il primo PDF attende la revisione dell'utente.",
            "Apri il primo PDF e approva o descrivi le correzioni; non è uno stallo computazionale.", number=1)

    exam = read_json(root / "esame.json", {})
    heartbeat = read_json(root.parent.parent / "state/worker-heartbeat.json", {})
    observed = _parse_time(heartbeat.get("checked_at"))
    if exam.get("attivo") and observed and (current_time - observed).total_seconds() > 1800:
        add("LOCAL_HEARTBEAT_OLD", "info", "Il worker locale non registra un controllo recente.",
            "Verifica se il Mac è acceso e il worker è avviato; questo dato da solo non prova uno stallo.")
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = None
    if free is not None and free < 512 * 1024 * 1024:
        severity = "critical" if free < 128 * 1024 * 1024 else "warning"
        add("DISK_SPACE_LOW", severity, "Spazio libero sotto 512 MiB: i salvataggi locali potrebbero fallire.",
            "Libera spazio sul disco prima di avviare nuovi rendering o esportazioni.")

    seen = set()
    unique = []
    for item in incidents:
        key = item["code"], item["number"]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique
