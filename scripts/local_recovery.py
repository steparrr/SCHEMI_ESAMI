"""Explicit L1 recovery for current local artifacts; cannot submit remote work."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from common import PROJECT, digest, load_config, pdf_filename
from exports import publish
from pipeline import Pipeline, render_job, render_signature
from qc_pdf import inspect_pdf
from telemetry import Telemetry


def recover_local(root: Path, project: Path = PROJECT, render=render_job, signature=None):
    root, project = Path(root), Path(project)
    cfg = load_config(root, project)
    actions = []
    lock = root / "state/pipeline.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BlockingIOError("La materia è già in lavorazione; il recupero locale non parte.")
        pipe = Pipeline(root, cfg, project, api=None, render=render)
        current_signature = signature or render_signature(project)
        audit = root / "state/local_recovery.jsonl"
        attempts = {}
        if audit.is_file():
            for line in audit.read_text(encoding="utf-8").splitlines():
                item = json.loads(line)
                if item.get("action") == "rerender_local" and item.get("outcome") not in {"attempt_limit", "html_missing", "html_invalid"}:
                    attempts[item.get("request_hash")] = attempts.get(item.get("request_hash"), 0) + 1
        for number, key in sorted(pipe.latest.items(), key=lambda pair: int(pair[0])):
            job = pipe.jobs.get(key)
            if not job or job.get("kind") != "schema" or job.get("hash") != key:
                continue
            if job.get("status") == "PDF_OK":
                version = (root / (job.get("pdf") or "__missing__")).resolve()
                versions_folder = (root / "output/pdf/versioni").resolve()
                public_relative = Path("output/pdf") / pdf_filename(job["numero"], job["title"])
                public = root / public_relative
                safe_version = version.is_relative_to(versions_folder) and version.suffix.lower() == ".pdf" and version.is_file()
                if public.is_symlink() or (public.exists() and not public.is_file()):
                    actions.append({"schema": int(number), "action": "publish_existing_pdf", "outcome": "precondition_failed"})
                    continue
                public_current = (job.get("public_pdf") == str(public_relative) and public.is_file()
                                  and safe_version and digest(public) == digest(version))
                if not public_current:
                    qc = inspect_pdf(version) if safe_version and job.get("render_hash") == current_signature else {"ok": False}
                    pages = qc.get("pages")
                    valid_pages = (type(pages) is int and pages >= 1 and
                                   (not job.get("page_budget") or pages <= job["page_budget"]) and
                                   (not job.get("page_min") or pages >= job["page_min"]))
                    if qc.get("ok") and valid_pages:
                        backup = None
                        if public.is_file():
                            previous_hash = digest(public)
                            if previous_hash != digest(version):
                                archived = root / "state/local_recovery_backups" / f"schema_{int(number):02d}_{previous_hash[:24]}.pdf"
                                archived.parent.mkdir(parents=True, exist_ok=True)
                                if not archived.is_file():
                                    temporary = archived.with_suffix(".tmp.pdf")
                                    try:
                                        shutil.copy2(public, temporary)
                                        os.replace(temporary, archived)
                                    finally:
                                        temporary.unlink(missing_ok=True)
                                backup = str(archived.relative_to(root))
                        publish(root, job)
                        pipe.save()
                        actions.append({"schema": int(number), "action": "publish_existing_pdf", "outcome": "success",
                                        "backup": backup})
                    else:
                        actions.append({"schema": int(number), "action": "publish_existing_pdf", "outcome": "precondition_failed"})
                continue
            if job.get("status") != "RENDER_FAILED":
                continue
            if attempts.get(key, 0) >= 2:
                actions.append({"schema": int(number), "action": "rerender_local", "outcome": "attempt_limit"})
                continue
            source = root / job.get("html", "__missing__")
            if not source.is_file():
                actions.append({"schema": int(number), "action": "rerender_local", "outcome": "html_missing"})
                continue
            from pipeline import valid_article
            if not valid_article(source.read_text(encoding="utf-8")):
                actions.append({"schema": int(number), "action": "rerender_local", "outcome": "html_invalid"})
                continue
            original = root / job["pdf"]
            temporary = original.with_name(original.stem + ".recovery.pdf")
            candidate = dict(job, pdf=str(temporary.relative_to(root)))
            try:
                with Telemetry(root).span("local_recovery", request_hash=key):
                    qc = render(root, candidate, cfg, project)
                if not qc.get("ok") or (job.get("page_budget") and qc["pages"] > job["page_budget"]) or (job.get("page_min") and qc["pages"] < job["page_min"]):
                    actions.append({"schema": int(number), "action": "rerender_local", "outcome": "qc_or_page_limit_failed"})
                    continue
                if not temporary.is_file() or not inspect_pdf(temporary)["ok"]:
                    actions.append({"schema": int(number), "action": "rerender_local", "outcome": "pdf_invalid"})
                    continue
                temporary.replace(original)
                job.update(status="PDF_OK", qc=qc, render_hash=current_signature)
                job.pop("error", None)
                publish(root, job)
                pipe.save()
                actions.append({"schema": int(number), "action": "rerender_local", "outcome": "success"})
            except Exception:
                actions.append({"schema": int(number), "action": "rerender_local", "outcome": "failed"})
            finally:
                temporary.unlink(missing_ok=True)
        if actions:
            with audit.open("a", encoding="utf-8") as stream:
                for action in actions:
                    entry = {**action, "request_hash": pipe.latest.get(str(action["schema"])),
                             "at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                    stream.write(json.dumps(entry, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        # This function never calls tick, prepare, advance_remote or an API client.
    return actions
