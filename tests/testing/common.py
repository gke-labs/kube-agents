"""Common test constants and fixtures shared across test suites."""

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
    import os

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GITHUB_", "RUNNER_")) and k not in ("CI", "CONTINUOUS_INTEGRATION")
    }
    if bin_dir:
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    if overrides:
        env.update(overrides)
    return env
