#!/usr/bin/env bash
#
# Signal quality — the stale alert.
#
# Alerts arrive late. By the time the agent looks, the capacity may have appeared, the
# workload may have been scaled down, or someone may have already fixed it. Acting on
# a stale alert costs an unnecessary PR and erodes trust in the ones that matter, so
# the skill's step 2B exists to catch exactly this.
#
# No workload is deployed: the alert names something that is not wedged, which is what
# a resolved incident looks like from the agent's side.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCENARIO_TITLE="Alert for a workload that is no longer in trouble"
SCENARIO_RULE="Signal quality — the agent should stand down, not remediate"
SCENARIO_CONTROLLER="frontend-web-app"
SCENARIO_PODS=2

# There is deliberately no scenario_manifest: the point is that the cluster is healthy.
# --no-workload is implied and forced, so the flag cannot be forgotten.
SKIP_WORKLOAD=1

scenario_reasons() {
    cat <<JSON
"napFailureReasons": [
  {
    "messageId": "no.scale.up.nap.pod.zonal.resource.pool.exhausted",
    "parameters": ["c2d", "${ZONE_A}"]
  }
]
JSON
}

scenario_notes() {
    cat <<'TXT'
Expected: the agent investigates, finds no Pending pods and no FailedScheduling events
for frontend-web-app, concludes the signal is stale, and does NOT open a PR.

A remediation PR here is a failure of the scenario. So is silence — the skill's step 1
asks for a chat notification when an investigation starts, and step 2B asks it to
report the stand-down, so there should be a visible "investigated, nothing to fix"
rather than nothing at all.

Run this after scenario 04 has been cleaned up, when the alert is genuinely stale and
the workload it names really did exist a moment ago:

  ./04-missing-zone-fallback.sh --teardown --no-wait && ./10-false-signal.sh
TXT
}

scenario_main "$@"
