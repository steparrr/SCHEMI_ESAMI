"""Manual, local diagnostic export with a strict technical-field allowlist."""
from __future__ import annotations

import json
import math
import platform
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from bottleneck_detector import detect
from common import digest, read_json
from telemetry import Telemetry

_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+00:00$")
_METRIC_NAMES = {"bytes", "pages", "images", "requests", "completed", "failed", "pdfs"}
_STATUSES = {"READY", "RETRY_READY", "HTML_OK", "PDF_OK", "RENDERING", "RENDER_FAILED",
             "FONTI_MANCANTI", "LOCAL_HTML_MISSING", "LIMITE_PAGINE", "SUPERSEDED",
             "DATA_OK", "DATA_FAILED", "FAILED", "INCOMPLETE", "PENDING", "SUBMITTED"}


def _safe_token(value):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else "unknown"


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _safe_number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _safe_event(event):
    try:
        metrics = json.loads(event["metrics_json"])
    except (TypeError, ValueError):
        metrics = {}
    if not isinstance(metrics, dict):
        metrics = {}
    return {"at": event["recorded_at_utc"] if _TIME.fullmatch(str(event["recorded_at_utc"])) else "unknown",
            "stage": _safe_token(event["stage"]), "event": _safe_token(event["event_name"]),
            "status": _safe_token(event["status"]), "duration_ms": _safe_number(event["duration_ms"]),
            "metrics": {key: value for key, raw in metrics.items() if key in _METRIC_NAMES
                        if (value := _safe_number(raw)) is not None}}


def build_sections(root: Path, event_limit=500):
    """Return every file that will enter the ZIP; no raw logs or source content."""
    root = Path(root)
    state = read_json(root / "state/pipeline.json", {})
    jobs = state.get("jobs") or {}
    latest = state.get("latest") or {}
    current = [jobs[key] for key in latest.values() if key in jobs]
    batches = state.get("batches") or []
    events = Telemetry(root).recent(event_limit)
    clean_events = [_safe_event(e) for e in reversed(events)]
    job_states = Counter(job.get("status") if job.get("status") in _STATUSES else "OTHER" for job in current)
    batch_states = Counter(_safe_token(batch.get("stage")) for batch in batches)
    summary = {"current_jobs": len(current), "job_states": dict(job_states),
               "batch_states": dict(batch_states),
               "accepted_batches": sum(bool(b.get("batch_id") or b.get("response_id")) for b in batches),
               "ambiguous_batches": sum(b.get("stage") in {"creating", "uncertain"}
                                        and not (b.get("batch_id") or b.get("response_id")) for b in batches),
               "pilot_approved": bool((state.get("pilot_review") or {}).get("approved_hash") == latest.get("1")
                                      and latest.get("1")),
               "pdf_ok": job_states.get("PDF_OK", 0)}
    durations = {}
    for event in clean_events:
        if event["duration_ms"] is not None and event["status"] == "success":
            durations.setdefault(event["stage"], []).append(event["duration_ms"])
    metrics = {stage: {"count": len(values), "min_ms": min(values), "max_ms": max(values),
                       "mean_ms": round(sum(values) / len(values), 2)} for stage, values in durations.items()}
    incidents = [{"code": item["code"], "severity": item["severity"], "schema_number": item["number"],
                  "detail": item["detail"], "suggestion": item["suggestion"]}
                 for item in detect(root, state=state)]
    warnings = ["Il report usa solo campi tecnici permessi; controlla comunque la preview prima di condividerlo.",
                "Non include documenti, domande, PDF, risposte API, log liberi o segreti.",
                "Le durate remote rappresentano osservazioni locali, non il tempo interno del server."]
    if not events:
        warnings.append("Telemetria assente: le durate storiche non sono disponibili.")
    body = {
        "report.md": ("# Diagnostica locale\n\n" + f"Schemi correnti: {len(current)}; PDF pronti: {summary['pdf_ok']}.\n"
                      + f"Batch ambigui: {summary['ambiguous_batches']}; avvisi: {len(incidents)}.\n\n"
                      + "Nessun nuovo invio API è stato eseguito per creare questo report.\n"),
        "state-summary.json": _json(summary),
        "incidents.json": _json(incidents),
        "metrics.json": _json(metrics),
        "telemetry.jsonl": "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in clean_events),
        "environment.json": _json({"platform": platform.system(), "python": sys.version_info[:3],
                                   "telemetry_schema": 1}),
        "warnings.txt": "\n".join(warnings) + "\n",
        "codex_prompt.txt": ("Analizza il report tecnico allegato in sola lettura. Distingui osservazioni certe, "
                             "ipotesi e dati mancanti. Non modificare file, non inviare richieste generative, "
                             "non assumere che il report contenga il materiale didattico.\n"),
    }
    manifest = {"schema_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "project_hash": digest(root.name), "files": {name: digest(content.encode("utf-8"))
                                                    for name, content in body.items()}}
    return {"manifest.json": _json(manifest), **body}


def preview(sections):
    return "\n".join(f"===== {name} =====\n{content}" for name, content in sections.items())


def export(root: Path, sections):
    root = Path(root)
    folder = root / "output/reports/diagnostic"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"diagnostic-{stamp}.zip"
    counter = 1
    while path.exists():
        path = folder / f"diagnostic-{stamp}-{counter}.zip"
        counter += 1
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sections.items():
            archive.writestr(name, content)
    return path
