"""Unit tests for cluster_agent_profile.profile_name (the kanban assignee resolver).

Run: python3 -m unittest agents.platform.scripts.test_cluster_agent_profile

profile_name is a pure, deterministic function; the module imports without pyyaml
(that import is lazy, only on the scaffold path).
"""

import io
import os
import re
import shutil
import subprocess
import yaml
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
# profile_overlay and profile_plugins ship beside these scripts in the image
# (/opt/defaults/scripts); in the repo they live in deploy/shared.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "deploy" / "shared"))

import cluster_agent_profile as cap  # noqa: E402
import terminal_env_pin  # noqa: E402

MAX = cap.MAX_NAME_LEN  # 63


class ProfileNameTest(unittest.TestCase):
    def test_basic_shape(self):
        self.assertEqual(
            cap.profile_name("agentic-harness-demo", "kage-management", "us-central1"),
            "cluster-agentic-harness-demo-kage-management-us-central1",
        )

    def test_deterministic(self):
        a = cap.profile_name("p", "c", "us-central1")
        b = cap.profile_name("p", "c", "us-central1")
        self.assertEqual(a, b)

    def test_valid_profile_id_chars(self):
        # Only lowercase alnum + dashes; matches Hermes _PROFILE_ID_RE expectations.
        name = cap.profile_name("Proj_X", "My.Cluster", "US-Central1")
        self.assertRegex(name, r"^[a-z0-9][a-z0-9-]*$")
        self.assertLessEqual(len(name), MAX)

    def test_collapses_and_lowercases(self):
        # Uppercase + non-alnum runs collapse to single dashes.
        name = cap.profile_name("A__B", "c//d", "e")
        self.assertEqual(name, "cluster-a-b-c-d-e")

    def test_long_name_is_hashed_and_bounded(self):
        long_cluster = "x" * 120
        name = cap.profile_name("proj", long_cluster, "us-central1")
        self.assertLessEqual(len(name), MAX)
        # hashed form ends with -<8 hex>
        self.assertRegex(name, r"-[0-9a-f]{8}$")

    def test_long_name_stable_hash(self):
        long_cluster = "y" * 120
        self.assertEqual(
            cap.profile_name("proj", long_cluster, "loc"),
            cap.profile_name("proj", long_cluster, "loc"),
        )


class PinKubeconfigEnvTest(unittest.TestCase):
    def test_writes_kubeconfig_line(self):
        home = Path(tempfile.mkdtemp())
        kubeconfig = home / "kubeconfig.yaml"
        cap._pin_kubeconfig_env(home, kubeconfig)
        self.assertEqual((home / ".env").read_text(), f"KUBECONFIG={kubeconfig}\n")

    def test_idempotent_and_preserves_other_lines(self):
        home = Path(tempfile.mkdtemp())
        kubeconfig = home / "kubeconfig.yaml"
        (home / ".env").write_text("FOO=bar\nKUBECONFIG=/stale/path\n")
        cap._pin_kubeconfig_env(home, kubeconfig)
        cap._pin_kubeconfig_env(home, kubeconfig)  # second run must not duplicate
        text = (home / ".env").read_text()
        self.assertEqual(text.count("KUBECONFIG="), 1)
        self.assertIn("FOO=bar\n", text)
        self.assertIn(f"KUBECONFIG={kubeconfig}\n", text)


class PushSandboxLayoutTest(unittest.TestCase):
    """The mirror pass step 2e runs so the sandbox has somewhere to write.

    It shells out to another pod's worth of SSH round trips, and it runs on the
    reconcile path, so the two things worth pinning are that it is bounded and
    that it cannot take the scaffold down with it.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mirror = self.tmp / "sandbox_mirror.py"
        self.mirror.write_text("")
        self._patch(cap, "SANDBOX_MIRROR", self.mirror)

    def _patch(self, obj, attr, value):
        original = getattr(obj, attr)
        setattr(obj, attr, value)
        self.addCleanup(setattr, obj, attr, original)

    def push(self):
        with redirect_stderr(io.StringIO()) as err:
            cap._push_sandbox_layout("cluster-one")
        return err.getvalue()

    def test_the_mirror_call_is_bounded(self):
        # The reconcile engine calls this once per onboarded cluster. An
        # unbounded wait on a sandbox that is up but wedged stops the reconcile
        # for every cluster behind this one, not just this one.
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            self.push()
        self.assertEqual(
            run.call_args.kwargs["timeout"], cap.SANDBOX_MIRROR_TIMEOUT_SECONDS
        )
        argv = run.call_args.args[0]
        self.assertIn("--skeleton-only", argv)
        # Past the mirror's own wait, so this timeout only fires when the
        # mirror itself is stuck rather than racing it.
        self.assertLess(
            float(argv[argv.index("--wait") + 1]), cap.SANDBOX_MIRROR_TIMEOUT_SECONDS
        )

    def test_a_mirror_that_hangs_does_not_fail_the_scaffold(self):
        # Without the sandbox layout the new profile's shell starts in the
        # machine home, which is where it starts anyway. Raising here would
        # instead leave the cluster unonboarded over a cosmetic difference.
        timed_out = subprocess.TimeoutExpired(
            cmd=[str(self.mirror)], timeout=cap.SANDBOX_MIRROR_TIMEOUT_SECONDS
        )
        with mock.patch.object(subprocess, "run", side_effect=timed_out):
            stderr = self.push()
        self.assertIn("could not push the profile layout", stderr)

    def test_a_mirror_that_exits_non_zero_does_not_fail_the_scaffold(self):
        failed = subprocess.CalledProcessError(1, [str(self.mirror)], "", "no route")
        with mock.patch.object(subprocess, "run", side_effect=failed):
            stderr = self.push()
        self.assertIn("could not push the profile layout", stderr)

    def test_an_install_without_the_mirror_script_runs_nothing(self):
        self._patch(cap, "SANDBOX_MIRROR", self.tmp / "absent.py")
        with mock.patch.object(subprocess, "run") as run:
            self.push()
        run.assert_not_called()


# --- The runtime scaffold path --------------------------------------------------
#
# A cluster profile is created when its cluster is onboarded, which is not a pod start:
# nothing rolls the agent, so this path — not docker-entrypoint.sh — is the only thing
# that can give the new profile the operator's tuning and the plugins targeted at it.


class CreateProfileTest(unittest.TestCase):
    PROJECT, CLUSTER, LOCATION = "proj", "clu", "us-east1"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.home_root = self.tmp / "data"
        self.template = self.tmp / "cluster-template"
        self.overlay_dir = self.tmp / "agent-config"
        self.mounts = self.tmp / "agent-plugins"
        self.template.mkdir()
        self.overlay_dir.mkdir()
        (self.template / "config.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["hermes_otel"]}, "toolsets": ["kanban"]})
        )

        self.name = cap.profile_name(self.PROJECT, self.CLUSTER, self.LOCATION)
        self.profile = self.home_root / "profiles" / self.name

        self._patch(cap, "HERMES_HOME", self.home_root)
        self._patch(cap, "PROFILES_BASE", self.home_root / "profiles")
        self._patch(cap, "TEMPLATE_DIR", self.template)
        self._patch(cap, "SHARED_PLUGINS_DIR", self.tmp / "shared-plugins")
        self._patch(cap, "OVERLAY_DIR", self.overlay_dir)
        self._patch(cap, "PLUGIN_MOUNT_ROOT", self.mounts)
        # `hermes profile create` — the real one registers the profile and makes its home.
        self._patch(cap, "ensure_profile", self._fake_ensure_profile)
        # `gcloud container clusters get-credentials`.
        self.runs = []
        self.writes_kubeconfig = True
        self._patch(subprocess, "run", self._fake_run)
        # Whether that command needs --dns-endpoint. Patched explicitly rather
        # than left to fall out of the fake above: gke_endpoint reads gcloud's
        # help text to decide the flag exists at all, the fake answers every
        # command with empty stdout, and the resulting "no flag" would be an
        # accident of the mock rather than a decision the test made. The
        # predicate itself is covered in test_gke_endpoint.py.
        self.dns_args = []
        self._patch(cap, "dns_endpoint_args", lambda *a, **k: self.dns_args)
        # The managed terminal pin needs Hermes, which is not installed here; its own
        # behaviour is covered in tests/test_terminal_env_pin.py and at image build.
        self.pinned = []
        self.pin_error = None
        self._patch(terminal_env_pin, "pin", self._fake_pin)

    def _patch(self, obj, attr, value):
        original = getattr(obj, attr)
        setattr(obj, attr, value)
        self.addCleanup(setattr, obj, attr, original)

    def _fake_pin(self, home):
        self.pinned.append((Path(home), (Path(home) / "USER.md").exists()))
        if self.pin_error:
            raise self.pin_error
        return True

    def _fake_ensure_profile(self, name, description, hermes_home):
        home = Path(hermes_home) / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "profile.yaml").write_text(f"name: {name}\n")
        return home

    def _fake_run(self, cmd, **kwargs):
        self.runs.append(cmd)
        # Real gcloud writes the file its KUBECONFIG names, and the scaffold
        # checks that it did before calling the profile finished. A fake that
        # exits 0 without writing is a fake of the failure, not of the success.
        if (
            self.writes_kubeconfig
            and cmd[:4] == ["gcloud", "container", "clusters", "get-credentials"]
        ):
            path = Path((kwargs.get("env") or {})["KUBECONFIG"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("apiVersion: v1\nkind: Config\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def get_credentials_argv(self):
        for cmd in self.runs:
            if cmd[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
                return cmd
        raise AssertionError(f"no get-credentials call in {self.runs}")

    def mount(self, plugin):
        d = self.mounts / self.name / plugin
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text("")
        return d

    def create(self):
        with redirect_stderr(io.StringIO()) as err:
            name = cap.create_profile(self.PROJECT, self.CLUSTER, self.LOCATION)
        self.stderr = err.getvalue()
        return name

    def config(self):
        return yaml.safe_load((self.profile / "config.yaml").read_text()) or {}

    def test_a_gcloud_that_wrote_nothing_is_not_a_scaffolded_profile(self):
        """Exit 0 from gcloud is not the same statement as a credential.

        The command runs in the sandbox and writes there, so this pod cannot
        see the file either way. Reporting the profile as created leaves every
        later kubectl failing with an error that names the cluster, and sends
        whoever reads it to IAM instead of to the pod that never got a
        kubeconfig.
        """
        self.writes_kubeconfig = False

        with self.assertRaises(SystemExit) as caught:
            self.create()

        self.assertIn("kubeconfig", str(caught.exception))
        # And the pin that follows it did not happen, so nothing points a
        # worker at a file that is not there.
        env_file = self.profile / ".env"
        self.assertNotIn("KUBECONFIG", env_file.read_text() if env_file.exists() else "")

    def test_pins_the_managed_terminal_before_writing_user_md(self):
        self.create()
        self.assertEqual([(self.profile, False)], self.pinned)
        self.assertTrue((self.profile / "USER.md").exists())

    def test_a_failed_terminal_pin_leaves_the_profile_unfinished(self):
        """USER.md is what reconcile and the roster read as "finished".

        A re-scaffold starts with the USER.md an earlier run wrote, so a failed pin
        has to remove it, or the profile keeps counting as usable while its
        scheduled runs would not use the managed terminal.
        """
        self.create()
        self.pin_error = terminal_env_pin.PinError("Hermes resolves TERMINAL_ENV='local'")

        with self.assertRaises(SystemExit) as caught:
            self.create()

        self.assertIn(self.name, str(caught.exception))
        self.assertIn("TERMINAL_ENV='local'", str(caught.exception))
        self.assertFalse((self.profile / "USER.md").exists())

    def test_the_kubeconfig_is_looked_for_where_kubectl_will_run(self):
        """With a sandbox the file is on its volume, not on this one."""
        missing = self.tmp / "nowhere" / "kubeconfig.yaml"
        with mock.patch.object(cap.sandbox_exec, "sandbox_enabled", return_value=True):
            probe = subprocess.CompletedProcess(["test"], 0, "", "")
            with mock.patch.object(cap.sandbox_exec, "run", return_value=probe) as run:
                self.assertTrue(cap.kubeconfig_landed(missing))
            self.assertEqual(run.call_args[0][0][1:], ["-s", str(missing)])

            probe = subprocess.CompletedProcess(["test"], 1, "", "")
            with mock.patch.object(cap.sandbox_exec, "run", return_value=probe):
                self.assertFalse(cap.kubeconfig_landed(missing))

            # A sandbox that went away between the fetch and the check answers
            # nothing, which is not the same as answering yes.
            unavailable = cap.sandbox_exec.SandboxUnavailable("gone")
            with mock.patch.object(cap.sandbox_exec, "run", side_effect=unavailable):
                self.assertFalse(cap.kubeconfig_landed(missing))

    def test_fetches_credentials_over_the_ip_endpoint_by_default(self):
        self.create()
        self.assertEqual(
            self.get_credentials_argv(),
            [
                "gcloud", "container", "clusters", "get-credentials", self.CLUSTER,
                f"--location={self.LOCATION}",
                f"--project={self.PROJECT}",
            ],
        )

    def test_the_credential_fetch_runs_as_the_login_that_owns_the_profile(self):
        """Step 2e mirrors the profile directory in as `agent:agent` 0755.

        The default `hermes` login is uid 1001 and cannot create a file in it,
        so a get-credentials that kept the default exits on EACCES and the
        profile is scaffolded without the kubeconfig every later kubectl reads.
        The alternative — widening the directory so uid 1001 could write into a
        tree uid 1000 owns — is what this assertion exists instead of.
        """
        original = cap.sandbox_exec.run
        principals = {}

        def spy(argv, **kwargs):
            if argv[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
                principals["get-credentials"] = kwargs.get(
                    "principal", cap.sandbox_exec.SANDBOX_PRINCIPAL
                )
            return original(argv, **kwargs)

        self._patch(cap.sandbox_exec, "run", spy)
        self.create()

        self.assertEqual(
            principals.get("get-credentials"),
            cap.sandbox_exec.TERMINAL_PRINCIPAL,
            "get-credentials writes into the agent-owned profile directory",
        )

    def test_fetches_credentials_over_the_dns_endpoint_when_detected(self):
        # An onboarded cluster whose control plane is only reachable by DNS. The
        # profile's pinned kubeconfig is what the Cluster Agent runs against for
        # its whole life, so the flag has to be present when it is scaffolded.
        self.dns_args = ["--dns-endpoint"]

        self.create()

        self.assertEqual(self.get_credentials_argv()[-1], "--dns-endpoint")

    def test_applies_the_cluster_class_overlay_at_scaffold_time(self):
        (self.overlay_dir / "profileclass-cluster.overlay.yaml").write_text(
            yaml.safe_dump({"agent": {"api_max_retries": 8, "max_turns": 150}})
        )

        self.assertEqual(self.create(), self.name)

        cfg = self.config()
        self.assertEqual(cfg["agent"], {"api_max_retries": 8, "max_turns": 150})
        self.assertEqual(cfg["plugins"]["enabled"], ["hermes_otel"], "the template's config must survive")
        self.assertEqual(
            cfg["cluster_identity"],
            {"project": self.PROJECT, "cluster": self.CLUSTER, "location": self.LOCATION},
            "the identity stamp the reconciler matches on must survive the merge",
        )

    def test_links_and_enables_a_plugin_targeting_this_cluster(self):
        self.mount("clusterone")
        (self.overlay_dir / "profileclass-cluster.overlay.yaml").write_text(
            yaml.safe_dump({"agent": {"max_turns": 150}})
        )
        (self.overlay_dir / f"profile-{self.name}.overlay.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["clusterone"]}})
        )

        self.create()

        link = self.profile / "plugins" / "clusterone"
        self.assertTrue(link.is_symlink(), "the plugin mounted for this profile must be linked in")
        self.assertTrue((link / "__init__.py").is_file())
        cfg = self.config()
        self.assertEqual(cfg["plugins"]["enabled"], ["hermes_otel", "clusterone"], "and enabled")
        self.assertEqual(cfg["agent"]["max_turns"], 150, "alongside the class-wide tuning")

    def test_no_operator_overlays_is_not_a_failure(self):
        """A deployment without the operator has no /opt/agent-config and no mounts."""
        self._patch(cap, "OVERLAY_DIR", self.tmp / "does-not-exist")
        self._patch(cap, "PLUGIN_MOUNT_ROOT", self.tmp / "also-missing")

        self.assertEqual(self.create(), self.name)

        cfg = self.config()
        self.assertNotIn("agent", cfg, "nothing to apply means nothing applied")
        self.assertIn("cluster_identity", cfg)

    def test_rescaffolding_does_not_double_apply(self):
        (self.overlay_dir / "profileclass-cluster.overlay.yaml").write_text(
            yaml.safe_dump({"agent": {"max_turns": 150}, "plugins": {"enabled": ["extra"]}})
        )
        self.create()
        first = self.config()
        self.create()
        self.assertEqual(self.config(), first)

    def test_every_user_md_field_is_readable_by_preflight(self):
        """USER.md's shape is a contract with cluster_preflight.sh's user_md_field().

        That reader matches only `- <key>: <value>` bullets. The kubeconfig line
        shipped for a long time without the leading `- `, so it parsed as
        nothing — and it is precisely the line a human edits when trying to
        repair a bad pin. Assert all four fields are bullets, so the writer
        cannot drift back out of the shape the reader can see.
        """
        self.create()
        user_md = (self.profile / "USER.md").read_text().lower()

        expected = {
            "project": self.PROJECT,
            "cluster": self.CLUSTER,
            "location": self.LOCATION,
            "kubeconfig": str(self.profile / "kubeconfig.yaml").lower(),
        }
        for key, value in expected.items():
            # The Python equivalent of the script's
            # `sed -n "s/^[[:space:]]*-[[:space:]]*<key>:[[:space:]]*//p"`.
            found = re.findall(rf"^[ \t]*-[ \t]*{key}:[ \t]*(.*)$", user_md, re.MULTILINE)
            self.assertTrue(found, f"`{key}` is not a `- {key}:` bullet, so preflight cannot read it")
            self.assertEqual(found[0].strip(), value)

    def test_preflight_still_parses_user_md_with_a_dash_anchor(self):
        """The other half of the contract above: if the reader stops requiring
        the `- ` anchor, this test is the note that USER.md's shape was written
        to satisfy it and can be relaxed too."""
        script = (Path(__file__).parent / "cluster_preflight.sh").read_text()
        self.assertIn(
            r"s/^[[:space:]]*-[[:space:]]*$1:[[:space:]]*//p",
            script,
            "user_md_field() changed; re-check the USER.md format written by create_profile",
        )


    # --- telemetry ---
    #
    # The profile copies the image's plugin config, so it also copies its endpoint.
    # Nothing rolls the pod when a cluster is onboarded, so the entrypoint's startup sweep
    # never sees this profile: without the scaffold-time pin, a cluster agent would export
    # to the GKE managed collector no matter what the operator resolved.

    BAKED = "http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces"
    CUSTOM = "http://otel-collector.otel-collector.svc.cluster.local:4318"

    def bake_shared_plugin(self):
        """The hermes_otel config as the image ships it, ready for the overlay to copy."""
        shared = self.tmp / "shared-plugins" / "hermes_otel"
        shared.mkdir(parents=True, exist_ok=True)
        (shared / "config.yaml").write_text(
            yaml.safe_dump({"backends": [{"name": "gke-managed-otel", "type": "otlp", "endpoint": self.BAKED}]})
        )

    def plugin_config(self):
        return yaml.safe_load((self.profile / "plugins" / "hermes_otel" / "config.yaml").read_text())

    def test_pins_the_resolved_endpoint(self):
        self.bake_shared_plugin()
        with mock.patch.dict(
            os.environ,
            {"OTEL_EXPORTER_OTLP_ENDPOINT": self.CUSTOM, "OTEL_SERVICE_NAME": "agent-gateway"},
        ):
            self.create()

        cfg = self.plugin_config()
        self.assertEqual(cfg["backends"][0]["endpoint"], self.CUSTOM + "/v1/traces")
        self.assertEqual(cfg["resource_attributes"]["service.name"], "agent-gateway")

    def test_unset_endpoint_keeps_the_image_default(self):
        self.bake_shared_plugin()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            os.environ.pop("OTEL_SERVICE_NAME", None)
            os.environ.pop("OTEL_SDK_DISABLED", None)
            os.environ.pop("HERMES_OTEL_ENABLED", None)
            self.create()

        self.assertEqual(self.plugin_config()["backends"][0]["endpoint"], self.BAKED)

    def test_disabled_telemetry_disables_plugin_and_clears_backends(self):
        self.bake_shared_plugin()
        with mock.patch.dict(
            os.environ,
            {"OTEL_SDK_DISABLED": "true", "OTEL_SERVICE_NAME": "agent-gateway"},
        ):
            self.create()

        cfg = self.plugin_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["backends"], [])

    def test_hermes_otel_enabled_false_disables_plugin(self):
        self.bake_shared_plugin()
        with mock.patch.dict(
            os.environ,
            {"HERMES_OTEL_ENABLED": "false", "OTEL_SERVICE_NAME": "agent-gateway"},
        ):
            self.create()

        cfg = self.plugin_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["backends"], [])

    def test_hermes_otel_enabled_true_overrides_otel_sdk_disabled(self):
        self.bake_shared_plugin()
        with mock.patch.dict(
            os.environ,
            {
                "HERMES_OTEL_ENABLED": "true",
                "OTEL_SDK_DISABLED": "true",
                "OTEL_EXPORTER_OTLP_ENDPOINT": self.CUSTOM,
            },
        ):
            self.create()

        cfg = self.plugin_config()
        self.assertTrue(cfg.get("enabled", True))
        self.assertEqual(cfg["backends"][0]["endpoint"], self.CUSTOM + "/v1/traces")

    def test_disabled_telemetry_handles_mixed_case(self):
        self.bake_shared_plugin()
        with mock.patch.dict(
            os.environ,
            {"OTEL_SDK_DISABLED": "TRUE", "OTEL_SERVICE_NAME": "agent-gateway"},
        ):
            self.create()

        cfg = self.plugin_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["backends"], [])


class ResolveProfilesBaseTest(unittest.TestCase):
    def test_resolves_when_hermes_home_is_set(self):
        import importlib
        try:
            with mock.patch.dict(os.environ, {"HERMES_HOME": "/custom/data"}, clear=True):
                reloaded = importlib.reload(cap)
                self.assertEqual(reloaded.HERMES_HOME, Path("/custom/data"))
                self.assertEqual(reloaded.PROFILES_BASE, Path("/custom/data/profiles"))
                self.assertEqual(reloaded._run_env()["HERMES_HOME"], "/custom/data")
        finally:
            importlib.reload(cap)

    def test_resolves_default_when_no_env_set(self):
        import importlib
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                reloaded = importlib.reload(cap)
                self.assertEqual(reloaded.HERMES_HOME, Path("/opt/data"))
                self.assertEqual(reloaded.PROFILES_BASE, Path("/opt/data/profiles"))
                self.assertEqual(reloaded._run_env()["HERMES_HOME"], "/opt/data")
        finally:
            importlib.reload(cap)


class ListProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cap-list-test-"))
        self.patcher = mock.patch.object(cap, "PROFILES_BASE", self.tmp)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nonexistent_directory_returns_empty(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(cap.list_profiles(), [])

    def test_filters_reserved_and_files(self):
        (self.tmp / "default").mkdir()
        (self.tmp / "platform").mkdir()
        (self.tmp / "not-a-dir.txt").touch()
        (self.tmp / "cluster-beta").mkdir()
        (self.tmp / "cluster-alpha").mkdir()

        self.assertEqual(cap.list_profiles(), ["cluster-alpha", "cluster-beta"])

    def test_cmd_list_prints_ready_sorted_by_default(self):
        for name in ("cluster-zeta", "cluster-beta"):
            p = self.tmp / name
            p.mkdir(parents=True, exist_ok=True)
            (p / "profile.yaml").touch()
            (p / "USER.md").write_text("- project: p\n- cluster: c\n- location: l\n", encoding="utf-8")
            (p / "config.yaml").write_text(
                "cluster_identity:\n  project: p\n  cluster: c\n  location: l\n",
                encoding="utf-8",
            )
        (self.tmp / "cluster-incomplete").mkdir()
        (self.tmp / "cluster-incomplete" / "config.yaml").write_text(
            "cluster_identity:\n  project: p\n  cluster: c\n  location: l\n",
            encoding="utf-8",
        )

        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            cap.cmd_list(mock.MagicMock(all=False))
        self.assertEqual(out.getvalue(), "cluster-beta\ncluster-zeta\n")

        out_all = io.StringIO()
        with mock.patch("sys.stdout", out_all):
            cap.cmd_list(mock.MagicMock(all=True))
        self.assertEqual(out_all.getvalue(), "cluster-beta\ncluster-incomplete\ncluster-zeta\n")

    def test_build_parser_list_all(self):
        parser = cap.build_parser()
        args = parser.parse_args(["list"])
        self.assertFalse(args.all)
        args_all = parser.parse_args(["list", "--all"])
        self.assertTrue(args_all.all)


class ListReadyProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cap-ready-test-"))
        self.patcher = mock.patch.object(cap, "PROFILES_BASE", self.tmp)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scaffold(self, name: str, user_md: bool = True, identity: bool = True, scaffolded: bool = True):
        p = self.tmp / name
        p.mkdir(parents=True, exist_ok=True)
        if scaffolded:
            (p / "profile.yaml").touch()
        if user_md:
            (p / "USER.md").write_text("- project: p\n- cluster: c\n- location: l\n", encoding="utf-8")
        if identity:
            (p / "config.yaml").write_text(
                "cluster_identity:\n  project: p\n  cluster: c\n  location: l\n",
                encoding="utf-8",
            )
        return p

    def test_ready_profiles_filters_incomplete_scaffolds(self):
        self._scaffold("cluster-ready")
        self._scaffold("cluster-no-user", user_md=False, identity=True)
        self._scaffold("cluster-unregistered", scaffolded=False)
        self._scaffold("default")
        self._scaffold("platform")

        self.assertEqual(cap.list_ready_profiles(), ["cluster-ready"])

    def test_ready_profiles_does_not_probe_sandbox_or_call_kubeconfig_landed(self):
        self._scaffold("cluster-ready")
        with mock.patch.object(cap, "kubeconfig_landed", side_effect=AssertionError("kubeconfig_landed should not be called")):
            self.assertEqual(cap.list_ready_profiles(), ["cluster-ready"])

    def test_ready_profiles_tolerates_corrupt_config_yaml(self):
        self._scaffold("cluster-ready")
        p_corrupt = self._scaffold("cluster-corrupt", user_md=True, identity=False)
        (p_corrupt / "config.yaml").write_text("invalid yaml: {{\n", encoding="utf-8")

        # Ready profiles match the dispatcher capability: scaffolded and USER.md present
        self.assertEqual(cap.list_ready_profiles(), ["cluster-corrupt", "cluster-ready"])

    def test_read_cluster_identity_robustness(self):
        # 1. Nonexistent directory
        self.assertIsNone(cap.read_cluster_identity(self.tmp / "nonexistent"))

        # 2. Scalar and list YAML raise AttributeError per docstring specification
        p1 = self.tmp / "p1"
        p1.mkdir()
        (p1 / "config.yaml").write_text("scalar_value\n", encoding="utf-8")
        with self.assertRaises(AttributeError):
            cap.read_cluster_identity(p1)

        (p1 / "config.yaml").write_text("[item1, item2]\n", encoding="utf-8")
        with self.assertRaises(AttributeError):
            cap.read_cluster_identity(p1)

        # 3. Non-dict cluster_identity
        (p1 / "config.yaml").write_text("cluster_identity: 12345\n", encoding="utf-8")
        self.assertIsNone(cap.read_cluster_identity(p1))

        # 4. Incomplete fields
        (p1 / "config.yaml").write_text("cluster_identity:\n  project: p\n", encoding="utf-8")
        self.assertIsNone(cap.read_cluster_identity(p1))

        # 5. Invalid YAML syntax
        (p1 / "config.yaml").write_text("{{invalid-yaml\n", encoding="utf-8")
        self.assertIsNone(cap.read_cluster_identity(p1))

        # 6. Valid cluster_identity
        (p1 / "config.yaml").write_text(
            "cluster_identity:\n  project: p\n  cluster: c\n  location: l\n", encoding="utf-8"
        )
        self.assertEqual(
            cap.read_cluster_identity(p1),
            {"project": "p", "cluster": "c", "location": "l"},
        )

        # 7. Non-UTF-8 bytes raises UnicodeDecodeError
        p_bin = self.tmp / "p_bin"
        p_bin.mkdir()
        (p_bin / "config.yaml").write_bytes(b"\xff\xfe")
        with self.assertRaises(UnicodeDecodeError):
            cap.read_cluster_identity(p_bin)

        # 8. Directory config.yaml raises OSError
        p_dir = self.tmp / "p_dir"
        p_dir.mkdir()
        (p_dir / "config.yaml").mkdir()
        with self.assertRaises(OSError):
            cap.read_cluster_identity(p_dir)



class SandboxStubTest(unittest.TestCase):
    def setUp(self):
        repo_root = Path(__file__).resolve().parents[3]
        self.stub_path = repo_root / "deploy" / "sandbox" / "agent-pod-only-stub.py"
        self.assertTrue(self.stub_path.is_file(), f"missing {self.stub_path}")
        self.tmp = Path(tempfile.mkdtemp(prefix="sandbox-stub-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stub_refuses_execution(self):
        wrapper = self.tmp / "cluster_agent_profile.py"
        wrapper.symlink_to(self.stub_path)

        res = subprocess.run(
            [sys.executable, str(wrapper), "list"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("cluster_agent_profile.py does not run in the shell sandbox", res.stderr)
        self.assertIn("Report the request as blocked", res.stderr)


class ClusterAgentLifecycleDelegationDocumentationTest(unittest.TestCase):
    def setUp(self):
        repo_root = Path(__file__).resolve().parents[3]
        self.skill_path = (
            repo_root / "agents" / "platform" / "skills" / "cluster-agent-lifecycle" / "SKILL.md"
        )
        self.assertTrue(self.skill_path.is_file(), f"missing {self.skill_path}")
        self.content = self.skill_path.read_text(encoding="utf-8")

    def test_delegation_step_1_mandates_fleet_enumeration_before_asking(self):
        # Must isolate Step 1 of delegation to ensure the unlocated branch is
        # strictly present in the delegation procedure, rather than relying
        # only on whole-document presence assertions.
        step_1_start = self.content.find("1. **Resolve the cluster's profile name**")
        self.assertNotEqual(step_1_start, -1, "missing Step 1 in SKILL.md")
        step_2_start = self.content.find("2. **Create the card**", step_1_start)
        self.assertNotEqual(step_2_start, -1, "missing Step 2 in SKILL.md")
        step_1 = self.content[step_1_start:step_2_start]

        self.assertIn("If the request does NOT name a cluster", step_1)
        self.assertIn("Do not ask the user which cluster before searching", step_1)
        self.assertIn("list_cluster_profiles()", step_1)
        self.assertIn("cluster_agent_profile.py list", step_1)
        self.assertIn("get_k8s_resource", step_1)
        self.assertIn("describe_k8s_resource", step_1)
        self.assertIn("Do not create throwaway kanban probe cards", step_1)
        self.assertIn("Never resolve silently", step_1)
        self.assertRegex(step_1, r"[Aa]sk only after (looking|checking|searching)")

    def test_delegation_handles_unnamed_cluster_via_fleet_enumeration(self):
        # The procedure must explicitly guide resolution when the cluster name is omitted (#953).
        self.assertIn("list_cluster_profiles()", self.content)
        self.assertIn("get_cluster_profile_name", self.content)
        self.assertIn("cluster_agent_profile.py list", self.content)
        # Must instruct checking before asking the user
        self.assertIn("existence", self.content.lower())
        # Must instruct polling to settlement and waiting before completing in fan-out
        self.assertIn("settlement", self.content.lower())
        self.assertIn("sleep 60", self.content)
        # Must handle ready cards without false timeouts
        self.assertIn("Do NOT classify cards in `ready` as timed out", self.content)
        self.assertIn("never complete while cards remain queued in `ready`", self.content)
        # Must acknowledge configurable concurrency (spec.harness.tuning.maxInProgress) rather than assuming a static cap
        self.assertIn("spec.harness.tuning.maxInProgress", self.content)
        # Must instruct asking only after searching / looking
        self.assertRegex(
            self.content,
            r"[Aa]sk only after (looking|checking|searching)",
        )
        # Must require identifying which cluster was picked in the report
        self.assertIn("Never resolve silently", self.content)
        # Must instruct completing with the answer you have if a worker blocks or times out
        self.assertRegex(
            self.content,
            r"[Cc]omplete with the answer you have",
        )


class UnlocatedCrashloopTaskSpecTest(unittest.TestCase):
    def setUp(self):
        repo_root = Path(__file__).resolve().parents[3]
        self.task_path = (
            repo_root
            / "bench"
            / "tasks"
            / "cluster-agent-unlocated-crashloop-debug"
            / "task.yaml"
        )
        self.assertTrue(self.task_path.is_file(), f"missing {self.task_path}")
        self.data = yaml.safe_load(self.task_path.read_text(encoding="utf-8"))

    def test_no_stubbed_profile_scripts_forbids_stubbed_scripts(self):
        spec = self.data.get("verification_spec", [])
        no_stubbed = next(
            (c for c in spec if c.get("name") == "no-stubbed-profile-scripts"),
            None,
        )
        self.assertIsNotNone(no_stubbed, "missing no-stubbed-profile-scripts check")
        assert no_stubbed is not None
        forbidden = no_stubbed.get("check", {}).get("forbidden_patterns", [])
        self.assertTrue(
            any("cluster_agent_profile" in p for p in forbidden),
            "cluster_agent_profile pattern must be present",
        )
        self.assertTrue(
            any("kanban_notify_propagate" in p for p in forbidden),
            "kanban_notify_propagate pattern must be present",
        )
        kubectl_pats = [p for p in forbidden if "kubectl" in p]
        self.assertEqual(kubectl_pats, [], "kubectl should not be in no-stubbed-profile-scripts")

        matching_commands = [
            "python3 /opt/data/scripts/cluster_agent_profile.py list",
            "cluster_agent_profile.py name --cluster foo",
            "/opt/data/scripts/cluster_agent_profile.py",
            "kanban_notify_propagate.py",
            "/opt/data/scripts/kanban_notify_propagate.py",
            "cd /opt/data/scripts && python3 -m cluster_agent_profile list",
            "python3 -m cluster_agent_profile",
            "python3 -u -m cluster_agent_profile list",
            "python3 -um cluster_agent_profile list",
            "python3 -m 'cluster_agent_profile' list",
            'python3 -m "cluster_agent_profile" list',
            "python3 -um 'cluster_agent_profile' list",
            'python3 -um "cluster_agent_profile" list',
            "python3 -W ignore -m cluster_agent_profile list",
            "python3 -X dev -m cluster_agent_profile",
            "python3 -mcluster_agent_profile list",
            "python3 -m kanban_notify_propagate",
            "python3 -um kanban_notify_propagate",
            "python3 -m 'kanban_notify_propagate'",
            'python3 -m "kanban_notify_propagate"',
            "python3 -W error -mkanban_notify_propagate",
            "python3 -X dev -m kanban_notify_propagate",
            "python -m cluster_agent_profile",
            # Module execution via runpy
            "python3 -m runpy cluster_agent_profile",
            "python3 -um runpy cluster_agent_profile",
            "python3 -m 'runpy' cluster_agent_profile",
            'python3 -m "runpy" cluster_agent_profile',
            "python3 -m runpy 'cluster_agent_profile'",
            'python3 -m runpy "cluster_agent_profile"',
            "python3 -m runpy kanban_notify_propagate",
            # Inline -c execution / imports
            'python3 -c "import cluster_agent_profile"',
            "python3 -c 'import cluster_agent_profile'",
            'python3 -c "from cluster_agent_profile import list_profiles"',
            "python3 -c 'from cluster_agent_profile import list_profiles'",
            'python3 -uc "import cluster_agent_profile"',
            'python3 -W ignore -c "import cluster_agent_profile"',
            'python -c "import cluster_agent_profile"',
            'python3 -c import\\ cluster_agent_profile',
            'python3 -c "import sys; import cluster_agent_profile"',
            'python3 -c "import kanban_notify_propagate"',
            "python3 -c 'from kanban_notify_propagate import notify'",
            # Attached -c syntax
            "python3 -c'import cluster_agent_profile'",
            'python3 -c"import cluster_agent_profile"',
            "python3 -uc'import cluster_agent_profile'",
            'python3 -uc"import cluster_agent_profile"',
            "python3 -c'from cluster_agent_profile import list_profiles'",
            "python3 -c'import kanban_notify_propagate'",
            'python3 -c"import kanban_notify_propagate"',
            # ANSI-C quoting and bash string concatenation forms
            "python3 -c $'import cluster_agent_profile'",
            "python3 -c'import '\"cluster_agent_profile\"",
            "python3 -c 'import '\"cluster_agent_profile\"",
            "python3 -c$'import cluster_agent_profile'",
            "python3 -c $'from cluster_agent_profile import list_profiles'",
            "python3 -c $'import kanban_notify_propagate'",
            "python3 -c 'import '\"kanban_notify_propagate\"",
            # Prefixed interpreter invocations (env vars, wrappers, interpreter paths)
            "PYTHONPATH=/opt/data/scripts python3 -m cluster_agent_profile list",
            "PYTHONPATH=/opt/data/scripts python3 -m kanban_notify_propagate",
            "/opt/hermes/.venv/bin/python3 -m cluster_agent_profile",
            "/usr/bin/env python3 -m cluster_agent_profile",
            "env python3 -c 'import cluster_agent_profile'",
            "env python3 -c 'import kanban_notify_propagate'",
            "exec python3 -m cluster_agent_profile",
            "time python3 -m cluster_agent_profile",
            "nohup python3 -m cluster_agent_profile",
            "timeout 60 python3 -m cluster_agent_profile",
            "timeout 60 python3 -c 'import cluster_agent_profile'",
            "PYTHONPATH=/opt/data/scripts /opt/hermes/.venv/bin/python3 -m cluster_agent_profile",
            "env FOO=bar timeout 10 python3 -m cluster_agent_profile",
            # Escaped compound-command and stdin contexts
            "for c in a b; do python3 -m cluster_agent_profile list; done",
            "if ...; then python3 -c 'import cluster_agent_profile'; fi",
            "{ python3 -m cluster_agent_profile; }",
            "x=`python3 -m cluster_agent_profile`",
            "x=$(python3 -m cluster_agent_profile)",
            'bash -c "python3 -m cluster_agent_profile list"',
            "xargs python3 -m cluster_agent_profile",
            "sudo python3 -m cluster_agent_profile",
            "watch python3 -m cluster_agent_profile",
            "find . -exec python3 -m cluster_agent_profile {} +",
            "echo 'import cluster_agent_profile' | python3",
            'echo "import cluster_agent_profile" | python',
            "echo 'from cluster_agent_profile import list_profiles' | python3",
            "printf 'import cluster_agent_profile\n' | python3",
            "echo 'import kanban_notify_propagate' | python3",
            # Multi-line commands with newline separators, stdin forms, and alternative wrappers/interpreters
            "cd /opt/data/scripts\npython3 -m cluster_agent_profile list",
            "python3 - <<'EOF'\nimport cluster_agent_profile\nEOF",
            "python3 <<EOF\nimport cluster_agent_profile\nEOF",
            "python3 <<< 'import cluster_agent_profile'",
            "cat <<'EOF' | python3\nimport cluster_agent_profile\nEOF",
            "nice python3 -m cluster_agent_profile",
            "'/opt/hermes/.venv/bin/python3' -m cluster_agent_profile",
            '"/opt/hermes/.venv/bin/python3" -m cluster_agent_profile',
            "$PY -m cluster_agent_profile",
            "${PY} -m cluster_agent_profile",
            "nice python3 -c 'import cluster_agent_profile'",
            "'/opt/hermes/.venv/bin/python3' -c 'import cluster_agent_profile'",
            "$PY -c 'import cluster_agent_profile'",
        ]
        for cmd in matching_commands:
            with self.subTest(cmd=cmd):
                self.assertTrue(
                    any(re.search(pat, cmd) for pat in forbidden),
                    f"expected matching command {cmd!r} to be caught",
                )

        non_matching_commands = [
            "python3 /opt/data/scripts/gitops_workspace.py",
            "cat README.md",
            "git status",
            "echo 'kube-agents repo'",
            "grep -rn cluster_agent_profile /opt/data/skills/",
            "ls /opt/data/scripts | grep cluster_agent_profile",
            'echo "not using cluster_agent_profile"',
            "cat notes/cluster_agent_profile.md",
            "grep -n kubectl /opt/data/skills/cluster-agent-lifecycle/SKILL.md",
            "which kubectl",
            "printf 'delegated; no kubectl run here' > notes.md",
            "cat notes/kubectl.md",
            "curl http://localhost:8080",
            "hermes profile list",
            # Shell separators and pipes (do not read across command separators)
            'python3 -c "print(1)" && grep cluster_agent_profile /opt/data',
            "python3 -c 'print(1)' ; echo cluster_agent_profile",
            'python3 -c "print(1)" | grep cluster_agent_profile',
            "python3 --version; grep -c cluster_agent_profile /opt/data/skills/cluster-agent-lifecycle/SKILL.md",
            "python3 -V && grep -c cluster_agent_profile README.md",
            "python3 --help | grep -c cluster_agent_profile",
            "test -f /tmp/cluster_agent_profile.log || python3 collect.py",
            "grep -ic cluster_agent_profile README.md",
            "grep -c cluster_agent_profile /opt/data/skills/cluster-agent-lifecycle/SKILL.md",
            "sort -c cluster_agent_profile.txt",
            "ls -m cluster_agent_profile",
            # Harmless mentions in redirect targets or CLI arguments after closed -c code string
            "python3 -c 'print(1)' > /tmp/cluster_agent_profile.log",
            'python3 -c "print(1)" > /tmp/cluster_agent_profile.log',
            "python3 -c'print(1)' > /tmp/cluster_agent_profile.log",
            'python3 -c"print(1)" > /tmp/cluster_agent_profile.log',
            'python3 -c "print(sys.argv)" cluster_agent_profile',
            "python3 -c 'print(sys.argv)' cluster_agent_profile",
            'python3 -c "print(1)" < cluster_agent_profile.txt',
            "python3 -c 'print(1)' < cluster_agent_profile.txt",
            "python3 -c 'print(1)' > /tmp/kanban_notify_propagate.log",
            'python3 -c "print(sys.argv)" kanban_notify_propagate',
            # Legitimate python heredocs or redirects
            "python3 - <<'EOF'\nimport json, sys\nprint(1)\nEOF",
            "python3 <<EOF\nprint(1)\nEOF",
            "python3 /opt/data/scripts/anything.py <<EOF\nfoo\nEOF",
            "python3 -m json.tool <<EOF\n{}\nEOF",
            'python3 <<< "$json"',
            'echo "see python3 later" <<EOF\nEOF',
            "ls /usr/lib/python3 && cat <<'EOF' > notes.md\nEOF",
            # Non-python heredocs or redirects
            "cat <<'EOF' > /tmp/test.txt",
            "cat <<EOF > /tmp/test.txt",
            "cat <<< 'test string'",
            # The mirror defect from review
            'echo "(python3 -m cluster_agent_profile)"',
            "echo '(python3 -m cluster_agent_profile)'",
            'echo "(python3 -c \'import cluster_agent_profile\')"',
            # Quoted mentions following subshells or conditional keywords
            'echo "$(hostname): do not run python3 -m cluster_agent_profile"',
            'cd $(dirname x) && echo "python3 -m cluster_agent_profile is stubbed"',
            'echo "then python3 -m cluster_agent_profile"',
            'echo "do not run python3 -m cluster_agent_profile"',
            'echo "else python3 -m cluster_agent_profile"',
            'echo "elif python3 -m cluster_agent_profile"',
        ]
        for cmd in non_matching_commands:
            with self.subTest(cmd=cmd):
                self.assertFalse(
                    any(re.search(pat, cmd) for pat in forbidden),
                    f"expected non-matching command {cmd!r} not to be caught",
                )

    def test_platform_checked_workload_existence_requires_mcp_tool(self):
        spec = self.data.get("verification_spec", [])
        check = next(
            (c for c in spec if c.get("name") == "platform-checked-workload-existence"),
            None,
        )
        self.assertIsNotNone(check, "missing platform-checked-workload-existence check")
        assert check is not None
        tool_names = check.get("check", {}).get("tool_names", [])
        self.assertIn("mcp__gke__get_k8s_resource", tool_names)
        self.assertIn("mcp_gke_get_k8s_resource", tool_names)
        self.assertIn("mcp__gke__describe_k8s_resource", tool_names)
        self.assertIn("mcp_gke_describe_k8s_resource", tool_names)
        self.assertEqual(check.get("check", {}).get("scope"), "workers")
        self.assertEqual(check.get("check", {}).get("agent"), "platform")
        self.assertTrue(check.get("check", {}).get("require_success"), "require_success must be true")

    def test_expected_output_requires_delegation(self):
        expected_output = self.data.get("expected_output", "")
        self.assertIn(
            "delegating the investigation to the Cluster Agent",
            expected_output,
            "expected_output must require delegating the investigation to the Cluster Agent",
        )

    def test_verification_spec_checks_and_delegation_requirements(self):
        spec = self.data.get("verification_spec", [])
        check_names = [c.get("name") for c in spec]
        self.assertIn("a-cluster-agent-did-the-work", check_names)
        self.assertIn("platform-enumerated-cluster-profiles", check_names)
        self.assertIn("platform-checked-workload-existence", check_names)
        self.assertIn("no-stubbed-profile-scripts", check_names)
        self.assertIn("rca-names-the-oom", check_names)
        self.assertIn("the-crashloop-was-diagnosed-not-fixed", check_names)
        # Ineffective in-pod platform_control safeguard was removed;
        # existence inspection across the fleet is verified by platform-checked-workload-existence,
        # worker delegation is verified by a-cluster-agent-did-the-work (worker_agents),
        # and expected_output prompts the LLM judge for RCA and delegation phrasing.
        self.assertNotIn("no-inline-platform-mcp-diagnostics", check_names)


if __name__ == "__main__":
    unittest.main()
