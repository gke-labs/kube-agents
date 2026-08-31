"""Shared fixtures and helpers for live GKE E2E promotion tests."""

import base64
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

try:
    from dotenv import load_dotenv
    _repo_root_env = pathlib.Path(__file__).resolve().parents[2] / ".env"
    if _repo_root_env.is_file():
        load_dotenv(_repo_root_env)
except ImportError:
    pass

try:
    import yaml
except ImportError:
    yaml = None

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "tests" / "e2e" / "e2e_config.yaml"


def pytest_configure(config: pytest.Config) -> None:
    """Configures session environment variables and ensures gke auth plugin settings."""
    if "CLOUDSDK_PYTHON" in os.environ:
        del os.environ["CLOUDSDK_PYTHON"]
    os.environ["CLOUDSDK_PYTHON_SITEPACKAGES"] = "0"
    os.environ["PYTHONNOUSERSITE"] = "1"
    if "USE_GKE_GCLOUD_AUTH_PLUGIN" not in os.environ:
        os.environ["USE_GKE_GCLOUD_AUTH_PLUGIN"] = "True"
    os.environ["CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS"] = "false"


def _parse_yaml_fallback(content: str) -> Dict[str, Any]:
    """Fallback parser for a simple suites list and env_vars in e2e_config.yaml.

    Keys off `- name:` blocks rather than the top-level key, so `suites:` and the
    legacy `environments:` spelling both parse.
    """
    result: Dict[str, Any] = {"defaults": {}, "suites": []}
    current_env: Optional[Dict[str, Any]] = None
    in_env_vars = False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- name:"):
            in_env_vars = False
            if current_env:
                result["suites"].append(current_env)
            name = stripped.split(":", 1)[1].strip().strip('"\'')
            current_env = {"name": name, "tests": [], "env_vars": {}}
        elif current_env and stripped.startswith("- ") and "tests/" in stripped:
            in_env_vars = False
            current_env["tests"].append(stripped.lstrip("- ").strip().strip('"\''))
        elif current_env and stripped.startswith("env_vars:"):
            in_env_vars = True
        elif current_env and in_env_vars and ":" in stripped:
            indent = len(line) - len(line.lstrip())
            if indent >= 4 or line.startswith("    ") or line.startswith("\t"):
                parts = stripped.split(":", 1)
                current_env["env_vars"][parts[0].strip().lstrip("- ")] = parts[1].strip().strip('"\'')
            else:
                in_env_vars = False
                parts = stripped.split(":", 1)
                current_env[parts[0].strip().lstrip("- ")] = parts[1].strip().strip('"\'')
        elif current_env and ":" in stripped:
            in_env_vars = False
            parts = stripped.split(":", 1)
            key = parts[0].strip().lstrip("- ")
            val = parts[1].strip().strip('"\'')
            if key in ("project_id", "cluster_name", "region", "namespace", "description"):
                current_env[key] = val
    if current_env:
        result["suites"].append(current_env)
    return result


def _get_default_config_env() -> Dict[str, Any]:
    """Retrieves and merges defaults and default environment settings defined in e2e_config.yaml."""
    if not _DEFAULT_CONFIG_PATH.is_file():
        return {}
    content = _DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    try:
        cfg = yaml.safe_load(content) if yaml else _parse_yaml_fallback(content)
    except Exception:
        cfg = _parse_yaml_fallback(content)

    defaults = (cfg or {}).get("defaults", {})
    # `suites:`/`E2E_SUITE`/`default_suite` are the names; the `environment`
    # spellings beside each are the pre-rename ones, read for one release so a
    # checkout mid-migration still resolves fixtures. execute_e2e_tests.py exports
    # both variables, so this fallback only matters when pytest is driven directly.
    envs = (cfg or {}).get("suites") or (cfg or {}).get("environments", [])
    default_env_name = (
        os.environ.get("E2E_SUITE")
        or os.environ.get("E2E_ENV")
        or defaults.get("default_suite")
        or defaults.get("default_environment", "investigations")
    )
    default_env = next((e for e in envs if e.get("name") == default_env_name), {})
    return {
        "env_vars": default_env.get("env_vars", {}),
        **defaults,
        **{k: v for k, v in default_env.items() if k != "env_vars"},
    }


@pytest.fixture(scope="session")
def gcp_project_id() -> Optional[str]:
    """Resolves GCP Project ID from environment or e2e_config.yaml."""
    val = os.environ.get("GCP_PROJECT_ID") or os.environ.get("PROJECT_ID")
    if val:
        return val

    cfg = _get_default_config_env()
    return cfg.get("project_id")


@pytest.fixture(scope="session")
def gke_cluster_name() -> Optional[str]:
    """Resolves GKE Cluster Name from environment or e2e_config.yaml."""
    val = os.environ.get("GKE_CLUSTER_NAME") or os.environ.get("CLUSTER_NAME")
    if val:
        return val

    cfg = _get_default_config_env()
    return cfg.get("cluster_name")


@pytest.fixture(scope="session")
def gcp_region() -> str:
    """Resolves GCP Region from environment or e2e_config.yaml."""
    val = os.environ.get("GCP_REGION") or os.environ.get("REGION")
    if val:
        return val

    cfg = _get_default_config_env()
    return cfg.get("region") or "us-central1"


@pytest.fixture(scope="session")
def agent_namespace() -> str:
    """Resolves Kubernetes namespace where platform-agent is deployed."""
    val = os.environ.get("AGENT_NAMESPACE")
    if val:
        return val
    cfg = _get_default_config_env()
    return cfg.get("namespace") or "kubeagents-system"





@pytest.fixture(scope="session")
def fleet_audit_streams() -> str:
    """Resolves FLEET_AUDIT_STREAMS filter ('all' or specific stream name like 'stockout-prevention')."""
    val = os.environ.get("FLEET_AUDIT_STREAMS")
    if val:
        return str(val).strip().lower()
    cfg = _get_default_config_env()
    env_vars = cfg.get("env_vars", {})
    return str(env_vars.get("FLEET_AUDIT_STREAMS") or "all").strip().lower()


def _clean(value: Optional[str]) -> Optional[str]:
    """Trims surrounding whitespace, returning None when no name is left."""
    text = str(value).strip() if value else ""
    return text or None


def _clean_owner(value: Optional[str]) -> Optional[str]:
    """_clean for an owner, which carries no slash of its own.

    Kept separate from _clean because stripping slashes off a *repository* would turn a
    half-written 'owner/' into the bare name 'owner' and hide the missing half.
    """
    owner = _clean(value)
    return _clean(owner.strip("/")) if owner else None


def _resolve_github_org() -> Optional[str]:
    """Resolves the GitHub owner from the environment or e2e_config.yaml.

    Deliberately does not consult GITHUB_REPO, unlike the github_org fixture:
    _qualify_repo calls this while it is still deciding what github_repo resolves to.
    """
    return _clean_owner(os.environ.get("GITHUB_ORG")) or _clean_owner(
        _get_default_config_env().get("env_vars", {}).get("GITHUB_ORG")
    )


def _qualify_repo(repo: Optional[str]) -> Optional[str]:
    """Prefixes a bare repository name with the owner.

    GH_REPO is bare by repository convention -- reusable-deploy-integrations.yml passes
    the org and the repo to the GitHub Token Minter as separate values -- while every
    consumer of this fixture wants 'owner/repo': test_github_target_repository_configuration
    asserts the shape, and github_token_refresh.py rejects anything else.

    The owner comes from GITHUB_ORG or, failing that, e2e_config.yaml. Most environments
    hard-code it there, and a bare name is composed against that default whenever nothing
    more specific is set. `rc` and `nightly` are the exceptions: they set neither,
    because the same pair scopes the token minter at install time and a value written in
    two places drifts, so e2e-run.yml passes the bound environment's GITOPS_ORG and
    GITOPS_REPO instead. With those unset there is no owner to compose against and a bare
    name is returned unqualified, which github_repo's caller then reports as a structure
    failure rather than resolving to the wrong repository.

    Only a value with no slash in it is composed. Anything else is returned as the
    caller gave it, trimmed of surrounding whitespace: 'owner/' and '/repo' stay as they
    are and fail the caller's structure check naming the value, rather than being
    reshaped into a repository that parses and does not exist. Whitespace alone
    resolves to None for the same reason -- 'org/  ' passes both halves of that check.
    """
    repo = _clean(repo)
    if not repo or "/" in repo:
        return repo
    org = _resolve_github_org()
    return f"{org}/{repo}" if org else repo


@pytest.fixture(scope="session")
def github_repo(agent_namespace: str) -> Optional[str]:
    """Resolves the registered GitOps/Audit repository (owner/repo)."""
    val = os.environ.get("GITHUB_REPO") or os.environ.get("GITOPS_REPO")
    if val:
        return _qualify_repo(val)

    # Try reading from platform-agent-settings in the cluster
    try:
        cmd = [
            "kubectl", "get", "cm", "platform-agent-settings",
            "-n", agent_namespace,
            "-o", "jsonpath={.data.SETTINGS\\.md}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "Git Repo:" in line:
                    repo_cand = line.split("Git Repo:", 1)[1].strip().strip("*` ")
                    if repo_cand and repo_cand.lower() != "none":
                        return _qualify_repo(repo_cand)
    except Exception:
        pass

    cfg = _get_default_config_env()
    env_vars = cfg.get("env_vars", {})
    return _qualify_repo(env_vars.get("GITHUB_REPO") or env_vars.get("GITOPS_REPO"))


@pytest.fixture(scope="session")
def github_org(github_repo: Optional[str]) -> Optional[str]:
    """Resolves the GitHub Organization name.

    Every branch is normalised the same way github_repo's owner is, so that
    GITHUB_ORG=' myorg' and GITHUB_REPO='myorg/repo' agree. Left un-normalised, the
    two disagree on the whitespace and test_github_target_repository_configuration
    fails on an owner mismatch instead of on the value that caused it.
    """
    val = _clean_owner(os.environ.get("GITHUB_ORG"))
    if val:
        return val
    if github_repo and "/" in github_repo:
        return _clean_owner(github_repo.split("/", 1)[0])
    cfg = _get_default_config_env()
    env_vars = cfg.get("env_vars", {})
    return _clean_owner(env_vars.get("GITHUB_ORG"))


@pytest.fixture(scope="session")
def github_app_id() -> Optional[str]:
    """Resolves the GitHub App ID."""
    val = os.environ.get("GITHUB_APP_ID")
    if val:
        return val
    cfg = _get_default_config_env()
    env_vars = cfg.get("env_vars", {})
    return env_vars.get("GITHUB_APP_ID")


@pytest.fixture(scope="session")
def github_installation_id() -> Optional[str]:
    """Resolves the GitHub App Installation ID."""
    val = os.environ.get("GITHUB_INSTALLATION_ID")
    if val:
        return val
    cfg = _get_default_config_env()
    env_vars = cfg.get("env_vars", {})
    return env_vars.get("GITHUB_INSTALLATION_ID")


@pytest.fixture(scope="session", autouse=True)
def ensure_cluster_credentials(
    gcp_project_id: Optional[str],
    gke_cluster_name: Optional[str],
    gcp_region: str,
) -> None:
    """Configures kubectl context for the target GKE cluster."""
    if gcp_project_id and gke_cluster_name and gcp_region:
        expected_ctx = f"gke_{gcp_project_id}_{gcp_region}_{gke_cluster_name}"
        ctx_res = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True)
        current_ctx = ctx_res.stdout.strip() if ctx_res.returncode == 0 else ""

        check_res = subprocess.run(["kubectl", "cluster-info"], capture_output=True, text=True)
        if check_res.returncode == 0 and (current_ctx == expected_ctx or (gke_cluster_name in current_ctx and gcp_project_id in current_ctx)):
            return

        subprocess.run(
            ["gcloud", "config", "set", "container/use_application_default_credentials", "false", "--quiet"],
            capture_output=True,
        )
        res = subprocess.run(
            [
                "gcloud", "container", "clusters", "get-credentials",
                gke_cluster_name,
                f"--region={gcp_region}",
                f"--project={gcp_project_id}",
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            pytest.fail(f"Failed to get-credentials for cluster '{gke_cluster_name}': {res.stderr}")



@pytest.fixture(scope="session")
def platform_agent_api_key(agent_namespace: str) -> Optional[str]:
    """Retrieves API_SERVER_KEY from platform-agent-secrets in the cluster.

    Returns None when there is no key to be had. It used to fall back to the
    literal "cluster-internal-trusted", which is `loopbackAgentAPIKey` in
    k8s-operator/internal/controller/platformagent_manifests.go — the in-pod
    sentinel the agent container accepts on loopback, and deliberately *not*
    the Secret the credential proxy checks. Substituting it turned a failed
    Secret read into a suite that authenticated against a different listener
    than the one under test and reported the result as the external path's.
    """
    try:
        cmd = [
            "kubectl", "get", "secret", "platform-agent-secrets",
            "-n", agent_namespace,
            "-o", "jsonpath={.data.API_SERVER_KEY}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        raw_key = result.stdout.strip()
        if raw_key:
            return base64.b64decode(raw_key).decode("utf-8")
    except Exception:
        pass
    return os.environ.get("PLATFORM_AGENT_TOKEN") or os.environ.get("API_SERVER_KEY") or None


# ---------------------------------------------------------------------------
# Reaching the Platform Agent API from the test runner
#
# The transport is a `kubectl exec` relay, not `kubectl port-forward`, because
# the RC environment runs the agent pod on a GKE Sandbox (gVisor) node pool
# (`ENABLE_GVISOR=true`, against install.sh's `false` default) and port-forward
# cannot see a listener inside a sandbox. scripts/exec_tunnel.py is canonical on
# that constraint and owns the relay; it is shared with
# scripts/hermes-dashboard-tunnel.py rather than written twice.
#
# It is used unconditionally rather than behind a gVisor detector: it works on
# both kinds of node pool, and a detector's wrong answer is the failure being
# fixed here -- guess "ordinary pool" on a sandboxed cluster and the suite is
# back to an unexplained reset.
#
# Which port, and which key, verified against the operator source rather than
# against another document:
#
#   * The Service the operator creates for the agent (buildPlatformService in
#     k8s-operator/internal/controller/platformagent_manifests.go) publishes
#     `api` on 8642 with targetPort 8643. 8643 is the credential-proxy sidecar's
#     listener: it checks `Authorization: Bearer $API_SERVER_EXTERNAL_KEY` and
#     re-signs the request to Hermes on the pod's 127.0.0.1:8642 with a
#     different bearer.
#   * API_SERVER_EXTERNAL_KEY comes from the API_SERVER_KEY entry of the
#     platform-agent-secrets Secret, which is what platform_agent_api_key reads.
#     Port 8643 plus that key is the pair an external caller presents, so it is
#     the pair this fixture targets.
#   * The agent container's own API_SERVER_KEY is a different value on purpose:
#     `loopbackAgentAPIKey`, a non-secret in-pod sentinel. Relaying to
#     127.0.0.1:8642 would reach Hermes directly, where the Secret's key is not
#     accepted -- a path no external caller uses. That is the diagnostic below,
#     never the transport.
#
# What this cannot cover is the Service VIP and endpoint hop: entering the
# sandbox puts us on the pod's own loopback, past kube-proxy. Dialling the
# Service from inside the pod is not an alternative either, because
# buildNetworkPolicy grants the gateway no egress to 8642 or 8643 anywhere, so
# the hairpin would be dropped on a healthy cluster. Read that policy rather
# than trusting this sentence -- it is rewritten often.
# ---------------------------------------------------------------------------

# scripts/ is not a package, so the relay is imported off the repo root already
# resolved at the top of this file. Appended rather than inserted: this runs at
# import time and stays for the session, and prepending would let a stray
# scripts/*.py shadow a stdlib module for every other import in the suite.
sys.path.append(str(_REPO_ROOT / "scripts"))
from exec_tunnel import TunnelConfig, serve_background  # noqa: E402

# The PlatformAgent CR name, which is also the Service name and the prefix of
# the `<name>-gateway` Deployment and its `app` label. Same env var
# bench/kube_agents_bench/harness.py reads for the same value.
_AGENT_NAME = os.environ.get("AGENT_SERVICE_NAME", "platform-agent")

_PROXY_CONTAINER = "envoy-credential-proxy"
_AGENT_CONTAINER = "platform-agent"

# The interpreter the relay runs inside _PROXY_CONTAINER. Named here rather than
# inherited from TunnelConfig's default, whose help text describes it as the
# dashboard's: this is a real coupling to the image, and it holds because the
# credential-proxy stage derives from proxy-tools -> agent-base
# (deploy/docker/Dockerfile) and start-services.sh launches credential_proxy.py
# with this same path.
_PROXY_PYTHON = "/opt/hermes/.venv/bin/python3"

# The credential-proxy sidecar's authenticated listener, and the Service's
# targetPort for `api`.
_PROXY_API_PORT = 8643
_HERMES_API_PORT = 8642

# Read-only, and the endpoint the operator's own readiness probe asks for
# (agentAPIProbe in platformagent_manifests.go), so a pass here means what a
# pass there means.
_AGENT_API_PROBE_PATH = "/api/sessions?limit=1"


def _numeric_env(name: str, default: str, cast: Any) -> Any:
    """Read a numeric env var, naming it in the error rather than the value.

    Matches the helper in bench/kube_agents_bench/harness.py, including the case
    its regression test names: a variable that is present but empty, which a
    bare `float(os.environ.get(...))` turns into an unattributed ValueError.
    """
    raw = os.environ.get(name) or default
    try:
        return cast(raw)
    except ValueError:
        raise ValueError(f"{name} must be numeric, got {raw!r}") from None


def _probe_agent_api(port: int, api_key: str, timeout: float) -> Tuple[Optional[int], str]:
    """Asks whatever is on the far end for one HTTP response.

    Returns (status, detail), with status None when no HTTP response arrived at
    all. That distinction is the point: a plain TCP connect to the local end of
    a tunnel succeeds whether or not the far end exists, so it cannot tell a
    working transport from a broken one.
    """
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{_AGENT_API_PROBE_PATH}",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    # ProxyHandler({}) rather than urlopen: urlopen reads http_proxy from the
    # environment, and a runner that sets one would send this loopback probe to
    # the proxy and report its answer as the agent's.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read()
            return response.status, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # An HTTP status is a success for this probe: something on the far end
        # spoke HTTP. Whether it is the right something is the caller's call.
        exc.read()
        return exc.code, f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:  # noqa: BLE001 - any transport failure is one answer
        return None, f"{type(exc).__name__}: {exc}"


def _read_log_tail(log: Any, limit: int = 800) -> str:
    """Returns the tail of a captured kubectl log, flattened onto one line.

    Re-opened by path rather than rewound: subprocess dups this handle's
    descriptor into each child, so the two share one file offset and a seek(0)
    here would send the next write to the top of the file.

    Repeats are collapsed with a count. One connection is one exec, and the
    probe retries for its whole budget, so a single persistent error otherwise
    fills the tail with twenty copies of itself and pushes out everything that
    was said only once.
    """
    try:
        with open(log.name, "r", errors="replace") as handle:
            text = handle.read()
    except Exception:  # noqa: BLE001 - diagnostics must not raise
        return "(kubectl output unavailable)"

    seen: List[str] = []
    counts: Dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line not in counts:
            seen.append(line)
            counts[line] = 0
        counts[line] += 1
    if not seen:
        return "(kubectl printed nothing)"
    rendered = " | ".join(
        line if counts[line] == 1 else f"{line} (x{counts[line]})" for line in seen
    )
    return rendered[-limit:]


def _discard_log(log: Any) -> None:
    """Closes and removes a capture file, whatever state it is in."""
    try:
        log.close()
    except Exception:  # noqa: BLE001 - cleanup must not mask a real failure
        pass
    try:
        os.unlink(log.name)
    except OSError:
        pass


def _gateway_pod_selector(namespace: str, note: Callable[[str], None]) -> str:
    """The label selector the agent Service itself uses, or the plain fallback.

    Not a hardcoded `app=<name>-gateway`. Above one replica the operator adds
    `kubeagents.io/is-leader: "true"` to that Service's selector
    (buildPlatformService), because only the leader runs `hermes gateway run` --
    a standby still answers on 8643, where the credential proxy's API listener
    is up, and the proxy then 502s trying to reach a Hermes that was never
    started. Reading the selector off the Service means this transport lands
    where a real caller lands, at any replica count, without restating the
    operator's leader logic here.

    Every fallback is announced through `note`. A silent one sends the reader
    the wrong way: the failure downstream reads "no Running pod matches -l
    app=<name>-gateway", which blames the pods when what actually failed was
    reading the Service -- wrong namespace, missing RBAC, or a Service the
    operator has not created yet.
    """
    fallback = f"app={_AGENT_NAME}-gateway"
    try:
        res = subprocess.run(
            ["kubectl", "get", "service", _AGENT_NAME, "-n", namespace, "-o", "json"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - the fallback is the documented shape
        note(f"could not run kubectl to read svc/{_AGENT_NAME}: {type(exc).__name__}: {exc}; "
             f"falling back to {fallback}")
        return fallback
    if res.returncode != 0:
        note(f"reading svc/{_AGENT_NAME} failed rc={res.returncode} "
             f"({res.stderr.strip() or 'no stderr'}); falling back to {fallback}")
        return fallback
    try:
        selector = json.loads(res.stdout).get("spec", {}).get("selector") or {}
    except ValueError as exc:
        note(f"svc/{_AGENT_NAME} returned unparseable JSON: {exc}; falling back to {fallback}")
        return fallback
    if not selector:
        note(f"svc/{_AGENT_NAME} has no selector; falling back to {fallback}")
        return fallback
    # Naive joining is safe: a Kubernetes label key or value cannot contain
    # a comma or an equals sign.
    return ",".join(f"{key}={value}" for key, value in sorted(selector.items()))


def _diagnose_hermes(namespace: str) -> str:
    """Separates "the agent is down" from "the credential proxy is broken".

    Runs the operator's own readiness probe on demand: curl against Hermes on
    pod loopback bearing the agent container's `$API_SERVER_KEY`, read inside
    the pod and never printed, so no copy of the sentinel value lives here to
    drift from platformagent_manifests.go.

    `deployment/<name>-gateway` selects any Ready pod rather than the leader.
    Above one replica that may be a standby, which never binds 8642 at all --
    agentAPIProbe passes it anyway, tolerating curl's exit 7 when
    `$ENABLE_LEADER_ELECTION` is `"true"` -- so read a failure here as "this pod
    is not serving" rather than "the agent is down".
    """
    curl = (
        'curl --silent --show-error --max-time 5 -o /dev/null -w "%{http_code}" '
        '-H "Authorization: Bearer $API_SERVER_KEY" '
        f"http://127.0.0.1:{_HERMES_API_PORT}{_AGENT_API_PROBE_PATH}"
    )
    try:
        res = subprocess.run(
            ["kubectl", "exec", "-n", namespace, f"deployment/{_AGENT_NAME}-gateway",
             "-c", _AGENT_CONTAINER, "--", "sh", "-c", curl],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not raise over the real failure
        return f"DIAGNOSE Hermes on pod loopback: kubectl exec failed: {type(exc).__name__}: {exc}"

    if res.returncode != 0:
        return (
            f"DIAGNOSE Hermes on pod loopback: curl rc={res.returncode} "
            f"({res.stderr.strip() or 'no stderr'}). This pod is not serving "
            f"{_AGENT_API_PROBE_PATH}, so the fault is at or below Hermes."
        )

    # There is no --fail on that curl, so a status came back and the branch is
    # decided on the status rather than on the exit code. 4xx here is not the
    # proxy's problem: it means the agent container's own $API_SERVER_KEY is not
    # what its Hermes accepts, which is issue #786's shape.
    code = res.stdout.strip()
    if code.startswith("4"):
        return (
            f"DIAGNOSE Hermes on pod loopback: HTTP {code}. Hermes is serving but rejects the "
            f"agent container's own $API_SERVER_KEY, so the two ends of the in-pod sentinel "
            f"disagree -- the credential proxy is downstream of that and cannot succeed either."
        )
    if not code.startswith("2"):
        return (
            f"DIAGNOSE Hermes on pod loopback: HTTP {code}. Hermes answered but not with success, "
            f"so the fault is at or below Hermes rather than in the credential proxy."
        )
    return (
        f"DIAGNOSE Hermes on pod loopback: HTTP {code}. Hermes is serving, so the fault is the "
        f"credential-proxy listener on {_PROXY_API_PORT} or the key it holds -- compare "
        f"`kubectl exec -c {_PROXY_CONTAINER} -- printenv API_SERVER_EXTERNAL_KEY` against the "
        f"platform-agent-secrets API_SERVER_KEY, which a pod that outlived a Secret rewrite "
        f"will not match."
    )


@pytest.fixture(scope="session")
def port_forward_agent(
    agent_namespace: str, platform_agent_api_key: Optional[str]
) -> Generator[str, None, None]:
    """Yields a local URL that reaches the agent API, or fails saying why.

    Never yields None -- every path that cannot produce a working URL fails the
    setup instead, which is why callers need no guard on the value.

    The name is historical: the transport is an exec relay, not a port-forward.
    """
    if not platform_agent_api_key:
        pytest.fail(
            "Platform Agent API key not found: platform-agent-secrets has no readable "
            "API_SERVER_KEY, and neither PLATFORM_AGENT_TOKEN nor API_SERVER_KEY is set."
        )

    # 0 takes an ephemeral port. A fixed one turns a leftover listener from an
    # earlier run into "address already in use", which reads like a broken agent.
    local_port = _numeric_env("AGENT_LOCAL_PORT", "0", int)
    budget = _numeric_env("AGENT_TUNNEL_TIMEOUT", "30", float)

    # kubectl's stderr, which is where a failed exec explains itself. A file
    # rather than a pipe: nothing drains it for the life of the fixture, and a
    # full pipe buffer would block the relay mid-session.
    log = tempfile.NamedTemporaryFile(
        mode="w", prefix="agent-exec-tunnel-", suffix=".log", delete=False
    )
    # Built before the selector is resolved, because resolving it can have
    # something to say.
    diary: List[str] = [f"Platform Agent API transport, namespace {agent_namespace}:"]
    selector = _gateway_pod_selector(
        agent_namespace, lambda message: diary.append(f"  {message}")
    )
    target = (
        f"exec relay -> {_PROXY_CONTAINER}:{_PROXY_API_PORT} "
        f"in a pod matching {selector}"
    )

    server = None
    try:
        server = serve_background(
            TunnelConfig(
                namespace=agent_namespace,
                selector=selector,
                container=_PROXY_CONTAINER,
                remote_port=_PROXY_API_PORT,
                python=_PROXY_PYTHON,
                ready_timeout=budget,
                stderr=log,
                log=lambda message: diary.append(f"  {message.strip()}"),
            ),
            local_port=local_port,
        )
        port = server.server_address[1]

        # 5xx is retried, everything else is final. The credential proxy answers
        # 502 whenever its upstream dial fails (credential_proxy.py, the
        # BAD_GATEWAY branch of AgentAPIProxyHandler._proxy), and its container
        # is Ready long before Hermes binds 8642: the sidecar's readiness probe
        # curls /healthz on 8765, which CredentialProxyHandler answers
        # unconditionally and which depends on nothing Hermes does, while the
        # agent's own startup probe sanctions a cold boot of several minutes.
        # Treating that first 502 as the verdict hard-fails the suite against an
        # agent that is merely still booting, with none of the budget spent.
        #
        # Each probe gets whatever is left of the budget rather than a fixed
        # slice, so the client's deadline and the relay's ready_timeout are the
        # same instant. A probe timeout shorter than ready_timeout is worse than
        # useless: it abandons a handshake the relay is still waiting on, so an
        # exec slower than the slice can never succeed however many times it is
        # retried, and each abandoned attempt strands a `kubectl exec` for the
        # rest of the session. A 502 costs nothing here -- it comes back as fast
        # as the upstream dial fails, leaving the rest of the budget to retry.
        deadline = time.time() + budget
        status, detail = None, "no probe completed"
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            status, detail = _probe_agent_api(port, platform_agent_api_key, remaining)
            if status is not None and status < 500:
                break
            time.sleep(0.5)

        if status is None or not 200 <= status < 300:
            diary.append(f"  REJECT {target} -- probe {_AGENT_API_PROBE_PATH} got {detail}")
            diary.append(f"  kubectl said: {_read_log_tail(log)}")
            diary.append("  " + _diagnose_hermes(agent_namespace))
            pytest.fail(
                "The Platform Agent API did not answer over the exec relay.\n" + "\n".join(diary)
            )

        diary.append(f"  USED   {target} -- probe {_AGENT_API_PROBE_PATH} answered {detail}")
        print("\n".join(diary))
        yield f"http://127.0.0.1:{port}"
    finally:
        if server is not None:
            server.shutdown()
            # Handlers are daemon threads, so shutdown() does not join them and
            # an abandoned probe would leave its `kubectl exec` running after
            # the suite reported. close_relays() ends those sessions before the
            # log they write to is unlinked.
            server.close_relays()
            server.server_close()
        _discard_log(log)
