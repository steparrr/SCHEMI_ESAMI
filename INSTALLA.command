#!/bin/zsh
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
for CANDIDATE in python3.13 python3.12 python3; do
  if command -v "$CANDIDATE" >/dev/null 2>&1 && "$CANDIDATE" -c 'import sys, tkinter; assert sys.version_info >= (3, 12)' >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$CANDIDATE")"
    break
  fi
done
if [[ -z "${PYTHON_BIN:-}" ]]; then
  echo "Serve Python 3.12 o successivo con Tkinter. Installa la versione macOS da python.org, poi riapri INSTALLA.command."
  read "?Premi Invio per chiudere."
  exit 2
fi
"$PYTHON_BIN" -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
echo "Dipendenze Python installate. Apri APRI.command per avviare Schemi Esami."
echo "Per PDF scansionati e fotografie, l'OCR richiede Tesseract (lingua italiana/inglese) o il helper mac-ocr già incluso in alcune distribuzioni."
read "?Premi Invio per chiudere."
