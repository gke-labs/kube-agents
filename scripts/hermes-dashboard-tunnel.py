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

`kubectl port-forward` cannot do that on a GKE Sandbox (gVisor) node pool: the
kubelet sets the forward up in the host-side CNI netns, while the listener
lives in the sandbox's own network stack, so every port on the pod refuses the
connection. `kubectl exec` does enter the sandbox correctly, so this relays
bytes through an exec channel instead.

Three things this has to get right, each of which broke it once:

1. Under `bufsize=0` the subprocess pipes are raw FileIO. There is no
   `.read1()` -- only `.read()`.
2. Raw pipe `write()` may consume only part of the buffer and return a short
   count. Discarding that count silently dropped 32-64KB chunks mid-stream and
   truncated the SPA's 1.9MB entrypoint, so the module would not parse.
3. The pod name changes on every redeploy. Pinning one produces a tunnel that
   accepts connections and returns zero bytes (ERR_EMPTY_RESPONSE) the moment
   the Deployment rolls. The pod is resolved by label and re-resolved whenever
   an exec fails to come up.
"""

import argparse
import socket
import socketserver
import subprocess
import sys
import threading

# The remote end writes READY (one NUL) as soon as it has actually connected to
# the dashboard, before relaying anything. That single byte is what lets the
# local side distinguish "container reachable and dashboard listening" from a
# stale pod name, without having to buffer and replay the client's request.
REMOTE_RELAY = """
import os, sys, socket, threading, time

def write_all(fd, buf):
    mv = memoryview(buf)
    while mv:
        mv = mv[os.write(fd, mv):]

s = socket.create_connection(("127.0.0.1", {port}))
os.write(1, b"\\x00")

def up():
    try:
        while True:
            d = os.read(0, 65536)
            if not d:
                break
            s.sendall(d)
    except Exception:
        pass
    try:
        s.shutdown(socket.SHUT_WR)
    except Exception:
        pass

threading.Thread(target=up, daemon=True).start()
try:
    while True:
        d = s.recv(65536)
        if not d:
            break
        write_all(1, d)
except Exception:
    pass
finally:
    # kubectl tears down the stdout stream when this process exits; give the
    # final frames a moment to ship before we go away.
    time.sleep(0.25)
"""


def write_all(fileobj, buf):
    """Write every byte of buf, tolerating short writes on a raw pipe."""
    mv = memoryview(buf)
    while mv:
        n = fileobj.write(mv)
        if not n:
            continue
        mv = mv[n:]
    fileobj.flush()


class PodResolver:
    """Cache the pod name; re-resolve by label when an exec fails to start."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.pod = cfg.pod

    def get(self, refresh=False):
        with self.lock:
            if self.pod and not refresh:
                return self.pod
            out = subprocess.run(
                ["kubectl", "get", "pods", "-n", self.cfg.namespace,
                 "-l", self.cfg.selector,
                 "--field-selector=status.phase=Running",
                 "-o", "jsonpath={.items[0].metadata.name}"],
                capture_output=True, text=True, timeout=60,
            )
            name = out.stdout.strip()
            if not name:
                raise RuntimeError(
                    f"no Running pod matches -l {self.cfg.selector} "
                    f"in {self.cfg.namespace}"
                )
            if name != self.pod:
                print(f"  pod → {name}", flush=True)
            self.pod = name
            return name


class Handler(socketserver.BaseRequestHandler):
    def _spawn(self, pod):
        """Start an exec relay; return it only once it reports READY."""
        cmd = [
            "kubectl", "exec", "-i",
            "-n", self.server.cfg.namespace, pod,
            "-c", self.server.cfg.container,
            "--", self.server.cfg.python, "-u", "-c",
            REMOTE_RELAY.format(port=self.server.cfg.remote_port),
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=None, bufsize=0,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  exec failed: {exc}", file=sys.stderr, flush=True)
            return None
        if proc.stdout.read(1) != b"\x00":
            proc.terminate()
            return None
        return proc

    def handle(self):
        resolver = self.server.resolver
        proc = None
        for refresh in (False, True):
            try:
                pod = resolver.get(refresh=refresh)
            except Exception as exc:  # noqa: BLE001
                print(f"  {exc}", file=sys.stderr, flush=True)
                return
            proc = self._spawn(pod)
            if proc:
                break
        if not proc:
            return

        def to_pod():
            try:
                while True:
                    data = self.request.recv(65536)
                    if not data:
                        break
                    write_all(proc.stdin, data)
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=to_pod, daemon=True).start()
        try:
            while True:
                # raw FileIO: .read(n) is one read() syscall, no .read1()
                data = proc.stdout.read(65536)
                if not data:
                    break
                self.request.sendall(data)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                self.request.shutdown(socket.SHUT_WR)
            except Exception:  # noqa: BLE001
                pass
            proc.terminate()


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", default="kubeagents-system")
    ap.add_argument("--selector", default="app=platform-agent-gateway")
    ap.add_argument("--pod", default="", help="pin a pod (default: by label)")
    ap.add_argument("--container", default="platform-agent-dashboard")
    ap.add_argument("--remote-port", type=int, default=9119)
    ap.add_argument("--local-port", type=int, default=9119)
    ap.add_argument("--python", default="/opt/hermes/.venv/bin/python3")
    cfg = ap.parse_args()

    resolver = PodResolver(cfg)
    resolver.get()

    server = Server(("127.0.0.1", cfg.local_port), Handler)
    server.cfg = cfg
    server.resolver = resolver
    print(f"Hermes dashboard → http://127.0.0.1:{cfg.local_port}", flush=True)
    print(f"  via kubectl exec into -l {cfg.selector} "
          f"({cfg.namespace}), Ctrl-C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
