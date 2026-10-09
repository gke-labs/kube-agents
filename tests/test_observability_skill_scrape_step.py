"""The observability skill reads the scrape config off the PodMonitoring, not the Deployment.

The kube-agents operator renders no Prometheus scrape annotations on the gateway
Deployment; the managed collector scrapes through the chart's `PodMonitoring`
resources instead. Step 1 of the skill's Metrics workflow used to send the agent
to the Deployment looking for those annotations, so on a scraped install it
reported "not scraped" (gke-labs/kube-agents#2141). The step now names the
`PodMonitoring` resources, their ports and the proving series.

The bench case `observability-watcher-scrape-state` grades the agent's behaviour,
and on the live-test install the worker read the PodMonitoring without the skill's
help, so the case does not red against the old step. Nothing executes the skill
prose, so this test pins the phrases the fix is made of and the shape of the step:
step 1 is a fixed three-command recipe, so the kubectl commands in its fenced code
blocks are pinned verbatim (`STEP_ONE_COMMANDS`). Any edit to that recipe -- a
command added, removed, reworded, or reverted to read the Deployment, in any shell
spelling -- changes the collected list and reds this test, forcing a deliberate
update. Pinning the list rather than parsing each line for a resource kind is what
earlier rounds kept chasing with one more shell-separator rule: a regex standing in
for shell grammar leaks both ways (an env-prefixed or continued command it fails to
collect, a plural kind it false-reds), while an exact list cannot. Four denylist
patterns back that up for a revert written as prose rather than a fenced command,
but each keys on an annotation word, so they catch an annotation-worded revert, not
one phrased in entirely other words. Per `.agents/rules/eval_driven_development.md`
this stand-in does not replace the case; it is the guard the case cannot be, because
the agent's behaviour was already correct.

Residual: a command written in an inline span rather than a fenced code block is not
collected, so it is neither pinned by the recipe nor, unless it names the Deployment
or an annotation word, caught by the denylist. Step 1's reads are all fenced;
collecting from inline spans would read a prose `kubectl` mention as a command.

Run:
  python3 -m unittest discover -s tests -p 'test_observability_skill_scrape_step.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from markdown_it import MarkdownIt

REPO_ROOT = Path(__file__).resolve().parents[1]
OBSERVABILITY_SKILL = REPO_ROOT / "agents/platform/skills/kube-agents-observability/SKILL.md"

# The Metrics workflow's first step, by its exact heading. Three other "### 1."
# headings in the file (Logging, Traces, Agent Status) make the full string the
# only unambiguous anchor; the step ends at the next "### 2." heading.
STEP_HEADING = "### 1. Verify Cloud Monitoring & Prometheus State"
NEXT_HEADING = "### 2. Inspect CPU and Memory Metrics"

# The fix, in the skill's own words: read the scrape config off the
# PodMonitoring, not off the Deployment the operator leaves unannotated.
READ_OFF_PODMONITORING = (
    "Read the scrape configuration off the `PodMonitoring` resources, not off Deployment annotations"
)
# The PodMonitoring read the step spells out, and the listener and proving series
# that make the watcher's answer.
GATEWAY_READ = "kubectl get podmonitoring <name>-gateway-monitoring"
WATCHER_PORT = "9095"
WATCHER_SERIES = 'up{job="<name>-gateway-monitoring"}'
WATCHER_UP = "k8s_event_watcher_cluster_up"

# Step 1 is a fixed recipe: these exact kubectl commands, in this order, are the
# only ones its fenced code blocks issue. Pin them verbatim. A revert to the #2141
# Deployment-annotation read, an added or reworded command, or a second resource
# hidden behind a chain, a redirect or an env prefix all change the collected list
# and red `test_step_one_issues_exactly_the_podmonitoring_reads`. This is the
# structural guard the separator-splitting regex of earlier rounds was reaching
# for: no shell grammar to parse, so no shape to miss and no benign spelling to
# false-red. Reword the step on purpose and this list is updated in the same change.
STEP_ONE_COMMANDS = [
    "kubectl get pods -n gmp-system",
    "kubectl get podmonitoring <name>-gateway-monitoring -n kubeagents-system -o yaml",
    "kubectl get podmonitoring <name>-credential-proxy-monitoring -n kubeagents-system -o yaml",
]

# The #2141 bug, reverted: the Deployment-annotation read must not come back into
# the step. The operator renders no such annotation, so any `kubectl get`/
# `describe` of a deployment in step 1 is the regression, in any spelling of the
# noun (`deploy`, `deployment`, `deployments`, `deployments.apps`) and with any
# flags between `kubectl`, the verb and the noun (`kubectl -n ns get deployment`,
# `kubectl get -o yaml deployment`): the gaps are `[^\n]*`, not whitespace, so the
# match describes "a deployment read on one line" rather than one command shape.
# Held to one line (`[^\n]*`, never `.`), so the prose that names the Deployment
# only to forbid it ("not off Deployment annotations", "whatever the Deployment
# says") carries no `kubectl` on its line and does not trip. This denylist catches
# a revert written in prose or an inline span, which the fenced-command recipe does
# not collect; the recipe catches a revert written as a fenced command.
DEPLOYMENT_READ = re.compile(r"(?i)kubectl\b[^\n]*\b(get|describe)\b[^\n]*\bdeploy(ments?)?(\.apps)?\b")
# The same bug by its own noun, not the resource it read: the scrape opt-in
# annotation. A revert that reads it off the pod template instead of the
# Deployment is the same regression, and no `deploy` pattern would catch it.
PROM_SCRAPE_ANNOTATION = re.compile(r"(?i)prometheus\.io/scrape")
# The same bug in prose, naming neither the literal annotation key nor a
# `kubectl ... deploy`: a step that tells the agent to read "scrape annotations"
# or "annotations for Prometheus scraping" (off any resource) is the regression
# the two patterns above miss. The current step names Deployment annotations only
# to forbid them ("not off Deployment annotations"), which is "deployment", not
# "scrape", annotations and does not trip this.
SCRAPE_ANNOTATION_PROSE = re.compile(
    r"(?i)annotations?\s+for\s+prometheus\s+scrap|scrap\w*\s+annotations?"
)
# The three patterns above are a denylist -- each keyed to one spelling of the
# revert (a deployment read, the literal annotation key, the prose "scrape
# annotations"). A revert spelled in none of them ("read the gateway pod's
# Prometheus annotations", a jsonpath over `.annotations`) slips all three. This
# last check widens the net to the annotation word itself: step 1 names annotations
# exactly once, in the sentence that forbids reading them (READ_OFF_PODMONITORING);
# strip that sentence and any surviving `annotat` is an annotation read the three
# patterns above may not spell out. It is not an "in any words" backstop -- it keys
# on the substring `annotat`, so a revert phrased without that word slips it too;
# the pinned recipe above is what holds each fenced command's read to the
# PodMonitoring. The cost of this one is that a future non-revert mention of the
# word would also trip it and have to update the guard; for a step whose only
# correct mention of annotations is to forbid them, that is the right default.
ANY_ANNOTATION = re.compile(r"(?i)annotat")

# Step 1's code blocks, parsed rather than regex-matched: CommonMark collects ```
# and ~~~ fences and indented blocks alike, so a revert in any of those shapes is
# seen, while an inline `kubectl` span is a `code_inline` child, not a block, and
# stays out by design.
MARKDOWN = MarkdownIt("commonmark")
CODE_BLOCK_TOKENS = ("fence", "code_block")


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) < 1000:
        raise AssertionError(f"{path} read back suspiciously short ({len(text)} chars)")
    return text


def _metrics_step_one(skill: str) -> str:
    start = skill.index(STEP_HEADING)
    end = skill.index(NEXT_HEADING, start)
    return skill[start:end]


def _step_one_fenced_commands(step: str) -> list[str]:
    """Every command line step 1's code blocks issue, in order.

    Collects every non-empty line from the step's fenced (``` or ~~~) and indented
    code blocks -- not just the lines that begin with `kubectl`, so a revert written
    as an env-prefixed (`KUBECONFIG=... kubectl ...`), `sudo`-prefixed or looped
    command is collected and reds the exact-match rather than being silently skipped.
    A `kubectl` written in prose (an inline span in a sentence) is a `code_inline`,
    not a code block, so it is not collected and not mistaken for a command to pin.
    """
    commands = []
    for token in MARKDOWN.parse(step):
        if token.type in CODE_BLOCK_TOKENS:
            for line in token.content.splitlines():
                line = line.strip()
                if line:
                    commands.append(line)
    return commands


class ObservabilitySkillReadsThePodMonitoring(unittest.TestCase):
    def test_step_one_reads_the_scrape_off_the_podmonitoring(self):
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        for phrase in (READ_OFF_PODMONITORING, GATEWAY_READ, WATCHER_PORT, WATCHER_SERIES, WATCHER_UP):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, step, f"the observability skill's Prometheus step lost {phrase!r}")

    def test_step_one_issues_exactly_the_podmonitoring_reads(self):
        # The positive guard the resource-read invariant rests on: step 1's fenced
        # code blocks issue exactly the three pinned reads, in order. A revert that
        # points a command at the Deployment or any other resource -- added,
        # substituted, chained, redirected, env-prefixed or looped -- changes this
        # list and reds here, whatever nouns the surrounding prose uses. A
        # deliberate reword of the step updates STEP_ONE_COMMANDS in the same change.
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        self.assertEqual(
            _step_one_fenced_commands(step),
            STEP_ONE_COMMANDS,
            "step 1's fenced kubectl recipe changed. If the change is intentional, "
            "update STEP_ONE_COMMANDS to match; if a command now reads the Deployment "
            "or another resource, that is the #2141 regression.",
        )

    def test_step_one_does_not_send_the_agent_to_the_deployment_annotations(self):
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        self.assertNotRegex(
            step,
            DEPLOYMENT_READ,
            "step 1 sends the agent back to the Deployment for scrape annotations (the #2141 regression)",
        )

    def test_step_one_does_not_read_scrape_opt_in_annotations(self):
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        self.assertNotRegex(
            step,
            PROM_SCRAPE_ANNOTATION,
            "step 1 tells the agent to read the prometheus.io/scrape annotation (the #2141 regression, by any resource)",
        )

    def test_step_one_does_not_describe_scrape_annotations_in_prose(self):
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        self.assertNotRegex(
            step,
            SCRAPE_ANNOTATION_PROSE,
            "step 1 describes reading scrape annotations in prose (the #2141 regression, without the literal key or a deployment read)",
        )

    def test_step_one_mentions_annotations_only_to_forbid_reading_them(self):
        # The invariant behind the three patterns above: step 1 names annotations
        # only in the sentence that forbids reading them. Strip that sentence and any
        # surviving "annotat" is an annotation read the denylist patterns may not
        # enumerate -- the #2141 regression wherever the word `annotation` appears.
        # This keys on that word, not on any wording of the revert; the pinned recipe
        # is the guard that holds each fenced command's read to the PodMonitoring.
        step = _metrics_step_one(_read(OBSERVABILITY_SKILL))
        residue = step.replace(READ_OFF_PODMONITORING, "")
        self.assertNotRegex(
            residue,
            ANY_ANNOTATION,
            "step 1 mentions annotations outside the sentence that forbids reading them (the #2141 regression, by the annotation word)",
        )

    def test_the_collector_reads_fenced_commands_not_prose_kubectl_mentions(self):
        # The collector reads commands from fenced (``` and ~~~) and indented code
        # blocks, so a revert in any of those shapes reds the exact-match, while a
        # `kubectl` written in prose (an inline span in a sentence) is not mistaken
        # for a command to pin -- the reason the exact-match above is safe against
        # step 1's prose, which names `kubectl` and the Deployment only to steer away
        # from them. It also collects a command that does not begin with `kubectl`
        # (an env prefix), so such a revert reds the exact-match rather than slipping.
        step = (
            "### 1. Verify Cloud Monitoring & Prometheus State\n"
            "\n"
            "Run `kubectl` against the collector, not the Deployment.\n"
            "\n"
            "```bash\n"
            "KUBECONFIG=/tmp/kc kubectl get deployment <name>-gateway -o yaml\n"
            "```\n"
            "\n"
            "~~~bash\n"
            "kubectl get podmonitoring <name>-gateway-monitoring -n kubeagents-system -o yaml\n"
            "~~~\n"
            "\n"
            "    kubectl get deployment <name>-credential-proxy -o yaml\n"
            "\n"
            "### 2. Inspect CPU and Memory Metrics\n"
        )
        commands = _step_one_fenced_commands(_metrics_step_one(step))
        self.assertEqual(
            commands,
            [
                "KUBECONFIG=/tmp/kc kubectl get deployment <name>-gateway -o yaml",
                "kubectl get podmonitoring <name>-gateway-monitoring -n kubeagents-system -o yaml",
                "kubectl get deployment <name>-credential-proxy -o yaml",
            ],
            "the collector dropped a ~~~ fence, an indented block or an env-prefixed "
            "command, or picked up a prose kubectl mention",
        )


if __name__ == "__main__":
    unittest.main()
