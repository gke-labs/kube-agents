"""The CI-side install configuration renderer.

`install.env` is the installer's input, and a GitHub runner has none — so every
job that drives the installer renders one from the environment's variables. What
that renderer gets wrong is not visible in its own output: an omitted setting
becomes a default, the default becomes a `terraform.tfvars` value, and
`terraform apply` plans the destruction of whatever the default did not mention.
That is #1060, and a scheduled unattended apply against a long-lived environment
is where it does the damage: nobody is watching the plan go by.

So these pin the two halves that keep it from happening: what --strict refuses,
and that an unset setting is left OUT of the file rather than written empty.
"""

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "render_install_env.sh"

# The coordinates every invocation needs. Individual tests add to these.
_COORDS = {
    "GCP_PROJECT_ID": "kube-agents-autopush",
    "GCP_REGION": "us-central1",
    "GKE_CLUSTER_NAME": "platform-agent-host",
}

# Everything --strict additionally requires, at plausible values.
_STRICT_SETTINGS = {
    "GOOGLE_CHAT_ENABLED": "true",
    "MODEL_PROVIDER": "gemini",
    "PLATFORM_AGENT_PERMISSION_SET": "custom",
    "ENABLE_GVISOR": "true",
    "MEMORY_PROVIDER": "kube_agents_memory",
    "USER_PROFILE_ENABLED": "true",
    "ENABLE_GKE_BACKUP_PLAN": "true",
    "ENABLE_PUBSUB_PLATFORM": "true",
    "ENABLE_STOCKOUT_INVESTIGATOR": "true",
}

# The allowlist is required too, but only while its integration is on, so it is
# not part of the unconditional list above. A strict render that is expected to
# SUCCEED needs it, because _STRICT_SETTINGS switches Google Chat on.
_ALLOWLIST = {"ALLOWED_USERS": "reconcile-tester@example.com"}
_STRICT_OK = {**_STRICT_SETTINGS, **_ALLOWLIST}


def render(env, strict=False):
    """Runs the renderer and returns (returncode, stdout+stderr, rendered text)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "install.env")
        args = [str(_SCRIPT), out]
        if strict:
            args.append("--strict")
        # A bare environment, so a variable the test did not set cannot arrive
        # from the developer's shell or from CI's own.
        proc = subprocess.run(
            args, capture_output=True, text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        )
        text = pathlib.Path(out).read_text() if os.path.exists(out) else ""
    return proc.returncode, proc.stdout + proc.stderr, text


def parse(text):
    """The rendered file as a dict, the way `set -a; . install.env` would read it."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, raw = line.partition("=")
        # The renderer writes with %q, so bash is the only correct reader.
        values[key] = subprocess.run(
            ["bash", "-c", 'printf "%s" ' + raw], capture_output=True, text=True
        ).stdout
    return values


class RequiredInputsTest(unittest.TestCase):
    def test_missing_coordinates_fail_without_strict(self):
        rc, log, _ = render({"GCP_PROJECT_ID": "p"})
        self.assertEqual(rc, 1)
        self.assertIn("GCP_REGION", log)
        self.assertIn("GKE_CLUSTER_NAME", log)

    def test_every_missing_variable_is_named_in_one_message(self):
        """One run per missing variable is eleven runs on an unconfigured environment."""
        rc, log, _ = render(dict(_COORDS), strict=True)
        self.assertEqual(rc, 1)
        for var in _STRICT_SETTINGS:
            with self.subTest(var=var):
                self.assertIn(var, log)

    def test_the_failure_is_a_github_error_annotation(self):
        """So the reason shows on the run summary rather than only in the log."""
        rc, log, _ = render(dict(_COORDS), strict=True)
        self.assertEqual(rc, 1)
        self.assertIn("::error title=Install configuration is incomplete::", log)

    def test_coordinates_alone_are_enough_without_strict(self):
        """The ephemeral path: nothing exists yet, so nothing can be destroyed."""
        rc, log, text = render(dict(_COORDS))
        self.assertEqual(rc, 0, log)
        self.assertEqual(parse(text)["PROJECT_ID"], "kube-agents-autopush")

    def test_strict_passes_once_every_setting_is_present(self):
        rc, log, text = render({**_COORDS, **_STRICT_OK}, strict=True)
        self.assertEqual(rc, 0, log)
        self.assertEqual(parse(text)["PLATFORM_AGENT_PERMISSION_SET"], "custom")


class MinterGuardTest(unittest.TestCase):
    """The one setting the installer reads as a unit rather than one at a time.

    `write_tfvars_from_state` enables the minter only when GITOPS_ORG,
    GITOPS_REPO and GITHUB_APP_ID are all non-empty, and renders
    `enable_github_minter = false` without a word when any is missing. On a
    fresh install that is an install without a minter; on one that already has
    a minter it is an apply that destroys it. #1117 found autopush configured
    with exactly the broken combination — GH_APP_ID and neither variable.
    """

    def test_a_half_configured_minter_is_refused_under_strict(self):
        rc, log, _ = render(
            {**_COORDS, **_STRICT_OK, "GITHUB_APP_ID": "12345"},
            strict=True)
        self.assertEqual(rc, 1)
        self.assertIn("half-configured", log)
        self.assertIn("GITOPS_ORG", log)
        self.assertIn("GITOPS_REPO", log)

    def test_all_three_together_are_accepted(self):
        rc, log, text = render(
            {**_COORDS, **_STRICT_OK, "GITHUB_APP_ID": "12345",
             "GITOPS_ORG": "gke-agentic", "GITOPS_REPO": "gke-fleet-iac"},
            strict=True)
        self.assertEqual(rc, 0, log)
        self.assertEqual(parse(text)["GITHUB_APP_ID"], "12345")

    def test_none_of_the_three_is_an_install_without_a_minter(self):
        rc, log, _ = render({**_COORDS, **_STRICT_OK}, strict=True)
        self.assertEqual(rc, 0, log)

    def test_the_coordinates_are_reported_before_the_minter(self):
        """A run missing everything should be told about the basics first."""
        rc, log, _ = render({"GITHUB_APP_ID": "12345"}, strict=True)
        self.assertEqual(rc, 1)
        self.assertIn("Install configuration is incomplete", log)
        self.assertNotIn("half-configured", log)


class AllowlistGuardTest(unittest.TestCase):
    """The one omission here that WIDENS access instead of removing a feature.

    `emit` drops an empty value, so an unset ALLOWED_USERS renders no line at
    all; the installer emits `google_chat_allowed_users = []`, the chart's
    `with` omits the key, and the operator reads an absent list as
    GOOGLE_CHAT_ALLOW_ALL_USERS=true (allowAllUsers in
    platformagent_manifests.go). Nothing reads the running CR's allowlist back,
    so on a long-lived environment a cleared variable is an unattended apply
    that admits the whole domain and loses the old list.
    """

    def test_chat_enabled_with_no_allowlist_is_refused_under_strict(self):
        rc, log, _ = render({**_COORDS, **_STRICT_SETTINGS}, strict=True)
        self.assertEqual(rc, 1)
        self.assertIn("ALLOWED_USERS", log)
        self.assertIn("::error title=Google Chat is enabled with no allowlist::", log)

    def test_an_allowlist_satisfies_it(self):
        rc, log, _ = render({**_COORDS, **_STRICT_OK}, strict=True)
        self.assertEqual(rc, 0, log)

    def test_allow_all_has_to_be_stated_and_then_is_accepted(self):
        rc, log, text = render(
            {**_COORDS, **_STRICT_SETTINGS, "GOOGLE_CHAT_ALLOW_ALL_USERS": "true"},
            strict=True)
        self.assertEqual(rc, 0, log)
        # It says the empty allowlist is intended; it is not itself a setting
        # the installer reads, so it must not reach install.env.
        self.assertNotIn("GOOGLE_CHAT_ALLOW_ALL_USERS", parse(text))
        self.assertNotIn("ALLOWED_USERS", parse(text))

    def test_slack_is_guarded_on_its_own_switch(self):
        rc, log, _ = render(
            {**_COORDS, **_STRICT_OK, "SLACK_ENABLED": "true"}, strict=True)
        self.assertEqual(rc, 1)
        self.assertIn("SLACK_ALLOWED_USERS", log)

        rc, log, _ = render(
            {**_COORDS, **_STRICT_OK, "SLACK_ENABLED": "true",
             "SLACK_ALLOWED_USERS": "someone@example.com"}, strict=True)
        self.assertEqual(rc, 0, log)

    def test_both_platforms_are_reported_in_one_run(self):
        """Same reason the missing-variable message names them all at once."""
        rc, log, _ = render(
            {**_COORDS, **_STRICT_SETTINGS, "SLACK_ENABLED": "true"}, strict=True)
        self.assertEqual(rc, 1)
        self.assertIn("Google Chat is enabled with no allowlist", log)
        self.assertIn("Slack is enabled with no allowlist", log)

    def test_enabled_means_what_it_means_to_the_installer(self):
        """A guard with its own vocabulary is a guard with its own blind spot.

        `is_truthy` accepts true/yes/y/1/on in any case, and it is what decides
        whether the composition provisions the integration at all — so a
        spelling it accepts and the guard does not is a configuration that
        installs Google Chat wide open and renders without complaint.
        """
        for spelling in ("true", "TRUE", "True", "yes", "y", "1", "on", "ON"):
            with self.subTest(spelling=spelling):
                rc, log, _ = render(
                    {**_COORDS, **_STRICT_SETTINGS,
                     "GOOGLE_CHAT_ENABLED": spelling}, strict=True)
                self.assertEqual(rc, 1, f"{spelling!r} was not read as enabled")
                self.assertIn("ALLOWED_USERS", log)

    def test_a_separator_only_allowlist_names_nobody_and_is_refused(self):
        """Non-empty to `-z`, empty to the installer — which is the gap.

        `hcl_csv_list` splits on `, \\t\\n` and drops empty items, so a list
        cleared down to a stray comma renders `google_chat_allowed_users = []`
        and admits the whole domain, exactly as an unset one does. The guard
        therefore has to measure emptiness the way the installer does rather
        than with a second expression of the rule.
        """
        for value in (" ", ",", ", ,", ",,", "\t", " , "):
            with self.subTest(value=value):
                rc, log, text = render(
                    {**_COORDS, **_STRICT_SETTINGS, "ALLOWED_USERS": value},
                    strict=True)
                self.assertEqual(
                    rc, 1, f"{value!r} names no users but was accepted")
                self.assertIn("Google Chat is enabled with no allowlist", log)
                self.assertNotIn("ALLOWED_USERS", parse(text))

    def test_an_integration_that_is_off_needs_no_allowlist(self):
        rc, log, _ = render(
            {**_COORDS, **_STRICT_SETTINGS, "GOOGLE_CHAT_ENABLED": "false"},
            strict=True)
        self.assertEqual(rc, 0, log)

    def test_the_guard_does_not_fire_without_strict(self):
        """The ephemeral path installs from nothing, so it takes nothing away."""
        rc, log, _ = render({**_COORDS, "GOOGLE_CHAT_ENABLED": "true"})
        self.assertEqual(rc, 0, log)


class MemoryMappingAgreementTest(unittest.TestCase):
    """Two live mappings from the same GitHub variables, so they must agree.

    `provision_environment.sh` builds an `install.sh` flag list for the
    destroy-and-rebuild path and carries its own copy of this translation.
    Collapsing the two into one is worth doing and has not been done, so this
    is all that keeps the copies from drifting: an environment would otherwise
    get a Hindsight store from one path and a Markdown file from the other.
    """

    def test_both_paths_recognise_the_same_provider_values(self):
        provision = (_REPO_ROOT / "scripts" / "release"
                     / "provision_environment.sh").read_text()
        renderer = _SCRIPT.read_text()
        for token in ("kube_agents_memory", "hindsight", "none", "off"):
            with self.subTest(token=token):
                self.assertIn(token, provision)
                self.assertIn(token, renderer)


class DefaultOnKeysAreRefusableTest(unittest.TestCase):
    """A shipped default of `true` inverts what an omission costs.

    The module docstring above states the hazard the renderer was built for: an
    omitted setting becomes a default and the apply destroys what the default
    does not mention. A key whose default is `true` runs the other way — the
    omission *provisions*, unattended, on a schedule. Either way the environment
    needs a way to say no, and MAPPING is the only one it has: the reconcile
    rewrites `install.env` from these variables every run, so a line added to
    that file by hand is gone before the next apply reads it.

    So this is the question nobody asked when ENABLE_DRIFT_DETECTOR's default
    was flipped: of the booleans `install.defaults.env` ships as `true`, which
    can a long-lived environment actually refuse? The answer was "not that one",
    and nothing failed.
    """

    # Keyed by the install.env name, valued by why MAPPING does not carry it.
    # Both are pre-existing and neither is this file's to fix; they are listed
    # so that the next `true` default has to be argued about rather than added.
    _NOT_MAPPED = {
        # Chooses where secrets go -- terraform.tfvars, or TF_VAR_* in the
        # process environment. Provisions nothing either way, so an environment
        # that cannot override it loses no cloud resource by the omission.
        "PERSIST_SECRETS_ON_DISK",
        # The same shape as the drift key and genuinely unaddressed: the default
        # enables the Vertex API on the serving project and grants the gateway's
        # role there, so an omission acts on a project rather than skipping it,
        # and a long-lived environment has no variable that declines. Narrower
        # in that nothing happens unless model_provider is vertex_ai. Left as it
        # was found -- flipping one default is not licence to widen the audit --
        # and tracked as issue #2269, which is what removes this entry.
        "VERTEX_MANAGE_SERVING_PROJECT",
    }

    def test_every_default_true_boolean_can_be_refused_by_an_environment(self):
        defaults = (_REPO_ROOT / "install.defaults.env").read_text()
        default_true = {
            m.group(1)
            for m in re.finditer(r'^DEFAULT_([A-Z0-9_]+)="?true"?\s*$',
                                 defaults, re.MULTILINE)
        }
        self.assertTrue(default_true, "no DEFAULT_*=true parsed; the regex is stale")

        block = _SCRIPT.read_text().split('MAPPING="', 1)[1].split('"', 1)[0]
        mapped = {line.split(":", 1)[0].strip()
                  for line in block.splitlines() if ":" in line}
        self.assertTrue(mapped, "MAPPING did not parse; the split is stale")

        unrefusable = sorted(default_true - mapped - self._NOT_MAPPED)
        self.assertEqual(
            unrefusable,
            [],
            "install.defaults.env ships these as true, so a long-lived "
            "environment that says nothing gets them -- and they are not in "
            "render_install_env.sh's MAPPING, so there is no GitHub variable "
            "that can decline them and no install.env line that survives the "
            f"next reconcile: {unrefusable}",
        )

    def test_the_exemptions_are_still_default_true(self):
        """An exemption for a key that no longer defaults to true is dead
        weight that would hide the next real one if the default came back."""
        defaults = (_REPO_ROOT / "install.defaults.env").read_text()
        for key in sorted(self._NOT_MAPPED):
            with self.subTest(key=key):
                self.assertRegex(
                    defaults,
                    re.compile(rf'^DEFAULT_{key}="?true"?\s*$', re.MULTILINE),
                )


class RenderingTest(unittest.TestCase):
    def test_an_unset_setting_is_omitted_rather_than_written_empty(self):
        """`KEY=` in install.env beats install.defaults.env and means "nothing".

        Which for MEMORY or PLATFORM_AGENT_PERMISSION_SET is a different install
        from the default one — and the difference is what an apply would destroy.
        """
        rc, log, text = render(dict(_COORDS))
        self.assertEqual(rc, 0, log)
        rendered = parse(text)
        self.assertNotIn("MODEL_DEFAULT_NAME", rendered)
        self.assertNotIn("SLACK_BOT_TOKEN", rendered)
        self.assertNotIn("MEMORY", rendered)
        self.assertNotIn("ENABLE_PUBSUB_PLATFORM", rendered)
        self.assertNotIn("ENABLE_STOCKOUT_INVESTIGATOR", rendered)

    def test_plugin_variables_are_rendered_when_set(self):
        rc, log, text = render({
            **_COORDS,
            "ENABLE_PUBSUB_PLATFORM": "true",
            "ENABLE_STOCKOUT_INVESTIGATOR": "false",
        })
        self.assertEqual(rc, 0, log)
        rendered = parse(text)
        self.assertEqual(rendered["ENABLE_PUBSUB_PLATFORM"], "true")
        self.assertEqual(rendered["ENABLE_STOCKOUT_INVESTIGATOR"], "false")

    def test_the_github_side_names_are_translated_to_installer_names(self):
        rc, log, text = render(dict(_COORDS))
        self.assertEqual(rc, 0, log)
        rendered = parse(text)
        self.assertEqual(rendered["PROJECT_ID"], "kube-agents-autopush")
        self.assertEqual(rendered["REGION"], "us-central1")
        self.assertEqual(rendered["CLUSTER_NAME"], "platform-agent-host")
        self.assertNotIn("GCP_PROJECT_ID", rendered)

    def test_memory_provider_maps_to_the_installers_vocabulary(self):
        for provider, expected in (
            ("kube_agents_memory", "hindsight"),
            ("hindsight", "hindsight"),
            ("none", "off"),
            ("off", "off"),
            ("multiuser_memory", "file"),
            ("anything-else", "file"),
        ):
            with self.subTest(provider=provider):
                _, _, text = render({**_COORDS, "MEMORY_PROVIDER": provider})
                self.assertEqual(parse(text)["MEMORY"], expected)

    def test_namespace_is_mapped(self):
        """NAMESPACE maps to NAMESPACE in install.env."""
        _, _, text = render({**_COORDS, "NAMESPACE": "kubeagents-system"})
        self.assertEqual(parse(text)["NAMESPACE"], "kubeagents-system")

        _, _, text = render({**_COORDS, "NAMESPACE": "other-ns"})
        self.assertEqual(parse(text)["NAMESPACE"], "other-ns")

    def test_a_value_with_shell_syntax_in_it_survives_a_round_trip(self):
        """install.env is SOURCED, so an unquoted value is executed, not read."""
        hostile = "a b; echo pwned $HOME `id`"
        _, _, text = render({**_COORDS, "SLACK_HOME_CHANNEL_NAME": hostile})
        self.assertEqual(parse(text)["SLACK_HOME_CHANNEL_NAME"], hostile)

    def test_google_chat_home_channel_is_mapped(self):
        _, _, text = render({**_COORDS, "GOOGLE_CHAT_HOME_CHANNEL": "spaces/TEST12345"})
        self.assertEqual(parse(text)["GOOGLE_CHAT_HOME_CHANNEL"], "spaces/TEST12345")

    def test_the_file_is_not_readable_by_anyone_else(self):
        """It carries the model provider's API key and the Slack tokens."""
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "install.env")
            subprocess.run(
                [str(_SCRIPT), out], capture_output=True, text=True,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **_COORDS},
                check=True,
            )
            self.assertEqual(os.stat(out).st_mode & 0o077, 0)

    def test_secret_values_are_never_echoed(self):
        """The listing it prints is keys only; the job log is world-readable."""
        rc, log, _ = render({**_COORDS, "GEMINI_API_KEY": "sk-not-a-real-key"})
        self.assertEqual(rc, 0, log)
        self.assertNotIn("sk-not-a-real-key", log)
        self.assertIn("GEMINI_API_KEY", log)


class PlatformAgentModeTest(unittest.TestCase):
    """PLATFORM_AGENT_MODE, the PlatformAgent's spec.mode, as the installer's install.env key.

    Mapped so a rendered install.env can carry `next`. `today` and unset must
    render the file an environment rendered before the key was mapped -- the
    installer reads an absent key as `today` -- and a value that is not a mode
    must stop the render before it writes anything.
    """

    def test_today_renders_exactly_what_unset_renders(self):
        for settings in (_COORDS, {**_COORDS, **_STRICT_OK}):
            with self.subTest(strict=settings is not _COORDS):
                strict = settings is not _COORDS
                rc_unset, log_unset, unset = render(settings, strict=strict)
                rc_today, log_today, today = render(
                    {**settings, "PLATFORM_AGENT_MODE": "today"}, strict=strict
                )
                self.assertEqual((rc_unset, rc_today), (0, 0), log_today)
                self.assertEqual(today, unset)
                # The log names the temporary output path, which differs per run.
                path = re.compile(r"\S*/install\.env")
                self.assertEqual(path.sub("OUT", log_today), path.sub("OUT", log_unset))
                self.assertNotIn("PLATFORM_AGENT_MODE", today)

    def test_next_is_written_under_the_installers_key(self):
        rc, log, text = render({**_COORDS, "PLATFORM_AGENT_MODE": "next"})
        self.assertEqual(rc, 0, log)
        self.assertEqual(parse(text).get("PLATFORM_AGENT_MODE"), "next")
        _, _, unset = render(_COORDS)
        self.assertEqual(
            [line for line in text.splitlines() if line not in unset.splitlines()],
            ["PLATFORM_AGENT_MODE=next"],
        )

    def test_a_value_that_is_not_a_mode_is_refused_before_anything_is_written(self):
        for value in ("Next", "nxt", "today ", "TODAY"):
            with self.subTest(value=value):
                rc, log, text = render({**_COORDS, "PLATFORM_AGENT_MODE": value})
                self.assertEqual(rc, 1)
                self.assertEqual(text, "")
                self.assertIn("::error title=PLATFORM_AGENT_MODE is not a mode::", log)


if __name__ == "__main__":
    unittest.main()
