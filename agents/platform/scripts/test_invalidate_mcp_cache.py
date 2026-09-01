import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from invalidate_mcp_cache import (
    CacheInvalidationError,
    invalidate_all_mcp_caches,
    invalidate_cache_file,
    is_local_mcp_server,
    main,
)


class InvalidateMcpCacheTest(unittest.TestCase):
    def test_is_local_mcp_server_known_servers(self):
        self.assertTrue(is_local_mcp_server("platform_control"))
        self.assertTrue(is_local_mcp_server("router"))

    def test_is_local_mcp_server_remote_proxy(self):
        remote_config = {
            "command": "node",
            "args": [
                "/opt/mcp-remote/dist/proxy.js",
                "https://developerknowledge.googleapis.com/mcp",
            ],
        }
        self.assertFalse(is_local_mcp_server("developer_knowledge", remote_config))

        remote_url_config = {
            "url": "https://mcp.example.com/sse",
            "transport": "sse",
        }
        self.assertFalse(is_local_mcp_server("custom_remote", remote_url_config))

    def test_is_local_mcp_server_local_script(self):
        local_config = {
            "command": "/opt/hermes/.venv/bin/python3",
            "args": ["${HERMES_HOME}/scripts/custom_server.py"],
        }
        self.assertTrue(is_local_mcp_server("custom_local", local_config))

    def test_invalidate_cache_file_removes_local_and_keeps_remote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "mcp_schema_cache.json"
            initial_data = {
                "platform_control": {
                    "fingerprint": "abc12345",
                    "tools": [{"name": "verify_gke_cluster"}],
                },
                "developer_knowledge": {
                    "fingerprint": "def67890",
                    "tools": [{"name": "search_docs"}],
                },
                "gke": {
                    "fingerprint": "ghi12345",
                    "tools": [{"name": "get_cluster"}],
                },
            }
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(initial_data, f)

            removed = invalidate_cache_file(cache_file)
            self.assertEqual(["platform_control"], removed)

            with open(cache_file, "r", encoding="utf-8") as f:
                updated_data = json.load(f)

            self.assertNotIn("platform_control", updated_data)
            self.assertIn("developer_knowledge", updated_data)
            self.assertIn("gke", updated_data)

    def test_invalidate_cache_file_non_existent(self):
        non_existent = Path("/tmp/non_existent_cache_dir_12345/mcp_schema_cache.json")
        self.assertEqual([], invalidate_cache_file(non_existent))

    def test_invalidate_cache_file_corrupted_json_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "mcp_schema_cache.json"
            cache_file.write_text("invalid-json{", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                invalidate_cache_file(cache_file)

    def test_invalidate_cache_file_non_dict_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "mcp_schema_cache.json"
            cache_file.write_text("[\"item\"]", encoding="utf-8")
            with self.assertRaises(ValueError):
                invalidate_cache_file(cache_file)

    def test_invalidate_all_mcp_caches_scans_root_and_profiles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            # 1. Root cache (holds router and gke)
            root_cache_dir = root / "cache"
            root_cache_dir.mkdir(parents=True)
            root_cache_file = root_cache_dir / "mcp_schema_cache.json"
            with open(root_cache_file, "w", encoding="utf-8") as f:
                json.dump({"router": {"fingerprint": "111"}, "gke": {"fingerprint": "222"}}, f)

            # 2. Platform profile cache (holds platform_control and developer_knowledge)
            platform_cache_dir = root / "profiles" / "platform" / "cache"
            platform_cache_dir.mkdir(parents=True)
            platform_cache_file = platform_cache_dir / "mcp_schema_cache.json"
            with open(platform_cache_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "platform_control": {"fingerprint": "333"},
                        "developer_knowledge": {"fingerprint": "444"},
                    },
                    f,
                )

            # 3. Cluster profile cache (holds only remote gke)
            cluster_cache_dir = root / "profiles" / "cluster-alpha" / "cache"
            cluster_cache_dir.mkdir(parents=True)
            cluster_cache_file = cluster_cache_dir / "mcp_schema_cache.json"
            with open(cluster_cache_file, "w", encoding="utf-8") as f:
                json.dump({"gke": {"fingerprint": "555"}}, f)

            results = invalidate_all_mcp_caches(root)

            self.assertEqual({"default": ["router"], "platform": ["platform_control"]}, results)

            # Verify contents
            with open(root_cache_file, "r", encoding="utf-8") as f:
                self.assertEqual({"gke": {"fingerprint": "222"}}, json.load(f))

            with open(platform_cache_file, "r", encoding="utf-8") as f:
                self.assertEqual({"developer_knowledge": {"fingerprint": "444"}}, json.load(f))

            with open(cluster_cache_file, "r", encoding="utf-8") as f:
                self.assertEqual({"gke": {"fingerprint": "555"}}, json.load(f))

    def test_invalidate_all_mcp_caches_corrupted_file_deletes_and_continues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            # 1. Corrupted root cache
            root_cache_dir = root / "cache"
            root_cache_dir.mkdir(parents=True)
            root_cache_file = root_cache_dir / "mcp_schema_cache.json"
            root_cache_file.write_text("corrupted-json{", encoding="utf-8")

            # 2. Healthy platform profile cache
            platform_cache_dir = root / "profiles" / "platform" / "cache"
            platform_cache_dir.mkdir(parents=True)
            platform_cache_file = platform_cache_dir / "mcp_schema_cache.json"
            with open(platform_cache_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "platform_control": {"fingerprint": "333"},
                        "developer_knowledge": {"fingerprint": "444"},
                    },
                    f,
                )

            err_stream = io.StringIO()
            with patch("sys.stderr", err_stream):
                with self.assertRaises(CacheInvalidationError) as ctx:
                    invalidate_all_mcp_caches(root)

            # Assert root cache was unlinked and stderr logged
            self.assertFalse(root_cache_file.exists())
            self.assertIn("unreadable cache", err_stream.getvalue())

            # Assert platform profile cache was still processed despite root cache error
            self.assertEqual({"platform": ["platform_control"]}, ctx.exception.results)
            with open(platform_cache_file, "r", encoding="utf-8") as f:
                self.assertEqual({"developer_knowledge": {"fingerprint": "444"}}, json.load(f))

    def test_main_cli(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_dir = root / "cache"
            cache_dir.mkdir(parents=True)
            cache_file = cache_dir / "mcp_schema_cache.json"
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"router": {"fingerprint": "111"}}, f)

            with patch.object(sys, "argv", ["invalidate_mcp_cache.py", str(root)]):
                main()

            with open(cache_file, "r", encoding="utf-8") as f:
                self.assertEqual({}, json.load(f))

    def test_main_cli_corrupted_cache_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_dir = root / "cache"
            cache_dir.mkdir(parents=True)
            cache_file = cache_dir / "mcp_schema_cache.json"
            cache_file.write_text("corrupted{", encoding="utf-8")

            err_stream = io.StringIO()
            with patch("sys.stderr", err_stream):
                with patch.object(sys, "argv", ["invalidate_mcp_cache.py", str(root)]):
                    with self.assertRaises(SystemExit) as cm:
                        main()
                    self.assertEqual(cm.exception.code, 1)

            self.assertFalse(cache_file.exists())


if __name__ == "__main__":
    unittest.main()
