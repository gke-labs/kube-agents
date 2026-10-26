#!/usr/bin/env python3
"""Unit tests for the documentation contracts check.

Run: cd scripts && python3 -m unittest test_check_docs_contracts

Each test builds a throwaway repository with the layout the check reads --
three installer scripts, a Go API package, three CRD pages, a cron roster,
skill directories, an image inventory, the release image list, an allowlist --
and asserts the verdict. The directions pinned: an unknown flag fails and a
known one passes, with another command's flags not attributed to the installer;
a field reached through an inline embed is required and an absent one fails
unless allowlisted; a stale count fails in digit, word and compound-word form
while "four of the" and a dotted version are not counts; an allowlist entry
with no finding fails; and every source file that goes missing fails the run
instead of thinning it.
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import check_docs_contracts as contracts

INSTALL_SH = """#!/usr/bin/env bash
show_help() {
  cat <<EOF
  --dry-run                     Validate prerequisites without creating resources
  --enable-gvisor[=true|false]  Enable GKE Sandbox (gVisor) runtime isolation
EOF
}
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -y|--yes|--non-interactive) NON_INTERACTIVE="true"; shift ;;
      --dry-run) DRY_RUN="true"; shift ;;
      --gcp-project-id=*) PROJECT_ID="${1#*=}"; shift ;;
      --enable-gvisor|--enable-gvisor=*) ENABLE_GVISOR="true"; shift ;;
      --help|-h) show_help; exit 0 ;;
      *) echo "Unknown parameter: $1"; exit 1 ;;
    esac
  done
}
"""

UPGRADE_SH = """#!/usr/bin/env bash
case "$1" in
  --upgrade-mode=*|-m=*) UPGRADE_MODE="${1#*=}"; shift ;;
  --upgrade-mode|-m) UPGRADE_MODE="$2"; shift 2 ;;
  --plan) PLAN="true"; shift ;;
  --dry-run) DRY_RUN="true"; shift ;;
esac
"""

UNINSTALL_SH = """#!/usr/bin/env bash
case "$1" in
  --dry-run) DRY_RUN="true"; shift ;;
  --source-ref=*) SOURCE_REF="${1#*=}"; shift ;;
  --help|-h|-\\?|help) show_help ;;
esac
"""

TYPES_GO = """package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// PlatformAgentSpec defines the desired state of PlatformAgent
type PlatformAgentSpec struct {
	AgentSpec `json:",inline"`

	// mode selects the stack; the comment mentions `json:"notAField"` to be ignored
	// +optional
	Mode string `json:"mode,omitempty"`
}

type AgentSpec struct {
	Storage *StorageSpec `json:"storage,omitempty"`
	Labels map[string]string `json:"labels,omitempty"`
	Rules []EgressRule `json:"rules,omitempty"`
	SecretRef corev1.LocalObjectReference `json:"secretRef,omitempty"`
	Hidden string `json:"-"`
	Level Level `json:"level,omitempty"`
}

type Level string

type StorageSpec struct {
	Size string `json:"storageSize,omitempty"` // trailing comment
}

type EgressRule struct {
	Port int `json:"port"`
}

type AgentStatus struct {
	Phase string `json:"phase,omitempty"`
}

type PlatformAgent struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec PlatformAgentSpec `json:"spec"`
	Status AgentStatus `json:"status,omitempty"`
}

type AgentProfileSpec struct {
	Persona string `json:"persona"`
}

type AgentProfileStatus struct {
	Ready bool `json:"ready"`
}

type AgentProfile struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec   AgentProfileSpec   `json:"spec"`
	Status AgentProfileStatus `json:"status,omitempty"`
}

type AgentPluginSpec struct {
	Image string `json:"image"`
}

type AgentPluginStatus struct {
	Applied bool `json:"applied"`
}

type AgentPlugin struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec   AgentPluginSpec   `json:"spec,omitempty"`
	Status AgentPluginStatus `json:"status,omitempty"`
}
"""

# A test file and a generated file each declare a struct that must not be read.
TYPES_TEST_GO = """package v1alpha1

type AgentPluginSpec struct {
	Bogus string `json:"bogusFromTest"`
}
"""
GENERATED_GO = """package v1alpha1

type AgentPluginSpec struct {
	Bogus string `json:"bogusFromGenerated"`
}
"""

PLATFORMAGENT_PAGE = "# PlatformAgent\n\nFields: `mode`, `storage`, `storageSize`, `labels`, `rules`, `port`, `secretRef`, `level`, `phase`.\n"
AGENTPROFILE_PAGE = "# AgentProfile\n\nFields: `persona`, `ready`.\n"
AGENTPLUGIN_PAGE = "# AgentPlugin\n\nFields: `image`, `applied`.\n"

JOBS_JSON = {
    "jobs": [
        {"id": "a", "skills": ["fleet-audit"], "enabled": True},
        {"id": "b", "skills": ["fleet-audit", "other"], "enabled": True},
        {"id": "c", "skills": ["fleet-audit"]},
        {"id": "disabled", "skills": ["fleet-audit"], "enabled": False},
        {"id": "watcher", "skills": [], "no_agent": True, "enabled": True},
    ]
}
FLEET_AUDITS = 3

IMAGES_JSON = {
    "images": [
        {"name": "platform-agent"},
        {"name": "a2a-gateway"},
        {"name": "a2a-worker"},
    ]
}
A2A_IMAGES = 2

RELEASE_COMMON_SH = """#!/usr/bin/env bash
export REQUIRED_RELEASE_IMAGES=(
  "k8s-operator"
  "platform-agent"
  "a2a-gateway"
  "a2a-worker"
)
other_function() { :; }
"""
RELEASE_IMAGES = 4

PLATFORM_SKILLS = 2
CLUSTER_SKILLS = 1

SITE = contracts.SITE_ROOT_REL
PLATFORMAGENT_PAGE_REL = contracts.CRD_PAGES_REL["PlatformAgent"]
PAGE_NAME = "page.md"


class RepoFixture(unittest.TestCase):
    """A throwaway repository per test, green until a test edits it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.write("install.sh", INSTALL_SH)
        self.write("upgrade.sh", UPGRADE_SH)
        self.write("uninstall.sh", UNINSTALL_SH)
        self.write(contracts.API_TYPES_DIR_REL / "types.go", TYPES_GO)
        self.write(contracts.API_TYPES_DIR_REL / "types_test.go", TYPES_TEST_GO)
        self.write(contracts.API_TYPES_DIR_REL / "zz_generated.deepcopy.go", GENERATED_GO)
        self.write(contracts.CRD_PAGES_REL["PlatformAgent"], PLATFORMAGENT_PAGE)
        self.write(contracts.CRD_PAGES_REL["AgentProfile"], AGENTPROFILE_PAGE)
        self.write(contracts.CRD_PAGES_REL["AgentPlugin"], AGENTPLUGIN_PAGE)
        self.write("INSTALL.md", "# Install\n\nRun `./install.sh --dry-run` first.\n")
        self.write("README.md", "# kube-agents\n")
        self.write(contracts.JOBS_REL, json.dumps(JOBS_JSON))
        self.write(contracts.IMAGES_REL, json.dumps(IMAGES_JSON))
        self.write(contracts.RELEASE_COMMON_REL, RELEASE_COMMON_SH)
        for persona, count in (("platform", PLATFORM_SKILLS), ("cluster", CLUSTER_SKILLS)):
            for index in range(count):
                self.write(contracts.AGENTS_DIR_REL / persona / "skills" / f"skill-{index}" / "SKILL.md", "---\n")
        # A persona with no skills directory, and a skills entry with no SKILL.md.
        (self.repo / contracts.AGENTS_DIR_REL / "chat").mkdir()
        (self.repo / contracts.AGENTS_DIR_REL / "platform" / "skills" / "not-a-skill").mkdir()
        self.write(contracts.ALLOWLIST_REL, "# empty\n")

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def remove(self, rel):
        (self.repo / rel).unlink()

    def run_main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = contracts.main(self.repo)
        return code, out.getvalue()

    def findings(self, body=None, name=PAGE_NAME):
        if body is not None:
            self.write(SITE / name, body)
        return [(f.check, f.token, f.line) for f in contracts.scan(self.repo)]

    def flag_findings(self, body):
        return self.flag_findings_in(body, PAGE_NAME)

    def flag_findings_in(self, body, name):
        return [(token, line) for check, token, line in self.findings(body, name) if check == contracts.CHECK_FLAG]

    def count_findings(self, body):
        return [token for check, token, _ in self.findings(body) if check == contracts.CHECK_COUNT]


class FixtureIsGreenTest(RepoFixture):
    def test_fixture_passes(self):
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.findings(), [])


class FlagTest(RepoFixture):
    def test_unknown_flag_in_fenced_command_fails(self):
        self.assertEqual(self.flag_findings("```bash\n./install.sh --gvisor=false\n```\n"), [("--gvisor", 2)])

    def test_known_flags_pass(self):
        self.assertEqual(self.flag_findings("```bash\n./install.sh --enable-gvisor=false --dry-run -y\n```\n"), [])

    def test_flag_of_another_installer_script_fails_on_a_command(self):
        """`--plan` is upgrade.sh's; on install.sh it is unknown."""
        self.assertEqual(self.flag_findings("```bash\n./install.sh --plan\n```\n"), [("--plan", 2)])

    def test_inline_code_command_is_checked(self):
        self.assertEqual(self.flag_findings("Run `./upgrade.sh --bogus` first.\n"), [("--bogus", 1)])

    def test_another_commands_flag_is_not_attributed(self):
        body = (
            "```bash\n"
            "gcloud container clusters create x --project p && ./install.sh --dry-run\n"
            "./install.sh --help | grep -- --bogus\n"
            "kubectl get pods --namespace x; ./upgrade.sh --plan\n"
            "```\n"
        )
        self.assertEqual(self.flag_findings(body), [])

    def test_curl_pipe_bash_form_attributes_the_runner_segment(self):
        self.assertEqual(
            self.flag_findings("```bash\ncurl -fsSL https://example/install.sh | bash -s -- --plan\n```\n"),
            [("--plan", 2)],
        )
        self.assertEqual(
            self.flag_findings(
                "```bash\ncurl -fsSL https://example/upgrade.sh | KUBE_AGENTS_INSTALL_ENV=x bash -s -- --plan\n```\n"
            ),
            [],
        )

    def test_runner_options_before_the_bare_separator_are_not_the_scripts(self):
        """`sudo --preserve-env` and `bash --noprofile` are the runner's; the script's start after `--`."""
        body = (
            "```bash\n"
            "curl -fsSL https://example/install.sh | sudo --preserve-env=PATH bash -s -- --dry-run\n"
            "curl -fsSL https://example/install.sh | bash --noprofile -s -- --bogus\n"
            "curl -fsSL https://example/install.sh | bash --norc\n"
            "```\n"
        )
        self.assertEqual(self.flag_findings(body), [("--bogus", 3)])

    def test_namesake_script_in_the_tree_is_not_the_installer(self):
        """A path ending in another install.sh's directory cites nothing on the root one; other paths are the root one."""
        self.write("plugins/pubsub/install.sh", 'case "$1" in\n  --context) CONTEXT="$2"; shift 2 ;;\nesac\n')
        body = (
            "```bash\n"
            "bash plugins/pubsub/install.sh --context kind-dev --namespace agents\n"
            "plugins/pubsub/install.sh --namespace agents\n"
            "https://example/kube-agents/main/plugins/pubsub/install.sh --namespace agents\n"
            "$HOME/kube-agents/install.sh --bogus-root\n"
            "~/Downloads/install.sh --bogus-download\n"
            "```\n"
            "\n"
            "Run `plugins/pubsub/install.sh` with `--context`.\n"
        )
        self.assertEqual(self.flag_findings(body), [("--bogus-root", 5), ("--bogus-download", 6)])

    def test_script_as_another_commands_argument_cites_nothing(self):
        body = (
            "```bash\n"
            "curl -o install.sh https://example/install.sh --retry 3\n"
            "diff install.sh --brief other.sh\n"
            "```\n"
        )
        self.assertEqual(self.flag_findings(body), [])

    def test_runner_and_prefix_forms_are_the_scripts_command(self):
        body = (
            "```bash\n"
            "$ sudo -E ./install.sh --bogus-one\n"
            "KUBE_AGENTS_INSTALL_ENV=x bash install.sh --bogus-two\n"
            "bash <(curl -fsSL https://example/install.sh) --bogus-three\n"
            "curl -fsSL https://example/install.sh | sudo -E bash -s -- --bogus-four\n"
            "```\n"
        )
        self.assertEqual(
            self.flag_findings(body),
            [("--bogus-one", 2), ("--bogus-two", 3), ("--bogus-three", 4), ("--bogus-four", 5)],
        )

    def test_pipe_inside_a_process_substitution_does_not_split_the_command(self):
        body = "```bash\nTOKEN_FILE=<(pass show x | head -n1) ./install.sh -y --bogus\n```\n"
        self.assertEqual(self.flag_findings(body), [("--bogus", 2)])

    def test_options_inside_substitutions_and_quoted_values_are_not_the_scripts(self):
        """`jq --raw-output` in `$(…)`, `head --lines` in `<(…)` and a quoted `--inner` are theirs, not the script's."""
        body = (
            "```bash\n"
            "curl -fsSL https://example/upgrade.sh | KUBE_AGENTS_INSTALL_ENV=\"x\" bash -s -- \\\n"
            "  --plan --upgrade-mode=\"$(cat state | jq --raw-output '.mode')\"\n"
            "./install.sh --gcp-project-id=$(git rev-parse --short HEAD) --bogus-one\n"
            "./install.sh --enable-gvisor=<(pass show x | head --lines=1) --dry-run >(tee --append log)\n"
            "./install.sh --bogus-two \"--inner\" '--inner' --yes=a\\;b --bogus-three\n"
            "```\n"
        )
        self.assertEqual(self.flag_findings(body), [("--bogus-one", 4), ("--bogus-three", 6), ("--bogus-two", 6)])

    def test_removal_script_is_not_a_mention_of_the_install_script(self):
        """`--source-ref` is the removal script's own flag; neither rule attributes it to install.sh."""
        body = "```bash\n./uninstall.sh --source-ref=v1.2.3 --dry-run\n```\n\nRun `uninstall.sh` with `--source-ref`.\n"
        self.assertEqual(self.flag_findings(body), [])
        self.assertEqual(self.flag_findings("```bash\n./uninstall.sh --bogus\n```\n"), [("--bogus", 2)])

    def test_mdx_pages_are_scanned(self):
        self.assertEqual(
            self.flag_findings_in("```bash\n./install.sh --gvisor=false\n```\n", "page.mdx"),
            [("--gvisor", 2)],
        )

    def test_continuation_lines_are_one_command(self):
        body = "```bash\n./install.sh --dry-run \\\n  --gcp-project-id=p \\\n  --bogus\n```\n"
        self.assertEqual(self.flag_findings(body), [("--bogus", 2)])

    def test_shell_comment_is_not_a_command(self):
        self.assertEqual(self.flag_findings("```bash\n./install.sh --dry-run  # not --bogus\n```\n"), [])

    def test_standalone_span_in_a_paragraph_about_one_script_fails(self):
        body = "`install.sh` runs the agent sandboxed by default (`--gvisor=false` opts out).\n"
        self.assertEqual(self.flag_findings(body), [("--gvisor", 1)])

    def test_standalone_span_line_number_inside_a_block(self):
        body = "The table below is what `install.sh` checks.\n| Requirement |\n| --- |\n| sandboxed (`--gvisor=false` opts out) |\n"
        self.assertEqual(self.flag_findings(body), [("--gvisor", 4)])

    def test_standalone_span_known_to_another_script_passes(self):
        """A paragraph about install.sh may quote an upgrade.sh flag."""
        self.assertEqual(self.flag_findings("After `install.sh`, `--plan` previews a run.\n"), [])

    def test_standalone_span_in_a_paragraph_naming_two_scripts_is_not_attributed(self):
        self.assertEqual(self.flag_findings("`install.sh` and `upgrade.sh` differ; `--bogus` is helm's.\n"), [])

    def test_standalone_span_in_a_paragraph_naming_no_script_is_not_attributed(self):
        self.assertEqual(self.flag_findings("Pass `--bogus` to the tool.\n\n`install.sh` is elsewhere.\n"), [])

    def test_span_with_an_argument_is_not_standalone(self):
        self.assertEqual(self.flag_findings("`install.sh` renders the chart; `--set a=b` tunes it.\n"), [])

    def test_fenced_code_is_not_a_paragraph(self):
        self.assertEqual(self.flag_findings("`install.sh` is below.\n```\n`--bogus`\n```\n"), [])

    def test_fence_closes_on_a_run_at_least_as_long_with_nothing_after(self):
        """A four-backtick block showing a three-backtick one stays code; a closer carries no info string."""
        body = (
            "````markdown\n"
            "```bash\n"
            "./install.sh --inner-bogus\n"
            "```\n"
            "Prose inside the outer block: `--not-a-span`.\n"
            "````\n"
            "\n"
            "After the block, `install.sh` takes `--outer-bogus`.\n"
            "\n"
            "```\n"
            "./install.sh --first\n"
            "```bash\n"
            "./install.sh --second\n"
            "```\n"
        )
        self.assertEqual(
            self.flag_findings(body),
            [("--inner-bogus", 3), ("--outer-bogus", 8), ("--first", 11), ("--second", 13)],
        )

    def test_allowlisted_flag_passes(self):
        self.write(SITE / PAGE_NAME, "```bash\n./install.sh --gvisor=false\n```\n")
        self.write(contracts.ALLOWLIST_REL, f"flag {(SITE / PAGE_NAME).as_posix()} --gvisor\n")
        code, out = self.run_main()
        self.assertEqual(code, 0, out)


class FieldTest(RepoFixture):
    def test_field_through_inline_embed_and_pointer_is_required(self):
        """`storageSize` is reached PlatformAgentSpec -> AgentSpec (inline) -> *StorageSpec."""
        self.write(PLATFORMAGENT_PAGE_REL, PLATFORMAGENT_PAGE.replace("`storageSize`, ", ""))
        self.assertEqual(self.findings(), [(contracts.CHECK_FIELD, "storageSize", 0)])

    def test_field_through_slice_is_required(self):
        self.write(PLATFORMAGENT_PAGE_REL, PLATFORMAGENT_PAGE.replace("`port`, ", ""))
        self.assertEqual(self.findings(), [(contracts.CHECK_FIELD, "port", 0)])

    def test_status_field_is_required(self):
        self.write(PLATFORMAGENT_PAGE_REL, PLATFORMAGENT_PAGE.replace("`phase`", "`other`"))
        self.assertEqual(self.findings(), [(contracts.CHECK_FIELD, "phase", 0)])

    def test_field_name_must_appear_whole(self):
        self.write(PLATFORMAGENT_PAGE_REL, PLATFORMAGENT_PAGE.replace("`mode`", "`modeSelector`"))
        self.assertEqual(self.findings(), [(contracts.CHECK_FIELD, "mode", 0)])

    def test_skipped_tag_and_other_kinds_fields_are_not_required(self):
        """`json:"-"` is never required, and AgentProfile's fields are not PlatformAgent's."""
        self.assertNotIn("Hidden", PLATFORMAGENT_PAGE)
        self.assertNotIn("persona", PLATFORMAGENT_PAGE)
        self.assertEqual(self.findings(), [])

    def test_test_and_generated_files_are_not_read(self):
        self.assertEqual(self.findings(), [])

    def test_missing_field_fails_and_allowlisted_passes(self):
        self.write(PLATFORMAGENT_PAGE_REL, PLATFORMAGENT_PAGE.replace("`storageSize`, ", ""))
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("storageSize", out)
        self.write(contracts.ALLOWLIST_REL, f"field {PLATFORMAGENT_PAGE_REL.as_posix()} storageSize\n")
        code, out = self.run_main()
        self.assertEqual(code, 0, out)

    def test_kind_without_spec_and_status_fails_loudly(self):
        self.write(contracts.API_TYPES_DIR_REL / "types.go", TYPES_GO.replace("type AgentPlugin struct", "type Renamed struct"))
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("AgentPlugin", out)
        self.assertIn("cannot run", out)


class CountTest(RepoFixture):
    def test_stale_count_fails_in_digit_and_word_form(self):
        self.assertEqual(self.count_findings(f"There are {FLEET_AUDITS + 1} fleet audits.\n"), [f"{FLEET_AUDITS + 1} fleet audits"])
        self.assertEqual(self.count_findings("The two governance watchdogs run nightly.\n"), ["two governance watchdogs"])
        self.assertEqual(self.count_findings("a Pod runs six audit crons alongside workers\n"), ["six audit crons"])

    def test_current_count_passes(self):
        body = (
            "The three fleet audits, 3 governance watchdogs and **three** audit crons.\n"
            "Two A2A next-stack images; the 2 A2A images.\n"
            "Four release images and 4 required release images.\n"
            "Two platform skills, one cluster skill, 2 Platform Agent skills, one cluster-agent skill.\n"
        )
        self.assertEqual(self.count_findings(body), [])

    def test_markup_between_number_and_phrase_is_read(self):
        self.assertEqual(self.count_findings("**two** fleet audits\n"), ["**two** fleet audits"])

    def test_partitive_is_not_a_count(self):
        self.assertEqual(self.count_findings("four of the fleet audits write a ledger.\n"), [])

    def test_singular_noun_is_not_a_count(self):
        body = "Run one fleet audit at a time; the one A2A console image; 1 release image; one platform skill.\n"
        self.assertEqual(self.count_findings(body), [])

    def test_compound_number_word_is_read_whole(self):
        """`twenty-one` is 21, never the `one` the hyphen refuses; the tens run to ninety."""
        self.assertEqual(self.count_findings("twenty-one fleet audits\n"), ["twenty-one fleet audits"])
        self.assertEqual(self.count_findings("the **Fifty-one** platform skills and ninety cluster skills\n"),
                         ["**Fifty-one** platform skills", "ninety cluster skills"])
        words = ("twenty-one", "Fifty-one", "ninety", "twenty")
        self.assertEqual([contracts.number_value(word) for word in words], [21, 51, 90, 20])
        self.write(SITE / PAGE_NAME, "# Title\n\ntwenty-one fleet audits\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("says 21 fleet audits", out)

    def test_dotted_version_before_a_count_noun_is_not_a_count(self):
        body = "The 0.5.0 release images; roll back to the v1.2 release images; the 1.30 cluster skills.\n"
        self.assertEqual(self.count_findings(body), [])

    def test_a2a_and_release_and_persona_counts(self):
        self.assertEqual(self.count_findings("the four A2A `next`-stack images\n"), ["four A2A `next`-stack images"])
        self.assertEqual(self.count_findings("three required release images\n"), ["three required release images"])
        self.assertEqual(self.count_findings("three platform skills\n"), ["three platform skills"])
        self.assertEqual(self.count_findings("two cluster skills\n"), ["two cluster skills"])

    def test_persona_without_skills_directory_has_no_rule(self):
        self.assertEqual(self.count_findings("seven chat skills\n"), [])

    def test_disabled_entry_is_not_counted(self):
        roster = json.loads(json.dumps(JOBS_JSON))
        roster["jobs"][0]["enabled"] = False
        self.write(contracts.JOBS_REL, json.dumps(roster))
        self.assertEqual(self.count_findings("two fleet audits\n"), [])

    def test_count_line_number_and_report(self):
        self.write(SITE / PAGE_NAME, "# Title\n\nnine fleet audits\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"{(SITE / PAGE_NAME).as_posix()}:3: count", out)
        self.assertIn(f"gives {FLEET_AUDITS}", out)

    def test_allowlisted_count_passes_whatever_its_markup(self):
        self.write(SITE / PAGE_NAME, "the four A2A `next`-stack images\n")
        self.write(contracts.ALLOWLIST_REL, f"count {(SITE / PAGE_NAME).as_posix()} Four a2a next-stack images\n")
        code, out = self.run_main()
        self.assertEqual(code, 0, out)


class AllowlistTest(RepoFixture):
    def test_entry_with_no_finding_fails(self):
        self.write(contracts.ALLOWLIST_REL, f"flag {(SITE / PAGE_NAME).as_posix()} --gvisor\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("drop the entry", out)

    def test_malformed_line_fails(self):
        self.write(contracts.ALLOWLIST_REL, "bogus docs/page.md token\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("ERROR", out)

    def test_missing_allowlist_fails(self):
        self.remove(contracts.ALLOWLIST_REL)
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("not found", out)


class SourceFileTest(RepoFixture):
    def test_each_missing_source_fails_loudly(self):
        sources = [
            Path("install.sh"),
            Path("upgrade.sh"),
            Path("uninstall.sh"),
            contracts.CRD_PAGES_REL["AgentProfile"],
            contracts.JOBS_REL,
            contracts.IMAGES_REL,
            contracts.RELEASE_COMMON_REL,
            Path("INSTALL.md"),
        ]
        for rel in sources:
            with self.subTest(source=rel.as_posix()):
                text = (self.repo / rel).read_text(encoding="utf-8")
                self.remove(rel)
                try:
                    code, out = self.run_main()
                finally:
                    self.write(rel, text)
                self.assertEqual(code, 1)
                self.assertIn(rel.as_posix(), out)
                self.assertIn("cannot run", out)

    def test_script_without_case_arms_fails_loudly(self):
        self.write("upgrade.sh", "#!/usr/bin/env bash\necho no flags\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("upgrade.sh", out)
        self.assertIn("cannot run", out)

    def test_empty_release_image_list_fails_loudly(self):
        self.write(contracts.RELEASE_COMMON_REL, "export REQUIRED_RELEASE_IMAGES=(\n)\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("REQUIRED_RELEASE_IMAGES", out)

    def test_roster_without_fleet_audits_fails_loudly(self):
        self.write(contracts.JOBS_REL, json.dumps({"jobs": [{"id": "x", "skills": [], "enabled": True}]}))
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("fleet-audit", out)

    def test_no_site_page_fails_loudly(self):
        for page in (self.repo / SITE).rglob("*.md"):
            page.unlink()
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("scans nothing", out)


class PatternBacktrackingTest(unittest.TestCase):
    """Each shell pattern has one parse per input, so a crafted line cannot stall the check.

    The match runs in a subprocess under a timeout. A pattern that backtracks
    exponentially never returns at this repetition count, and CPython's `re`
    checks no signal while matching, so an in-process clock could only hang
    the test sweep; the timeout turns the hang into a failed test.
    """

    REPETITIONS = 60
    # Interpreter start included. A regressed pattern never returns at
    # REPETITIONS, so the bound sets how soon the test goes red, not how fast
    # a sound pattern must be (those match in under a millisecond).
    TIME_BOUND_SECONDS = 5.0
    MATCH_SNIPPET = "import sys, check_docs_contracts; getattr(check_docs_contracts, sys.argv[1]).match(sys.argv[2])"

    def assert_fast(self, pattern_name, text):
        timed_out = False
        try:
            subprocess.run(
                [sys.executable, "-c", self.MATCH_SNIPPET, pattern_name, text],
                cwd=Path(__file__).resolve().parent,
                check=True,
                capture_output=True,
                timeout=self.TIME_BOUND_SECONDS,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
        self.assertFalse(timed_out, f"{pattern_name} did not finish on {len(text)} characters in {self.TIME_BOUND_SECONDS}s")

    def test_case_arm_with_many_bare_separators(self):
        self.assert_fast("CASE_ARM_RE", "--0|" + "!|" * self.REPETITIONS)

    def test_assignment_with_many_process_substitutions(self):
        """The match fails as a whole (`y ./` is no script), so every tiling of the value would be tried."""
        value = "X=" + "<(a)" * self.REPETITIONS + " y ./"
        self.assert_fast("PIPED_RUNNER_RE", value)
        self.assert_fast("SCRIPT_COMMAND_PREFIX_RE", value)

    def test_unclosed_substitution_is_a_value_character(self):
        self.assertIsNotNone(contracts.SCRIPT_COMMAND_PREFIX_RE.match("X=<(a ./"))
        self.assertIsNotNone(contracts.PIPED_RUNNER_RE.match("X=<(a bash -s -- "))


class RealTreeTest(unittest.TestCase):
    def test_repository_passes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = contracts.main()
        self.assertEqual(code, 0, out.getvalue())


if __name__ == "__main__":
    unittest.main()
