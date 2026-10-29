"""Local project housekeeping; no content, credentials or remote work copied."""
from __future__ import annotations

from common import create_exam, load_config, read_json, write_json
from local_app import edit_lock
from model_settings import MODEL_KEYS

_COPY_KEYS = set(MODEL_KEYS) | {"schema_page_min", "schema_page_max", "token_budget", "max_chunks",
                                "schema_reasoning_effort", "schema_max_output_tokens",
                                "schema_retry_max_output_tokens", "compact_planner_input",
                                "review_first_schema", "safe_table_layout", "hide_sources_section"}


def duplicate_settings(app, source_slug, new_name):
    source = app.root(source_slug)
    original = read_json(source / "esame.json")
    load_config(source, app.project)  # Refuse to duplicate an invalid active configuration.
    name = str(new_name).strip()
    if not name or len(name) > 160:
        raise ValueError("Indica un nuovo nome di materia entro 160 caratteri.")
    destination = create_exam(name, app.project)
    exam = read_json(destination / "esame.json")
    exam.update(modalita=original.get("modalita", "completa"),
                max_pagine=original.get("max_pagine"), attivo=False,
                start_policy="manual", execution_mode="batch", istruzioni="",
                titolo_breve="", data_esame=None)
    exam["pipeline"].update({key: value for key, value in (original.get("pipeline") or {}).items()
                             if key in _COPY_KEYS})
    write_json(destination / "esame.json", exam)
    return destination.name


def set_archived(app, slug, archived):
    root = app.root(slug)
    with edit_lock(root):
        exam = read_json(root / "esame.json")
        if archived:
            worker = app.workers.get(slug)
            if worker and worker.is_alive():
                raise ValueError("La materia ha un lavoro locale attivo; attendi prima di archiviarla.")
            state = read_json(root / "state/pipeline.json", {})
            if any(not batch.get("processed") and batch.get("stage") not in {"superseded", "rejected"}
                   for batch in state.get("batches") or []):
                raise ValueError("La materia ha un invio in corso o incerto; riconcilialo prima di archiviarla.")
        exam["archived"] = bool(archived)
        if archived:
            exam["attivo"] = False
        write_json(root / "esame.json", exam)
    return exam["archived"]
