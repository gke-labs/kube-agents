"""Tests for apply_no_telegram_lazy_install.py.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_apply_no_telegram_lazy_install.py'
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import unittest

import apply_no_telegram_lazy_install as applier

# The v2026.9.14 shape of tools/lazy_deps.py around the entry, reduced to
# something that runs on its own: the allowlist and the lookup ensure() does.
UPSTREAM = '''\
"""Lazy deps. A docstring may name "platform.telegram" without being the entry."""


class FeatureUnavailable(Exception):
    pass


LAZY_DEPS = {
    "memory.mem0": ("mem0ai==2.0.10",),

    # Messaging platforms (lazy-installable on demand)
    "platform.telegram": ("python-telegram-bot[webhooks]==22.8",),
    "platform.discord": ("discord.py[voice]==2.7.1",),
}


def ensure(feature):
    if feature not in LAZY_DEPS:
        raise FeatureUnavailable(feature)
    return LAZY_DEPS[feature]
'''


class ApplyNoTelegramLazyInstallTest(unittest.TestCase):
    def write_tree(self, source: str) -> tuple[pathlib.Path, pathlib.Path]:
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        target = root / "tools" / "lazy_deps.py"
        target.parent.mkdir(parents=True)
        target.write_text(source, encoding="utf-8")
        return root, target

    def load(self, target: pathlib.Path) -> dict:
        scope: dict = {}
        exec(compile(target.read_text(encoding="utf-8"), str(target), "exec"), scope)
        return scope

    def test_the_entry_is_gone_and_the_feature_is_refused(self):
        root, target = self.write_tree(UPSTREAM)
        applier.apply(root)
        scope = self.load(target)
        self.assertNotIn("platform.telegram", scope["LAZY_DEPS"])
        with self.assertRaises(scope["FeatureUnavailable"]):
            scope["ensure"]("platform.telegram")

    def test_the_other_entries_stay(self):
        root, target = self.write_tree(UPSTREAM)
        applier.apply(root)
        scope = self.load(target)
        self.assertEqual(scope["ensure"]("platform.discord"), ("discord.py[voice]==2.7.1",))
        self.assertEqual(scope["ensure"]("memory.mem0"), ("mem0ai==2.0.10",))

    def test_no_spec_for_the_package_is_left_in_the_allowlist(self):
        root, target = self.write_tree(UPSTREAM)
        applier.apply(root)
        specs = [spec for specs in self.load(target)["LAZY_DEPS"].values() for spec in specs]
        self.assertEqual([spec for spec in specs if "telegram" in spec], [])

    def test_a_changed_pin_fails_the_build(self):
        root, _ = self.write_tree(UPSTREAM.replace("==22.8", "==23.0"))
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("found 0", str(caught.exception))

    def test_the_patch_is_not_applied_twice(self):
        root, _ = self.write_tree(UPSTREAM)
        applier.apply(root)
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("already patched", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
