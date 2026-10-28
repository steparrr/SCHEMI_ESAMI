from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from common import PROJECT


def ocr_image(path: Path):
    native = PROJECT / ".cache" / "mac-ocr"
    if sys.platform == "darwin" and native.is_file():
        process = subprocess.run([str(native), str(path)], capture_output=True, text=True, timeout=150)
    elif shutil.which("tesseract"):
        languages = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=20)
        available = set(languages.stdout.splitlines())
        chosen = "+".join(x for x in ("ita", "eng") if x in available)
        if not chosen:
            raise ValueError("L'OCR richiede la lingua italiana o inglese di Tesseract.")
        process = subprocess.run(["tesseract", str(path), "stdout", "-l", chosen, "--psm", "3"], capture_output=True, text=True, timeout=150)
    else:
        raise ValueError("OCR non disponibile. Per scansioni e fotografie installa Tesseract con lingua italiana/inglese oppure usa il helper mac-ocr.")
    if process.returncode:
        raise ValueError("OCR fallito: " + process.stderr.strip()[:700])
    return process.stdout.strip()
