#!/bin/zsh
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "La .app va costruita su macOS."
  exit 2
fi
if [[ ! -x "$PY" ]]; then
  echo "Esegui prima INSTALLA.command in questa cartella."
  read "?Premi Invio per chiudere."
  exit 2
fi

cd "$ROOT"
VERIFY_DIR="$(mktemp -d "${TMPDIR:-/tmp}schemi-esami-build.XXXXXX")"
trap 'rm -rf "$VERIFY_DIR"' EXIT
echo "Controllo le dipendenze locali senza usare l'API..."
SCHEMI_PROJECT_DIR="$VERIFY_DIR/source" "$PY" "$ROOT/native.py" --self-test

if ! "$PY" -m PyInstaller --version >/dev/null 2>&1; then
  echo "Installo PyInstaller nella .venv per creare la .app (nessun servizio a pagamento)."
  "$PY" -m pip install 'pyinstaller>=6,<7'
fi

"$PY" -m PyInstaller --noconfirm --clean --distpath "$ROOT/dist" \
  --workpath "$ROOT/build" "$ROOT/Schemi Esami.spec"

APP="$ROOT/dist/Schemi Esami.app"
BIN="$APP/Contents/MacOS/Schemi Esami"
if [[ ! -x "$BIN" ]]; then
  echo "Build incompleta: eseguibile non trovato nella .app."
  exit 2
fi
echo "Controllo la .app costruita senza usare l'API..."
SCHEMI_PROJECT_DIR="$VERIFY_DIR/app" "$BIN" --self-test
echo "Build pronta: $APP"
echo "Provala prima di sostituire un'eventuale app installata."
read "?Premi Invio per chiudere."
