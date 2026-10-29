"""Read-only local project view. No remote client is constructed here."""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from pathlib import Path

from bottleneck_detector import detect
from common import read_json
from local_eta import estimate_local_rendering
from monitor import overview
from pipeline import render_signature
from projects_view import project_cards
from telemetry import Telemetry


def _percentile(sorted_values, fraction):
    position = (len(sorted_values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    return round(sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower), 1)


def summarize_performance(events):
    """Descriptive local stage timings; percentiles require 20 successes."""
    durations = defaultdict(list)
    failures = Counter()
    for event in events:
        stage = event["stage"]
        if event["status"] == "failure":
            failures[stage] += 1
        if event["status"] == "success" and event["duration_ms"] is not None:
            durations[stage].append(event["duration_ms"])
    performance = []
    for stage in sorted(set(durations) | set(failures)):
        values = sorted(durations[stage])
        enough = len(values) >= 20
        performance.append({"stage": stage, "success": len(values), "failed": failures[stage],
                            "mean_ms": round(statistics.mean(values), 1) if values else None,
                            "median_ms": round(statistics.median(values), 1) if values else None,
                            "p90_ms": _percentile(values, .90) if enough else None,
                            "p95_ms": _percentile(values, .95) if enough else None,
                            "min_ms": round(values[0], 1) if values else None,
                            "max_ms": round(values[-1], 1) if values else None})
    return performance


def _usage_triplet(usage):
    if not isinstance(usage, dict):
        return None
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if type(input_tokens) is not int or type(output_tokens) is not int or input_tokens < 0 or output_tokens < 0:
        return None
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
    cached = cached if type(cached) is int and 0 <= cached <= input_tokens else 0
    return input_tokens, output_tokens, cached


def observed_token_usage(jobs):
    """Sum valid retained attempts; older jobs fall back to their last usage."""
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
              "jobs_with_usage": 0, "attempts_with_usage": 0,
              "by_mode": {mode: {"input_tokens": 0, "output_tokens": 0, "attempts": 0}
                          for mode in ("batch", "urgent", "unknown")}}
    for job in jobs.values():
        attempts = job.get("usage_attempts")
        rows = attempts.values() if isinstance(attempts, dict) else []
        valid = [(item, row.get("mode") if row.get("mode") in ("batch", "urgent") else "unknown")
                 for row in rows if isinstance(row, dict)
                 if (item := _usage_triplet(row.get("usage"))) is not None]
        if not valid:
            fallback = _usage_triplet(job.get("usage"))
            if fallback is not None:
                valid.append((fallback, "unknown"))
        if valid:
            totals["jobs_with_usage"] += 1
            totals["attempts_with_usage"] += len(valid)
            for (input_tokens, output_tokens, cached), mode in valid:
                totals["input_tokens"] += input_tokens
                totals["output_tokens"] += output_tokens
                totals["cached_input_tokens"] += cached
                totals["by_mode"][mode]["input_tokens"] += input_tokens
                totals["by_mode"][mode]["output_tokens"] += output_tokens
                totals["by_mode"][mode]["attempts"] += 1
    return totals


def global_snapshot(app):
    """Read all local projects and conservative incidents without remote checks."""
    projects = []
    for card in project_cards(app):
        incidents = detect(app.root(card["slug"]))
        projects.append({"slug": card["slug"], "name": card["name"],
                         "phase": card["phase"], "ready": card["ready"],
                         "total": card["total"], "last_remote_check": card["last_remote_check"],
                         "incidents": incidents})
    return projects


def format_global_snapshot(projects):
    lines = [f"Materie locali: {len(projects)}", "Stati e avvisi letti dal disco; nessun controllo OpenAI eseguito.", ""]
    for project in projects:
        lines.append(f"{project['name']} · PDF {project['ready']}/{project['total']} · {project['phase']}")
        lines.append("  Ultimo controllo remoto registrato: " + (project["last_remote_check"] or "mai"))
        if project["incidents"]:
            lines.extend(f"  {item['severity']} · {item['code']} · {item['detail']}"
                         for item in project["incidents"])
        else:
            lines.append("  Nessun avviso rilevato.")
        lines.append("")
    return "\n".join(lines).rstrip()


def snapshot(root: Path, worker_active=False):
    root = Path(root)
    state = read_json(root / "state/pipeline.json", {})
    exam = read_json(root / "esame.json", {})
    jobs = state.get("jobs") or {}
    latest = state.get("latest") or {}
    current = [jobs[key] for key in latest.values() if key in jobs]
    current.sort(key=lambda job: int(job.get("numero") or 0))
    batches = state.get("batches") or []
    events = Telemetry(root).recent(1000)
    performance = summarize_performance(events)
    signature = None
    if any(job.get("status") == "HTML_OK" for job in current):
        try:
            signature = render_signature(root.parent.parent)
        except (OSError, ValueError):
            pass
    eta = estimate_local_rendering(current, batches, latest, state.get("pilot_review") or {}, events, signature, root=root)
    incidents = []
    for job in current:
        if job.get("error"):
            incidents.append({"kind": "schema", "number": job.get("numero"),
                              "status": job.get("status"), "detail": str(job["error"])[:300]})
    for batch in batches:
        if batch.get("error"):
            incidents.append({"kind": "batch", "status": batch.get("status"),
                              "detail": str(batch["error"])[:300]})
    for item in detect(root, state=state):
        incidents.append({"kind": item["code"], "status": item["severity"],
                          "detail": item["detail"], "suggestion": item["suggestion"],
                          "number": item["number"]})
    pilot = state.get("pilot_review") or {}
    approved = bool(pilot.get("approved_hash") and pilot.get("approved_hash") == latest.get("1"))
    ready = bool(current and current[0].get("numero") == 1 and current[0].get("status") == "PDF_OK")
    return {
        "project": root.name, "name": exam.get("nome", root.name),
        "execution_mode": exam.get("execution_mode", "batch"),
        "summary": overview(root, worker_active),
        "counts": {"pdf_ok": sum(job.get("status") == "PDF_OK" for job in current),
                   "html_ok": sum(job.get("status") in {"HTML_OK", "RENDERING", "PDF_OK"} for job in current),
                   "total": len(current),
                   "accepted_batches": sum(bool(batch.get("batch_id") or batch.get("response_id")) for batch in batches),
                   "waiting_batches": sum(batch.get("stage") in {"prepared", "uploaded"} for batch in batches)},
        "pilot": {"generated": ready, "ready_for_review": ready and not approved, "approved": approved},
        "jobs": [{"number": job.get("numero"), "title": job.get("title"), "hash": str(job.get("hash", ""))[:12],
                  "status": job.get("status"), "retry": job.get("retry_count", 0),
                  "pdf": job.get("public_pdf") if job.get("status") == "PDF_OK" else None,
                  "error": str(job.get("error") or "")[:300]} for job in current],
        "batches": [{"mode": batch.get("mode"), "id": batch.get("batch_id") or batch.get("response_id"),
                     "stage": batch.get("stage"), "status": batch.get("status"),
                     "counts": batch.get("request_counts") or {}, "checked_at": batch.get("remote_checked_at")}
                    for batch in batches],
        "performance": performance, "incidents": incidents, "local_eta": eta,
        "observed_usage": observed_token_usage(jobs),
        "events": [{"at": e["recorded_at_utc"], "stage": e["stage"], "name": e["event_name"],
                    "status": e["status"], "duration_ms": e["duration_ms"],
                    "hash": (e["request_hash"] or "")[:12]} for e in events[:100]],
        "telemetry_available": bool(events),
    }


def format_snapshot(view):
    counts = view["counts"]
    eta = view["local_eta"]
    usage = view["observed_usage"]
    eta_line = (f"Impaginazione locale stimata: {max(1, round(eta['low_ms'] / 1000))}–"
                f"{max(1, round(eta['high_ms'] / 1000))} secondi per gli schemi restanti "
                f"(almeno {eta['samples_min']} misure comparabili). {eta['reason']}"
                if eta["status"] == "estimated" else "Impaginazione locale: " + eta["reason"])
    lines = [f"{view['name']} · {view['execution_mode']}",
             f"PDF pronti {counts['pdf_ok']}/{counts['total']} · HTML pronti {counts['html_ok']}/{counts['total']}",
             f"Batch accettati {counts['accepted_batches']} · in attesa locale {counts['waiting_batches']}",
             (f"Token nelle risposte conservate ({usage['jobs_with_usage']} job, {usage['attempts_with_usage']} tentativi): input {usage['input_tokens']:,} "
              f"(di cui cache {usage['cached_input_tokens']:,}) · output {usage['output_tokens']:,}. "
              "Dato parziale, non spesa fatturata." if usage["jobs_with_usage"] else
              "Token locali: non disponibili; per la spesa reale consulta la dashboard OpenAI."),
             ("Per modalità: " + " · ".join(
                 f"{label} {usage['by_mode'][mode]['attempts']} tentativi, "
                 f"input {usage['by_mode'][mode]['input_tokens']:,}, output {usage['by_mode'][mode]['output_tokens']:,}"
                 for mode, label in (("batch", "Batch"), ("urgent", "Urgente"), ("unknown", "Non attribuiti"))
                 if usage["by_mode"][mode]["attempts"])
              if usage["attempts_with_usage"] else ""),
             "Pilota: " + ("approvato" if view["pilot"]["approved"] else
                           "pronto per revisione" if view["pilot"]["ready_for_review"] else "in attesa"),
             eta_line, "", "Mac → OpenAI → Mac", view["summary"]["local"], view["summary"]["server"],
             view["summary"]["finish"], "", "Schemi correnti:"]
    lines += [f"{j['number']:02d} · {j['title']} · {j['status']} · hash {j['hash']} · retry {j['retry']}"
              for j in view["jobs"] if isinstance(j["number"], int)]
    lines += ["", "Batch (stati osservati):"]
    for b in view["batches"]:
        lines.append(f"{b['mode'] or 'batch'} · {b['id'] or 'ID non confermato'} · locale {b['stage']} · server {b['status'] or 'non noto'}"
                     f" · ultimo controllo {b['checked_at'] or 'mai'}")
    lines += ["", "Durate locali misurate (p90/p95 solo con almeno 20 successi per fase; lavori di dimensioni diverse possono mescolarsi):" if view["telemetry_available"] else
              "Durate: non disponibili per questo progetto storico."]
    for row in view["performance"]:
        lines.append(f"{row['stage']} · riuscite {row['success']} · errori {row['failed']}"
                     f" · mediana {row['median_ms'] if row['median_ms'] is not None else 'n/d'} ms"
                     f" · p90 {row['p90_ms'] if row['p90_ms'] is not None else 'n/d'} ms"
                     f" · p95 {row['p95_ms'] if row['p95_ms'] is not None else 'n/d'} ms"
                     f" · massimo {row['max_ms'] if row['max_ms'] is not None else 'n/d'} ms")
    lines += ["", "Anomalie:"]
    lines += [f"{i['kind']} · {i['status']} · {i['detail']}"
              + (" Azione: " + i["suggestion"] if i.get("suggestion") else "")
              for i in view["incidents"][:10]] or ["Nessuna anomalia registrata."]
    lines += ["", "Ultimi eventi locali:"]
    lines += [f"{e['at']} · {e['stage']} · {e['name']} · {e['status'] or '—'} · "
              f"{e['duration_ms'] if e['duration_ms'] is not None else 'n/d'} ms"
              for e in view["events"][:30]]
    return "\n".join(lines)
