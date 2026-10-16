"""Offline tests for the gitops-fix-cycle stack's pinned-base guards.

bench/tf/prebuilt/gitops-fix-cycle/scripts/run-branch.sh and
agent-base-branch.sh write to a GitHub repository and to the PlatformAgent on a
shared install. Their refusal and cleanup paths run for the first time in a
live campaign run otherwise, and a regression there moves a default branch it
should not, or removes an administrator's base. Each test runs the real script
with `curl` or `kubectl` replaced by a stub on PATH that answers from a JSON
state file and logs every call, then asserts on the exit status, the calls
made, and the state left behind. run-gitops-pilot.sh is run from its TASK/CASE
resolution and its leaked-pin and pinned-base refusals, through its agent state reset, to its
base-mode decision (step 2), and stops at the token read that follows; its post-run pull-request listing is
gitops-run-prs.sh, run here with `gh` stubbed. The three scripts read the
repository through gitops_repo.py, which these tests run through them. The
stack's plan-time preconditions on the run branch are evaluated over its
variables. The wrapper's post-teardown leak check is not covered: reaching it
means stubbing the whole run.
"""

import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_STACK_SCRIPTS = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "gitops-fix-cycle" / "scripts"
_RUN_BRANCH = _STACK_SCRIPTS / "run-branch.sh"
_AGENT_BASE_BRANCH = _STACK_SCRIPTS / "agent-base-branch.sh"
_GITOPS_REPO_HELPER = _STACK_SCRIPTS / "gitops_repo.py"
_MAIN_TF = _STACK_SCRIPTS.parent / "main.tf"
_MANIFESTS = _STACK_SCRIPTS.parent / "manifests"
_WRAPPER = _REPO_ROOT / "bench" / "hack" / "run-gitops-pilot.sh"
_RUN_PRS = _REPO_ROOT / "bench" / "hack" / "gitops-run-prs.sh"

_SLUG = "example-org/gitops-run"
_REPO = f"https://github.com/{_SLUG}"
_RUN = "run/gitops-pilot-t1/b-0022b"
_OTHER_RUN = "run/gitops-pilot-t2/b-0022b"
_BASE_SHA = "a" * 40
_ELSEWHERE_SHA = "e" * 40
_COMMIT_SHA = "c" * 40
_CR = "platformagents.kubeagents.x-k8s.io/platform-agent"
_BROKER = "deploy/platform-agent-credential-proxy"
# Exit status of the stub uv once the wrapper is past what these tests cover.
_UV_STOP = 97
# Exit status of the stub kubectl at the wrapper's token read (step 3), the
# first call after its base-mode decision.
_KUBECTL_STOP = 98

# curl as run-branch.sh's api() calls it: -o <body> -w '%{http_code}', headers,
# then [-X METHOD] URL [-d DATA]. Answers with the first route in the state
# file whose method and path (after the API host) match.
_STUB_CURL = """\
import json, os, re, sys
state = json.load(open(os.environ["STUB_STATE"]))
method, out, data, url = "GET", None, None, None
args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] in ("-o", "-w", "-H", "-X", "-d"):
        flag, value = args[i], args[i + 1]
        i += 2
        if flag == "-o":
            out = value
        elif flag == "-X":
            method = value
        elif flag == "-d":
            data = value
        continue
    if args[i].startswith("https://"):
        url = args[i]
    i += 1
path = url.split("https://api.github.com", 1)[1]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({"method": method, "path": path, "data": data}) + "\\n")
for route in state["routes"]:
    if route["method"] == method and re.fullmatch(route["path"], path):
        code, body = route["code"], route.get("body", {})
        break
else:
    code, body = 599, {"message": "stub curl: no route"}
with open(out, "w") as f:
    json.dump(body, f)
sys.stdout.write(str(code))
"""

# kubectl as agent-base-branch.sh calls it, against one PlatformAgent, its CRD
# and the credential broker's Deployment, all held in the state file. A JSON
# patch is applied to the PlatformAgent op by op (test, add, remove) and fails
# whole when a test op does not hold, and rv_bumps_before_patch other writes
# (status updates) move the resourceVersion before that many patches; a CRD without
# spec.integration.repositories[].baseBranch prunes it after the patch, as the
# API server does; a CRD from before the lists form (crd_has_lists false)
# declares neither forges nor repositories, one from before the drift detector
# (crd_has_drift_detector false) no spec.harness.driftDetector, and `apply`
# refuses any spec.integration or spec.harness field the CRD does not declare,
# as strict field validation does, and two forges with one name, as the
# schema's list-map key does. The broker's CREDENTIAL_PROXY_PINNED_BASES is rendered from
# the PlatformAgent at each read, under render_repository when the state sets
# one. For run-gitops-pilot.sh, its agent state reset (delete, PVC wait, apply,
# waits) is answered and the applied PlatformAgent kept, its freshness report
# answers fresh, any other `exec` (gitops-run-repo.sh check) succeeds, and the
# secret read records the default-branch switch the wrapper exported before
# it, then stops the wrapper.
_STUB_KUBECTL = """\
import json, os, sys
path = os.environ["STUB_STATE"]
state = json.load(open(path))
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
rest = []
i = 0
while i < len(args):
    if args[i] in ("--context", "-n"):
        i += 2
        continue
    rest.append(args[i])
    i += 1

def save():
    json.dump(state, open(path, "w"))

def flag(prefix):
    for j, a in enumerate(rest):
        if a == prefix:
            return rest[j + 1]
        if a.startswith(prefix + "="):
            return a.split("=", 1)[1]
    return None

def fail(message):
    save()
    sys.stderr.write(message + "\\n")
    sys.exit(1)

def missing_cr():
    fail('Error from server (NotFound): platformagents "platform-agent" not found')

def repositories(cr):
    return cr["spec"].get("integration", {}).get("repositories", [])

def set_base(value):
    # Another writer: the GitOps entry's base changes, and so does the resourceVersion.
    entry = next(r for r in repositories(state["cr"]) if r["role"] == "gitops")
    entry.pop("baseBranch", None)
    if value:
        entry["baseBranch"] = value
    state["rv"] = state.get("rv", 1) + 1

def cr_json():
    cr = json.loads(json.dumps(state["cr"]))
    cr.setdefault("metadata", {})["resourceVersion"] = str(state.get("rv", 1))
    return cr

def apply_patch(doc, ops):
    for op in ops:
        *parents, last = op["path"].split("/")[1:]
        node = doc
        for key in parents:
            node = node[int(key)] if isinstance(node, list) else node[key]
        key = int(last) if isinstance(node, list) else last
        if op["op"] == "test":
            if node[key] != op["value"]:
                raise ValueError(op["path"])
        elif op["op"] == "add":
            node[key] = op["value"]
        elif op["op"] == "remove":
            del node[key]

def spec_schema():
    repository = {"forge": {"type": "string"}, "repository": {"type": "string"}, "role": {"type": "string"}}
    if state["crd_has_field"]:
        repository["baseBranch"] = {"type": "string"}
    integration = {"github": {"type": "object"}}
    if state.get("crd_has_lists", True):
        integration.update({"forges": {"type": "array"}, "repositories": {"type": "array", "items": {"properties": repository}}})
    if state.get("crd_integration_field"):
        integration["baseBranch"] = {"type": "string"}
    harness = {"eventWatcher": {"type": "object"}}
    if state.get("crd_has_drift_detector", True):
        harness["driftDetector"] = {"type": "object"}
    return {"integration": {"properties": integration}, "harness": {"properties": harness}}

verb, target = rest[0], rest[1]
if verb == "get" and target == "crd":
    if state.get("crd_unreadable"):
        fail('Error from server (Forbidden): customresourcedefinitions.apiextensions.k8s.io is forbidden')
    schema = {"properties": {"spec": {"properties": spec_schema()}}}
    print(json.dumps({"spec": {"versions": [{"name": "v1alpha1", "served": True, "schema": {"openAPIV3Schema": schema}}]}}))
elif verb == "get" and target == "%(cr)s":
    if not state["cr_exists"] or flag("-o") != "json":
        missing_cr()
    print(json.dumps(cr_json()))
elif verb == "get" and target == "%(broker)s":
    if state.get("broker_read_failures", 0) > 0:
        state["broker_read_failures"] -= 1
        fail("Unable to connect to the server: net/http: TLS handshake timeout")
    pins = [{"repository": state.get("render_repository", "%(repo)s"), "branch": r["baseBranch"]}
            for r in repositories(state["cr"]) if r["role"] in ("gitops", "managed") and r.get("baseBranch")]
    env = [{"name": "CREDENTIAL_PROXY_PINNED_BASES", "value": json.dumps(pins)}] if pins else []
    print(json.dumps({"spec": {"template": {"spec": {"containers": [{"name": "credential-proxy", "env": env}]}}}}))
elif verb == "patch" and target == "%(cr)s":
    if not state["cr_exists"]:
        missing_cr()
    ops = json.loads(flag("-p"))
    state.setdefault("json_patches", []).append(ops)
    removal = any(op["op"] == "remove" for op in ops)
    if not removal and state.get("written_before_patch") is not None:
        # Another writer between pin's read and its patch.
        set_base(state.pop("written_before_patch"))
    if not removal and state.get("rv_bumps_before_patch", 0) > 0:
        # A write that leaves the base alone, such as the operator's status update.
        state["rv_bumps_before_patch"] -= 1
        state["rv"] = state.get("rv", 1) + 1
    if removal and state.get("remove_fails"):
        if state.get("remove_fails_sets_base") is not None:
            set_base(state.pop("remove_fails_sets_base"))
        fail("error: stream error: context deadline exceeded")
    doc = cr_json()
    try:
        apply_patch(doc, ops)
    except (ValueError, KeyError, IndexError, TypeError):
        fail("The request is invalid: the server rejected our request due to an error in our request")
    if not state["crd_has_field"]:
        for r in repositories(doc):
            r.pop("baseBranch", None)
    doc["metadata"].pop("resourceVersion")
    state["cr"] = doc
    state["rv"] = state.get("rv", 1) + 1
    save()
    if not removal and state.get("patch_fails_after_apply"):
        fail("error: stream error: context deadline exceeded")
    if removal and state.pop("remove_fails_after_apply", False):
        fail("error: stream error: context deadline exceeded")
elif verb == "delete" and target == "%(cr)s":
    state["deleted"] = True
    save()
elif verb == "delete" and target == "pvc":
    pass
elif verb == "get" and target == "pvc":
    fail('Error from server (NotFound): persistentvolumeclaims not found')
elif verb == "apply":
    source = flag("-f")
    applied = json.load(sys.stdin if source == "-" else open(source))
    # kubectl apply validates strictly by default.
    for section, declared in spec_schema().items():
        for field in applied["spec"].get(section, {}):
            if field not in declared["properties"]:
                fail('Error from server (BadRequest): error when creating "STDIN": PlatformAgent in version '
                     '"v1alpha1" cannot be handled as a PlatformAgent: strict decoding error: '
                     'unknown field "spec.%%s.%%s"' %% (section, field))
    # The schema keys spec.integration.forges on name.
    names = [f.get("name") for f in applied["spec"].get("integration", {}).get("forges") or []]
    if len(names) != len(set(names)):
        fail('The PlatformAgent "platform-agent" is invalid: spec.integration.forges: Duplicate value: %%r' %% names)
    state["applied"] = applied
    state["cr"] = state["applied"]
    save()
elif verb == "wait":
    pass
elif verb == "exec":
    if any(a.startswith("ONBOARDING_CARD_PREFIX=") for a in args):
        print(json.dumps({"kanban_cards": [], "onboarding_cards": [], "front_messages": 0, "platform_messages": 0,
                          "platform_messages_outside_onboarding": 0, "scratch": [], "gitops": [],
                          "workspaces": [], "profiles": []}))
elif verb == "get" and target == "secret":
    state["switch_at_token_read"] = os.environ.get("TF_VAR_gitops_switch_default_branch")
    save()
    sys.exit(%(stop)d)
elif verb == "rollout" and target == "status":
    if state.get("rollout_sets_base") is not None:
        set_base(state.pop("rollout_sets_base"))
        save()
    if state.get("rollout_fails"):
        fail("error: timed out waiting for the condition")
else:
    sys.stderr.write("stub kubectl: unexpected call %%r\\n" %% (args,))
    sys.exit(2)
""" % {"cr": _CR, "broker": _BROKER, "stop": _KUBECTL_STOP, "repo": _REPO}


def _platform_agent(base="", alias=False, gitops_repository=_REPO, forge=None):
    """A PlatformAgent whose GitOps repository entry (index 1) carries `base`, or one on the github alias."""
    if alias:
        return {"metadata": {"name": "platform-agent"},
                "spec": {"integration": {"github": {"org": "example-org", "gitRepo": _REPO}}}}
    gitops = {"forge": "github", "repository": gitops_repository, "role": "gitops"}
    if base:
        gitops["baseBranch"] = base
    return {"metadata": {"name": "platform-agent"}, "spec": {"integration": {
        "forges": [forge or {"name": "github", "provider": "github", "namespace": "example-org"}],
        "repositories": [{"forge": "github", "repository": "example-org/docs", "role": "context"}, gitops],
    }}}


# uv as run-gitops-pilot.sh calls it up to its base-mode decision: `uv sync`
# succeeds, case_var's `uv run --no-sync python - <task.yaml> <name>` runs the
# real snippet, the HOLD_SUPPORTED probe (the same call with no arguments)
# answers no, and anything else stops the wrapper.
_STUB_UV = """\
import os, sys
args = sys.argv[1:]
if args[:1] == ["sync"]:
    sys.exit(0)
if args[:4] == ["run", "--no-sync", "python", "-"] and len(args) == 6 and args[4].endswith("task.yaml"):
    os.execv(sys.executable, [sys.executable, "-"] + args[4:])
if args == ["run", "--no-sync", "python", "-"]:
    print("no")
    sys.exit(0)
sys.exit(%d)
""" % _UV_STOP


# gh as gitops-run-prs.sh calls it: `gh api <path>`, answered from the state
# file, or failing when the state says so.
_STUB_GH = """\
import json, os, sys
state = json.load(open(os.environ["STUB_STATE"]))
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if state.get("fails"):
    sys.stderr.write("HTTP 502: Bad Gateway\\n")
    sys.exit(1)
print(json.dumps(state["pulls"]))
"""


class _StubbedScriptTest(unittest.TestCase):
    """A temp dir with stub executables first on PATH, a state file and a call log."""

    stubs = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        # The scripts' own python3 calls get this interpreter, which has pyyaml.
        # A wrapper, not a symlink: a venv is found from the path the interpreter
        # was invoked by, so a symlink elsewhere runs the base interpreter without
        # the venv's packages (render-broken-base.sh imports yaml through it).
        python3 = self.bin_dir / "python3"
        python3.write_text(f"#!{sys.executable}\nimport os, sys\nos.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n")
        python3.chmod(0o755)
        for name, source in self.stubs.items():
            stub = self.bin_dir / name
            stub.write_text(f"#!{sys.executable}\n{source}")
            stub.chmod(0o755)
        self.state_path = self.tmp / "state.json"
        self.log_path = self.tmp / "calls.log"
        self.log_path.write_text("")

    def tearDown(self):
        self._tmp.cleanup()

    def write_state(self, state):
        self.state_path.write_text(json.dumps(state))

    def state(self):
        return json.loads(self.state_path.read_text())

    def calls(self):
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def run_script(self, argv, env):
        base = get_isolated_test_env(bin_dir=self.bin_dir)
        for key in list(base):
            if key.startswith(("GITOPS_", "TF_VAR_", "AGENT_", "BENCH_", "DEVOPS_BENCH_")) or key in ("TASK", "CASE", "BASE_BRANCH_MODE"):
                del base[key]
        base.update({"STUB_STATE": str(self.state_path), "STUB_LOG": str(self.log_path)})
        base.update(env)
        return subprocess.run(argv, capture_output=True, text=True, env=base, timeout=60)


class RunBranchSeedTest(_StubbedScriptTest):
    """run-branch.sh create with GITOPS_SEED_DEFAULT_BRANCH=true."""

    stubs = {"curl": _STUB_CURL}

    def setUp(self):
        super().setUp()
        token = self.tmp / "token"
        token.write_text("ghp_test\n")
        self.env = {
            "GITOPS_REPO": _REPO,
            "GITOPS_RUN_BRANCH": _RUN,
            "GITOPS_TOKEN_FILE": str(token),
            "GITOPS_BASE_SHA": _BASE_SHA,
            "GITOPS_TASK": "b-0022b",
            "GITOPS_TASK_PATH": "tasks/b-0022b",
            "GITOPS_MANIFESTS_DIR": str(_MANIFESTS / "b-0022b"),
            "GITOPS_SEED_DEFAULT_BRANCH": "true",
        }

    def routes(self, base_has_task, default_head, fast_forward_code=200, base_parents=()):
        repo = f"/repos/{_SLUG}"
        routes = [
            {"method": "GET", "path": f"{repo}/contents/tasks/b-0022b\\?ref={_BASE_SHA}",
             "code": 200 if base_has_task else 404},
            # check_default_seed's root check, and commit_stage for a base
            # without the task directory.
            {"method": "GET", "path": f"{repo}/git/commits/{_BASE_SHA}", "code": 200,
             "body": {"tree": {"sha": "t" * 40}, "parents": [{"sha": p} for p in base_parents]}},
            {"method": "POST", "path": f"{repo}/git/blobs", "code": 201, "body": {"sha": "b" * 40}},
            {"method": "POST", "path": f"{repo}/git/trees", "code": 201, "body": {"sha": "d" * 40}},
            {"method": "GET", "path": "/user", "code": 200, "body": {"login": "bench-bot"}},
            {"method": "POST", "path": f"{repo}/git/commits", "code": 201, "body": {"sha": _COMMIT_SHA}},
            # check_default_seed.
            {"method": "GET", "path": repo, "code": 200, "body": {"default_branch": "main"}},
            {"method": "GET", "path": f"{repo}/git/refs/heads/main", "code": 200,
             "body": {"object": {"sha": default_head}}},
            {"method": "POST", "path": f"{repo}/git/refs", "code": 201, "body": {}},
            {"method": "PATCH", "path": f"{repo}/git/refs/heads/main", "code": fast_forward_code,
             "body": {} if fast_forward_code == 200 else {"message": "Update is not a fast forward"}},
            {"method": "DELETE", "path": f"{repo}/git/refs/heads/{_RUN}", "code": 204},
        ]
        self.write_state({"routes": routes})

    def test_default_branch_away_from_the_base_is_refused_before_the_run_branch_is_created(self):
        self.routes(base_has_task=True, default_head=_ELSEWHERE_SHA)
        proc = self.run_script(["bash", str(_RUN_BRANCH), "create"], self.env)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("refusing to move it", proc.stderr)
        writes = [c for c in self.calls() if c["method"] != "GET"]
        self.assertEqual(writes, [], "a refused seed must write nothing, the run branch included")

    def test_failed_fast_forward_deletes_the_run_branch(self):
        self.routes(base_has_task=False, default_head=_BASE_SHA, fast_forward_code=422)
        proc = self.run_script(["bash", str(_RUN_BRANCH), "create"], self.env)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        writes = [(c["method"], c["path"]) for c in self.calls() if c["path"].startswith(f"/repos/{_SLUG}/git/refs")
                  and c["method"] != "GET"]
        self.assertEqual(writes, [
            ("POST", f"/repos/{_SLUG}/git/refs"),
            ("PATCH", f"/repos/{_SLUG}/git/refs/heads/main"),
            ("DELETE", f"/repos/{_SLUG}/git/refs/heads/{_RUN}"),
        ])

    def test_default_branch_at_the_base_is_fast_forwarded_onto_the_run_branch_commit(self):
        self.routes(base_has_task=False, default_head=_BASE_SHA)
        proc = self.run_script(["bash", str(_RUN_BRANCH), "create"], self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        patches = [c for c in self.calls() if c["method"] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["path"], f"/repos/{_SLUG}/git/refs/heads/main")
        self.assertEqual(json.loads(patches[0]["data"]), {"sha": _COMMIT_SHA, "force": False})
        self.assertFalse([c for c in self.calls() if c["method"] == "DELETE"])
        # A render that fails inside commit_stage's command substitution does not
        # stop create; it shows only as blobs with nothing in them.
        blobs = [json.loads(c["data"]) for c in self.calls() if c["method"] == "POST" and c["path"].endswith("/git/blobs")]
        self.assertTrue(blobs and all(b["content"] for b in blobs), "the blobs carry the broken render's files")

    def test_base_with_history_is_refused_before_any_write(self):
        self.routes(base_has_task=False, default_head=_BASE_SHA, base_parents=[_ELSEWHERE_SHA])
        proc = self.run_script(["bash", str(_RUN_BRANCH), "create"], self.env)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("has parents", proc.stderr)
        writes = [c for c in self.calls() if c["method"] != "GET"]
        self.assertEqual(writes, [], "a base that is not a root commit must write nothing, git objects included")

    def test_staged_history_is_refused_before_its_healthy_commit_is_written(self):
        self.routes(base_has_task=True, default_head=_BASE_SHA)
        proc = self.run_script(["bash", str(_RUN_BRANCH), "create"],
                               {**self.env, "GITOPS_HISTORY_PARENT_SHA": _ELSEWHERE_SHA})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("not offered for staged history", proc.stderr)
        writes = [c for c in self.calls() if c["method"] != "GET"]
        self.assertEqual(writes, [], "staged history must be refused before any git object is written")

    def test_gitops_repo_is_read_as_the_operator_reads_it(self):
        for spelled in (f"{_REPO}/", f"{_REPO}.git/", f"git@github.com:{_SLUG}.git"):
            with self.subTest(spelled=spelled):
                self.routes(base_has_task=False, default_head=_BASE_SHA)
                self.log_path.write_text("")
                proc = self.run_script(["bash", str(_RUN_BRANCH), "create"], {**self.env, "GITOPS_REPO": spelled})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertTrue(all(c["path"].startswith(f"/repos/{_SLUG}/") or c["path"] in (f"/repos/{_SLUG}", "/user")
                                    for c in self.calls()), self.calls())

    def test_gitops_repo_that_is_not_a_github_repository_is_refused_before_any_call(self):
        self.routes(base_has_task=False, default_head=_BASE_SHA)
        proc = self.run_script(["bash", str(_RUN_BRANCH), "create"],
                               {**self.env, "GITOPS_REPO": f"https://gitlab.com/{_SLUG}"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("is not a github.com repository", proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_default_already_at_the_target_needs_no_write_on_a_base_with_history(self):
        self.routes(base_has_task=True, default_head=_BASE_SHA, base_parents=[_ELSEWHERE_SHA])
        proc = self.run_script(["bash", str(_RUN_BRANCH), "create"], self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already at", proc.stdout)
        writes = [(c["method"], c["path"]) for c in self.calls() if c["method"] != "GET"]
        self.assertEqual(writes, [("POST", f"/repos/{_SLUG}/git/refs")])


class AgentBaseBranchTest(_StubbedScriptTest):
    """agent-base-branch.sh pin and unpin."""

    stubs = {"kubectl": _STUB_KUBECTL}

    env = {
        "AGENT_HOST_CONTEXT": "agent-host",
        "AGENT_NAMESPACE": "kubeagents-system",
        "GITOPS_REPO": _REPO,
        "GITOPS_RUN_BRANCH": _RUN,
        # Short, so a wait that never matches fails the test in seconds.
        "AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC": "2",
        "AGENT_BASE_BRANCH_POLL_SECONDS": "1",
    }

    # Where the GitOps entry of _platform_agent() sits.
    entry = "/spec/integration/repositories/1"

    def given(self, base="", alias=False, cr=None, **overrides):
        state = {"cr": cr or _platform_agent(base, alias=alias), "crd_has_field": True, "cr_exists": True}
        state.update(overrides)
        self.write_state(state)

    def base(self):
        (entry,) = [r for r in self.state()["cr"]["spec"]["integration"]["repositories"] if r["role"] == "gitops"]
        return entry.get("baseBranch", "")

    def run_action(self, action, **env):
        return self.run_script(["bash", str(_AGENT_BASE_BRANCH), action], {**self.env, **env})

    def patches(self):
        return [c for c in self.calls() if c[4:5] == ["patch"]]

    def removals(self):
        return [ops for ops in self.state().get("json_patches", []) if any(op["op"] == "remove" for op in ops)]

    def test_pin_sets_the_base_with_a_patch_that_tests_the_entry_and_waits_for_the_broker(self):
        self.given()
        proc = self.run_action("pin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.base(), _RUN)
        self.assertEqual(self.state()["json_patches"], [[
            {"op": "test", "path": "/metadata/resourceVersion", "value": "1"},
            {"op": "test", "path": f"{self.entry}/repository", "value": _REPO},
            {"op": "test", "path": f"{self.entry}/role", "value": "gitops"},
            {"op": "add", "path": f"{self.entry}/baseBranch", "value": _RUN},
        ]])
        self.assertIn(f"CREDENTIAL_PROXY_PINNED_BASES holds {_REPO} -> {_RUN}", proc.stdout)
        self.assertTrue([c for c in self.calls() if c[4:6] == ["rollout", "status"]])

    def test_the_gitops_entry_is_found_in_each_way_a_repository_is_written(self):
        # Every spelling the operator's GitProvider.Resolve reads as this repository.
        for written in (_SLUG, "gitops-run", f"git@github.com:{_SLUG}.git", "https://github.com/Example-Org/gitops-run/",
                        f"github.com/{_SLUG}", f"www.github.com/{_SLUG}", f"/{_SLUG}", f"https://github.com/{_SLUG}.git/",
                        f"ssh://git@ssh.github.com:443/{_SLUG}.git", f"git@github.com/{_SLUG}"):
            with self.subTest(written=written):
                self.given(cr=_platform_agent(gitops_repository=written))
                proc = self.run_action("pin")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.base(), _RUN)

    def test_a_bare_name_is_qualified_by_the_entrys_namespace_or_its_forges(self):
        for name, forge, namespace in (
            ("forge namespace", {"name": "github", "provider": "github", "namespace": "example-org"}, None),
            ("entry namespace", {"name": "github", "provider": "github"}, "example-org"),
            ("entry namespace over the forge's", {"name": "github", "namespace": "elsewhere"}, "example-org"),
            ("another spelling of the forge's host", {"name": "github", "host": "WWW.github.com", "namespace": "example-org"}, None),
        ):
            with self.subTest(name):
                cr = _platform_agent(gitops_repository="gitops-run", forge=forge)
                if namespace:
                    cr["spec"]["integration"]["repositories"][1]["namespace"] = namespace
                self.given(cr=cr)
                proc = self.run_action("pin")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.base(), _RUN)

    def test_an_entry_the_operator_reads_as_another_repository_is_not_this_one(self):
        for name, written, forge in (
            ("another host under a github forge", f"https://github.example.com/{_SLUG}", None),
            ("another forge's host, schemeless", f"gitlab.com/{_SLUG}", None),
            ("a bare name with no namespace", "gitops-run", {"name": "github", "provider": "github"}),
            ("a name the operator refuses", f"{_SLUG}.git.git", None),
            ("a forge on another provider", _SLUG, {"name": "github", "provider": "gitlab", "namespace": "example-org"}),
        ):
            with self.subTest(name):
                self.given(cr=_platform_agent(gitops_repository=written, forge=forge))
                self.log_path.write_text("")
                proc = self.run_action("pin")
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn(f"has no spec.integration.repositories[] entry with role gitops naming {_SLUG}",
                              proc.stderr)
                self.assertEqual(self.patches(), [])

    def test_a_gitops_entry_the_operator_refuses_is_not_this_one(self):
        # ResolvedIntegration.check() refuses each of these entries, and the
        # operator renders no pin for a refused one.
        for name, edit, slug in (
            ("a namespace override GitHub's grammar refuses, beside a full URL",
             lambda repositories: repositories[1].update(namespace="bad_ns"), _SLUG),
            ("a second entry with role gitops",
             lambda repositories: repositories.insert(1, {"forge": "github", "repository": "example-org/another-repo",
                                                          "role": "gitops"}), _SLUG),
            ("a repository an earlier entry declares",
             lambda repositories: repositories.insert(1, {"forge": "github", "repository": _SLUG, "role": "managed"}),
             _SLUG),
            # The deprecated alias's "no repository", which a list refuses
            # rather than qualify into example-org/None.
            ("the alias's no-repository value", lambda repositories: repositories[1].update(repository="None"),
             "example-org/None"),
        ):
            with self.subTest(name):
                cr = _platform_agent()
                edit(cr["spec"]["integration"]["repositories"])
                self.given(cr=cr)
                self.log_path.write_text("")
                proc = self.run_action("pin", GITOPS_REPO=f"https://github.com/{slug}")
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn(f"has no spec.integration.repositories[] entry with role gitops naming {slug}",
                              proc.stderr)
                self.assertEqual(self.patches(), [])

    def test_an_earlier_entry_the_operator_refuses_does_not_claim_the_repository(self):
        # A refused entry claims no URL, so the gitops entry after it is still accepted.
        for name, earlier in (
            ("an unsupported role", {"forge": "github", "repository": _SLUG, "role": "mirror"}),
            ("a refused namespace override", {"forge": "github", "repository": _SLUG, "namespace": "bad_ns",
                                              "role": "managed"}),
            ("an undeclared forge", {"forge": "elsewhere", "repository": _SLUG, "role": "managed"}),
        ):
            with self.subTest(name):
                cr = _platform_agent()
                cr["spec"]["integration"]["repositories"].insert(0, earlier)
                self.given(cr=cr)
                proc = self.run_action("pin")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.base(), _RUN)

    def test_gitops_repo_is_read_as_the_operator_reads_it(self):
        for spelled in (f"{_REPO}/", f"{_REPO}.git/", f"{_REPO}.git", "HTTPS://GitHub.com/Example-Org/Gitops-Run"):
            with self.subTest(spelled=spelled):
                self.given()
                proc = self.run_action("pin", GITOPS_REPO=spelled)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.base(), _RUN)
                self.assertIn("CREDENTIAL_PROXY_PINNED_BASES holds https://github.com/", proc.stdout)
                self.assertNotIn(".git", proc.stdout)

    def test_gitops_repo_that_is_not_a_github_repository_is_refused_before_kubectl(self):
        for spelled in ("https://gitlab.com/example-org/gitops-run", "gitops-run", "https://github.com/"):
            with self.subTest(spelled=spelled):
                self.given()
                self.log_path.write_text("")
                proc = self.run_action("pin", GITOPS_REPO=spelled)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn(f"'{spelled}' is not a github.com repository", proc.stderr)
                self.assertEqual(self.calls(), [])

    def test_pin_refuses_a_platformagent_without_a_gitops_entry_for_the_repository_and_writes_nothing(self):
        for name, cr in (
            ("github alias", _platform_agent(alias=True)),
            ("another repository", _platform_agent(gitops_repository="example-org/another-repo")),
            ("same name on another host", _platform_agent(
                forge={"name": "github", "provider": "github", "host": "github.example.com"})),
        ):
            with self.subTest(name):
                self.given(cr=cr)
                self.log_path.write_text("")
                proc = self.run_action("pin")
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn(f"has no spec.integration.repositories[] entry with role gitops naming {_SLUG}",
                              proc.stderr)
                self.assertEqual(self.patches(), [])

    def test_pin_without_a_gitops_entry_on_a_crd_without_the_field_logs_it_and_succeeds(self):
        # Before the lists form the PlatformAgent can only be on the alias; that
        # is the case's red, not a setup failure.
        for name, crd in (("before the lists form", {"crd_has_lists": False}), ("lists without baseBranch", {})):
            with self.subTest(name):
                self.given(alias=True, crd_has_field=False, **crd)
                self.log_path.write_text("")
                proc = self.run_action("pin")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("this install pins no base", proc.stdout)
                self.assertEqual(self.patches(), [])

    def test_pin_without_a_gitops_entry_and_an_unreadable_crd_fails(self):
        self.given(alias=True, crd_unreadable=True)
        proc = self.run_action("pin")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("cannot read the CRD", proc.stderr)
        self.assertEqual(self.patches(), [])

    def test_pin_refuses_an_existing_different_base_and_leaves_it(self):
        self.given(base="production")
        proc = self.run_action("pin")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing to overwrite", proc.stderr)
        self.assertEqual(self.base(), "production")
        self.assertEqual(self.patches(), [])

    def test_a_base_written_between_the_read_and_the_patch_is_not_overwritten(self):
        self.given(written_before_patch=_OTHER_RUN)
        proc = self.run_action("pin")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(f"is already '{_OTHER_RUN}'; refusing to overwrite it", proc.stderr)
        self.assertEqual(self.base(), _OTHER_RUN)
        self.assertEqual(len(self.state()["json_patches"]), 1, "the refusal comes from the re-read, not another patch")
        self.assertEqual(self.removals(), [], "the undo must leave the other run's base")

    def test_another_write_between_the_read_and_the_patch_is_retried_from_a_fresh_read(self):
        # A status update moves the resourceVersion and leaves the base alone.
        self.given(rv_bumps_before_patch=1)
        proc = self.run_action("pin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.base(), _RUN)
        tested = [ops[0] for ops in self.state()["json_patches"]]
        self.assertEqual(tested, [{"op": "test", "path": "/metadata/resourceVersion", "value": "1"},
                                  {"op": "test", "path": "/metadata/resourceVersion", "value": "2"}])

    def test_a_patch_that_keeps_conflicting_fails_the_pin_after_its_tries(self):
        self.given(rv_bumps_before_patch=10)
        proc = self.run_action("pin")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(f"setting spec.integration.repositories[{_SLUG}].baseBranch failed 3 times", proc.stderr)
        self.assertNotIn("another run", proc.stderr)
        self.assertEqual(len(self.state()["json_patches"]), 3)
        self.assertEqual(self.base(), "")
        self.assertEqual(self.removals(), [])

    def test_failed_wait_after_the_patch_removes_this_runs_base(self):
        self.given(rollout_fails=True)
        proc = self.run_action("pin")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.base(), "")
        self.assertEqual(len(self.removals()), 1)

    def test_undo_leaves_a_base_that_no_longer_names_this_run(self):
        self.given(rollout_fails=True, rollout_sets_base=_OTHER_RUN)
        proc = self.run_action("pin")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.base(), _OTHER_RUN)
        self.assertEqual(self.removals(), [])

    def test_patch_applied_but_reported_failed_is_read_again_and_kept(self):
        self.given(patch_fails_after_apply=True)
        proc = self.run_action("pin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.base(), _RUN)
        self.assertEqual(len(self.state()["json_patches"]), 1, "a base the re-read shows as this run's is not patched again")

    def test_last_patch_applied_but_reported_failed_is_undone(self):
        # Two conflicts, then a third try the server applies but kubectl reports as failed.
        self.given(rv_bumps_before_patch=2, patch_fails_after_apply=True)
        proc = self.run_action("pin")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.base(), "", "the undo trap must be armed before the patch")
        self.assertEqual(len(self.removals()), 1)

    def test_broker_pinning_another_repository_or_host_is_refused_and_the_pin_undone(self):
        for rendered in ("https://github.com/example-org/another-repo", f"https://github.example.com/{_SLUG}"):
            with self.subTest(rendered=rendered):
                self.given(render_repository=rendered)
                proc = self.run_action("pin")
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(f'CREDENTIAL_PROXY_PINNED_BASES is \'[{{"repository": "{rendered}"', proc.stderr)
                self.assertIn(f"expected the pin of {_REPO} to {_RUN} to be held", proc.stderr)
                self.assertEqual(self.base(), "")
                self.assertEqual(len(self.removals()), 1)

    def test_failed_broker_reads_in_the_wait_count_as_not_yet(self):
        self.given(broker_read_failures=2)
        # A ceiling with margin: bash's SECONDS ticks on wall-clock boundaries,
        # so a 2s timeout can expire after about one second of waiting.
        proc = self.run_action("pin", AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC="10")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.base(), _RUN)
        self.assertEqual(self.state()["broker_read_failures"], 0)

    def test_unreadable_crd_fails_the_pin_before_any_write(self):
        self.given(crd_unreadable=True)
        proc = self.run_action("pin")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("cannot read the CRD", proc.stderr)
        self.assertNotIn("API server dropped it", proc.stdout)
        self.assertEqual(self.base(), "")
        self.assertEqual(self.patches(), [], "an unreadable CRD must write nothing")

    def test_pin_on_a_crd_without_the_field_logs_it_writes_nothing_and_succeeds(self):
        # The earlier scalar spec.integration.baseBranch is not the field.
        for integration_field in (False, True):
            with self.subTest(integration_field=integration_field):
                self.given(crd_has_field=False, crd_integration_field=integration_field)
                self.log_path.write_text("")
                proc = self.run_action("pin")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("so the API server would drop spec.integration.repositories", proc.stdout)
                self.assertIn("this install pins no base", proc.stdout)
                self.assertEqual(self.patches(), [], "a patch that cannot take effect is not sent")
                self.assertEqual(self.base(), "")

    def test_poll_and_timeout_settings_must_be_whole_seconds_within_bounds(self):
        # Unbounded, the largest value overflows the deadline into the past, or
        # makes the poll sleep for ever.
        for name, value, top in (
            ("AGENT_BASE_BRANCH_POLL_SECONDS", "0", 60),
            ("AGENT_BASE_BRANCH_POLL_SECONDS", "fast", 60),
            ("AGENT_BASE_BRANCH_POLL_SECONDS", "61", 60),
            ("AGENT_BASE_BRANCH_POLL_SECONDS", "9223372036854775807", 60),
            ("AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC", "5m", 3600),
            ("AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC", "3601", 3600),
            ("AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC", "9223372036854775807", 3600),
        ):
            with self.subTest(name=name, value=value):
                self.given()
                self.log_path.write_text("")
                proc = self.run_action("pin", **{name: value})
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn(f"{name} must be a whole number of seconds from 1 to {top}, not '{value}'", proc.stderr)
                self.assertEqual(self.calls(), [], "a bad setting is refused before kubectl runs")

    def test_unpin_whose_removal_keeps_failing_retries_then_warns_and_succeeds(self):
        self.given(base=_RUN, remove_fails=True)
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("failed 3 times", proc.stderr)
        self.assertEqual(len(self.removals()), 3)
        self.assertEqual(self.base(), _RUN)

    def test_unpin_removal_tests_the_entry_and_the_run_branch_before_removing(self):
        self.given(base=_RUN)
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.base(), "")
        self.assertEqual(self.state()["json_patches"], [[
            {"op": "test", "path": f"{self.entry}/repository", "value": _REPO},
            {"op": "test", "path": f"{self.entry}/role", "value": "gitops"},
            {"op": "test", "path": f"{self.entry}/baseBranch", "value": _RUN},
            {"op": "remove", "path": f"{self.entry}/baseBranch"},
        ]])

    def test_unpin_removal_applied_but_reported_failed_stops_retrying(self):
        self.given(base=_RUN, remove_fails_after_apply=True)
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("failed 3 times", proc.stderr)
        self.assertNotIn("WARN", proc.stderr)
        self.assertIn("baseBranch is gone", proc.stdout)
        self.assertEqual(len(self.removals()), 1)
        self.assertEqual(self.base(), "")
        self.assertTrue([c for c in self.calls() if c[4:6] == ["rollout", "status"]],
                        "the broker wait still runs once the field is gone")

    def test_unpin_retry_leaves_a_base_another_run_set_in_between(self):
        self.given(base=_RUN, remove_fails=True, remove_fails_sets_base=_OTHER_RUN)
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"baseBranch is now '{_OTHER_RUN}', not this run's; leaving it", proc.stdout)
        self.assertEqual(len(self.removals()), 1)
        self.assertEqual(self.base(), _OTHER_RUN)

    def test_unpin_with_a_failing_rollout_wait_removes_the_base_and_succeeds(self):
        self.given(base=_RUN, rollout_fails=True)
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.base(), "")
        self.assertIn("WARN", proc.stderr)

    def test_unpin_leaves_another_base(self):
        self.given(base="production")
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.base(), "production")
        self.assertEqual(self.patches(), [])

    def test_unpin_without_the_platformagent_warns_and_succeeds(self):
        self.given(cr_exists=False)
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WARN", proc.stderr)

    def test_unpin_without_a_gitops_entry_for_the_repository_succeeds_with_nothing_written(self):
        self.given(alias=True)
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("nothing to unpin", proc.stdout)
        self.assertEqual(self.patches(), [])


class WrapperCaseSelectionTest(_StubbedScriptTest):
    """run-gitops-pilot.sh from its TASK/CASE resolution to its base-mode decision."""

    stubs = {"uv": _STUB_UV, "kubectl": _STUB_KUBECTL}

    def setUp(self):
        super().setUp()
        self.given_cr(_platform_agent())

    def given_cr(self, cr, cr_exists=True, **overrides):
        self.write_state({"cr": cr, "crd_has_field": True, "cr_exists": cr_exists, **overrides})

    def run_wrapper(self, wrapper=_WRAPPER, **env):
        token = self.tmp / "token"
        token.write_text("ghp_test\n")
        return self.run_script(["bash", str(wrapper)], {
            "GCP_PROJECT_ID": "example-project",
            "AGENT_HOST_CONTEXT": "agent-host",
            "GITOPS_REPO": _REPO,
            "GITOPS_TOKEN_FILE": str(token),
            "CLUSTER_NAME": "gitops-pilot-t1",
            # Given, so the wrapper reads neither the repository nor LiteLLM.
            "GITOPS_BROKEN_BASE_SHA": _BASE_SHA,
            "AGENT_MODEL": "example-model",
            # The task copy's mktemp and the reset's backup land here, so a run
            # killed at the timeout leaves nothing outside the test's directory.
            "TMPDIR": str(self.tmp),
            **env,
        })

    def wrapper_with_case(self, case, text):
        """A copy of the wrapper whose ./tasks holds one more case, as a run's frozen copy is."""
        bench = self.tmp / "bench"
        (bench / "hack").mkdir(parents=True)
        for script in (_WRAPPER, _WRAPPER.parent / "gitops-run-repo.sh"):
            shutil.copy(script, bench / "hack" / script.name)
        scripts = bench / _GITOPS_REPO_HELPER.parent.relative_to(_WRAPPER.parents[1])
        scripts.mkdir(parents=True)
        shutil.copy(_GITOPS_REPO_HELPER, scripts / _GITOPS_REPO_HELPER.name)
        (bench / "tasks" / case).mkdir(parents=True)
        (bench / "tasks" / case / "task.yaml").write_text(text)
        return bench / "hack" / _WRAPPER.name

    def test_task_that_disagrees_with_the_case_is_refused(self):
        proc = self.run_wrapper(TASK="b-0011", CASE="b-0022b-gitops-pinned-base")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("disagrees with b-0022b-gitops-pinned-base's gitops_task b-0022b", proc.stderr)
        self.assertNotIn("==> run", proc.stdout)

    def test_case_alone_takes_the_task_from_the_case(self):
        proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base")
        self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
        self.assertIn(
            "==> run gitops-pilot-t1: case b-0022b-gitops-pinned-base, branch run/gitops-pilot-t1/b-0022b",
            proc.stdout,
        )

    def test_another_runs_pin_on_the_platformagent_is_refused_as_leaked(self):
        self.given_cr(_platform_agent(_OTHER_RUN))
        proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(f"spec.integration.repositories[1].baseBranch ({_REPO}) is '{_OTHER_RUN}', "
                      f"not this run's {_RUN}: a leaked pin", proc.stderr)
        self.assertIn('patch platformagents.kubeagents.x-k8s.io/platform-agent --type=json', proc.stderr)
        self.assertIn('"path":"/spec/integration/repositories/1/baseBranch"', proc.stderr)
        self.assertEqual(self.state()["cr"], _platform_agent(_OTHER_RUN), "the wrapper only refuses; it removes nothing")

    def test_this_runs_own_pin_or_a_crd_without_the_field_does_not_stop_a_pinned_case(self):
        for name, cr, crd in (
            ("own pin", _platform_agent(_RUN), {}),
            # The stack logs that such an install pins no base: the case's red.
            ("install base on a CRD without the field", _platform_agent("production"), {"crd_has_field": False}),
            ("github alias on a CRD without the field", _platform_agent(alias=True), {"crd_has_field": False}),
        ):
            with self.subTest(name):
                self.given_cr(cr, **crd)
                proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base")
                self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
                self.assertNotIn("leaked pin", proc.stderr)

    def test_an_install_base_on_the_gitops_entry_stops_a_pinned_case_before_the_reset(self):
        # The reset keeps that base, and the stack would refuse to overwrite it
        # after the task cluster is built.
        for name, written in (("same spelling", _REPO), ("another spelling", f"github.com/{_SLUG}.git")):
            with self.subTest(name):
                self.given_cr(_platform_agent("production", gitops_repository=written))
                proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base", AGENT_STATE_RESET="true")
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn(f"entry with role gitops for {_SLUG} has baseBranch 'production', the install's own base",
                              proc.stderr)
                self.assertNotIn("deleted", self.state())

    def test_an_install_base_does_not_stop_a_case_that_pins_nothing(self):
        self.given_cr(_platform_agent("production"))
        proc = self.run_wrapper(CASE="b-0022b-gitops")
        self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)

    def test_a_pinned_case_without_a_gitops_entry_needs_the_reset_on_a_crd_with_the_field(self):
        for name, cr in (("github alias", _platform_agent(alias=True)),
                         ("another repository", _platform_agent(gitops_repository="example-org/another-repo"))):
            with self.subTest(name):
                self.given_cr(cr)
                proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base")
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn(f"no spec.integration.repositories[] entry with role gitops for {_SLUG}", proc.stderr)
                self.assertIn("rerun with AGENT_STATE_RESET=true", proc.stderr)
                self.assertNotIn("switch_at_token_read", self.state())

    def test_unreadable_platformagent_stops_the_wrapper(self):
        self.given_cr(_platform_agent(), cr_exists=False)
        proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("whether an earlier run left a pin there is unknown", proc.stderr)

    def test_reset_for_a_case_that_pins_the_base_writes_the_lists_form(self):
        self.given_cr(_platform_agent(alias=True))
        proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base", AGENT_STATE_RESET="true")
        self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
        self.assertEqual(self.state()["applied"]["spec"]["integration"], {
            "forges": [{"name": "github", "provider": "github", "namespace": "example-org"}],
            "repositories": [{"forge": "github", "repository": _REPO, "role": "gitops"}],
        })

    def test_reset_turns_the_drift_detector_off_unless_asked_and_keeps_its_settings(self):
        # Left on, the detector files triage cards about the reset's own
        # deletions, and the freshness check refuses the run.
        for env, enabled in (({}, False), ({"AGENT_DRIFT_DETECTOR": "true"}, True)):
            with self.subTest(env=env):
                cr = _platform_agent(alias=True)
                cr["spec"].setdefault("harness", {})["driftDetector"] = {"enabled": True, "subscription": "drift-sub"}
                self.given_cr(cr)
                proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base", AGENT_STATE_RESET="true", **env)
                self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
                self.assertEqual(self.state()["applied"]["spec"]["harness"]["driftDetector"],
                                 {"enabled": enabled, "subscription": "drift-sub"})

    def test_reset_on_a_crd_without_the_drift_detector_writes_none(self):
        # The stub's apply refuses a harness field the CRD does not declare, as
        # strict field validation does, after the delete.
        for case in ("b-0022b-gitops-pinned-base", "b-0022b-gitops"):
            with self.subTest(case):
                self.given_cr(_platform_agent(alias=True), crd_has_drift_detector=False)
                proc = self.run_wrapper(CASE=case, AGENT_STATE_RESET="true")
                self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
                self.assertIn("has no spec.harness.driftDetector, so the reset writes none", proc.stdout)
                self.assertEqual(self.state()["applied"]["spec"]["harness"], {"eventWatcher": {"enabled": False}})

    def test_reset_on_an_unreadable_crd_deletes_nothing(self):
        self.given_cr(_platform_agent(alias=True), crd_unreadable=True)
        proc = self.run_wrapper(CASE="b-0022b-gitops", AGENT_STATE_RESET="true")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("so whether the reset can write spec.harness.driftDetector is unknown; nothing was reset", proc.stderr)
        self.assertNotIn("deleted", self.state())

    def test_reset_for_a_case_that_pins_the_base_keeps_the_alias_on_a_crd_without_the_lists_form(self):
        # The stub's apply refuses forges and repositories on such a CRD, as
        # strict field validation does, after the delete.
        self.given_cr(_platform_agent(alias=True), crd_has_lists=False, crd_has_field=False)
        proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base", AGENT_STATE_RESET="true")
        self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
        self.assertIn("so the reset keeps the github alias", proc.stdout)
        self.assertEqual(self.state()["applied"]["spec"]["integration"],
                         {"github": {"org": "example-org", "gitRepo": _REPO}})

    def test_reset_for_a_case_that_pins_the_base_on_an_unreadable_crd_deletes_nothing(self):
        self.given_cr(_platform_agent(alias=True), crd_unreadable=True)
        proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base", AGENT_STATE_RESET="true")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("cannot read the PlatformAgent CRD", proc.stderr)
        self.assertNotIn("deleted", self.state())
        self.assertNotIn("applied", self.state())

    def test_reset_keeps_the_base_and_namespace_of_a_gitops_entry_for_the_same_repository(self):
        cr = _platform_agent()
        cr["spec"]["integration"]["forges"].append({"name": "work", "provider": "github", "namespace": "elsewhere"})
        cr["spec"]["integration"]["repositories"][1] = {
            "forge": "work", "repository": "Gitops-Run", "namespace": "example-org", "role": "gitops",
            "baseBranch": "production"}
        # GITOPS_REPO in each spelling the operator reads as the same
        # repository, written back as its https URL.
        for repo, written in ((_REPO, _REPO), (f"{_REPO}/", _REPO), (f"{_REPO}.git/", _REPO),
                              ("https://github.com/EXAMPLE-ORG/gitops-run", "https://github.com/EXAMPLE-ORG/gitops-run")):
            with self.subTest(repo=repo):
                self.given_cr(cr)
                proc = self.run_wrapper(CASE="b-0022b-gitops", AGENT_STATE_RESET="true", GITOPS_REPO=repo)
                self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
                self.assertEqual(self.state()["applied"]["spec"]["integration"]["repositories"], [
                    {"forge": "work", "repository": written, "role": "gitops", "namespace": "example-org",
                     "baseBranch": "production"},
                    {"forge": "github", "repository": "example-org/docs", "role": "context"},
                ])
                self.assertNotIn("replaces the gitops entry", proc.stderr)

    def test_gitops_repo_in_another_spelling_reaches_the_run_as_its_https_url(self):
        # The harness reads only https://github.com/<owner>/<name> as a
        # repository, so every spelling the helper accepts is handed on as that.
        for spelled in (f"https://www.github.com/{_SLUG}", f"ssh://git@github.com/{_SLUG}.git",
                        f"https://x@github.com/{_SLUG}", f"{_REPO}.git/"):
            with self.subTest(spelled=spelled):
                self.given_cr(_platform_agent(alias=True))
                proc = self.run_wrapper(CASE="b-0022b-gitops", AGENT_STATE_RESET="true", GITOPS_REPO=spelled)
                self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
                self.assertIn(f"==> repository {_REPO} (broken base {_BASE_SHA}); prompt rendered", proc.stdout)
                self.assertEqual(self.state()["applied"]["spec"]["integration"]["github"]["gitRepo"], _REPO)

    def test_reset_beside_a_forge_named_github_that_is_no_valid_github_forge_adds_its_own(self):
        # The schema keys forges on name, so a second forge named github is
        # refused, after the delete.
        for name, forge in (
            ("a namespace GitHub's grammar refuses", {"name": "github", "provider": "github", "namespace": "example.org"}),
            ("another provider", {"name": "github", "provider": "gitlab", "namespace": "example-org"}),
        ):
            with self.subTest(name):
                cr = _platform_agent(forge=forge)
                cr["spec"]["integration"]["forges"].append({"name": "github-2", "provider": "gitlab"})
                self.given_cr(cr)
                proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base", AGENT_STATE_RESET="true")
                self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
                integration = self.state()["applied"]["spec"]["integration"]
                self.assertEqual(integration["forges"], [forge, {"name": "github-2", "provider": "gitlab"},
                                                         {"name": "github-3", "provider": "github"}])
                self.assertEqual(integration["repositories"][0], {"forge": "github-3", "repository": _REPO, "role": "gitops"})

    def test_reset_whose_new_spec_cannot_be_built_deletes_nothing(self):
        # The re-applied spec is built before the delete, so a failure there
        # leaves the PlatformAgent in place: here a gitops_repo.py that a
        # checkout broke mid-run, after the wrapper's early reads.
        wrapper = self.wrapper_with_case("b-0022b-gitops", (_WRAPPER.parents[1] / "tasks" / "b-0022b-gitops" / "task.yaml").read_text())
        helper = wrapper.parents[1] / _GITOPS_REPO_HELPER.relative_to(_WRAPPER.parents[1])
        helper.write_text('if __name__ != "__main__":\n    raise ImportError("moved mid-run")\n' + helper.read_text())
        self.given_cr(_platform_agent())
        proc = self.run_wrapper(wrapper=wrapper, CASE="b-0022b-gitops", AGENT_STATE_RESET="true")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("moved mid-run", proc.stderr)
        self.assertNotIn("deleted", self.state())
        self.assertNotIn("applied", self.state())

    def test_wrapper_refuses_a_gitops_repo_that_is_not_a_github_repository(self):
        proc = self.run_wrapper(CASE="b-0022b-gitops", GITOPS_REPO=f"https://gitlab.com/{_SLUG}")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("is not a github.com repository", proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_reset_for_a_case_that_pins_nothing_keeps_the_spec_form(self):
        for name, cr, integration in (
            ("github alias", _platform_agent(alias=True), {"github": {"org": "example-org", "gitRepo": _REPO}}),
            # The alias beside the lists would be refused; a pinned run leaves an install so.
            ("lists", _platform_agent("production", gitops_repository="example-org/earlier-run"), {
                "forges": [{"name": "github", "provider": "github", "namespace": "example-org"}],
                "repositories": [{"forge": "github", "repository": _REPO, "role": "gitops"},
                                 {"forge": "github", "repository": "example-org/docs", "role": "context"}],
            }),
        ):
            with self.subTest(name):
                self.given_cr(cr)
                proc = self.run_wrapper(CASE="b-0022b-gitops", AGENT_STATE_RESET="true")
                self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
                self.assertEqual(self.state()["applied"]["spec"]["integration"], integration)
                if name == "lists":
                    # Another repository's entry is replaced, and the base it held is logged.
                    self.assertIn("replaces the gitops entry example-org/earlier-run, and with it its baseBranch production",
                                  proc.stderr)

    def test_a_case_that_pins_the_base_leaves_the_default_branch_switch_off(self):
        proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base")
        self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
        self.assertIn("==> base branch via the PlatformAgent's spec.integration.repositories[].baseBranch", proc.stdout)
        self.assertIsNone(self.state()["switch_at_token_read"])

    def test_a_case_that_pins_nothing_switches_the_default_branch(self):
        proc = self.run_wrapper(CASE="b-0022b-gitops")
        self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
        self.assertIn("==> base branch via repository default", proc.stdout)
        self.assertEqual(self.state()["switch_at_token_read"], "true")

    def test_a_case_that_turns_the_switch_off_and_pins_nothing_is_refused(self):
        pinned = (_WRAPPER.parents[1] / "tasks" / "b-0022b-gitops-pinned-base" / "task.yaml").read_text()
        pin_line = "    gitops_pin_agent_base_branch: true\n"
        self.assertIn(pin_line, pinned)
        case = "b-0022b-gitops-no-base"
        proc = self.run_wrapper(wrapper=self.wrapper_with_case(case, pinned.replace(pin_line, "")), CASE=case)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(f"{case} sets gitops_switch_default_branch false and pins no base", proc.stderr)
        self.assertNotIn("switch_at_token_read", self.state())


class PinnedBasePreconditionTest(unittest.TestCase):
    """main.tf's plan-time preconditions on null_resource.run_branch, evaluated over its variables."""

    def conditions(self):
        text = _MAIN_TF.read_text()
        block = text[text.index('resource "null_resource" "run_branch"'):text.index("  triggers = {")]
        staged = json.loads(re.search(r"staged_history_tasks\s*=\s*(\[.*?\])", text).group(1))
        # The operators these conditions use, in Python.
        translated = []
        for condition in re.findall(r"condition\s*=\s*(.+)", block):
            expr = condition.replace("local.staged_history_tasks", repr(staged))
            expr = re.sub(r"var\.(\w+)", r'v["\1"]', expr)
            expr = expr.replace("&&", " and ").replace("||", " or ")
            expr = re.sub(r"!(?!=)", " not ", expr)
            translated.append(expr)
        return translated

    def admitted(self, **overrides):
        v = {"gitops_task": "b-0022b", "gitops_history_parent_sha": "", "gitops_pin_agent_base_branch": False,
             "gitops_switch_default_branch": False, "agent_host_context": "agent-host", **overrides}
        return all(eval(c, {"__builtins__": {}}, {"v": v, "contains": lambda items, item: item in items})
                   for c in self.conditions())

    def test_pin_on_a_task_with_staged_history_is_refused_at_plan(self):
        self.assertTrue(self.admitted(gitops_pin_agent_base_branch=True))
        self.assertFalse(self.admitted(gitops_pin_agent_base_branch=True, gitops_task="b-0011",
                                       gitops_history_parent_sha=_BASE_SHA))
        # run-branch.sh runs staged history for any task given a parent.
        self.assertFalse(self.admitted(gitops_pin_agent_base_branch=True, gitops_history_parent_sha=_BASE_SHA))
        self.assertTrue(self.admitted(gitops_task="b-0011", gitops_history_parent_sha=_BASE_SHA))


class RunPullRequestsTest(_StubbedScriptTest):
    """gitops-run-prs.sh, the wrapper's post-run pull-request listing."""

    stubs = {"gh": _STUB_GH}

    started = "2026-10-05T12:00:00Z"

    def pull(self, number, base, head, created_at):
        return {"number": number, "base": {"ref": base}, "head": {"ref": head},
                "html_url": f"https://github.com/{_SLUG}/pull/{number}", "created_at": created_at,
                "title": "ignored"}

    def run_listing(self):
        return self.run_script(["bash", str(_RUN_PRS), _SLUG, self.started, "main"], {"GH_TOKEN": "ghp_test"})

    def test_pull_requests_since_the_run_started_are_listed_with_their_bases(self):
        self.write_state({"pulls": [
            self.pull(3, "main", "platform-agent/fix-checkout-main", "2026-10-05T12:30:00Z"),
            self.pull(2, _RUN, "platform-agent/fix-checkout-run", "2026-10-05T12:00:00Z"),
            self.pull(1, "main", "platform-agent/earlier", "2026-10-05T11:59:59Z"),
        ]})
        proc = self.run_listing()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [
            {"number": 3, "base": "main", "head": "platform-agent/fix-checkout-main",
             "url": f"https://github.com/{_SLUG}/pull/3", "created_at": "2026-10-05T12:30:00Z"},
            {"number": 2, "base": _RUN, "head": "platform-agent/fix-checkout-run",
             "url": f"https://github.com/{_SLUG}/pull/2", "created_at": "2026-10-05T12:00:00Z"},
        ])
        self.assertIn(f"pull requests opened since {self.started}: 2, 1 onto main", proc.stderr)
        self.assertIn(f"#3 base main head platform-agent/fix-checkout-main https://github.com/{_SLUG}/pull/3",
                      proc.stderr)
        (call,) = self.calls()
        self.assertEqual(call[:1], ["api"])
        self.assertTrue(call[1].startswith(f"repos/{_SLUG}/pulls?state=all&sort=created&direction=desc"), call)

    def test_failed_listing_yields_null_not_an_empty_list(self):
        self.write_state({"fails": True})
        proc = self.run_listing()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), None)
        self.assertIn("WARN could not list the pull requests", proc.stderr)

    def test_no_pull_requests_since_the_start_is_an_empty_list(self):
        self.write_state({"pulls": [self.pull(1, "main", "platform-agent/earlier", "2026-10-04T09:00:00Z")]})
        proc = self.run_listing()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [])
        self.assertIn("2026-10-05T12:00:00Z: 0, 0 onto main", proc.stderr)


if __name__ == "__main__":
    unittest.main()
