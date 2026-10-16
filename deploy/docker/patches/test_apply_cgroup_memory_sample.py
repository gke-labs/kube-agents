"""Unit tests for apply_cgroup_memory_sample.py against a miniature lifecycle_ledger.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The real file is checked in the shipped image by verify_cgroup_memory_sample.py;
these tests cover the applier's contract (one insert in sample_memory, before
its return, and a refusal when the anchor is missing, repeated or already
patched) and the helper's arithmetic against a fixture cgroup.
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from apply_cgroup_memory_sample import LEDGER_RELATIVE, MARKER, apply

# The shape of gateway/lifecycle_ledger.py at v2026.9.14, cut to what the patch touches.
LEDGER_STUB = '''from __future__ import annotations

import os
from typing import Any, Dict


def _proc_fields(path: str, wanted: Dict[str, str]) -> Dict[str, int]:
    found: Dict[str, int] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                key = line.split(":", 1)[0]
                if key in wanted:
                    found[wanted[key]] = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return {}
    return found


def sample_memory() -> Dict[str, Any]:
    """Cheap /proc snapshot."""
    sample = {"rss_kib": 1}
    sample.update({"mem_total_kib": 32 * 1024 * 1024, "mem_available_kib": 3 * 1024 * 1024})
    return sample


def other() -> Dict[str, Any]:
    sample = {}
    return sample
'''

GIB = 1024**3


def _write_ledger(root: Path, source: str) -> Path:
    path = root / LEDGER_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(source)
    return path


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("ledger_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cgroup(directory: Path, limit: str, current: int, inactive: int, active: int = 0, slab: int = 0) -> None:
    (directory / "memory.max").write_text(limit + "\n")
    (directory / "memory.current").write_text(f"{current}\n")
    (directory / "memory.stat").write_text(
        f"anon 1\nfile 9\nactive_file {active}\ninactive_file {inactive}\nshmem 7\nslab_reclaimable {slab}\n"
    )


class ApplyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "hermes"
        self.cgroup = Path(tmp.name) / "cgroup"
        self.cgroup.mkdir()

    def patched(self):
        path = _write_ledger(self.root, LEDGER_STUB)
        apply(self.root)
        module = _load(path)
        module._KUBE_CGROUP_DIR = str(self.cgroup)
        return path, module

    def test_a_limit_replaces_the_node_figures(self):
        _, module = self.patched()
        _cgroup(self.cgroup, str(8 * GIB), int(0.7 * GIB), int(0.1 * GIB))
        sample = module.sample_memory()
        self.assertEqual(sample["mem_total_kib"], 8 * 1024 * 1024)
        self.assertEqual(sample["mem_available_kib"], int(8 * GIB - 0.6 * GIB) // 1024)
        self.assertEqual(sample["rss_kib"], 1)

    def test_active_cache_and_reclaimable_slab_count_as_available(self):
        _, module = self.patched()
        _cgroup(self.cgroup, str(8 * GIB), 7 * GIB, inactive=GIB, active=2 * GIB, slab=GIB)
        self.assertEqual(module.sample_memory()["mem_available_kib"], 5 * GIB // 1024)

    def test_swap_comes_from_the_cgroup_when_it_has_it(self):
        _, module = self.patched()
        _cgroup(self.cgroup, str(8 * GIB), GIB, 0)
        (self.cgroup / "memory.swap.current").write_text(f"{GIB}\n")
        self.assertEqual(module.sample_memory()["swap_used_kib"], GIB // 1024)

    def test_95_percent_held_leaves_5_percent_available(self):
        _, module = self.patched()
        _cgroup(self.cgroup, str(8 * GIB), int(0.95 * 8 * GIB), 0)
        sample = module.sample_memory()
        self.assertAlmostEqual(sample["mem_available_kib"] / sample["mem_total_kib"], 0.05, places=3)

    def test_no_limit_or_no_cgroup_keeps_the_node_figures(self):
        _, module = self.patched()
        _cgroup(self.cgroup, "max", GIB, 0)
        self.assertEqual(module.sample_memory()["mem_total_kib"], 32 * 1024 * 1024)
        module._KUBE_CGROUP_DIR = str(self.cgroup / "absent")
        self.assertEqual(module.sample_memory()["mem_available_kib"], 3 * 1024 * 1024)

    def test_the_insert_lands_once_before_sample_memorys_return(self):
        path, _ = self.patched()
        patched = path.read_text()
        self.assertEqual(patched.count("sample.update(_kube_cgroup_memory())"), 1)
        sample_def = patched.index("def sample_memory(")
        insert = patched.index("sample.update(_kube_cgroup_memory())")
        self.assertLess(sample_def, insert)
        self.assertLess(insert, patched.index("    return sample", insert))
        self.assertNotIn("_kube_cgroup_memory()", patched[patched.index("def other("):patched.index(f"\n# {MARKER}\n")])

    def test_refuses_a_second_run(self):
        self.patched()
        with self.assertRaises(SystemExit):
            apply(self.root)

    def test_refuses_when_the_return_repeats(self):
        twice = LEDGER_STUB.replace(
            "    return sample\n\n\ndef other",
            "    if sample:\n        pass\n    return sample\n    return sample\n\n\ndef other",
        )
        _write_ledger(self.root, twice)
        with self.assertRaises(SystemExit):
            apply(self.root)

    def test_a_longer_name_after_return_is_not_the_anchor(self):
        renamed = LEDGER_STUB.replace("    return sample\n\n\ndef other", "    return samples\n\n\ndef other")
        _write_ledger(self.root, renamed)
        with self.assertRaises(SystemExit):
            apply(self.root)

    def test_refuses_when_the_return_is_gone(self):
        gone = LEDGER_STUB.replace(
            '    sample.update({"mem_total_kib": 32 * 1024 * 1024, "mem_available_kib": 3 * 1024 * 1024})\n    return sample\n',
            '    return {"rss_kib": 1}\n',
        )
        _write_ledger(self.root, gone)
        with self.assertRaises(SystemExit):
            apply(self.root)


if __name__ == "__main__":
    sys.exit(unittest.main())
