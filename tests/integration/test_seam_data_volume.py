"""Seam: a filling data volume → the Session KV server's check → the home channel,
and the same volume → the board health report.

The real `session_kv_server` runs as a subprocess and checks the volume once as
it starts, so each start is one check, and the session database it keeps across
starts holds where the crossing stands. The volume is the seam's far end: a
`sitecustomize` on PYTHONPATH answers `os.statvfs` for the agent home with the
percentage a file holds, counts the reads, and passes every other path to the
real call. The other end is a recording fake `hermes`, which takes the post as
Slack would. The board health report runs as a real subprocess on the same fake
volume, so the line it leads with is checked against the one the server posted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from _seams import SCRIPTS_DIR, KVServer, fake_executable, wait_until

# The fake volume's size; the percentages below are of this.
FAKE_VOLUME_BLOCKS = 1000
FAKE_VOLUME_BLOCK_BYTES = 10 * 1024 * 1024
# How long a check may take to read the volume after the server answers, and
# how long it then has to act on the reading before the server is stopped.
CHECK_READ_TIMEOUT_SECONDS = 30.0
CHECK_SETTLE_SECONDS = 2.0
REPORT_TIMEOUT_SECONDS = 120
SLACK_ON = "platforms:\n  slack:\n    enabled: true\n"
WARNING_PREFIX = "The agent's data volume is "
ALL_CLEAR_PREFIX = "The agent's data volume is back to "
STATE_ENV = "KANBAN_HEALTH_STATE"
# A board with one invariant finding, so the report has a finding for the
# volume line to lead.
GHOST_CLAIM_TASK = {"id": "t_ghost", "status": "ready", "claim_lock": "host:9"}
GHOST_CLAIM_FINDING = "t_ghost"

SITECUSTOMIZE = '''
import os
_real_statvfs = os.statvfs
_home = os.environ.get("PLATFORM_AGENT_HOME", "")
_percent_file = os.environ.get("SEAM_VOLUME_PERCENT_FILE", "")
_reads_file = os.environ.get("SEAM_VOLUME_READS_FILE", "")


class _Volume:
    def __init__(self, blocks, free, frsize):
        self.f_blocks, self.f_bavail, self.f_bfree = blocks, free, free
        self.f_frsize = self.f_bsize = frsize


def statvfs(path):
    if _home and _percent_file and os.path.realpath(os.fspath(path)) == os.path.realpath(_home):
        with open(_percent_file) as handle:
            used = int(handle.read())
        if _reads_file:
            with open(_reads_file, "a") as handle:
                handle.write("read\\n")
        blocks = {blocks}
        return _Volume(blocks, blocks * (100 - used) // 100, {frsize})
    return _real_statvfs(path)


os.statvfs = statvfs
'''.format(blocks=FAKE_VOLUME_BLOCKS, frsize=FAKE_VOLUME_BLOCK_BYTES)


class TestDataVolumeSignals(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "agent-home"
        self.home.mkdir()
        self.percent_file = self.tmp / "volume-percent"
        self.reads_file = self.tmp / "volume-reads"
        site = self.tmp / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
        managed = self.tmp / "managed"
        managed.mkdir()
        (managed / "config.yaml").write_text(SLACK_ON)
        self.record = self.tmp / "hermes-calls.jsonl"
        fake_executable(
            self.tmp / "bin",
            "hermes",
            f"""
            import json, sys
            with open({str(self.record)!r}, "a") as handle:
                handle.write(json.dumps(sys.argv[1:]) + "\\n")
            print(json.dumps({{"message_id": "m-1"}}))
            """,
        )
        self.env = {
            "PLATFORM_AGENT_HOME": str(self.home),
            "SEAM_VOLUME_PERCENT_FILE": str(self.percent_file),
            "SEAM_VOLUME_READS_FILE": str(self.reads_file),
            "PYTHONPATH": os.pathsep.join([str(site), str(SCRIPTS_DIR)]),
            "HERMES_MANAGED_DIR": str(managed),
        }

    def _lines(self, path: Path) -> list[str]:
        return path.read_text().splitlines() if path.exists() else []

    def _posts(self) -> list[str]:
        return [" ".join(json.loads(line)) for line in self._lines(self.record)]

    def _check_at(self, percent: int) -> list[str]:
        """Start the server on a volume `percent` used, let its first check run,
        and return every post so far."""
        self.percent_file.write_text(str(percent))
        reads = len(self._lines(self.reads_file))
        server = KVServer(self.tmp, env=self.env, path_prepend=str(self.tmp / "bin"))
        try:
            wait_until(
                lambda: len(self._lines(self.reads_file)) > reads,
                timeout=CHECK_READ_TIMEOUT_SECONDS,
                message=f"the data volume check at {percent}%",
            )
            # The reading is acted on at once; this covers a post's subprocess.
            time.sleep(CHECK_SETTLE_SECONDS)
        finally:
            server.stop()
        return self._posts()

    def _board_report(self, percent: int) -> str:
        """Build a board with one finding and run the board health report on the
        same fake volume; return its output."""
        self.percent_file.write_text(str(percent))
        guard = self.tmp / "report-bin"
        fake_executable(guard, "hermes", "import sys\nsys.exit(127)\n")
        env = dict(os.environ, **self.env)
        env["PATH"] = os.pathsep.join([str(guard), env.get("PATH", "")])
        build = textwrap.dedent(
            f"""
            from pathlib import Path
            from test_kanban_board_health import make_board
            make_board(Path({str(self.home)!r}), tasks=[{GHOST_CLAIM_TASK!r}])
            """
        )
        subprocess.run([sys.executable, "-c", build], check=True, env=env, cwd=str(SCRIPTS_DIR),
                       timeout=REPORT_TIMEOUT_SECONDS)
        env[STATE_ENV] = str(self.tmp / "board-state.json")
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "kanban_board_health.py")],
            env=env,
            cwd=str(SCRIPTS_DIR),
            capture_output=True,
            text=True,
            timeout=REPORT_TIMEOUT_SECONDS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_volume_that_fills_and_drains_says_so_once_each_way(self):
        self.assertEqual(self._check_at(50), [], "a half-full volume posts nothing")

        posts = self._check_at(95)
        self.assertEqual(len(posts), 1, posts)
        self.assertIn(WARNING_PREFIX + "95% full", posts[0])
        warning = posts[0][posts[0].index(WARNING_PREFIX):]

        # A restart above the line does not post again: where the crossing
        # stands is on the session database, which the next start reads.
        self.assertEqual(len(self._check_at(96)), 1)

        # The board health report on the same volume leads its finding with
        # the server's own line.
        report = self._board_report(95)
        self.assertIn(warning, report)
        self.assertLess(report.index(warning), report.index(GHOST_CLAIM_FINDING), report)

        posts = self._check_at(80)
        self.assertEqual(len(posts), 2, posts)
        self.assertIn(ALL_CLEAR_PREFIX + "80% full", posts[1])


if __name__ == "__main__":
    unittest.main()
