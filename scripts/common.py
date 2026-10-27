from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path

PROJECT = Path(os.environ.get("SCHEMI_PROJECT_DIR", Path(__file__).resolve().parents[1])).expanduser().resolve()


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    # Un file di stato corrotto deve fermare il job, mai azzerare gli invii.
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def digest(data) -> str:
    if isinstance(data, Path):
        data = data.read_bytes()
    elif not isinstance(data, bytes):
        data = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name)
    text = "".join(x for x in text if not unicodedata.combining(x))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64]


def safe_title(title: str) -> str:
    title = re.sub(r'[\x00-\x1f/\\:*?"<>|]', " - ", str(title))
    title = re.sub(r"\s+", " ", title).strip(" .-") or "Argomento"
    # Un nome Unicode deve stare nel limite in byte del filesystem del Mac.
    while len(title.encode("utf-8")) > 170:
        title = title[:-1]
    return title.rstrip(" .-")


def pdf_filename(number: int, title: str) -> str:
    return f"{int(number)} - {safe_title(title)}.pdf"


def exam_dir(slug: str, project: Path = PROJECT) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise ValueError("Identificativo esame non valido.")
    root = project.resolve() / "esami" / slug
    if not root.resolve().is_relative_to((project.resolve() / "esami").resolve()):
        raise ValueError("La cartella esame esce dal progetto.")
    if not (root / "esame.json").is_file():
        raise ValueError(f"Esame inesistente: {slug}. Usa NUOVO_ESAME.command.")
    return root


def load_config(root: Path, project: Path = PROJECT):
    settings = read_json(project / "config" / "pipeline.json", {})
    exam = read_json(root / "esame.json", {})
    settings.update(exam.get("pipeline", {}))
    settings.update({"exam_id": root.name, "exam_name": exam["nome"], "exam_instructions": exam.get("istruzioni", ""), "mode": exam.get("modalita", "completa"), "max_pages": exam.get("max_pagine"), "execution_mode": exam.get("execution_mode", "batch")})
    for key in ("max_output_tokens", "retry_max_output_tokens", "max_chunks", "max_questions", "max_images", "token_budget", "micro_batch_size", "max_batch_bytes", "poll_seconds"):
        if not isinstance(settings.get(key), int) or settings[key] < 1:
            raise ValueError(f"Configurazione non valida: {key}")
    for key in ("schema_page_min", "schema_page_max", "schema_max_output_tokens", "schema_retry_max_output_tokens", "total_page_limit"):
        if key in settings and (type(settings[key]) is not int or settings[key] < 1):
            raise ValueError(f"Configurazione non valida: {key}")
    if bool(settings.get("schema_page_min")) != bool(settings.get("schema_page_max")):
        raise ValueError("Imposta insieme schema_page_min e schema_page_max.")
    if settings.get("schema_page_min") and settings["schema_page_min"] > settings["schema_page_max"]:
        raise ValueError("L'intervallo di pagine per schema non è valido.")
    if settings.get("retry_limit") not in (0, 1):
        raise ValueError("retry_limit deve essere 0 oppure 1.")
    if settings.get("image_mode") not in ("data-url", "manifest"):
        raise ValueError("image_mode non valido.")
    if not settings.get("model"):
        raise ValueError("Il modello è obbligatorio.")
    if settings["mode"] not in {"completa", "veloce"}:
        raise ValueError("Modalità non valida.")
    if settings["execution_mode"] not in {"batch", "urgent"}:
        raise ValueError("Tipo di invio non valido.")
    if settings["mode"] == "veloce" and (type(settings["max_pages"]) is not int or not 1 <= settings["max_pages"] <= 1000):
        raise ValueError("Nella modalità veloce imposta un massimo di pagine fra 1 e 1000.")
    return settings


def create_exam(name: str, project: Path = PROJECT) -> Path:
    slug = slugify(name)
    if not slug:
        raise ValueError("Inserisci il nome dell'esame.")
    root = project / "esami" / slug
    if root.exists():
        raise ValueError(f"La cartella {slug} esiste già; scegli un nome diverso.")
    for folder in ("input/sbobine", "input/domande", "input/programma", "corpus", "images/originals", "source_packs", "external_supplements", "output/pdf", "output/html", "output/raw", "output/reports", "batch", "state", "logs", "diagrams/generated"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    defaults = read_json(project / "config" / "pipeline.json", read_json(PROJECT / "config" / "pipeline.json", {}))
    pinned = {key: defaults[key] for key in ("model", "reasoning_effort", "max_output_tokens", "retry_max_output_tokens", "image_mode", "structured_outputs") if key in defaults}
    pinned.update(schema_page_min=25, schema_page_max=30, token_budget=60000, max_chunks=50,
                  schema_reasoning_effort="medium", schema_max_output_tokens=64000,
                  schema_retry_max_output_tokens=96000, compact_planner_input=True,
                  review_first_schema=True,
                  reasoning_effort="medium", max_output_tokens=64000,
                  retry_max_output_tokens=96000)
    write_json(root / "esame.json", {"nome": name.strip(), "attivo": True, "modalita": "completa", "max_pagine": None, "istruzioni": "", "professori": [], "pipeline": pinned, "execution_mode": "batch", "start_policy": "manual"})
    (root / "input" / "programma.txt").write_text(
        "# La suddivisione è automatica: puoi lasciare questo file vuoto.\n"
        "# Il programma ufficiale si carica in input/programma/.\n"
        "# Solo per una suddivisione manuale avanzata: una riga per schema.\n",
        encoding="utf-8",
    )
    return root


def prompt_text(root: Path, settings: dict, project: Path = PROJECT) -> str:
    custom = root / "prompt.txt"
    prompt = (custom if custom.exists() else project / "prompts" / "main.txt").read_text(encoding="utf-8")
    return prompt + "\n\nESAME: " + settings["exam_name"] + "\nISTRUZIONI SPECIFICHE: " + settings.get("exam_instructions", "")
