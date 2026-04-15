#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${HERMES_LINK_INSTALL_ROOT:-$HOME/.local/share/hermes-link}"
VENV_DIR="${HERMES_LINK_VENV_DIR:-$INSTALL_ROOT/venv}"
REMOVE_DATA="${HERMES_LINK_REMOVE_DATA:-0}"

if [[ ! -x "${VENV_DIR}/bin/hermes-link" ]]; then
  echo "Hermes Link is not installed in ${VENV_DIR}." >&2
  exit 1
fi

if [[ "${REMOVE_DATA}" == "1" ]]; then
  "${VENV_DIR}/bin/hermes-link" uninstall --yes --remove-data
else
  "${VENV_DIR}/bin/hermes-link" uninstall --yes
fi

echo "Hermes Link uninstalled."
