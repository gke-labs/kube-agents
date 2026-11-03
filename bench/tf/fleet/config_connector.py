#!/usr/bin/env python3
"""Apply seeded-a's ConfigConnector object, run by a terraform_data local-exec in config-connector.tf.

The GKE Config Connector add-on installs the CRDs and the operator. It does
not create the cluster-scoped ConfigConnector object that tells the operator
which mode to run in, so this script creates it, in namespaced mode. A
kubernetes_manifest resource cannot do this: it reads the CRD at plan time,
and the CRD does not exist until the apply that enables the add-on is done.

The script reads the API server address and its CA from the environment, and
the object's name, mode and labels as well. It takes a fresh access token as
gateway_status.py does: from Application Default Credentials, then from
`gcloud auth print-access-token`. It sends one server-side apply, and it tries
again while the API answers 404, because the add-on can install the CRD some
minutes after the cluster update is done. Standard library only: the reconcile
image has python3 and gcloud, and kubectl is not promised. It exits non-zero,
with the API's answer, on any other failure, so the apply fails and does not
record an object that is not there.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

LOG_PREFIX = "config_connector"
HOST_ENV = "K8S_HOST"
CA_ENV = "K8S_CA_BASE64"
NAME_ENV = "CONFIG_CONNECTOR_NAME"
MODE_ENV = "CONFIG_CONNECTOR_MODE"
# The object's labels, as a JSON object.
LABELS_ENV = "CONFIG_CONNECTOR_LABELS"
# The access token, tried in order, as in gateway_status.py.
TOKEN_COMMANDS = (
    ("gcloud", "auth", "application-default", "print-access-token"),
    ("gcloud", "auth", "print-access-token"),
)
TOKEN_TIMEOUT_SECONDS = 60

API_VERSION = "core.cnrm.cloud.google.com/v1beta1"
KIND = "ConfigConnector"
OBJECT_PATH = "/apis/core.cnrm.cloud.google.com/v1beta1/configconnectors/{name}"
# Server-side apply: one request creates the object or brings its spec to
# this one, and `force` takes over a field another manager set.
APPLY_CONTENT_TYPE = "application/apply-patch+yaml"
FIELD_MANAGER = "seeded-fleet"
AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
CONTENT_TYPE_HEADER = "Content-Type"
PATCH = "PATCH"
REQUEST_TIMEOUT_SECONDS = 30
# The answer while the add-on has not yet installed the CRD.
NOT_FOUND = 404
# How long to wait for the CRD, and how often to ask. Ten minutes covers the
# add-on's install on a cluster update; a CRD that is still missing after
# that is an add-on that did not install, and the apply fails.
CRD_WAIT_SECONDS = 600
CRD_RETRY_SECONDS = 15


def access_token() -> str:
    failures = []
    for command in TOKEN_COMMANDS:
        try:
            done = subprocess.run(list(command), capture_output=True, text=True, timeout=TOKEN_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{' '.join(command)} failed: {exc}")
            continue
        if done.returncode == 0 and done.stdout.strip():
            if failures:
                sys.stderr.write(f"{LOG_PREFIX}: token from {' '.join(command)} after {'; '.join(failures)}\n")
            return done.stdout.strip()
        failures.append(f"{' '.join(command)} exited {done.returncode}: {done.stderr.strip()}")
    raise SystemExit(f"{LOG_PREFIX}: no access token; " + "; ".join(failures))


def manifest(name: str, mode: str, labels: dict) -> dict:
    return {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": {"name": name, "labels": labels},
        "spec": {"mode": mode},
    }


def apply(host: str, token: str, context: ssl.SSLContext, body: dict) -> int:
    """Sends the apply once. Returns 0 on success, NOT_FOUND while the CRD is missing; exits on anything else."""
    query = urllib.parse.urlencode({"fieldManager": FIELD_MANAGER, "force": "true"})
    path = OBJECT_PATH.format(name=body["metadata"]["name"])
    request = urllib.request.Request(f"{host}{path}?{query}", data=json.dumps(body).encode(), method=PATCH)
    request.add_header(AUTHORIZATION_HEADER, BEARER_PREFIX + token)
    request.add_header(CONTENT_TYPE_HEADER, APPLY_CONTENT_TYPE)
    try:
        with urllib.request.urlopen(request, context=context, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
            reply.read()
            return 0
    except urllib.error.HTTPError as exc:
        if exc.code == NOT_FOUND:
            return NOT_FOUND
        raise SystemExit(f"{LOG_PREFIX}: PATCH {path} answered {exc.code}: {exc.read().decode(errors='replace')}")


def main() -> int:
    env = {name: os.environ.get(name, "") for name in (HOST_ENV, CA_ENV, NAME_ENV, MODE_ENV, LABELS_ENV)}
    missing = sorted(name for name, value in env.items() if not value)
    if missing:
        sys.stderr.write(f"{LOG_PREFIX}: missing {', '.join(missing)}\n")
        return 1
    try:
        labels = json.loads(env[LABELS_ENV])
    except ValueError as exc:
        sys.stderr.write(f"{LOG_PREFIX}: {LABELS_ENV} is not JSON: {exc}\n")
        return 1
    body = manifest(env[NAME_ENV], env[MODE_ENV], labels)
    deadline = time.monotonic() + CRD_WAIT_SECONDS
    with tempfile.NamedTemporaryFile("wb", suffix=".crt") as ca:
        ca.write(base64.b64decode(env[CA_ENV]))
        ca.flush()
        context = ssl.create_default_context(cafile=ca.name)
        while True:
            # A fresh token for each attempt: the wait can be long.
            if apply(env[HOST_ENV], access_token(), context, body) == 0:
                break
            if time.monotonic() >= deadline:
                sys.stderr.write(f"{LOG_PREFIX}: the ConfigConnector CRD is not on the cluster after {CRD_WAIT_SECONDS}s; the add-on did not install it\n")
                return 1
            sys.stderr.write(f"{LOG_PREFIX}: the ConfigConnector CRD is not on the cluster yet; trying again in {CRD_RETRY_SECONDS}s\n")
            time.sleep(CRD_RETRY_SECONDS)
    print(f"{LOG_PREFIX}: applied {KIND} {env[NAME_ENV]} with spec.mode {env[MODE_ENV]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
