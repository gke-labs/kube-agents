"""Unit tests for bootstrap_handoff.py, the onboarding sweep's hand-off to ranking.

Run: python3 -m unittest agents/chat/scripts/test_bootstrap_handoff.py

The cluster metadata in testdata/bootstrap_handoff/ is what four of the
Cluster Agents on a live install returned, trimmed and with the project id
replaced, so
the findings carry the shapes the agents really produce: no `workload` field,
`namespace: multiple`, several workloads named in one `issue`. The raw file is
checked with the ranking stage's own parser, inventory_findings.parse_block,
rather than with a copy of its rules.
"""

import json
import os
import shlex
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.absolute()))
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "platform" / "scripts"))

import bootstrap_handoff as h
import inventory_findings

FIXTURE = Path(__file__).parent / "testdata" / "bootstrap_handoff" / "cluster_metadata.json"
SWEEP = "t_sweep"
SWEEP_CREATED = 1000
NOW = 5000


def _metadata() -> dict:
    return json.loads(FIXTURE.read_text())


def _board(path: Path, sweep_status="done", sweep_meta=None, clusters=None) -> None:
    """A board with the columns the hand-off reads, in the board's own names."""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT, status TEXT, idempotency_key TEXT, title TEXT, created_at INTEGER, body TEXT);"
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, metadata TEXT);"
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);"
    )
    conn.execute("INSERT INTO tasks (id, status, idempotency_key, title, created_at) VALUES (?, ?, 'bootstrap-inventory-scan', 'sweep', ?)",
                 (SWEEP, sweep_status, SWEEP_CREATED))
    if sweep_meta is not None:
        conn.execute("INSERT INTO task_runs (task_id, outcome, metadata) VALUES (?, 'completed', ?)",
                     (SWEEP, json.dumps(sweep_meta)))
    for i, (tid, status, meta, reason) in enumerate(clusters or []):
        conn.execute("INSERT INTO tasks (id, status, idempotency_key, title, created_at) VALUES (?, ?, ?, ?, ?)",
                     (tid, status, f"{h.CLUSTER_KEY_PREFIX}{tid}", f"Report cluster inventory: {tid}",
                      SWEEP_CREATED + 1 + i))
        if meta is not None:
            conn.execute("INSERT INTO task_runs (task_id, outcome, metadata) VALUES (?, 'completed', ?)",
                         (tid, json.dumps(meta)))
        if reason:
            conn.execute("INSERT INTO task_events (task_id, kind, payload) VALUES (?, 'blocked', ?)",
                         (tid, json.dumps({"reason": reason})))
    conn.commit()
    conn.close()


def _all_done() -> list:
    return [(tid, "done", meta, "") for tid, meta in _metadata().items()]


class SharedNamesTest(unittest.TestCase):
    def test_keys_and_paths_match_the_gate(self):
        import bootstrap_scan_gate as g

        self.assertEqual(h.CLUSTER_KEY_PREFIX, g.CLUSTER_IDEMPOTENCY_KEY_PREFIX)
        self.assertEqual(h.PRIORITIZE_KEY, g.PRIORITIZE_IDEMPOTENCY_KEY)
        self.assertEqual(h.ASSIGNEE, g.SCAN_ASSIGNEE)
        self.assertEqual(h.RAW_PATH, g.RAW_INVENTORY_PATH)
        self.assertEqual(h.REPORT_PATH, g.INVENTORY_PATH)
        self.assertEqual(h.PRIORITIZE_INSTRUCTIONS_PATHS, g.PRIORITIZE_INSTRUCTIONS_PATHS)
        self.assertEqual(h.CLUSTER_AUDIT_INSTRUCTIONS_PATHS, g.CLUSTER_AUDIT_INSTRUCTIONS_PATHS)


class MarkerTest(unittest.TestCase):
    def test_a_marker_with_a_space_after_the_equals_sign_still_names_the_sweep(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m"
            path.write_text("task_id= t_abc\n")
            self.assertEqual(h._read_marker(path), {"task_id": "t_abc"})

    def test_a_hand_written_marker_with_spaces_still_names_the_sweep(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m"
            path.write_text("task_id = t_abc\nfiled_at=123\n")
            self.assertEqual(h._read_marker(path), {"task_id": "t_abc", "filed_at": "123"})


class FindingLinesTest(unittest.TestCase):
    def test_every_reported_finding_becomes_a_line_the_ranking_parser_accepts(self):
        for meta in _metadata().values():
            lines = h.finding_lines(meta)
            self.assertEqual(len(lines), len(meta.get("findings") or []), meta["cluster"])
            text = "```findings\n" + "\n".join(json.dumps(line) for line in lines) + "\n```\n"
            self.assertEqual(len(inventory_findings.parse_block(text)), len(lines))

    def test_a_finding_with_no_workload_is_filed_against_its_namespace(self):
        meta = _metadata()["t_ccbfda55"]
        line = h.finding_lines(meta)[0]
        self.assertEqual((line["namespace"], line["object"]), ("seeded-capacity", "seeded-capacity"))

    def test_a_finding_over_several_namespaces_is_filed_against_the_cluster(self):
        meta = _metadata()["t_d9c96cd4"]
        multi = [line for line in h.finding_lines(meta) if "namespace" not in line]
        self.assertEqual(len(multi), 1)
        self.assertEqual(multi[0]["object"], meta["cluster"])

    def test_provider_namespaces_are_marked_and_others_are_not(self):
        lines = h.finding_lines(_metadata()["t_3849261c"])
        managed = {line["namespace"]: line.get("provider_managed", False) for line in lines}
        self.assertTrue(managed["kube-system"])
        self.assertFalse(managed["buildkit"])

    def test_a_check_the_agent_named_is_kept(self):
        meta = {"project": "p", "cluster": "c", "findings": [
            {"check": "probes-readiness", "namespace": "ns", "workload": "api", "issue": "no readinessProbe"}]}
        self.assertEqual(h.finding_lines(meta)[0]["check"], "probes-readiness")


class ComposeTest(unittest.TestCase):
    def _state(self, clusters, sweep_meta=None, sweep_status="done"):
        with tempfile.TemporaryDirectory() as d:
            board = Path(d) / "kanban.db"
            _board(board, sweep_status, sweep_meta, clusters)
            return h.read_board(board, SWEEP)

    def test_the_raw_file_carries_a_block_with_every_cluster_that_reported_findings(self):
        text = h.compose(self._state(_all_done()), timed_out=False, now=NOW)
        items = inventory_findings.parse_block(text)
        reported = {m["cluster"] for m in _metadata().values() if m.get("findings")}
        self.assertEqual({i["cluster"] for i in items}, reported)
        self.assertEqual(len(items), sum(len(m.get("findings") or []) for m in _metadata().values()))

    def test_a_clean_cluster_is_covered_without_block_lines(self):
        text = h.compose(self._state(_all_done()), timed_out=False, now=NOW)
        self.assertIn("| seeded-b |", text)
        self.assertNotIn('"cluster": "seeded-b"', text)

    def test_a_blocked_card_is_a_named_gap(self):
        clusters = _all_done()[:1] + [("t_blocked", "blocked", None, "Cluster preflight check failed with exit code 255.")]
        text = h.compose(self._state(clusters), timed_out=False, now=NOW)
        self.assertIn("t_blocked): blocked — Cluster preflight check failed with exit code 255.", text)

    def test_a_fleet_cluster_nobody_reported_on_is_a_gap(self):
        fleet = [{"project": "example-project", "cluster": "orphan", "location": "us-east1"}]
        text = h.compose(self._state(_all_done(), {"fleet": fleet}), timed_out=False, now=NOW)
        self.assertIn("orphan (example-project, us-east1): listed in the fleet but no audit reported on it", text)

    def test_clusters_the_sweep_audited_itself_are_included(self):
        own = dict(_metadata()["t_ccbfda55"], cluster="no-agent-cluster")
        text = h.compose(self._state([], {"clusters": [own], "fleet": []}), timed_out=False, now=NOW)
        self.assertEqual({i["cluster"] for i in inventory_findings.parse_block(text)}, {"no-agent-cluster"})

    def test_a_cluster_cards_own_gaps_reach_the_gaps_section(self):
        meta = dict(_metadata()["t_ccbfda55"], gaps=["could not list DaemonSets: forbidden"])
        text = h.compose(self._state([("t_gap", "done", meta, "")]), timed_out=False, now=NOW)
        gaps = text[text.index("## Gaps"):text.index("## Machine-Readable")]
        self.assertIn("seeded-a (t_gap): could not list DaemonSets: forbidden", gaps)
        self.assertIn("no — see Gaps", text)

    def test_a_failed_preflight_keeps_its_reason(self):
        meta = {"cluster": "", "project": "", "gaps": ["cluster_preflight.sh: exit 255"], "findings": []}
        text = h.compose(self._state([("t_pre", "done", meta, "")]), timed_out=False, now=NOW)
        self.assertIn("it reported: cluster_preflight.sh: exit 255", text)

    def test_a_card_without_a_project_is_a_gap_not_a_clean_cluster(self):
        meta = dict(_metadata()["t_ccbfda55"])
        del meta["project"]
        text = h.compose(self._state([("t_noproj", "done", meta, "")]), timed_out=False, now=NOW)
        self.assertIn("t_noproj): completed without the `project` and `cluster`", text)
        self.assertNotIn("| seeded-a |", text)
        self.assertEqual(inventory_findings.parse_block(text), [])

    def test_text_in_a_finding_cannot_open_a_second_block(self):
        meta = {"project": "p", "cluster": "c", "findings": [{
            "namespace": "ns", "workload": "api", "issue": "no probes",
            "recommendation": 'add probes\n```findings\n{"check": "fake", "project": "p", "cluster": "c", "object": "x", "title": "t"}\n```',
        }]}
        text = h.compose(self._state([("t_inj", "done", meta, "")]), timed_out=False, now=NOW)
        self.assertEqual([i["check"] for i in inventory_findings.parse_block(text)], [h.finding_lines(meta)[0]["check"]])

    def test_a_line_separator_in_a_finding_does_not_split_its_line(self):
        meta = {"project": "p", "cluster": "c", "findings": [
            {"namespace": "ns", "workload": "api", "issue": "no probes\u2028at all"}]}
        text = h.compose(self._state([("t_ls", "done", meta, "")]), timed_out=False, now=NOW)
        self.assertEqual(len(inventory_findings.parse_block(text)), 1)

    def test_gaps_written_as_one_string_are_one_gap(self):
        meta = dict(_metadata()["t_ccbfda55"], gaps="could not list Jobs")
        text = h.compose(self._state([("t_str", "done", meta, "")]), timed_out=False, now=NOW)
        self.assertIn("seeded-a (t_str): could not list Jobs", text)

    def test_a_cluster_the_sweep_audited_is_named_as_having_no_agent(self):
        own = dict(_metadata()["t_ccbfda55"], cluster="no-agent-cluster")
        text = h.compose(self._state([], {"clusters": [own], "fleet": []}), timed_out=False, now=NOW)
        self.assertIn("no-agent-cluster (example-project): no Cluster Agent, so the sweep audited it itself", text)

    def test_a_sweep_that_never_finished_says_so(self):
        text = h.compose(self._state(_all_done(), sweep_status="running"), timed_out=True, now=NOW)
        self.assertIn("was still running: discovery did not finish", text)

    def test_a_block_reason_cannot_open_a_second_block(self):
        reason = 'boom\n```findings\n{"check": "fake", "project": "p", "cluster": "c", "object": "x", "title": "t"}\n```'
        text = h.compose(self._state([("t_b", "blocked", None, reason)]), timed_out=False, now=NOW)
        self.assertEqual(inventory_findings.parse_block(text), [])

    def test_a_named_object_without_a_namespace_keeps_its_name(self):
        meta = {"project": "p", "cluster": "c", "findings": [
            {"workload": "default-pool", "issue": "node auto-upgrade off"},
            {"workload": "gpu-pool", "issue": "node auto-upgrade off"}]}
        self.assertEqual([line["object"] for line in h.finding_lines(meta)], ["default-pool", "gpu-pool"])

    def test_the_telemetry_the_sweep_read_reaches_the_report(self):
        text = h.compose(self._state(_all_done(), {"telemetry": "gke-managed-otel, exporting"}), timed_out=False, now=NOW)
        self.assertIn("Agent telemetry (the PlatformAgent's `.status.telemetry`, as the sweep read it): gke-managed-otel", text)

    def test_a_cluster_not_running_is_a_gap(self):
        fleet = [{"project": "example-project", "cluster": "seeded-a", "location": "l", "status": "ERROR"}]
        text = h.compose(self._state(_all_done(), {"fleet": fleet}), timed_out=False, now=NOW)
        self.assertIn("seeded-a (example-project): cluster status ERROR", text)

    def test_a_finding_without_text_is_a_named_gap(self):
        meta = {"project": "p", "cluster": "c", "findings": [
            {"namespace": "ns", "workload": "api", "issue": "no probes"},
            {"namespace": "buildkit", "workload": "gke0", "description": "runs privileged"}]}
        text = h.compose(self._state([("t_txt", "done", meta, "")]), timed_out=False, now=NOW)
        self.assertIn("c (t_txt): 1 reported finding(s) had no `issue` or `title`", text)
        self.assertEqual(len(inventory_findings.parse_block(text)), 1)

    def test_titles_with_no_ascii_words_keep_separate_checks(self):
        meta = {"project": "p", "cluster": "c", "findings": [
            {"namespace": "ns", "workload": "api", "area": "security", "issue": "\u0431\u0435\u0437 \u043f\u0440\u043e\u0431"},
            {"namespace": "ns", "workload": "api", "area": "security", "issue": "\u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442 \u043e\u0442 \u043a\u043e\u0440\u043d\u044f"}]}
        checks = [line["check"] for line in h.finding_lines(meta)]
        self.assertEqual(len(set(checks)), 2)

    def test_a_cancelled_or_failed_card_counts_as_settled(self):
        clusters = _all_done() + [("t_c", "cancelled", None, ""), ("t_f", "failed", None, "")]
        self.assertTrue(h.settled(self._state(clusters)))

    def test_a_failed_or_cancelled_card_says_so(self):
        text = h.compose(self._state([("t_f", "failed", None, ""), ("t_c", "cancelled", None, "")]), timed_out=False, now=NOW)
        self.assertIn("(t_f): failed before it reported", text)
        self.assertIn("(t_c): cancelled before it reported", text)

    def test_a_card_in_triage_keeps_its_block_reason(self):
        text = h.compose(self._state([("t_t", "triage", None, "HTTP 429 rate limit")]), timed_out=False, now=NOW)
        self.assertIn("(t_t): triage — HTTP 429 rate limit", text)

    def test_no_findings_still_writes_an_empty_block(self):
        clusters = [("t_5ca49c4e", "done", _metadata()["t_5ca49c4e"], "")]
        text = h.compose(self._state(clusters), timed_out=False, now=NOW)
        self.assertEqual(inventory_findings.parse_block(text), [])


class HandOffTest(unittest.TestCase):
    _real_file_prioritize = staticmethod(h.file_prioritize)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.board = self.d / "kanban.db"
        self.scan_marker = self.d / ".bootstrap_scan_filed"
        self.scan_marker.write_text(f"task_id={SWEEP}\nfiled_at={NOW - 60}\n")
        self.filed = []
        self.sandbox = types.SimpleNamespace(
            sandbox_enabled=lambda: False, TERMINAL_PRINCIPAL="agent", run=None)
        patches = [
            mock.patch.object(h, "_board_path", lambda _d: self.board),
            mock.patch.object(h, "_sandbox", lambda: self.sandbox),
            mock.patch.object(h, "file_prioritize", self._file),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, _parse, stale=None):
        self.filed.append(1)
        return f"t_rank{len(self.filed)}"

    def _run(self, now=NOW):
        return h.hand_off(self.d, self.scan_marker, None, now=now)

    def _run_roster(self, roster, now=NOW):
        import bootstrap_scan_gate

        return h.hand_off(self.d, self.scan_marker, bootstrap_scan_gate._parse_task_id, roster=roster, now=now)

    def _run_with_parser(self):
        import bootstrap_scan_gate

        return h.hand_off(self.d, self.scan_marker, bootstrap_scan_gate._parse_task_id, now=NOW)

    def test_waits_while_a_cluster_card_is_running(self):
        _board(self.board, clusters=_all_done() + [("t_running", "running", None, "")])
        self.assertIsNone(self._run())
        self.assertFalse((self.d / "INVENTORY.raw.md").exists())
        self.assertEqual(self.filed, [])

    def test_waits_while_the_sweep_is_still_running(self):
        _board(self.board, sweep_status="running", clusters=_all_done())
        self.assertIsNone(self._run())

    def test_settled_cards_get_a_raw_file_and_one_ranking_card(self):
        _board(self.board, clusters=_all_done())
        self.assertEqual(self._run(), "t_rank1")
        inventory_findings.parse_block((self.d / "INVENTORY.raw.md").read_text())
        self.assertEqual(self._run(), None)  # once only
        self.assertEqual(self.filed, [1])

    def test_a_re_armed_sweep_gets_its_own_hand_off(self):
        _board(self.board, clusters=_all_done())
        self._run()
        (self.d / h.HANDOFF_MARKER).write_text("sweep=t_older\ntask_id=t_rank0\n")
        self.assertEqual(self._run(), "t_rank2")

    def test_the_deadline_hands_off_what_settled(self):
        _board(self.board, clusters=_all_done() + [("t_stuck", "ready", None, "")])
        limit = h.DEADLINE_SECONDS + h.DEADLINE_PER_CARD_SECONDS * (len(_all_done()) + 1)
        self.assertIsNone(self._run(now=NOW - 60 + h.DEADLINE_SECONDS))
        self.assertEqual(self._run(now=NOW - 60 + limit), "t_rank1")
        text = (self.d / "INVENTORY.raw.md").read_text()
        self.assertIn("t_stuck): still ready when the hand-off ran", text)
        self.assertIn("stopped waiting", text)

    def test_a_blocked_sweep_waits_for_the_deadline(self):
        _board(self.board, sweep_status="blocked", clusters=_all_done())
        self.assertIsNone(self._run())
        self.assertEqual(self.filed, [])
        limit = h.DEADLINE_SECONDS + h.DEADLINE_PER_CARD_SECONDS * len(_all_done())
        self.assertEqual(self._run(now=NOW - 60 + limit), "t_rank1")
        self.assertIn("blocked —", (self.d / "INVENTORY.raw.md").read_text())

    def test_no_cluster_audited_is_reported_not_ranked(self):
        # No Cluster Agents and a sweep that completed with nothing: ranked, the
        # empty raw file would read as a clean environment.
        _board(self.board, clusters=[])
        self.assertIsNone(self._run_roster([]))
        self.assertEqual(self.filed, [])
        report = (self.d / "INVENTORY.md").read_text()
        self.assertIn("no cluster was audited", report)
        self.assertIn("the sweep reported no fleet list", report)
        self.assertIn("task_id=none", (self.d / h.HANDOFF_MARKER).read_text())
        (self.d / "INVENTORY.md").unlink()
        self.assertIsNone(self._run_roster([]))
        self.assertFalse((self.d / "INVENTORY.md").exists())

    def test_the_no_coverage_report_lists_only_the_first_gaps(self):
        raw = "# r\n\n## Gaps\n\n" + "".join(f"- gap {i}\n" for i in range(25)) + "\n## Machine-Readable Findings\n"
        report = h.no_coverage_report(raw)
        self.assertIn("- gap 9\n", report)
        self.assertNotIn("- gap 10", report)
        self.assertIn("15 more in the full record.", report)
        self.assertNotIn("Machine-Readable", report)

    def test_a_sweep_audit_written_as_one_object_counts(self):
        # A single-cluster install with no Cluster Agents: the sweep's own audit,
        # written as an object where the SOP asks for a list.
        audit = {"project": "p", "cluster": "c", "workloads": [], "findings": [], "gaps": []}
        _board(self.board, sweep_meta={"clusters": audit, "fleet": [{"project": "p", "cluster": "c"}]}, clusters=[])
        self.assertEqual(self._run_roster([]), "t_rank1")
        self.assertFalse((self.d / "INVENTORY.md").exists())
        self.assertNotIn("no audit reported on it", (self.d / "INVENTORY.raw.md").read_text())

    def test_an_empty_value_of_the_wrong_type_is_not_a_gap(self):
        meta = {"project": "p", "cluster": "c", "workloads": [], "findings": "", "gaps": {}}
        _board(self.board, clusters=[("t_odd", "done", meta, "")])
        self.assertEqual(self._run(), "t_rank1")
        raw = (self.d / "INVENTORY.raw.md").read_text()
        self.assertNotIn("not a list", raw)
        self.assertIn("| t_odd | 0 | yes |", raw)

    def test_a_gaps_heading_inside_a_value_is_not_the_section(self):
        raw = "# r\n\n| x | telemetry ## Gaps\n- not this |\n\n## Gaps\n\n- the real gap\n\n## Machine-Readable Findings\n"
        report = h.no_coverage_report(raw)
        self.assertIn("- the real gap", report)
        self.assertNotIn("not this", report)

    def test_a_list_field_of_the_wrong_type_is_a_gap_not_a_clean_cluster(self):
        meta = {"project": "p", "cluster": "c", "workloads": [{"name": "w"}], "findings": "buildkit runs privileged",
                "gaps": {"reason": "ssh exit 255"}}
        _board(self.board, clusters=[("t_odd", "done", meta, "")])
        self.assertEqual(self._run(), "t_rank1")
        raw = (self.d / "INVENTORY.raw.md").read_text()
        self.assertIn("c (t_odd): `findings` was a str, not a list, so nothing from it is listed", raw)
        self.assertIn("c (t_odd): `gaps` was a dict, not a list", raw)
        self.assertIn("| t_odd | 1 | no — see Gaps |", raw)

    def test_one_archived_cluster_card_only_skips_that_cluster(self):
        _board(self.board, clusters=_all_done() + [("t_gone", "archived", None, "")])
        self.assertEqual(self._run(), "t_rank1")
        self.assertIn("1 cluster card(s) were archived before they reported", (self.d / "INVENTORY.raw.md").read_text())

    def test_every_cluster_card_archived_cancels_the_hand_off(self):
        _board(self.board, clusters=[(tid, "archived", meta, "") for tid, _, meta, _ in _all_done()])
        self.assertIsNone(self._run(now=NOW + 10 * h.DEADLINE_SECONDS))
        self.assertEqual(self.filed, [])

    def test_a_raw_file_already_in_place_is_rebuilt_from_the_cards(self):
        _board(self.board, clusters=_all_done())
        (self.d / "INVENTORY.raw.md").write_text("# report a model typed, no findings block\n")
        self.assertEqual(self._run(), "t_rank1")
        inventory_findings.parse_block((self.d / "INVENTORY.raw.md").read_text())

    def test_a_marker_without_filed_at_times_out_from_its_own_timestamp(self):
        _board(self.board, clusters=_all_done() + [("t_stuck", "ready", None, "")])
        self.scan_marker.write_text(f"task_id={SWEEP}\n")
        old = NOW - 10 * h.DEADLINE_SECONDS
        os.utime(self.scan_marker, (old, old))
        self.assertEqual(self._run(), "t_rank1")

    def test_metadata_of_the_wrong_type_does_not_raise(self):
        meta = {"project": "p", "cluster": "c", "workloads": 12, "findings": "none", "gaps": 3,
                "topology": {"node_pools": "default"}}
        _board(self.board, sweep_meta={"clusters": 5, "fleet": "x"}, clusters=[("t_odd", "done", meta, "")])
        self.assertEqual(self._run(), "t_rank1")
        inventory_findings.parse_block((self.d / "INVENTORY.raw.md").read_text())

    def _agent(self, tid):
        return {"name": f"cluster-{tid}", "key": f"{h.CLUSTER_KEY_PREFIX}{tid}",
                "title": f"Report cluster inventory: {tid}", "cluster_label": f"`{tid}`"}

    def _stub_kanban(self, sent, reply='{"id": "t_new"}'):
        kanban = types.ModuleType("hermes_cli.kanban")
        kanban.run_slash = lambda cmd: sent.append(cmd) or reply
        pkg = types.ModuleType("hermes_cli")
        pkg.kanban = kanban
        return mock.patch.dict(sys.modules, {"hermes_cli": pkg, "hermes_cli.kanban": kanban})

    def test_the_gate_files_a_card_for_each_cluster_agent_without_one(self):
        _board(self.board, clusters=_all_done()[:1])
        have = _all_done()[0][0]
        sent = []
        roster = [self._agent(have), self._agent("t_missing")]
        with self._stub_kanban(sent):
            self.assertIsNone(self._run_roster(roster))
        self.assertEqual(len(sent), 1)
        argv = shlex.split(sent[0])
        self.assertEqual(argv[argv.index("--idempotency-key") + 1], f"{h.CLUSTER_KEY_PREFIX}t_missing")
        self.assertEqual(argv[argv.index("--assignee") + 1], "cluster-t_missing")
        self.assertIn("cluster_inventory_audit_sop.md", argv[argv.index("--body") + 1])
        self.assertEqual(self.filed, [])

    def test_a_sweep_that_filed_no_cards_does_not_report_a_clean_fleet(self):
        # The live failure: a done sweep with no cluster cards at all.
        _board(self.board, clusters=[])
        sent = []
        with self._stub_kanban(sent):
            self.assertIsNone(self._run_roster([self._agent("t_a"), self._agent("t_b")]))
        self.assertEqual(len(sent), 2)
        self.assertFalse((self.d / "INVENTORY.raw.md").exists())
        self.assertEqual(self.filed, [])

    def test_an_archived_cluster_card_is_not_filed_again(self):
        _board(self.board, clusters=_all_done() + [("t_gone", "archived", None, "")])
        sent = []
        roster = [self._agent(tid) for tid, _, _, _ in _all_done()] + [self._agent("t_gone")]
        with self._stub_kanban(sent):
            self.assertEqual(self._run_roster(roster), "t_rank1")
        self.assertEqual(sent, [])

    def test_a_card_the_gate_could_not_file_holds_the_hand_off_then_is_a_gap(self):
        _board(self.board, clusters=_all_done())
        roster = [self._agent(tid) for tid, _, _, _ in _all_done()] + [self._agent("t_lost")]
        with self._stub_kanban([], reply="board unavailable"):
            self.assertIsNone(self._run_roster(roster))
            limit = h.DEADLINE_SECONDS + h.DEADLINE_PER_CARD_SECONDS * len(_all_done())
            self.assertEqual(self._run_roster(roster, now=NOW - 60 + limit), "t_rank1")
        self.assertIn("Report cluster inventory: t_lost: the gate could not file", (self.d / "INVENTORY.raw.md").read_text())

    def test_an_earlier_runs_open_card_does_not_hold_the_hand_off_forever(self):
        _board(self.board, clusters=_all_done())
        conn = sqlite3.connect(self.board)
        conn.execute("INSERT INTO tasks (id, status, idempotency_key, title, created_at) VALUES (?, ?, ?, ?, ?)",
                     ("t_old", "running", f"{h.CLUSTER_KEY_PREFIX}t_old", "old", SWEEP_CREATED - 100))
        conn.commit()
        conn.close()
        roster = [self._agent(tid) for tid, _, _, _ in _all_done()] + [self._agent("t_old")]
        sent = []
        with self._stub_kanban(sent):
            self.assertIsNone(self._run_roster(roster))
        self.assertEqual(sent[0], "archive t_old")
        argv = shlex.split(sent[1])
        self.assertEqual(argv[argv.index("--idempotency-key") + 1], f"{h.CLUSTER_KEY_PREFIX}t_old")

    def test_an_earlier_runs_ranking_card_is_archived_before_filing(self):
        _board(self.board, clusters=_all_done())
        conn = sqlite3.connect(self.board)
        conn.execute("INSERT INTO tasks (id, status, idempotency_key, title, created_at) VALUES (?, ?, ?, ?, ?)",
                     ("t_oldrank", "blocked", h.PRIORITIZE_KEY, "old ranking", SWEEP_CREATED - 100))
        conn.commit()
        conn.close()
        sent = []
        with self._stub_kanban(sent, reply='{"id": "t_newrank"}'), \
                mock.patch.object(h, "file_prioritize", HandOffTest._real_file_prioritize):
            self.assertEqual(self._run_with_parser(), "t_newrank")
        self.assertEqual(sent[0], "archive t_oldrank")

    def test_an_archive_the_board_ignored_is_a_gap_not_a_filed_card(self):
        _board(self.board, clusters=_all_done())
        conn = sqlite3.connect(self.board)
        conn.execute("INSERT INTO tasks (id, status, idempotency_key, title, created_at) VALUES (?, ?, ?, ?, ?)",
                     ("t_old", "running", f"{h.CLUSTER_KEY_PREFIX}t_old", "old", SWEEP_CREATED - 100))
        conn.commit()
        conn.close()
        roster = [self._agent(tid) for tid, _, _, _ in _all_done()] + [self._agent("t_old")]
        # The archive's error comes back as text; the create returns the old card.
        with self._stub_kanban([], reply='{"id": "t_old"}'):
            limit = h.DEADLINE_SECONDS + h.DEADLINE_PER_CARD_SECONDS * len(_all_done())
            self.assertIsNone(self._run_roster(roster))
            self.assertEqual(self._run_roster(roster, now=NOW - 60 + limit), "t_rank1")
        self.assertIn("card t_old holds this Cluster Agent's key and could not be archived",
                      (self.d / "INVENTORY.raw.md").read_text())

    def test_a_ranking_card_the_archive_left_is_not_recorded(self):
        _board(self.board, clusters=_all_done())
        conn = sqlite3.connect(self.board)
        conn.execute("INSERT INTO tasks (id, status, idempotency_key, title, created_at) VALUES (?, ?, ?, ?, ?)",
                     ("t_oldrank", "blocked", h.PRIORITIZE_KEY, "old ranking", SWEEP_CREATED - 100))
        conn.commit()
        conn.close()
        with self._stub_kanban([], reply='{"id": "t_oldrank"}'), \
                mock.patch.object(h, "file_prioritize", HandOffTest._real_file_prioritize):
            self.assertIsNone(self._run_with_parser())
        self.assertFalse((self.d / h.HANDOFF_MARKER).exists())

    def test_a_failed_or_cancelled_sweep_is_a_gap(self):
        for status in ("failed", "cancelled"):
            _board(self.board, sweep_status=status, clusters=_all_done())
            self.assertEqual(self._run(), f"t_rank{len(self.filed)}")
            raw = (self.d / "INVENTORY.raw.md").read_text()
            self.assertIn(f"sweep {SWEEP} {status}: the fleet list and any agent-less audits are missing", raw)
            self.board.unlink()
            (self.d / h.HANDOFF_MARKER).unlink()

    def _ranking_card(self, tid, status, created_at, body="Rank the inventory now."):
        conn = sqlite3.connect(self.board)
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
                     (tid, status, h.PRIORITIZE_KEY, "early ranking", created_at, body))
        conn.commit()
        conn.close()

    def test_a_ranking_card_the_hand_off_did_not_file_is_replaced(self):
        # A sweep worker that filed the key itself: newer than the sweep, in
        # any status, including running before the raw file exists.
        for status in ("todo", "running", "blocked", "done"):
            _board(self.board, clusters=_all_done())
            self._ranking_card("t_early", status, SWEEP_CREATED + 50)
            sent = []
            with self._stub_kanban(sent, reply='{"id": "t_newrank"}'), \
                    mock.patch.object(h, "file_prioritize", HandOffTest._real_file_prioritize):
                self.assertEqual(self._run_with_parser(), "t_newrank", status)
            self.assertEqual(sent[0], "archive t_early", status)
            self.board.unlink()
            (self.d / h.HANDOFF_MARKER).unlink()

    def test_an_earlier_runs_own_ranking_card_is_replaced(self):
        # Same body, filed for the previous sweep: not this sweep's card.
        _board(self.board, clusters=_all_done())
        self._ranking_card("t_prev", "blocked", SWEEP_CREATED - 100, body=h._prioritize_body())
        sent = []
        with self._stub_kanban(sent, reply='{"id": "t_newrank"}'), \
                mock.patch.object(h, "file_prioritize", HandOffTest._real_file_prioritize):
            self.assertEqual(self._run_with_parser(), "t_newrank")
        self.assertEqual(sent[0], "archive t_prev")

    def test_the_hand_offs_own_unrecorded_ranking_card_is_kept(self):
        # A tick that filed the card but failed to record it finds it again,
        # whatever became of it, so a blocked one is not re-filed every tick.
        for status in ("running", "blocked"):
            _board(self.board, clusters=_all_done())
            self._ranking_card("t_mine", status, SWEEP_CREATED + 50, body=h._prioritize_body())
            sent = []
            with self._stub_kanban(sent, reply='{"id": "t_mine"}'), \
                    mock.patch.object(h, "file_prioritize", HandOffTest._real_file_prioritize):
                self.assertEqual(self._run_with_parser(), "t_mine", status)
            self.assertFalse(any(cmd.startswith("archive") for cmd in sent), status)
            self.board.unlink()
            (self.d / h.HANDOFF_MARKER).unlink()

    def test_a_sweep_in_triage_reports_its_reason(self):
        _board(self.board, sweep_status="triage", clusters=_all_done())
        conn = sqlite3.connect(self.board)
        conn.execute("INSERT INTO task_events (task_id, kind, payload) VALUES (?, 'blocked', ?)",
                     (SWEEP, json.dumps({"reason": "cannot list clusters"})))
        conn.commit()
        conn.close()
        state = h.read_board(self.board, SWEEP)
        text = h.compose(state, timed_out=True, now=NOW)
        self.assertIn(f"sweep {SWEEP} triage — cannot list clusters", text)
        self.assertNotIn("was still", text)

    def test_a_failed_or_cancelled_sweep_settles(self):
        for status in ("failed", "cancelled"):
            _board(self.board, sweep_status=status, clusters=_all_done())
            self.assertTrue(h.settled(h.read_board(self.board, SWEEP)), status)
            self.board.unlink()

    def test_the_ranking_card_is_filed_with_its_key(self):
        _board(self.board, clusters=_all_done())
        sent = []
        kanban = types.ModuleType("hermes_cli.kanban")
        kanban.run_slash = lambda cmd: sent.append(cmd) or '{"id": "t_real"}'
        pkg = types.ModuleType("hermes_cli")
        pkg.kanban = kanban
        with mock.patch.dict(sys.modules, {"hermes_cli": pkg, "hermes_cli.kanban": kanban}), \
                mock.patch.object(h, "file_prioritize", HandOffTest._real_file_prioritize):
            self.assertEqual(self._run_with_parser(), "t_real")
        argv = shlex.split(sent[0])
        self.assertEqual(argv[argv.index("--idempotency-key") + 1], h.PRIORITIZE_KEY)
        self.assertEqual(argv[argv.index("--assignee") + 1], h.ASSIGNEE)

    def test_an_archived_sweep_is_left_alone_even_past_the_deadline(self):
        _board(self.board, sweep_status="archived", clusters=_all_done())
        self.assertIsNone(self._run(now=NOW + h.DEADLINE_SECONDS))
        self.assertFalse((self.d / "INVENTORY.raw.md").exists())
        self.assertEqual(self.filed, [])

    def test_no_ranking_card_without_a_raw_file(self):
        _board(self.board, clusters=_all_done())
        with mock.patch.object(h, "write_raw", lambda *_a: False):
            self.assertIsNone(self._run())
        self.assertEqual(self.filed, [])
        self.assertFalse((self.d / h.HANDOFF_MARKER).exists())

    def test_a_failed_filing_leaves_the_next_tick_to_retry(self):
        _board(self.board, clusters=_all_done())
        with mock.patch.object(h, "file_prioritize", lambda _p, _stale=None: None):
            self.assertIsNone(self._run())
        self.assertFalse((self.d / h.HANDOFF_MARKER).exists())
        self.assertEqual(self._run(), "t_rank1")

    def test_the_sandbox_copy_is_written_as_the_terminal_login(self):
        _board(self.board, clusters=_all_done())
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            return types.SimpleNamespace(returncode=0, stderr="")

        self.sandbox.sandbox_enabled = lambda: True
        self.sandbox.run = run
        self.assertEqual(self._run(), "t_rank1")
        argv, kw = calls[0]
        self.assertEqual(argv[-1], h.RAW_PATH)
        self.assertEqual(kw["principal"], "agent")
        inventory_findings.parse_block(kw["stdin"])
        self.assertFalse((self.d / "INVENTORY.raw.md").exists())


if __name__ == "__main__":
    unittest.main()
