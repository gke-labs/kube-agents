"""Every Hermes profile config gives the gateway's provider its model-call deadlines.

The agent reaches its model through the custom provider in each profile's
config.yaml. Hermes reads `providers.<id>.stale_timeout_seconds` and
`request_timeout_seconds` for it (hermes_cli/timeouts.py); a profile without
them, or with the block under another key, falls back to Hermes' own defaults
(180s stale, 1800s per read). The configs carry the block separately, so this
holds them together.

The front door keeps a 600s stale bound because Gemini thinks in silence on
this path. The kanban workers (the Platform Agent and the Cluster Agents) use
120s, because they hardly think and a long silence there is a stalled call.
The Platform Agent's config is merged onto the shared defaults at image build
(deploy/docker/merge_configs.py), so its deadlines are read from the merge.
"""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SHARED_DEFAULTS = _REPO_ROOT / "deploy" / "shared" / "defaults" / "config.yaml"
_CHAT = _REPO_ROOT / "agents" / "chat" / "config.yaml"
_CLUSTER = _REPO_ROOT / "agents" / "cluster" / "config.yaml"
_TSE = _REPO_ROOT / "agents" / "tse" / "config.yaml"
_PLATFORM_OVERLAY = _REPO_ROOT / "agents" / "platform" / "config.yaml"
_MERGE_CONFIGS = _REPO_ROOT / "deploy" / "docker" / "merge_configs.py"

FRONT_DOOR_STALE_SECONDS = 600
WORKER_STALE_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 900


def _load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


def _merged_platform_config() -> dict:
    """The Platform Agent's config as the image build merges it."""
    spec = importlib.util.spec_from_file_location("merge_configs", _MERGE_CONFIGS)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.merge(_load(_SHARED_DEFAULTS), _load(_PLATFORM_OVERLAY))


def _deadlines(config: dict) -> dict:
    provider = config["model"]["provider"]
    if provider != "custom":
        raise AssertionError(f"model.provider is {provider!r}, want 'custom'")
    return config["providers"][provider]


class TestGatewayDeadlines(unittest.TestCase):
    def test_each_profile_sets_both_deadlines_on_its_model_provider(self) -> None:
        cases = (
            ("deploy/shared/defaults/config.yaml", _load(_SHARED_DEFAULTS), FRONT_DOOR_STALE_SECONDS),
            ("agents/chat/config.yaml (front door)", _load(_CHAT), FRONT_DOOR_STALE_SECONDS),
            ("agents/cluster/config.yaml (Cluster Agents)", _load(_CLUSTER), WORKER_STALE_SECONDS),
            ("agents/tse/config.yaml (GKE Support Agent)", _load(_TSE), WORKER_STALE_SECONDS),
            ("platform profile (shared defaults + agents/platform/config.yaml)", _merged_platform_config(), WORKER_STALE_SECONDS),
        )
        for name, config, stale in cases:
            with self.subTest(config=name):
                deadlines = _deadlines(config)
                self.assertEqual(deadlines["stale_timeout_seconds"], stale)
                self.assertEqual(deadlines["request_timeout_seconds"], REQUEST_TIMEOUT_SECONDS)

    def test_workers_fail_faster_than_the_front_door(self) -> None:
        front_door = _deadlines(_load(_CHAT))["stale_timeout_seconds"]
        for name, config in (("cluster", _load(_CLUSTER)), ("tse", _load(_TSE)), ("platform", _merged_platform_config())):
            with self.subTest(profile=name):
                worker = _deadlines(config)
                self.assertLess(worker["stale_timeout_seconds"], front_door)
                self.assertLess(worker["stale_timeout_seconds"], worker["request_timeout_seconds"])


if __name__ == "__main__":
    unittest.main()
