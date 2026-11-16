import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import script_log


class AppendTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        env = mock.patch.dict(os.environ, {"PLATFORM_AGENT_HOME": home.name})
        env.start()
        self.addCleanup(env.stop)

    def test_each_line_is_one_timestamped_record_under_the_agent_homes_logs(self):
        script_log.append("watch.log", ["first", "second\nwith a newline"])
        script_log.append("watch.log", ["third"])
        records = (self.home / "logs" / "watch.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual([record.split(" ", 1)[1] for record in records], ["first", "second with a newline", "third"])

    def test_nothing_to_write_creates_nothing(self):
        script_log.append("watch.log", [])
        self.assertFalse((self.home / "logs").exists())

    def test_a_log_that_cannot_be_written_is_dropped(self):
        (self.home / "logs").write_text("a file where the directory goes", encoding="utf-8")
        script_log.append("watch.log", ["lost"])


if __name__ == "__main__":
    unittest.main()
