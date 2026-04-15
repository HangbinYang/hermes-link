#!/usr/bin/env bash
set -euo pipefail

DEFAULT_GITHUB_REPOSITORY="HangbinYang/hermes-link"
INSTALL_ROOT="${HERMES_LINK_INSTALL_ROOT:-$HOME/.local/share/hermes-link}"
VENV_DIR="${HERMES_LINK_VENV_DIR:-$INSTALL_ROOT/venv}"
GITHUB_REPOSITORY="${HERMES_LINK_GITHUB_REPOSITORY:-$DEFAULT_GITHUB_REPOSITORY}"
REF="${HERMES_LINK_REF:-main}"
REF_TYPE="${HERMES_LINK_REF_TYPE:-branch}"
PACKAGE_SPEC="${HERMES_LINK_PACKAGE_SPEC:-}"
ENABLE_AUTOSTART="${HERMES_LINK_ENABLE_AUTOSTART:-0}"
START_AFTER_INSTALL="${HERMES_LINK_START_AFTER_INSTALL:-1}"

detect_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    printf '%s\n' "${PYTHON}"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  echo "Python 3.11 or newer is required, but no python executable was found." >&2
  exit 1
}

build_default_package_spec() {
  case "${REF_TYPE}" in
    branch)
      printf 'https://github.com/%s/archive/refs/heads/%s.tar.gz\n' "${GITHUB_REPOSITORY}" "${REF}"
      ;;
    tag)
      printf 'https://github.com/%s/archive/refs/tags/%s.tar.gz\n' "${GITHUB_REPOSITORY}" "${REF}"
      ;;
    commit)
      printf 'git+https://github.com/%s.git@%s\n' "${GITHUB_REPOSITORY}" "${REF}"
      ;;
    *)
      echo "Unsupported HERMES_LINK_REF_TYPE: ${REF_TYPE}. Use branch, tag, or commit." >&2
      exit 1
      ;;
  esac
}

PYTHON_BIN="$(detect_python)"

if [[ -z "${PACKAGE_SPEC}" ]]; then
  PACKAGE_SPEC="$(build_default_package_spec)"
fi

"${PYTHON_BIN}" - <<'PY'
import sys

if sys.version_info < (3, 11):
    raise SystemExit("Hermes Link requires Python 3.11 or newer.")
PY

mkdir -p "${INSTALL_ROOT}"
"${PYTHON_BIN}" -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/pip" install --upgrade "${PACKAGE_SPEC}"
INSTALL_ARGS=(install)
if [[ "${START_AFTER_INSTALL}" == "1" ]]; then
  INSTALL_ARGS+=(--start)
else
  INSTALL_ARGS+=(--no-start)
fi
if [[ "${ENABLE_AUTOSTART}" == "1" ]]; then
  INSTALL_ARGS+=(--autostart)
else
  INSTALL_ARGS+=(--no-autostart)
fi
"${VENV_DIR}/bin/hermes-link" "${INSTALL_ARGS[@]}"

echo "Hermes Link installed."
echo "  source: ${PACKAGE_SPEC}"
echo "  venv: ${VENV_DIR}"
echo "  cli:  ${VENV_DIR}/bin/hermes-link"
