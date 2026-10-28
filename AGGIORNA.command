#!/bin/zsh
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Serve Python 3 per l'aggiornamento."
  read "?Premi Invio per chiudere."
  exit 2
fi
python3 "$ROOT/scripts/update_native.py" "$@"
read "?Premi Invio per chiudere."
