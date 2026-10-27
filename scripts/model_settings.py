"""Parametri API separati dall'organizzazione dei materiali e dal renderer."""
from __future__ import annotations

from common import read_json, write_json

MODEL_KEYS = ("model", "reasoning_effort", "max_output_tokens", "retry_max_output_tokens", "image_mode", "structured_outputs")


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
