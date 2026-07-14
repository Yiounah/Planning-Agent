#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(python3 -c 'import json; print(json.load(open("manifest.json"))["version"])')"
TARGET="${1:-current}"

if [[ "${TARGET}" == "current" ]]; then
  OS_NAME="$(uname -s)"
  ARCH_NAME="$(uname -m)"
  case "${OS_NAME}" in
    Darwin) PLATFORM="macos" ;;
    Linux) PLATFORM="linux" ;;
    MINGW*|MSYS*|CYGWIN*) PLATFORM="windows" ;;
    *) PLATFORM="$(echo "${OS_NAME}" | tr '[:upper:]' '[:lower:]')" ;;
  esac
  case "${ARCH_NAME}" in
    arm64|aarch64) ARCH="arm64" ;;
    x86_64|amd64) ARCH="x64" ;;
    *) ARCH="${ARCH_NAME}" ;;
  esac
  TARGET="${PLATFORM}-${ARCH}"
fi

ARCHIVE="${ROOT_DIR}/dist/sya-function-scheduler-${VERSION}-${TARGET}.tar.gz"

mkdir -p "${ROOT_DIR}/dist"

tar -czf "${ARCHIVE}" \
  --exclude='.DS_Store' \
  --exclude='*/__pycache__' \
  --exclude='*/.pytest_cache' \
  --exclude='*/.venv' \
  --exclude='*/.venv313' \
  --exclude='*/.pycache_local' \
  -C "${ROOT_DIR}" \
  README.md \
  manifest.json \
  api.openapi.json \
  bin \
  assets \
  sya_task_scheduler

echo "${ARCHIVE}"
