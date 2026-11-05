""".github/workflows/docs-check.yml runs every CI documentation target unconditionally.

`docs/README.md` states that `make docs-check` runs the same checks as
`.github/workflows/docs-check.yml`. Contributors and automated agents without
the GitHub App `workflows` permission add a new `docs-check-*` prerequisite to
`Makefile` first and list it temporarily in `PENDING_WORKFLOW_TARGETS`; a
maintainer follow-up pull request wires the workflow step and drops the
temporary entry (as this test finishes for `docs-check-prose` after `#3961`;
protected instruction-file references are tracked in `#3996`).
"""

import pathlib
import re
import unittest
from unittest import mock

import yaml

from tests._run_make import run_make

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "docs-check.yml"

#: Targets added to `Makefile` `docs-check:` by a contributor without `workflows`
#: permission that still await their `.github/workflows/docs-check.yml` step.
#: Must stay empty once the maintainer follow-up lands.
PENDING_WORKFLOW_TARGETS: frozenset[str] = frozenset()

_JOB_ID = "docs-check"
_MAKEFILE_DB_RULE_RE = re.compile(r"^docs-check:(?:\s+(?P<rest>.*))?$")
_TARGET_VAR_MODIFIERS = frozenset({"export", "unexport", "override", "private", "undefine"})
_RECIPE_PREFIX_RE = re.compile(r"^\t([@+ \t-]*)")
_EXACT_MAKE_STEP_RE = re.compile(r"^make (?P<target>docs-check-[a-z0-9-]+)$")
_PINNED_CHECKOUT_RE = re.compile(r"^actions/checkout@[0-9a-f]{40}$")

_MAKE_ENV_VARIABLES = ("MAKEFLAGS", "GNUMAKEFLAGS", "MAKEFILES", "MFLAGS")
_FAIL_FAST_SHELLS = (None, "bash", "sh")
_REPOSITORY_ROOT_DIRECTORIES = (None, ".", "./")
_REQUIRED_PR_TYPES = frozenset({"opened", "synchronize", "reopened"})
_ALLOWED_WORKFLOW_KEYS = frozenset({"name", "on", True, "concurrency", "permissions", "jobs"})
_ALLOWED_JOB_KEYS = frozenset({"name", "runs-on", "timeout-minutes", "permissions", "steps"})
_ALLOWED_RUN_STEP_KEYS = frozenset({"name", "run"})
_ALLOWED_CHECKOUT_STEP_KEYS = frozenset({"name", "uses", "with"})
_ALLOWED_CHECKOUT_WITH_KEYS = frozenset({"fetch-depth", "persist-credentials"})


def _is_target_specific_variable(before_comment: str) -> bool:
    """Return True when a `docs-check: ...` line in `make -p` is a target variable."""
    if "=" in before_comment:
        return True
    tokens = before_comment.split()
    return bool(tokens and tokens[0] in _TARGET_VAR_MODIFIERS)


def _resolved_target_recipe(database_text: str, target: str) -> list[str]:
    """Return logical recipe lines GNU Make resolved for `target`."""
    lines = database_text.splitlines()
    rule_re = re.compile(rf"^{re.escape(target)}:(?:\s+(?P<rest>.*))?$")
    start: int | None = None
    for idx, line in enumerate(lines):
        match = rule_re.match(line)
        if not match:
            continue
        before_comment = (match.group("rest") or "").split("#", 1)[0]
        if _is_target_specific_variable(before_comment):
            continue
        start = idx
        break
    if start is None:
        raise ValueError(f"GNU Make database declares no `{target}:` target")

    physical: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            continue
        if not line.startswith("\t"):
            break
        physical.append(line)

    logical: list[str] = []
    for line in physical:
        if logical and logical[-1].rstrip().endswith("\\"):
            logical[-1] = logical[-1].rstrip()[:-1] + line.lstrip()
        else:
            logical.append(line)
    return logical


def _ignored_recipe_lines(logical_lines: list[str]) -> list[str]:
    """Return recipe lines whose Make prefix cluster includes `-` (ignore-errors)."""
    return [
        line
        for line in logical_lines
        if (prefix := _RECIPE_PREFIX_RE.match(line)) is not None and "-" in prefix.group(1)
    ]


def makefile_docs_check_prereqs(database_text: str | None = None) -> list[str]:
    """Return expanded `docs-check:` prerequisites and verify fail-fast recipes."""
    if database_text is None:
        proc = run_make(["-pn", "--eval=__probe: ;@:", "__probe"], timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"make -pn failed (rc={proc.returncode}): {proc.stderr}")
        database_text = proc.stdout

    prereqs: list[str] = []
    found_rule = False
    for line in database_text.splitlines():
        match = _MAKEFILE_DB_RULE_RE.match(line)
        if not match:
            continue
        before_comment = (match.group("rest") or "").split("#", 1)[0]
        if _is_target_specific_variable(before_comment):
            continue
        found_rule = True
        normal_prereqs = before_comment.split("|", 1)[0]
        for token in normal_prereqs.split():
            if token not in prereqs:
                prereqs.append(token)
    if not found_rule:
        raise ValueError("GNU Make database declares no docs-check: target")

    for target in prereqs:
        recipe = _resolved_target_recipe(database_text, target)
        if not recipe:
            raise ValueError(f"Makefile prerequisite `{target}` declares an empty recipe")
        ignored = _ignored_recipe_lines(recipe)
        if ignored:
            raise ValueError(
                f"Makefile prerequisite `{target}` ignores recipe errors via `-` prefix: {ignored}"
            )
    return prereqs


def _has_make_env_override(env_block: object) -> bool:
    return isinstance(env_block, dict) and any(
        variable in env_block for variable in _MAKE_ENV_VARIABLES
    )


def _check_defaults_block(defaults_block: object, scope_label: str) -> None:
    if defaults_block is None:
        return
    if not isinstance(defaults_block, dict):
        raise ValueError(f"{scope_label} `defaults` must be a mapping")
    run_defaults = defaults_block.get("run")
    if run_defaults is None:
        return
    if not isinstance(run_defaults, dict):
        raise ValueError(f"{scope_label} `defaults.run` must be a mapping")
    if run_defaults.get("working-directory") not in _REPOSITORY_ROOT_DIRECTORIES:
        raise ValueError(f"{scope_label} must not override defaults.run.working-directory")
    if run_defaults.get("shell") not in _FAIL_FAST_SHELLS:
        raise ValueError(f"{scope_label} defaults.run.shell must be fail-fast (bash or sh)")


def validate_docs_check_workflow(
    workflow_text: str,
    prereqs: list[str],
    pending: frozenset[str] | None = None,
) -> list[str]:
    """Validate `docs-check.yml` against `Makefile` `docs-check:` prerequisites."""
    active_pending = PENDING_WORKFLOW_TARGETS if pending is None else pending

    doc = yaml.safe_load(workflow_text)
    if not isinstance(doc, dict):
        raise ValueError("docs-check.yml must parse to a YAML mapping")

    on_block = doc.get("on", doc.get(True))
    if not isinstance(on_block, dict) or "pull_request" not in on_block:
        raise ValueError("docs-check.yml must trigger on pull_request")
    pr_trigger = on_block["pull_request"]
    if pr_trigger is not None and not isinstance(pr_trigger, dict):
        raise ValueError("docs-check.yml pull_request trigger must be empty or a mapping")
    if isinstance(pr_trigger, dict):
        if "paths" in pr_trigger or "paths-ignore" in pr_trigger:
            raise ValueError(
                "docs-check.yml pull_request trigger must not filter paths; "
                "Documentation Checks is a required status check on every pull request"
            )
        if "branches" in pr_trigger or "branches-ignore" in pr_trigger:
            raise ValueError("docs-check.yml pull_request trigger must not filter branches")
        if "types" in pr_trigger:
            pr_types = pr_trigger["types"]
            if not isinstance(pr_types, list) or not (_REQUIRED_PR_TYPES <= set(pr_types)):
                raise ValueError(
                    "docs-check.yml pull_request.types must include opened, synchronize, and reopened"
                )
        extra_pr_keys = sorted(set(pr_trigger) - {"types"})
        if extra_pr_keys:
            raise ValueError(
                f"docs-check.yml pull_request trigger carries unexpected filter keys: {extra_pr_keys}"
            )

    if _has_make_env_override(doc.get("env")):
        raise ValueError(
            "docs-check.yml must not set workflow-level MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, or MFLAGS"
        )
    _check_defaults_block(doc.get("defaults"), "docs-check.yml")
    extra_workflow_keys = sorted(
        [str(key) for key in doc if key not in _ALLOWED_WORKFLOW_KEYS]
    )
    if extra_workflow_keys:
        raise ValueError(
            f"docs-check.yml carries unexpected top-level keys: {extra_workflow_keys}"
        )

    jobs = doc.get("jobs")
    job = jobs.get(_JOB_ID) if isinstance(jobs, dict) else None
    if not isinstance(job, dict):
        raise ValueError(f"docs-check.yml declares no `{_JOB_ID}` job")
    extra_jobs = sorted(set(jobs) - {_JOB_ID})
    if extra_jobs:
        raise ValueError(f"docs-check.yml declares unexpected extra jobs: {extra_jobs}")
    if "if" in job:
        raise ValueError(f"`{_JOB_ID}` job must not carry an `if:` condition")
    if "needs" in job:
        raise ValueError(
            f"`{_JOB_ID}` job must not declare `needs`; a skipped upstream job skips `{_JOB_ID}`"
        )
    if job.get("continue-on-error") is not None:
        raise ValueError(f"`{_JOB_ID}` job must not set `continue-on-error`")
    if _has_make_env_override(job.get("env")):
        raise ValueError(
            f"`{_JOB_ID}` job must not set MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, or MFLAGS"
        )
    _check_defaults_block(job.get("defaults"), f"`{_JOB_ID}` job")
    extra_job_keys = sorted(set(job) - _ALLOWED_JOB_KEYS)
    if extra_job_keys:
        raise ValueError(
            f"`{_JOB_ID}` job carries unexpected keys outside {sorted(_ALLOWED_JOB_KEYS)}: {extra_job_keys}"
        )

    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"`{_JOB_ID}` job declares no steps")

    workflow_targets: list[str] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"step #{index} in `{_JOB_ID}` must be a mapping")
        step_label = step.get("name", f"step #{index}")
        if "run" not in step:
            if index != 0:
                raise ValueError(
                    f"non-run step {step_label!r} is only permitted as step #0 in `{_JOB_ID}`"
                )
            uses_target = step.get("uses")
            if not isinstance(uses_target, str) or not _PINNED_CHECKOUT_RE.match(uses_target):
                raise ValueError(
                    f"non-run step {step_label!r} in `{_JOB_ID}` must be `actions/checkout@<40-hex-sha>`"
                )
            extra_checkout_keys = sorted(set(step) - _ALLOWED_CHECKOUT_STEP_KEYS)
            if extra_checkout_keys:
                raise ValueError(
                    f"checkout step {step_label!r} carries unexpected keys: {extra_checkout_keys}"
                )
            if "with" in step:
                checkout_with = step["with"]
                if not isinstance(checkout_with, dict):
                    raise ValueError(f"checkout step {step_label!r} `with` must be a mapping")
                extra_with_keys = sorted(set(checkout_with) - _ALLOWED_CHECKOUT_WITH_KEYS)
                if extra_with_keys:
                    raise ValueError(
                        f"checkout step {step_label!r} `with` carries disallowed keys "
                        f"(such as ref/repository/path/sparse-checkout): {extra_with_keys}"
                    )
            continue

        if "if" in step:
            raise ValueError(f"run step {step_label!r} must not carry an `if:` condition")
        if step.get("continue-on-error") is not None:
            raise ValueError(f"run step {step_label!r} must not set `continue-on-error`")
        if step.get("working-directory") not in _REPOSITORY_ROOT_DIRECTORIES:
            raise ValueError(f"run step {step_label!r} must not override `working-directory`")
        if step.get("shell") not in _FAIL_FAST_SHELLS:
            raise ValueError(
                f"run step {step_label!r} must not override `shell` with a non-fail-fast template"
            )
        if _has_make_env_override(step.get("env")):
            raise ValueError(
                f"run step {step_label!r} must not set MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, or MFLAGS"
            )
        extra_step_keys = sorted(set(step) - _ALLOWED_RUN_STEP_KEYS)
        if extra_step_keys:
            raise ValueError(
                f"run step {step_label!r} carries unexpected keys outside {sorted(_ALLOWED_RUN_STEP_KEYS)}: {extra_step_keys}"
            )

        run_script = step["run"]
        lines = (
            [line.strip() for line in run_script.splitlines() if line.strip()]
            if isinstance(run_script, str)
            else []
        )
        if len(lines) != 1:
            raise ValueError(
                f"run step {step_label!r} must execute a single `make docs-check-*` command, got {lines!r}"
            )
        match = _EXACT_MAKE_STEP_RE.match(lines[0])
        if not match:
            raise ValueError(
                f"run step {step_label!r} must match `make docs-check-<name>`, got {lines[0]!r}"
            )
        workflow_targets.append(match.group("target"))

    stale_pending = sorted(active_pending - set(prereqs))
    if stale_pending:
        raise ValueError(
            f"PENDING_WORKFLOW_TARGETS names targets absent from Makefile docs-check: {stale_pending}"
        )
    already_wired = sorted(active_pending & set(workflow_targets))
    if already_wired:
        raise ValueError(
            f"PENDING_WORKFLOW_TARGETS still lists targets already wired in docs-check.yml: {already_wired}"
        )

    expected_targets = [target for target in prereqs if target not in active_pending]
    if workflow_targets != expected_targets:
        raise ValueError(
            f"docs-check.yml steps {workflow_targets} do not match Makefile docs-check prerequisites {expected_targets}"
        )
    return workflow_targets


_SAMPLE_PREREQS = ["docs-check-links", "docs-check-prose"]
_VALID_WORKFLOW = """
name: Documentation Checks
on:
  pull_request:
    types: [opened, synchronize, reopened]
  merge_group:
    branches: [main]
jobs:
  docs-check:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Checkout code
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
      - name: Links resolve
        run: make docs-check-links
      - name: Prose stays within its ratchet
        run: make docs-check-prose
"""


class DocsCheckWorkflowTest(unittest.TestCase):
    def test_committed_workflow_matches_makefile_docs_check_prerequisites(self) -> None:
        prereqs = makefile_docs_check_prereqs()
        self.assertIn("docs-check-prose", prereqs)
        targets = validate_docs_check_workflow(
            _WORKFLOW_PATH.read_text(encoding="utf-8"),
            prereqs,
        )
        self.assertEqual(
            targets,
            [target for target in prereqs if target not in PENDING_WORKFLOW_TARGETS],
        )

    def test_dropping_prose_step_from_committed_workflow_fails(self) -> None:
        prereqs = makefile_docs_check_prereqs()
        doc = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
        original_steps = doc["jobs"][_JOB_ID]["steps"]
        remaining_steps = [
            step
            for step in original_steps
            if not (isinstance(step, dict) and str(step.get("run", "")).strip() == "make docs-check-prose")
        ]
        self.assertEqual(
            len(remaining_steps),
            len(original_steps) - 1,
            "expected exactly one `make docs-check-prose` step in docs-check.yml",
        )
        doc["jobs"][_JOB_ID]["steps"] = remaining_steps
        without_prose = yaml.safe_dump(doc, sort_keys=False)
        with self.assertRaisesRegex(ValueError, "docs-check-prose"):
            validate_docs_check_workflow(without_prose, prereqs)

    def test_pending_allowlist_supports_two_step_handoff_and_rejects_stale_entries(self) -> None:
        real_prereqs = makefile_docs_check_prereqs()
        module = __import__(__name__, fromlist=["PENDING_WORKFLOW_TARGETS"])
        with (
            mock.patch.object(
                module,
                "PENDING_WORKFLOW_TARGETS",
                frozenset({"docs-check-future"}),
            ),
            mock.patch.object(
                module,
                "makefile_docs_check_prereqs",
                return_value=[*real_prereqs, "docs-check-future"],
            ),
        ):
            self.test_committed_workflow_matches_makefile_docs_check_prerequisites()
            self.test_dropping_prose_step_from_committed_workflow_fails()
            self.test_rejects_softened_conditional_or_filtered_gates()

        prereqs_with_new = [*_SAMPLE_PREREQS, "docs-check-future"]
        self.assertEqual(
            validate_docs_check_workflow(
                _VALID_WORKFLOW,
                prereqs_with_new,
                pending=frozenset({"docs-check-future"}),
            ),
            _SAMPLE_PREREQS,
        )
        with self.assertRaisesRegex(ValueError, "absent from Makefile"):
            validate_docs_check_workflow(
                _VALID_WORKFLOW,
                _SAMPLE_PREREQS,
                pending=frozenset({"docs-check-nonexistent"}),
            )
        with self.assertRaisesRegex(ValueError, "already wired"):
            validate_docs_check_workflow(
                _VALID_WORKFLOW,
                _SAMPLE_PREREQS,
                pending=frozenset({"docs-check-prose"}),
            )

    def test_rejects_softened_conditional_or_filtered_gates(self) -> None:
        mutations = (
            ("empty YAML", "", "YAML mapping"),
            (
                "missing pull_request trigger",
                _VALID_WORKFLOW.replace("  pull_request:\n    types: [opened, synchronize, reopened]\n", ""),
                "trigger on pull_request",
            ),
            (
                "pull_request paths filter",
                _VALID_WORKFLOW.replace(
                    "    types: [opened, synchronize, reopened]",
                    "    paths: ['docs/**']",
                ),
                "must not filter paths",
            ),
            (
                "pull_request branches filter",
                _VALID_WORKFLOW.replace(
                    "    types: [opened, synchronize, reopened]",
                    "    branches: ['release/**']",
                ),
                "must not filter branches",
            ),
            (
                "pull_request types missing synchronize",
                _VALID_WORKFLOW.replace(
                    "    types: [opened, synchronize, reopened]",
                    "    types: [closed]",
                ),
                "pull_request.types must include opened, synchronize, and reopened",
            ),
            (
                "workflow MAKEFLAGS",
                "env:\n  MAKEFLAGS: -n\n" + _VALID_WORKFLOW,
                "workflow-level MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, or MFLAGS",
            ),
            (
                "workflow GNUMAKEFLAGS",
                "env:\n  GNUMAKEFLAGS: -n\n" + _VALID_WORKFLOW,
                "workflow-level MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, or MFLAGS",
            ),
            (
                "workflow MAKEFILES",
                "env:\n  MAKEFILES: hack/soft.mk\n" + _VALID_WORKFLOW,
                "workflow-level MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, or MFLAGS",
            ),
            (
                "workflow unexpected env key",
                "env:\n  OTHER: 1\n" + _VALID_WORKFLOW,
                "unexpected top-level keys",
            ),
            (
                "workflow working-directory",
                "defaults:\n  run:\n    working-directory: docs\n" + _VALID_WORKFLOW,
                "defaults.run.working-directory",
            ),
            (
                "workflow non-fail-fast shell",
                "defaults:\n  run:\n    shell: true {0}\n" + _VALID_WORKFLOW,
                "defaults.run.shell must be fail-fast",
            ),
            (
                "missing docs-check job",
                _VALID_WORKFLOW.replace("  docs-check:", "  other-job:"),
                "declares no `docs-check` job",
            ),
            (
                "extra sibling job",
                _VALID_WORKFLOW + "  decoy:\n    runs-on: ubuntu-latest\n    steps: []\n",
                "unexpected extra jobs",
            ),
            (
                "job if condition",
                _VALID_WORKFLOW.replace("    runs-on: ubuntu-latest", "    if: false\n    runs-on: ubuntu-latest"),
                "job must not carry an `if:`",
            ),
            (
                "job needs upstream gate",
                _VALID_WORKFLOW.replace(
                    "    runs-on: ubuntu-latest",
                    "    needs: [gate]\n    runs-on: ubuntu-latest",
                ),
                "job must not declare `needs`",
            ),
            (
                "job continue-on-error",
                _VALID_WORKFLOW.replace(
                    "    runs-on: ubuntu-latest",
                    "    continue-on-error: true\n    runs-on: ubuntu-latest",
                ),
                "job must not set `continue-on-error`",
            ),
            (
                "job MAKEFLAGS",
                _VALID_WORKFLOW.replace(
                    "    runs-on: ubuntu-latest",
                    "    env:\n      MAKEFLAGS: -i\n    runs-on: ubuntu-latest",
                ),
                "job must not set MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, or MFLAGS",
            ),
            (
                "job GNUMAKEFLAGS",
                _VALID_WORKFLOW.replace(
                    "    runs-on: ubuntu-latest",
                    "    env:\n      GNUMAKEFLAGS: -n\n    runs-on: ubuntu-latest",
                ),
                "job must not set MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, or MFLAGS",
            ),
            (
                "job MAKEFILES",
                _VALID_WORKFLOW.replace(
                    "    runs-on: ubuntu-latest",
                    "    env:\n      MAKEFILES: hack/soft.mk\n    runs-on: ubuntu-latest",
                ),
                "job must not set MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, or MFLAGS",
            ),
            (
                "job working-directory",
                _VALID_WORKFLOW.replace(
                    "    runs-on: ubuntu-latest",
                    "    defaults:\n      run:\n        working-directory: docs\n    runs-on: ubuntu-latest",
                ),
                "job must not override defaults.run.working-directory",
            ),
            (
                "job non-fail-fast shell",
                _VALID_WORKFLOW.replace(
                    "    runs-on: ubuntu-latest",
                    "    defaults:\n      run:\n        shell: 'bash -c \"exit 0\" {0}'\n    runs-on: ubuntu-latest",
                ),
                "defaults.run.shell must be fail-fast",
            ),
            (
                "job unexpected key",
                _VALID_WORKFLOW.replace(
                    "    runs-on: ubuntu-latest",
                    "    env:\n      OTHER: 1\n    runs-on: ubuntu-latest",
                ),
                "carries unexpected keys",
            ),
            (
                "empty steps",
                "name: C\non:\n  pull_request:\njobs:\n  docs-check:\n    steps: []\n",
                "declares no steps",
            ),
            (
                "non-checkout uses step",
                _VALID_WORKFLOW.replace(
                    "        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                    "        uses: actions/github-script@3d3c42e5aac5ba805825da76410c181273ba90b1",
                ),
                "must be `actions/checkout@<40-hex-sha>`",
            ),
            (
                "checkout with ref main",
                _VALID_WORKFLOW.replace(
                    "        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                    "        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
                    "        with:\n          ref: main",
                ),
                "disallowed keys",
            ),
            (
                "checkout with repository override",
                _VALID_WORKFLOW.replace(
                    "        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                    "        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
                    "        with:\n          repository: other/repo",
                ),
                "disallowed keys",
            ),
            (
                "second checkout step mid-job",
                _VALID_WORKFLOW
                + "      - name: Reset tree\n"
                + "        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n",
                "only permitted as step #0",
            ),
            (
                "step if condition",
                _VALID_WORKFLOW.replace(
                    "        run: make docs-check-prose",
                    "        if: github.event_name == 'push'\n        run: make docs-check-prose",
                ),
                "must not carry an `if:`",
            ),
            (
                "step continue-on-error",
                _VALID_WORKFLOW.replace(
                    "        run: make docs-check-prose",
                    "        continue-on-error: true\n        run: make docs-check-prose",
                ),
                "must not set `continue-on-error`",
            ),
            (
                "step working-directory",
                _VALID_WORKFLOW.replace(
                    "        run: make docs-check-prose",
                    "        working-directory: docs\n        run: make docs-check-prose",
                ),
                "must not override `working-directory`",
            ),
            (
                "step non-fail-fast shell",
                _VALID_WORKFLOW.replace(
                    "        run: make docs-check-prose",
                    "        shell: true {0}\n        run: make docs-check-prose",
                ),
                "must not override `shell` with a non-fail-fast template",
            ),
            (
                "step MAKEFLAGS",
                _VALID_WORKFLOW.replace(
                    "        run: make docs-check-prose",
                    "        env:\n          MAKEFLAGS: -n\n        run: make docs-check-prose",
                ),
                "must not set MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, or MFLAGS",
            ),
            (
                "step GNUMAKEFLAGS",
                _VALID_WORKFLOW.replace(
                    "        run: make docs-check-prose",
                    "        env:\n          GNUMAKEFLAGS: -n\n        run: make docs-check-prose",
                ),
                "must not set MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, or MFLAGS",
            ),
            (
                "step MAKEFILES",
                _VALID_WORKFLOW.replace(
                    "        run: make docs-check-prose",
                    "        env:\n          MAKEFILES: hack/soft.mk\n        run: make docs-check-prose",
                ),
                "must not set MAKEFLAGS, GNUMAKEFLAGS, MAKEFILES, or MFLAGS",
            ),
            (
                "step unexpected key",
                _VALID_WORKFLOW.replace(
                    "        run: make docs-check-prose",
                    "        shell: bash\n        run: make docs-check-prose",
                ),
                "carries unexpected keys",
            ),
            (
                "multiline shell wrapper",
                _VALID_WORKFLOW.replace(
                    "        run: make docs-check-prose",
                    "        run: |\n          set +e\n          make docs-check-prose",
                ),
                "single `make docs-check-\\*` command",
            ),
            (
                "dry-run make flag",
                _VALID_WORKFLOW.replace(
                    "        run: make docs-check-prose",
                    "        run: make -n docs-check-prose",
                ),
                "must match `make docs-check-<name>`",
            ),
            (
                "duplicate step",
                _VALID_WORKFLOW + "      - name: Dup\n        run: make docs-check-prose\n",
                "do not match Makefile",
            ),
        )
        for label, mutated_yaml, expected_pattern in mutations:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, expected_pattern):
                    validate_docs_check_workflow(
                        mutated_yaml,
                        _SAMPLE_PREREQS,
                        pending=frozenset(),
                    )

    def test_makefile_database_parser_ignores_variables_and_order_only_prereqs(self) -> None:
        sample_db = (
            "docs-check := not-a-rule\n"
            "docs-check: PYTHONWARNINGS := error\n"
            "docs-check: FOO += bar\n"
            "docs-check: export BAR\n"
            "docs-check: undefine BAZ\n"
            "docs-check: docs-check-a docs-check-b | order-only-dep # comment\n"
            "docs-check: docs-check-b docs-check-c\n"
            "docs-check-a:\n"
            "\t@python3 scripts/check_a.py\n"
            "docs-check-b:\n"
            "# make database comment before recipe\n"
            "\t@python3 \\\n"
            "\t  scripts/check_b.py\n"
            "docs-check-c:\n"
            "\t+@python3 scripts/check_c.py\n"
        )
        self.assertEqual(
            makefile_docs_check_prereqs(sample_db),
            ["docs-check-a", "docs-check-b", "docs-check-c"],
        )
        with self.assertRaisesRegex(ValueError, "no docs-check: target"):
            makefile_docs_check_prereqs("docs-check: PYTHONWARNINGS := error\n")
        with self.assertRaisesRegex(ValueError, "ignores recipe errors via `-` prefix"):
            makefile_docs_check_prereqs(
                sample_db.replace("\t@python3 scripts/check_a.py", "\t-@python3 scripts/check_a.py")
            )
        with self.assertRaisesRegex(ValueError, "empty recipe"):
            makefile_docs_check_prereqs(
                sample_db.replace("\t@python3 scripts/check_a.py\n", "")
            )


if __name__ == "__main__":
    unittest.main()
