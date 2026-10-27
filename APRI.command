#!/bin/zsh
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Esegui prima INSTALLA.command."
  read "?Premi Invio per chiudere."
  exit 2
fi
cd "$ROOT"
mkdir -p "$ROOT/logs"
nohup "$PY" "$ROOT/native.py" >> "$ROOT/logs/native.out.log" 2>&1 < /dev/null &
NATIVE_PID=$!
sleep 2
if ! kill -0 "$NATIVE_PID" 2>/dev/null; then
  cat "$ROOT/logs/native.out.log"
  echo "Apertura interrotta: leggi l'errore sopra."
  read "?Premi Invio per chiudere."
  exit 2
fi
echo "La finestra di Schemi Esami è stata avviata. Puoi chiudere il Terminale."
