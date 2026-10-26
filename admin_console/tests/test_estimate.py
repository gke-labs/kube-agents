from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from admin_console import estimate
from admin_console import harness_limits as hl

MIB = 1024**2


def _record(factor: str, level: float, megabytes: float, clusters: int, run: int = 1) -> dict:
    return {
        "key": f"aaaaaaaa|next|{factor}|{level:g}|watcher_soak|{run}",
        "commit": "a" * 40,
        "mode": "next",
        "phase": "phase2",
        "factor": factor,
        "level": level,
        "operation": "watcher_soak",
        "run": run,
        "started": "2026-10-27T01:00:00Z",
        "ended": "2026-10-27T02:00:00Z",
        "values": {**hl.PLAN_BASE_LEVELS, "clusters": clusters},
        "totals": {"clusters": clusters},
        "limits": {"platform-agent": {"memory_bytes": 1000 * MIB, "cpu_cores": 1.0}},
        "metrics": [
            {"name": hl.MEMORY_PEAK, "container": "platform-agent", "value": megabytes * MIB},
            {"name": hl.PROXY_COMMANDS, "value": 60.0 * clusters},
        ],
    }


class EstimateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.data = Path(self.directory.name) / "harness-limits.json"
        self.data.write_text(
            json.dumps({"schema": 1, "records": [_record("base", 0, 500, 2), _record("clusters", 8, 800, 8)]})
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_main(self, *args: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(estimate.main(["--data", str(self.data), *args]), 0)
        return out.getvalue()

    def test_the_report_names_the_breaking_point_and_the_proxy_demand(self):
        text = self.run_main()
        self.assertIn("| Memory peak · platform-agent | 1,000 MiB (platform-agent limit) | 500 MiB | 12.0 clusters |", text)
        self.assertIn("| watcher_soak | base | 2 | 1 | 2.0 | 1.0 | — | — |", text)
        self.assertIn("| watcher_soak | Clusters 8 | 8 | 1 | 8.0 | 1.0 | — | — |", text)
        # 8 clusters: 800 MiB predicted, 1,040 MiB with the 30% margin.
        self.assertIn("| platform-agent | 1040Mi · — |", text)
        self.assertIn("| Node pool | 1 × e2-standard-2 |", text)

    def test_limits_from_a_file_replace_the_measured_ones(self):
        limits = Path(self.directory.name) / "requests.yaml"
        limits.write_text(f"platform-agent:\n  memory_bytes: {700 * MIB}\n")
        text = self.run_main("--limits", str(limits))
        self.assertIn("| 700 MiB (platform-agent limit) | 500 MiB | 6.0 clusters |", text)

    def test_requests_replace_the_limits_when_asked(self):
        self.assertEqual(estimate.main(["--data", str(self.data), "--against", "requests"]), 1)
        data = json.loads(self.data.read_text())
        data["records"][-1]["requests"] = {"platform-agent": {"memory_bytes": 600 * MIB}}
        self.data.write_text(json.dumps(data))
        text = self.run_main("--against", "requests")
        self.assertIn("against the latest run's requests", text)
        self.assertIn("| 600 MiB (platform-agent limit) | 500 MiB | 4.0 clusters |", text)

    def test_an_unknown_commit_or_mode_is_an_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(estimate.main(["--data", str(self.data), "--commit", "deadbeef"]), 2)
            self.assertEqual(estimate.main(["--data", str(self.data), "--mode", "today"]), 2)
