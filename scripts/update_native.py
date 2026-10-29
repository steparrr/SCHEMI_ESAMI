"""Aggiorna soltanto interfaccia e launcher in una V2 esistente."""
from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path


def install_overlay(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    config = target / "config/pipeline.json"
    if not config.is_file() or json.loads(config.read_text()).get("version") != 2 or not (target / "scripts/local_app.py").is_file():
        raise ValueError("Seleziona la cartella schemi-esami-v2 che contiene esami, scripts e config.")
    python = target / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("Completa prima INSTALLA.command nella cartella V2.")
    check = subprocess.run([str(python), "-c", "import tkinter"], capture_output=True, text=True)
    if check.returncode:
        raise ValueError("Questo Python non include Tkinter. Usa l'installazione standard da python.org.")
    backup = target / "state/aggiornamenti-nativa" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    files = ("native.py", "scripts/native_app.py", "scripts/telemetry.py", "scripts/control_room.py",
             "scripts/bottleneck_detector.py", "scripts/local_eta.py",
             "scripts/medical_illustrator.py", "scripts/diagnostic_report.py", "scripts/visual_qc.py",
             "scripts/materials_view.py", "scripts/coverage_matrix.py", "scripts/projects_view.py", "scripts/project_wizard.py",
             "scripts/project_actions.py",
             "scripts/local_recovery.py", "scripts/local_app.py", "scripts/ingest.py",
             "scripts/pipeline.py", "scripts/ocr.py", "APRI.command", "LEGGIMI-NATIVA.md", "tests/test_native.py")
    for relative in files:
        if not (source / relative).is_file():
            raise ValueError("Pacchetto incompleto: manca " + relative)
    for relative in files:
        destination = target / relative
        if destination.is_file() and destination.read_bytes() != (source / relative).read_bytes():
            archived = backup / relative
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, archived)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".native-update.tmp")
        shutil.copy2(source / relative, temporary)
        temporary.replace(destination)
    (target / "APRI.command").chmod(0o755)
    return target


def select_target():
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).expanduser()
    candidates = [Path.home() / "Desktop/schemi-esami/schemi-esami-v2", Path.home() / "Downloads/schemi-esami-v2", Path.home() / "Desktop/schemi-esami-v2"]
    found = [path for path in candidates if (path / "config/pipeline.json").is_file()]
    if len(found) == 1:
        return found[0]
    if sys.platform != "darwin":
        raise ValueError("Indica il percorso della V2 come argomento.")
    result = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Seleziona la cartella schemi-esami-v2 già installata")'], capture_output=True, text=True)
    if result.returncode:
        raise ValueError("Selezione annullata. Nessuna modifica effettuata.")
    return Path(result.stdout.strip())


def main():
    bundle = Path(__file__).resolve().parents[1]
    target = install_overlay(bundle, select_target())
    print("Interfaccia nativa installata in: " + str(target))
    print("Materie, fonti, impostazioni, invii e PDF conservati.")
    print("Se la vecchia maschera è in esecuzione, chiudila con Ctrl+C nel suo Terminale.")
    print("Ora apri APRI.command nella cartella V2: comparirà la finestra locale.")


if __name__ == "__main__":
    main()
