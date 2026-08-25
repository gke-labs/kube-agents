"""Release pipeline specific test constants and fixtures."""

import pathlib

from tests.testing.common import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_NONEXISTENT_REF,
    MOCK_NONEXISTENT_TAG,
    MOCK_SAMPLE_COMMIT_SHA,
    MOCK_SAMPLE_SHORT_SHA,
    VALID_GA_RELEASE_TAGS,
)

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
MOCK_TARGET_RELEASE_VERSION = "0.2.0"
MOCK_TARGET_RELEASE_TAG = "0.2.0"
MOCK_EXPLICIT_RELEASE_VERSION_NEXT = "0.3.0"
MOCK_DOWNGRADE_RELEASE_VERSION = "0.1.0"
MOCK_COLLIDING_RELEASE_TAG = "0.1.9"

MOCK_EMERGENCY_OVERRIDE_REASON = "INCIDENT_NUMBER critical security hotfix"

MOCK_COMMIT_MSG_FEAT = "feat(agent): add multi-cluster discovery"
MOCK_COMMIT_MSG_FIX = "fix(installer): resolve port conflict"
MOCK_COMMIT_MSG_DOCS = "docs: update installation instructions"
MOCK_COMMIT_MSG_BREAKING_PRE_1_0 = "feat(operator)!: break CRD schema format"
MOCK_COMMIT_MSG_BREAKING_1_X = "feat!: remove deprecated v1alpha1 APIs"
MOCK_COMMIT_MSG_BREAKING_BODY = "refactor: overhaul config format\n\nBREAKING CHANGE: old yaml spec is deprecated"

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
MOCK_GH_TOKEN = "mock-gh-token"
MOCK_GH_USER = "mock-github-actor"

# Mock invocation signals and file names
MOCK_CALLS_LOG = "calls.log"
MOCK_UNINSTALL_SCRIPT = "uninstall.sh"
MOCK_INSTALL_SCRIPT = "install.sh"
MOCK_UNINSTALL_FAIL_SIGNAL = "uninstall: failed as expected"
MOCK_INSTALL_SUCCESS_SIGNAL = "install: succeeded"


def create_mock_docker_binary(bin_dir, log_file=None, existing_images=(), image_digests=None):
    """Creates a mock docker CLI supporting buildx imagetools and manifest inspect."""
    bin_path = pathlib.Path(bin_dir)
    bin_path.mkdir(parents=True, exist_ok=True)
    docker_path = bin_path / "docker"
    log_path = log_file if log_file else (bin_path / "docker.log")

    digests_map = {}
    if isinstance(existing_images, dict):
        digests_map.update(existing_images)
    elif isinstance(existing_images, (list, tuple, set)):
        for img in existing_images:
            digests_map[img] = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    if image_digests:
        digests_map.update(image_digests)

    manifest_checks = ""
    imagetools_checks = ""
    for img, dig in digests_map.items():
        manifest_checks += f'  if [ "$3" = "{img}" ]; then exit 0; fi\n'
        if isinstance(dig, dict):
            fmt_val = dig.get("format", "")
            raw_val = dig.get("raw", "")
        else:
            fmt_val = dig
            raw_val = f'{{"mediaType":"application/vnd.oci.image.index.v1+json","digest":"{dig}","manifests":[{{"digest":"{dig}"}}]}}'
        imagetools_checks += f"""    if [ "$target_arg" = "{img}" ]; then
      if [ "$is_format" = "true" ]; then
        echo "{fmt_val}"
        exit 0
      fi
      if [ "$is_raw" = "true" ]; then
        printf '%s\\n' '{raw_val}'
        exit 0
      fi
      echo "Name: {img}"
      echo "Digest: {fmt_val}"
      exit 0
    fi
"""

    content = f"""#!/bin/sh
echo "mock docker: $@" >> "{log_path}"
if [ "$1" = "manifest" ] && [ "$2" = "inspect" ]; then
{manifest_checks}  exit 1
fi
if [ "$1" = "buildx" ] && [ "$2" = "imagetools" ]; then
  if [ "$3" = "inspect" ]; then
    is_format="false"
    is_raw="false"
    target_arg=""
    prev=""
    for arg in "$@"; do
      if [ "$arg" = "--raw" ]; then
        is_raw="true"
      elif [ "$prev" = "--format" ]; then
        is_format="true"
      elif [ "$arg" != "buildx" ] && [ "$arg" != "imagetools" ] && [ "$arg" != "inspect" ] && [ "$arg" != "--format" ] && [ "$arg" != "--raw" ]; then
        target_arg="$arg"
      fi
      prev="$arg"
    done
{imagetools_checks}    echo "ERROR: image not found: $target_arg" >&2
    exit 1
  fi
  if [ "$3" = "create" ]; then
    exit 0
  fi
  exit 0
fi
exit 0
"""
    docker_path.write_text(content)
    docker_path.chmod(0o755)
    return docker_path, log_path


def create_mock_cosign_binary(bin_dir, log_file=None, fail_sign=False):
    """Creates a mock cosign CLI supporting sign commands."""
    bin_path = pathlib.Path(bin_dir)
    bin_path.mkdir(parents=True, exist_ok=True)
    cosign_path = bin_path / "cosign"
    log_path = log_file if log_file else (bin_path / "cosign.log")
    exit_code = 1 if fail_sign else 0
    content = f"""#!/bin/sh
echo "mock cosign: $@" >> "{log_path}"
exit {exit_code}
"""
    cosign_path.write_text(content)
    cosign_path.chmod(0o755)
    return cosign_path, log_path


def create_mock_gh_binary(bin_dir, log_file=None, existing_releases=()):
    """Creates a mock gh CLI supporting release view and create commands."""
    bin_path = pathlib.Path(bin_dir)
    bin_path.mkdir(parents=True, exist_ok=True)
    gh_path = bin_path / "gh"
    log_path = log_file if log_file else (bin_path / "gh.log")
    existing_check = ""
    for rel in existing_releases:
        existing_check += f'if [ "$3" = "{rel}" ]; then exit 0; fi\n'

    content = f"""#!/bin/sh
echo "mock gh: $@" >> "{log_path}"
if [ "$1" = "release" ] && [ "$2" = "view" ]; then
  {existing_check}
  exit 1
fi
if [ "$1" = "release" ] && [ "$2" = "create" ]; then
  exit 0
fi
exit 1
"""
    gh_path.write_text(content)
    gh_path.chmod(0o755)
    return gh_path, log_path


def create_mock_helm_binary(bin_dir, log_file=None, fail_lint=False, fail_package=False, fail_push=False):
    """Creates a mock helm CLI supporting lint, package, and push commands."""
    bin_path = pathlib.Path(bin_dir)
    bin_path.mkdir(parents=True, exist_ok=True)
    helm_path = bin_path / "helm"
    log_path = log_file if log_file else (bin_path / "helm.log")

    lint_exit = "exit 1" if fail_lint else "exit 0"
    package_exit = "exit 1" if fail_package else "exit 0"
    push_exit = "exit 1" if fail_push else "exit 0"

    content = f"""#!/bin/sh
echo "mock helm: $@" >> "{log_path}"
if [ "$1" = "lint" ]; then
  {lint_exit}
fi
if [ "$1" = "package" ]; then
  if [ "{fail_package}" = "True" ]; then
    exit 1
  fi
  dest=""
  ver=""
  prev=""
  for arg in "$@"; do
    if [ "$prev" = "--destination" ]; then
      dest="$arg"
    elif [ "$prev" = "--version" ]; then
      ver="$arg"
    fi
    prev="$arg"
  done
  if [ -n "$dest" ] && [ -n "$ver" ]; then
    touch "$dest/kube-agents-${{ver}}.tgz"
  fi
  {package_exit}
fi
if [ "$1" = "push" ]; then
  if [ "{fail_push}" = "True" ]; then
    echo "mock push error" >&2
    exit 1
  fi
  echo "Pushed: $2 to $3"
  echo "Digest: sha256:1111111111111111111111111111111111111111111111111111111111111111"
  {push_exit}
fi
exit 0
"""
    helm_path.write_text(content)
    helm_path.chmod(0o755)
    return helm_path, log_path


def create_mock_git_binary(
    bin_dir,
    log_file=None,
    resolved_commit=None,
    fail_rev_parse=False,
    fail_archive=False,
):
    """Creates a mock git CLI supporting rev-parse and archive commands."""
    bin_path = pathlib.Path(bin_dir)
    bin_path.mkdir(parents=True, exist_ok=True)
    git_path = bin_path / "git"
    log_path = log_file if log_file else (bin_path / "git.log")

    commit_sha = resolved_commit if resolved_commit else MOCK_SAMPLE_COMMIT_SHA
    rev_parse_action = "exit 1" if fail_rev_parse else f'echo "{commit_sha}"\n  exit 0'
    archive_exit = "exit 1" if fail_archive else "exit 0"

    content = f"""#!/bin/sh
echo "mock git: $@" >> "{log_path}"
if [ "$1" = "rev-parse" ]; then
  {rev_parse_action}
fi
if [ "$1" = "archive" ]; then
  {archive_exit}
fi
exit 0
"""
    git_path.write_text(content)
    git_path.chmod(0o755)
    return git_path, log_path


