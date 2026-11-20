#!/usr/bin/env python3
"""Create a stalled Config Sync RootSync on a fleet cluster, for a terraform_data local-exec.

No Config Sync operator runs on the fleet, so nothing creates a RootSync and
nothing writes its status. This script does both: one server-side apply of
`RootSync/root-sync`, then one merge patch of its status subresource with the
conditions the reconciler-manager writes when its reconciler Deployment fails.
A kubernetes_manifest resource cannot hold the object: it reads the CRD at
plan time, and the CRD is created by the same apply.

The script reads the API server address and its CA from the environment, and
the object's namespace, name, labels, spec and conditions as well. It takes a
fresh access token as config_connector.py does: from Application Default
Credentials, then from `gcloud auth print-access-token`. Each request is tried
again inside one wait window while the API answers 404, because the API server
serves a new CRD some seconds after it is created, and while the API answers
429 or 5xx or does not answer, because a failed run taints the resource and
the next reconcile then plans a replace. Standard library only: the reconcile
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
from datetime import datetime, timezone

LOG_PREFIX = "config_sync"
HOST_ENV = "K8S_HOST"
CA_ENV = "K8S_CA_BASE64"
NAMESPACE_ENV = "ROOT_SYNC_NAMESPACE"
NAME_ENV = "ROOT_SYNC_NAME"
# The object's labels, spec and status conditions, each as JSON.
LABELS_ENV = "ROOT_SYNC_LABELS"
SPEC_ENV = "ROOT_SYNC_SPEC"
CONDITIONS_ENV = "ROOT_SYNC_CONDITIONS"
JSON_ENVS = (LABELS_ENV, SPEC_ENV, CONDITIONS_ENV)
# The access token, tried in order, as in config_connector.py.
TOKEN_COMMANDS = (
    ("gcloud", "auth", "application-default", "print-access-token"),
    ("gcloud", "auth", "print-access-token"),
)
TOKEN_TIMEOUT_SECONDS = 60

API_VERSION = "configsync.gke.io/v1beta1"
KIND = "RootSync"
OBJECT_PATH = "/apis/configsync.gke.io/v1beta1/namespaces/{namespace}/rootsyncs/{name}"
STATUS_SUFFIX = "/status"
# Server-side apply: one request creates the object or brings its spec to
# this one, and `force` takes over a field another manager set.
APPLY_CONTENT_TYPE = "application/apply-patch+yaml"
MERGE_CONTENT_TYPE = "application/merge-patch+json"
FIELD_MANAGER = "seeded-fleet"
AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
CONTENT_TYPE_HEADER = "Content-Type"
PATCH = "PATCH"
REQUEST_TIMEOUT_SECONDS = 30
# The answer while the API server does not yet serve the CRD.
NOT_FOUND = 404
# An answer at or above this is the API server's own failure, tried again.
SERVER_ERROR_FLOOR = 500
# The API server's priority and fairness limit: a busy control plane, tried
# again.
TOO_MANY_REQUESTS = 429
# How long to wait, and how often to ask. A new CRD is served within seconds;
# ten minutes also covers a control plane that is briefly away, as in
# config_connector.py. A request still failing after that is a fault, and the
# apply fails.
WAIT_SECONDS = 600
RETRY_SECONDS = 15
# Kubernetes condition times: RFC 3339, to the second, in UTC.
CONDITION_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
CONDITION_TIME_FIELDS = ("lastTransitionTime", "lastUpdateTime")


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


def manifest(namespace: str, name: str, labels: dict, spec: dict) -> dict:
    return {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": {"namespace": namespace, "name": name, "labels": labels},
        "spec": spec,
    }


def status_patch(conditions: list, generation: int, now: datetime) -> dict:
    """The status the reconciler-manager would hold: the given conditions,
    stamped with one time, observed at the object's own generation."""
    stamp = now.strftime(CONDITION_TIME_FORMAT)
    stamped = [{**condition, **dict.fromkeys(CONDITION_TIME_FIELDS, stamp)} for condition in conditions]
    return {"status": {"observedGeneration": generation, "conditions": stamped}}


def send(host: str, token: str, context: ssl.SSLContext, path: str, query: dict, content_type: str, body: dict) -> tuple[dict | None, str]:
    """Sends one PATCH. Returns (the object, "") on success, and (None, why)
    while the CRD is not served, the API answers 429 or 5xx, or nothing
    answers; exits on any other answer."""
    url = f"{host}{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method=PATCH)
    request.add_header(AUTHORIZATION_HEADER, BEARER_PREFIX + token)
    request.add_header(CONTENT_TYPE_HEADER, content_type)
    try:
        with urllib.request.urlopen(request, context=context, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
            return json.loads(reply.read() or b"{}"), ""
    except urllib.error.HTTPError as exc:
        if exc.code == NOT_FOUND:
            return None, f"PATCH {path} answered 404: the API server does not serve the RootSync CRD yet"
        if exc.code >= SERVER_ERROR_FLOOR or exc.code == TOO_MANY_REQUESTS:
            return None, f"PATCH {path} answered {exc.code}"
        raise SystemExit(f"{LOG_PREFIX}: PATCH {path} answered {exc.code}: {exc.read().decode(errors='replace')}")
    except (urllib.error.URLError, OSError) as exc:
        # URLError and the connection errors (reset, refused, timeout) are
        # OSErrors: the control plane did not answer.
        return None, f"PATCH {path} got no answer: {exc}"


def until_done(deadline: float, attempt) -> dict | None:
    """Runs `attempt` until it returns an object or the window ends; None then."""
    while True:
        answer, why = attempt()
        if answer is not None:
            return answer
        if time.monotonic() >= deadline:
            sys.stderr.write(f"{LOG_PREFIX}: {why} after {WAIT_SECONDS}s\n")
            return None
        sys.stderr.write(f"{LOG_PREFIX}: {why}; trying again in {RETRY_SECONDS}s\n")
        time.sleep(RETRY_SECONDS)


def main() -> int:
    names = (HOST_ENV, CA_ENV, NAMESPACE_ENV, NAME_ENV, *JSON_ENVS)
    env = {name: os.environ.get(name, "") for name in names}
    missing = sorted(name for name, value in env.items() if not value)
    if missing:
        sys.stderr.write(f"{LOG_PREFIX}: missing {', '.join(missing)}\n")
        return 1
    parsed = {}
    for name in JSON_ENVS:
        try:
            parsed[name] = json.loads(env[name])
        except ValueError as exc:
            sys.stderr.write(f"{LOG_PREFIX}: {name} is not JSON: {exc}\n")
            return 1
    host, namespace, name = env[HOST_ENV], env[NAMESPACE_ENV], env[NAME_ENV]
    path = OBJECT_PATH.format(namespace=namespace, name=name)
    body = manifest(namespace, name, parsed[LABELS_ENV], parsed[SPEC_ENV])
    deadline = time.monotonic() + WAIT_SECONDS
    with tempfile.NamedTemporaryFile("wb", suffix=".crt") as ca:
        ca.write(base64.b64decode(env[CA_ENV]))
        ca.flush()
        context = ssl.create_default_context(cafile=ca.name)
        apply_query = {"fieldManager": FIELD_MANAGER, "force": "true"}
        # A fresh token for each attempt: the wait can be long.
        applied = until_done(deadline, lambda: send(host, access_token(), context, path, apply_query, APPLY_CONTENT_TYPE, body))
        if applied is None:
            return 1
        generation = int((applied.get("metadata") or {}).get("generation") or 1)
        patch = status_patch(parsed[CONDITIONS_ENV], generation, datetime.now(timezone.utc))
        if until_done(deadline, lambda: send(host, access_token(), context, path + STATUS_SUFFIX, {}, MERGE_CONTENT_TYPE, patch)) is None:
            return 1
    stalled = [c.get("type") for c in parsed[CONDITIONS_ENV] if c.get("status") == "True"]
    print(f"{LOG_PREFIX}: applied {KIND} {namespace}/{name} and wrote its status ({', '.join(stalled)} True)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
