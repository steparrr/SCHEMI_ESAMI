#!/usr/bin/env python3
"""Single entry point for the desktop app and its login worker."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path


def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def data_root():
    return Path.home() / "Library/Application Support/Schemi Esami"


def bootstrap():
    resource = resource_root()
    project = Path(os.environ.get("SCHEMI_PROJECT_DIR", data_root() if getattr(sys, "frozen", False) else resource))
    project.mkdir(parents=True, exist_ok=True)
    for folder in ("scripts", "renderer", "prompts"):
        source = resource / folder
        if source.is_dir() and source.resolve() != (project / folder).resolve():
            shutil.copytree(source, project / folder, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (project / "config").mkdir(exist_ok=True)
    for source in (resource / "config").glob("*"):
        target = project / "config" / source.name
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)
    ocr = resource / ".cache/mac-ocr"
    if ocr.is_file():
        target = project / ".cache/mac-ocr"
        target.parent.mkdir(exist_ok=True)
        if not target.exists() or target.read_bytes() != ocr.read_bytes():
            temporary = target.with_suffix(".tmp")
            shutil.copy2(ocr, temporary)
            temporary.chmod(0o755)
            temporary.replace(target)
    (project / "esami").mkdir(exist_ok=True)
    (project / "state").mkdir(exist_ok=True)
    os.environ["SCHEMI_PROJECT_DIR"] = str(project)
    sys.path.insert(0, str(resource / "scripts"))
    return project


def worker_executable(agent_path):
    """Keep the login item attached to an installed app, not a ZIP preview."""
    current = Path(sys.executable).resolve()
    applications = (Path.home() / "Applications").resolve()
    if current.is_relative_to(applications):
        return current
    if agent_path.is_file():
        try:
            previous = Path(plistlib.loads(agent_path.read_bytes())["ProgramArguments"][0]).resolve()
            if previous.is_file() and previous.is_relative_to(applications):
                return previous
        except (KeyError, IndexError, OSError, ValueError, TypeError):
            pass
    installed = sorted(applications.glob("Schemi Esami*.app"), reverse=True)
    for bundle in installed:
        candidate = bundle / "Contents/MacOS/Schemi Esami"
        if candidate.is_file():
            return candidate.resolve()
    return current


def _launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _bootstrap_agent(path):
    """launchd can briefly reject bootstrap immediately after bootout."""
    for attempt in range(4):
        result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
        if result.returncode == 0:
            return result
        if attempt < 3:
            time.sleep(0.5 * (attempt + 1))
    return result


def worker_agent(project, path):
    """Keep the source checkout's worker separate from an installed app bundle."""
    project = Path(project).resolve()
    if getattr(sys, "frozen", False):
        label = "it.schemi-esami.worker"
        arguments = [str(worker_executable(path)), "--worker"]
    else:
        label = "it.schemi-esami.source-worker"
        python = resource_root() / ".venv/bin/python"
        if not python.is_file():
            raise ValueError("Completa INSTALLA.command prima di attivare il worker dei sorgenti.")
        arguments = [str(python), str(resource_root() / "native.py"), "--worker"]
    agent = {"Label": label, "ProgramArguments": arguments,
             "RunAtLoad": True, "KeepAlive": True,
             "StandardOutPath": str(project / "state/worker-stdout.log"),
             "StandardErrorPath": str(project / "state/worker-stderr.log")}
    if not getattr(sys, "frozen", False):
        agent["WorkingDirectory"] = str(project)
        agent["EnvironmentVariables"] = {"SCHEMI_PROJECT_DIR": str(project)}
    return label, agent


def ensure_login_worker(project):
    """Register the existing local worker at login while preserving app installs."""
    if sys.platform != "darwin":
        return
    label = "it.schemi-esami.worker" if getattr(sys, "frozen", False) else "it.schemi-esami.source-worker"
    folder = Path.home() / "Library/LaunchAgents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (label + ".plist")
    try:
        _, agent = worker_agent(project, path)
    except (OSError, ValueError) as error:
        (Path(project) / "state/worker-agent-error.txt").write_text(str(error), encoding="utf-8")
        return
    payload = plistlib.dumps(agent)
    if not path.exists() or path.read_bytes() != payload:
        previous_payload = path.read_bytes() if path.exists() else None
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)
        target = f"gui/{os.getuid()}/{label}"
        _launchctl("bootout", target)
        result = _bootstrap_agent(path)
        if result.returncode and previous_payload:
            temporary.write_bytes(previous_payload)
            temporary.replace(path)
            restored = _bootstrap_agent(path)
            if restored.returncode == 0:
                result = restored
    else:
        service = _launchctl("print", f"gui/{os.getuid()}/{label}")
        if service.returncode == 0:
            result = (service if "state = running" in service.stdout
                      else _launchctl("kickstart", f"gui/{os.getuid()}/{label}"))
        else:
            result = _bootstrap_agent(path)
    error_file = project / "state/worker-agent-error.txt"
    if result.returncode:
        error_file.write_text((result.stderr or result.stdout or "Avvio automatico non riuscito")[:2000], encoding="utf-8")
    else:
        error_file.unlink(missing_ok=True)


def main():
    project = bootstrap()
    if "--self-test" in sys.argv:
        import codecs
        import pymupdf
        import sqlite3
        import background_worker
        import native_app
        import finalize_pdf
        codecs.lookup("utf-8-sig")
        assert sqlite3.sqlite_version
        assert background_worker.main and native_app.main and finalize_pdf.finalize
        ocr_available = (project / ".cache/mac-ocr").is_file() or shutil.which("tesseract") is not None
        assert pymupdf.paper_rect("a4").width > 500
        print("SELF_TEST_OK" + ("" if ocr_available else " OCR_NON_DISPONIBILE"), flush=True)
    elif "--worker" in sys.argv:
        from background_worker import main as worker_main
        worker_main(project)
    else:
        ensure_login_worker(project)
        from native_app import main as window_main
        window_main()


if __name__ == "__main__":
    main()
