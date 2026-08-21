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
                    json.dumps({"check": "x", "cluster": "prod"}),
                    json.dumps({"check": "x", "cluster": "p", "object": "o", "title": "t", "sev": "hi"}),
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
        code = self.register({"complete_clusters": ["prod", "dev"], "scores": {"f001": SCORE, "f002": SCORE}})
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual({s[2]["cluster"] for s in self.sent}, {"prod", "dev"})
        self.assertTrue(all(s[2]["complete"] for s in self.sent))

    def test_scope_is_omitted_for_a_cluster_not_declared_complete(self):
        self.extract()
        self.register({"complete_clusters": ["prod"], "scores": {"f001": SCORE, "f002": SCORE}})
        scopes = {s[1][0]["cluster"]: s[2] for s in self.sent}
        self.assertIsNone(scopes["dev"])
        self.assertEqual(scopes["prod"], {"cluster": "prod", "complete": True})

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
        self.assertEqual(self.register({"complete_clusters": ["prod"], "scores": {}}), 0)
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


class RankedTests(unittest.TestCase):
    def setUp(self):
        self.real = inv.fetch_ranked
        self.addCleanup(setattr, inv, "fetch_ranked", self.real)

    def test_the_order_and_the_total_are_printed(self):
        inv.fetch_ranked = lambda endpoint: [
            {
                "rank_score": 90,
                "severity": "major",
                "check": "probes-readiness",
                "cluster": "prod",
                "namespace": "payments",
                "object": "api",
                "title": "no readinessProbe",
                "actionable": True,
            }
        ]
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(inv.main(["ranked"]), 0)
        self.assertIn("total: 1", out.getvalue())
        self.assertIn("prod/payments/api", out.getvalue())

    def test_the_flags_the_report_rules_key_off_are_shown(self):
        inv.fetch_ranked = lambda endpoint: [
            {
                "rank_score": 3,
                "severity": "minor",
                "check": "no-memory-limit",
                "cluster": "prod",
                "namespace": "kube-system",
                "object": "kube-dns",
                "title": "no limit",
                "provider_managed": True,
                "actionable": False,
            }
        ]
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            inv.main(["ranked"])
        self.assertIn("provider_managed,not_actionable", out.getvalue())

    def test_an_unreachable_queue_exits_5_rather_than_tracing(self):
        def boom(endpoint):
            raise OSError("connection refused")

        inv.fetch_ranked = boom
        self.assertEqual(inv.main(["ranked"]), inv.EXIT_POST_FAILED)


if __name__ == "__main__":
    unittest.main()
