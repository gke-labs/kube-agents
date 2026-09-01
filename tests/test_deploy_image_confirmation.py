"""The agent deploy must prove the tag it set reached the pod template.

scripts/confirm_agent_image.sh's header owns why the deploy needs this at all.
Four things are pinned here. That the workflow runs the read-back before the
rollout gate, and that its failure can still fail the job. That the read-back
covers every release image in the template, because the credential-proxy
sidecar is derived separately from the agent's own reference and the two can
come apart. That it identifies those images the way images.json does rather
than by registry prefix, which a mirrored install would break. And that the
script fails loudly when its own inputs are wrong, rather than exiting 0
having read nothing.
"""

import os
import pathlib
import subprocess
import tempfile
import textwrap
import unittest

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_AGENT_WORKFLOW = _ROOT / ".github" / "workflows" / "reusable-deploy-agent.yml"
_SCRIPT = _ROOT / "scripts" / "confirm_agent_image.sh"
_READINESS_SCRIPT = _ROOT / "scripts" / "release" / "wait_for_gke_readiness.sh"

_GATEWAY = "platform-agent-gateway"
_TAG = "f1908801e545abffd967d3b8bf34d58833f5d945"
_OLD = "a1456a4b0b5b60090b96bd70edb030a53873768d"

_GHCR = "ghcr.io/gke-labs/kube-agents"
_MIRROR = "europe-docker.pkg.dev/acme/mirror"


class DeployWorkflowWiringTest(unittest.TestCase):
    """Where the read-back sits in the deploy job."""

    def setUp(self):
        self.steps = yaml.safe_load(_AGENT_WORKFLOW.read_text())["jobs"]["deploy"]["steps"]
        self.runs = [step.get("run", "") for step in self.steps]

    def _index_of(self, needle, description):
        for index, run in enumerate(self.runs):
            if needle in run:
                return index
        self.fail(f"no step in {_AGENT_WORKFLOW.name} {description}")

    def test_the_deploy_confirms_the_tag_it_set(self):
        self._index_of(_SCRIPT.name, f"runs {_SCRIPT.name}")

    def test_the_confirmation_precedes_the_rollout_gate(self):
        # Ordering is not cosmetic; the step's own comment in the workflow says
        # why. Past the gate the job has already reported success.
        confirm = self._index_of(_SCRIPT.name, f"runs {_SCRIPT.name}")
        gate = self._index_of(
            f"rollout status deployment/{_GATEWAY}",
            f"runs `kubectl rollout status` on {_GATEWAY}",
        )
        self.assertLess(
            confirm,
            gate,
            "the tag confirmation must run before `kubectl rollout status`",
        )

    def test_the_confirmation_can_still_fail_the_job(self):
        # A guard whose failure is swallowed is worse than no guard: the deploy
        # reports the same green it did before, and the step in the log implies
        # the tag was checked. Both routes to that are cheap to add later and
        # invisible in review, so pin them.
        index = self._index_of(_SCRIPT.name, f"runs {_SCRIPT.name}")
        step = self.steps[index]
        self.assertNotIn(
            "continue-on-error",
            step,
            "continue-on-error on the confirmation step lets an ignored tag deploy green",
        )
        self.assertNotRegex(
            step["run"],
            r"(\|\|\s*true|;\s*exit\s+0)\s*$",
            "the confirmation's exit status must reach the job",
        )


class ReleaseReadinessDelegatesTest(unittest.TestCase):
    """The RC path asserts the same thing, and must do it the same way.

    wait_for_gke_readiness.sh had its own copy of this read-back. It matched on
    `.containers[*]` only and passed on the first container carrying the SHA, so
    an agent left behind by a credential-proxy that had rolled forward -- the
    skew the guard exists to name -- went green on the release path.
    """

    def setUp(self):
        self.text = _READINESS_SCRIPT.read_text()

    def test_it_calls_the_shared_confirmation(self):
        self.assertIn(_SCRIPT.name, self.text)

    def test_it_does_not_open_code_the_image_read_back(self):
        self.assertNotRegex(
            self.text,
            r"jsonpath='\{\.spec\.template\.spec\.containers\[\*\]\.image\}'",
            "a second, weaker expression of the image read-back has come back",
        )

    def test_the_confirmation_precedes_the_gateway_rollout_gate(self):
        confirm = self.text.index(_SCRIPT.name)
        gate = self.text.index("rollout status deployment/platform-agent-gateway")
        self.assertLess(confirm, gate)


class _StubKubectl:
    """Drives the script against a stub `kubectl` on PATH.

    Shared so the classes below exercise the script's real control flow rather
    than a transcription of it.
    """

    def _run(
        self,
        *templates,
        tag=_TAG,
        timeout="0",
        interval="0",
        cr_image=f"{_GHCR}/platform-agent",
        cr_phase="Ready",
        cr_reason="Reconciled",
        deployment_missing=False,
        images_json=None,
    ):
        """Run the script against one or more stubbed reads of the template.

        Each `name=image` listing answers one `kubectl get deployment` call, the
        last repeating once they run out, so a sequence exercises the poll. The
        two CR reads on the failure path are answered separately, from
        `cr_image` and `cr_phase`, and do not consume one — they are what the
        script uses to tell a CR that is wrong from an operator that never
        reconciled. `deployment_missing` makes the template read fail the way a
        real cluster does, on stderr, which is a third outcome again.

        A zero timeout makes the failure paths immediate: the loop checks for
        success before it checks the deadline, so the passing cases are
        unaffected. Tests that need the loop to go round pass their own.
        """
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        stub_dir = pathlib.Path(holder.name)

        for index, template in enumerate(templates):
            (stub_dir / f"read-{index}").write_text(textwrap.dedent(template).strip() + "\n")

        if deployment_missing:
            template_branch = textwrap.dedent(
                f"""\
                echo 'Error from server (NotFound): deployments.apps "{_GATEWAY}" not found' >&2
                exit 1
                """
            )
        else:
            template_branch = textwrap.dedent(
                f"""\
                count_file="{stub_dir}/calls"
                count=$(cat "$count_file" 2>/dev/null || echo 0)
                echo $((count + 1)) >"$count_file"
                last={len(templates) - 1}
                [ "$count" -gt "$last" ] && count="$last"
                cat "{stub_dir}/read-${{count}}"
                """
            )

        kubectl = stub_dir / "kubectl"
        kubectl.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                if [[ "$*" == *status.phase* ]]; then
                  echo '  platform-agent: {cr_phase}'
                  echo '    Ready=False {cr_reason}'
                  exit 0
                fi
                if [[ "$*" == *platformagent* ]]; then
                  echo 'platform-agent={cr_image}={tag}'
                  exit 0
                fi
                """
            )
            + template_branch
        )
        kubectl.chmod(0o755)

        env = dict(os.environ)
        env["PATH"] = f"{stub_dir}:{env['PATH']}"
        if images_json is not None:
            env["IMAGES_JSON"] = images_json
        env["AGENT_IMAGE_CONFIRM_TIMEOUT"] = timeout
        env["AGENT_IMAGE_CONFIRM_INTERVAL"] = interval
        result = subprocess.run(
            [str(_SCRIPT), "kubeagents-system", _GATEWAY, tag],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        calls_file = stub_dir / "calls"
        self.calls = int(calls_file.read_text()) if calls_file.exists() else 0
        return result


class ConfirmAgentImageScriptTest(_StubKubectl, unittest.TestCase):
    """What the script accepts and what it refuses."""

    def test_it_passes_when_every_release_image_carries_the_tag(self):
        result = self._run(
            f"""
            sandbox-credential-cleanup={_GHCR}/platform-agent:{_TAG}
            envoy-credential-proxy={_GHCR}/credential-proxy:{_TAG}
            platform-agent={_GHCR}/platform-agent:{_TAG}
            fluent-bit=docker.io/fluent/fluent-bit:5.1.0
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("3 release image", result.stdout)

    def test_it_ignores_a_third_party_pin_on_its_own_registry(self):
        result = self._run(
            f"""
            platform-agent={_GHCR}/platform-agent:{_TAG}
            fluent-bit=docker.io/fluent/fluent-bit:5.1.0
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_it_ignores_a_third_party_pin_mirrored_under_the_release_prefix(self):
        # The shape a single-prefix mirror renders: fluent-bit under the same
        # prefix as the release images, on its own upstream version. The script
        # header owns why that is what a mirror produces.
        result = self._run(
            f"""
            sandbox-credential-cleanup={_MIRROR}/platform-agent:{_TAG}
            envoy-credential-proxy={_MIRROR}/credential-proxy:{_TAG}
            platform-agent={_MIRROR}/platform-agent:{_TAG}
            fluent-bit={_MIRROR}/fluent-bit:5.1.0
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("3 release image", result.stdout)

    def test_it_fails_when_the_agent_is_pinned_to_an_older_tag(self):
        # spec.deployment.image pinned to a full reference, so the tag the
        # deploy set was never consulted.
        result = self._run(
            f"""
            sandbox-credential-cleanup={_GHCR}/platform-agent:{_OLD}
            envoy-credential-proxy={_GHCR}/credential-proxy:{_OLD}
            platform-agent={_GHCR}/platform-agent:{_OLD}
            fluent-bit=docker.io/fluent/fluent-bit:5.1.0
            """,
            cr_image=f"{_GHCR}/platform-agent:{_OLD}",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(_OLD, result.stdout)
        self.assertIn("kubectl patch platformagent", result.stdout)

    def test_it_recognises_a_digest_pin_on_a_registry_with_a_port(self):
        # A registry port puts a colon in the first path segment, so the
        # segment has to be taken before the tag is stripped. Get that order
        # wrong and the reference reduces to the registry host, stops matching
        # any release name, and the failure reports finding no release image
        # rather than naming the pin it exists to name.
        result = self._run(
            "platform-agent=registry.local:5000/kube-agents/platform-agent@sha256:" + "0" * 64,
            cr_image="registry.local:5000/kube-agents/platform-agent@sha256:" + "0" * 64,
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Found no first-party release image", result.stdout)
        self.assertIn("kubectl patch platformagent", result.stdout)

    def test_it_reports_the_operator_status_on_failure(self):
        result = self._run(
            f"platform-agent={_GHCR}/platform-agent:{_OLD}",
            cr_phase="Degraded",
            cr_reason="RuntimeClassNotFound",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Degraded", result.stdout)
        self.assertIn("RuntimeClassNotFound", result.stdout)

    def test_it_does_not_blame_the_cr_when_the_cr_is_healthy(self):
        # A CR that is wrong is not the only way to get here -- an operator that
        # is absent, crash-looping, or returning early never re-renders the pod
        # template. Printing a CR remedy regardless sends the reader after a CR
        # that is fine.
        result = self._run(
            f"platform-agent={_GHCR}/platform-agent:{_OLD}",
            cr_image=f"{_GHCR}/platform-agent",
            cr_phase="Degraded",
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("kubectl patch platformagent", result.stdout)
        self.assertIn("the CR is not the cause", result.stdout)

    def test_it_names_an_unset_cr_image_as_the_cause(self):
        # The third cause, and the one an unpinned/pinned split misses:
        # resolveAgentImage reads spec.deployment.tag only when
        # spec.deployment.image is set, so clearing that field makes the
        # operator serve its own default and skip the tag entirely. Reported as
        # a healthy CR, this sends the on-caller to audit an operator that is
        # working exactly as written.
        result = self._run(
            f"platform-agent={_GHCR}/platform-agent:{_OLD}",
            cr_image="",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("is unset", result.stdout)
        self.assertNotIn("the CR is not the cause", result.stdout)

    def test_it_separates_a_missing_deployment_from_an_unrecognisable_one(self):
        # buildStatefulSet renders the gateway as a StatefulSet when custom RWO
        # storage meets multiple replicas, so the Deployment may never arrive
        # under this name at all. Folded into "found no release image", that
        # reads as a template problem and drags the CR diagnostics in behind it.
        result = self._run("", deployment_missing=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"No Deployment {_GATEWAY}", result.stdout)
        self.assertNotIn("Found no first-party release image", result.stdout)

    def test_it_fails_on_a_sidecar_that_moved_without_the_agent(self):
        # The digest-pin path: the agent freezes at its digest while the
        # credential proxy beside it takes spec.deployment.tag and rolls
        # forward every deploy. Only a template-wide check reports which images
        # came apart, and the reverse skew is invisible without it.
        result = self._run(
            f"""
            platform-agent={_GHCR}/platform-agent:{_TAG}
            envoy-credential-proxy={_GHCR}/credential-proxy:{_OLD}
            fluent-bit=docker.io/fluent/fluent-bit:5.1.0
            """
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("envoy-credential-proxy", result.stdout)

    def test_it_fails_on_a_digest_pinned_agent(self):
        # A digest carries no tag, so the deploy's tag demonstrably did not
        # take effect however current the digest happens to be.
        result = self._run(f"platform-agent={_GHCR}/platform-agent@sha256:" + "0" * 64)
        self.assertEqual(result.returncode, 1)

    def test_it_fails_when_no_release_image_is_present(self):
        # An unrecognisable read-back is not a pass.
        result = self._run("fluent-bit=docker.io/fluent/fluent-bit:5.1.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Found no first-party release image", result.stdout)

    def test_it_waits_out_a_template_the_operator_has_not_written_yet(self):
        # The reason the script is a loop. The operator reconciles
        # asynchronously and the deploy returns before it does, so a stale read
        # is the expected first answer -- giving up on it would red every real
        # deploy while accusing the CR of being pinned.
        result = self._run(
            f"platform-agent={_GHCR}/platform-agent:{_OLD}",
            f"platform-agent={_GHCR}/platform-agent:{_OLD}",
            f"platform-agent={_GHCR}/platform-agent:{_TAG}",
            timeout="30",
            interval="1",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls, 3, "expected the loop to poll until the template caught up")

    def test_it_waits_out_an_empty_read(self):
        # A Deployment the operator has not created yet reads back as nothing,
        # which is indistinguishable from a slow one this early.
        result = self._run(
            "",
            f"platform-agent={_GHCR}/platform-agent:{_TAG}",
            timeout="30",
            interval="1",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls, 2)


class ConfirmAgentImageMisconfigurationTest(_StubKubectl, unittest.TestCase):
    """The guard must not go green when the guard itself is misconfigured.

    This is the script's own version of the bug it exists to catch. Both paths
    below reached `exit 0` having read nothing from the cluster, which in CI is
    a step that logs an error and lets the deploy through.
    """

    def test_a_non_integer_budget_fails_loudly(self):
        # `set -u` makes $((SECONDS + abc)) fatal, and an EXIT trap whose last
        # command is a successful `rm` then supplies the shell's exit status.
        result = self._run(f"platform-agent={_GHCR}/platform-agent:{_TAG}", timeout="abc")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("AGENT_IMAGE_CONFIRM_TIMEOUT", result.stdout)

    def test_a_non_integer_interval_fails_loudly(self):
        result = self._run(f"platform-agent={_GHCR}/platform-agent:{_TAG}", interval="1.5")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("AGENT_IMAGE_CONFIRM_INTERVAL", result.stdout)

    def test_a_missing_inventory_fails_with_an_annotation(self):
        # `set -e` aborted on jq's own status before the "no release images"
        # guard could run, so the step carried jq's exit code and raw stderr --
        # which GitHub does not render as an annotation.
        result = self._run(
            f"platform-agent={_GHCR}/platform-agent:{_TAG}",
            images_json="/nonexistent/images.json",
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error::", result.stdout)
        self.assertIn("does not exist", result.stdout)

    def test_an_inventory_with_no_release_images_fails_with_an_annotation(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        empty = pathlib.Path(holder.name) / "images.json"
        empty.write_text('{"images": []}\n')
        result = self._run(
            f"platform-agent={_GHCR}/platform-agent:{_TAG}",
            images_json=str(empty),
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("No first-party release images", result.stdout)


if __name__ == "__main__":
    unittest.main()
