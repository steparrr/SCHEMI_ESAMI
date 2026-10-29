"""Conservative estimate for remaining local PDF rendering only."""
from __future__ import annotations

import json
import math
from pathlib import Path


def _percentile(sorted_values, fraction):
    position = (len(sorted_values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def _sample(event, signature):
    if event.get("stage") != "pdf_render" or event.get("status") != "success":
        return None
    duration = event.get("duration_ms")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        return None
    try:
        metrics = json.loads(event.get("metrics_json") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(metrics, dict) or metrics.get("renderer_signature") != signature:
        return None
    pages = metrics.get("pages")
    if type(pages) is not int or pages < 1:
        return None
    return pages, duration


def estimate_local_rendering(jobs, batches, latest, pilot, events, signature, root=None):
    """Return a duration interval only when every pending job is locally renderable."""
    def unavailable(reason):
        return {"status": "unavailable", "reason": reason, "low_ms": None,
                "high_ms": None, "samples_min": 0}
    if not jobs:
        return unavailable("Piano degli schemi non ancora disponibile.")
    if root is not None:
        root = Path(root).resolve()
        for job in jobs:
            if job.get("status") not in {"PDF_OK", "HTML_OK"}:
                continue
            fields = ("pdf", "public_pdf") if job["status"] == "PDF_OK" else ("html",)
            for field in fields:
                relative = job.get(field)
                if not isinstance(relative, str) or not relative:
                    return unavailable("Un artefatto locale corrente non è disponibile sul disco.")
                path = (root / relative).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    return unavailable("Un artefatto locale corrente non è disponibile sul disco.")
    first = next((job for job in jobs if job.get("numero") == 1), None)
    if first and first.get("status") == "PDF_OK" and pilot and pilot.get("approved_hash") != latest.get("1"):
        return {"status": "waiting_user", "reason": "Il primo PDF attende la tua approvazione.",
                "low_ms": None, "high_ms": None, "samples_min": 0}
    if all(job.get("status") == "PDF_OK" for job in jobs):
        return {"status": "complete", "reason": "Tutti i PDF correnti risultano pronti.",
                "low_ms": 0, "high_ms": 0, "samples_min": 0}
    if any(not batch.get("processed") and batch.get("stage") not in {"superseded", "rejected"}
           for batch in batches):
        return unavailable("Sono presenti invii o risposte da verificare; l'attesa OpenAI non è prevedibile qui.")
    pending = [job for job in jobs if job.get("status") != "PDF_OK"]
    if any(job.get("status") != "HTML_OK" for job in pending):
        return unavailable("Non tutti gli schemi restanti hanno HTML pronto per l'impaginazione locale.")
    if not signature:
        return unavailable("Versione del renderer non disponibile.")
    samples = [sample for event in events if (sample := _sample(event, signature)) is not None]
    low_ms = high_ms = 0.0
    minimum = None
    for job in pending:
        target = job.get("page_budget") or job.get("page_min")
        if type(target) is not int or target < 1:
            return unavailable("Manca un numero di pagine confrontabile per uno schema.")
        durations = sorted(duration for pages, duration in samples
                           if target * .8 <= pages <= target * 1.2)
        if len(durations) < 20:
            return unavailable("Meno di 20 rendering riusciti con stessa versione e pagine simili.")
        minimum = len(durations) if minimum is None else min(minimum, len(durations))
        low_ms += _percentile(durations, .10)
        high_ms += _percentile(durations, .90)
    return {"status": "estimated", "reason": "Solo impaginazione locale; Mac acceso e nessuna attesa remota inclusa.",
            "low_ms": round(low_ms), "high_ms": round(high_ms), "samples_min": minimum}
