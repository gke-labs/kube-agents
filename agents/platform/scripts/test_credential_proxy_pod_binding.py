"""The credential broker holds a pod-bound token to the pod it was issued to.

PodAddressBinding reads the pod the TokenReview names and refuses any request
whose connection peer is not one of that pod's addresses. These tests drive
the binding directly (with the pod read stubbed at urlopen, so its error
handling runs for real) and through the handler over real sockets: a TCP
listener, which uses the socket's own peer and ignores every header, and the
Unix socket behind Envoy, which takes the address Envoy stamps.
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import yaml

import credential_proxy
from credential_proxy import (
    AuthenticationError,
    CredentialProxyHandler,
    PodAddressBinding,
    Principal,
)

PEER_HEADER_VALUE = "%DOWNSTREAM_DIRECT_REMOTE_ADDRESS_WITHOUT_PORT%"


def envoy_peer_header_gaps(config, header):
    """Every place an Envoy config can reach a Unix-socket cluster without stamping ``header``.

    A cluster is a socket cluster if any endpoint is a pipe. A listener reaches
    one through a tcp_proxy (never allowed: no header can be set) or through an
    HTTP connection manager whose inline route table has a route to it. Such a
    route table must set ``header`` to the downstream direct address with
    OVERWRITE_IF_EXISTS_OR_ADD at the route-table level, and no virtual host or
    route beneath it may add or remove the same header. Returns the gaps found,
    empty when there are none.
    """
    header = header.lower()
    resources = config.get("static_resources") or {}
    socket_clusters = set()
    for cluster in resources.get("clusters") or []:
        for locality in (cluster.get("load_assignment") or {}).get("endpoints") or []:
            for endpoint in locality.get("lb_endpoints") or []:
                if "pipe" in ((endpoint.get("endpoint") or {}).get("address") or {}):
                    socket_clusters.add(cluster["name"])
    gaps = []
    if not socket_clusters:
        gaps.append("no cluster reaches a Unix socket; the check would prove nothing")

    def names_header(options):
        return [o for o in options or [] if str((o.get("header") or {}).get("key", "")).lower() == header]

    def removes_header(level):
        return header in [str(h).lower() for h in level.get("request_headers_to_remove") or []]

    for listener in resources.get("listeners") or []:
        lname = listener.get("name", "?")
        for chain in listener.get("filter_chains") or []:
            for flt in chain.get("filters") or []:
                cfg = flt.get("typed_config") or {}
                kind = cfg.get("@type", "")
                if kind.endswith("TcpProxy"):
                    if cfg.get("cluster") in socket_clusters or "weighted_clusters" in cfg:
                        gaps.append(f"{lname}: a tcp_proxy reaches the socket, where no header can be set")
                    continue
                if not kind.endswith("HttpConnectionManager"):
                    continue
                if "rds" in cfg or "scoped_routes" in cfg:
                    gaps.append(f"{lname}: routes not inline, so the header cannot be checked")
                    continue
                table = cfg.get("route_config") or {}
                forwards = False
                for vhost in table.get("virtual_hosts") or []:
                    if names_header(vhost.get("request_headers_to_add")) or removes_header(vhost):
                        gaps.append(f"{lname}/{vhost.get('name')}: a virtual host changes {header}")
                    for route in vhost.get("routes") or []:
                        action = route.get("route") or {}
                        target = action.get("cluster")
                        if target in socket_clusters or "weighted_clusters" in action or "cluster_header" in action:
                            forwards = True
                        if names_header(route.get("request_headers_to_add")) or removes_header(route):
                            gaps.append(f"{lname}/{vhost.get('name')}: a route changes {header}")
                if not forwards:
                    continue
                stamped = names_header(table.get("request_headers_to_add"))
                good = [
                    o for o in stamped
                    if o["header"].get("value") == PEER_HEADER_VALUE
                    and o.get("append_action") == "OVERWRITE_IF_EXISTS_OR_ADD"
                ]
                if len(stamped) != 1 or len(good) != 1:
                    gaps.append(f"{lname}: forwards to the socket without overwriting {header} with the direct peer")
                if removes_header(table):
                    gaps.append(f"{lname}: the route table removes {header}")
    return gaps


NAMESPACE = "kubeagents-system-profiles"
CALLER = f"system:serviceaccount:{NAMESPACE}:profile-worker"
POD = "profile-worker-7f9c-abcde"
POD_UID = "0b5c7e0e-4c1d-4f3c-9c55-1f0f2a3b4c5d"


def pod_object(ips=("10.8.0.12",), uid=POD_UID, name=POD, namespace=NAMESPACE, phase="Running", pod_ip=None):
    status = {"phase": phase}
    if ips is not None:
        status["podIPs"] = [{"ip": ip} for ip in ips]
    if pod_ip is not None:
        status["podIP"] = pod_ip
    return {"metadata": {"name": name, "namespace": namespace, "uid": uid}, "status": status}


def principal(**overrides):
    values = dict(workload=CALLER, uid="sa-uid", role=credential_proxy.CALLER_ROLE_PROFILE, pod=POD, pod_uid=POD_UID)
    values.update(overrides)
    return Principal(**values)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


class FakeApiServer:
    """Stands in for urlopen: answers pod reads from a script, recording each."""

    def __init__(self):
        self.answers = []
        self.requests = []

    def push(self, answer):
        self.answers.append(answer)

    def __call__(self, request, timeout=None, context=None):  # noqa: ARG002
        self.requests.append((request.get_method(), request.full_url, request.get_header("Authorization")))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "refused", {}, io.BytesIO(b"{}"))
        if isinstance(answer, BaseException):
            raise answer
        return _Response(json.dumps(answer).encode())


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class PodAddressBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.own_token = Path(self.tmp.name) / "token"
        self.own_token.write_text("broker-own-token", encoding="utf-8")
        self.api = FakeApiServer()
        patcher = mock.patch.object(credential_proxy.urllib.request, "urlopen", self.api)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()

    def binding(self, cache_seconds=30.0):
        return PodAddressBinding(
            api_host="10.0.0.1", api_port="443", ca_file="", token_file=str(self.own_token),
            cache_seconds=cache_seconds, clock=self.clock,
        )

    def test_a_peer_at_the_pods_address_passes(self):
        self.api.push(pod_object())
        self.binding().check(principal(), "10.8.0.12")
        method, url, authorization = self.api.requests[0]
        self.assertEqual("GET", method)
        self.assertEqual(f"https://10.0.0.1:443/api/v1/namespaces/{NAMESPACE}/pods/{POD}", url)
        # The broker's own credential, as the TokenReview uses: never the caller's.
        self.assertEqual("Bearer broker-own-token", authorization)

    def test_a_different_peer_with_a_valid_token_is_refused(self):
        self.api.push(pod_object())
        with self.assertRaisesRegex(AuthenticationError, "did not come from the token's pod"):
            self.binding().check(principal(), "10.8.0.99")

    def test_a_pod_that_is_gone_is_refused(self):
        self.api.push(404)
        with self.assertRaisesRegex(AuthenticationError, "does not exist"):
            self.binding().check(principal(), "10.8.0.12")

    def test_an_api_server_error_is_refused(self):
        for answer in (500, urllib.error.URLError("connection refused"), TimeoutError(), ValueError("bad json")):
            with self.subTest(answer=answer):
                api = FakeApiServer()
                api.push(answer)
                with mock.patch.object(credential_proxy.urllib.request, "urlopen", api):
                    with self.assertRaises(AuthenticationError):
                        self.binding().check(principal(), "10.8.0.12")

    def test_an_unreadable_answer_is_refused(self):
        for answer in ([], {"metadata": {}}, {"status": {}}, {"metadata": "x", "status": {}}):
            with self.subTest(answer=answer):
                api = FakeApiServer()
                api.push(answer)
                with mock.patch.object(credential_proxy.urllib.request, "urlopen", api):
                    with self.assertRaises(AuthenticationError):
                        self.binding().check(principal(), "10.8.0.12")

    def test_a_missing_permission_is_refused_and_said_once_a_minute_per_namespace(self):
        self.api.push(403)
        binding = self.binding()
        with self.assertLogs(credential_proxy.LOGGER, level="ERROR") as logs:
            for _ in range(3):
                with self.assertRaisesRegex(AuthenticationError, "may not read pods"):
                    binding.check(principal(), "10.8.0.12")
            self.clock.now += credential_proxy.POD_ADDRESS_PERMISSION_LOG_SECONDS + 1
            with self.assertRaises(AuthenticationError):
                binding.check(principal(), "10.8.0.12")
        self.assertEqual(2, len(logs.records), [r.getMessage() for r in logs.records])
        line = logs.records[0].getMessage()
        self.assertIn("get on pods", line)
        self.assertIn(NAMESPACE, line)
        self.assertIn("HTTP 403", line)
        # Every refusal went back to the API server: none was cached.
        self.assertEqual(4, len(self.api.requests))

    def test_a_uid_mismatch_is_refused(self):
        # The same name, a new pod: the token was issued to the one before.
        self.api.push(pod_object(uid="ffffffff-0000-0000-0000-000000000000"))
        with self.assertRaisesRegex(AuthenticationError, "UID differs"):
            self.binding().check(principal(), "10.8.0.12")

    def test_another_pod_in_the_answer_is_refused(self):
        self.api.push(pod_object(name="someone-else"))
        with self.assertRaises(AuthenticationError):
            self.binding().check(principal(), "10.8.0.12")

    def test_a_pod_with_no_address_is_refused(self):
        for pod in (pod_object(ips=()), pod_object(ips=None), pod_object(ips=("not-an-ip",))):
            with self.subTest(pod=pod):
                api = FakeApiServer()
                api.push(pod)
                with mock.patch.object(credential_proxy.urllib.request, "urlopen", api):
                    with self.assertRaisesRegex(AuthenticationError, "has no address"):
                        self.binding().check(principal(), "10.8.0.12")

    def test_a_stopped_pod_is_refused(self):
        for phase in ("Succeeded", "Failed"):
            with self.subTest(phase=phase):
                api = FakeApiServer()
                api.push(pod_object(phase=phase))
                with mock.patch.object(credential_proxy.urllib.request, "urlopen", api):
                    with self.assertRaisesRegex(AuthenticationError, "stopped"):
                        self.binding().check(principal(), "10.8.0.12")

    def test_pod_ip_is_the_fallback_when_pod_ips_is_absent(self):
        self.api.push(pod_object(ips=None, pod_ip="10.8.0.12"))
        self.binding().check(principal(), "10.8.0.12")

    def test_dual_stack_either_family_matches(self):
        self.api.push(pod_object(ips=("10.8.0.12", "fd00:10:8::12")))
        binding = self.binding()
        binding.check(principal(), "10.8.0.12")
        binding.check(principal(), "fd00:10:8::12")
        # The same address spelled differently is the same address.
        binding.check(principal(), "fd00:10:8:0:0:0:0:12")
        with self.assertRaises(AuthenticationError):
            binding.check(principal(), "fd00:10:8::13")

    def test_an_ipv4_mapped_peer_matches_the_ipv4_address(self):
        self.api.push(pod_object(ips=("10.8.0.12",)))
        binding = self.binding()
        binding.check(principal(), "::ffff:10.8.0.12")
        with self.assertRaises(AuthenticationError):
            binding.check(principal(), "::ffff:10.8.0.13")

    def test_an_ipv4_mapped_pod_address_matches_an_ipv4_peer(self):
        self.api.push(pod_object(ips=("::ffff:10.8.0.12",)))
        self.binding().check(principal(), "10.8.0.12")

    def test_an_unknown_peer_is_refused_without_a_read(self):
        self.api.push(pod_object())
        for peer in (None, "", "envoy", "10.8.0.12:443"):
            with self.subTest(peer=peer):
                with self.assertRaisesRegex(AuthenticationError, "peer address is unknown"):
                    self.binding().check(principal(), peer)
        self.assertEqual([], self.api.requests)

    def test_a_principal_with_no_pod_is_refused_without_a_read(self):
        self.api.push(pod_object())
        for p in (principal(pod=""), principal(pod_uid=""), principal(workload="alice@example.com"),
                  principal(pod="../../secrets/x")):
            with self.subTest(principal=p):
                with self.assertRaises(AuthenticationError):
                    self.binding().check(p, "10.8.0.12")
        self.assertEqual([], self.api.requests)

    def test_the_lookup_is_cached_and_re_read_once_it_expires(self):
        self.api.push(pod_object(ips=("10.8.0.12",)))
        binding = self.binding(cache_seconds=30.0)
        binding.check(principal(), "10.8.0.12")
        self.clock.now += 29
        binding.check(principal(), "10.8.0.12")
        self.assertEqual(1, len(self.api.requests))
        # Expired: read again, and the new answer is the one enforced.
        self.clock.now += 2
        self.api.answers = [pod_object(ips=("10.8.0.40",))]
        with self.assertRaises(AuthenticationError):
            binding.check(principal(), "10.8.0.12")
        binding.check(principal(), "10.8.0.40")
        self.assertEqual(2, len(self.api.requests))

    def test_an_expired_pass_that_now_fails_is_refused(self):
        self.api.push(pod_object())
        binding = self.binding(cache_seconds=30.0)
        binding.check(principal(), "10.8.0.12")
        self.clock.now += 31
        self.api.answers = [404]
        with self.assertRaises(AuthenticationError):
            binding.check(principal(), "10.8.0.12")

    def test_a_refusal_is_not_cached_as_a_pass(self):
        self.api.answers = [404, pod_object()]
        binding = self.binding(cache_seconds=30.0)
        with self.assertRaises(AuthenticationError):
            binding.check(principal(), "10.8.0.12")
        binding.check(principal(), "10.8.0.12")
        self.assertEqual(2, len(self.api.requests))
        # A UID mismatch is not kept either, so the cache never holds an
        # entry under the token's UID for a pod that is not its.
        self.api.answers = [pod_object(uid="other")]
        with self.assertRaises(AuthenticationError):
            binding.check(principal(pod_uid="third"), "10.8.0.12")
        self.assertNotIn((NAMESPACE, POD, "third"), binding._cache)

    def test_the_cache_is_keyed_by_uid(self):
        self.api.push(pod_object())
        binding = self.binding()
        binding.check(principal(), "10.8.0.12")
        # A recreated pod of the same name has a new UID, and is read afresh.
        self.api.answers = [pod_object(uid="new-uid", ips=("10.8.0.77",))]
        binding.check(principal(pod_uid="new-uid"), "10.8.0.77")
        self.assertEqual(2, len(self.api.requests))

    def test_no_cache_reads_every_time(self):
        self.api.push(pod_object())
        binding = self.binding(cache_seconds=0.0)
        binding.check(principal(), "10.8.0.12")
        binding.check(principal(), "10.8.0.12")
        self.assertEqual(2, len(self.api.requests))

    def test_the_cache_is_never_longer_than_the_token_cache(self):
        binding = self.binding(cache_seconds=30.0)
        credential_proxy.ServiceAccountAuthenticator(
            audience_roles={"a": ""}, allowed_callers=frozenset({CALLER}), api_host="10.0.0.1",
            api_port="443", ca_file="", token_file=str(self.own_token), cache_seconds=5.0,
            pod_binding=binding,
        )
        self.assertEqual(5.0, binding.cache_seconds)
        binding = self.binding(cache_seconds=30.0)
        credential_proxy.ServiceAccountAuthenticator(
            audience_roles={"a": ""}, allowed_callers=frozenset({CALLER}), api_host="10.0.0.1",
            api_port="443", ca_file="", token_file=str(self.own_token), cache_seconds=60.0,
            pod_binding=binding,
        )
        self.assertEqual(30.0, binding.cache_seconds)

    def test_an_unreadable_own_token_is_refused(self):
        self.api.push(pod_object())
        binding = PodAddressBinding(api_host="10.0.0.1", api_port="443", ca_file="", token_file="/nonexistent/token")
        with self.assertRaisesRegex(AuthenticationError, "unreadable"):
            binding.check(principal(), "10.8.0.12")


class ConcurrentLookupsTest(unittest.TestCase):
    """A burst of requests for one pod makes one read."""

    def binding(self, read):
        binding = PodAddressBinding(api_host="10.0.0.1", api_port="443", ca_file="", token_file="/nonexistent",
                                    cache_seconds=30.0, timeout_seconds=5.0)
        binding._read_pod = read
        return binding

    def burst(self, binding, count=8):
        results = [None] * count

        def call(i):
            try:
                binding.check(principal(), "10.8.0.12")
                results[i] = "pass"
            except AuthenticationError as exc:
                results[i] = str(exc)

        threads = [threading.Thread(target=call, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        return threads, results

    def test_a_burst_shares_one_read(self):
        release, started, reads = threading.Event(), threading.Event(), []

        def read(namespace, name):
            reads.append(name)
            started.set()
            release.wait(5)
            return pod_object()

        binding = self.binding(read)
        threads, results = self.burst(binding)
        self.assertTrue(started.wait(5))
        # Let the others reach the wait before the read returns.
        for _ in range(100):
            with binding._lock:
                if binding._inflight:
                    break
        release.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(["pass"] * len(results), results)
        self.assertEqual(1, len(reads))
        self.assertEqual({}, binding._inflight)

    def test_a_shared_refusal_refuses_every_waiter_and_is_not_kept(self):
        release, reads = threading.Event(), []

        def read(namespace, name):
            reads.append(name)
            release.wait(5)
            raise AuthenticationError("the token's pod default/x does not exist")

        binding = self.binding(read)
        threads, results = self.burst(binding)
        release.set()
        for thread in threads:
            thread.join(10)
        self.assertTrue(all(r and "does not exist" in r for r in results), results)
        self.assertLess(len(reads), len(results))
        self.assertEqual({}, binding._inflight)
        self.assertEqual({}, binding._cache)
        # The next request reads again.
        binding._read_pod = lambda namespace, name: pod_object()
        binding.check(principal(), "10.8.0.12")

    def test_an_unexpected_failure_is_a_refusal(self):
        def read(namespace, name):
            raise RuntimeError("boom")

        binding = self.binding(read)
        with self.assertRaises(AuthenticationError):
            binding.check(principal(), "10.8.0.12")
        self.assertEqual({}, binding._inflight)

    def test_the_read_timeout_is_never_longer_than_the_token_reviews(self):
        binding = PodAddressBinding(api_host="10.0.0.1", api_port="443", ca_file="", token_file="/t")
        credential_proxy.ServiceAccountAuthenticator(
            audience_roles={"a": ""}, allowed_callers=frozenset({CALLER}), api_host="10.0.0.1",
            api_port="443", ca_file="", token_file="/t", timeout_seconds=4.0, pod_binding=binding,
        )
        self.assertEqual(4.0, binding.timeout_seconds)


class _FakePeerSocket:
    def __init__(self, pid, uid, gid=0):
        # struct ucred's layout: a signed pid, then an unsigned uid and gid.
        self.raw = (
            pid.to_bytes(4, sys.byteorder, signed=True)
            + uid.to_bytes(4, sys.byteorder, signed=False)
            + gid.to_bytes(4, sys.byteorder, signed=False)
        )

    def getsockopt(self, level, option, size):
        return self.raw[:size]


class EnvoyPeerCheckTest(unittest.TestCase):
    """Only the Envoy whose pid start-services handed over may speak for a Unix-socket peer."""

    ENVOY_PID = 4242

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.proc = root / "proc"
        self.envoy_binary = root / "envoy"
        self.envoy_binary.write_text("")
        self.other_binary = root / "git"
        self.other_binary.write_text("")
        patcher = mock.patch.object(credential_proxy.socket, "SO_PEERCRED", 17, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def process(self, pid, binary):
        entry = self.proc / str(pid)
        entry.mkdir(parents=True)
        (entry / "exe").symlink_to(binary)

    def check(self, pid=ENVOY_PID):
        return credential_proxy.EnvoyPeerCheck(pid, binary=str(self.envoy_binary), proc_root=str(self.proc))

    def test_the_handed_over_envoy_is_admitted(self):
        self.process(self.ENVOY_PID, self.envoy_binary)
        self.assertTrue(self.check().admits(_FakePeerSocket(self.ENVOY_PID, os.getuid())))

    def test_another_process_running_the_envoy_binary_is_refused(self):
        self.process(self.ENVOY_PID, self.envoy_binary)
        self.process(5000, self.envoy_binary)
        self.assertFalse(self.check().admits(_FakePeerSocket(5000, os.getuid())))

    def test_the_handed_over_pid_running_another_binary_is_refused(self):
        self.process(self.ENVOY_PID, self.other_binary)
        self.assertFalse(self.check().admits(_FakePeerSocket(self.ENVOY_PID, os.getuid())))

    def test_another_uid_or_an_unreadable_process_is_refused(self):
        self.process(self.ENVOY_PID, self.envoy_binary)
        self.assertFalse(self.check().admits(_FakePeerSocket(self.ENVOY_PID, (os.getuid() + 1) % 2**32)))
        self.assertFalse(self.check(pid=7777).admits(_FakePeerSocket(7777, os.getuid())))

    def test_no_pin_refuses_everything_and_says_so(self):
        self.process(self.ENVOY_PID, self.envoy_binary)
        for raw in (None, "", "0", "-3", "4242x", "4242,5000"):
            with self.subTest(raw=raw):
                env = {} if raw is None else {credential_proxy.ENVOY_PID_ENV: raw}
                with self.assertLogs(credential_proxy.LOGGER, level="ERROR") as logs:
                    check = credential_proxy.EnvoyPeerCheck.from_env(env)
                self.assertIsNone(check.pid)
                self.assertIn("ALERT", logs.records[0].getMessage())
                self.assertIn(credential_proxy.ENVOY_PID_ENV, logs.records[0].getMessage())
                self.assertFalse(check.admits(_FakePeerSocket(self.ENVOY_PID, os.getuid())))
        self.assertEqual(4242, credential_proxy.EnvoyPeerCheck.from_env({credential_proxy.ENVOY_PID_ENV: "4242"}).pid)
        # The handler's own default, before serve() builds the check, refuses too.
        self.assertIsNone(CredentialProxyHandler.__dict__["envoy_peer"].pid)

    def test_a_uid_past_two_to_the_31_is_read_unsigned(self):
        self.process(self.ENVOY_PID, self.envoy_binary)
        large = 2**31 + 12345
        self.assertEqual(
            (self.ENVOY_PID, large, 2**32 - 2),
            credential_proxy._socket_peer_credentials(_FakePeerSocket(self.ENVOY_PID, large, 2**32 - 2)),
        )
        with mock.patch.object(credential_proxy.os, "getuid", return_value=large):
            self.assertTrue(self.check().admits(_FakePeerSocket(self.ENVOY_PID, large)))

    def test_no_peer_credentials_is_refused(self):
        self.process(self.ENVOY_PID, self.envoy_binary)
        check = self.check()
        with mock.patch.object(credential_proxy.socket, "SO_PEERCRED", None):
            self.assertFalse(check.admits(_FakePeerSocket(self.ENVOY_PID, os.getuid())))
        self.assertFalse(check.admits(None))

        class Broken:
            def getsockopt(self, *args):
                raise OSError("not a socket")

        self.assertFalse(check.admits(Broken()))


@unittest.skipUnless(sys.platform.startswith("linux"), "SO_PEERCRED and /proc are Linux's")
class EnvoyPeerCheckOnARealSocketTest(unittest.TestCase):
    """Real processes, real peer credentials: the pinned stand-in passes, a second one from the same path does not."""

    CONNECT = (
        "import socket, sys, time\n"
        "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "s.connect(sys.argv[1])\n"
        "sys.stdout.write('connected\\n'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # A stand-in for /usr/local/bin/envoy: the interpreter, under that
        # name. /proc/<pid>/exe and the check both resolve the link.
        self.stand_in = os.path.join(self.tmp.name, "envoy")
        os.symlink(os.path.realpath(sys.executable), self.stand_in)
        self.path = os.path.join(self.tmp.name, "backend.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.server.close)
        self.server.bind(self.path)
        self.server.listen(4)
        self.server.settimeout(10)

    def connect_from_child(self):
        import subprocess

        child = subprocess.Popen([self.stand_in, "-I", "-c", self.CONNECT, self.path], stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual("connected\n", child.stdout.readline())
        accepted, _ = self.server.accept()
        self.addCleanup(accepted.close)
        return child, accepted

    def test_only_the_pinned_stand_in_is_admitted(self):
        pinned, pinned_connection = self.connect_from_child()
        other, other_connection = self.connect_from_child()
        check = credential_proxy.EnvoyPeerCheck(pinned.pid, binary=self.stand_in)
        self.assertTrue(check.admits(pinned_connection))
        # Same executable, same uid, another pid: a brokered command that
        # started the Envoy binary itself.
        self.assertFalse(check.admits(other_connection))

    def test_this_process_is_refused(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.connect(self.path)
        accepted, _ = self.server.accept()
        self.addCleanup(accepted.close)
        self.assertFalse(credential_proxy.EnvoyPeerCheck(os.getpid() + 100000, binary=self.stand_in).admits(accepted))


class BuildAuthenticatorWiresTheBindingTest(unittest.TestCase):
    def test_the_serviceaccount_mode_always_binds(self):
        env = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": CALLER,
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "KUBERNETES_SERVICE_PORT": "6443",
            "CREDENTIAL_PROXY_KUBE_CA_FILE": "/ca.crt",
            "CREDENTIAL_PROXY_KUBE_TOKEN_FILE": "/token",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            authenticator = credential_proxy.build_authenticator()
        binding = authenticator.pod_binding
        self.assertIsInstance(binding, PodAddressBinding)
        # The same API server and the same credential as the TokenReview.
        self.assertEqual(
            (authenticator.api_host, authenticator.api_port, authenticator.ca_file, authenticator.token_file),
            (binding.api_host, binding.api_port, binding.ca_file, binding.token_file),
        )
        self.assertEqual(("10.0.0.1", "6443", "/ca.crt", "/token"), (binding.api_host, binding.api_port, binding.ca_file, binding.token_file))
        self.assertLessEqual(binding.cache_seconds, authenticator.cache_seconds)


ENVOY_CONFIG = Path(__file__).resolve().parents[3] / "deploy" / "shared" / "envoy-credential-proxy.yaml"


class EnvoyStampsThePeerOnEveryPathToTheSocketTest(unittest.TestCase):
    """Every listener and route that reaches the broker's Unix socket overwrites the peer header."""

    def config(self):
        return yaml.safe_load(ENVOY_CONFIG.read_text(encoding="utf-8"))

    def test_the_shipped_config_has_no_gap(self):
        config = self.config()
        self.assertEqual([], envoy_peer_header_gaps(config, credential_proxy.POD_PEER_ADDRESS_HEADER))
        # Both listeners reach the socket, the health one included.
        names = {listener["name"] for listener in config["static_resources"]["listeners"]}
        self.assertEqual({"credential_proxy", "credential_health"}, names)

    def _listener_table(self, config, name):
        for listener in config["static_resources"]["listeners"]:
            if listener["name"] == name:
                return listener["filter_chains"][0]["filters"][0]["typed_config"]["route_config"]
        self.fail(f"no listener {name}")

    def test_a_listener_without_the_overwrite_is_a_gap(self):
        for name in ("credential_proxy", "credential_health"):
            with self.subTest(listener=name):
                config = self.config()
                del self._listener_table(config, name)["request_headers_to_add"]
                gaps = envoy_peer_header_gaps(config, credential_proxy.POD_PEER_ADDRESS_HEADER)
                self.assertTrue(any(name in gap for gap in gaps), gaps)

    def test_an_append_or_another_value_is_a_gap(self):
        for field, value in (("append_action", "APPEND_IF_EXISTS_OR_ADD"), ("value", "%DOWNSTREAM_REMOTE_ADDRESS_WITHOUT_PORT%")):
            with self.subTest(field=field):
                config = self.config()
                option = self._listener_table(config, "credential_health")["request_headers_to_add"][0]
                (option if field == "append_action" else option["header"])[field] = value
                self.assertNotEqual([], envoy_peer_header_gaps(config, credential_proxy.POD_PEER_ADDRESS_HEADER))

    def test_a_route_or_virtual_host_that_changes_the_header_is_a_gap(self):
        config = self.config()
        vhost = self._listener_table(config, "credential_proxy")["virtual_hosts"][0]
        vhost["routes"][0]["request_headers_to_remove"] = [credential_proxy.POD_PEER_ADDRESS_HEADER]
        self.assertNotEqual([], envoy_peer_header_gaps(config, credential_proxy.POD_PEER_ADDRESS_HEADER))
        config = self.config()
        vhost = self._listener_table(config, "credential_proxy")["virtual_hosts"][0]
        vhost["request_headers_to_add"] = [{"header": {"key": credential_proxy.POD_PEER_ADDRESS_HEADER, "value": "1.2.3.4"}}]
        self.assertNotEqual([], envoy_peer_header_gaps(config, credential_proxy.POD_PEER_ADDRESS_HEADER))

    def test_a_tcp_proxy_to_the_socket_is_a_gap(self):
        config = self.config()
        config["static_resources"]["listeners"].append({
            "name": "raw",
            "filter_chains": [{"filters": [{"name": "envoy.filters.network.tcp_proxy", "typed_config": {
                "@type": "type.googleapis.com/envoy.extensions.filters.network.tcp_proxy.v3.TcpProxy",
                "cluster": "credential_runtime"}}]}],
        })
        self.assertTrue(any("raw" in gap for gap in envoy_peer_header_gaps(config, credential_proxy.POD_PEER_ADDRESS_HEADER)))


class _HandlerHarness:
    """A CredentialProxyHandler whose TokenReview and pod read are stubbed.

    Each token names a role. The pod read answers ``self.pod_ips``. The
    handler's own role check is replaced by a recorder that answers 418, so
    reaching it proves the request authenticated and nothing past it runs.
    """

    ROLE_TOKENS = {f"{role}-token": role for role in credential_proxy.CALLER_ROLES}

    def install(self, test):
        self.test = test
        self.reached = []
        self.pod_ips = ("127.0.0.1",)
        self.reads = 0
        self.role_permits = CredentialProxyHandler._role_permits
        for attribute in ("authenticator", "profile_gate", "_role_permits", "max_request_bytes", "envoy_peer"):
            test.addCleanup(
                self._restore, attribute, attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )
        harness = self
        binding = PodAddressBinding(api_host="10.0.0.1", api_port="443", ca_file="", token_file="/nonexistent",
                                    cache_seconds=0.0)

        def read_pod(namespace, name):
            harness.reads += 1
            return pod_object(ips=harness.pod_ips, namespace=namespace, name=name)

        binding._read_pod = read_pod
        authenticator = credential_proxy.ServiceAccountAuthenticator(
            audience_roles={"aud": ""}, allowed_callers=frozenset({CALLER}), api_host="10.0.0.1",
            api_port="443", ca_file="", token_file="/nonexistent", cache_seconds=0.0, pod_binding=binding,
        )

        def fake_review(token):
            if token not in harness.ROLE_TOKENS:
                raise AuthenticationError("not our token")
            return principal(role=harness.ROLE_TOKENS[token])

        authenticator._review = fake_review
        CredentialProxyHandler.authenticator = authenticator
        CredentialProxyHandler.max_request_bytes = 1 << 20

        class RecordingGate:
            def admit(self, p, token):
                harness.reached.append(("gate", token))
                return p

        CredentialProxyHandler.profile_gate = RecordingGate()

        class AdmitEnvoy:
            # The kernel's peer credentials are Linux's; EnvoyPeerCheckTest
            # covers the real check.
            admitted = True

            def admits(self, connection):
                return self.admitted

        self.envoy = AdmitEnvoy()
        CredentialProxyHandler.envoy_peer = self.envoy

        def role_permits(handler, p):
            harness.reached.append((handler.command, handler.path, p.role))
            handler._json(418, {"status": "authenticated"})
            return False

        CredentialProxyHandler._role_permits = role_permits

    @staticmethod
    def _restore(attribute, was_set, original):
        if was_set:
            setattr(CredentialProxyHandler, attribute, original)
        elif attribute in CredentialProxyHandler.__dict__:
            delattr(CredentialProxyHandler, attribute)


def _route_path(prefix):
    return prefix if not prefix.endswith("/") else prefix + "x"


class EveryRouteEnforcesTheBindingTest(unittest.TestCase):
    """Every route that takes a pod token goes through _authenticated, and so through the binding."""

    def setUp(self):
        self.harness = _HandlerHarness()
        self.harness.install(self)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, token, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(connection.close)
        sent = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        sent.update(headers or {})
        body = b"{}" if method in ("POST", "PUT") else None
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        response.read()
        return response.status

    def routes(self):
        for prefix, roles in credential_proxy.ROUTE_ROLES:
            for role in roles:
                for method in ("GET", "POST"):
                    yield method, _route_path(prefix), role
        for role in (credential_proxy.CALLER_ROLE_SHELL, credential_proxy.CALLER_ROLE_PROFILE):
            for method in ("PUT", "DELETE"):
                yield method, credential_proxy.AUDIT_REPORTS_PREFIX + "/x", role

    def test_every_route_refuses_a_peer_that_is_not_the_tokens_pod(self):
        self.harness.pod_ips = ("10.8.0.12",)
        for method, path, role in self.routes():
            with self.subTest(method=method, path=path, role=role):
                self.assertEqual(401, self.request(method, path, f"{role}-token"))
        self.assertEqual([], self.harness.reached, "a refused request must reach nothing past authentication")

    def test_every_route_admits_the_tokens_own_pod(self):
        self.harness.pod_ips = ("127.0.0.1",)
        expected = []
        for method, path, role in self.routes():
            with self.subTest(method=method, path=path, role=role):
                self.assertEqual(418, self.request(method, path, f"{role}-token"))
                expected.append((method, path, role))
        self.assertEqual(expected, self.harness.reached)

    def test_the_route_table_is_covered(self):
        # The pod-token routes, by family: a new family lands in ROUTE_ROLES
        # and so in the two tests above without any change here.
        families = {prefix for prefix, _ in credential_proxy.ROUTE_ROLES}
        for family in ("/v1/exec", "/v1/vcs/", credential_proxy.AUDIT_REPORTS_PREFIX, "/v1/mcp/"):
            self.assertIn(family, families)

    def test_a_peer_header_is_ignored_on_a_tcp_listener(self):
        self.harness.pod_ips = ("10.8.0.12",)
        status = self.request("POST", "/v1/exec", "profile-token",
                              headers={credential_proxy.POD_PEER_ADDRESS_HEADER: "10.8.0.12",
                                       "X-Forwarded-For": "10.8.0.12"})
        self.assertEqual(401, status)

    def test_the_task_credential_is_not_read_from_a_foreign_peer(self):
        self.harness.pod_ips = ("10.8.0.12",)
        status = self.request("POST", "/v1/exec", "profile-token",
                              headers={credential_proxy.TASK_CREDENTIAL_HEADER: "a-valid-credential"})
        self.assertEqual(401, status)
        self.assertEqual([], self.harness.reached)

    def test_healthz_needs_no_token_and_reads_no_pod(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", credential_proxy.HEALTHZ_PATH)
        self.assertEqual(200, connection.getresponse().status)
        self.assertEqual(0, self.harness.reads)

    def test_a_refusal_answers_like_a_failed_token(self):
        self.harness.pod_ips = ("10.8.0.12",)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(connection.close)
        with self.assertLogs(credential_proxy.LOGGER, level=logging.WARNING) as logs:
            connection.request("POST", "/v1/exec", body=b"{}", headers={"Authorization": "Bearer profile-token"})
            response = connection.getresponse()
            payload = json.loads(response.read())
        self.assertEqual(401, response.status)
        self.assertEqual({"error": "caller could not be authenticated"}, payload)
        self.assertTrue(any("did not come from the token's pod" in r.getMessage() for r in logs.records))

    def test_the_real_role_table_still_runs_after_the_binding(self):
        # Without the recorder: a matching peer whose role the route refuses
        # gets the role table's 403, not a pass.
        CredentialProxyHandler._role_permits = self.harness.role_permits
        self.harness.pod_ips = ("127.0.0.1",)
        self.assertEqual(403, self.request("POST", "/v1/github/refresh", "profile-token"))


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(self.socket_path)


class UnixSocketBehindEnvoyTest(unittest.TestCase):
    """The broker Pod's shape: the peer is the address Envoy stamps, exactly once."""

    def setUp(self):
        self.harness = _HandlerHarness()
        self.harness.install(self)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.socket_path = os.path.join(self.directory.name, "backend.sock")
        self.server = credential_proxy.ThreadingUnixHTTPServer(self.socket_path, CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.harness.pod_ips = ("10.8.0.12", "fd00:10:8::12")

    def request(self, peers):
        connection = _UnixConnection(self.socket_path)
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/v1/exec")
        connection.putheader("Authorization", "Bearer profile-token")
        # No body: the answer comes before any body would be read, and a
        # body sent after it would meet a closed socket.
        connection.putheader("Content-Length", "0")
        for peer in peers:
            connection.putheader(credential_proxy.POD_PEER_ADDRESS_HEADER, peer)
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        return response.status

    def test_the_stamped_address_of_the_tokens_pod_passes(self):
        self.assertEqual(418, self.request(["10.8.0.12"]))
        self.assertEqual(418, self.request(["fd00:10:8::12"]))
        self.assertEqual(418, self.request(["::ffff:10.8.0.12"]))

    def test_another_stamped_address_is_refused(self):
        self.assertEqual(401, self.request(["10.8.0.13"]))

    def test_no_stamped_address_is_refused(self):
        self.assertEqual(401, self.request([]))

    def test_more_than_one_stamped_address_is_refused(self):
        self.assertEqual(401, self.request(["10.8.0.12", "10.8.0.13"]))
        self.assertEqual(401, self.request(["10.8.0.12", "10.8.0.12"]))

    def test_a_stamped_value_that_is_not_one_bare_address_is_refused(self):
        for value in ("10.8.0.12:443", "10.8.0.12, 10.8.0.13", "10.8.0.12 10.8.0.13", "[fd00:10:8::12]",
                      "[fd00:10:8::12]:443", "10.8.0.12/32", "", "unknown"):
            with self.subTest(value=value):
                self.assertEqual(401, self.request([value]))
        self.assertEqual([], self.harness.reached)

    def test_a_connection_that_is_not_envoys_is_refused_whatever_it_stamps(self):
        self.harness.envoy.admitted = False
        self.assertEqual(401, self.request(["10.8.0.12"]))
        self.assertEqual([], self.harness.reached)


if __name__ == "__main__":
    unittest.main()
