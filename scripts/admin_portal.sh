#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly REQUIREMENTS="${REPO_ROOT}/admin_console/requirements.txt"
readonly APP="${REPO_ROOT}/admin_console/app.py"
readonly PORT="${ADMIN_PORTAL_PORT:-8501}"
readonly HOST="127.0.0.1"

fail() {
  echo "Error: $*" >&2
  exit 1
}

if [[ ! "${PORT}" =~ ^[0-9]+$ ]] || ((PORT < 1024 || PORT > 65535)); then
  fail "ADMIN_PORTAL_PORT must be an integer between 1024 and 65535."
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

streamlit_bin="${REPO_ROOT}/.venv/bin/streamlit"
if [[ ! -x "${streamlit_bin}" ]]; then
  echo "Preparing the local admin portal environment..."
  if command -v uv >/dev/null 2>&1; then
    if [[ ! -x "${REPO_ROOT}/.venv/bin/python" ]]; then
      uv venv "${REPO_ROOT}/.venv"
    fi
    uv pip install \
      --python "${REPO_ROOT}/.venv/bin/python" \
      --requirement "${REQUIREMENTS}"
  else
    command -v python3 >/dev/null 2>&1 ||
      fail "Python 3 or uv is required to create the portal environment."
    if [[ ! -x "${REPO_ROOT}/.venv/bin/python" ]]; then
      python3 -m venv "${REPO_ROOT}/.venv"
    fi
    "${REPO_ROOT}/.venv/bin/python" -m pip install \
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
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

# These command-line settings take precedence over user-level Streamlit config.
# Loopback is the network boundary for this development portal; XSRF and CORS
# protections remain enabled as defense in depth.
exec "${streamlit_bin}" run "${APP}" \
  --server.address="${HOST}" \
  --server.port="${PORT}" \
  --server.headless=true \
  --server.enableXsrfProtection=true \
  --server.enableCORS=true \
  --browser.serverAddress="${HOST}" \
  --browser.serverPort="${PORT}" \
  --browser.gatherUsageStats=false
