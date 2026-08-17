"""Shared fake test data and constants for installer and upgrade script unit tests.

Centralizes valid and invalid SemVer refs, commit SHAs, help banners, and mock
environment variables used across test_install_script.py, test_upgrade_script.py,
and test_provision_rc_environment.py.
"""

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

# Help banners
INSTALLER_HELP_BANNER = "kube-agents Zero-Friction Installer"
UPGRADER_HELP_BANNER = "Lifecycle Upgrade Engine"

# Shared mock fixtures for RC environment testing
MOCK_GCP_PROJECT_ID = "mock-rc-project"
MOCK_GCP_REGION = "us-central1"
MOCK_GKE_CLUSTER_NAME = "mock-rc-cluster"
MOCK_IMAGE_TAG_SEMVER = "0.1.0"
MOCK_IMAGE_TAG_SHA = "01084e7dc912249e4d1176030e54f62427677ce1"
MOCK_MODEL_PROVIDER = "gemini"
MOCK_MODEL_DEFAULT_NAME = "gemini-2.0-flash"
MOCK_GEMINI_API_KEY = "test-gemini-api-key"
MOCK_PERMISSION_SET = "gke-admin"
MOCK_REGISTRY_PREFIX = "ghcr.io/mock-org"
MOCK_GOOGLE_CHAT_MODE = "debug"
MOCK_CHAT_TOPIC_NAME = "custom-rc-chat-topic"
MOCK_USER_PROFILE_ENABLED = "true"

# Mock invocation signals and file names
MOCK_CALLS_LOG = "calls.log"
MOCK_UNINSTALL_SCRIPT = "uninstall.sh"
MOCK_INSTALL_SCRIPT = "install.sh"
MOCK_UNINSTALL_FAIL_SIGNAL = "uninstall: failed as expected"
MOCK_INSTALL_SUCCESS_SIGNAL = "install: succeeded"
