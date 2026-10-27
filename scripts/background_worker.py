"""Persistent local scheduler. Server jobs survive without this process; local steps resume at login."""
from __future__ import annotations

import fcntl
from contextlib import closing
import os
import shutil
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from common import PROJECT, read_json, write_json
from local_app import App


def backup_if_due(project: Path, current: datetime):
    folder = project / "backups"
    folder.mkdir(exist_ok=True)
    stamp = current.date().isoformat()
    for root in sorted((project / "esami").iterdir()):
        if not (root / "esame.json").is_file():
            continue
        final = folder / f"{root.name}-{stamp}"
        if final.exists():
            continue
        with (root / "state/pipeline.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            temporary = folder / f".{root.name}-{stamp}-{os.getpid()}"
            if temporary.exists():
                shutil.rmtree(temporary)
            shutil.copytree(root, temporary, ignore=shutil.ignore_patterns("*.lock", "*.db", "*.db-shm", "*.db-wal", ".DS_Store"))
            for database in root.rglob("*.db"):
                target = temporary / database.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(target)) as destination:
                    source.backup(destination)
            temporary.rename(final)
        versions = sorted(folder.glob(root.name + "-20*"))
        for old in versions[:-3]:
            if old.is_dir():
                shutil.rmtree(old)


def due(root: Path, current: datetime) -> bool:
    exam = read_json(root / "esame.json", {})
    if not exam.get("attivo", False):
        return False
    policy = exam.get("start_policy", "now")
    if policy == "manual":
        return False
    if policy == "night":
        if exam.get("night_due_at"):
            try:
                if current < datetime.fromisoformat(exam["night_due_at"]):
                    return False
            except ValueError:
                return False
        if current.hour == 0 and current.minute < 30:
            return False
        if exam.get("night_started_for") == current.date().isoformat():
            return False
        return True
    report = read_json(root / "output/reports/pipeline_report.json", {})
    state = read_json(root / "state/pipeline.json", {})
    if exam.get("revision", 0) > report.get("revision", -1):
        return True
    if any(not batch.get("processed") and
           (batch.get("stage") != "uncertain" or (batch.get("mode") != "urgent" and batch.get("input_file_id")))
           for batch in state.get("batches", [])):
        return True
    if (report.get("pilot_review") or {}).get("status") == "ready_for_review":
        return False
    if any(job.get("status") in {"READY", "RETRY_READY", "RENDERING", "HTML_OK", "RENDER_FAILED"}
           for job in (state.get("jobs") or {}).values()):
        return True
    return False


def sweep(app: App):
    current = datetime.now().astimezone()
    for root in sorted((app.project / "esami").iterdir()):
        if not (root / "esame.json").is_file():
            continue
        if not due(root, current):
            state = read_json(root / "state/pipeline.json", {})
            if any(not batch.get("processed") and (batch.get("batch_id") or batch.get("response_id"))
                   for batch in state.get("batches", [])) and not read_json(root / "esame.json").get("attivo", False):
                try:
                    app.refresh_server(root.name)
                except Exception as error:
                    app.errors[root.name] = str(error)
            continue
        try:
            result = app.run(root.name)
            if result.get("started"):
                exam = read_json(root / "esame.json")
                if exam.get("start_policy") == "night":
                    exam["night_started_for"] = current.date().isoformat()
                    write_json(root / "esame.json", exam)
        except Exception as error:
            app.errors[root.name] = str(error)


def main(project=PROJECT, interval=45):
    project = Path(project)
    (project / "state").mkdir(parents=True, exist_ok=True)
    with (project / "state/worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        app = App(project)
        while True:
            write_json(project / "state/worker-heartbeat.json", {
                "pid": os.getpid(), "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "errors": app.errors,
            })
            try:
                sweep(app)
                backup_if_due(project, datetime.now().astimezone())
            except Exception:
                with (project / "state/worker-errors.log").open("a", encoding="utf-8") as output:
                    output.write(traceback.format_exc() + "\n")
            time.sleep(interval)


if __name__ == "__main__":
    main()
