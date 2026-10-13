import io
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import inventory_findings as inv

RUBRIC = {"B": 3, "L": 6, "detect": 3, "recover": 2, "C": 1.0}
SCORE = {
    "rubric": RUBRIC,
    "recommendation": {"action": "add a readinessProbe", "rationale": "traffic", "risk": "5xx"},
    "remediation": {"kind": "manifest", "path": "k8s/api.yaml", "note": "add the probe"},
    "verification": {"kind": "kubectl", "command": "kubectl get deploy api", "still_failing_when": "empty"},
}


def raw_file(*lines: str) -> str:
    body = "\n".join(lines)
    return f"# Report\n\nsome prose\n\n```findings\n{body}\n```\n\ntrailing prose\n"


def item_line(**overrides) -> str:
    item = {
        "check": "probes-readiness",
        "project": "acme",
        "cluster": "prod",
        "namespace": "payments",
        "object": "api",
        "title": "no readinessProbe on api",
    }
    item.update(overrides)
    return json.dumps(item)


class ParseBlockTests(unittest.TestCase):
    def test_ids_are_assigned_in_file_order(self):
        items = inv.parse_block(raw_file(item_line(object="api"), item_line(object="web")))
        self.assertEqual([i["id"] for i in items], ["f001", "f002"])
        self.assertEqual([i["object"] for i in items], ["api", "web"])

    def test_optional_fields_survive_and_absent_ones_are_omitted(self):
        items = inv.parse_block(raw_file(item_line(detail="observed empty", severity_hint="high")))
        self.assertEqual(items[0]["detail"], "observed empty")
        self.assertEqual(items[0]["severity_hint"], "high")
        self.assertNotIn("evidence", items[0])

    def test_blank_and_comment_lines_are_skipped(self):
        items = inv.parse_block(raw_file("", "// a note", "# another", item_line()))
        self.assertEqual(len(items), 1)

    def test_several_blocks_are_one_list(self):
        text = raw_file(item_line(object="api")) + "\n```findings\n" + item_line(object="web") + "\n```\n"
        self.assertEqual([i["object"] for i in inv.parse_block(text)], ["api", "web"])

    def test_longer_fence_and_trailing_space_are_accepted(self):
        text = f"````findings  \n{item_line()}\n````\n"
        self.assertEqual(len(inv.parse_block(text)), 1)

    def test_no_block_is_its_own_exit_code(self):
        with self.assertRaises(inv.Failure) as caught:
            inv.parse_block("# Report\n\njust prose, priority 1: fix everything\n")
        self.assertEqual(caught.exception.code, inv.EXIT_NO_BLOCK)

    def test_an_empty_block_is_a_clean_fleet_not_an_error(self):
        self.assertEqual(inv.parse_block("```findings\n\n```\n"), [])

    def test_every_bad_line_is_reported_at_once(self):
        with self.assertRaises(inv.Failure) as caught:
            inv.parse_block(
                raw_file(
                    "{not json",
                    json.dumps({"check": "x", "project": "acme", "cluster": "prod"}),
                    json.dumps({"check": "x", "project": "acme", "cluster": "p", "object": "o", "title": "t", "sev": "hi"}),
                )
            )
        self.assertEqual(caught.exception.code, inv.EXIT_BAD_BLOCK)
        self.assertEqual(len(caught.exception.errors), 3)
        joined = " ".join(caught.exception.errors)
        self.assertIn("not valid JSON", joined)
        self.assertIn("missing object, title", joined)
        self.assertIn("unknown field(s) sev", joined)

    def test_error_line_numbers_are_the_raw_files_own(self):
        with self.assertRaises(inv.Failure) as caught:
            inv.parse_block(raw_file("{not json"))
        # header, blank, prose, blank, fence -> the first body line is line 6.
        self.assertIn("line 6:", caught.exception.errors[0])

    def test_one_bad_line_discards_the_whole_extract(self):
        with self.assertRaises(inv.Failure):
            inv.parse_block(raw_file(item_line(), "{not json"))

    def test_a_fence_indented_inside_a_list_item_is_still_the_block(self):
        # inventory.md ships its only example indented three spaces under a
        # numbered item, so this is what a sweep copying it emits.
        text = f"1. **Findings:**\n\n   ```findings\n   {item_line()}\n   ```\n"
        self.assertEqual(len(inv.parse_block(text)), 1)

    def test_a_longer_closing_fence_still_closes_the_block(self):
        self.assertEqual(len(inv.parse_block(f"```findings\n{item_line()}\n`````\n")), 1)

    def test_the_no_block_exit_code_does_not_collide_with_an_argparse_usage_error(self):
        # argparse exits 2 for a mistyped flag; the SOP reads EXIT_NO_BLOCK as
        # "the sweep is broken" and blocks the onboarding card over it.
        self.assertNotIn(2, {inv.EXIT_NO_BLOCK, inv.EXIT_BAD_BLOCK, inv.EXIT_INCOMPLETE, inv.EXIT_POST_FAILED})
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as caught:
                inv.main(["register", "--items", "x.json"])
        self.assertEqual(caught.exception.code, 2)

    def test_a_line_without_a_project_is_a_bad_line(self):
        # The queue keys on it; catching the omission here costs one edit,
        # catching it at register costs the whole batch a round trip.
        line = dict(json.loads(item_line()))
        del line["project"]
        with self.assertRaises(inv.Failure) as caught:
            inv.parse_block(raw_file(json.dumps(line)))
        self.assertIn("missing project", " ".join(caught.exception.errors))

    def test_a_non_string_identity_field_is_a_bad_line_not_a_stringified_one(self):
        with self.assertRaises(inv.Failure) as caught:
            inv.parse_block(raw_file(item_line(cluster=["prod", "dev"])))
        self.assertIn("cluster must be a string", " ".join(caught.exception.errors))

    def test_provider_managed_as_a_string_is_rejected_rather_than_read_as_true(self):
        with self.assertRaises(inv.Failure) as caught:
            inv.parse_block(raw_file(item_line(provider_managed="false")))
        self.assertIn("provider_managed must be true or false", " ".join(caught.exception.errors))


class BuildPayloadsTests(unittest.TestCase):
    def setUp(self):
        self.items = inv.parse_block(raw_file(item_line(object="api"), item_line(object="web")))

    def test_a_complete_score_set_builds_one_payload_each(self):
        payloads = inv.build_payloads(self.items, {"f001": SCORE, "f002": SCORE})
        self.assertEqual(len(payloads), 2)
        self.assertEqual({p["source"] for p in payloads}, {"inventory"})
        self.assertEqual(sorted(p["object"] for p in payloads), ["api", "web"])

    def test_a_string_flag_in_the_scores_file_is_rejected(self):
        """The SOP has the model author these freehand, once per run."""
        for key, value in (("provider_managed", "false"), ("actionable", None)):
            with self.subTest(key=key):
                scores = {"f001": {**SCORE, key: value}, "f002": SCORE}
                with self.assertRaises(inv.Failure) as caught:
                    inv.build_payloads(self.items, scores)
                self.assertEqual(caught.exception.code, inv.EXIT_INCOMPLETE)
                self.assertIn(
                    f"{key} must be true or false", " ".join(caught.exception.errors)
                )

    def test_an_unscored_finding_blocks_the_whole_batch(self):
        with self.assertRaises(inv.Failure) as caught:
            inv.build_payloads(self.items, {"f001": SCORE})
        self.assertEqual(caught.exception.code, inv.EXIT_INCOMPLETE)
        self.assertIn("unscored: f002", " ".join(caught.exception.errors))

    def test_a_null_score_counts_as_unscored_rather_than_dropping_the_finding(self):
        with self.assertRaises(inv.Failure) as caught:
            inv.build_payloads(self.items, {"f001": SCORE, "f002": None})
        self.assertEqual(caught.exception.code, inv.EXIT_INCOMPLETE)
        self.assertIn("unscored: f002", " ".join(caught.exception.errors))

    def test_a_score_for_an_unknown_id_is_an_error(self):
        with self.assertRaises(inv.Failure) as caught:
            inv.build_payloads(self.items, {"f001": SCORE, "f002": SCORE, "f009": SCORE})
        self.assertIn("f009: scored but not an extracted id", caught.exception.errors)

    def test_every_score_error_is_reported_in_one_pass(self):
        broken = dict(SCORE)
        del broken["verification"]
        with self.assertRaises(inv.Failure) as caught:
            inv.build_payloads(self.items, {"f001": broken, "f002": {"rubric": RUBRIC, "title": "x"}})
        joined = " ".join(caught.exception.errors)
        self.assertIn("f001: missing verification", joined)
        self.assertIn("f002: unknown field(s) title", joined)

    def test_a_rubric_the_queue_would_reject_fails_before_the_wire(self):
        bad = dict(SCORE, rubric={"B": 4, "L": 6, "detect": 3, "recover": 2, "C": 1.0})
        with self.assertRaises(inv.Failure) as caught:
            inv.build_payloads(self.items, {"f001": bad, "f002": SCORE})
        self.assertIn("f001:", " ".join(caught.exception.errors))

    def test_the_model_cannot_supply_source_or_identity(self):
        with self.assertRaises(inv.Failure) as caught:
            inv.build_payloads(self.items, {"f001": dict(SCORE, cluster="other"), "f002": SCORE})
        self.assertIn("unknown field(s) cluster", " ".join(caught.exception.errors))

    def test_provider_managed_from_the_sweep_reaches_the_payload(self):
        items = inv.parse_block(raw_file(item_line(provider_managed=True)))
        payloads = inv.build_payloads(items, {"f001": SCORE})
        self.assertTrue(payloads[0]["provider_managed"])

    def test_optional_judgement_fields_pass_through(self):
        payloads = inv.build_payloads(
            self.items,
            {"f001": dict(SCORE, actionable=False, root_cause="never templated"), "f002": SCORE},
        )
        first = next(p for p in payloads if p["object"] == "api")
        self.assertFalse(first["actionable"])
        self.assertEqual(first["root_cause"], "never templated")


class RegisterTests(unittest.TestCase):
    """The command end to end, with the POST captured rather than sent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "raw.md").write_text(
            raw_file(item_line(object="api"), item_line(cluster="dev", object="web")),
            encoding="utf-8",
        )
        self.items = self.dir / "items.json"
        self.scores = self.dir / "scores.json"
        self.sent = []

        def capture(endpoint, findings, scope):
            self.sent.append((endpoint, findings, scope))
            return {"results": [{"id": f["object"], "outcome": "created"} for f in findings]}

        self.real_post = inv.post_batch
        inv.post_batch = capture
        self.addCleanup(setattr, inv, "post_batch", self.real_post)

    def extract(self):
        return inv.main(["extract", "--raw", str(self.dir / "raw.md"), "--out", str(self.items)])

    def register(self, scores):
        self.scores.write_text(json.dumps(scores), encoding="utf-8")
        return inv.main(["register", "--items", str(self.items), "--scores", str(self.scores)])

    def test_extract_then_register_sends_one_batch_per_cluster(self):
        self.assertEqual(self.extract(), 0)
        self.assertEqual(json.loads(self.items.read_text())["total"], 2)
        code = self.register(
            {"complete_clusters": ["acme/prod", "acme/dev"], "scores": {"f001": SCORE, "f002": SCORE}}
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual({s[2]["cluster"] for s in self.sent}, {"prod", "dev"})
        self.assertTrue(all(s[2]["complete"] for s in self.sent))

    def test_scope_is_omitted_for_a_cluster_not_declared_complete(self):
        self.extract()
        self.register({"complete_clusters": ["acme/prod"], "scores": {"f001": SCORE, "f002": SCORE}})
        scopes = {s[1][0]["cluster"]: s[2] for s in self.sent}
        self.assertIsNone(scopes["dev"])
        self.assertEqual(scopes["prod"], {"project": "acme", "cluster": "prod", "complete": True})

    def test_an_unmatched_complete_clusters_entry_is_warned_about(self):
        # The likely shape is the old bare cluster name; matching nothing must
        # not be silent, because the absence rule quietly not running is what
        # keeps a fixed critical nagging forever.
        self.extract()
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = self.register({"complete_clusters": ["prod"], "scores": {"f001": SCORE, "f002": SCORE}})
        self.assertEqual(code, 0)
        self.assertIn("complete_clusters entry 'prod' matched no registered batch", out.getvalue())
        self.assertTrue(all(s[2] is None for s in self.sent))

    def test_a_malformed_scores_file_is_a_listed_error_not_a_traceback(self):
        self.extract()
        self.scores.write_text('{"scores": {"f001": {},}}', encoding="utf-8")
        code = inv.main(["register", "--items", str(self.items), "--scores", str(self.scores)])
        self.assertEqual(code, inv.EXIT_INCOMPLETE)
        self.assertEqual(self.sent, [])

    def test_an_absent_items_file_is_a_listed_error_not_a_traceback(self):
        self.scores.write_text(json.dumps({"scores": {}}), encoding="utf-8")
        code = inv.main(["register", "--items", str(self.dir / "gone.json"), "--scores", str(self.scores)])
        self.assertEqual(code, inv.EXIT_INCOMPLETE)
        self.assertEqual(self.sent, [])

    def test_one_cluster_failing_leaves_the_other_registered_and_says_so(self):
        self.extract()

        def half_fail(endpoint, findings, scope):
            if findings[0]["cluster"] == "prod":
                raise OSError("connection reset")
            self.sent.append((endpoint, findings, scope))
            return {"results": [{"id": f["object"], "outcome": "created"} for f in findings]}

        inv.post_batch = half_fail
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = self.register({"scores": {"f001": SCORE, "f002": SCORE}})
        self.assertEqual(code, inv.EXIT_POST_FAILED)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("registered 1 of 2", out.getvalue())

    def test_an_incomplete_score_set_sends_nothing(self):
        self.extract()
        code = self.register({"scores": {"f001": SCORE}})
        self.assertEqual(code, inv.EXIT_INCOMPLETE)
        self.assertEqual(self.sent, [])

    def test_a_scores_file_of_the_wrong_shape_is_rejected(self):
        self.extract()
        self.assertEqual(self.register({"f001": SCORE}), inv.EXIT_INCOMPLETE)
        self.assertEqual(self.sent, [])

    def test_extract_on_a_raw_file_without_a_block_writes_no_items(self):
        (self.dir / "raw.md").write_text("# Report\n\nPriority 1 — fix the probes\n", encoding="utf-8")
        self.assertEqual(self.extract(), inv.EXIT_NO_BLOCK)
        self.assertFalse(self.items.exists())

    def test_a_clean_fleet_extracts_zero_and_registers_nothing(self):
        (self.dir / "raw.md").write_text("# Report\n\n```findings\n```\n", encoding="utf-8")
        self.assertEqual(self.extract(), 0)
        self.assertEqual(json.loads(self.items.read_text())["total"], 0)
        self.assertEqual(self.register({"complete_clusters": ["acme/prod"], "scores": {}}), 0)
        self.assertEqual(self.sent, [])

    def test_a_missing_raw_file_exits_rather_than_traces(self):
        code = inv.main(["extract", "--raw", str(self.dir / "absent.md"), "--out", str(self.items)])
        self.assertEqual(code, inv.EXIT_NO_BLOCK)

    def test_a_failed_post_reports_the_cluster_and_the_exit_code(self):
        self.extract()

        def boom(endpoint, findings, scope):
            raise OSError("connection refused")

        inv.post_batch = boom
        code = self.register({"scores": {"f001": SCORE, "f002": SCORE}})
        self.assertEqual(code, inv.EXIT_POST_FAILED)

    def test_dry_run_validates_everything_and_sends_nothing(self):
        self.extract()
        self.scores.write_text(json.dumps({"scores": {"f001": SCORE, "f002": SCORE}}), encoding="utf-8")
        code = inv.main(
            ["register", "--items", str(self.items), "--scores", str(self.scores), "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [])


# 288 critical; 90 critical by the floor; 120 major; 90 major; 36 critical by the floor.
CRITICAL = {"B": 8, "L": 6, "detect": 3, "recover": 3, "C": 1.0}
FLOORED = {"B": 3, "L": 10, "detect": 1, "recover": 2, "C": 1.0}
MAJOR_HIGH = {"B": 5, "L": 4, "detect": 3, "recover": 3, "C": 1.0}
MAJOR = RUBRIC
FLOORED_LOW = {"B": 3, "L": 10, "detect": 1, "recover": 1, "C": 0.6}


def batch(*specs) -> tuple[list[dict], dict]:
    """Extracted items and their scores from (rubric, item overrides[, score overrides]) specs."""
    items, scores = [], {}
    for index, spec in enumerate(specs, 1):
        rubric, overrides = spec[0], spec[1]
        item = json.loads(item_line(**overrides))
        item["id"] = f"f{index:03d}"
        items.append(item)
        scores[item["id"]] = {**SCORE, "rubric": rubric, **(spec[2] if len(spec) > 2 else {})}
    return items, scores


def criticals(count: int) -> list:
    # Distinct checks, so each is its own item, scored 288 down to a floored 90.
    rubrics = [CRITICAL, {**CRITICAL, "L": 4}, {**CRITICAL, "B": 5}, FLOORED, FLOORED, FLOORED]
    return [(rubrics[i], {"check": f"check-{i}", "object": f"obj-{i}"}) for i in range(count)]


def refs(shown: list[list[dict]]) -> list[list[str]]:
    return [[row["ref"] for row in members] for members in shown]


class SelectItemsTests(unittest.TestCase):
    def select(self, specs, limit=2, exclude=frozenset()):
        items, scores = batch(*specs)
        return inv.select_items(items, scores, limit, exclude)

    def test_six_criticals_list_the_top_two_and_defer_the_rest(self):
        shown, deferred, others = self.select(criticals(6) + [(MAJOR, {"check": "major", "object": "m"})])
        self.assertEqual(refs(shown), [["f001"], ["f002"]])
        self.assertEqual((deferred, others), (4, 1))

    def test_exactly_two_criticals_are_both_listed(self):
        shown, deferred, others = self.select(criticals(2))
        self.assertEqual(refs(shown), [["f001"], ["f002"]])
        self.assertEqual((deferred, others), (0, 0))

    def test_one_critical_is_listed_alone_and_never_padded(self):
        specs = criticals(1) + [(MAJOR_HIGH, {"check": f"major-{i}", "object": "m"}) for i in range(3)]
        shown, deferred, others = self.select(specs)
        self.assertEqual(refs(shown), [["f001"]])
        self.assertEqual((deferred, others), (0, 3))

    def test_no_critical_lists_nothing(self):
        shown, deferred, others = self.select([(MAJOR_HIGH, {"check": "a"}), (MAJOR, {"check": "b"})])
        self.assertEqual((shown, deferred, others), ([], 0, 2))

    def test_a_limit_of_zero_lists_none(self):
        shown, deferred, others = self.select(criticals(2), limit=0)
        self.assertEqual((shown, deferred, others), ([], 2, 0))

    def test_a_limit_of_three_lists_three(self):
        shown, deferred, _ = self.select(criticals(6), limit=3)
        self.assertEqual(refs(shown), [["f001"], ["f002"], ["f003"]])
        self.assertEqual(deferred, 3)

    def test_a_gathered_line_is_one_item_of_its_most_severe_rows_class(self):
        # One check on one cluster: a major scoring 120 and a floored critical
        # scoring 90 are one critical item, at the major's place, ahead of a
        # critical item scoring 36.
        specs = [
            (FLOORED_LOW, {"check": "lone-critical", "object": "x"}),
            (FLOORED, {"check": "crashloop", "object": "api"}),
            (MAJOR_HIGH, {"check": "crashloop", "object": "web"}),
            (MAJOR, {"check": "crashloop", "cluster": "dev", "object": "api"}),
        ]
        shown, deferred, others = self.select(specs, limit=1)
        self.assertEqual(refs(shown), [["f003", "f002"]])
        self.assertEqual([row["severity"] for row in shown[0]], ["major", "critical"])
        # The lone critical waits; the same check on another cluster is another item.
        self.assertEqual((deferred, others), (1, 1))

    def test_a_provider_managed_observation_is_dropped_and_a_fault_is_listed(self):
        specs = [
            (CRITICAL, {"check": "observation", "namespace": "kube-system", "object": "kube-dns"}, {"actionable": False}),
            (FLOORED, {"check": "fault", "namespace": "kube-system", "object": "konnectivity"}),
        ]
        shown, deferred, others = self.select(specs)
        self.assertEqual(refs(shown), [["f002"]])
        self.assertEqual((deferred, others), (0, 1))

    def test_provider_managed_observations_are_counted_by_line(self):
        # Three observations of one check on one cluster are one line; the
        # same check on another cluster is another, as the nudge counts them.
        observation = {"actionable": False}
        specs = [
            (MAJOR, {"check": "observation", "namespace": "kube-system", "object": name}, observation)
            for name in ("kube-dns", "konnectivity", "metrics-server")
        ] + [(MAJOR, {"check": "observation", "cluster": "dev", "namespace": "kube-system"}, observation)]
        shown, deferred, others = self.select(specs)
        self.assertEqual((shown, deferred, others), ([], 0, 2))

    def test_a_line_with_an_observation_and_an_ordinary_row_counts_once(self):
        # One check on one cluster, on a kube-system workload and on a user one.
        specs = [
            (MAJOR, {"check": "observation", "namespace": "kube-system", "object": "kube-dns"}, {"actionable": False}),
            (MAJOR, {"check": "observation", "namespace": "payments", "object": "api"}),
        ]
        shown, deferred, others = self.select(specs)
        self.assertEqual((shown, deferred, others), ([], 0, 1))

    def test_an_excluded_row_is_neither_listed_nor_counted(self):
        items, scores = batch(*criticals(3))
        dismissed = inv.fq.derive_finding_id("check-0", "acme", "prod", "payments", "obj-0")
        shown, deferred, others = inv.select_items(items, scores, 2, frozenset({dismissed}))
        self.assertEqual(refs(shown), [["f002"], ["f003"]])
        self.assertEqual((deferred, others), (0, 0))

    def test_the_order_does_not_depend_on_the_input_order(self):
        # Equal scores: the queue's tie-break, by object, decides.
        specs = [(CRITICAL, {"check": f"c{i}", "object": name}) for i, name in enumerate(("zeta", "alpha", "mid"))]
        forward = self.select(specs)[0]
        backward = self.select(list(reversed(specs)))[0]
        self.assertEqual([m[0]["object"] for m in forward], ["alpha", "mid"])
        self.assertEqual([m[0]["object"] for m in backward], ["alpha", "mid"])

    def test_an_unscored_item_selects_nothing(self):
        items, scores = batch(*criticals(2))
        del scores["f002"]
        with self.assertRaises(inv.Failure) as caught:
            inv.select_items(items, scores, 2)
        self.assertEqual(caught.exception.code, inv.EXIT_INCOMPLETE)


class ReadLimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "limits.json"

    def read(self, text=None):
        if text is not None:
            self.path.write_text(text, encoding="utf-8")
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            limits = inv.read_limits(str(self.path))
        return limits, err.getvalue()

    def test_the_hand_offs_limits_are_read(self):
        limits, err = self.read(json.dumps({"first_report_criticals": 1, "noncritical_after_hour": 9}))
        self.assertEqual((limits.first_report_criticals, limits.noncritical_after_hour), (1, 9))
        self.assertEqual(err, "")

    def test_a_missing_file_is_the_default(self):
        limits, err = self.read()
        self.assertEqual(limits.first_report_criticals, inv.fq.DEFAULT_FIRST_REPORT_CRITICALS)
        self.assertIn("using the default limits", err)

    def test_an_unusable_file_or_value_is_the_default(self):
        for text in ("{not json", "[2]", '{"first_report_criticals": -1}', '{"first_report_criticals": "x"}',
                     '{"first_report_criticals": true}', '{"first_report_criticals": 1.5}'):
            with self.subTest(text=text):
                limits, err = self.read(text)
                self.assertEqual(limits.first_report_criticals, inv.fq.DEFAULT_FIRST_REPORT_CRITICALS)
                self.assertTrue(err)


    def test_bytes_that_are_not_utf8_are_the_default(self):
        self.path.write_bytes(b"\xff")
        limits, err = self.read()
        self.assertEqual(limits.first_report_criticals, inv.fq.DEFAULT_FIRST_REPORT_CRITICALS)
        self.assertIn("cannot read", err)


class SelectCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.shown = self.dir / "shown.json"

    def run_select(self, specs, limits=None, *extra):
        items, scores = batch(*specs)
        (self.dir / "items.json").write_text(json.dumps({"items": items}), encoding="utf-8")
        (self.dir / "scores.json").write_text(json.dumps({"scores": scores}), encoding="utf-8")
        if limits is not None:
            (self.dir / "limits.json").write_text(json.dumps(limits), encoding="utf-8")
        argv = [
            "select",
            "--items", str(self.dir / "items.json"),
            "--scores", str(self.dir / "scores.json"),
            "--limits", str(self.dir / "limits.json"),
            "--out", str(self.shown),
            *extra,
        ]
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = inv.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_the_listed_items_and_every_row_id_are_written(self):
        specs = criticals(3) + [(FLOORED, {"check": "check-0", "object": "obj-0b"})]
        code, out, _ = self.run_select(specs, {"first_report_criticals": 2})
        self.assertEqual(code, 0)
        record = json.loads(self.shown.read_text(encoding="utf-8"))
        self.assertEqual(
            record,
            {
                "items": [
                    {"class": "critical", "ids": ["check-0.acme.prod.payments.obj-0", "check-0.acme.prod.payments.obj-0b"]},
                    {"class": "critical", "ids": ["check-1.acme.prod.payments.obj-1"]},
                ]
            },
        )
        self.assertIn("list exactly 2 critical items, in this order", out)
        self.assertIn("f001", out)
        self.assertIn("f004", out)
        self.assertIn("roll-up: 1 more item, 1 of them critical", out)
        # The two listed fill the delivery day's allowance of 2.
        self.assertIn(
            "pace: the critical items not listed are added in chat from 12:00 UTC the day after the report "
            "arrives, at most 2 a day",
            out,
        )

    def test_criticals_left_out_start_the_same_day_while_the_allowance_lasts(self):
        _, out, _ = self.run_select(criticals(3), {"first_report_criticals": 1, "daily_criticals": 2})
        self.assertIn("pace: the critical items not listed are added in chat from 12:00 UTC, at most 2 a day", out)

    def test_a_daily_limit_of_zero_gives_no_arrival_time(self):
        _, out, _ = self.run_select(criticals(3), {"daily_criticals": 0})
        self.assertIn("pace: the critical items not listed are not added in chat; the daily limit is 0", out)
        _, out, _ = self.run_select([(MAJOR, {"check": "a"})], {"noncritical_max": 0})
        self.assertIn("pace: non-critical items are not added in chat; the daily limit is 0", out)

    def test_no_critical_says_so_and_when_the_rest_arrives(self):
        code, out, _ = self.run_select(
            [(MAJOR, {"check": "a"}), (MAJOR, {"check": "b"})], {"noncritical_after_hour": 15, "noncritical_max": 4}
        )
        self.assertEqual(code, 0)
        self.assertIn("list no item; there are no critical findings", out)
        self.assertIn("roll-up: 2 more items, none of them critical", out)
        self.assertIn("pace: non-critical items are added in chat from 15:00 UTC, at most 4 a day", out)
        self.assertEqual(json.loads(self.shown.read_text(encoding="utf-8")), {"items": []})

    def test_a_limit_of_zero_says_so(self):
        _, out, _ = self.run_select(criticals(2), {"first_report_criticals": 0})
        self.assertIn("list no item; the limit is 0", out)
        self.assertIn("roll-up: 2 more items, 2 of them critical", out)

    def test_nothing_left_over_means_no_roll_up_line(self):
        _, out, _ = self.run_select(criticals(2))
        self.assertIn("roll-up: none; the report has no roll-up line", out)
        self.assertNotIn("pace:", out)

    def test_no_limits_file_uses_the_default(self):
        code, out, err = self.run_select(criticals(3))
        self.assertEqual(code, 0)
        self.assertIn("list exactly 2 critical items", out)
        self.assertIn("using the default limits", err)

    def test_exclude_takes_a_suppressed_queue_id(self):
        dismissed = inv.fq.derive_finding_id("check-0", "acme", "prod", "payments", "obj-0")
        _, out, _ = self.run_select(criticals(2), None, "--exclude", dismissed)
        self.assertIn("list exactly 1 critical item,", out)
        self.assertNotIn("f001", out)

    def test_a_clean_fleet_needs_no_scores_file(self):
        # Step 2 sends a clean fleet past scoring, so no scores file exists.
        (self.dir / "items.json").write_text(json.dumps({"items": []}), encoding="utf-8")
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                unittest.mock.patch("sys.stderr", new_callable=io.StringIO):
            code = inv.main(["select", "--items", str(self.dir / "items.json"),
                             "--scores", str(self.dir / "absent.json"), "--out", str(self.shown)])
        self.assertEqual(code, 0)
        self.assertIn("list no item; there are no critical findings", out.getvalue())
        self.assertIn("roll-up: none", out.getvalue())
        self.assertEqual(json.loads(self.shown.read_text(encoding="utf-8")), {"items": []})

    def test_an_incomplete_score_set_writes_nothing(self):
        items, scores = batch(*criticals(2))
        del scores["f001"]
        (self.dir / "items.json").write_text(json.dumps({"items": items}), encoding="utf-8")
        (self.dir / "scores.json").write_text(json.dumps({"scores": scores}), encoding="utf-8")
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = inv.main(["select", "--items", str(self.dir / "items.json"),
                             "--scores", str(self.dir / "scores.json"), "--out", str(self.shown)])
        self.assertEqual(code, inv.EXIT_INCOMPLETE)
        self.assertIn("Nothing was selected", err.getvalue())
        self.assertFalse(self.shown.exists())


if __name__ == "__main__":
    unittest.main()
