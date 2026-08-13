#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly REQUIREMENTS="${REPO_ROOT}/admin_console/requirements.txt"
readonly PORT="${ADMIN_PORTAL_PORT:-8501}"
readonly HOST="127.0.0.1"

fail() {
  echo "Error: $*" >&2
  exit 1
}

if [[ ! "${PORT}" =~ ^[0-9]+$ ]] || ((PORT < 1024 || PORT > 65535)); then
  fail "ADMIN_PORTAL_PORT must be an integer between 1024 and 65535."
fi
STREAMLIT_PORT="${ADMIN_PORTAL_STREAMLIT_PORT:-$((PORT + 1))}"
readonly STREAMLIT_PORT
if [[ ! "${STREAMLIT_PORT}" =~ ^[0-9]+$ ]] ||
  ((STREAMLIT_PORT < 1024 || STREAMLIT_PORT > 65535)); then
  fail "ADMIN_PORTAL_STREAMLIT_PORT must be an integer between 1024 and 65535."
fi
if [[ "${PORT}" == "${STREAMLIT_PORT}" ]]; then
  fail "ADMIN_PORTAL_PORT and ADMIN_PORTAL_STREAMLIT_PORT must differ."
fi

command -v gcloud >/dev/null 2>&1 ||
  fail "gcloud is required. Install the Google Cloud CLI, then run: gcloud auth login"

active_account="$(
  gcloud auth list \
    --filter='status:ACTIVE' \
    --format='value(account)' \
    --limit=1 \
    2>/dev/null || true
)"

if [[ -z "${active_account}" ]]; then
  echo "No active gcloud account was found." >&2
  echo "Authenticate, then run this launcher again:" >&2
  echo "  gcloud auth login" >&2
  exit 1
fi

# Validate that the cached login can still mint a token. Discard the token:
# the local portal receives only the verified account name.
if ! gcloud auth print-access-token \
  --account="${active_account}" \
  >/dev/null 2>&1; then
  echo "The active gcloud login for ${active_account} is expired or invalid." >&2
  echo "Refresh it, then run this launcher again:" >&2
  echo "  gcloud auth login" >&2
  exit 1
fi

active_project="$(
  gcloud config get-value project 2>/dev/null || true
)"
if [[ "${active_project}" == "(unset)" ]]; then
  active_project=""
fi

portal_python="${REPO_ROOT}/.venv/bin/python"
needs_dependencies=false
if [[ ! -x "${portal_python}" ]]; then
  needs_dependencies=true
elif ! "${portal_python}" -c \
  'import fastapi, httpx, streamlit, uvicorn, websockets' \
  >/dev/null 2>&1; then
  needs_dependencies=true
fi

if [[ "${needs_dependencies}" == true ]]; then
  echo "Preparing the local admin portal environment..."
  if command -v uv >/dev/null 2>&1; then
    if [[ ! -x "${portal_python}" ]]; then
      uv venv "${REPO_ROOT}/.venv"
    fi
    uv pip install \
      --python "${portal_python}" \
      --requirement "${REQUIREMENTS}"
  else
    command -v python3 >/dev/null 2>&1 ||
      fail "Python 3 or uv is required to create the portal environment."
    if [[ ! -x "${portal_python}" ]]; then
      python3 -m venv "${REPO_ROOT}/.venv"
    fi
    "${portal_python}" -m pip install \
      --requirement "${REQUIREMENTS}"
  fi
fi

readonly PORTAL_URL="http://${HOST}:${PORT}"

echo
echo "Kube Agents Admin Portal"
echo "Authenticated gcloud account: ${active_account}"
if [[ -n "${active_project}" ]]; then
  echo "Configured gcloud project: ${active_project}"
else
  echo "Configured gcloud project: none (select one in the portal)"
fi
echo "Local-only listener: ${HOST}:${PORT}"
echo "Launch: ${PORTAL_URL}"
echo
echo "Press Ctrl-C to stop the portal."

cd -- "${REPO_ROOT}"
export KUBE_AGENTS_ADMIN_USER="${active_account}"
export KUBE_AGENTS_GCLOUD_PROJECT="${active_project}"
export KUBE_AGENTS_PORTAL_API_URL="${PORTAL_URL}/api/v1"
export ADMIN_PORTAL_STREAMLIT_PORT="${STREAMLIT_PORT}"
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

# FastAPI owns the public loopback listener and lifecycle. Streamlit binds a
# separate private loopback port and is reachable only through the proxy.
exec "${portal_python}" -m uvicorn admin_console.api.main:app \
  --host="${HOST}" \
  --port="${PORT}" \
  --workers=1 \
  --no-access-log
