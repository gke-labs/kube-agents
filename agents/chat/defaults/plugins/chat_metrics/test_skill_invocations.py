"""Tests for the skill-load counter the chat metrics listener reads off the audit files.

Run: python3 -m unittest discover -s agents/chat/defaults/plugins/chat_metrics -p 'test_skill_invocations.py'
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_metrics import metrics  # noqa: E402
from chat_metrics import skill_invocations as skills  # noqa: E402
from common import audit_sink  # noqa: E402
from common.audit_schema import envelope  # noqa: E402

_SKILL_LINE = re.compile(r'^kubeagents_skill_invocations_total\{skill="([^"]*)"\} (\d+)$', re.M)


def _start(tool="skill_view", **args):
    """The line tool_call_audit writes when a call starts, as the sink serialises it."""
    record = envelope(
        "tool_call_start",
        {
            "tool_name": tool,
            "tool": tool,
            "status": "started",
            "task_id": "t_1",
            "session_id": "s_1",
            "args": json.dumps(args, sort_keys=True),
        },
    )
    return audit_sink.serialize(record) + "\n"


def _end(tool="skill_view"):
    record = envelope("tool_call_end", {"tool_name": tool, "tool": tool, "status": "completed", "result": "{}"})
    return audit_sink.serialize(record) + "\n"


class _VolumeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.counter = skills.SkillInvocationsCounter(root=lambda: self.root)

    def audit(self, profile=None):
        home = self.root / "profiles" / profile if profile else self.root
        return audit_sink.audit_file_path(home)

    def append(self, text, profile=None):
        path = self.audit(profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def rotate(self, profile=None):
        """What the sink does at the cap: shift the backups and move the live file to .1."""
        path = self.audit(profile)
        for index in range(audit_sink.AUDIT_FILE_BACKUP_COUNT - 1, 0, -1):
            older = skills.rotated(path, index)
            if older.exists():
                os.replace(older, skills.rotated(path, index + 1))
        os.replace(path, skills.rotated(path, 1))


class RecordTest(unittest.TestCase):
    def test_a_skill_load_is_read_from_the_record_the_audit_plugin_writes(self):
        self.assertEqual("fleet-audit", skills.skill_loaded(_start(name="fleet-audit").encode()))

    def test_a_reference_document_of_a_skill_is_not_a_second_load(self):
        line = _start(name="fleet-audit", file_path="references/lifecycle.md")
        self.assertIsNone(skills.skill_loaded(line.encode()))

    def test_other_records_are_not_loads(self):
        for line in (
            _end(),
            _start(tool="terminal", command="echo skill_view"),
            _start(tool="skills_list"),
            '{"args": "skill_view"}\n',
            '"skill_view"\n',
            'not json "skill_view"\n',
        ):
            with self.subTest(line=line):
                self.assertIsNone(skills.skill_loaded(line.encode()))

    def test_a_call_whose_arguments_cannot_be_read_counts_as_other(self):
        cut = _start(name="fleet-audit").replace('\\"}"', '...(truncated)"')
        self.assertNotEqual(cut, _start(name="fleet-audit"))
        self.assertEqual("other", skills.skill_loaded(cut.encode()))
        record = json.loads(_start(name="fleet-audit"))
        record["args"] = ["fleet-audit"]
        self.assertEqual("other", skills.skill_loaded(json.dumps(record).encode()))

    def test_a_line_the_parser_cannot_take_is_not_a_load_and_does_not_raise(self):
        nested = b'{"tool": "skill_view", "args": ' + b"[" * 200_000 + b"]" * 200_000 + b"}\n"
        self.assertIsNone(skills.skill_loaded(nested))
        deep_args = json.loads(_start(name="fleet-audit"))
        deep_args["args"] = "[" * 10_000 + "]" * 10_000
        self.assertEqual("other", skills.skill_loaded(json.dumps(deep_args).encode()))

    def test_a_line_longer_than_any_record_the_plugin_writes_is_skipped_unparsed(self):
        long_line = _start(name="fleet-audit", padding="x" * skills.MAX_RECORD_BYTES).encode()
        with mock.patch.object(skills.json, "loads", side_effect=AssertionError("parsed")):
            self.assertIsNone(skills.skill_loaded(long_line))

    def test_the_label_is_the_name_only_when_it_is_shaped_like_one(self):
        for name in ("fleet-audit", "gke-rightsizing-checks", "a2", "devops/fleet-audit", "stockout:investigate"):
            with self.subTest(name=name):
                self.assertEqual(name, skills.skill_label(name))
        for name in ("", "Fleet-Audit", "fleet audit", "fleet-audit\n", "../etc", "a--b", "-a", "a/", ":a", "a:b:c", "x" * 129, None, 7, ["a"]):
            with self.subTest(name=name):
                self.assertEqual("other", skills.skill_label(name))


class RootTest(unittest.TestCase):
    """Where the audit files are, for a gateway run at the root and for one run under a named profile."""

    def root_for(self, hermes_home):
        env = {} if hermes_home is None else {audit_sink.HERMES_HOME_ENV: hermes_home}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.dict(sys.modules, {"hermes_constants": None}):
            if hermes_home is None:
                os.environ.pop(audit_sink.HERMES_HOME_ENV, None)
            return skills.hermes_root()

    def test_the_home_is_the_root_for_the_default_profile(self):
        self.assertEqual(Path("/opt/data"), self.root_for("/opt/data"))
        self.assertEqual(Path(audit_sink.DEFAULT_HERMES_HOME), self.root_for(None))

    def test_a_named_profiles_home_is_mapped_back_to_the_root(self):
        self.assertEqual(Path("/opt/data"), self.root_for("/opt/data/profiles/platform"))

    def test_hermes_own_resolution_is_used_when_the_process_is_hermes(self):
        module = type(sys)("hermes_constants")
        module.get_default_hermes_root = lambda: "/srv/hermes"
        with mock.patch.dict(sys.modules, {"hermes_constants": module}):
            self.assertEqual(Path("/srv/hermes"), skills.hermes_root())

    def test_a_gateway_under_a_named_profile_counts_every_profile(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        homes = {"front": root, "platform": root / "profiles" / "platform", "cluster": root / "profiles" / "cluster-a"}
        for home in homes.values():
            (home / "logs").mkdir(parents=True)
        with mock.patch.dict(os.environ, {audit_sink.HERMES_HOME_ENV: str(homes["platform"])}), \
                mock.patch.dict(sys.modules, {"hermes_constants": None}):
            counter = skills.SkillInvocationsCounter()
            counter.counts()
            for name, home in homes.items():
                with open(audit_sink.audit_file_path(home), "a", encoding="utf-8") as handle:
                    handle.write(_start(name=f"from-{name}"))
            self.assertEqual({"from-front": 1, "from-platform": 1, "from-cluster": 1}, counter.counts())


class CounterTest(_VolumeTest):
    def test_what_the_files_held_before_the_first_read_is_the_baseline(self):
        self.append(_start(name="fleet-audit"))
        self.append(_start(name="pod-crashloop-oom"), profile="cluster-a")
        self.assertEqual({}, self.counter.counts())
        self.assertEqual({}, self.counter.counts())

    def test_loads_after_the_baseline_count_by_skill_across_profiles(self):
        self.append(_start(name="fleet-audit"), profile="platform")
        self.counter.counts()
        self.append(_start(name="fleet-audit") + _end() + _start(tool="terminal", command="ls"), profile="platform")
        self.append(_start(name="pod-crashloop-oom") + _start(name="fleet-audit"), profile="cluster-a")
        self.append(_start(name="version-control"))
        self.assertEqual({"fleet-audit": 2, "pod-crashloop-oom": 1, "version-control": 1}, self.counter.counts())

    def test_a_second_read_adds_only_what_was_appended(self):
        self.counter.counts()
        self.append(_start(name="fleet-audit"), profile="platform")
        self.assertEqual({"fleet-audit": 1}, self.counter.counts())
        self.assertEqual({"fleet-audit": 1}, self.counter.counts())
        self.append(_start(name="fleet-audit"), profile="platform")
        self.assertEqual({"fleet-audit": 2}, self.counter.counts())

    def test_a_profile_that_appears_after_the_baseline_is_read_from_its_start(self):
        self.counter.counts()
        self.append(_start(name="pod-pending-scheduling") * 3, profile="cluster-new")
        self.assertEqual({"pod-pending-scheduling": 3}, self.counter.counts())

    def test_a_record_still_being_written_is_counted_once_it_is_whole(self):
        self.counter.counts()
        line = _start(name="fleet-audit")
        self.append(line[:40], profile="platform")
        self.assertEqual({}, self.counter.counts())
        self.append(line[40:], profile="platform")
        self.assertEqual({"fleet-audit": 1}, self.counter.counts())

    def test_a_rotation_loses_nothing_and_counts_nothing_twice(self):
        self.append(_start(name="before-baseline"), profile="platform")
        self.counter.counts()
        self.append(_start(name="fleet-audit"), profile="platform")
        self.assertEqual({"fleet-audit": 1}, self.counter.counts())
        self.append(_start(name="tail-of-old-file"), profile="platform")
        self.rotate("platform")
        self.append(_start(name="head-of-new-file"), profile="platform")
        self.assertEqual(
            {"fleet-audit": 1, "tail-of-old-file": 1, "head-of-new-file": 1},
            self.counter.counts(),
        )
        self.assertEqual(3, sum(self.counter.counts().values()))

    def test_two_rotations_between_reads_are_followed_through_the_backups(self):
        self.counter.counts()
        self.append(_start(name="first"), profile="platform")
        self.counter.counts()
        self.append(_start(name="second"), profile="platform")
        self.rotate("platform")
        self.append(_start(name="third"), profile="platform")
        self.rotate("platform")
        self.append(_start(name="fourth"), profile="platform")
        self.assertEqual({"first": 1, "second": 1, "third": 1, "fourth": 1}, self.counter.counts())

    def test_a_file_rotated_past_its_last_backup_starts_again_from_the_live_file(self):
        self.counter.counts()
        self.append(_start(name="first"), profile="platform")
        self.counter.counts()
        self.append(_start(name="lost"), profile="platform")
        self.rotate("platform")
        skills.rotated(self.audit("platform"), 1).unlink()
        self.append(_start(name="kept"), profile="platform")
        self.assertEqual({"first": 1, "kept": 1}, self.counter.counts())

    def test_a_file_truncated_in_place_is_read_from_its_start(self):
        self.counter.counts()
        path = self.append(_start(name="fleet-audit") * 2, profile="platform")
        self.assertEqual({"fleet-audit": 2}, self.counter.counts())
        path.write_text(_start(name="version-control"), encoding="utf-8")
        self.assertEqual({"fleet-audit": 2, "version-control": 1}, self.counter.counts())

    def test_a_pruned_profile_keeps_its_counts_and_a_new_one_of_that_name_starts_over(self):
        self.counter.counts()
        self.append(_start(name="fleet-audit"), profile="cluster-a")
        self.assertEqual({"fleet-audit": 1}, self.counter.counts())
        shutil.rmtree(self.root / "profiles" / "cluster-a")
        self.assertEqual({"fleet-audit": 1}, self.counter.counts())
        self.append(_start(name="fleet-audit"), profile="cluster-a")
        self.assertEqual({"fleet-audit": 2}, self.counter.counts())

    def test_a_file_in_the_profiles_directory_is_not_a_profile(self):
        (self.root / "profiles").mkdir()
        (self.root / "profiles" / "notes.txt").write_text("x", encoding="utf-8")
        with self.assertNoLogs(skills.logger, level="WARNING"):
            self.assertEqual({}, self.counter.counts())

    def test_a_volume_with_no_audit_file_yet_reads_as_nothing(self):
        with self.assertNoLogs(skills.logger, level="WARNING"):
            self.assertEqual({}, self.counter.counts())
        self.append(_start(name="fleet-audit"))
        self.assertEqual({"fleet-audit": 1}, self.counter.counts())

    def test_an_unreadable_file_is_logged_once_and_read_when_it_recovers(self):
        self.counter.counts()
        path = self.append(_start(name="fleet-audit"), profile="platform")
        self.append(_start(name="version-control"))
        real_open = open

        def refuse(target, *args, **kwargs):
            if Path(target) == path:
                raise PermissionError("denied")
            return real_open(target, *args, **kwargs)

        with mock.patch.object(skills, "open", refuse, create=True):
            with self.assertLogs(skills.logger, level="WARNING") as logged:
                self.assertEqual({"version-control": 1}, self.counter.counts())
            self.assertEqual(1, len(logged.records))
            self.assertIn("PermissionError", logged.output[0])
            with self.assertNoLogs(skills.logger, level="WARNING"):
                self.counter.counts()
        self.assertEqual({"fleet-audit": 1, "version-control": 1}, self.counter.counts())

    def test_a_crafted_line_is_passed_once_and_the_records_after_it_count(self):
        self.counter.counts()
        nested = b'{"tool": "skill_view", "args": ' + b"[" * 200_000 + b"]" * 200_000 + b"}\n"
        path = self.append(_start(name="fleet-audit"), profile="platform")
        with open(path, "ab") as handle:
            handle.write(nested)
        self.append(_start(name="version-control"), profile="platform")
        self.assertEqual({"fleet-audit": 1, "version-control": 1}, self.counter.counts())
        self.assertEqual({"fleet-audit": 1, "version-control": 1}, self.counter.counts())

    def test_a_read_that_fails_part_way_does_not_count_the_lines_before_it_twice(self):
        self.counter.counts()
        self.append(_start(name="fleet-audit") + _start(name="version-control"), profile="platform")
        real = skills.skill_loaded
        calls = []

        def fail_on_second(line):
            calls.append(line)
            if len(calls) == 2:
                raise OSError("EIO")
            return real(line)

        with mock.patch.object(skills, "skill_loaded", fail_on_second):
            with self.assertLogs(skills.logger, level="WARNING"):
                self.assertEqual({"fleet-audit": 1}, self.counter.counts())
        self.assertEqual({"fleet-audit": 1}, self.counter.counts())

    def test_no_failure_of_a_read_reaches_the_caller(self):
        self.counter.counts()
        self.append(_start(name="fleet-audit"), profile="platform")
        self.append(_start(name="version-control"))
        for error in (RuntimeError("bug"), RecursionError(), MemoryError(), UnicodeDecodeError("utf-8", b"", 0, 1, "x")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(self.counter, "_read_file", side_effect=error):
                    self.assertEqual({}, self.counter.counts())
                with mock.patch.object(skills, "audit_files", side_effect=error):
                    self.assertEqual({}, self.counter.counts())
        self.assertEqual({"fleet-audit": 1, "version-control": 1}, self.counter.counts())

    def test_a_listing_that_fails_once_does_not_make_a_file_count_again(self):
        self.append(_start(name="before-baseline"), profile="platform")
        self.counter.counts()
        self.append(_start(name="fleet-audit"), profile="platform")
        self.assertEqual({"fleet-audit": 1}, self.counter.counts())
        with mock.patch.object(skills.os, "scandir", side_effect=OSError("EIO")):
            with self.assertLogs(skills.logger, level="WARNING"):
                self.assertEqual({"fleet-audit": 1}, self.counter.counts())
        self.assertEqual({"fleet-audit": 1}, self.counter.counts())

    def test_the_files_already_followed_are_still_read_while_the_listing_fails(self):
        self.counter.counts()
        self.append(_start(name="fleet-audit"), profile="platform")
        self.counter.counts()
        self.append(_start(name="version-control"), profile="platform")
        with mock.patch.object(skills.os, "scandir", side_effect=OSError("EIO")):
            with self.assertLogs(skills.logger, level="WARNING"):
                self.assertEqual({"fleet-audit": 1, "version-control": 1}, self.counter.counts())

    def test_a_listing_that_fails_at_the_baseline_does_not_count_history_later(self):
        self.append(_start(name="old-history") * 5, profile="platform")
        self.append(_start(name="old-front-door"))
        with mock.patch.object(skills.os, "scandir", side_effect=OSError("EIO")):
            with self.assertLogs(skills.logger, level="WARNING"):
                self.assertEqual({}, self.counter.counts())
        self.assertEqual({}, self.counter.counts())
        self.append(_start(name="fleet-audit"), profile="platform")
        self.assertEqual({"fleet-audit": 1}, self.counter.counts())

    def test_a_file_unreadable_at_the_baseline_does_not_count_its_history_later(self):
        path = self.append(_start(name="before-baseline"), profile="platform")
        real_open = open

        def refuse(target, *args, **kwargs):
            if Path(target) == path:
                raise PermissionError("denied")
            return real_open(target, *args, **kwargs)

        with mock.patch.object(skills, "open", refuse, create=True):
            with self.assertLogs(skills.logger, level="WARNING"):
                self.assertEqual({}, self.counter.counts())
        self.assertEqual({}, self.counter.counts())
        self.append(_start(name="fleet-audit"), profile="platform")
        self.assertEqual({"fleet-audit": 1}, self.counter.counts())

    def test_a_backup_that_is_the_file_already_open_is_not_read_twice(self):
        """A rotation between the open of the live file and the read of the backups."""
        self.counter.counts()
        path = self.append(_start(name="first"), profile="platform")
        self.counter.counts()
        self.append(_start(name="second"), profile="platform")
        self.rotate("platform")
        self.append(_start(name="third"), profile="platform")
        real_finish = self.counter._finish_rotated

        def rotate_then_finish(target, cursor, live_inode):
            self.rotate("platform")
            self.append(_start(name="fourth"), profile="platform")
            real_finish(target, cursor, live_inode)

        with mock.patch.object(self.counter, "_finish_rotated", rotate_then_finish):
            self.assertEqual({"first": 1, "second": 1, "third": 1}, self.counter.counts())
        self.assertEqual({"first": 1, "second": 1, "third": 1, "fourth": 1}, self.counter.counts())
        self.assertTrue(path.exists())

    def test_names_past_the_series_cap_count_as_other(self):
        counter = skills.SkillInvocationsCounter(root=lambda: self.root, max_series=2)
        counter.counts()
        self.append(_start(name="one") + _start(name="two") + _start(name="three") + _start(name="one") + _start(name="four"))
        self.assertEqual({"one": 2, "two": 1, "other": 2}, counter.counts())

    def test_the_other_series_does_not_take_a_place_under_the_cap(self):
        counter = skills.SkillInvocationsCounter(root=lambda: self.root, max_series=2)
        counter.counts()
        self.append(_start(name="Not A Name") + _start(name="one") + _start(name="two") + _start(name="three"))
        self.assertEqual({"other": 2, "one": 1, "two": 1}, counter.counts())

    def test_a_name_that_is_not_a_skill_name_counts_as_other(self):
        self.counter.counts()
        self.append(_start(name="Not A Skill") + _start() + _start(name="fleet-audit"))
        self.assertEqual({"other": 2, "fleet-audit": 1}, self.counter.counts())


class RenderTest(_VolumeTest):
    def registry(self):
        return metrics.ChatMetrics(skills=self.counter)

    def test_the_family_is_declared_with_no_series_before_any_load(self):
        text = self.registry().render()
        self.assertIn("# TYPE kubeagents_skill_invocations_total counter", text)
        self.assertEqual([], _SKILL_LINE.findall(text))

    def test_prime_takes_the_baseline_so_the_first_scrape_counts_what_follows(self):
        self.append(_start(name="before-the-listener"), profile="platform")
        registry = self.registry()
        registry.prime()
        self.append(_start(name="fleet-audit"), profile="platform")
        self.assertEqual([("fleet-audit", "1")], _SKILL_LINE.findall(registry.render()))

    def test_a_crafted_audit_line_does_not_take_the_other_families_down(self):
        registry = self.registry()
        registry.prime()
        registry.record_inbound("slack")
        path = self.append(_start(name="fleet-audit"), profile="platform")
        with open(path, "ab") as handle:
            handle.write(b'{"tool": "skill_view", "args": ' + b"[" * 200_000 + b"]" * 200_000 + b"}\n")
        for _ in range(2):
            text = registry.render()
            self.assertIn('kubeagents_chat_messages_inbound_total{platform="slack"} 1', text)
            self.assertEqual([("fleet-audit", "1")], _SKILL_LINE.findall(text))

    def test_series_render_sorted_by_skill_and_two_scrapes_agree(self):
        registry = self.registry()
        registry.prime()
        self.append(_start(name="version-control") + _start(name="fleet-audit") * 2 + _start(name="Bad Name"))
        text = registry.render()
        self.assertEqual([("fleet-audit", "2"), ("other", "1"), ("version-control", "1")], _SKILL_LINE.findall(text))
        self.assertEqual(text, registry.render())


if __name__ == "__main__":
    unittest.main()
