#!/usr/bin/env python3
"""Mutual exclusion for a kube-agents install that several agents share.

`AGENTS.md` requires every pull request to be live-tested against a running
installation, and standing one up per contributor is not realistic -- so a team
shares one. Concurrent agents then clobber each other: two sessions patching the
operator env, pushing the same image tag, or re-running the installer produce a
last-writer-wins install and two live-validation sections that both describe
something that is no longer true. Nothing fails loudly; the losing agent reads
back the winner's state and reports success.

So a mutating command takes a lease first. The lease is a ConfigMap named
`live-test-lease` in the install's own namespace, which is what makes it work
across agents: it is visible to anyone holding a kubeconfig for that cluster --
another checkout, another machine, CI -- rather than to one workstation.

Holder identity is a random token minted at acquire time and kept locally under
$XDG_STATE_HOME (`~/.local/state` when that is unset). It is keyed on the session
id -- `KUBE_AGENTS_LEASE_SESSION` if the harness sets it, otherwise `CLAUDE_PID`,
otherwise the parent process id. A Claude Code session shares `CLAUDE_PID` with
its subagents but not with a second concurrent CLI process, so a subagent
inherits its parent's claim and a separate session does not. Another harness
should set `KUBE_AGENTS_LEASE_SESSION`; the parent-pid fallback is stable only
within one shell.

Which installs are protected is discovered, never hardcoded: the checkout's
`install.env` records the install it is pointed at (and a legacy
`k8s-operator/scripts/vars.sh` still counts, so a checkout from before the
change stays protected), and `$KUBE_AGENTS_LIVE_TEST_ENVS` (default
`$XDG_CONFIG_HOME/kube-agents/live-test-envs.json`, falling back to `~/.config`,
see `scripts/live_test_envs.example.json`) adds installs you protect but have no
checkout for. With neither present nothing is protected and the hook is a no-op.

This file is the source of truth for which commands count as mutations and for
which configuration keys are read; `admin_console/project_config.py` parses the
same files for a different purpose, and both must be updated if the on-disk
format changes. Both accept `K=V` and `export K=V`, because install.env is a
hand-authored dotenv and vars.sh was generated with `printf %q`.

Usage:
  live_test_lease.py status  [--env NAME] [--json]
  live_test_lease.py acquire [--env NAME] [--pr N] [--note TEXT] [--ttl MINUTES]
  live_test_lease.py renew   [--env NAME] [--ttl MINUTES]
  live_test_lease.py release [--env NAME] [--all]
  live_test_lease.py steal   [--env NAME] [--force]
  live_test_lease.py hook-pretooluse   # reads hook JSON on stdin
  live_test_lease.py hook-sessionend   # reads hook JSON on stdin

Design rationale: docs/designs/live-test-lease.md.
"""

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

CM_NAME = "live-test-lease"
DEFAULT_TTL_MIN = 60
# A PreToolUse hook that overruns its own timeout is killed, which Claude Code
# treats as a hook error and lets the command through -- so the worst case has
# to stay well inside the `timeout` set in .claude/settings.json. The hook makes
# at most three of these in a row (read the lease, then renew: read again and
# replace) plus one context probe, which is cached per classification because a
# compound line would otherwise pay for one probe per segment.
KUBECTL_TIMEOUT = 8

# SessionEnd `reason` values that do not end the session. Claude Code fires the
# event for `/clear` and `/resume` too, on a process that carries on with the
# same holder key -- so these are the two cases where the hook must leave the
# lease alone. Everything else (`prompt_input_exit`, `logout`, `other`) is a
# real exit.
SESSION_CONTINUES = frozenset({"clear", "resume"})

# The namespace an install uses unless its configuration says otherwise. Source
# of truth: NAMESPACE in scripts/installer/common.sh.
DEFAULT_NAMESPACE = "kubeagents-system"

# Only these keys are read out of an install's configuration. Both files are
# mode-600 and hold credentials as well as coordinates, and neither is ever
# sourced -- sourcing a file to read a handful of variables out of it executes
# everything else in there. No ZONE: the installer writes REGION for every
# install (installer_common.sh), and a zonal location would derive an Artifact
# Registry host that does not exist. CHAT_TOPIC_NAME becomes a marker: a
# `gcloud pubsub publish` to it drives a real agent turn, and it is the only
# part of that command that names the install when the project comes from
# `gcloud config`.
VARS_KEYS = ("PROJECT_ID", "CLUSTER_NAME", "REGION", "NAMESPACE",
             "REGISTRY_PREFIX", "CHAT_TOPIC_NAME")

# The two files an install's configuration can live in, relative to a checkout
# root. install.env is the hand-authored input; vars.sh is the generated state
# it replaced, still read so a checkout from before the change stays protected.
INSTALL_ENV_RELPATH = ("install.env",)
VARS_SH_RELPATH = ("k8s-operator", "scripts", "vars.sh")


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "kube-agents", "live-test-lease")


def config_path():
    explicit = os.environ.get("KUBE_AGENTS_LIVE_TEST_ENVS")
    if explicit:
        return os.path.expanduser(explicit)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "kube-agents", "live-test-envs.json")


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def human_delta(dt):
    """'in 43m' / '17m ago' -- expiry is the field people misread."""
    secs = (dt - now()).total_seconds()
    past = secs < 0
    secs = abs(secs)
    if secs < 90:
        chunk = "%ds" % int(secs)
    elif secs < 5400:
        chunk = "%dm" % round(secs / 60)
    else:
        chunk = "%.1fh" % (secs / 3600)
    return ("%s ago" % chunk) if past else ("in %s" % chunk)


# --------------------------------------------------------------------------
# The protected installs, discovered rather than configured
# --------------------------------------------------------------------------
class Install:
    """One kube-agents installation that must not be mutated concurrently."""

    def __init__(self, name, context, namespace=DEFAULT_NAMESPACE,
                 kubeconfig=None, markers=(), registry=None, label=None,
                 source="config", project=None, aliases=()):
        self.name = name
        self.context = context
        # `kubectl config rename-context` leaves an install answering to a name
        # the canonical gke_... string does not match, and an explicit
        # --context is otherwise a definite "not protected". Declarable in the
        # config file so a team that renames has a route out.
        self.contexts = (context,) + tuple(a for a in aliases if a)
        self.namespace = namespace or DEFAULT_NAMESPACE
        self.kubeconfig = os.path.expanduser(kubeconfig) if kubeconfig else None
        self.markers = tuple(m for m in markers if m)
        # Everything but the project name. Two installs in one project share
        # the project marker, so it cannot decide between them on its own.
        self.specific_markers = tuple(m for m in self.markers if m != project)
        self.registry = registry
        self.label = label or context
        self.source = source

    def add_aliases(self, aliases):
        """Extra names the classifier should read as this install."""
        self.contexts += tuple(a for a in aliases
                               if a and a not in self.contexts)

    def token_file(self):
        return os.path.join(state_dir(), "token-%s-%s" % (self.context, holder_key()))


def _split_context(context):
    """('project', 'region', 'cluster') out of a GKE context, or Nones.

    `gke_<project>_<location>_<cluster>` is the name `gcloud container clusters
    get-credentials` writes and the one installer_common.sh reconstructs to
    check you are pointed at your own install. None of the three parts may
    itself contain an underscore, so the split is unambiguous.
    """
    parts = (context or "").split("_")
    if len(parts) == 4 and parts[0] == "gke":
        return parts[1], parts[2], parts[3]
    return None, None, None


def _install_from_context(context, **overrides):
    """Build an Install, filling in what the context alone already tells us."""
    project, region, cluster = _split_context(context)
    name = overrides.pop("name", None) or cluster or context
    markers = overrides.pop("markers", None)
    if not markers:
        markers = [m for m in (project, cluster) if m]
    registry = overrides.pop("registry", None)
    if not registry and project and region:
        registry = "%s-docker.pkg.dev/%s" % (region, project)
    label = overrides.pop("label", None)
    if not label and project and cluster:
        label = "%s / %s (%s)" % (project, cluster, region)
    return Install(name=name, context=context, markers=markers,
                   registry=registry, label=label, project=project, **overrides)


_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(%s)=(.*)$" % "|".join(VARS_KEYS)
)

_REFERENCE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
)


def _expand(value, scope):
    """Substitute `$VAR` and `${VAR}` from keys the file has already set.

    The installers load these files with `set -a; . install.env; set +a`, and
    install.env.example advertises shell syntax -- so `CLUSTER_NAME=${PROJECT_ID}-host`
    is legal and the installers resolve it. Reading it literally instead is the
    silent failure this guard exists to prevent: the context below becomes
    `gke_myproj_us-central1_${PROJECT_ID}-host`, which matches no kubeconfig
    entry, and since context outranks markers the lease falls back to matching
    on PROJECT_ID alone -- so two installs in one project stop being told apart
    and each is free to take the other's lease.

    `scope` is the allowlisted keys resolved so far, in file order, so only
    those can be referenced. That is narrower than the shell, which would also
    expand from its own environment and from any other assignment in the file;
    both are deliberate. Reading the environment would make protection depend on
    the shell a command happened to run in, and keeping non-allowlisted values
    out of `scope` keeps the API keys and tokens these files also hold out of
    this function entirely.

    A reference `scope` cannot resolve is left as written rather than dropped:
    the literal is the pre-expansion behaviour, whereas dropping it would fail
    the `project and cluster and location` check below and turn a protected
    install into an undiscovered one -- trading a degraded match for no
    protection at all.

    `admin_console/project_config.py` carries the same expansion for the same
    files; change both together.
    """
    return _REFERENCE.sub(
        lambda m: scope.get(m.group(1) or m.group(2), m.group(0)), value
    )


def _parse_install_state(text, scope=None):
    """The allowlisted coordinates out of an install configuration, unquoted.

    Accepts both spellings, because the two files differ: vars.sh is generated
    with `printf %q` and carries `export K=V`, while install.env is a
    hand-authored `K=V` dotenv. A parser that insisted on `export` would read
    nothing out of install.env and report the install as unprotected -- silently,
    which is the failure mode this whole guard exists to prevent.

    A plain identifier arrives bare and anything else arrives quoted. shlex
    unquotes both; a value it cannot parse is dropped rather than guessed at.

    Lines are read in order and a later assignment to the same key wins, as it
    would when the shell sources the file. `scope` accumulates across the call
    so a later assignment can reference an earlier one; pass the same dict for
    vars.sh and install.env to let the second reference the first, matching the
    order the front doors source them in.
    """
    found = {}
    if scope is None:
        scope = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        try:
            raw = match.group(2).strip()
            parts = shlex.split(raw)
        except ValueError:
            continue
        if not parts:
            continue
        # Single quotes suppress expansion in the shell, so they suppress it
        # here. Testing the raw value rather than the parsed one is what keeps
        # that true: shlex has already removed the quotes by now.
        value = parts[0] if "'" in raw else _expand(parts[0], scope)
        found[match.group(1)] = value
        scope[match.group(1)] = value
    return found


def find_install_state(cwd):
    """(install_env, vars_sh) for the checkout governing `cwd`; either may be None.

    Walks up because commands run from anywhere in the checkout, and the
    installers act on the install the configuration names whatever the working
    directory or the current kubectl context happens to be.

    The walk runs to the filesystem root rather than a fixed number of levels.
    A depth limit turns a deep working directory -- `deploy/docker/plugins/x/`
    is already seven down -- into silently unprotected, which is
    indistinguishable from the intended "nothing configured" state.

    Both files are returned from the FIRST level that has either, rather than
    each being searched independently: a checkout mid-migration has both, and
    they describe one install. Taking install.env from one level and a vars.sh
    from a parent checkout would invent a third.
    """
    if not cwd:
        return (None, None)
    path = os.path.abspath(cwd)
    while True:
        install_env = os.path.join(path, *INSTALL_ENV_RELPATH)
        vars_sh = os.path.join(path, *VARS_SH_RELPATH)
        has_env = os.path.isfile(install_env)
        has_vars = os.path.isfile(vars_sh)
        if has_env or has_vars:
            return (install_env if has_env else None,
                    vars_sh if has_vars else None)
        parent = os.path.dirname(path)
        if parent == path:
            return (None, None)
        path = parent


def find_vars_sh(cwd):
    """The single path that names the install governing `cwd`, or None.

    install.env when there is one, since it is the input and wins on every key.
    Callers use this as the install's identity, so it must be stable for a
    given checkout rather than switching between the two files.
    """
    install_env, vars_sh = find_install_state(cwd)
    return install_env or vars_sh


def _read_install_state(path, scope=None):
    if not path:
        return {}
    try:
        with open(path) as fh:
            return _parse_install_state(fh.read(), scope)
    except OSError:
        return {}


def _install_from_state(install_env, vars_sh):
    """One Install merged from whichever of the two files exist.

    vars.sh first, install.env over the top: the hand-authored input wins,
    matching the order every shell front door loads them in. The two share one
    expansion scope for the same reason, so a `$VAR` in install.env can name a
    key vars.sh set.
    """
    scope = {}
    fields = _read_install_state(vars_sh, scope)
    fields.update(_read_install_state(install_env, scope))
    if not fields:
        return None
    project = fields.get("PROJECT_ID")
    cluster = fields.get("CLUSTER_NAME")
    location = fields.get("REGION")
    if not (project and cluster and location):
        return None
    markers = [project, cluster]
    if fields.get("CHAT_TOPIC_NAME"):
        markers.append(fields["CHAT_TOPIC_NAME"])
    return _install_from_context(
        "gke_%s_%s_%s" % (project, location, cluster),
        namespace=fields.get("NAMESPACE") or DEFAULT_NAMESPACE,
        registry=fields.get("REGISTRY_PREFIX"),
        markers=markers,
        source=install_env or vars_sh,
    )


def _installs_from_config():
    path = config_path()
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except OSError:
        return []
    except ValueError as exc:
        print("ignoring unreadable %s: %s" % (path, exc), file=sys.stderr)
        return []
    entries = raw.get("installs", []) if isinstance(raw, dict) else raw
    out = []
    for entry in entries or []:
        context = (entry or {}).get("context")
        if not context:
            continue
        out.append(_install_from_context(
            context,
            name=entry.get("name"),
            namespace=entry.get("namespace"),
            kubeconfig=entry.get("kubeconfig"),
            markers=entry.get("markers"),
            aliases=entry.get("aliases") or (),
            registry=entry.get("registry"),
            label=entry.get("label"),
            source=path,
        ))
    return out


def resolve_installs(cwd=None, also=None):
    """Every install this invocation must protect, keyed by name.

    The checkout's own install comes first and wins a collision: a contributor
    editing a checkout is acting on the install that checkout is pointed at,
    whatever a config file written months ago says.

    `also` is a second directory to discover from -- where a `cd` on the
    command line lands. `cd <other-checkout> && ./upgrade.sh` reconfigures that
    checkout's install, and an install missing from this set cannot be resolved
    by anything downstream: the classifier falls back to the ambient context
    and takes the session's lease while the installer rewrites another install.

    Identity is the context, not the name. Two clusters called `agents` in
    different projects are two installs, and keying on the name would drop one
    of them -- unprotected, unnameable by `--env`, and silent about it. The
    loser of a name collision is renamed to `<project>/<name>` instead.
    """
    installs, by_context = {}, {}
    discovered = []
    for where in (cwd or os.environ.get("CLAUDE_CWD") or os.getcwd(), also):
        install_env, vars_sh = find_install_state(where) if where else (None, None)
        found = _install_from_state(install_env, vars_sh)
        if found:
            discovered.append(found)
    for install in discovered + _installs_from_config():
        seen = by_context.get(install.context)
        if seen is not None:
            # The checkout wins every field it discovered, but a config entry
            # for the same install still contributes its aliases. Dropping the
            # entry whole is how the documented remedy for a locally renamed
            # context stopped working for the install most likely to need it:
            # the one you have a checkout of.
            seen.add_aliases(install.contexts[1:])
            continue
        by_context[install.context] = install
        if install.name in installs:
            project = _split_context(install.context)[0]
            install.name = "%s/%s" % (project or install.context, install.name)
            if install.name in installs:
                continue
        installs[install.name] = install
    return installs


def _resolvable_installs(cwd=None):
    """`resolve_installs`, for callers that have something to do either way.

    Release is the case: an unreadable config file or a malformed install configuration is a
    reason to lose the nicer labels, not a reason to leave a held lease on a
    shared cluster until it expires.
    """
    try:
        return resolve_installs(cwd)
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------
# Local holder token. Keyed on the session so subagents share the claim.
# --------------------------------------------------------------------------
def holder_key():
    return (os.environ.get("KUBE_AGENTS_LEASE_SESSION")
            or os.environ.get("CLAUDE_PID")
            or ("nopid-%d" % os.getppid()))


def _decode_token(raw):
    """The token file's contents as a dict.

    Older files held the bare token hex and nothing else. One left behind by a
    session that is still running has to keep working, so a payload that is not
    JSON is read as the token alone -- which is all `do_release` strictly needs
    once the install is in hand, and the only thing the reader can be sure of.
    """
    if raw.startswith("{"):
        try:
            record = json.loads(raw)
        except ValueError:
            return {}
        return record if isinstance(record, dict) else {}
    return {"token": raw}


def _read_token_record(install):
    try:
        with open(install.token_file()) as fh:
            return _decode_token(fh.read().strip())
    except OSError:
        return {}


def read_local_token(install):
    return _read_token_record(install).get("token")


def write_local_token(install, token):
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    # Where the lease was taken, not just what it was taken with. The session
    # that has to release it may no longer be standing anywhere that can
    # discover this install, and a reconstruction that guessed DEFAULT_NAMESPACE
    # would delete nothing while reporting success.
    record = {"token": token, "context": install.context, "name": install.name,
              "namespace": install.namespace}
    if install.kubeconfig:
        record["kubeconfig"] = install.kubeconfig
    path = install.token_file()
    with open(path, "w") as fh:
        json.dump(record, fh)
    # The token is a capability: whoever reads it can renew or drop the lease.
    os.chmod(path, 0o600)


def clear_local_token(install):
    try:
        os.remove(install.token_file())
    except OSError:
        pass


def held_installs(known=None):
    """The installs this session holds a token for, keyed by name.

    `resolve_installs` answers a different question -- what the directory you
    are standing in protects. A session that ran `cd ../other-checkout &&
    ./upgrade.sh` took a lease the hook discovered from *there*, and by
    `SessionEnd` the cwd is back to somewhere that cannot see it. Iterating the
    resolvable installs releases the wrong set: the one you are standing in
    (which you may not hold) and not the one you do, which then sits held until
    it expires an hour later and blocks every other agent in the meantime.

    The token files are the record of what was actually claimed, so they are
    what this reads. `known` supplies the discovered Install where the checkout
    has one and it agrees with where the lease was taken.
    """
    suffix = "-%s" % holder_key()
    by_context = {i.context: i for i in (known or {}).values()}
    out = {}
    try:
        names = sorted(os.listdir(state_dir()))
    except OSError:
        return out
    for filename in names:
        if not filename.startswith("token-") or not filename.endswith(suffix):
            continue
        try:
            with open(os.path.join(state_dir(), filename)) as fh:
                record = _decode_token(fh.read().strip())
        except OSError:
            continue
        if not record.get("token"):
            continue
        context = record.get("context") or filename[len("token-"):-len(suffix)]
        if not context:
            continue
        namespace = record.get("namespace") or DEFAULT_NAMESPACE
        seen = by_context.get(context)
        # The discovered Install carries a label and markers the reconstruction
        # cannot, but only where it points at the same namespace the lease was
        # taken in. Where they disagree the record wins: it says where the
        # ConfigMap really is.
        if seen is not None and seen.namespace == namespace:
            out[seen.name] = seen
            continue
        install = _install_from_context(
            context, name=record.get("name") or None, namespace=namespace,
            kubeconfig=record.get("kubeconfig") or None, source="token")
        out[install.name] = install
    return out


# --------------------------------------------------------------------------
# Cluster I/O
# --------------------------------------------------------------------------
def kubectl(install, args, stdin=None):
    cmd = ["kubectl", "--context", install.context, "-n", install.namespace] + args
    kenv = dict(os.environ)
    if install.kubeconfig:
        kenv["KUBECONFIG"] = install.kubeconfig
    try:
        proc = subprocess.run(
            cmd, input=stdin, capture_output=True, text=True,
            timeout=KUBECTL_TIMEOUT, env=kenv,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "kubectl timed out after %ds" % KUBECTL_TIMEOUT
    except FileNotFoundError:
        return 127, "", "kubectl not found on PATH"
    return proc.returncode, proc.stdout, proc.stderr


class Unreachable(Exception):
    """The cluster could not be consulted -- distinct from 'lease is free'."""


# The one failure that means the lease is free, and nothing else. A substring
# scan for "not found" is far wider than it looks: a missing namespace, an
# absent gke-gcloud-auth-plugin, and this module's own "kubectl not found on
# PATH" all say it, and reading any of them as an unheld lease turns a cluster
# we cannot see into one we believe is idle -- the single answer this tool
# must never give by accident.
CM_ABSENT_RE = re.compile(r'configmaps?\s+"%s"\s+not found' % re.escape(CM_NAME))


def get_lease(install):
    """Returns (data_dict, resource_version) or (None, None) if unheld."""
    rc, out, err = kubectl(install, ["get", "configmap", CM_NAME, "-o", "json"])
    if rc == 0:
        obj = json.loads(out)
        return obj.get("data", {}), obj["metadata"]["resourceVersion"]
    if CM_ABSENT_RE.search(err):
        return None, None
    raise Unreachable(err.strip().splitlines()[-1] if err.strip() else "rc=%d" % rc)


def lease_is_live(data):
    if not data:
        return False
    exp = parse_iso(data.get("expiresAt", ""))
    return bool(exp and exp > now())


def describe(data):
    who = data.get("holder", "unknown")
    bits = []
    if data.get("branch"):
        bits.append("branch %s" % data["branch"])
    if data.get("pr"):
        bits.append("PR #%s" % data["pr"])
    if data.get("note"):
        bits.append(data["note"])
    tail = (" (%s)" % ", ".join(bits)) if bits else ""
    exp = parse_iso(data.get("expiresAt", ""))
    when = ("expires %s" % human_delta(exp)) if exp else "no expiry recorded"
    if exp and exp <= now():
        when = "EXPIRED %s" % human_delta(exp)
    return "%s%s -- claimed %s, %s" % (who, tail, data.get("acquiredAt", "?"), when)


def build_manifest(install, data, resource_version=None):
    meta = {"name": CM_NAME, "namespace": install.namespace}
    if resource_version:
        meta["resourceVersion"] = resource_version
    return json.dumps({
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": meta, "data": data,
    })


def context_fields(pr=None, note=None, ttl=DEFAULT_TTL_MIN):
    cwd = os.environ.get("CLAUDE_CWD") or os.getcwd()
    branch = ""
    try:
        rc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if rc.returncode == 0:
            branch = rc.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    started = now()
    data = {
        "token": uuid.uuid4().hex,
        "holder": "%s@%s" % (os.environ.get("USER", "unknown"), socket.gethostname()),
        "cwd": cwd,
        "branch": branch,
        "session": holder_key(),
        "acquiredAt": iso(started),
        "expiresAt": iso(started + timedelta(minutes=ttl)),
        "ttlMinutes": str(ttl),
    }
    if pr:
        data["pr"] = str(pr)
    if note:
        data["note"] = note
    return data


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------
def do_acquire(install, pr=None, note=None, ttl=DEFAULT_TTL_MIN, steal_expired=True):
    """Returns (ok, message). Atomic against concurrent acquirers."""
    mine = read_local_token(install)
    data, rv = get_lease(install)

    if data and mine and data.get("token") == mine and lease_is_live(data):
        return do_renew(install, ttl)

    if lease_is_live(data) and (not mine or data.get("token") != mine):
        return False, describe(data)

    fresh = context_fields(pr, note, ttl)
    if data is None:
        # create fails with AlreadyExists if another agent won the race
        rc, _, err = kubectl(install, ["create", "-f", "-"],
                             stdin=build_manifest(install, fresh))
        if rc != 0:
            if "AlreadyExists" in err:
                other, _ = get_lease(install)
                return False, describe(other or {})
            return False, "could not create lease: %s" % err.strip()
    else:
        if not steal_expired:
            return False, describe(data)
        # replace with resourceVersion: rejected if anyone touched it since the read
        rc, _, err = kubectl(install, ["replace", "-f", "-"],
                             stdin=build_manifest(install, fresh, rv))
        if rc != 0:
            if "Conflict" in err or "conflict" in err:
                other, _ = get_lease(install)
                return False, "lost the race for an expired lease: %s" % describe(other or {})
            return False, "could not take over expired lease: %s" % err.strip()

    write_local_token(install, fresh["token"])
    return True, describe(fresh)


def do_renew(install, ttl=DEFAULT_TTL_MIN):
    mine = read_local_token(install)
    data, rv = get_lease(install)
    if not data:
        return False, "no lease to renew"
    if not mine or data.get("token") != mine:
        return False, "not the holder: %s" % describe(data)
    data = dict(data)
    data["expiresAt"] = iso(now() + timedelta(minutes=ttl))
    rc, _, err = kubectl(install, ["replace", "-f", "-"],
                         stdin=build_manifest(install, data, rv))
    if rc != 0:
        return False, "renew failed: %s" % err.strip()
    return True, describe(data)


def do_release(install):
    mine = read_local_token(install)
    try:
        data, _ = get_lease(install)
    except Unreachable as exc:
        clear_local_token(install)
        return False, "cluster unreachable (%s); cleared the local token only" % exc
    if not data:
        clear_local_token(install)
        return True, "already free"
    if not mine or data.get("token") != mine:
        return False, "not yours to release: %s" % describe(data)
    rc, _, err = kubectl(install, ["delete", "configmap", CM_NAME, "--ignore-not-found"])
    clear_local_token(install)
    if rc != 0:
        return False, "delete failed: %s" % err.strip()
    return True, "released"


def do_steal(install, force=False):
    data, rv = get_lease(install)
    if not data:
        return do_acquire(install)
    if lease_is_live(data) and not force:
        return False, (
            "still live -- %s\nCoordinate with the holder, or re-run with --force "
            "if you are certain that session is gone." % describe(data)
        )
    fresh = context_fields()
    rc, _, err = kubectl(install, ["replace", "-f", "-"],
                         stdin=build_manifest(install, fresh, rv))
    if rc != 0:
        return False, "steal failed: %s" % err.strip()
    write_local_token(install, fresh["token"])
    return True, "stolen -- %s" % describe(fresh)


# --------------------------------------------------------------------------
# Command classification
# --------------------------------------------------------------------------
# A flag whose value is missing from this set becomes the first positional, and
# the subcommand is then read off the wrong token. On kubectl that fails safe --
# an unrecognised verb is treated as mutating -- but helm's branch acts only on
# a recognised subcommand, so `helm --kube-context X upgrade ...` would read as
# a read. Keep helm's global flags here.
FLAGS_WITH_VALUES = {
    "-n", "--namespace", "--context", "--kubeconfig", "-f", "--filename",
    "-o", "--output", "-l", "--selector", "-c", "--container", "--type",
    "-p", "--patch", "--image", "--replicas", "--timeout", "--for",
    "--kube-context", "--kube-apiserver", "--kube-token", "--kube-as-user",
    "--kube-as-group", "--kube-ca-file", "--kube-tls-server-name",
    "--registry-config", "--repository-config", "--repository-cache",
    "--burst-limit", "--qps",
}

# `auth` is in the read-only set for `auth can-i`, but `auth reconcile` creates
# and updates RBAC objects in the cluster, so the top-level verb is not the
# whole answer.
KUBECTL_READONLY_EXCEPTIONS = {("auth", "reconcile")}

# The inverse: verbs in the mutating set whose subcommand only reads. `rollout
# status` is how you watch a deploy you did not start, and denying it -- or
# taking an hour-long lease for it -- is the false positive that gets the hook
# switched off.
KUBECTL_MUTATING_EXCEPTIONS = {("rollout", "status"), ("rollout", "history")}

# `--dry-run=client` renders locally and `--dry-run=server` asks the API server
# to validate without persisting. Neither changes the install. `--dry-run=none`
# is the explicit spelling of a real write.
DRY_RUN_READONLY = {"--dry-run", "--dry-run=client", "--dry-run=server",
                    "--dry-run=true"}

KUBECTL_READONLY = {
    "get", "describe", "logs", "log", "top", "explain", "api-resources",
    "api-versions", "version", "cluster-info", "auth", "diff", "events",
    "config", "whoami", "kustomize", "wait",
}

# exec/cp/port-forward are not reads: they reach inside the running install.
KUBECTL_MUTATING = {
    "apply", "patch", "edit", "delete", "create", "replace", "scale", "set",
    "rollout", "annotate", "label", "cordon", "uncordon", "drain", "taint",
    "exec", "cp", "port-forward", "run", "attach", "debug", "expose",
    "autoscale", "rollback",
}

HELM_MUTATING = {"install", "upgrade", "uninstall", "delete", "rollback"}

GCLOUD_CLUSTER_MUTATING = {"create", "delete", "update", "upgrade", "resize"}

# `state` is deliberately absent: `terraform state list` is a read, and the
# state subcommands that do write touch the state file rather than the install.
TERRAFORM_MUTATING = {"apply", "destroy", "import", "taint", "untaint"}

# Repo entry points that reconfigure an install wholesale. Source of truth for
# the make targets: the root Makefile and k8s-operator/Makefile -- every target
# there that applies manifests, runs the controller against a cluster, or
# pushes an image. test_live_test_lease.py re-derives both families from those
# two Makefiles, because a `deploy-*` target added later would otherwise ship
# unguarded past a green suite.
INSTALLER_SCRIPTS = {
    "install.sh", "uninstall.sh", "upgrade.sh", "lifecycle.sh",
    "dev_rebuild_agent.sh",
}
MAKE_TARGETS_MUTATING = {
    "deploy", "undeploy", "install", "uninstall", "run", "dev-rebuild-agent",
    "docker-push", "mirror-images", "tf-apply", "tf-destroy",
}
MAKE_TARGET_PREFIXES = ("deploy-", "undeploy-", "docker-push-")

# Installer flags that print and exit without touching the install. Taking an
# hour-long lease on a shared cluster for `./install.sh --help` is the same
# false positive as classifying `cat install.sh` as a run.
#
# `--plan` belongs here for the same reason `--dry-run` does, and is the one
# that needs saying out loud: it reads the install's real Terraform state and
# so looks like the mutating path, but it takes no state lock, creates no state
# bucket, runs none of the adoption imports, and skips the Session KV backfill.
# What it does write is local to the checkout -- terraform.tfvars and
# backend_override.tf -- which the lease does not govern.
INSTALLER_NOOP_FLAGS = {"-h", "--help", "-?", "--dry-run", "--plan"}

# Prefixes that wrap a command without changing what it runs. Stripping them is
# what lets the classifier look at the command word rather than at every token
# on the line -- `grep -rn install.sh docs/` reads about the installer.
#
# `xargs` is here for the bulk-delete idiom: `kubectl get pods -o name | xargs
# kubectl delete pod` puts the mutation after a command word of its own.
WRAPPER_WORDS = {"sudo", "nohup", "command", "exec", "nice", "ionice", "stdbuf",
                 "time", "doas", "xargs"}
WRAPPER_VALUE_FLAGS = {
    "xargs": {"-n", "-P", "-I", "-L", "-d", "-s", "-E", "-a", "--max-args",
              "--max-procs", "--replace", "--delimiter", "--arg-file"},
    # `timeout -s KILL 30 kubectl delete ...` -- without these the flag's value
    # is mistaken for the duration and `30` becomes the command word.
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
}

# Shell syntax that precedes a command word without being one. `for ns in a b;
# do kubectl delete ns $ns; done` splits into a segment whose first token is
# `do`, and the bulk-cleanup loop is the most ordinary thing an agent writes.
# `()` is here for `function f () {`, where the space leaves the parens as a
# token of their own once the `function` branch has consumed the name.
SHELL_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "for",
                  "!", "{", "}", "()"}
SHELL_WORDS = {"bash", "sh", "zsh", "dash", "ksh", "busybox"}
ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# `f() { kubectl delete ns x; }; f` -- the definition's name is where the
# command word would otherwise be, and `{` right behind it is already a keyword.
FUNCTION_DEF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\)$")
# The same definition with a space in it, which POSIX also allows: `f () {`.
# shlex hands back `f` and `()` separately, so the name has to be recognised
# by what follows it rather than by its own shape.
FUNCTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DASH_C_RE = re.compile(r"^-[a-zA-Z]*c$")
# `OUT=$(kubectl apply ...)` -- the assignment swallows the command word, so
# the substitution is pulled out and classified in its own right.
SUBSTITUTION_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
CD_RE = re.compile(r"^\s*cd\s")
# `<<EOF`, `<<-'EOF'`, `<<"EOF"` -- but not `<<<`, a here-string. Both guards
# are needed: the lookahead rejects `<<<yes` at the `<` it starts on, and the
# lookbehind rejects the same string at the next offset, where a plain search
# retries and finds a `<<` followed by a perfectly good delimiter word.
HEREDOC_RE = re.compile(
    r"(?<!<)<<-?[ \t]*(?![<(])(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

# `bash -c "bash -c ..."` terminates on its own because the payload shrinks,
# but a hostile or generated line should not be able to spin the hook.
MAX_WRAPPER_DEPTH = 4


def strip_heredocs(command):
    """The command with any heredoc bodies removed.

    A heredoc body is data, not commands, and this repository's own workflow
    puts commands in one: `AGENTS.md` requires a **Live validation** section
    naming what was run, so `gh pr create --body "$(cat <<'EOF' ... EOF)"`
    carries a `kubectl patch` on a line of its own. Splitting on newlines
    without this classifies opening a pull request as that patch -- taking an
    hour-long lease on a shared cluster, or being denied while somebody else
    holds one, for a command that touches nothing.

    Only the body goes. What follows the introducer on its own line is
    command text, and the pipeline `kubectl apply` is most often written into
    is exactly that: `kubectl apply -f -` sits after the `<<EOF`, not before
    it. Cutting the introducer's whole line loses the mutation.

    `<<<` is a here-string, not a heredoc, and is left alone.
    """
    while True:
        m = HEREDOC_RE.search(command)
        if not m:
            return command
        nl = command.find("\n", m.end())
        if nl == -1:
            # An introducer whose body is not in this string at all. Everything
            # after it is still command text, so drop the introducer alone.
            command = command[:m.start()] + command[m.end():]
            continue
        rest = command[nl + 1:]
        term = re.search(r"^[ \t]*%s[ \t]*$" % re.escape(m.group(2)), rest,
                         re.MULTILINE)
        end = nl + 1 + (term.end() if term else len(rest))
        command = command[:m.start()] + command[m.end():nl + 1] + command[end:]


def split_segments(command):
    """Break a compound shell line into individually-classifiable pieces.

    A lone `&` backgrounds the segment before it and starts another, so it
    separates commands exactly as `;` does. `&&` and `||` are matched before
    the single characters so they are not torn in half.

    Separators inside quotes are text: `gh pr create --body "we ran; kubectl
    delete ns x"` is one command, and splitting through the quote finds a
    `kubectl delete` nobody typed. An unterminated quote therefore swallows the
    rest of the line -- which the shell would reject anyway.

    Grouping syntax is stripped from the ends, so the command word of
    `(cd /tmp && ./install.sh)` is the installer rather than `./install.sh)`.

    A `$(...)` is one piece, however many separators are inside it. Splitting
    through it strands the opening `$(` on a fragment that `SUBSTITUTION_RE`
    can no longer match, and the `VAR=$(cmd` left at the front is an assignment
    as far as `strip_wrappers` is concerned -- so the command word goes with
    it. `OUT=$(kubectl delete ns x 2>&1)` classified as nothing that way, and
    `VAR=$(cmd 2>&1 || echo "")` is an idiom this repository's own scripts are
    full of. The contents are still classified: `classify` recurses into the
    substitution once the segment survives whole.

    An `&` that belongs to a redirection is not a separator either -- `2>&1`
    ends a command, it does not background one.
    """
    out, buf, quote, depth, i = [], [], None, 0, 0
    command = strip_heredocs(command)

    def redirection_ampersand(at):
        """Is the `&` at `at` part of `2>&1`, `>&2`, or `&>log`?"""
        if command[at + 1:at + 2] == ">":
            return True
        before = "".join(buf).rstrip()
        return before.endswith(">") or before.endswith("<")

    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote != "'" and i + 1 < len(command):
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command[i:i + 2] == "$(":
            depth += 1
            buf.append(command[i:i + 2])
            i += 2
            continue
        if depth:
            # Inside a substitution nothing separates, but the parens still
            # have to be counted so `$(a $(b) c)` ends where it really ends.
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            buf.append(ch)
            i += 1
            continue
        if command[i:i + 2] in ("&&", "||"):
            out.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == "&" and redirection_ampersand(i):
            buf.append(ch)
            i += 1
            continue
        if ch in ";\n|&":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [seg for seg in (_ungroup(s) for s in out) if seg]


def _ungroup(segment):
    """A segment without the grouping syntax wrapped around it.

    Leading `(` and `{` can never begin a command word, so they go
    unconditionally. A trailing `)` goes only when the segment has more of them
    than it opened -- `kubectl patch -p '{"replicas":1}'` closes its own, and
    trimming that would corrupt the argument rather than reveal the command.
    """
    seg = segment.strip().lstrip("({").strip()
    while seg.endswith(")") and seg.count(")") > seg.count("("):
        seg = seg[:-1].strip()
    return seg


def strip_wrappers(tokens):
    """The tokens of the command actually being run.

    Drops leading `VAR=value` assignments, shell keywords, and prefix
    wrappers, so `sudo env FOO=1 timeout 30 kubectl delete ...` and `do kubectl
    delete ...` both present as `kubectl delete ...`.
    """
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if (ASSIGNMENT_RE.match(tok) or tok in SHELL_KEYWORDS
                or FUNCTION_DEF_RE.match(tok)):
            i += 1
            continue
        # `f () { ... }` -- one token later the parens say this was a
        # definition, not the command word it otherwise looks exactly like.
        if (tokens[i + 1:i + 2] == ["()"] and FUNCTION_NAME_RE.match(tok)):
            i += 2
            continue
        # `function f { ... }` and `function f() { ... }` are the other two
        # spellings; the name is a bare word the loop would otherwise stop on.
        if tok == "function":
            i += 2 if i + 1 < len(tokens) else 1
            continue
        base = os.path.basename(tok)
        if base == "env":
            i += 1
            while i < len(tokens) and (ASSIGNMENT_RE.match(tokens[i])
                                       or tokens[i].startswith("-")):
                i += 1
            continue
        if base == "timeout":
            takes_value = WRAPPER_VALUE_FLAGS["timeout"]
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                skip_value = tokens[i] in takes_value
                i += 1
                if skip_value and i < len(tokens):
                    i += 1
            i += 1  # the duration
            continue
        if base in WRAPPER_WORDS:
            takes_value = WRAPPER_VALUE_FLAGS.get(base, frozenset())
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                skip_value = tokens[i] in takes_value
                i += 1
                if skip_value and i < len(tokens):
                    i += 1
            continue
        break
    return tokens[i:]


def shell_payloads(head):
    """The command strings a `bash -c '...'` or `eval` wrapper will go on to run.

    shlex collapses the payload into a single token, so without this a shell
    wrapper hides every marker and verb inside it from the classifier.

    `eval` is the same hiding place reached without a flag: it takes no `-c`,
    and everything after it is the command whether it arrived as one quoted
    token or as several bare ones. Joining covers both -- the recursive
    `classify` re-splits what it is handed, so the quoting lost here is
    quoting it was going to redo anyway.
    """
    if not head:
        return []
    if os.path.basename(head[0]) == "eval":
        return [" ".join(head[1:])] if len(head) > 1 else []
    if os.path.basename(head[0]) not in SHELL_WORDS:
        return []
    out = []
    for i, tok in enumerate(head):
        if DASH_C_RE.match(tok) and i + 1 < len(head):
            out.append(head[i + 1])
    return out


def noop_invocation(args):
    """Is this an installer being asked about rather than run?

    Matched in flag position only. Scanning the whole argument list for the
    members of `INSTALLER_NOOP_FLAGS` reads a flag's *value* as the flag --
    `./install.sh --note help` is a real run -- and that is a silent pass on
    the family of commands that reconfigures an install wholesale.
    """
    for tok in args:
        if tok.startswith("-") and tok.split("=", 1)[0] in INSTALLER_NOOP_FLAGS:
            return True
    return args[:1] == ["help"]


def entry_point(head):
    """The installer entry point this segment runs, or None.

    Matched on the command word -- and, for a shell or `make`, on the script or
    target it was handed -- rather than anywhere on the line. `grep -rn
    install.sh docs/` and `cat upgrade.sh` read about the installers; taking a
    60-minute lease on a shared cluster for them is worse than not having one.
    """
    if not head:
        return None
    if noop_invocation(head[1:]):
        return None
    base = os.path.basename(head[0])
    if base in INSTALLER_SCRIPTS:
        return base
    if base in SHELL_WORDS:
        for tok in head[1:]:
            if tok.startswith("-"):
                continue
            return os.path.basename(tok) if os.path.basename(tok) in INSTALLER_SCRIPTS else None
    if base == "make":
        for tok in head[1:]:
            if tok.startswith("-") or "=" in tok:
                continue
            if tok in MAKE_TARGETS_MUTATING or tok.startswith(MAKE_TARGET_PREFIXES):
                return "make %s" % tok
    return None


def plugin_installer(head):
    """Is this an `agentplugins/*/install.sh` rather than a root installer?

    They share a basename and nothing else. A plugin installer applies an
    AgentPlugin CR and a chart through `$KUBECTL_CONTEXT` or the current
    context, and never reads the checkout's install configuration -- so resolving it through the checkout,
    the way the root installers must be resolved, would take one install's
    lease while the command mutated another.
    """
    return any("agentplugins/" in tok and tok.endswith(".sh") for tok in head)


def positionals(args):
    """The positional arguments in `args`, skipping flags and their values."""
    out = []
    skip = False
    for tok in args:
        if skip:
            skip = False
            continue
        if tok in FLAGS_WITH_VALUES:
            skip = True
            continue
        if tok.startswith("-"):
            continue
        out.append(tok)
    return out


def positional(args):
    """The subcommand: first positional argument, or None."""
    found = positionals(args)
    return found[0] if found else None


def image_refs(subs, args):
    """The image references a docker push acts on.

    Both spellings: the positionals after `push`, and the `-t` values a
    `docker buildx build --push` tags instead.
    """
    refs = []
    if "push" in subs[:2]:
        refs += subs[subs.index("push") + 1:]
    for i, tok in enumerate(args):
        if tok in ("-t", "--tag") and i + 1 < len(args):
            refs.append(args[i + 1])
        elif tok.startswith("--tag="):
            refs.append(tok.split("=", 1)[1])
    return refs


def unreadable_registry(ref, registries):
    """Does this ref hide which protected registry it pushes to?

    A recorded registry prefix is more than a host -- `us-central1-docker.pkg
    .dev/acme-prod`, host and project -- so judging the host alone leaves the
    spelling an agent is most likely to write wide open. The install
    configuration carries `PROJECT_ID`, and one that sourced it writes `docker push
    us-central1-docker.pkg.dev/$PROJECT_ID/platform:dev`: the literal
    substring match below finds no registry on the line, the host expanded
    fine, and the push overwrites the tag the shared install runs with no
    lease taken and no deny.

    So the question is asked of everything the ref spells out, not of its
    first segment: unreadable when the literal half -- up to the first token
    that did not expand -- is still on the path to a protected registry.
    `ghcr.io/x/y:$(git rev-parse HEAD)` computes its tag and still says
    plainly it is going elsewhere, and no protected registry starts with
    `ghcr.io/x/y:`, so it stays silent. Prompting on that instead is the
    false positive that costs an agent an hour.
    """
    if unexpanded(ref.split("/", 1)[0]):
        return True
    if not unexpanded(ref):
        # Fully readable. Whether it is a protected registry is then the
        # literal match's question, and answering it here would prompt on a
        # ref that names one outright.
        return False
    literal = re.split(r"[$`]", ref, maxsplit=1)[0]
    return any(registry.startswith(literal) for registry in registries)


def install_from_markers(installs, text):
    """The install a command names, preferring its most specific marker.

    Two installs in one project share the project marker, so a first-match
    scan would take one install's lease and then let the command hit the
    other. Cluster-level markers therefore decide first, and only a line that
    names no cluster falls back to the project.

    A tie between two installs is no answer. Installs that took the default
    chat topic name share that marker exactly the way they share the project,
    and picking whichever came first out of a dict is how the lease ends up on
    the wrong cluster.
    """
    for attr in ("specific_markers", "markers"):
        best, best_len = None, 0
        for install in installs.values():
            for marker in getattr(install, attr, ()):
                if marker not in text:
                    continue
                if len(marker) > best_len:
                    best, best_len = install, len(marker)
                elif len(marker) == best_len and best is not install:
                    best = None
        if best:
            return best
    return None


def install_for_context(installs, ctx):
    for install in installs.values():
        if ctx in install.contexts:
            return install
    return None


UNKNOWN = "?"  # dangerous, but we could not tell which cluster it hits


def expand_path(raw):
    """$HOME/${HOME}/~ survive shlex intact, so expand them ourselves."""
    return os.path.expanduser(os.path.expandvars(raw))


def unexpanded(value):
    """Does this token still hold shell syntax the hook cannot resolve?

    The session's variables are not in the hook's environment, so a `$VAR` or a
    `$(...)` is a value we do not know rather than one that names nothing.
    """
    return "$" in value or "`" in value


_CONTEXT_PROBE_CACHE = {}


def current_context_install(installs, kubeconfig=None):
    """Which protected install the ambient kubeconfig points at.

    Install -> protected; None -> resolved, but not a protected cluster;
    UNKNOWN -> could not resolve at all (missing file, no kubectl, timeout).

    Cached for the duration of one classification, which `classify` clears at
    the top. The answer cannot change while a single command line is being
    classified, and a compound line asks for it once per segment -- at six
    seconds a probe, enough segments overrun the hook's own timeout, and a
    killed hook lets the command through.
    """
    key = expand_path(kubeconfig) if kubeconfig else ""
    if key in _CONTEXT_PROBE_CACHE:
        return _CONTEXT_PROBE_CACHE[key]
    kenv = dict(os.environ)
    if kubeconfig:
        kenv["KUBECONFIG"] = key
    try:
        proc = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True, text=True, timeout=6, env=kenv,
        )
        answer = (UNKNOWN if proc.returncode != 0
                  else install_for_context(installs, proc.stdout.strip()))
    except (subprocess.SubprocessError, OSError):
        answer = UNKNOWN
    _CONTEXT_PROBE_CACHE[key] = answer
    return answer


def install_from_vars_sh(installs, cwd):
    """Which protected install a checkout's installers are pointed at.

    The installers read the checkout's `install.env` and act on the install it
    names, ignoring your current kubectl context entirely. So the checkout, not
    the context, is the honest signal for those.

    Matched on where an install came from, and failing that on the context the
    file derives. Grepping the file for markers would re-run the ambiguity
    `install_from_markers` exists to resolve, on the one path that already
    knows the answer exactly.
    """
    install_env, vars_sh = find_install_state(cwd)
    path = install_env or vars_sh
    if not path:
        return None
    for install in installs.values():
        if install.source == path:
            return install
    derived = _install_from_state(install_env, vars_sh)
    if not derived:
        return None
    return install_for_context(installs, derived.context)


def cwd_after_cd(command, cwd):
    """Where a `cd` on this line puts the commands that follow it.

    `cd <other-checkout> && ./upgrade.sh` acts on that checkout's install, not
    on the session's. Ignoring the `cd` is not a near miss but a mix-up: it
    takes one install's lease and lets the installer reconfigure another. And
    from a session started outside any checkout, `cd ~/kube-agents &&
    ./install.sh` would find no install configuration at all and run unguarded.
    """
    for segment in split_segments(command):
        seg = segment.strip()
        if not CD_RE.match(seg):
            continue
        try:
            where = shlex.split(seg)[1:2]
        except ValueError:
            continue
        if not where or where[0].startswith("-"):
            continue
        moved = expand_path(where[0])
        cwd = moved if os.path.isabs(moved) else os.path.join(
            cwd or os.getcwd(), moved)
    return cwd


def classify(command, installs, cwd=None, _depth=0):
    """(install, reason) if this mutates a protected install, else (None, None).

    `install` is UNKNOWN when the command is of a kind that reconfigures an
    install but the target could not be resolved -- the caller asks rather than
    guessing.

    Every branch below dispatches on the *command word* of a segment, after
    wrappers are stripped, rather than on whether a token appears somewhere on
    the line. Presence-based matching reads `grep -rn kubectl deploy/` as a
    mutating kubectl and `cat upgrade.sh` as an installer run, either of which
    takes an hour-long lease on a shared cluster for a command that touched
    nothing.
    """
    if not installs:
        return None, None

    if not _depth:
        _CONTEXT_PROBE_CACHE.clear()
    cwd = cwd_after_cd(command, cwd)
    ambient_kubeconfig = None
    # A marker anywhere on the line pins the whole line: `export KUBECONFIG=...
    # && kubectl patch ...` names the cluster in a different segment than the
    # one doing the damage.
    line_marker = install_from_markers(installs, command)

    for segment in split_segments(command):
        seg = segment.strip()
        if not seg:
            continue

        # `OUT=$(kubectl apply -f x)` -- the assignment swallows the command
        # word, so classify what the substitution runs.
        if _depth < MAX_WRAPPER_DEPTH:
            inner = [g for m in SUBSTITUTION_RE.finditer(seg) for g in m.groups() if g]
            for payload in inner:
                target, reason = classify(payload, installs, cwd=cwd,
                                          _depth=_depth + 1)
                if target:
                    return target, reason
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        if not tokens:
            continue

        # KUBECONFIG=... set inline carries into later segments of the same line
        for tok in tokens:
            if tok.startswith("KUBECONFIG="):
                ambient_kubeconfig = tok.split("=", 1)[1]

        head = strip_wrappers(tokens)
        if not head:
            continue

        # `bash -c "kubectl delete ..."` -- shlex collapses the payload into one
        # token, so classify what the shell will actually run.
        if _depth < MAX_WRAPPER_DEPTH:
            handled = False
            for payload in shell_payloads(head):
                handled = True
                target, reason = classify(payload, installs, cwd=cwd,
                                          _depth=_depth + 1)
                if target:
                    return target, reason
            if handled:
                continue

        binary = os.path.basename(head[0])
        args = head[1:]
        marker = install_from_markers(installs, seg)

        def resolve(argv=args):
            """Which protected install does this segment act on, if any?

            Evidence in order of how directly it names the target. An explicit
            --context wins outright: naming an unprotected cluster is a
            definite answer, not a missing one. helm spells the same flag
            --kube-context and is one of the families guarded here, so it has
            to count for as much -- otherwise `helm --kube-context <other>
            upgrade` resolves to the ambient install and takes the wrong
            lease. An explicit --kubeconfig is the same kind of answer one
            step removed -- the documented live-test workflow keeps a
            dedicated kubeconfig per install -- and it outranks markers,
            because a marker is a substring that happens to appear on the
            line. Get that order wrong and a command carrying install A's name
            but B's kubeconfig takes A's lease and mutates B.

            An unexpanded `$VAR` is the exception to the flag winning
            outright. shlex does no expansion and the hook does not have the
            session shell's variables, so `--context "$CTX"` arrives as a
            literal that matches no install -- and reading that as "names an
            unprotected cluster" turns `CTX=$(kubectl config current-context)
            && kubectl --context "$CTX" delete ns kubeagents-system` into a
            silent pass. It is a missing answer, so it falls through.
            """
            explicit_kubeconfig = None
            for i, tok in enumerate(argv):
                for flag in ("--context", "--kube-context"):
                    named = None
                    if tok.startswith(flag + "="):
                        named = tok.split("=", 1)[1]
                    elif tok == flag:
                        named = argv[i + 1] if i + 1 < len(argv) else ""
                    if named is None or unexpanded(named):
                        continue
                    return install_for_context(installs, named)
                if tok.startswith("--kubeconfig="):
                    explicit_kubeconfig = tok.split("=", 1)[1]
                elif tok == "--kubeconfig" and i + 1 < len(argv):
                    explicit_kubeconfig = argv[i + 1]
            if explicit_kubeconfig:
                named = current_context_install(installs, explicit_kubeconfig)
                # None here is "that file points somewhere unprotected", which
                # is an answer. Only UNKNOWN -- unreadable, no kubectl -- falls
                # through to the weaker evidence below.
                if named != UNKNOWN:
                    return named
            return (marker or line_marker
                    or current_context_install(installs, ambient_kubeconfig))

        # ---- kubectl -------------------------------------------------------
        if binary == "kubectl":
            subs = positionals(args)
            sub = subs[0] if subs else None
            if sub is None:
                continue
            if (sub in KUBECTL_READONLY and sub not in KUBECTL_MUTATING
                    and tuple(subs[:2]) not in KUBECTL_READONLY_EXCEPTIONS):
                continue
            if tuple(subs[:2]) in KUBECTL_MUTATING_EXCEPTIONS:
                continue
            if DRY_RUN_READONLY.intersection(args):
                continue
            target = resolve()
            # An unresolvable target is almost never a protected install --
            # those are in the kubeconfig by construction. kubectl runs
            # constantly; don't tax it with a prompt on a maybe.
            if target and target != UNKNOWN:
                return target, "kubectl %s" % sub
            continue

        # ---- helm ----------------------------------------------------------
        if binary == "helm":
            sub = positional(args)
            if sub in HELM_MUTATING:
                # helm reinstalls the release wholesale, so an unresolvable
                # target is worth a prompt rather than a silent pass.
                target = resolve()
                if target:
                    return target, "helm %s" % sub
            continue

        # ---- docker push to a protected install's registry ------------------
        if binary == "docker":
            subs = positionals(args)
            # `docker buildx build --push -t <ref>` pushes just as `docker push`
            # does, and it is the standard single-step form for the
            # `--platform linux/amd64` builds AGENTS.md asks for.
            if "push" in subs[:2] or "--push" in args:
                # Matched against the whole line, as every other family is:
                # `IMG=<ref> && docker push $IMG` names the registry in the
                # segment before the one doing the pushing.
                for install in installs.values():
                    if install.registry and install.registry in command:
                        return install, ("docker push to the %s registry"
                                         % install.name)
                # No registry on the line, and the ref does not say where it
                # is going -- `docker push $(cat last-image)`, the `IMG=` a
                # `source .env` two segments back set, or the half-spelled
                # `.../$PROJECT_ID/...` that `unreadable_registry` owns. A
                # registry that is
                # absent and one the hook cannot read are different answers,
                # and reading the second as the first is how the incident this
                # exists to prevent gets through: the tag the shared install
                # is running, overwritten with no lease taken and no deny.
                # `resolve` already treats an unexpanded `--context "$CTX"`
                # this way. Pushes are rare, so the prompt is cheap -- the
                # same asymmetry the installer branch argues. Silent when no
                # protected install records a registry at all, since then
                # there is nothing the push could be overwriting.
                registries = [i.registry for i in installs.values() if i.registry]
                if registries and any(unreadable_registry(ref, registries)
                                      for ref in image_refs(subs, args)):
                    return UNKNOWN, "docker push whose registry could not be read"
            continue

        # ---- gcloud --------------------------------------------------------
        if binary == "gcloud":
            subs = positionals(args)
            if subs[:1] == ["pubsub"] and "publish" in subs:
                # Marker-based, not topic-based: a publish that drives a real
                # agent turn names the install's project, or a chat topic the
                # install lists in its `markers`.
                target = marker or line_marker
                if target:
                    return target, "gcloud pubsub publish (drives a real agent turn)"
            if subs[:2] == ["container", "clusters"] and len(subs) > 2:
                if subs[2] in GCLOUD_CLUSTER_MUTATING:
                    target = marker or line_marker
                    if target:
                        return target, "gcloud container clusters %s" % subs[2]
            continue

        # ---- terraform ------------------------------------------------------
        # The composition in terraform/examples/full-install installs the same
        # thing the installer does, so `terraform apply` there is an install.
        if binary in ("terraform", "tofu"):
            if positional(args) in TERRAFORM_MUTATING:
                target = (marker or line_marker or install_from_vars_sh(installs, cwd)
                          or current_context_install(installs, ambient_kubeconfig))
                if target:
                    return target, "terraform %s" % positional(args)
            continue

        # ---- installer / redeploy scripts ----------------------------------
        # These read install.env and reconfigure an install wholesale, so an
        # unresolvable target is worth a prompt rather than a silent pass.
        hit = entry_point(head)
        if hit:
            if plugin_installer(head):
                # Context-driven, not checkout-driven; `resolve` reads the
                # --context these accept and falls back to the ambient one.
                target = resolve()
                if target:
                    return target, "agent plugin installer (%s)" % hit
                continue
            target = marker or line_marker or install_from_vars_sh(installs, cwd)
            if not target:
                # None here means "resolved, and it is not a protected
                # install" -- a real answer, not a shrug. Only UNKNOWN is a
                # shrug, and current_context_install distinguishes them.
                target = current_context_install(installs, ambient_kubeconfig)
            if target:
                return target, "installer/redeploy entry point (%s)" % hit

    return None, None


def looks_interesting(command):
    """Cheap pre-filter so ordinary commands never pay for a cluster round-trip."""
    return any(k in command for k in
               ("kubectl", "helm", "docker", "gcloud", "terraform", "tofu",
                "make ", ".sh"))


# --------------------------------------------------------------------------
# Hook entry points
# --------------------------------------------------------------------------
def hook_decision(decision, reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def hook_pretooluse():
    """PreToolUse entry point, wrapped so a crash asks rather than passes.

    Claude Code treats a non-zero hook exit other than 2 as a hook error and
    lets the tool call through, so an unhandled traceback here is a silent
    fail-open -- the one outcome the design rules out. Anything unexpected
    becomes the same `ask` an unreachable cluster gets.
    """
    try:
        return _hook_pretooluse()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- fail to `ask`, never to `allow`
        hook_decision("ask", (
            "The live-test lease hook failed (%s: %s), so the lease was not\n"
            "checked. If this command mutates a shared install, claim the lease\n"
            "first: python3 scripts/live_test_lease.py acquire"
            % (type(exc).__name__, exc)
        ))


def _hook_pretooluse():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if payload.get("tool_name") != "Bash":
        sys.exit(0)
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command or not looks_interesting(command):
        sys.exit(0)

    # the hook's own cwd is not the session's; pass it through for the record
    cwd = payload.get("cwd")
    if cwd:
        os.environ["CLAUDE_CWD"] = cwd

    installs = resolve_installs(cwd, also=cwd_after_cd(command, cwd))
    if not installs:
        sys.exit(0)

    target, reason = classify(command, installs, cwd=cwd)
    if not target:
        sys.exit(0)

    if target == UNKNOWN:
        hook_decision("ask", (
            "This looks like it reconfigures a kube-agents install (%s), but the\n"
            "target cluster could not be determined, so the live-test lease was\n"
            "not checked. If it points at a shared install, claim the lease first:\n"
            "  python3 scripts/live_test_lease.py acquire" % reason
        ))

    try:
        data, _ = get_lease(target)
    except Unreachable as exc:
        hook_decision("ask", (
            "Could not check the live-test lease for %s (%s).\n"
            "Another agent may be mid-change. Proceed only if you know the "
            "install is yours." % (target.name, exc)
        ))

    mine = read_local_token(target)
    # `mine and` matters: a record with no `token` and no local token file
    # would otherwise compare None == None and read as your own lease, which
    # is the one comparison in the hook that fails open.
    if mine and lease_is_live(data) and data.get("token") == mine:
        # keep it warm; renew lazily once past the halfway mark
        exp = parse_iso(data.get("expiresAt", ""))
        try:
            ttl = int(data.get("ttlMinutes", DEFAULT_TTL_MIN))
        except (TypeError, ValueError):
            ttl = DEFAULT_TTL_MIN
        if exp and (exp - now()) < timedelta(minutes=ttl / 2):
            ok, msg = do_renew(target, ttl)
            if not ok:
                # Losing the renew means the claim is no longer ours, and
                # carrying on believing otherwise is how two agents end up
                # mid-test on one install.
                hook_decision("ask", (
                    "Your live-test lease on %s could not be renewed (%s).\n"
                    "Another agent may have taken it over. Re-check with:\n"
                    "  python3 scripts/live_test_lease.py status"
                    % (target.name, msg)
                ))
        sys.exit(0)

    if lease_is_live(data):
        # A live lease whose token we cannot match is usually someone else's,
        # but it is also what a lost token file looks like -- a cleared
        # $XDG_STATE_HOME, or a session id the harness did not carry over.
        # Saying "another agent" about your own lease sends you to `steal`.
        yours = data.get("session") == holder_key()
        hook_decision("deny", (
            "Blocked: the %s live-test install is held by %s.\n"
            "   %s\n"
            "   %s\n\n"
            "   This command would mutate it (%s).\n\n"
            "   %s\n"
            "     python3 scripts/live_test_lease.py steal --env %s%s\n"
            "   Read-only commands are never blocked."
            % (target.name,
               "your own session, under a token this machine no longer has"
               if yours else "another agent",
               target.label, describe(data), reason,
               "The local token is gone, so take the lease back with:" if yours
               else ("Wait for it, or coordinate with the holder. If that "
                     "session is\n   gone, take it over with:"),
               target.name,
               # `steal` refuses a lease that has not expired. Where the holder
               # is provably this session, say so up front rather than making
               # the agent discover it from a refusal.
               " --force" if yours else "")
        ))

    ok, msg = do_acquire(target)
    if ok:
        sys.exit(0)
    hook_decision("deny", (
        "Blocked: could not claim the live-test lease for %s.\n   %s\n\n"
        "   This command would mutate %s (%s)."
        % (target.name, msg, target.label, reason)
    ))


def hook_sessionend():
    try:
        payload = json.load(sys.stdin)
        if payload.get("cwd"):
            os.environ["CLAUDE_CWD"] = payload["cwd"]
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if payload.get("reason") in SESSION_CONTINUES:
        # Not every SessionEnd ends the session. `/clear` and `/resume` fire it
        # on a process that keeps running under the same holder key, still in
        # the middle of whatever it was live-testing; releasing there hands the
        # install to the next agent while this one is still writing to it. The
        # lease survives, the next mutating command renews it as usual, and the
        # real exit -- which reports one of the other reasons -- releases it.
        sys.exit(0)
    try:
        # What this session holds, not what the exit directory can see. A
        # token file exists only where this session's holder key claimed a
        # lease, so releasing somebody else's on the way out -- the failure the
        # whole mechanism exists to prevent -- is not reachable from here.
        installs = held_installs(_resolvable_installs(payload.get("cwd")))
    except Exception:  # noqa: BLE001 -- nothing to release if we cannot look
        sys.exit(0)
    for install in installs.values():
        try:
            do_release(install)
        except Exception:  # noqa: BLE001 -- one install must not strand the rest
            clear_local_token(install)
    sys.exit(0)


# --------------------------------------------------------------------------
def pick(installs, name):
    """The install `--env` names, or the only one there is."""
    if name:
        if name not in installs:
            raise SystemExit("no protected install named %r (known: %s)"
                             % (name, ", ".join(sorted(installs)) or "none"))
        return installs[name]
    if len(installs) == 1:
        return next(iter(installs.values()))
    raise SystemExit("--env is required: %s" % ", ".join(sorted(installs)))


def print_status(installs):
    for name in sorted(installs):
        install = installs[name]
        try:
            data, _ = get_lease(install)
        except Unreachable as exc:
            print("%-16s %s\n%17s unreachable: %s" % (name, install.label, "", exc))
            continue
        if not lease_is_live(data):
            extra = " (expired lease present)" if data else ""
            print("%-16s %s\n%17s FREE%s" % (name, install.label, "", extra))
        else:
            yours = "  <-- yours" if data.get("token") == read_local_token(install) else ""
            print("%-16s %s\n%17s HELD  %s%s"
                  % (name, install.label, "", describe(data), yours))


def status_json(installs):
    """`status` for a caller that has to branch on the answer.

    The human listing above is two lines per install and says "HELD" inside a
    sentence, so the only way to act on it is to match text -- and a scheduled
    `terraform apply` deciding whether to destroy somebody's live validation is
    the wrong place for a grep. `unreachable` is reported as its own state
    rather than folded into free: a cluster that cannot be asked has not
    answered "no".
    """
    out = []
    for name in sorted(installs):
        install = installs[name]
        entry = {"name": name, "label": install.label}
        try:
            data, _ = get_lease(install)
        except Unreachable as exc:
            entry.update(state="unreachable", detail=str(exc))
            out.append(entry)
            continue
        if not lease_is_live(data):
            entry.update(state="free", detail=describe(data) if data else "")
        else:
            entry.update(state="held", detail=describe(data),
                         holder=data.get("holder", ""),
                         expiresAt=data.get("expiresAt", ""),
                         pr=data.get("pr", ""))
        out.append(entry)
    print(json.dumps(out, indent=2))


def main():
    ap = argparse.ArgumentParser(
        prog="live_test_lease.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=[
        "status", "acquire", "renew", "release", "steal",
        "hook-pretooluse", "hook-sessionend",
    ])
    ap.add_argument("--env", help="which install, when more than one is protected")
    ap.add_argument("--pr")
    ap.add_argument("--note")
    ap.add_argument("--ttl", type=int, default=DEFAULT_TTL_MIN)
    ap.add_argument("--all", action="store_true", help="release every install you hold")
    ap.add_argument("--force", action="store_true", help="steal even a live lease")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable `status` output")
    args = ap.parse_args()

    if args.action == "hook-pretooluse":
        return hook_pretooluse()
    if args.action == "hook-sessionend":
        return hook_sessionend()

    installs = resolve_installs()
    # An install this session holds a lease on belongs in every answer below,
    # whether or not this directory can see it. Otherwise `status` reports a
    # cluster you are holding as none of your business and `release --all`
    # cannot reach it -- which is precisely the lease taken from a checkout the
    # session has since left.
    for name, install in held_installs(installs).items():
        installs.setdefault(name, install)
    if not installs:
        print("No protected install found. This checkout has no install.env "
              "(nor a legacy k8s-operator/scripts/vars.sh) and %s does not "
              "list one." % config_path(), file=sys.stderr)
        return 2

    try:
        if args.action == "status":
            selected = ({args.env: pick(installs, args.env)} if args.env
                        else installs)
            if args.json:
                status_json(selected)
            else:
                print_status(selected)
            return 0

        if args.action == "acquire":
            ok, msg = do_acquire(pick(installs, args.env), args.pr, args.note, args.ttl)
        elif args.action == "renew":
            ok, msg = do_renew(pick(installs, args.env), args.ttl)
        elif args.action == "release":
            targets = (list(held_installs(installs).values()) if args.all
                       else [pick(installs, args.env)])
            ok = True
            msgs = []
            for install in targets:
                good, m = do_release(install)
                ok = ok and good
                msgs.append("%s: %s" % (install.name, m))
            msg = "\n".join(msgs) or "nothing held"
        else:
            ok, msg = do_steal(pick(installs, args.env), args.force)
    except Unreachable as exc:
        print("cluster unreachable: %s" % exc, file=sys.stderr)
        return 2

    print(("OK  " if ok else "FAIL  ") + msg, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
