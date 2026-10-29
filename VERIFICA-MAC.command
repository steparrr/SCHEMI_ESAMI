#!/bin/zsh
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Esegui prima INSTALLA.command, oppure AGGIORNA.command nella V2 già installata."
  read "?Premi Invio per chiudere."
  exit 2
fi
cd "$ROOT"
"$PY" "$ROOT/scripts/mac_smoke.py" || {
  echo "La verifica Mac non è riuscita. Copia l'errore mostrato qui per la diagnosi."
  read "?Premi Invio per chiudere."
  exit 2
}
read "?Premi Invio per chiudere."
