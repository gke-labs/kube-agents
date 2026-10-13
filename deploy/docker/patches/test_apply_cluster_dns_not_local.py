"""Unit tests for apply_cluster_dns_not_local.py against a miniature model_metadata.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The real file is checked in the shipped image by verify_cluster_dns_not_local.py;
these tests cover the applier's contract: one guard, right after the
``host is None`` check of ``is_local_endpoint``, nowhere else, and a refusal
when the anchor is missing, repeated or already patched.
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from apply_cluster_dns_not_local import LOCAL_DEF, MARKER, METADATA_RELATIVE, apply

# The shape of agent/model_metadata.py at v2026.9.14, cut to what the patch and
# its classification touch. A second def with the same guard proves the edit
# lands in is_local_endpoint alone.
METADATA_STUB = '''import ipaddress
from urllib.parse import urlparse

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
_CONTAINER_LOCAL_SUFFIXES = (".docker.internal", ".containers.internal", ".lima.internal")


def _parse_base_url(base_url):
    return urlparse(base_url if "://" in base_url else "http://" + base_url)


def other_host_check(base_url):
    host = _parse_base_url(base_url).hostname
    if host is None:
        return False
    return True


def is_local_endpoint(base_url: str) -> bool:
    """True for loopback, container-internal DNS, unqualified hosts, RFC-1918."""
    try:
        parsed = _parse_base_url(base_url)
        host = parsed.hostname or "" if parsed is not None else None
    except Exception:
        return False
    if host is None:
        return False
    if host in _LOCAL_HOSTS or host.endswith(_CONTAINER_LOCAL_SUFFIXES) or host.endswith(".local") or (host and "." not in host and ":" not in host):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False
'''

GATEWAY = "http://inference-gateway.kubeagents-system.svc.cluster.local/v1"


def _write(root: Path, source: str) -> Path:
    path = root / METADATA_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(source)
    return path


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("model_metadata_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ApplyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_service_dns_stops_being_local_and_nothing_else_moves(self):
        path = _write(self.root, METADATA_STUB)
        self.assertTrue(_load(path).is_local_endpoint(GATEWAY))
        apply(self.root)
        module = _load(path)
        for url in (GATEWAY, "http://gw.ns.svc/v1", "http://GW.NS.SVC.CLUSTER.LOCAL:8080/v1"):
            self.assertFalse(module.is_local_endpoint(url), url)
        for url in ("http://localhost:11434", "http://ollama:11434", "http://192.168.1.2:11434", "http://nas.local"):
            self.assertTrue(module.is_local_endpoint(url), url)

    def test_the_guard_lands_once_in_is_local_endpoint_only(self):
        path = _write(self.root, METADATA_STUB)
        apply(self.root)
        patched = path.read_text()
        self.assertEqual(patched.count(MARKER), 1)
        local_def = patched.index(f"def {LOCAL_DEF}(")
        self.assertGreater(patched.index(MARKER), local_def)
        self.assertLess(patched.index(MARKER), patched.index('host.endswith(".local")'))
        other = patched[patched.index("def other_host_check(") : local_def]
        self.assertNotIn(MARKER, other)

    def test_refuses_a_second_run(self):
        _write(self.root, METADATA_STUB)
        apply(self.root)
        with self.assertRaises(SystemExit):
            apply(self.root)

    def test_refuses_when_the_anchor_is_gone(self):
        _write(self.root, METADATA_STUB.replace("    if host is None:\n        return False\n    if host in", "    if host in"))
        with self.assertRaises(SystemExit):
            apply(self.root)


if __name__ == "__main__":
    sys.exit(unittest.main())
