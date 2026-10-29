#!/usr/bin/env python3
"""The scaled-zero-gateway fixture's status, run by a terraform_data local-exec in defects-a.tf.

No controller owns the fixture's GatewayClass, so nothing writes the status a
Gateway API controller would. This script writes it once, as Envoy Gateway
v1.9.2 wrote it for a Gateway whose proxy Deployment was scaled to zero (read
on a live cluster): the GatewayClass Accepted, the Gateway Accepted and not
Programmed (NoResources), its listener programmed with one attached route,
and the HTTPRoute Accepted and ResolvedRefs with no backend available
(EndpointsNotFound).

Reads the API server address and its CA from the environment, takes a fresh
access token from `gcloud auth print-access-token` (the reconciler's own
identity; the plan-time token the kubernetes provider holds can expire during
a long apply), GETs each object for its generation, and merge-patches its
status subresource. Standard library
only: the reconcile image carries python3 and gcloud, and kubectl is not
promised. Exits non-zero, with the API's answer, on any failure, so the apply
fails rather than leaving a fixture that reads as a fresh Gateway.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

HOST_ENV = "K8S_HOST"
TOKEN_COMMAND = ("gcloud", "auth", "print-access-token")
TOKEN_TIMEOUT_SECONDS = 60
CA_ENV = "K8S_CA_BASE64"
NAMESPACE_ENV = "FIXTURE_NAMESPACE"
GATEWAY_ENV = "FIXTURE_GATEWAY"
ROUTE_ENV = "FIXTURE_ROUTE"
LISTENER_ENV = "FIXTURE_LISTENER"
CLASS_ENV = "FIXTURE_GATEWAY_CLASS"
CONTROLLER_ENV = "FIXTURE_CONTROLLER"

GATEWAY_API_PATH = "/apis/gateway.networking.k8s.io/v1/namespaces/{namespace}/{resource}/{name}"
GATEWAY_CLASS_PATH = "/apis/gateway.networking.k8s.io/v1/gatewayclasses/{name}"
STATUS_SUFFIX = "/status"
GATEWAYS = "gateways"
HTTPROUTES = "httproutes"
MERGE_PATCH = "application/merge-patch+json"
AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
CONTENT_TYPE_HEADER = "Content-Type"
GET = "GET"
PATCH = "PATCH"
REQUEST_TIMEOUT_SECONDS = 30

# What Envoy Gateway v1.9.2 reported, by condition type: (status, reason, message).
GATEWAY_GROUP = "gateway.networking.k8s.io"
GATEWAY_KIND = "Gateway"
CLASS_CONDITIONS = (
    ("Accepted", "True", "Accepted", "Valid GatewayClass"),
)
GATEWAY_CONDITIONS = (
    ("Accepted", "True", "Accepted", "The Gateway has been scheduled by Envoy Gateway"),
    ("Programmed", "False", "NoResources", "Envoy replicas unavailable"),
)
LISTENER_CONDITIONS = (
    ("Programmed", "True", "Programmed", "Sending translated listener configuration to the data plane"),
    ("Accepted", "True", "Accepted", "Listener has been successfully translated"),
    ("ResolvedRefs", "True", "ResolvedRefs", "Listener references have been resolved"),
)
ROUTE_CONDITIONS = (
    ("Accepted", "True", "Accepted", "Route is accepted"),
    ("ResolvedRefs", "True", "ResolvedRefs", "Resolved all the Object references for the Route"),
    ("BackendsAvailable", "False", "EndpointsNotFound", "No endpoints found for the backend"),
)
ATTACHED_ROUTES = 1
SUPPORTED_KINDS = ({"group": GATEWAY_GROUP, "kind": "HTTPRoute"}, {"group": GATEWAY_GROUP, "kind": "GRPCRoute"})


def conditions(spec: tuple, generation: int, now: str) -> list[dict]:
    return [
        {"type": ctype, "status": status, "reason": reason, "message": message,
         "observedGeneration": generation, "lastTransitionTime": now}
        for ctype, status, reason, message in spec
    ]


def call(host: str, token: str, context: ssl.SSLContext, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"{host}{path}", data=data, method=GET if body is None else PATCH)
    request.add_header(AUTHORIZATION_HEADER, BEARER_PREFIX + token)
    if body is not None:
        request.add_header(CONTENT_TYPE_HEADER, MERGE_PATCH)
    try:
        with urllib.request.urlopen(request, context=context, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
            return json.loads(reply.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"gateway_status: {request.method} {path} answered {exc.code}: {exc.read().decode(errors='replace')}")


def access_token() -> str:
    try:
        done = subprocess.run(list(TOKEN_COMMAND), capture_output=True, text=True, timeout=TOKEN_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"gateway_status: {' '.join(TOKEN_COMMAND)} failed: {exc}")
    if done.returncode != 0 or not done.stdout.strip():
        raise SystemExit(f"gateway_status: {' '.join(TOKEN_COMMAND)} exited {done.returncode}: {done.stderr.strip()}")
    return done.stdout.strip()


def main() -> int:
    env = {name: os.environ.get(name, "") for name in (HOST_ENV, CA_ENV, NAMESPACE_ENV, GATEWAY_ENV, ROUTE_ENV, LISTENER_ENV, CLASS_ENV, CONTROLLER_ENV)}
    missing = sorted(name for name, value in env.items() if not value)
    if missing:
        sys.stderr.write(f"gateway_status: missing {', '.join(missing)}\n")
        return 1
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with tempfile.NamedTemporaryFile("wb", suffix=".crt") as ca:
        ca.write(base64.b64decode(env[CA_ENV]))
        ca.flush()
        context = ssl.create_default_context(cafile=ca.name)
        namespace = env[NAMESPACE_ENV]
        gateway_path = GATEWAY_API_PATH.format(namespace=namespace, resource=GATEWAYS, name=env[GATEWAY_ENV])
        route_path = GATEWAY_API_PATH.format(namespace=namespace, resource=HTTPROUTES, name=env[ROUTE_ENV])
        host, token = env[HOST_ENV], access_token()

        class_path = GATEWAY_CLASS_PATH.format(name=env[CLASS_ENV])
        gateway_class = call(host, token, context, class_path)
        call(host, token, context, class_path + STATUS_SUFFIX, {"status": {
            "conditions": conditions(CLASS_CONDITIONS, gateway_class["metadata"]["generation"], now),
        }})

        gateway = call(host, token, context, gateway_path)
        generation = gateway["metadata"]["generation"]
        call(host, token, context, gateway_path + STATUS_SUFFIX, {"status": {
            "conditions": conditions(GATEWAY_CONDITIONS, generation, now),
            "listeners": [{
                "name": env[LISTENER_ENV],
                "attachedRoutes": ATTACHED_ROUTES,
                "supportedKinds": list(SUPPORTED_KINDS),
                "conditions": conditions(LISTENER_CONDITIONS, generation, now),
            }],
        }})

        route = call(host, token, context, route_path)
        call(host, token, context, route_path + STATUS_SUFFIX, {"status": {"parents": [{
            "parentRef": {"group": GATEWAY_GROUP, "kind": GATEWAY_KIND, "name": env[GATEWAY_ENV]},
            "controllerName": env[CONTROLLER_ENV],
            "conditions": conditions(ROUTE_CONDITIONS, route["metadata"]["generation"], now),
        }]}})
    print(f"gateway_status: wrote the status of {env[CLASS_ENV]}, {namespace}/{env[GATEWAY_ENV]} and {namespace}/{env[ROUTE_ENV]} at {now}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
