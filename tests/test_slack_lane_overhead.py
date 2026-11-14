"""hack/slack-lane/overhead.py turns the A2A gateway's Slack metrics into a night's Slack overhead,
and hack/slack-lane/run.sh scrapes them once its canaries have run.

The run test uses a fake gcloud (the three secrets), a fake uv (no case runs) and a fake kubectl
(two gateway pods, one serving the Slack series), so the scrape is pinned without a cluster.

Run: python3 -m unittest discover -s tests -p 'test_slack_lane_overhead.py' -v
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LANE = _REPO_ROOT / "hack" / "slack-lane"

spec = importlib.util.spec_from_file_location("slack_lane_overhead", _LANE / "overhead.py")
overhead = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overhead)

METRICS = """# HELP kubeagents_a2a_gateway_slack_ingress_lag_seconds lag
# TYPE kubeagents_a2a_gateway_slack_ingress_lag_seconds histogram
kubeagents_a2a_gateway_slack_ingress_lag_seconds_bucket{le="0.5"} 2
kubeagents_a2a_gateway_slack_ingress_lag_seconds_bucket{le="+Inf"} 3
kubeagents_a2a_gateway_slack_ingress_lag_seconds_sum 1.5
kubeagents_a2a_gateway_slack_ingress_lag_seconds_count 3
kubeagents_a2a_gateway_slack_api_request_duration_seconds_sum{method="chat.postMessage",outcome="ok"} 2.4
kubeagents_a2a_gateway_slack_api_request_duration_seconds_count{method="chat.postMessage",outcome="ok"} 6
kubeagents_a2a_gateway_slack_api_request_duration_seconds_sum{method="chat.postMessage",outcome="rate_limited"} 9
kubeagents_a2a_gateway_slack_api_request_duration_seconds_count{method="chat.postMessage",outcome="rate_limited"} 1
kubeagents_a2a_gateway_slack_api_request_duration_seconds_sum{method="chat.update",outcome="ok"} 5
kubeagents_a2a_gateway_slack_api_request_duration_seconds_count{method="chat.update",outcome="ok"} 5
kubeagents_a2a_gateway_slack_first_ack_seconds_bucket{le="0.25"} 0
kubeagents_a2a_gateway_slack_first_ack_seconds_bucket{le="0.5"} 2
kubeagents_a2a_gateway_slack_first_ack_seconds_bucket{le="1"} 4
kubeagents_a2a_gateway_slack_first_ack_seconds_bucket{le="+Inf"} 4
kubeagents_a2a_gateway_slack_first_ack_seconds_sum 2.2
kubeagents_a2a_gateway_slack_first_ack_seconds_count 4
"""


class OverheadTest(unittest.TestCase):
    def test_the_nights_overhead(self):
        got = overhead.overhead(METRICS)
        self.assertEqual(0.5, got["delivery_lag_s"])
        self.assertEqual(0.4, got["post_s"], "only chat.postMessage calls Slack answered")
        self.assertEqual(0.5, got["first_ack_p50_s"])
        self.assertEqual(0.95, got["first_ack_p95_s"])
        self.assertEqual({"delivery_lag": 3, "post": 6, "first_ack": 4}, got["samples"])

    def test_no_samples_reads_as_none(self):
        got = overhead.overhead("")
        self.assertEqual((None, None, None, None), (got["delivery_lag_s"], got["post_s"], got["first_ack_p50_s"], got["first_ack_p95_s"]))

    def test_two_pods_sum(self):
        self.assertEqual(6, overhead.overhead(METRICS + METRICS)["samples"]["delivery_lag"])

    def test_estimate(self):
        night = overhead.overhead(METRICS)
        self.assertEqual(12.1, overhead.estimate(10.0, 4, night), "10 s of agent time, one 0.5 s delivery, 4 posts of 0.4 s")
        self.assertIsNone(overhead.estimate(10.0, None, night))
        self.assertIsNone(overhead.estimate(10.0, 4, overhead.overhead("")))


_FAKE_GCLOUD = '#!/usr/bin/env bash\nprintf %s fake-value\n'
_FAKE_UV = '#!/usr/bin/env bash\nexit 0\n'
# Two gateway pods and one other pod; a port-forward is a process the lane kills.
_FAKE_KUBECTL = """#!/usr/bin/env bash
case "$*" in
  *"get pods -o name"*) printf 'pod/platform-agent-a2a-gateway-a\\npod/platform-agent-a2a-gateway-b\\npod/litellm-x\\n' ;;
  *"port-forward"*) exec sleep 30 ;;
  *) exit 1 ;;
esac
"""
# The first pod (local port 19097) serves the Slack series: the empty scrape before the
# cases, METRICS after them; the standby (19098) serves none.
_FAKE_CURL = """#!/usr/bin/env bash
case "$*" in
  *19097*) if [ -e "$SCRAPED" ]; then cat "$FAKE_METRICS"; else : >"$SCRAPED"; printf 'process_start_time_seconds 1\\n'; fi ;;
  *19098*) printf '# no slack series on the standby\\n' ;;
  *) exit 7 ;;
esac
"""


class RunScrapesTheGatewayTest(unittest.TestCase):
    def test_the_lane_writes_what_the_run_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name, body in (("gcloud", _FAKE_GCLOUD), ("uv", _FAKE_UV), ("kubectl", _FAKE_KUBECTL), ("curl", _FAKE_CURL)):
                (root / name).write_text(body)
                (root / name).chmod(0o755)
            (root / "metrics.txt").write_text("process_start_time_seconds 1\n" + METRICS)
            env = {"PATH": f"{root}:{os.environ['PATH']}", "FAKE_METRICS": str(root / "metrics.txt"),
                   "SCRAPED": str(root / "scraped"), "SLACK_LANE_ARTIFACTS": str(root / "artifacts")}
            proc = subprocess.run(["bash", str(_LANE / "run.sh")], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(0, proc.returncode, proc.stderr)
            got = json.loads((root / "artifacts" / "slack-overhead.json").read_text())
            self.assertEqual(0.5, got["delivery_lag_s"])
            self.assertEqual(3, got["samples"]["delivery_lag"])


class SinceTest(unittest.TestCase):
    def test_the_run_is_the_difference_and_a_restart_counts_from_zero(self):
        before = "# pod: a\nprocess_start_time_seconds 1\n" + METRICS
        doubled = METRICS.replace("_count 3", "_count 5").replace("_sum 1.5", "_sum 2.5")
        after = "# pod: a\nprocess_start_time_seconds 1\n" + doubled
        got = overhead.overhead(overhead.since(after, before))
        self.assertEqual({"delivery_lag": 2, "post": 0, "first_ack": 0}, got["samples"])
        self.assertEqual(0.5, got["delivery_lag_s"], "1 s over 2 new deliveries")
        restarted = "# pod: a\nprocess_start_time_seconds 2\n" + METRICS
        self.assertEqual(3, overhead.overhead(overhead.since(restarted, before))["samples"]["delivery_lag"])


if __name__ == "__main__":
    unittest.main()
