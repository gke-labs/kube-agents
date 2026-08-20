"""Release pipeline specific test constants and fixtures."""

MOCK_REQUIRED_RELEASE_IMAGES = [
    "k8s-operator",
    "platform-agent",
    "credential-proxy",
    "replay-proxy",
]

MOCK_INITIAL_VERSION = "0.1.0"
MOCK_BASE_TAG_PRE_1_0 = "0.1.4"
MOCK_BASE_TAG_1_X = "1.2.3"
MOCK_RC_VALIDATED_TAG = "rc_0.2.0_validated"
MOCK_TARGET_RELEASE_TAG = "0.2.0"
MOCK_COLLIDING_RELEASE_TAG = "0.1.9"

MOCK_EMERGENCY_OVERRIDE_REASON = "INCIDENT_NUMBER critical security hotfix"

MOCK_COMMIT_MSG_FEAT = "feat(agent): add multi-cluster discovery"
MOCK_COMMIT_MSG_FIX = "fix(installer): resolve port conflict"
MOCK_COMMIT_MSG_DOCS = "docs: update installation instructions"
MOCK_COMMIT_MSG_BREAKING_PRE_1_0 = "feat(operator)!: break CRD schema format"
MOCK_COMMIT_MSG_BREAKING_1_X = "feat!: remove deprecated v1alpha1 APIs"
MOCK_COMMIT_MSG_BREAKING_BODY = "refactor: overhaul config format\n\nBREAKING CHANGE: old yaml spec is deprecated"

from tests.testing.common import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_NONEXISTENT_REF,
    MOCK_NONEXISTENT_TAG,
    MOCK_SAMPLE_COMMIT_SHA,
    MOCK_SAMPLE_SHORT_SHA,
    VALID_GA_RELEASE_TAGS,
)

# Shared mock fixtures for RC environment testing (provision_rc_environment.sh)
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
MOCK_CHAT_TOPIC_NAME = "custom-rc-chat-topic"
MOCK_USER_PROFILE_ENABLED = "true"

# Mock invocation signals and file names
MOCK_CALLS_LOG = "calls.log"
MOCK_UNINSTALL_SCRIPT = "uninstall.sh"
MOCK_INSTALL_SCRIPT = "install.sh"
MOCK_UNINSTALL_FAIL_SIGNAL = "uninstall: failed as expected"
MOCK_INSTALL_SUCCESS_SIGNAL = "install: succeeded"

