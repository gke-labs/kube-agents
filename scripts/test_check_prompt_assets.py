#!/usr/bin/env python3
"""Tests for check_prompt_assets.py.

Two kinds of test here, and the split is deliberate.

The synthetic ones build a miniature profile in a temporary directory and point
the checker at it. They are the ones that prove each rule *fires* -- a lint
nobody has watched fail is a lint nobody knows is wired up, and this one has to
be trusted enough to gate on.

The rest run against the real repository. They are not "does the repository
pass" assertions dressed up as tests; they pin the two things that would
silently turn this check into a no-op: the `/opt/defaults` layout model going
stale against the Dockerfile, and the reference-shaped strings that must keep
being ignored. A lint that stops reporting anything looks exactly like a clean
repository.
"""

from __future__ import annotations

import contextlib
import io
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import check_prompt_assets as cpa

REPO = Path(__file__).resolve().parents[1]


def _copy_instructions(dockerfile: Path) -> list[list[str]]:
    """Every build-context COPY in a Dockerfile, as its argument list.

    Continuations and the multi-source form both matter here: the lines this
    model depends on are written as `COPY a.md \\\n b.md \\\n /opt/defaults/docs/`,
    and a parser that reads physical lines sees none of them. That failure is
    silent in the worst way -- it makes the comparison below pass over an empty
    list until a `self.assertTrue(expected)` catches it.

    `COPY --from=<stage>` is skipped rather than having its flag dropped. Its
    source is another build stage, not this repository, so a path in it says
    nothing about a file in the checkout. Today the only two land in
    /usr/local/bin and OPT_DEFAULTS would ignore them anyway; the point is that
    one aimed at an asset directory would otherwise be read as a repo path and
    quietly agree with a model that is wrong.
    """
    text = re.sub(r"\\\n", " ", dockerfile.read_text(encoding="utf-8"))
    instructions = []
    for line in text.splitlines():
        if not line.startswith("COPY "):
            continue
        flags = [argument for argument in line.split()[1:] if argument.startswith("--")]
        if any(flag.startswith("--from=") for flag in flags):
            continue
        arguments = [
            argument
            for argument in line.split()[1:]
            if not argument.startswith("--")
        ]
        if len(arguments) >= 2:
            instructions.append(arguments)
    return instructions


class ProfileFixture:
    """A minimal agents/<profile>/ tree the checker can be pointed at."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "agents" / "platform"
        (self.home / "governance").mkdir(parents=True)
        (self.home / "scripts").mkdir()
        (self.home / "skills").mkdir()
        (self.home / "cron").mkdir()
        self.write("agents/platform/SOUL.md", "# persona\n")

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def skill(
        self,
        name: str,
        body: str = "",
        description: str = "does a thing",
        profile: str = "platform",
    ) -> Path:
        return self.write(
            f"agents/{profile}/skills/{name}/SKILL.md",
            f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n{body}\n",
        )

    def run(self) -> list[cpa.Finding]:
        with mock.patch.object(cpa, "REPO", self.root):
            files = cpa.instruction_files()
            skills = cpa.skill_directories()
            return (
                cpa.check_asset_paths(files)
                + cpa.check_skill_refs(files, skills)
                + cpa.check_skill_manifests(skills)
                + cpa.check_cron_assets()
            )

    def rules(self) -> list[str]:
        return sorted(f.rule for f in self.run())


class SyntheticProfileTests(unittest.TestCase):
    """Each rule, shown failing on a tree built to break it."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fixture = ProfileFixture(Path(self._tmp.name))

    def test_a_clean_profile_reports_nothing(self):
        self.fixture.skill("fleet-audit")
        self.fixture.write("agents/platform/governance/audit_sop.md", "# sop\n")
        self.fixture.write(
            "agents/platform/SOUL.md",
            "Read `governance/audit_sop.md` and use the `fleet-audit` skill.\n",
        )
        self.assertEqual([], self.fixture.run())

    def test_missing_governance_document_is_reported(self):
        self.fixture.write("agents/platform/SOUL.md", "Read `governance/gone.md` first.\n")
        findings = self.fixture.run()
        self.assertEqual(["asset-path"], [f.rule for f in findings])
        self.assertIn("governance/gone.md", findings[0].message)
        self.assertIn("SOUL.md:1", findings[0].where)

    def test_renamed_skill_script_is_reported(self):
        """The rename that motivates the whole check."""
        self.fixture.skill("fleet-audit", body="Run `scripts/audit_report.py`.")
        self.assertEqual(["asset-path"], self.fixture.rules())

        (self.fixture.home / "skills/fleet-audit/scripts").mkdir()
        self.fixture.write(
            "agents/platform/skills/fleet-audit/scripts/audit_report.py", "#\n"
        )
        self.assertEqual([], self.fixture.rules())

    def test_skill_named_in_prose_must_exist(self):
        self.fixture.skill("gke-compute-classes")
        self.fixture.write(
            "agents/platform/SOUL.md", "Defer to the `gke-compute-class` skill.\n"
        )
        findings = self.fixture.run()
        self.assertEqual(["skill-ref"], [f.rule for f in findings])
        self.assertIn("gke-compute-class", findings[0].message)

    def test_a_near_miss_names_the_skill_that_was_meant(self):
        self.fixture.skill("gke-compute-classes")
        self.fixture.write("agents/platform/SOUL.md", "the `gke-compute-class` skill\n")
        self.assertIn("did you mean 'gke-compute-classes'?", self.fixture.run()[0].message)

    def test_cron_job_declaring_an_unknown_skill_is_reported(self):
        self.fixture.write(
            "agents/platform/cron/jobs.json",
            '{"jobs": [{"id": "audit", "prompt": "go", "skills": ["no-such-skill"]}]}',
        )
        findings = self.fixture.run()
        self.assertEqual(["skill-ref"], [f.rule for f in findings])
        self.assertIn("no-such-skill", findings[0].message)

    def test_cron_prompt_pointing_at_a_missing_sop_is_reported(self):
        self.fixture.write(
            "agents/platform/cron/jobs.json",
            '{"jobs": [{"id": "audit", "prompt": '
            "\"Read the SOP at 'governance/moved_sop.md' in your profile home.\"}]}",
        )
        findings = self.fixture.run()
        self.assertEqual(["cron-asset"], [f.rule for f in findings])
        self.assertIn("governance/moved_sop.md", findings[0].message)

    def test_frontmatter_name_must_match_the_directory(self):
        """The agent loads by directory; prose cites the frontmatter name."""
        self.fixture.write(
            "agents/platform/skills/gke-upgrades/SKILL.md",
            "---\nname: gke-upgrade\ndescription: d\n---\n\n# skill\n",
        )
        findings = self.fixture.run()
        self.assertEqual(["skill-manifest"], [f.rule for f in findings])
        self.assertIn("gke-upgrade", findings[0].message)
        self.assertIn("gke-upgrades", findings[0].message)

    def test_skill_with_no_manifest_is_reported(self):
        (self.fixture.home / "skills" / "empty-skill").mkdir()
        self.assertEqual(["skill-manifest"], self.fixture.rules())

    def test_a_description_folded_onto_following_lines_is_accepted(self):
        """prettier reflows the long ones; that is not a missing description."""
        self.fixture.write(
            "agents/platform/skills/wordy/SKILL.md",
            "---\nname: wordy\ndescription:\n  A description long enough that the\n"
            "  formatter moved it onto its own lines.\n---\n\n# wordy\n",
        )
        self.assertEqual([], self.fixture.rules())

    def test_a_folded_description_with_no_body_is_reported(self):
        """`>-` is the form most bundles use, so an emptied one is the slip.

        `\\S.*` matched the block indicator itself, so a manifest whose folded
        body had been deleted satisfied the rule that exists to require a
        description -- and the agent loads a skill it will then never select.
        """
        for indicator in (">-", ">", "|", "|-"):
            with self.subTest(indicator=indicator):
                self.fixture.write(
                    "agents/platform/skills/hollow/SKILL.md",
                    f"---\nname: hollow\ndescription: {indicator}\n---\n\n# hollow\n",
                )
                self.assertEqual(["skill-manifest"], self.fixture.rules())

    def test_a_folded_description_with_a_body_is_accepted(self):
        self.fixture.write(
            "agents/platform/skills/folded/SKILL.md",
            "---\nname: folded\ndescription: >-\n  Text the formatter folded\n"
            "  onto its own lines.\n---\n\n# folded\n",
        )
        self.assertEqual([], self.fixture.rules())

    def test_a_cron_prompt_resolves_through_the_profile_home_model(self):
        """This rule used the whole of agents/<profile>/, unlike asset-path.

        So it accepted `docs/glossary.md` from the platform roster -- a path
        that reaches the default profile's home and no other, which is the
        exact class its own message ("not in its profile") claims to be about.
        """
        self.fixture.write("agents/platform/docs/glossary.md", "# glossary\n")
        self.fixture.write(
            "agents/platform/cron/jobs.json",
            '{"jobs": [{"id": "audit", "prompt": "Read \'docs/glossary.md\' first."}]}',
        )
        self.assertEqual(["cron-asset"], self.fixture.rules())

    def test_a_cron_prompt_path_of_any_depth_is_checked(self):
        """`dir/file.ext` only was the old shape, so deeper paths went unread.

        The `agents/...` spelling is the one the asset-path rule reports as
        always wrong at runtime: the SOP was guarded against it while the cron
        prompt sending a worker to that SOP was not.
        """
        self.fixture.write("agents/platform/governance/real_sop.md", "# sop\n")
        for ref in (
            "agents/platform/governance/real_sop.md",
            "skills/thing/scripts/missing.py",
        ):
            with self.subTest(ref=ref):
                self.fixture.write(
                    "agents/platform/cron/jobs.json",
                    '{"jobs": [{"id": "audit", "prompt": "Read \'%s\'."}]}' % ref,
                )
                self.assertEqual(["cron-asset"], self.fixture.rules())

    def test_a_deep_cron_path_the_profile_does_have_is_accepted(self):
        self.fixture.write("agents/platform/skills/thing/scripts/run.py", "x = 1\n")
        self.fixture.skill("thing")
        self.fixture.write(
            "agents/platform/cron/jobs.json",
            '{"jobs": [{"id": "audit", "prompt": '
            "\"Run 'skills/thing/scripts/run.py'.\"}]}",
        )
        self.assertEqual([], self.fixture.rules())

    def test_a_roster_that_moves_within_agents_is_still_found(self):
        """Two hardcoded paths behind is_file() made both cron rules no-ops.

        Move a roster and the run stayed byte-identical: same file count, same
        bundle count, exit 0, nothing checked. Discovery is a glob now, so the
        rules follow the roster.
        """
        self.fixture.write(
            "agents/cluster/defaults/cron/jobs.json",
            '{"jobs": [{"id": "sweep", "prompt": "Read \'governance/gone.md\'."}]}',
        )
        self.assertEqual(["cron-asset"], self.fixture.rules())

    def test_runtime_state_paths_are_not_asset_references(self):
        self.fixture.write(
            "agents/platform/SOUL.md",
            "State lives in `/opt/data/INVENTORY.md` and `/opt/data/SETTINGS.md`.\n",
        )
        self.assertEqual([], self.fixture.rules())

    def test_identifiers_that_merely_contain_a_slash_are_not_paths(self):
        """The reason this check has no hand-maintained allowlist.

        `roles/container.admin` is an IAM role and `manifests/vendor/x.yaml` is
        an illustration of a path in somebody else's repository. Neither has an
        asset extension in a directory this layout has, and neither may be
        reported -- a lint with a false positive per skill gets switched off.
        """
        self.fixture.write(
            "agents/platform/SOUL.md",
            "Grant `roles/container.admin`, never `roles/container.clusterAdmin`.\n"
            "A remediation path such as `manifests/vendor/x.yaml` stays in the clone.\n"
            "Annotate with `kubernetes.io/ingress.global-static-ip-name`.\n",
        )
        self.assertEqual([], self.fixture.rules())

    def test_placeholders_are_not_resolved(self):
        self.fixture.write(
            "agents/platform/SOUL.md",
            "Write `memory/YYYY-MM-DD.md` under `$HERMES_HOME/notes.md`,\n"
            "and see `<cluster>/kustomization.yaml`.\n",
        )
        self.assertEqual([], self.fixture.rules())

    def test_a_skill_asset_is_found_from_a_reference_page(self):
        """`./assets/x` in references/ means the bundle's assets, not its own."""
        self.fixture.skill("gke-cluster-autoscaler")
        skill = self.fixture.home / "skills/gke-cluster-autoscaler"
        (skill / "assets").mkdir()
        (skill / "assets/find.sh").write_text("#\n", encoding="utf-8")
        self.fixture.write(
            "agents/platform/skills/gke-cluster-autoscaler/references/debug.md",
            "- **Asset:** `./assets/find.sh`\n",
        )
        self.assertEqual([], self.fixture.rules())

    def test_developer_readmes_are_out_of_scope(self):
        """They describe Hermes internals and never reach a profile."""
        self.fixture.skill("fleet-audit")
        self.fixture.write(
            "agents/platform/skills/fleet-audit/README.md",
            "The upstream router lives in `gateway/run.py`.\n",
        )
        self.assertEqual([], self.fixture.rules())


class ResolutionModelTests(unittest.TestCase):
    """The model of where a file ends up, checked against what builds it."""

    def test_a_copy_from_another_stage_is_not_read_as_a_repository_path(self):
        """The source of a `COPY --from=` is a build stage, not this checkout.

        Dropping the flag instead of the instruction would make the drift guard
        below expect an OPT_DEFAULTS entry for a path that does not exist in the
        repository -- a model that is wrong agreeing with a check that cannot
        tell. Both forms are exercised so that neither the flag-before-source
        nor the extra-flag spelling slips through.
        """
        with TemporaryDirectory() as directory:
            dockerfile = Path(directory) / "Dockerfile"
            dockerfile.write_text(
                "COPY --from=builder /workspace/out.md /opt/defaults/docs/out.md\n"
                "COPY --from=builder --chmod=0755 /bin/x /usr/local/bin/x\n"
                "COPY agents/platform/docs/glossary.md /opt/defaults/docs/\n",
                encoding="utf-8",
            )
            parsed = _copy_instructions(dockerfile)
        self.assertEqual(
            parsed, [["agents/platform/docs/glossary.md", "/opt/defaults/docs/"]]
        )

    def test_opt_defaults_matches_the_dockerfile(self):
        """OPT_DEFAULTS is a hand-written copy of the image layout.

        The entrypoint copies /opt/defaults over every profile home, so this
        table decides whether a profile-relative reference is resolvable at all.
        Let it drift and the check does not fail loudly -- it starts reporting
        correct references as broken, or worse, stops resolving through a layer
        and quietly widens what it accepts. Re-derive it from the COPY lines so
        that adding one to the Dockerfile fails here, at the one place that has
        to know.
        """
        expected: list[tuple[str, str]] = []
        for arguments in _copy_instructions(REPO / "deploy/docker/Dockerfile"):
            *sources, destination = arguments
            if not destination.startswith("/opt/defaults/"):
                continue
            rest = destination[len("/opt/defaults/") :]
            for source in sources:
                if destination.endswith("/") and not source.endswith("/"):
                    # COPY <file>... <dir>/ keeps each file's own name.
                    entry = rest + Path(source).name
                else:
                    entry = rest
                expected.append((entry.rstrip("/"), source.rstrip("/")))

        self.assertTrue(expected, "no COPY into /opt/defaults found; parser is stale")
        self.assertEqual(
            expected,
            list(cpa.OPT_DEFAULTS),
            "check_prompt_assets.OPT_DEFAULTS has drifted from the Dockerfile",
        )

    def test_the_defaults_layer_reaches_the_default_profile_and_no_other(self):
        """The `cp -ru` is to $TARGET_DIR, which is one home, not every home.

        An earlier revision had this backwards -- the table's comment claimed
        the copy ran "for *every* profile" and a test here pinned that as
        correct, so the next reader to notice would have been told by a green
        suite that they were mistaken. The entrypoint's own line 538 is the
        tell: it symlinks $TARGET_DIR/scripts into profiles/platform, which it
        would not need if the layer arrived there on its own.
        """
        entrypoint = (REPO / "deploy/shared/docker-entrypoint.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('TARGET_DIR="${PLATFORM_AGENT_HOME:-/opt/data}"', entrypoint)
        self.assertIn('cp -ru /opt/defaults/. "$TARGET_DIR/"', entrypoint)
        # Specialist homes are $TARGET_DIR/profiles/<name>, which that copy
        # does not reach -- so `docs`, which lives only in the layer, is in
        # neither specialist item list.
        self.assertIsNotNone(cpa._resolve_opt("docs/gcp-console-links.md"))
        for profile, items in cpa.PROFILE_HOME_ITEMS.items():
            self.assertNotIn("docs", items, f"{profile} does not receive docs/")
        self.assertNotIn("chat", cpa.PROFILE_HOME_ITEMS, "chat IS the default profile")

    def test_profile_home_items_match_the_entrypoint(self):
        """PROFILE_HOME_ITEMS decides what a specialist home holds.

        Widen either list by hand and the check starts accepting references to
        files that are not there; let the entrypoint widen without this and it
        starts rejecting ones that are. Re-derive both from the code that does
        the populating.
        """
        entrypoint = (REPO / "deploy/shared/docker-entrypoint.sh").read_text(
            encoding="utf-8"
        )
        # The platform list is no longer written inline at its --items: the
        # front-door flag drops config.yaml from the force-sync (that file
        # becomes one the running agent writes to), so the entrypoint passes
        # "$(platform_sync_items)" and the function assembles the list from a
        # base plus a conditional prefix. Re-derive it from those two lines --
        # both halves, so a name moving between them is not read as a removal.
        base = re.search(r'_items="([^"$]+)"', entrypoint)
        prefix = re.search(r'_items="([^"$]*)\$_items"', entrypoint)
        self.assertIsNotNone(base, "platform_sync_items no longer sets a base list")
        self.assertIsNotNone(prefix, "platform_sync_items no longer prepends to it")
        platform = prefix.group(1).split() + base.group(1).split()
        self.assertIn("governance", platform, "picked up the wrong --items list")
        self.assertEqual(
            set(platform) | {"scripts"},
            set(cpa.PROFILE_HOME_ITEMS["platform"]),
            "platform home model has drifted from docker-entrypoint.sh --items",
        )
        # The `scripts` symlink is the one addition, and it is conditional on
        # the profile marker -- assert the line that makes it, not just its name.
        self.assertIn(
            'ln -sfn "$TARGET_DIR/scripts" "$TARGET_DIR/profiles/platform/scripts"',
            entrypoint,
        )

        scaffold = (REPO / "agents/platform/scripts/cluster_agent_profile.py").read_text(
            encoding="utf-8"
        )
        declared = re.search(r"OVERLAY_ITEMS = \(([^)]*)\)", scaffold)
        self.assertIsNotNone(declared, "OVERLAY_ITEMS moved or changed shape")
        self.assertEqual(
            set(re.findall(r'"([^"]+)"', declared.group(1))),
            set(cpa.PROFILE_HOME_ITEMS["cluster"]),
            "cluster home model has drifted from cluster_agent_profile.py",
        )


class CheckoutPathTests(unittest.TestCase):
    """A path that addresses the checkout is the one spelling always wrong.

    `agents/platform/skills/x/y.md` names a real file, so it survives every
    review a human gives it, and names no file the agent has: that tree is
    COPYed to /opt/platform-template/skills, so the profile home holds
    `skills/`, never `agents/`. Accepting it because it exists in the checkout
    is worse than having no rule -- it is the rule agreeing with the mistake.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fixture = ProfileFixture(Path(self._tmp.name))

    def _findings(self) -> list[cpa.Finding]:
        return [f for f in self.fixture.run() if f.rule == "asset-path"]

    def test_a_checkout_path_to_a_file_the_profile_does_have_is_reported(self):
        self.fixture.write("agents/platform/skills/thing/references/how.md", "# how\n")
        self.fixture.write(
            "agents/platform/governance/sop.md",
            "Read `agents/platform/skills/thing/references/how.md` first.\n",
        )
        self.assertEqual(len(self._findings()), 1)

    def test_the_in_profile_spelling_of_the_same_file_passes(self):
        self.fixture.write("agents/platform/skills/thing/references/how.md", "# how\n")
        self.fixture.write(
            "agents/platform/governance/sop.md",
            "Read `skills/thing/references/how.md` first.\n",
        )
        self.assertEqual([], self._findings())

    def test_the_finding_names_the_spelling_that_would_work(self):
        """The author checked that the file exists; saying only "does not
        resolve" reads as the checker being wrong. Hand them the rewrite."""
        self.fixture.write("agents/platform/skills/thing/references/how.md", "# how\n")
        self.fixture.write(
            "agents/platform/governance/sop.md",
            "Read `agents/platform/skills/thing/references/how.md` first.\n",
        )
        self.assertIn(
            "write 'skills/thing/references/how.md'", self._findings()[0].message
        )

    def test_another_profiles_file_is_not_offered_as_a_rewrite(self):
        """Stripping `agents/chat/` off a path in a platform document leaves
        `SOUL.md`, which resolves -- to the platform persona, a different
        document. A hint that turns a broken reference into a wrong one is
        worse than no hint."""
        self.fixture.write("agents/chat/SOUL.md", "# chat persona\n")
        self.fixture.write(
            "agents/platform/governance/sop.md",
            "The router's contract is in `agents/chat/SOUL.md`.\n",
        )
        message = self._findings()[0].message
        self.assertNotIn("write ", message)
        self.assertIn("chat profile", message)

    def test_a_defaults_layer_path_is_reported_for_a_specialist_profile(self):
        """The layer lands in the default profile's home, not in every home.

        This is the reference that looks most obviously fine and is not: the
        file exists, the path is the shape the default profile uses, and the
        specialist simply never receives that directory.
        """
        self.fixture.write(
            "agents/platform/governance/sop.md",
            "Console URL templates are in `docs/gcp-console-links.md`.\n",
        )
        with mock.patch.object(
            cpa,
            "OPT_DEFAULTS",
            (("docs/gcp-console-links.md", "agents/platform/docs/links.md"),),
        ):
            self.fixture.write("agents/platform/docs/links.md", "# links\n")
            findings = self._findings()
        self.assertEqual(len(findings), 1)
        self.assertIn("write '/opt/defaults/docs/gcp-console-links.md'", findings[0].message)

    def test_repository_tooling_that_ships_in_no_profile_is_reported(self):
        self.fixture.write("deploy/docker/patches/thing.py", "x = 1\n")
        self.fixture.write(
            "agents/platform/governance/sop.md",
            "The image applies `deploy/docker/patches/thing.py`.\n",
        )
        self.assertIn("nothing copies it into this profile", self._findings()[0].message)


class RepositoryTests(unittest.TestCase):
    def test_the_repository_passes(self):
        """Not a coverage claim -- a claim that the gate is holdable.

        If this fails, either somebody broke a reference or the checker has
        started reporting one that is fine. Both are worth stopping for.
        """
        with mock.patch.object(cpa, "REPO", REPO):
            files = cpa.instruction_files()
            skills = cpa.skill_directories()
            findings = (
                cpa.check_asset_paths(files)
                + cpa.check_skill_refs(files, skills)
                + cpa.check_skill_manifests(skills)
                + cpa.check_cron_assets()
            )
        self.assertEqual([], [str(f) for f in findings])

    def test_every_skill_bundle_in_the_tree_is_discovered(self):
        """Compared against the tree, not against a number I chose.

        This replaces a `> 20` floor, which is the kind of assertion that looks
        like coverage and is not: skill discovery was keyed on the bare
        directory name, six platform bundles were being shadowed by same-named
        cluster ones and dropped from the manifest rule entirely, and a floor of
        20 sailed through it. A count I pick cannot detect a gap I did not
        anticipate. `git ls-files` can.
        """
        with mock.patch.object(cpa, "REPO", REPO):
            discovered = cpa.skill_directories()
        found = {
            (bundle.parent.parent.name, name)
            for profile in discovered
            for name, bundle in discovered[profile].items()
        }
        on_disk = {
            (manifest.parts[-4], manifest.parts[-2])
            for manifest in REPO.glob("agents/*/skills/*/SKILL.md")
        }
        self.assertEqual(on_disk - found, set(), "bundles the checker never opens")
        self.assertEqual(found - on_disk, set(), "bundles the checker invented")

    def test_the_same_name_in_two_profiles_is_two_bundles(self):
        """The specific collision that hid six manifests.

        Pinned against the tree rather than by name, so it keeps its meaning
        if these six stop colliding and some other pair starts.
        """
        with mock.patch.object(cpa, "REPO", REPO):
            discovered = cpa.skill_directories()
        shared = set(discovered.get("platform", {})) & set(discovered.get("cluster", {}))
        self.assertTrue(shared, "no cross-profile name collisions left to guard")
        for name in shared:
            self.assertNotEqual(
                discovered["platform"][name],
                discovered["cluster"][name],
                f"{name!r} resolves to one directory for both profiles",
            )

    def test_every_cron_roster_in_the_tree_is_discovered(self):
        """The cron rules' scope, diffed against the tree like the bundles.

        Both cron rules run only over what cron_rosters() returns, and a rule
        that runs over nothing passes. The bundle count had a floor and it
        still missed six; this has no floor at all, so compare with what is
        on disk in both directions.
        """
        with mock.patch.object(cpa, "REPO", REPO):
            discovered = {path for path, _ in cpa.cron_rosters()}
            jobs = sum(len(j) for _, j in cpa.cron_rosters())
        on_disk = set(REPO.glob("agents/**/cron/jobs.json"))
        self.assertTrue(on_disk, "no cron roster in the tree; the glob is stale")
        self.assertEqual(on_disk - discovered, set(), "rosters the checker never reads")
        self.assertEqual(discovered - on_disk, set(), "rosters the checker invented")
        self.assertGreater(jobs, 0, "rosters found but every one of them is empty")

    def test_it_is_actually_looking_at_something(self):
        """A checker whose scope silently empties reports a clean repository.

        Every rule above passes vacuously over an empty file list. This floor is
        well under the current count and exists only to catch a glob that
        stopped matching.
        """
        with mock.patch.object(cpa, "REPO", REPO):
            files = cpa.instruction_files()
        self.assertGreater(len(files), 50, "instruction file discovery has narrowed")
        self.assertIn(REPO / "agents/platform/SOUL.md", files)
        self.assertIn(REPO / "agents/platform/governance/compliance_audit_sop.md", files)

    def test_it_reads_the_baked_docs_and_leaves_the_design_docs_alone(self):
        """`agents/<profile>/docs/` holds two kinds of file, not one.

        The Dockerfile bakes named runtime references out of that directory
        into /opt/defaults/docs; the design docs beside them ship nowhere and
        are read by a human in a checkout, where `agents/platform/...` is the
        spelling that resolves. Globbing the directory swept both in and asked
        the design docs to cite paths against a profile root they never reach
        -- rewriting working citations into ones that resolve nowhere, which
        is the opposite of this checker's purpose.
        """
        with mock.patch.object(cpa, "REPO", REPO):
            files = cpa.instruction_files()
        for baked in ("glossary.md", "gcp-console-links.md"):
            self.assertIn(
                REPO / "agents/platform/docs" / baked,
                files,
                f"{baked} is baked into the image; the agent does read it",
            )
        for design in ("autoops-architecture.md", "session_management.md"):
            self.assertNotIn(
                REPO / "agents/platform/docs" / design,
                files,
                f"{design} reaches no profile; checking it demands a rewrite "
                "that breaks the citation for its only reader",
            )


class ProfileIsolationTests(unittest.TestCase):
    """Skills belong to one profile; a merged namespace hides two failures."""

    def test_a_same_named_bundle_in_another_profile_does_not_shadow_this_one(self):
        """The bug: a flat dict kept one of the two and dropped the other.

        Both bundles are broken here in different ways, so a run that reports
        one finding is a run that opened only one of them.
        """
        with TemporaryDirectory() as directory:
            fixture = ProfileFixture(Path(directory))
            fixture.skill("gke-storage", profile="platform")
            fixture.write(
                "agents/platform/skills/gke-storage/SKILL.md",
                "---\nname: gke-storage-fleet\ndescription: d\n---\n\n# x\n",
            )
            fixture.write(
                "agents/cluster/skills/gke-storage/SKILL.md",
                "---\nname: gke-storage-node\ndescription: d\n---\n\n# x\n",
            )
            findings = fixture.run()
        reported = sorted(f.where for f in findings if f.rule == "skill-manifest")
        self.assertEqual(
            reported,
            [
                "agents/cluster/skills/gke-storage/SKILL.md",
                "agents/platform/skills/gke-storage/SKILL.md",
            ],
        )

    def test_citing_a_skill_from_another_profile_is_reported(self):
        """agents/platform/skills/* is never delivered to a Cluster Agent."""
        with TemporaryDirectory() as directory:
            fixture = ProfileFixture(Path(directory))
            fixture.skill("fleet-audit", profile="platform")
            fixture.write(
                "agents/cluster/SOUL.md",
                "# Soul\n\nDefer to the `fleet-audit` skill when unsure.\n",
            )
            findings = [f for f in fixture.run() if f.rule == "skill-ref"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].where, "agents/cluster/SOUL.md:3")
        self.assertIn("the cluster profile does not have", findings[0].message)

    def test_the_wrong_profile_is_named_rather_than_called_nonexistent(self):
        """"Does not exist" about a bundle sitting in the tree reads as a bug.

        The author looks, finds it, and stops believing the checker. Say which
        profile has it instead.
        """
        with TemporaryDirectory() as directory:
            fixture = ProfileFixture(Path(directory))
            fixture.skill("fleet-audit", profile="platform")
            fixture.write(
                "agents/cluster/SOUL.md", "# S\n\nUse the `fleet-audit` skill.\n"
            )
            message = [f for f in fixture.run() if f.rule == "skill-ref"][0].message
        self.assertIn("it exists in platform", message)
        self.assertIn("which this profile does not receive", message)
        self.assertNotIn("did you mean", message)

    def test_a_typo_still_gets_the_nearest_name_in_its_own_profile(self):
        """The wrong-profile hint must not swallow the typo hint."""
        with TemporaryDirectory() as directory:
            fixture = ProfileFixture(Path(directory))
            fixture.skill("gke-compute-classes", profile="platform")
            fixture.write(
                "agents/platform/SOUL.md",
                "# S\n\nDefer to the `gke-compute-class` skill.\n",
            )
            message = [f for f in fixture.run() if f.rule == "skill-ref"][0].message
        self.assertIn("did you mean 'gke-compute-classes'?", message)


class AnnotationTests(unittest.TestCase):
    """Findings have to reach the author, not just the exit code."""

    def test_a_finding_with_a_line_anchors_to_that_line(self):
        finding = cpa.Finding(
            "skill-ref",
            "agents/platform/skills/gke-cluster-autoscaler/SKILL.md:19",
            "names the 'gke-compute-class' skill, which does not exist",
        )
        self.assertEqual(
            finding.annotation(),
            "::error file=agents/platform/skills/gke-cluster-autoscaler/SKILL.md"
            ",line=19,title=prompt skill-ref::names the 'gke-compute-class' "
            "skill, which does not exist",
        )

    def test_a_file_level_finding_omits_the_line(self):
        """cron-asset and skill-manifest name a file, not a line."""
        finding = cpa.Finding("cron-asset", "agents/platform/cron/jobs.json", "gone")
        self.assertNotIn("line=", finding.annotation())
        self.assertIn("file=agents/platform/cron/jobs.json,", finding.annotation())

    def test_a_windows_style_path_is_not_read_as_a_line_number(self):
        """`partition(':')` on `C:/x.md` must not yield line=`/x.md`.

        Nothing produces such a path today -- the guard is `line.isdigit()`, and
        this pins it so a future `where` format cannot emit a malformed command
        that GitHub drops without telling anyone.
        """
        self.assertNotIn("line=", cpa.Finding("r", "C:/x.md", "m").annotation())

    def test_percent_and_newlines_are_escaped(self):
        """An unescaped newline would silently start a second annotation."""
        annotation = cpa.Finding("r", "a.md:1", "100% of\nit").annotation()
        self.assertTrue(annotation.endswith("::100%25 of%0Ait"))
        self.assertEqual(annotation.count("\n"), 0)

    def _main_stdout(self, argv: list[str], environment: dict[str, str]) -> str:
        """main() over a one-broken-reference profile; its stdout."""
        with TemporaryDirectory() as directory:
            fixture = ProfileFixture(Path(directory))
            fixture.skill("real-skill")
            fixture.write(
                "agents/platform/SOUL.md",
                "# Soul\n\nDefer to the `made-up-skill` skill for that.\n",
            )
            buffer = io.StringIO()
            with mock.patch.object(cpa, "REPO", fixture.root), mock.patch.dict(
                cpa.os.environ, environment, clear=True
            ), contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(
                io.StringIO()
            ):
                self.assertEqual(cpa.main(argv), 1)
        return buffer.getvalue()

    def test_a_broken_reference_is_annotated_under_actions(self):
        """End to end, and through the env rather than the flag.

        The CI job is a plain `make prompt-check` with no flag to keep in step
        with the workflow, so GITHUB_ACTIONS is the switch that has to work.
        """
        annotations = [
            line
            for line in self._main_stdout([], {"GITHUB_ACTIONS": "true"}).splitlines()
            if line.startswith("::")
        ]
        self.assertEqual(len(annotations), 1)
        self.assertIn("file=agents/platform/SOUL.md,line=3", annotations[0])
        self.assertIn("title=prompt skill-ref", annotations[0])
        self.assertIn("'made-up-skill'", annotations[0])

    def test_a_local_run_is_not_littered_with_workflow_commands(self):
        self.assertNotIn("::error", self._main_stdout([], {}))

    def test_the_flags_override_the_environment_both_ways(self):
        self.assertIn("::error", self._main_stdout(["--annotate"], {}))
        self.assertNotIn(
            "::error",
            self._main_stdout(["--no-annotate"], {"GITHUB_ACTIONS": "true"}),
        )


if __name__ == "__main__":
    unittest.main()
