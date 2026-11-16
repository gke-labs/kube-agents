#!/usr/bin/env python3
"""Fail unless `kubectl get --raw` keeps the kubeconfig server's base path.

The sandbox's kubeconfigs point every context at the k8s-api-proxy with a
`/clusters/<context>` base path, and the proxy routes by that path alone. kubectl
before 1.34 drops the base path on `kubectl get --raw` (client-go's
`RequestURI` replaced it until v0.34), so a fleet read reached the proxy with no
cluster in it (#4739). The proxy now refuses such a request, but the sandbox's
kubectl has to send the right one. The image build runs this against the
binary it installs, so a kubectl that drops the path never ships.

It serves one HTTP listener on loopback, points `kubectl get --raw` at it with
a `/clusters/probe` base path, and checks the path the request arrived with.
Standard library only, and nothing leaves the host.

Usage: check_kubectl_base_path.py [KUBECTL]   (default: kubectl on PATH)
"""

from __future__ import annotations

import http.server
import subprocess
import sys
import threading

BASE = "/clusters/probe"
RAW = "/apis/metrics.k8s.io/v1beta1/nodes"


def received_path(kubectl: str) -> str:
    """The path one `kubectl get --raw` request arrived with."""
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen.append(self.path)
            body = b'{"kind":"NodeMetricsList","apiVersion":"metrics.k8s.io/v1beta1","items":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}{BASE}"
        result = subprocess.run(
            [kubectl, "--kubeconfig=/dev/null", f"--server={url}", "--token=probe", "get", "--raw", RAW],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        server.shutdown()
        server.server_close()
    if result.returncode != 0:
        raise SystemExit(f"kubectl get --raw failed ({result.returncode}): {result.stderr.strip()}")
    if len(seen) != 1:
        raise SystemExit(f"expected one request, saw {len(seen)}: {seen}")
    return seen[0]


def main(argv: list[str]) -> int:
    kubectl = argv[1] if len(argv) > 1 else "kubectl"
    path = received_path(kubectl)
    if path.split("?", 1)[0] != BASE + RAW:
        print(
            f"kubectl get --raw dropped the kubeconfig server's base path: the request arrived as "
            f"{path!r}, want {BASE + RAW!r}. kubectl before 1.34 does this (#4739); the k8s-api-proxy "
            f"refuses such a request, so every --raw read through it would fail.",
            file=sys.stderr,
        )
        return 1
    print(f"kubectl get --raw keeps the base path: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
