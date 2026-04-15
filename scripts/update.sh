#!/usr/bin/env bash
set -euo pipefail

DEFAULT_GITHUB_REPOSITORY="HangbinYang/hermes-link"
INSTALL_ROOT="${HERMES_LINK_INSTALL_ROOT:-$HOME/.local/share/hermes-link}"
VENV_DIR="${HERMES_LINK_VENV_DIR:-$INSTALL_ROOT/venv}"
GITHUB_REPOSITORY="${HERMES_LINK_GITHUB_REPOSITORY:-$DEFAULT_GITHUB_REPOSITORY}"
REF="${HERMES_LINK_REF:-main}"
REF_TYPE="${HERMES_LINK_REF_TYPE:-branch}"
PACKAGE_SPEC="${HERMES_LINK_PACKAGE_SPEC:-}"
RESTART_AFTER_UPDATE="${HERMES_LINK_RESTART_AFTER_UPDATE:-1}"

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

if [[ ! -x "${VENV_DIR}/bin/hermes-link" ]]; then
  echo "Hermes Link is not installed in ${VENV_DIR}. Run scripts/install.sh first." >&2
  exit 1
fi

if [[ -z "${PACKAGE_SPEC}" ]]; then
  PACKAGE_SPEC="$(build_default_package_spec)"
fi

WAS_RUNNING="$("${VENV_DIR}/bin/hermes-link" status --json | "${VENV_DIR}/bin/python" -c 'import json,sys; print("1" if json.load(sys.stdin)["service"]["running"] else "0")')"
"${VENV_DIR}/bin/hermes-link" update --spec "${PACKAGE_SPEC}"

if [[ "${RESTART_AFTER_UPDATE}" == "1" && "${WAS_RUNNING}" == "1" ]]; then
  "${VENV_DIR}/bin/hermes-link" restart
fi

echo "Hermes Link updated."
echo "  source: ${PACKAGE_SPEC}"
echo "  cli:    ${VENV_DIR}/bin/hermes-link"
