from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

import batch_builder as builder
from common import PROJECT, digest, prompt_text, read_json, write_json
from ingest import ingest
from packs import build_packs, scheme_list
from remote import HTTPError
from exports import export_all, publish
from model_settings import apply_profile
from planner import check_analysis, check_plan, prepare_auto
from telemetry import Telemetry

TERMINAL = {"completed", "failed", "expired", "cancelled"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def response_text(body):
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    parts = []
    for item in body.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "\n".join(parts)


def valid_article(text):
    if "</article>" not in text.lower():
        return False
    soup = BeautifulSoup(text, "html.parser")
    return soup.find("article", class_="study-schema") is not None


def render_signature(project=PROJECT):
    files = [project / "renderer" / "render_pdf.py"]
    files += [project / "scripts" / x for x in ("finalize_pdf.py", "render_mermaid.py", "resolve_images.py", "typography.py", "qc_pdf.py")]
    files.append(project / "config" / "mermaid.json")
    return digest({str(p.relative_to(project)): digest(p) for p in files})


def render_job(root, job, cfg, project=PROJECT):
    from finalize_pdf import finalize
    from qc_pdf import inspect_pdf
    pack = read_json(root / job["pack"])
    destination = root / job["pdf"]
    source = root / job["html"]
    renderer_path = None
    if cfg.get("safe_table_layout"):
        source = source.with_name("schema.safe.html")
        source.write_text(linearize_tables((root / job["html"]).read_text(encoding="utf-8")), encoding="utf-8")
        renderer_path = project / "scripts" / "safe_renderer.py"
    finalize(source, destination, f"{job['numero']:02d} — {job['title']}", root, pack, root / job["manifest"], project, renderer_path)
    result = inspect_pdf(destination)
    if not result.get("ok"):
        raise ValueError("QC PDF fallito: " + json.dumps(result, ensure_ascii=False))
    return result


def linearize_tables(article_html):
    """Avoid Story's repeated decoration across pages, preserving every cell."""
    soup = BeautifulSoup(article_html, "html.parser")
    for table in list(soup.find_all("table")):
        rows = table.find_all("tr")
        if not rows:
            table.decompose()
            continue
        header = rows[0].find_all("th")
        headings = [cell.get_text(" ", strip=True) for cell in header]
        data_rows = rows[1:] if headings else rows
        wrapper = soup.new_tag("div", attrs={"class": "safe-table"})
        for row in data_rows:
            cells = row.find_all(["th", "td"], recursive=False)
            if not cells:
                continue
            card = soup.new_tag("div", attrs={"class": "safe-table-row",
                                             "style": "border-left:2px solid #9eb9cf;padding:1.5mm 3mm;margin:2mm 0;break-inside:avoid-page;"})
            for index, cell in enumerate(cells):
                paragraph = soup.new_tag("p", style="margin:0 0 1mm;line-height:1.25;")
                if index < len(headings) and headings[index]:
                    label = soup.new_tag("strong")
                    label.string = headings[index] + ": "
                    paragraph.append(label)
                for child in list(cell.contents):
                    paragraph.append(child.extract())
                card.append(paragraph)
            wrapper.append(card)
        if not data_rows and headings:
            paragraph = soup.new_tag("p")
            paragraph.string = " · ".join(headings)
            wrapper.append(paragraph)
        table.replace_with(wrapper)
    return str(soup)


class Pipeline:
    def __init__(self, root: Path, cfg: dict, project: Path = PROJECT, api=None, render=render_job):
        self.root, self.cfg, self.project, self.api, self.render = root, cfg, project, api, render
        self.path = root / "state" / "pipeline.json"
        self.state = read_json(self.path, {"version": 1, "jobs": {}, "latest": {}, "batches": []})
        if self.state.get("version") != 1:
            raise ValueError("Versione di stato non supportata.")
        self.jobs = self.state["jobs"]
        self.latest = self.state["latest"]
        self.batches = self.state["batches"]
        self.log_path = root / "logs" / "pipeline.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.telemetry = Telemetry(root)

    def save(self):
        write_json(self.path, self.state)

    def log(self, text):
        line = f"[{now()}] [{self.root.name}] {text}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def prepare(self):
        schemes = scheme_list(self.root)
        ingest(self.root, self.log)
        if not schemes:
            schemes = prepare_auto(self)
        else:
            self.state["planning_latest"] = {}
        if not schemes:
            self.latest.clear()
            self.save()
            return
        packs = build_packs(self.root, self.cfg, schemes)
        prompt = prompt_text(self.root, self.cfg, self.project) + "\n" + self.cfg.get("schema_extra_instructions", "")
        schema_effort = self.cfg.get("schema_reasoning_effort", self.cfg["reasoning_effort"])
        schema_output = self.cfg.get("schema_max_output_tokens", self.cfg["max_output_tokens"])
        api_settings = {"model": self.cfg["model"], "reasoning_effort": schema_effort,
                        "max_output_tokens": schema_output, "image_mode": self.cfg["image_mode"],
                        "image_detail": self.cfg["image_detail"]}
        new_latest = {}
        for pack in sorted(packs, key=lambda item: item["scheme"]["numero"]):
            number = pack["scheme"]["numero"]
            base_hash = digest({"pack": pack, "prompt": prompt, "api": api_settings})
            job_prompt = prompt
            request_hash = base_hash
            if number == 1 and self.cfg.get("review_first_schema"):
                review = self.state.get("pilot_review") or {}
                if review.get("base_hash") != base_hash:
                    review = {"base_hash": base_hash, "round": 0, "feedback": "", "approved_hash": None,
                              "created_at": now(), "updated_at": now()}
                    self.state["pilot_review"] = review
                if review.get("round", 0):
                    job_prompt += ("\n\nCORREZIONI RICHIESTE PER LO SCHEMA PILOTA (sostituisci la versione precedente):\n"
                                   + review["feedback"] + "\nMantieni la copertura delle fonti e il limite di pagine previsto.")
                    request_hash = digest({"base": base_hash, "round": review["round"], "feedback": review["feedback"]})
            elif number > 1 and self.cfg.get("review_first_schema"):
                review = self.state.get("pilot_review") or {}
                if review.get("approved_hash") == new_latest.get("1") and review.get("feedback"):
                    job_prompt += ("\n\nSTILE E CORREZIONI APPROVATI NEL PRIMO SCHEMA: applica anche a questo schema, "
                                   "adattando ai suoi contenuti:\n" + review["feedback"])
                    request_hash = digest({"pack": pack, "prompt": job_prompt, "api": api_settings})
            new_latest[str(number)] = request_hash
            if request_hash in self.jobs:
                if self.jobs[request_hash]["status"] == "SUPERSEDED":
                    self.jobs[request_hash]["status"] = "READY"
                continue
            folder = Path("output") / "raw" / f"schema_{number:02d}" / request_hash
            (self.root / folder).mkdir(parents=True, exist_ok=True)
            job = {"hash": request_hash, "kind": "schema", "numero": number, "title": pack["scheme"]["titolo"], "status": "READY" if pack["chunks"] or pack.get("external_supplement") else "FONTI_MANCANTI", "page_budget": pack.get("page_budget"), "page_min": pack.get("page_min"), "retry_count": 0, "created_at": now(), "pack": str(folder / "pack.json"), "manifest": str(folder / "images.jsonl"), "request": str(folder / "request.json"), "html": str(folder / "schema.raw.html"), "pdf": str(Path("output") / "pdf" / "versioni" / f"schema_{number:02d}_{request_hash}.pdf")}
            write_json(self.root / job["pack"], pack)
            (self.root / job["manifest"]).write_text("".join(json.dumps(picture, ensure_ascii=False) + "\n" for picture in pack["images"]), encoding="utf-8")
            visual_pack = copy.deepcopy(pack)
            for picture in visual_pack["images"]:
                picture["resolved_path"] = str(self.root / picture["path"])
            body = builder.build_body(visual_pack, self.cfg["model"], schema_effort, schema_output, self.cfg["image_mode"], self.cfg["image_detail"], instructions=job_prompt)
            apply_profile(body, self.cfg)
            body["reasoning"] = {"effort": schema_effort}
            body["metadata"].update({"exam_id": self.root.name, "request_hash": request_hash})
            write_json(self.root / job["request"], body)
            self.jobs[request_hash] = job
        self.latest.clear()
        self.latest.update(new_latest)
        for job in self.jobs.values():
            if job["status"] in {"READY", "RETRY_READY"} and not self.is_current(job) and not job.get("assigned"):
                job["status"] = "SUPERSEDED"
        self.state["input_status"] = "OK"
        self.save()

    def is_current(self, job):
        if job.get("kind") in {"analysis", "plan"}:
            return self.state.get("planning_latest", {}).get(job["key"]) == job["hash"]
        return self.latest.get(str(job["numero"])) == job["hash"]

    def logical_key(self, job):
        return job.get("key", "schema:" + str(job["numero"]))

    def pilot_review(self):
        if not self.cfg.get("review_first_schema") or "1" not in self.latest:
            return None
        review = self.state.get("pilot_review") or {}
        pilot = self.jobs[self.latest["1"]]
        approved = review.get("approved_hash") == pilot["hash"] and pilot["status"] == "PDF_OK"
        return {"status": "approved" if approved else "ready_for_review" if pilot["status"] == "PDF_OK" else "generating",
                "number": 1, "hash": pilot["hash"], "job_status": pilot["status"],
                "round": review.get("round", 0), "approved_at": review.get("approved_at"),
                "pdf": pilot.get("public_pdf") if pilot["status"] == "PDF_OK" else None}

    def queue(self):
        review = self.pilot_review()
        busy = {self.logical_key(self.jobs[item["hash"]]) for batch in self.batches if not batch.get("processed") for item in batch["requests"]}
        candidates = [job for job in self.jobs.values() if self.is_current(job) and job["status"] in {"READY", "RETRY_READY"} and not job.get("assigned") and self.logical_key(job) not in busy]
        if review and review["status"] != "approved":
            candidates = [job for job in candidates if job.get("kind") in {"analysis", "plan"} or job.get("numero") == 1]
        groups, current, size, model, current_mode = [], [], 0, None, None
        for job in sorted(candidates, key=lambda j: j["numero"]):
            mode = ("urgent" if review and review["status"] != "approved" and job.get("kind") == "schema" and job.get("numero") == 1
                    else self.cfg.get("execution_mode", "batch"))
            body = read_json(self.root / job["request"])
            row_size = len(json.dumps(body, ensure_ascii=False).encode()) + 512
            if row_size > self.cfg["max_batch_bytes"]:
                job.update(status="REQUEST_TOO_LARGE", error="Request oltre max_batch_bytes; riduci immagini o source pack.")
                continue
            if current and (mode == "urgent" or mode != current_mode or len(current) >= self.cfg["micro_batch_size"] or size + row_size > self.cfg["max_batch_bytes"] or body["model"] != model):
                groups.append((current_mode, current))
                current, size = [], 0
            current.append(job)
            size += row_size
            model = body["model"]
            current_mode = mode
        if current:
            groups.append((current_mode, current))
        for mode, group in groups:
            run_id = uuid.uuid4().hex
            path = Path("batch") / f"{run_id}.jsonl"
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            items, rows = [], []
            for job in group:
                cid = f"{self.root.name[:24]}-s{job['numero']:04d}-{job['hash'][:12]}-r{job['retry_count']}"
                rows.append({"custom_id": cid, "method": "POST", "url": "/v1/responses", "body": read_json(self.root / job["request"])})
                items.append({"cid": cid, "hash": job["hash"], "attempt": job["retry_count"]})
                job["assigned"] = run_id
            temporary = (self.root / path).with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.root / path)
            self.batches.append({"run_id": run_id, "input_path": str(path), "request_path": group[0]["request"] if mode == "urgent" else None, "mode": mode, "stage": "prepared", "requests": items, "created_at": now()})
            self.save()
            self.telemetry.record("batch_prepared", "batch_prepare", metrics={"requests": len(items)})
            self.log(f"{'RICHIESTA URGENTE' if mode == 'urgent' else 'MICRO-BATCH'} PRONTA: {len(items)} richieste, run {run_id}")
        self.save()

    def failed_job(self, job, message, retryable=False):
        job["error"] = message
        if retryable and job["retry_count"] < self.cfg["retry_limit"] and self.is_current(job):
            job["retry_count"] += 1
            body = read_json(self.root / job["request"])
            body["max_output_tokens"] = (self.cfg["retry_max_output_tokens"] if job.get("kind") in {"analysis", "plan"}
                                         else self.cfg.get("schema_retry_max_output_tokens", self.cfg["retry_max_output_tokens"]))
            if job.get("kind") in {"analysis", "plan"}:
                body["instructions"] += "\nCorreggi la precedente risposta non valida: " + str(message)[:2200]
            path = Path(job["request"]).with_name(f"request-r{job['retry_count']}.json")
            write_json(self.root / path, body)
            job["request"] = str(path)
            job["status"] = "RETRY_READY"
            job.pop("assigned", None)
        else:
            job["status"] = "FAILED_API"

    def consume(self, batch, remote):
        rows = {}
        for kind in ("output", "error"):
            path = batch.get(kind + "_path")
            if path:
                with (self.root / path).open(encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            row = json.loads(line)
                            rows[row["custom_id"]] = row
        for item in batch["requests"]:
            job = self.jobs[item["hash"]]
            row = rows.get(item["cid"])
            if row is None:
                status = remote.get("status")
                self.failed_job(job, "Nessun risultato per la request; batch " + str(status), status == "expired")
                continue
            response = row.get("response") or {}
            error = row.get("error") or {}
            code = int(response.get("status_code") or 0)
            if code != 200:
                transient = code in {408, 429, 500, 502, 503, 504} or error.get("code") in {"batch_expired", "request_timeout", "server_error"}
                self.failed_job(job, {"status_code": code, "error": error, "response": response}, transient)
                continue
            body = response.get("body") or {}
            job["usage"] = body.get("usage")
            job["request_id"] = response.get("request_id")
            write_json((self.root / job["pack"]).with_name(f"response-r{item['attempt']}.json"), row)
            text = response_text(body).strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:html|json)?\s*", "", text, flags=re.I)
                text = re.sub(r"\s*```$", "", text)
            incomplete = body.get("status") == "incomplete"
            if job.get("kind") in {"analysis", "plan"}:
                try:
                    if incomplete:
                        raise ValueError((body.get("incomplete_details") or {}).get("reason", "json_incomplete"))
                    snapshot = read_json(self.root / job["pack"])
                    value = json.loads(text)
                    validator = check_analysis if job["kind"] == "analysis" else check_plan
                    validator(value, snapshot)
                    write_json(self.root / job["result"], value)
                    job.update(status="DATA_OK", api_completed_at=now())
                    job.pop("error", None)
                except (ValueError, KeyError, TypeError) as error:
                    self.failed_job(job, "Pianificazione non valida: " + str(error), True)
                continue
            if incomplete or not valid_article(text):
                reason = (body.get("incomplete_details") or {}).get("reason", "html_incomplete")
                self.failed_job(job, reason, reason in {"max_output_tokens", "html_incomplete"})
                continue
            (self.root / job["html"]).write_text(text, encoding="utf-8")
            job.update({"status": "HTML_OK", "api_completed_at": now()})
            job.pop("error", None)
        batch["processed"] = True
        batch["stage"] = "processed"
        batch["status"] = remote.get("status")
        batch["request_counts"] = remote.get("request_counts")
        batch["usage"] = remote.get("usage")
        batch["remote_errors"] = remote.get("errors")
        self.save()

    def advance_urgent(self, batch):
        """Use a durable background Response for each urgent request."""
        if batch["stage"] == "prepared":
            body = read_json(self.root / batch["request_path"])
            batch["stage"] = "creating"
            self.save()  # A lost POST reply must never cause an automatic duplicate.
            try:
                response = self.api.create_urgent_response(body, batch["run_id"], self.root.name)
            except HTTPError as error:
                if error.status in {400, 401, 403, 404, 422}:
                    for item in batch["requests"]:
                        self.failed_job(self.jobs[item["hash"]], str(error))
                    batch.update(stage="rejected", processed=True, error=str(error))
                    self.save()
                    return
                raise
            batch.update(response_id=response["id"], stage="submitted", status=response.get("status"),
                         submitted_at=now(), remote_checked_at=now())
            for item in batch["requests"]:
                self.jobs[item["hash"]]["status"] = "SUBMITTED"
            self.save()
            self.telemetry.record("remote_accepted", "remote", locality="remote_observed",
                                  metrics={"requests": 1})
            self.log("RISPOSTA URGENTE INVIATA: " + response["id"])
        elif batch["stage"] in {"creating", "uncertain"} and not batch.get("response_id"):
            batch.update(stage="uncertain", error="Invio urgente ambiguo: nessun ID ricevuto. Nessun reinvio automatico.")
            for item in batch["requests"]:
                self.jobs[item["hash"]]["status"] = "SUBMISSION_UNCERTAIN"
            self.save()
            return
        response = self.api.get_response(batch["response_id"])
        batch["status"] = response.get("status")
        batch["remote_checked_at"] = now()
        batch["request_counts"] = {"total": 1, "completed": int(response.get("status") == "completed"),
                                   "failed": int(response.get("status") in {"failed", "cancelled", "incomplete"})}
        batch.pop("error", None)
        self.save()
        self.telemetry.record("remote_polled", "remote", locality="remote_observed",
                              metrics={"requests": 1, "completed": batch["request_counts"]["completed"]})
        if response.get("status") in {"queued", "in_progress"}:
            return
        if response.get("status") in {"completed", "incomplete"}:
            item = batch["requests"][0]
            path = Path("batch") / f"{batch['response_id']}_output.jsonl"
            row = {"custom_id": item["cid"], "response": {"status_code": 200, "request_id": batch["response_id"], "body": response}}
            temporary = (self.root / path).with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.root / path)
            batch["output_path"] = str(path)
            self.consume(batch, response)
        else:
            for item in batch["requests"]:
                self.failed_job(self.jobs[item["hash"]], response.get("error") or response.get("status"), False)
            batch.update(processed=True, stage="processed")
        self.save()

    def advance_remote(self):
        if self.api is None:
            raise ValueError("Client API assente.")
        for batch in self.batches:
            if batch.get("processed"):
                continue
            try:
                if batch.get("mode") == "urgent":
                    self.advance_urgent(batch)
                    continue
                if batch["stage"] in {"prepared", "uploaded"}:
                    active = sum(not other.get("processed") and other["stage"] in {"submitted", "creating", "uncertain"} for other in self.batches)
                    if active >= self.cfg.get("max_active_batches", 2):
                        continue
                if batch["stage"] == "prepared":
                    batch["local_step"] = "uploading"
                    self.save()
                    file = self.api.upload(self.root / batch["input_path"])
                    batch["input_file_id"] = file["id"]
                    batch["stage"] = "uploaded"
                    batch["local_step"] = "uploaded"
                    self.save()
                if batch["stage"] == "uploaded":
                    # Registrare prima del POST consente di riconciliare un crash.
                    batch["stage"] = "creating"
                    self.save()
                    try:
                        remote = self.api.create_batch(batch["input_file_id"], batch["run_id"], self.root.name)
                    except HTTPError as error:
                        if 400 <= error.status < 500:
                            for item in batch["requests"]:
                                self.failed_job(self.jobs[item["hash"]], str(error))
                            batch.update(stage="rejected", processed=True, error=str(error))
                            self.save()
                            continue
                        raise
                    batch.update(batch_id=remote["id"], stage="submitted", status=remote.get("status"), submitted_at=now(), remote_checked_at=now(), request_counts=remote.get("request_counts"))
                    batch.pop("error", None)
                    for item in batch["requests"]:
                        self.jobs[item["hash"]]["status"] = "SUBMITTED"
                    self.save()
                    self.telemetry.record("remote_accepted", "remote", locality="remote_observed",
                                          metrics={"requests": len(batch["requests"])})
                    self.log("BATCH INVIATO: " + remote["id"])
                elif batch["stage"] in {"creating", "uncertain"}:
                    remote = self.api.find_batch(batch["input_file_id"], batch["run_id"], self.root.name)
                    if remote is None:
                        batch["stage"] = "uncertain"
                        batch["error"] = "Invio ambiguo: nessun batch trovato. Nessun reinvio automatico."
                        for item in batch["requests"]:
                            self.jobs[item["hash"]]["status"] = "SUBMISSION_UNCERTAIN"
                        self.save()
                        continue
                    batch.update(batch_id=remote["id"], stage="submitted", status=remote.get("status"), remote_checked_at=now(), request_counts=remote.get("request_counts"))
                    batch.pop("error", None)
                    self.save()
                remote = self.api.get_batch(batch["batch_id"])
                batch["status"] = remote.get("status")
                batch["request_counts"] = remote.get("request_counts")
                batch["remote_checked_at"] = now()
                batch.pop("error", None)
                self.save()
                counts = batch.get("request_counts") or {}
                self.telemetry.record("remote_polled", "remote", locality="remote_observed",
                                      metrics={"requests": counts.get("total", 0),
                                               "completed": counts.get("completed", 0),
                                               "failed": counts.get("failed", 0)})
                if remote.get("status") not in TERMINAL:
                    continue
                for kind in ("output", "error"):
                    file_id = remote.get(kind + "_file_id")
                    if file_id:
                        batch["local_step"] = "downloading_results"
                        self.save()
                        path = Path("batch") / f"{batch['batch_id']}_{kind}.jsonl"
                        if not (self.root / path).exists():
                            data = self.api.download(file_id)
                            temporary = (self.root / path).with_suffix(".tmp")
                            with temporary.open("wb") as stream:
                                stream.write(data)
                                stream.flush()
                                os.fsync(stream.fileno())
                            temporary.replace(self.root / path)
                        batch[kind + "_path"] = str(path)
                write_json(self.root / "batch" / (batch["batch_id"] + "_status.json"), remote)
                self.consume(batch, remote)
                batch["local_step"] = "results_recorded"
                self.save()
            except Exception as error:
                batch["error"] = str(error)
                batch["error_at"] = now()
                if batch["stage"] == "creating":
                    batch["stage"] = "uncertain"
                self.save()
                self.log("RECUPERO NECESSARIO: " + str(error))
                # Un problema di trasporto nel primo Batch preparato non deve
                # far partire quelli successivi mentre questo resta in attesa.
                if batch["stage"] in {"prepared", "uploaded"}:
                    break

    def render_ready(self):
        signature = render_signature(self.project)
        for job in self.jobs.values():
            if job.get("kind") in {"analysis", "plan"}:
                continue
            exists = (self.root / job["html"]).exists()
            needs = job["status"] in {"HTML_OK", "RENDERING", "RENDER_FAILED"} or (job["status"] == "PDF_OK" and (job.get("render_hash") != signature or not (self.root / job["pdf"]).exists()))
            if not needs:
                if job["status"] == "PDF_OK" and self.latest.get(str(job["numero"])) == job["hash"] and (self.root / job["pdf"]).exists():
                    publish(self.root, job)
                continue
            if not exists:
                job.update(status="LOCAL_HTML_MISSING", error="HTML locale mancante: ripristina il file dalla copia conservata.")
                continue
            try:
                (self.root / job["pdf"]).parent.mkdir(parents=True, exist_ok=True)
                job.update(status="RENDERING", render_started_at=now())
                self.save()
                render_metrics = {"renderer_signature": signature}
                with self.telemetry.span("pdf_render", request_hash=job["hash"], metrics=render_metrics):
                    qc = self.render(self.root, job, self.cfg, self.project)
                    render_metrics["pages"] = qc["pages"]
                if job.get("page_budget") and qc["pages"] > job["page_budget"]:
                    self.shorten(job, qc["pages"])
                    self.save()
                    continue
                if job.get("page_min") and qc["pages"] < job["page_min"]:
                    self.lengthen(job, qc["pages"])
                    self.save()
                    continue
                job.update(status="PDF_OK", qc=qc, render_hash=signature, completed_at=now())
                job.pop("error", None)
                if self.latest.get(str(job["numero"])) == job["hash"]:
                    publish(self.root, job)
                self.log(f"PDF OK: schema {job['numero']:02d} — {job['title']}")
            except Exception as error:
                job.update(status="RENDER_FAILED", error=str(error))
                self.log(f"ERRORE PDF LOCALE: schema {job['numero']:02d}: {error}")
            self.save()

    def shorten(self, job, actual_pages):
        message = f"Impaginazione: {actual_pages} pagine, massimo {job['page_budget']}."
        if job["retry_count"] >= self.cfg["retry_limit"] or not self.is_current(job):
            job.update(status="LIMITE_PAGINE", error=message + " Aumenta il limite o usa Completa; nessuna pagina è stata tagliata.")
            return
        job["retry_count"] += 1
        body = read_json(self.root / job["request"])
        words = len(BeautifulSoup((self.root / job["html"]).read_text(encoding="utf-8"), "html.parser").get_text(" ", strip=True).split())
        target_pages = max(job.get("page_min") or 1, job["page_budget"] - 1)
        target_words = max(80, round(words * target_pages / actual_pages))
        body["instructions"] += ("\nLa precedente stesura aveva troppe pagine. RISCRIVI in modo più sintetico: " + message +
            f" La stesura precedente aveva circa {words} parole in {actual_pages} pagine. " +
            f"Mira a circa {target_words} parole e {target_pages} pagine con la stessa impaginazione; " +
            "Conserva le distinzioni e i criteri essenziali, senza tagliare frasi, inventare dati o ridurre i caratteri. " +
            "Evita immagini o diagrammi non indispensabili e ripetizioni. Restituisci sempre un article completo.")
        body["max_output_tokens"] = self.cfg.get("schema_retry_max_output_tokens", self.cfg["retry_max_output_tokens"])
        path = Path(job["request"]).with_name(f"request-r{job['retry_count']}.json")
        write_json(self.root / path, body)
        job.update(request=str(path), status="RETRY_READY", error=message)
        job.pop("assigned", None)
        self.log("RISCRITTURA PER LIMITE PAGINE: " + message)

    def lengthen(self, job, actual_pages):
        message = f"Impaginazione: {actual_pages} pagine, minimo {job['page_min']}."
        if job["retry_count"] >= self.cfg["retry_limit"] or not self.is_current(job):
            job.update(status="LIMITE_PAGINE", error=message + " Fonti insufficienti o generazione troppo breve; il PDF non viene dichiarato completo.")
            return
        job["retry_count"] += 1
        body = read_json(self.root / job["request"])
        words = len(BeautifulSoup((self.root / job["html"]).read_text(encoding="utf-8"), "html.parser").get_text(" ", strip=True).split())
        target_pages = min(job["page_budget"], job["page_min"] + 1)
        target_words = max(80, round(words * target_pages / actual_pages))
        body["instructions"] += ("\nLa precedente stesura era troppo breve: " + message +
            f" La stesura precedente aveva circa {words} parole in {actual_pages} pagine. " +
            f"Mira a circa {target_words} parole e {target_pages} pagine con la stessa impaginazione. " +
            " RISCRIVI ampliando soltanto i contenuti verificabili nel SOURCE PACK: definizioni, criteri, eccezioni, "
            "diagnosi differenziali, percorsi e risposte alle domande. Conserva una buona leggibilità. "
            "Non ripetere frasi e non inventare contenuti per raggiungere il minimo. Restituisci un article completo.")
        body["max_output_tokens"] = self.cfg.get("schema_retry_max_output_tokens", self.cfg["retry_max_output_tokens"])
        path = Path(job["request"]).with_name(f"request-r{job['retry_count']}.json")
        write_json(self.root / path, body)
        job.update(request=str(path), status="RETRY_READY", error=message)
        job.pop("assigned", None)
        self.log("RISCRITTURA PER SCHEMA BREVE: " + message)

    def report(self, export=True):
        rows = []
        for number, key in sorted(self.latest.items(), key=lambda x: int(x[0])):
            job = self.jobs[key]
            rows.append({k: job.get(k) for k in ("numero", "title", "status", "pdf", "public_pdf", "page_budget", "page_min", "qc", "error", "retry_count", "usage", "request_id")})
        planning = [{"kind": self.jobs[key].get("kind"), "status": self.jobs[key]["status"], "error": self.jobs[key].get("error")} for key in self.state.get("planning_latest", {}).values()]
        exports = export_all(self.root, self.cfg, self.jobs, self.latest) if export else {}
        if not export:
            write_json(self.root / "output/reports/exports.json", {})
        report = {"exam": self.cfg["exam_name"], "generated_at": now(), "revision": read_json(self.root / "esame.json").get("revision", 0), "input_status": self.state.get("input_status"), "rows": rows, "planning": planning, "exports": exports, "batches": self.batches, "pilot_review": self.pilot_review()}
        write_json(self.root / "output" / "reports" / "pipeline_report.json", report)
        lines = [f"# {self.cfg['exam_name']} — report", "", f"Generato: {report['generated_at']}", "", f"Input: {report['input_status']}", ""]
        for row in rows:
            lines.append(f"- Schema {row['numero']:02d} — {row['title']}: **{row['status']}**")
            if row["error"]:
                lines.append("  " + str(row["error"]))
        lines += ["", f"PDF pronti: {sum(x['status'] == 'PDF_OK' for x in rows)}/{len(rows)}", ""]
        (self.root / "output" / "reports" / "pipeline_report.md").write_text("\n".join(lines), encoding="utf-8")
        return report

    def tick(self, submit=False):
        lock = self.root / "state" / "pipeline.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.log("Pipeline già attiva per questo esame.")
                return False
            # Ricaricare lo stato solo dopo avere preso il lock.
            self.state = read_json(self.path, self.state)
            self.jobs, self.latest, self.batches = self.state["jobs"], self.state["latest"], self.state["batches"]
            try:
                self.prepare()
                # Un dry run precedente può essere diventato obsoleto prima dell'invio.
                for batch in self.batches:
                    if batch["stage"] in {"prepared", "uploaded"} and any(not self.is_current(self.jobs[x["hash"]]) for x in batch["requests"]):
                        batch.update(stage="superseded", processed=True)
                        for item in batch["requests"]:
                            self.jobs[item["hash"]].pop("assigned", None)
                self.save()
                self.render_ready()
                if submit:
                    self.advance_remote()
                    self.render_ready()
                self.queue()
                if submit:
                    self.advance_remote()
                    self.render_ready()
                    # Analisi, piano e generazione possono avanzare nello stesso
                    # avvio se il servizio ha già completato la fase precedente.
                    for _ in range(3):
                        planning_jobs = [self.jobs[key] for key in self.state.get("planning_latest", {}).values()]
                        if not planning_jobs or any(job["status"] != "DATA_OK" for job in planning_jobs):
                            break
                        before = digest({"latest": self.latest, "planning": self.state.get("planning_latest"), "states": [(key, job["status"]) for key, job in self.jobs.items()]})
                        self.prepare()
                        self.queue()
                        self.advance_remote()
                        self.render_ready()
                        after = digest({"latest": self.latest, "planning": self.state.get("planning_latest"), "states": [(key, job["status"]) for key, job in self.jobs.items()]})
                        if before == after:
                            break
                self.save()
                self.report()
                review = self.pilot_review()
                return (any(not b.get("processed") and b["stage"] != "uncertain" for b in self.batches)
                        or any(job["status"] in {"READY", "RETRY_READY"} and self.is_current(job)
                               and (not review or review["status"] == "approved" or job.get("kind") in {"analysis", "plan"} or job.get("numero") == 1)
                               for job in self.jobs.values()))
            except Exception as error:
                self.state["input_status"] = "ERRORE: " + str(error)
                self.save()
                self.report(export=False)
                raise
