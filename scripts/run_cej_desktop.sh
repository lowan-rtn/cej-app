#!/bin/sh
set -eu

VENV="$HOME/.local/share/cej-dashboard/venv"
VENV_SITE="$VENV/lib/python3.13/site-packages"

if [ -d "$VENV_SITE" ]; then
  if [ "${PYTHONPATH:-}" != "" ]; then
    export PYTHONPATH="$VENV_SITE:$PYTHONPATH"
  else
    export PYTHONPATH="$VENV_SITE"
  fi
fi

exec python3 /home/lreutin-rab-cc/scripts/cej_desktop.py "$@"
