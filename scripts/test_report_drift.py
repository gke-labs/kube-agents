"""report_drift.py reads the plan as Terraform writes it, colour codes and all."""

import importlib.util
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
REPORT_DRIFT = REPO / "scripts" / "release" / "report_drift.py"
# Lines as the 2026-10-24 staging plan log carried them: `terraform plan` runs without
# `-no-color`, so every line opens with an escape sequence.
COLOURED_PLAN = (
    '\x1b[1m  # google_project_service.required["gkehub.googleapis.com"]\x1b[0m will be created\n'
    "\x1b[1m  # module.chat_pubsub[0].data.google_project.this\x1b[0m will be read during apply\n"
    "\x1b[1m  # module.chat_pubsub[0].google_pubsub_topic_iam_member.gsuiteaddons_publisher\x1b[0m"
    " must be \x1b[1m\x1b[31mreplaced\x1b[0m\n"
    "\x1b[1m  # module.gke_cluster.terraform_data.create_after\x1b[0m will be updated in-place\n"
    "\x1b[1mPlan:\x1b[0m \x1b[0m4 to add, 1 to change, 2 to destroy.\n"
)
PLAIN_PLAN = (
    "  # google_project_service.required[\"gkehub.googleapis.com\"] will be created\n"
    "  # module.gke_cluster.terraform_data.create_after will be updated in-place\n"
    "Plan: 1 to add, 1 to change, 0 to destroy.\n"
)


def load_report_drift():
    spec = importlib.util.spec_from_file_location("report_drift", REPORT_DRIFT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SummariseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report_drift = load_report_drift()

    def test_a_coloured_plan_yields_its_totals_actions_and_destroy_flag(self):
        totals, actions, destructive = self.report_drift.summarise(COLOURED_PLAN)
        self.assertEqual(totals, "Plan: 4 to add, 1 to change, 2 to destroy.")
        self.assertEqual(
            actions,
            [
                ('google_project_service.required["gkehub.googleapis.com"]', "created"),
                ("module.chat_pubsub[0].data.google_project.this", "read during apply"),
                ("module.chat_pubsub[0].google_pubsub_topic_iam_member.gsuiteaddons_publisher", "replaced"),
                ("module.gke_cluster.terraform_data.create_after", "updated in-place"),
            ],
        )
        self.assertTrue(destructive)

    def test_a_plain_plan_reads_the_same_as_before(self):
        totals, actions, destructive = self.report_drift.summarise(PLAIN_PLAN)
        self.assertEqual(totals, "Plan: 1 to add, 1 to change, 0 to destroy.")
        self.assertEqual(len(actions), 2)
        self.assertFalse(destructive)

    def test_the_issue_body_carries_the_warning_and_the_totals_for_a_coloured_plan(self):
        totals, actions, destructive = self.report_drift.summarise(COLOURED_PLAN)
        body = self.report_drift.body_for("staging", "https://example.invalid/run/1", totals, actions, destructive)
        self.assertIn("DESTROY or REPLACE", body)
        self.assertIn("Plan: 4 to add, 1 to change, 2 to destroy.", body)
        self.assertIn("gsuiteaddons_publisher", body)
        self.assertNotIn("\x1b", body)


if __name__ == "__main__":
    unittest.main()
