#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${ROOT_DIR}/sya_task_scheduler"

select_python() {
  for candidate in "${PYTHON:-}" "${APP_DIR}/.venv313/bin/python" "${APP_DIR}/.venv/bin/python" "python3" "python"; do
    [[ -z "${candidate}" ]] && continue
    if command -v "${candidate}" >/dev/null 2>&1 || [[ -x "${candidate}" ]]; then
      if "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        echo "${candidate}"
        return
      fi
    fi
  done
  echo "Python 3.11+ is required." >&2
  exit 1
}

PYTHON_BIN="$(select_python)"

"${PYTHON_BIN}" -m json.tool "${ROOT_DIR}/manifest.json" >/dev/null
"${PYTHON_BIN}" -m json.tool "${ROOT_DIR}/api.openapi.json" >/dev/null
"${PYTHON_BIN}" -m json.tool "${ROOT_DIR}/module.json" >/dev/null
"${PYTHON_BIN}" -m json.tool "${ROOT_DIR}/config.example.json" >/dev/null

cd "${APP_DIR}"
PYTHONPATH="${APP_DIR}" "${PYTHON_BIN}" -m unittest discover -s tests -p 'test*.py' -v
