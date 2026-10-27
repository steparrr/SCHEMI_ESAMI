"""Recover existing OpenAI results and local PDFs without submitting new work."""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from common import read_json, write_json
from pipeline import Pipeline, TERMINAL, render_job, response_text, valid_article


def _save_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".rescue.tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _output_rows(path: Path):
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as stream:
        return {row["custom_id"]: row for line in stream if line.strip()
                for row in (json.loads(line),)}


def _local_results(root: Path, pipeline: Pipeline):
    """Restore missing HTML only from a saved, successful response."""
    restored = []
    for batch in pipeline.batches:
        path = batch.get("output_path")
        if not path and batch.get("batch_id"):
            candidate = Path("batch") / f"{batch['batch_id']}_output.jsonl"
            if (root / candidate).is_file():
                path = str(candidate)
        if not path:
            continue
        rows = _output_rows(root / path)
        for item in batch.get("requests", []):
            job = pipeline.jobs.get(item["hash"])
            if not job or job.get("kind") != "schema" or not pipeline.is_current(job):
                continue
            html = root / job["html"]
            if html.is_file():
                continue
            row = rows.get(item["cid"], {})
            response = row.get("response") or {}
            body = response.get("body") or {}
            if response.get("status_code") != 200 or body.get("status") == "incomplete":
                continue
            article = response_text(body).strip()
            if article.startswith("```"):
                import re
                article = re.sub(r"^```(?:html|json)?\s*", "", article, flags=re.I)
                article = re.sub(r"\s*```$", "", article)
            if not valid_article(article):
                continue
            _save_bytes(html, article.encode("utf-8"))
            if job["status"] != "PDF_OK":
                job["status"] = "HTML_OK"
                job.pop("error", None)
            restored.append(job["numero"])
    if restored:
        pipeline.save()
    return sorted(set(restored))


def _remote_updates(root: Path, state: dict, api):
    """Network requests run before taking the local pipeline lock."""
    updates, errors = {}, []
    for batch in state.get("batches", []):
        if batch.get("processed") or batch.get("stage") in {"prepared", "uploaded", "superseded", "rejected"}:
            continue
        run_id = batch.get("run_id")
        try:
            if batch.get("mode") == "urgent":
                if not batch.get("response_id"):
                    continue  # The Responses API cannot search an unknown response ID.
                remote = api.get_response(batch["response_id"])
            elif batch.get("batch_id"):
                remote = api.get_batch(batch["batch_id"])
            elif batch.get("input_file_id") and batch.get("stage") in {"creating", "uncertain"}:
                remote = api.find_batch(batch["input_file_id"], run_id, root.name)
                if remote is None:
                    updates[run_id] = {"not_found": True}
                    continue
            else:
                continue
            files = {}
            if batch.get("mode") != "urgent" and remote.get("status") in TERMINAL:
                for kind in ("output", "error"):
                    file_id = remote.get(kind + "_file_id")
                    if file_id:
                        target = root / "batch" / f"{remote['id']}_{kind}.jsonl"
                        if not target.is_file():
                            files[kind] = api.download(file_id)
            updates[run_id] = {"remote": remote, "files": files}
        except Exception as error:
            errors.append(f"{run_id}: {error}")
    return updates, errors


def search_and_rescue(root: Path, cfg: dict, project: Path, api=None, render=render_job):
    """Reconcile existing jobs, then render available HTML. Never POST to OpenAI."""
    root, project = Path(root), Path(project)
    before = read_json(root / "state/pipeline.json", {})
    before_ready = {int(n) for n, key in before.get("latest", {}).items()
                    if before["jobs"][key].get("status") == "PDF_OK"
                    and (root / before["jobs"][key].get("pdf", "")).is_file()}
    updates, errors = _remote_updates(root, before, api) if api else ({}, ["API non disponibile: controllo limitato ai file sul Mac."])
    lock_path = root / "state/pipeline.lock"
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BlockingIOError("Questa materia è già in lavorazione. Riprova fra poco.")
        pipe = Pipeline(root, cfg, project, api=api, render=render)
        pipe.state = read_json(pipe.path, pipe.state)
        pipe.jobs, pipe.latest, pipe.batches = pipe.state["jobs"], pipe.state["latest"], pipe.state["batches"]
        checked = 0
        for batch in pipe.batches:
            if batch.get("processed") or batch.get("run_id") not in updates:
                continue
            result = updates[batch["run_id"]]
            if result.get("not_found"):
                batch["stage"] = "uncertain"
                batch["error"] = "Nessun Batch corrispondente trovato sul server; nessun reinvio eseguito."
                continue
            remote = result["remote"]
            checked += 1
            batch["status"] = remote.get("status")
            batch["remote_checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            batch.pop("error", None)
            if batch.get("mode") == "urgent":
                batch["response_id"] = remote["id"]
                batch["request_counts"] = {"total": 1,
                    "completed": int(remote.get("status") == "completed"),
                    "failed": int(remote.get("status") in {"failed", "cancelled", "incomplete"})}
                if remote.get("status") in {"completed", "incomplete"}:
                    item = batch["requests"][0]
                    path = Path("batch") / f"{remote['id']}_output.jsonl"
                    row = {"custom_id": item["cid"], "response": {
                        "status_code": 200, "request_id": remote["id"], "body": remote}}
                    _save_bytes(root / path, (json.dumps(row, ensure_ascii=False) + "\n").encode())
                    batch["output_path"] = str(path)
                    pipe.consume(batch, remote)
                elif remote.get("status") in {"failed", "cancelled"}:
                    for item in batch["requests"]:
                        pipe.failed_job(pipe.jobs[item["hash"]], remote.get("error") or remote["status"])
                    batch.update(processed=True, stage="processed")
                continue
            batch["batch_id"] = remote["id"]
            batch["stage"] = "submitted"
            batch["request_counts"] = remote.get("request_counts")
            for item in batch.get("requests", []):
                if pipe.jobs[item["hash"]]["status"] == "SUBMISSION_UNCERTAIN":
                    pipe.jobs[item["hash"]]["status"] = "SUBMITTED"
            if remote.get("status") not in TERMINAL:
                continue
            for kind in ("output", "error"):
                file_id = remote.get(kind + "_file_id")
                if file_id:
                    path = Path("batch") / f"{remote['id']}_{kind}.jsonl"
                    if not (root / path).is_file():
                        if kind not in result["files"]:
                            errors.append(f"{remote['id']}: file {kind} non scaricato")
                            continue
                        _save_bytes(root / path, result["files"][kind])
                    batch[kind + "_path"] = str(path)
            if remote.get("output_file_id") and not batch.get("output_path"):
                continue
            write_json(root / "batch" / f"{remote['id']}_status.json", remote)
            pipe.consume(batch, remote)
        pipe.save()
        restored = _local_results(root, pipe)
        pipe.render_ready()
        pipe.save()
        pipe.report()
        rows = []
        for number, key in sorted(pipe.latest.items(), key=lambda pair: int(pair[0])):
            job = pipe.jobs[key]
            if job["status"] == "PDF_OK" and (root / job["pdf"]).is_file():
                if int(number) in before_ready:
                    continue
                detail = "PDF recuperato"
            elif job["status"] == "FONTI_MANCANTI":
                detail = "Nessuna fonte associata: serve aggiungere o collegare materiale."
            elif job["status"] == "SUBMISSION_UNCERTAIN":
                detail = "Invio OpenAI non confermato: nessun risultato locale."
            elif job["status"] == "SUBMITTED":
                detail = "Lavoro trovato su OpenAI; risultato non ancora pronto."
            elif job["status"] == "RENDER_FAILED":
                detail = "HTML presente, ma impaginazione PDF fallita: " + str(job.get("error", ""))[:250]
            elif job["status"] == "LOCAL_HTML_MISSING":
                detail = "HTML mancante sia localmente sia nei risultati salvati."
            else:
                detail = str(job.get("error") or job["status"])
            rows.append({"numero": int(number), "title": job["title"], "status": job["status"], "detail": detail})
        planning = []
        for key, job_hash in sorted(pipe.state.get("planning_latest", {}).items()):
            job = pipe.jobs.get(job_hash, {})
            if job.get("status") in {"FAILED_API", "REQUEST_TOO_LARGE", "SUBMISSION_UNCERTAIN"}:
                planning.append({"phase": key, "status": job["status"],
                                 "detail": str(job.get("error") or job["status"])})
        report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "checked_batches": checked, "restored_html": restored,
                  "recovered_pdfs": [row["numero"] for row in rows if row["detail"] == "PDF recuperato"],
                  "rows": rows, "planning": planning, "errors": errors}
        write_json(root / "output/reports/search-and-rescue.json", report)
        return report
