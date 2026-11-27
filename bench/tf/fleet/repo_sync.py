#!/usr/bin/env python3
"""Apply the seeded-post-merge namespace's RepoSync, run by a terraform_data local-exec in post-merge-sync.tf.

Config Sync installs the RepoSync CRD some minutes after its fleet feature is
turned on for seeded-a, so a kubernetes_manifest resource, which reads the CRD
at plan time, cannot plan the object on the apply that turns Config Sync on.
This is config_connector.py's answer to the same problem, for this object: one
server-side apply, tried again inside one wait window while the API answers
404 (no CRD yet), 429 or 5xx, or does not answer. The token comes from
config_connector.access_token. Standard library only, as there.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from config_connector import (
    APPLY_CONTENT_TYPE,
    AUTHORIZATION_HEADER,
    BEARER_PREFIX,
    CA_ENV,
    CONTENT_TYPE_HEADER,
    CRD_RETRY_SECONDS,
    CRD_WAIT_SECONDS,
    FIELD_MANAGER,
    HOST_ENV,
    NOT_FOUND,
    PATCH,
    REQUEST_TIMEOUT_SECONDS,
    SERVER_ERROR_FLOOR,
    TOO_MANY_REQUESTS,
    access_token,
)

LOG_PREFIX = "repo_sync"
# The RepoSync, as JSON: post-merge-sync.tf builds it.
MANIFEST_ENV = "REPO_SYNC_MANIFEST"
API_VERSION = "configsync.gke.io/v1beta1"
OBJECT_PATH = "/apis/configsync.gke.io/v1beta1/namespaces/{namespace}/reposyncs/{name}"


def object_path(body: dict) -> str:
    """The object's URL path, refusing a manifest that is not a named, namespaced RepoSync."""
    metadata = body.get("metadata") or {}
    if body.get("apiVersion") != API_VERSION or body.get("kind") != "RepoSync":
        raise ValueError(f"not a {API_VERSION} RepoSync: {body.get('apiVersion')} {body.get('kind')}")
    if not metadata.get("name") or not metadata.get("namespace"):
        raise ValueError("the RepoSync names no name or no namespace")
    return OBJECT_PATH.format(namespace=metadata["namespace"], name=metadata["name"])


def apply(host: str, token: str, context: ssl.SSLContext, path: str, body: dict) -> str:
    """Sends the apply once; "" when applied, the reason to try again otherwise. Exits on any other answer."""
    query = urllib.parse.urlencode({"fieldManager": FIELD_MANAGER, "force": "true"})
    request = urllib.request.Request(f"{host}{path}?{query}", data=json.dumps(body).encode(), method=PATCH)
    request.add_header(AUTHORIZATION_HEADER, BEARER_PREFIX + token)
    request.add_header(CONTENT_TYPE_HEADER, APPLY_CONTENT_TYPE)
    try:
        with urllib.request.urlopen(request, context=context, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
            reply.read()
            return ""
    except urllib.error.HTTPError as exc:
        if exc.code == NOT_FOUND:
            return "the RepoSync CRD is not on the cluster yet"
        if exc.code >= SERVER_ERROR_FLOOR or exc.code == TOO_MANY_REQUESTS:
            return f"PATCH {path} answered {exc.code}"
        raise SystemExit(f"{LOG_PREFIX}: PATCH {path} answered {exc.code}: {exc.read().decode(errors='replace')}")
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # A reply cut short (IncompleteRead) or malformed is no answer either.
        return f"PATCH {path} got no answer: {exc}"


def main() -> int:
    env = {name: os.environ.get(name, "") for name in (HOST_ENV, CA_ENV, MANIFEST_ENV)}
    missing = sorted(name for name, value in env.items() if not value)
    if missing:
        sys.stderr.write(f"{LOG_PREFIX}: missing {', '.join(missing)}\n")
        return 1
    try:
        body = json.loads(env[MANIFEST_ENV])
        path = object_path(body)
    except ValueError as exc:
        sys.stderr.write(f"{LOG_PREFIX}: {MANIFEST_ENV}: {exc}\n")
        return 1
    deadline = time.monotonic() + CRD_WAIT_SECONDS
    with tempfile.NamedTemporaryFile("wb", suffix=".crt") as ca:
        ca.write(base64.b64decode(env[CA_ENV]))
        ca.flush()
        context = ssl.create_default_context(cafile=ca.name)
        while why := apply(env[HOST_ENV], access_token(), context, path, body):
            if time.monotonic() >= deadline:
                sys.stderr.write(f"{LOG_PREFIX}: {why} after {CRD_WAIT_SECONDS}s; Config Sync did not install it, or the API server did not recover\n")
                return 1
            sys.stderr.write(f"{LOG_PREFIX}: {why}; trying again in {CRD_RETRY_SECONDS}s\n")
            time.sleep(CRD_RETRY_SECONDS)
    print(f"{LOG_PREFIX}: applied {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
