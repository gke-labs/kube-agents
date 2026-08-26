#!/usr/bin/env python3
"""Open the Hermes dashboard of a running Platform Agent in a local browser.

    scripts/hermes-dashboard-tunnel.py       # then browse to 127.0.0.1:9119

The Service advertises a dashboard port, but nothing answers on it: `hermes
dashboard` binds 127.0.0.1:9119 inside the pod, so `9119 -> podIP:9119` routes
to a closed address. That bind is not incidental -- the dashboard's auth gate
keys on the bind host, so binding 0.0.0.0 switches authentication on, and with
no auth provider registered the server exits at startup instead of serving
unauthenticated. The loopback bind is what keeps the dashboard usable, so
reaching it means getting inside the pod's network namespace rather than
changing how it listens.

`kubectl port-forward` cannot do that on a GKE Sandbox (gVisor) node pool --
docs/site/src/content/docs/operator/platformagent-crd.md is canonical on why --
so this relays bytes through a `kubectl exec` channel instead. The relay, and
everything it has to get right, moved to `scripts/exec_tunnel.py` when the E2E
suite needed the same mechanism against the credential-proxy sidecar. What
stays here is the dashboard's access path: its defaults, and why its loopback
bind is deliberate.
"""

import argparse
import pathlib
import sys

# Python already puts a script's own directory on sys.path, so this is only
# needed under `python3 -P` or PYTHONSAFEPATH=1, which suppress that.
sys.path.append(str(pathlib.Path(__file__).parent))

from exec_tunnel import TunnelConfig, build_server  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", default="kubeagents-system")
    ap.add_argument("--selector", default="app=platform-agent-gateway")
    ap.add_argument("--pod", default="", help="pin a pod (default: by label)")
    ap.add_argument("--container", default="platform-agent-dashboard")
    ap.add_argument("--remote-port", type=int, default=9119)
    ap.add_argument("--local-port", type=int, default=9119)
    ap.add_argument("--python", default="/opt/hermes/.venv/bin/python3")
    args = ap.parse_args()

    cfg = TunnelConfig(
        namespace=args.namespace,
        selector=args.selector,
        pod=args.pod,
        container=args.container,
        remote_port=args.remote_port,
        python=args.python,
        # stderr, so the tunnel's commentary never lands in a pipe a caller is
        # reading data from.
        log=lambda message: print(message, file=sys.stderr, flush=True),
    )
    server = build_server(cfg, args.local_port)
    server.resolver.get()
    print(f"Hermes dashboard → http://127.0.0.1:{args.local_port}", flush=True)
    print(f"  via kubectl exec into -l {args.selector} "
          f"({args.namespace}), Ctrl-C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
