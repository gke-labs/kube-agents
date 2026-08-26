"""Relay a local TCP port to a pod-loopback listener through `kubectl exec`.

This module is the canonical home for that mechanism and for why it is needed;
`scripts/hermes-dashboard-tunnel.py` and the `port_forward_agent` fixture in
`tests/e2e/conftest.py` both use it rather than restating it.

`kubectl port-forward` is the obvious way to reach a listener bound to
127.0.0.1 inside a pod, and on an ordinary node pool it works: the kubelet sets
the forward up inside the pod's own network namespace. On a GKE Sandbox
(gVisor) node pool it does not. The forward is established in the host-side CNI
netns while the listener lives in the sandbox's own network stack, so every
port on the pod refuses the connection --
`docs/site/src/content/docs/operator/platformagent-crd.md` is canonical on that
constraint. `kubectl exec` does enter the sandbox, so this relays bytes through
an exec channel instead.

Five things this has to get right, each of which broke it once:

1. Under `bufsize=0` the subprocess pipes are raw FileIO. There is no
   `.read1()` -- only `.read()`.
2. Raw pipe `write()` may consume only part of the buffer and return a short
   count. Discarding that count silently dropped 32-64KB chunks mid-stream and
   truncated the SPA's 1.9MB entrypoint, so the module would not parse.
3. The pod name changes on every redeploy. Pinning one produces a tunnel that
   accepts connections and returns zero bytes (ERR_EMPTY_RESPONSE) the moment
   the Deployment rolls. The pod is resolved by label and re-resolved whenever
   an exec fails to come up.
4. A terminated or killed child has to be waited on, and its pipes closed.
   Without the reap the kubectl process becomes a zombie holding its end of the
   exec stream, and since one connection is one exec, a caller that opens
   several accumulates both zombies and descriptors.
5. Resolution failures are not all empty output. A missing kubectl raises
   OSError and an unreachable control plane raises TimeoutExpired, and a caller
   that only expects RuntimeError gets a bare traceback instead of the message
   naming the selector.
"""

import dataclasses
import json
import socket
import socketserver
import subprocess
import threading
from typing import Any, Callable, List, Optional, Set

# The remote end writes READY (one NUL) as soon as it has actually connected to
# the target port, before relaying anything. That single byte is what lets the
# local side distinguish "container reachable and listener up" from a stale pod
# name, without having to buffer and replay the client's request.
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


@dataclasses.dataclass
class TunnelConfig:
    """Where to exec, and what to connect to once inside."""

    namespace: str = "kubeagents-system"
    selector: str = "app=platform-agent-gateway"
    pod: str = ""
    container: str = "platform-agent-dashboard"
    remote_port: int = 9119
    python: str = "/opt/hermes/.venv/bin/python3"
    # Bounds the wait for the remote end's READY byte. Without it a `kubectl
    # exec` that connects but never returns -- an unschedulable pod, an API
    # server that accepts the upgrade and stalls -- hangs the handler thread
    # and leaks the child for the life of the process.
    ready_timeout: float = 30.0
    # A file object for kubectl's stderr. None inherits, which is what an
    # interactive caller wants and a test harness does not.
    stderr: Any = None
    # Diagnostics go here. Callers that print should print to stderr: stdout is
    # where a piped caller expects data, not commentary.
    log: Callable[[str], None] = lambda message: None


def write_all(fileobj, buf) -> None:
    """Write every byte of buf, tolerating short writes on a raw pipe."""
    mv = memoryview(buf)
    while mv:
        n = fileobj.write(mv)
        if not n:
            continue
        mv = mv[n:]
    fileobj.flush()


def reap(proc: Optional[subprocess.Popen], timeout: float = 5.0) -> None:
    """Terminate a child, wait for it, and close its pipes.

    The wait is what stops a caller that tears tunnels down in a loop from
    accumulating zombies; closing the pipes is what stops it running out of
    descriptors, since one connection is one exec here.

    Callers must not have a thread still writing to `proc.stdin` when they call
    this -- `handle` joins its uploader first. Closing a descriptor another
    thread is mid-write on risks that thread writing into whatever the fd
    number gets reused for.
    """
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except Exception:  # noqa: BLE001 - a closed or in-flight pipe is not an error here
            pass


class PodResolver:
    """Cache the pod name; re-resolve by label when an exec fails to start."""

    def __init__(self, cfg: TunnelConfig):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.pod = cfg.pod

    def _list(self) -> List[dict]:
        """Every Running pod matching the selector, or RuntimeError saying why not.

        Every failure leaves by the same door. `kubectl` missing raises
        OSError, a control plane behind a VPN raises TimeoutExpired, and a
        cluster with no such pod returns success and an empty list -- three
        very different causes that a caller can only sensibly treat one way,
        and one of which used to escape as a bare traceback.
        """
        try:
            out = subprocess.run(
                ["kubectl", "get", "pods", "-n", self.cfg.namespace,
                 "-l", self.cfg.selector,
                 "--field-selector=status.phase=Running",
                 "-o", "json"],
                capture_output=True, text=True, timeout=60,
            )
        except OSError as exc:
            raise RuntimeError(f"could not run kubectl: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"kubectl get pods -l {self.cfg.selector} timed out after 60s"
            ) from exc
        if out.returncode != 0:
            raise RuntimeError(
                f"kubectl get pods -l {self.cfg.selector} failed: "
                f"{out.stderr.strip() or 'no stderr'}"
            )
        try:
            return json.loads(out.stdout).get("items", []) or []
        except ValueError as exc:
            raise RuntimeError(f"kubectl returned unparseable JSON: {exc}") from exc

    def get(self, refresh: bool = False) -> str:
        with self.lock:
            if self.pod and not refresh:
                return self.pod
            items = self._list()
            if not items:
                raise RuntimeError(
                    f"no Running pod matches -l {self.cfg.selector} "
                    f"in {self.cfg.namespace}"
                )
            # Ready first. Running only means the containers started; a pod
            # still inside its startup probe is Running and not yet serving,
            # and the agent's startup probe sanctions a cold boot of minutes.
            name = next(
                (
                    pod["metadata"]["name"]
                    for pod in items
                    if any(
                        condition.get("type") == "Ready" and condition.get("status") == "True"
                        for condition in pod.get("status", {}).get("conditions", [])
                    )
                ),
                items[0]["metadata"]["name"],
            )
            if name != self.pod:
                self.cfg.log(f"  pod → {name}")
            self.pod = name
            return name


class Handler(socketserver.BaseRequestHandler):
    def _spawn(self, pod: str) -> Optional[subprocess.Popen]:
        """Start an exec relay; return it only once it reports READY."""
        cfg = self.server.cfg
        cmd = [
            "kubectl", "exec", "-i",
            "-n", cfg.namespace, pod,
            "-c", cfg.container,
            "--", cfg.python, "-u", "-c",
            REMOTE_RELAY.format(port=cfg.remote_port),
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=cfg.stderr, bufsize=0,
            )
        except OSError as exc:
            cfg.log(f"  exec failed: {exc}")
            return None
        # read(1) on a raw pipe has no timeout of its own, so the deadline is a
        # timer that kills the child; the read then returns b"" and falls into
        # the rejection path below.
        fired = threading.Event()

        def expire():
            fired.set()
            proc.kill()

        watchdog = threading.Timer(cfg.ready_timeout, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            ready = proc.stdout.read(1)
        finally:
            watchdog.cancel()
        # cancel() is a no-op once the timer thread has entered its callback, so
        # a READY arriving in that window would otherwise hand back a relay that
        # is already being killed. The flag is set before the kill, so it is
        # true for the whole of that window -- poll() alone is not, since the
        # process may not have died yet when it is read.
        if ready != b"\x00" or fired.is_set() or proc.poll() is not None:
            reap(proc)
            return None
        if not self.server.track(proc):
            # The server was torn down between Popen and here, so nothing will
            # come back for this one.
            reap(proc)
            return None
        return proc

    def handle(self) -> None:
        resolver = self.server.resolver
        cfg = self.server.cfg
        proc = None
        for refresh in (False, True):
            try:
                pod = resolver.get(refresh=refresh)
            except RuntimeError as exc:
                cfg.log(f"  {exc}")
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

        uploader = threading.Thread(target=to_pod, daemon=True)
        uploader.start()
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
            # Before reap, so the uploader is not mid-write on a descriptor
            # reap is about to close. What ends it is the client closing its
            # end: the shutdown(SHUT_WR) above does not interrupt a recv()
            # blocked in another thread, so for a peer that holds the socket
            # open the 5s timeout is the normal exit path rather than a
            # backstop. Shortening it reopens the descriptor race.
            uploader.join(timeout=5.0)
            self.server.untrack(proc)
            reap(proc)


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        # Handlers are daemon threads, so server_close() does not join them: a
        # probe the caller abandoned leaves one waiting on its own exec. Track
        # the children so close_relays() can end the exec sessions even when
        # the threads outlive the caller.
        self._relays: Set[subprocess.Popen] = set()
        self._relay_lock = threading.Lock()
        self._closing = False
        super().__init__(*args, **kwargs)

    def track(self, proc: subprocess.Popen) -> bool:
        """Register a live relay. False once close_relays() has run.

        The return value is what closes the window between a handler getting
        past Popen and reaching here: without it, a relay registered after
        close_relays() has taken its snapshot is never reaped, which is the
        leak close_relays() exists to prevent.
        """
        with self._relay_lock:
            if self._closing:
                return False
            self._relays.add(proc)
            return True

    def untrack(self, proc: subprocess.Popen) -> None:
        with self._relay_lock:
            self._relays.discard(proc)

    def close_relays(self) -> None:
        """Reap every exec session this server still holds. Idempotent."""
        with self._relay_lock:
            self._closing = True
            outstanding = list(self._relays)
            self._relays.clear()
        for proc in outstanding:
            reap(proc)


def build_server(cfg: TunnelConfig, local_port: int, host: str = "127.0.0.1") -> Server:
    """Bind the local listener. Pass local_port=0 for an ephemeral port.

    The caller reads the bound port back from `server.server_address[1]`, which
    is what a test wants: a fixed port turns two concurrent runs into an
    `address already in use` that reads like a broken tunnel.
    """
    server = Server((host, local_port), Handler)
    server.cfg = cfg
    server.resolver = PodResolver(cfg)
    return server


def serve_background(cfg: TunnelConfig, local_port: int = 0, host: str = "127.0.0.1") -> Server:
    """Bind and serve on a daemon thread.

    The caller owns teardown: shutdown(), then close_relays(), then
    server_close().
    """
    server = build_server(cfg, local_port, host)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
