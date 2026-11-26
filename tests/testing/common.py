"""Common test constants and fixtures shared across test suites."""

import atexit
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

MOCK_DEFAULT_RELEASE_REPO = "gke-labs/kube-agents"
MOCK_DEFAULT_REGISTRY_PREFIX = "ghcr.io/gke-labs/kube-agents"
MOCK_CUSTOM_ORG = "custom-org"
MOCK_CUSTOM_REPO = "custom-repo"
MOCK_CUSTOM_TARGET_REPO = "custom-org/custom-repo"
MOCK_CUSTOM_REGISTRY_PREFIX = "us-docker.pkg.dev/my-proj/my-repo"

TRUTHY_BOOLEAN_INPUTS = [
    "true",
    "True",
    "TRUE",
    "yes",
    "YES",
    "y",
    "1",
    "on",
    "  true  ",
]

FALSY_BOOLEAN_INPUTS = [
    "false",
    "0",
    "no",
    "off",
    "",
    "random",
    "null",
]

# Valid immutable references (pure numeric SemVer X.Y.Z and 40-character commit SHAs)
VALID_IMMUTABLE_REFS = [
    "0.1.0",
    "0.2.0",
    "1.0.0",
    "0.2.3-rc.1",
    "0.2.0-beta.1",
    "05ab1c49768b011fde5ca5a588f809e346911478",
    "dc695ce3fd082d1d3e2008c9c8928a0c7d9efa0d",
]

# Invalid references that must be rejected (v-prefixed SemVer, mutable refs, malformed strings)
INVALID_IMMUTABLE_REFS = [
    "",
    "latest",
    "main",
    "master",
    "HEAD",
    "v0.1.0",
    "v0.2.0",
    "v1.0.0",
    "v0.2.3-rc.1",
    "feature-branch",
    "v1",
    "v1.2",
    "0.1",
    "12345",  # too short for 40-char SHA
    "invalid_semver_tag!",
]

# Supported pure numeric SemVer release tags (X.Y.Z)
VALID_GA_RELEASE_TAGS = [
    "0.1.0",
    "0.2.0",
    "1.0.0",
    "1.2.3",
]

# Mock test hashes and nonexistent references
MOCK_SAMPLE_COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
MOCK_SAMPLE_SHORT_SHA = "abc1234"
MOCK_NONEXISTENT_TAG = "0.9.9"
MOCK_NONEXISTENT_REF = "nonexistent-ref"

# Unsupported GA release tags (v-prefixed, pre-releases, branches, short hashes, malformed strings)
INVALID_GA_RELEASE_TAGS = [
    "v0.1.0",
    "v0.2.0",
    "0.1",
    "main",
    "latest",
    "0.1.0-alpha",
    "0.1.0-rc1",
    "0.2.3-rc.1",
    "release",
    MOCK_SAMPLE_SHORT_SHA,
]

# Shared chat mock mode
MOCK_GOOGLE_CHAT_MODE = "debug"

# Help banners
INSTALLER_HELP_BANNER = "kube-agents Zero-Friction Installer"
UPGRADER_HELP_BANNER = "Lifecycle Upgrade Engine"


# Installer configuration keys that install.sh and installer_common.sh read from
# the environment when seeding PARAM_* defaults. Stripped by default in
# get_isolated_test_env so a developer's shell exports cannot steer hermetic
# unit tests; tests that want a specific value pass it via `overrides`.
SEEDED_INSTALLER_ENV_KEYS = (
    "ACCEPT_NO_NETWORK_POLICY",
    "ENABLE_NETWORK_POLICY",
    "MIGRATE_NODE_POOLS",
    "GOOGLE_CHAT_ENABLED",
    "SLACK_ENABLED",
    "ENABLE_GKE_BACKUP_PLAN",
    "HELM_UPGRADE_INSTALL",
    "CARD_STOP_ENABLED",
    "ENABLE_PUBSUB_PLATFORM",
    "ENABLE_STOCKOUT_INVESTIGATOR",
    "ENABLE_DRIFT_DETECTOR",
    "WEB_CONSOLE_ENABLED",
    "LITELLM_REDACTION_ENABLED",
    "LITELLM_REDACTION_IP_ACTION",
    "LITELLM_REDACTION_IP_ALLOW_CIDRS",
    "LITELLM_REDACTION_RULES",
    "LITELLM_BUDGET_USD",
    "LITELLM_BUDGET_PERIOD",
    "SCOPED_SA_POOL_ENABLED",
)


#: Git in the child reads neither the global nor the system configuration file, only the
#: repository's own. A developer's global rule such as
#: `url.git@github.com:.pushInsteadOf https://github.com/` is applied to a push before a
#: repository's `insteadOf`, so without this a script's push to the test's local stand-in for the
#: release repository goes to GitHub, with the developer's key. Configuration that arrives in the
#: environment (GIT_CONFIG_COUNT) still applies, and git before 2.32 does not know
#: GIT_CONFIG_GLOBAL.
HERMETIC_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


#: The variable that names the report file of each front-door script, and the name of the file a
#: real run writes under /tmp. A script writes its report also when it is only sourced and a
#: command fails, through its ERR trap, so a test's child must not get the real path.
INSTALLER_REPORT_FILE_ENV_VARS = {
    "KUBE_AGENTS_INSTALL_REPORT_FILE": "kube-agents-install-report.json",
    "KUBE_AGENTS_UPGRADE_REPORT_FILE": "kube-agents-upgrade-report.json",
    "KUBE_AGENTS_UNINSTALL_REPORT_FILE": "kube-agents-uninstall-report.json",
}
_INSTALLER_REPORT_DIR_PREFIX = "kube-agents-test-reports-"
# Made at import, so the folder exists and its removal is registered before a test forks a child.
_INSTALLER_REPORT_DIR = tempfile.mkdtemp(prefix=_INSTALLER_REPORT_DIR_PREFIX)
atexit.register(shutil.rmtree, _INSTALLER_REPORT_DIR, ignore_errors=True)
#: The variable that names uninstall.sh's lock file, and the name of the file a real run locks
#: under /tmp. The script takes the lock also when a test only sources it, and a run that finds
#: the lock held exits 1, so a test's child gets a lock file of this test process, in the folder
#: of the report files.
UNINSTALL_LOCK_FILE_ENV = "KUBE_AGENTS_UNINSTALL_LOCK_FILE"
UNINSTALL_LOCK_FILE_NAME = "kube-agents-uninstall.lock"


# For the token renewers in hack/ci-eval-pr.sh (test_ci_eval_ledger_mint,
# test_ci_eval_gitops_write_token). How long the stalled renewal write holds, and how long after the stop the test
# looks again: longer than the stall, so a write the stop left running has landed.
RENEWAL_WRITE_STALL_SECONDS = 0.5
RENEWAL_AFTER_STALL_SECONDS = 1.0
# The renewer's umask, made to hold while STALL exists: its renewal write is then
# under way (STALLED says so) for as long as the test needs to stop it there.
STALLED_RENEWAL_WRITE = """\
umask() {
  if [ -e "${STALL}" ]; then
    : > "${STALLED}"
    command sleep "${WRITE_STALL_SECONDS}"
  fi
  builtin umask "$@"
}
"""

# A renewer tick long enough to tell a stop that ends the nap from one that waits
# it out or leaves it running, and the bound the whole run must finish inside:
# a nap left behind holds the run's output open until the tick ends.
RENEWAL_NAP_TICK_SECONDS = 20
RENEWAL_NAP_STOP_BOUND_SECONDS = 10
# How long the test waits before it stops, so the renewer is in its nap.
RENEWAL_NAP_SETTLE_SECONDS = 0.5
# Static checks for bash 5.2 signal-unsafe constructs inside a TERM-trapped renewer
# subshell and its callees: command/process substitutions and loop jumps.
RENEWAL_SUBSTITUTION_RE = re.compile(r"\$\((?!\()|`|<\(|>\(")
RENEWAL_UNCLOSED_ARITHMETIC_RE = re.compile(r"\$\(\((?!.*\)\))")
RENEWAL_LOOP_JUMP_RE = re.compile(r"\b(?:continue|break)\b")


def find_renewal_trap_offenders(scopes):
    """Returns a list of bash 5.2 signal-unsafe constructs in (scope, text) pairs."""
    offenders = []
    for scope, text in scopes:
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if RENEWAL_SUBSTITUTION_RE.search(line) or RENEWAL_UNCLOSED_ARITHMETIC_RE.search(line):
                offenders.append(f"{scope}:{number} (substitution): {line.strip()}")
            if RENEWAL_LOOP_JUMP_RE.search(line):
                offenders.append(f"{scope}:{number} (loop jump): {line.strip()}")
    return offenders


def installer_report_env():
    """The report file of each front-door script, in one folder of this test process.

    `get_isolated_test_env` sets these. An environment that a test builds from nothing takes
    them from here: `env={"PATH": ..., **installer_report_env()}`. A child of uninstall.sh
    needs `**uninstall_lock_env()` too.
    """
    return {name: os.path.join(_INSTALLER_REPORT_DIR, file) for name, file in INSTALLER_REPORT_FILE_ENV_VARS.items()}


def uninstall_lock_env():
    """uninstall.sh's lock file, in the folder of the report files. `get_isolated_test_env` sets it."""
    return {UNINSTALL_LOCK_FILE_ENV: os.path.join(_INSTALLER_REPORT_DIR, UNINSTALL_LOCK_FILE_NAME)}


# The test process has them too, for a child that inherits its environment (no `env=`).
os.environ.update(installer_report_env())
os.environ.update(uninstall_lock_env())


def get_isolated_test_env(overrides=None, bin_dir=None, absent=()):
    """Returns a sanitized environment for hermetic script execution, free of CI runner pollution.

    `SEEDED_INSTALLER_ENV_KEYS` and `absent` name variables the child must not
    see even when the developer's shell exports them: a test that relies on a
    variable being unset has to be insulated from os.environ. Popped before the
    overrides, so a caller can name a key in `overrides` and the override wins.
    `HERMETIC_GIT_ENV`, `installer_report_env()` and `uninstall_lock_env()` are
    set before the overrides for the same reason.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GITHUB_", "RUNNER_")) and k not in ("CI", "CONTINUOUS_INTEGRATION", "GH_TOKEN")
    }
    for key in SEEDED_INSTALLER_ENV_KEYS:
        env.pop(key, None)
    for key in absent:
        env.pop(key, None)
    env.update(HERMETIC_GIT_ENV)
    env.update(installer_report_env())
    env.update(uninstall_lock_env())
    if bin_dir:
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    if overrides:
        env.update(overrides)
    return env


NOTES_PROBE_DEFINE = "notes-under-test"
NOTES_PROBE_CONFIGMAP = "notes-under-test"
NOTES_PROBE_TEMPLATE = "templates/notes-under-test.yaml"


def chart_with_notes_probe(chart_src, chart_dst):
    """Copies the chart to `chart_dst` with its NOTES.txt captured into a ConfigMap.

    `helm template` renders no notes, so a test that reads them wraps NOTES.txt in a
    define and renders it as the `notes` key of a ConfigMap named NOTES_PROBE_CONFIGMAP
    (`--show-only NOTES_PROBE_TEMPLATE` selects it). Returns the copied chart's path.
    """
    chart_dst = pathlib.Path(chart_dst)
    shutil.copytree(chart_src, chart_dst)
    templates = chart_dst / "templates"
    notes = (templates / "NOTES.txt").read_text()
    (templates / "NOTES.txt").unlink()
    (templates / "_notes_under_test.tpl").write_text(
        '{{- define "' + NOTES_PROBE_DEFINE + '" -}}\n' + notes + "\n{{- end -}}\n"
    )
    (chart_dst / NOTES_PROBE_TEMPLATE).write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: " + NOTES_PROBE_CONFIGMAP + "\n"
        'data:\n  notes: {{ include "' + NOTES_PROBE_DEFINE + '" . | toJson }}\n'
    )
    return chart_dst


def create_minimal_tools_bin(temp_dir_path, exclude=()):
    """Creates a minimal bin directory with symlinks to essential shell utilities, excluding specified tools."""
    bin_dir = pathlib.Path(temp_dir_path) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    essential_tools = [
        "bash", "sh", "git", "tr", "cut", "sed", "awk", "grep",
        "cat", "dirname", "basename", "echo", "mkdir", "rm", "cp", "mv",
        "chmod", "date", "sort", "tee",
        "mktemp", "touch", "ls", "head", "tail", "find", "tar", "gzip",
        "sha256sum", "shasum", "zip"
    ]
    for tool in essential_tools:
        if tool in exclude:
            continue
        tool_path = shutil.which(tool)
        if tool_path:
            symlink = bin_dir / tool
            if not symlink.exists():
                symlink.symlink_to(tool_path)
    return bin_dir


def create_mock_git_repo(temp_dir=None):
    """Initializes an isolated mock Git repository for testing."""
    if temp_dir is None:
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    base_path = pathlib.Path(temp_dir.name if hasattr(temp_dir, "name") else temp_dir)
    repo_dir = base_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    def git_cmd(*args, cwd=repo_dir):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    git_cmd("init", "-b", "main")
    git_cmd("config", "user.name", "Test User")
    git_cmd("config", "user.email", "test@example.com")
    git_cmd("config", "commit.gpgsign", "false")

    init_file = repo_dir / "init.txt"
    init_file.write_text("initial commit\n")
    git_cmd("add", "init.txt")
    git_cmd("commit", "-m", "chore: initial commit")

    return temp_dir, str(repo_dir), git_cmd
