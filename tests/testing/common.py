"""Common test constants and fixtures shared across test suites."""

import os
import pathlib
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


def get_isolated_test_env(overrides=None, bin_dir=None):
    """Returns a sanitized environment for hermetic script execution, free of CI runner pollution."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GITHUB_", "RUNNER_")) and k not in ("CI", "CONTINUOUS_INTEGRATION", "GH_TOKEN")
    }
    if bin_dir:
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    if overrides:
        env.update(overrides)
    return env


def create_minimal_tools_bin(temp_dir_path, exclude=()):
    """Creates a minimal bin directory with symlinks to essential shell utilities, excluding specified tools."""
    bin_dir = pathlib.Path(temp_dir_path) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    essential_tools = [
        "bash", "sh", "git", "tr", "cut", "sed", "awk", "grep",
        "cat", "dirname", "basename", "echo", "mkdir", "rm",
        "mktemp", "touch", "ls", "head", "tail"
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
