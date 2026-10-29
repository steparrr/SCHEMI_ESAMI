"""Parametri API separati dall'organizzazione dei materiali e dal renderer."""
from __future__ import annotations

from common import load_config, read_json, write_json

MODEL_KEYS = ("model", "reasoning_effort", "max_output_tokens", "retry_max_output_tokens", "image_mode", "structured_outputs")
PROFILE_LABELS = (
    ("model", "Modello API"),
    ("reasoning_effort", "Ragionamento generale"),
    ("schema_reasoning_effort", "Ragionamento schemi"),
    ("max_output_tokens", "Token massimi generali"),
    ("schema_max_output_tokens", "Token massimi schemi"),
    ("retry_max_output_tokens", "Token massimi retry generale"),
    ("schema_retry_max_output_tokens", "Token massimi retry schemi"),
    ("image_mode", "Modalità immagini"),
    ("image_detail", "Dettaglio immagini"),
    ("retry_limit", "Numero massimo retry"),
    ("micro_batch_size", "Richieste per micro batch"),
    ("review_first_schema", "Revisione del primo schema"),
    ("mode", "Obiettivo della materia"),
    ("execution_mode", "Modalità invio"),
)


def effective_profile_sources(root, project):
    """Read effective settings and their origin without changing any profile."""
    effective = load_config(root, project)
    global_config = read_json(project / "config/pipeline.json", {})
    exam = read_json(root / "esame.json", {})
    pinned = exam.get("pipeline") or {}
    rows = []
    for key, label in PROFILE_LABELS:
        if key not in effective:
            continue
        if key in {"mode", "execution_mode"}:
            source = "materia" if ("modalita" if key == "mode" else key) in exam else "default implicito"
        else:
            source = "materia" if key in pinned else "globale" if key in global_config else "default implicito"
        rows.append({"key": key, "label": label, "value": effective[key], "source": source})
    return rows


def format_effective_profile(rows):
    lines = ["Valori salvati ed effettivi per questa materia.",
             "I valori fissati nella materia non cambiano quando aggiorni il profilo globale.", ""]
    for row in rows:
        value = row["value"]
        lines.append(f"{row['label']}: {'sì' if value is True else 'no' if value is False else value} · origine: {row['source']}")
    return "\n".join(lines)


def validate_profile(raw):
    model = str(raw.get("model", "")).strip()
    if not model or len(model) > 180 or any(c.isspace() for c in model):
        raise ValueError("Inserisci l'identificativo API del modello.")
    effort = str(raw.get("reasoning_effort", "")).strip()
    if len(effort) > 32 or (effort and not effort.replace("-", "").isalnum()):
        raise ValueError("Livello di ragionamento non valido.")
    profile = {"model": model, "reasoning_effort": effort}
    for key in ("max_output_tokens", "retry_max_output_tokens"):
        value = int(raw.get(key, 24000 if key == "max_output_tokens" else 32000))
        if not 1000 <= value <= 200000:
            raise ValueError("Il limite di output deve essere fra 1000 e 200000 token.")
        profile[key] = value
    profile["image_mode"] = "data-url" if raw.get("image_mode", "data-url") == "data-url" else "manifest"
    profile["structured_outputs"] = bool(raw.get("structured_outputs", True))
    return profile


def apply_profile(body, settings):
    body["model"] = settings["model"]
    effort = settings.get("reasoning_effort", "")
    if effort:
        body["reasoning"] = {"effort": effort}
    else:
        body.pop("reasoning", None)
    body.setdefault("metadata", {})["pipeline"] = "schemi-esami-v2"
    return body


def save_default_profile(project, raw):
    profile = validate_profile(raw)
    path = project / "config" / "pipeline.json"
    config = read_json(path)
    config.update(profile)
    write_json(path, config)
    # Le materie esistenti hanno un proprio profilo: nessuna rigenerazione.
    return profile
