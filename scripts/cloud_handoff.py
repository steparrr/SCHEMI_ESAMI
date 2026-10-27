"""Build and verify a new exam's transport package for a future cloud worker.

This module does not submit a job or imply that the Mac can be switched off.
Only a durable acknowledgement from an independently deployed cloud service
can establish that condition.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from common import PROJECT, digest, exam_dir, read_json

FORMAT = "schemi-esami-cloud-job-v1"
MAX_FILES = 2000
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
_SECRET_FIELDS = {"api_key", "openai_api_key", "password", "secret", "client_secret",
                  "private_key", "access_token", "refresh_token", "authorization"}


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def code_fingerprint(project: Path) -> str:
    """Pin the processing code, so a worker cannot silently use another build."""
    project = Path(project)
    sources = [*sorted((project / "scripts").glob("*.py")),
               *sorted((project / "renderer").glob("*.py"))]
    required = (project / "scripts/pipeline.py", project / "scripts/local_app.py",
                project / "renderer/render_pdf.py")
    if not sources or any(not path.is_file() for path in [*sources, *required]):
        raise ValueError("Sorgenti del programma incompleti.")
    return digest({str(path.relative_to(project)): _sha256(path) for path in sources})


def _has_secret_field(value):
    if isinstance(value, dict):
        return any(str(key).lower() in _SECRET_FIELDS or _has_secret_field(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_has_secret_field(item) for item in value)
    return False


def _source_files(project: Path, root: Path, slug: str) -> list[tuple[str, Path]]:
    files = [("config/pipeline.json", project / "config/pipeline.json"),
             ("config/mermaid.json", project / "config/mermaid.json"),
             ("prompts/main.txt", project / "prompts/main.txt"),
             (f"esami/{slug}/esame.json", root / "esame.json")]
    custom = root / "prompt.txt"
    if custom.is_file():
        files.append((f"esami/{slug}/prompt.txt", custom))
    files.extend((f"esami/{slug}/{path.relative_to(root).as_posix()}", path)
                 for path in sorted((root / "input").rglob("*"))
                 if path.is_file() and not any(part.startswith(".") for part in path.relative_to(root).parts))
    if not any(name.startswith(f"esami/{slug}/input/") for name, _ in files):
        raise ValueError("Nessun materiale d'esame da trasferire.")
    if len(files) > MAX_FILES:
        raise ValueError("Troppi file nel pacchetto cloud.")
    return files


def _valid_name(name: str, slug: str) -> bool:
    if "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    parts = tuple(name.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return False
    if name in {"config/pipeline.json", "config/mermaid.json", "prompts/main.txt", f"esami/{slug}/esame.json",
                f"esami/{slug}/prompt.txt"}:
        return True
    return len(parts) >= 4 and parts[:3] == ("esami", slug, "input")


def build_package(project: Path, slug: str, destination: Path, job_id: str | None = None) -> dict:
    """Snapshot a draft exam under the pipeline lock without modifying its state."""
    project, destination = Path(project).resolve(), Path(destination)
    root = exam_dir(slug, project)
    job_id = job_id or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("ID lavoro non valido.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.resolve().is_relative_to(project):
        relative = destination.resolve().relative_to(project)
        if not relative.parts or relative.parts[0] not in {"state", "esami", ".cache"}:
            raise ValueError("Salva il pacchetto fuori dai sorgenti o in una cartella privata del progetto.")
    if destination.exists():
        raise FileExistsError(destination)
    temp = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with (root / "state/pipeline.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = read_json(root / "state/pipeline.json", {})
            if state.get("jobs") or state.get("batches") or state.get("planning_latest"):
                raise ValueError("Esame già avviato: conserva gli ID remoti; il trasferimento richiede una migrazione dedicata.")
            exam = read_json(root / "esame.json")
            config = read_json(project / "config/pipeline.json")
            if _has_secret_field(exam) or _has_secret_field(config):
                raise ValueError("Una configurazione contiene un campo di credenziale: rimuovilo prima del trasferimento.")
            files = _source_files(project, root, slug)
            code_hash = code_fingerprint(project)
            sizes = [path.stat().st_size for _, path in files]
            if any(path.is_symlink() or not path.resolve().is_relative_to(project) for _, path in files):
                raise ValueError("Il pacchetto non può contenere collegamenti o file esterni al progetto.")
            if any(size > MAX_FILE_BYTES for size in sizes) or sum(sizes) > MAX_TOTAL_BYTES:
                raise ValueError("Materiali troppo grandi per un singolo pacchetto cloud.")
            manifest = {"format": FORMAT, "job_id": job_id, "exam_slug": slug,
                        "revision": exam.get("revision", 0), "code_fingerprint": code_hash,
                        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": {}}
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    for name, path in files:
                        if not _valid_name(name, slug):
                            raise ValueError("Percorso non consentito nel pacchetto.")
                        before = path.stat()
                        checksum = hashlib.sha256()
                        count = 0
                        with path.open("rb") as source, archive.open(name, "w") as target:
                            for block in iter(lambda: source.read(1024 * 1024), b""):
                                checksum.update(block)
                                target.write(block)
                                count += len(block)
                        after = path.stat()
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise ValueError("Un file è cambiato durante la preparazione del pacchetto.")
                        manifest["files"][name] = {"sha256": checksum.hexdigest(), "size": count}
                    archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True))
                output.flush()
                os.fsync(output.fileno())
            os.link(temp, destination)
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return {"job_id": job_id, "sha256": _sha256(destination), "bytes": destination.stat().st_size,
                "code_fingerprint": code_hash}
    finally:
        temp.unlink(missing_ok=True)


def verify_package(package: Path, project: Path | None = None) -> dict:
    """Reject incomplete, altered, unexpected or incompatible archives."""
    with zipfile.ZipFile(package) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)) or names.count("manifest.json") != 1 or len(names) > MAX_FILES + 1:
            raise ValueError("Indice del pacchetto non valido.")
        item = archive.getinfo("manifest.json")
        if item.file_size > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest troppo grande.")
        manifest = json.loads(archive.read(item))
        slug = manifest.get("exam_slug", "")
        if (manifest.get("format") != FORMAT or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug)
                or not re.fullmatch(r"[0-9a-f]{32}", manifest.get("job_id", ""))):
            raise ValueError("Formato o identificativi del pacchetto non validi.")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(names) != set(files) | {"manifest.json"}:
            raise ValueError("Elenco dei file del pacchetto non corrispondente.")
        if not {"config/pipeline.json", "config/mermaid.json", "prompts/main.txt",
                f"esami/{slug}/esame.json"}.issubset(files):
            raise ValueError("File obbligatori mancanti.")
        if not any(name.startswith(f"esami/{slug}/input/") for name in files):
            raise ValueError("Nessun materiale d'esame nel pacchetto.")
        if (archive.getinfo(f"esami/{slug}/esame.json").file_size > MAX_MANIFEST_BYTES
                or archive.getinfo("config/pipeline.json").file_size > MAX_MANIFEST_BYTES):
            raise ValueError("Configurazione o esame troppo grandi.")
        exam = json.loads(archive.read(f"esami/{slug}/esame.json"))
        config = json.loads(archive.read("config/pipeline.json"))
        if _has_secret_field(exam) or _has_secret_field(config):
            raise ValueError("Il pacchetto contiene un campo di credenziale.")
        if exam.get("revision", 0) != manifest.get("revision"):
            raise ValueError("Revisione dell'esame non corrispondente.")
        total = 0
        for info in infos:
            if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Collegamenti e directory non sono consentiti.")
            if info.filename == "manifest.json":
                continue
            name = info.filename
            if not _valid_name(name, slug):
                raise ValueError("Percorso non consentito nel pacchetto.")
            details = files[name]
            if (not isinstance(details, dict) or not isinstance(details.get("size"), int)
                    or details["size"] < 0 or details["size"] > MAX_FILE_BYTES
                    or info.file_size != details["size"]):
                raise ValueError("Dimensione di file non valida.")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("Pacchetto troppo grande.")
            checksum = hashlib.sha256()
            with archive.open(info) as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    checksum.update(block)
            if checksum.hexdigest() != details.get("sha256"):
                raise ValueError("Integrità del file non valida: " + name)
        if project is not None and manifest.get("code_fingerprint") != code_fingerprint(project):
            raise ValueError("Versione del motore diversa da quella usata per creare il pacchetto.")
        return manifest


def extract_package(package: Path, destination: Path, project: Path) -> dict:
    """Install a verified package into a new private job directory atomically."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    manifest = verify_package(package, project)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(package) as archive:
            for name, details in manifest["files"].items():
                target = temporary / name
                target.parent.mkdir(parents=True, exist_ok=True)
                checksum = hashlib.sha256()
                with archive.open(name) as source, target.open("xb") as output:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        checksum.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if checksum.hexdigest() != details["sha256"]:
                    raise ValueError("Il pacchetto è cambiato durante l'estrazione.")
        for folder in ("scripts", "renderer"):
            (temporary / folder).mkdir()
            for source in (Path(project) / folder).glob("*.py"):
                shutil.copy2(source, temporary / folder / source.name)
        if code_fingerprint(temporary) != manifest["code_fingerprint"]:
            raise ValueError("Il codice del worker è cambiato durante la preparazione.")
        exam_root = temporary / "esami" / manifest["exam_slug"]
        for folder in ("corpus", "images/originals", "source_packs", "external_supplements",
                       "output/pdf", "output/html", "output/raw", "output/reports", "batch", "state",
                       "logs", "diagrams/generated"):
            (exam_root / folder).mkdir(parents=True, exist_ok=True)
        for root, dirs, _ in os.walk(temporary, topdown=False):
            handle = os.open(root, os.O_RDONLY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
        temporary.rename(destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepara o verifica un pacchetto per il futuro worker cloud.")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("slug")
    build.add_argument("destination", type=Path)
    build.add_argument("--project", type=Path, default=PROJECT)
    verify = commands.add_parser("verify")
    verify.add_argument("package", type=Path)
    verify.add_argument("--project", type=Path, default=PROJECT)
    args = parser.parse_args(argv)
    if args.command == "build":
        result = build_package(args.project, args.slug, args.destination)
    else:
        manifest = verify_package(args.package, args.project)
        result = {"job_id": manifest["job_id"], "files": len(manifest["files"]),
                  "code_fingerprint": manifest["code_fingerprint"]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
