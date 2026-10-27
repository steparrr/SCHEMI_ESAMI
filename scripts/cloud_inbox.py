"""Durable, idempotent local acceptance for a future HTTPS cloud gateway.

The gateway must authenticate its caller and enforce transport limits before
using this module. No network listener is started here.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cloud_handoff import _sha256, extract_package, verify_package
from common import write_json


def _sync_directory(path: Path):
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def accept_package(package: Path, expected_sha256: str, storage: Path, code_project: Path) -> dict:
    """Return a receipt only after the archive and extracted job are durable.

    A retry with the same package returns the original receipt. Reusing the ID
    with different bytes fails, even if the first upload was interrupted.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Impronta del pacchetto non valida.")
    package, storage, code_project = Path(package), Path(storage), Path(code_project)
    if _sha256(package) != expected_sha256:
        raise ValueError("Il contenuto caricato non corrisponde all'impronta attesa.")
    incoming = verify_package(package, code_project)
    job_id = incoming["job_id"]
    storage.mkdir(mode=0o700, parents=True, exist_ok=True)
    for folder in ("packages", "jobs", "receipts"):
        (storage / folder).mkdir(mode=0o700, exist_ok=True)
    with (storage / ".accept.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved_package = storage / "packages" / (job_id + ".zip")
        job = storage / "jobs" / job_id
        receipt_path = storage / "receipts" / (job_id + ".json")
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if (receipt.get("sha256") != expected_sha256 or not job.is_dir()
                    or not saved_package.is_file() or _sha256(saved_package) != expected_sha256):
                raise ValueError("L'ID lavoro è già associato a un altro pacchetto o a dati incompleti.")
            return receipt
        temporary = saved_package.with_name("." + job_id + "." + uuid.uuid4().hex + ".tmp")
        try:
            if not saved_package.exists():
                with package.open("rb") as source, temporary.open("xb") as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                if _sha256(temporary) != expected_sha256:
                    raise ValueError("Il contenuto caricato non corrisponde all'impronta attesa.")
                verify_package(temporary, code_project)
                os.link(temporary, saved_package)
                _sync_directory(saved_package.parent)
            elif _sha256(saved_package) != expected_sha256:
                raise ValueError("L'ID lavoro è già associato a un altro pacchetto.")
            if not job.exists():
                extract_package(saved_package, job, code_project)
            elif not job.is_dir():
                raise ValueError("La cartella del lavoro non è valida.")
            receipt = {"job_id": job_id, "sha256": expected_sha256, "status": "accepted",
                       "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "files": len(incoming["files"]), "code_fingerprint": incoming["code_fingerprint"]}
            write_json(receipt_path, receipt)
            _sync_directory(receipt_path.parent)
            return receipt
        finally:
            temporary.unlink(missing_ok=True)
