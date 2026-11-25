# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A throwaway delegating AgentProfile for one run of a case (#6789).

A profile with ``spec.delegates: true`` gets a delegate tool, so its task can
hand work to another profile and wait for the result. The eval install enables
only the chart's ``cluster`` and ``tse`` profiles, neither delegates, and the
chart cannot add one, so a case that grades delegation from a profile task has
the harness make one for each run.

A case declares it in two places, and scripts/validate_bench_cases.py holds
them together:

- ``delegating_profile: true`` at the top level of task.yaml, the declaration
  the case tooling reads (KEY);
- ``{{BENCH_DELEGATING_PROFILE}}`` as the ``name=`` of the
  ``[bench:profile ...]`` directive that opens the prompt (PLACEHOLDER), which
  is all the harness sees: devops-bench hands it the prompt alone.

For a prompt carrying the placeholder, ``KubeAgentsHarness.run`` calls
:func:`setup` before the ask is sent: it reads the live ``cluster``
AgentProfile (SOURCE_PROFILE) in AGENT_NAMESPACE on AGENT_CLUSTER_CONTEXT,
creates a copy under a run-unique name (``delegating-<hex>``) with
``spec.delegates: true``, ``spec.yieldToPinned: false``, its own description,
no named ServiceAccount, and LABEL, then waits until the profile's card is
published (READY_SECONDS), which is when the gateway can address it. The
placeholder is then replaced with the name. :func:`teardown` runs in the
harness's ``finally``, whatever the run did: on a graded run, an errored one,
an exception, Ctrl-C, or SIGTERM (:func:`signals_raise`). It deletes the
profile and waits (TEARDOWN_SECONDS) until neither the profile nor any of its
pool Deployments (``agentprofile-<name>-<tier>`` in ``<namespace>-profiles``)
is left; one it cannot confirm makes the run an infrastructure failure.

An install whose AgentProfile API has no ``spec.delegates`` (main before #6789
lands) is told apart from the CRD's schema, as scope_hold.py tells apart one
without ``spec.hold``: the copy is created without the field, so the case runs
on a profile that cannot delegate and is graded, which is the red step of the
eval-driven loop.

A run killed outright (SIGKILL) runs no ``finally``. Its profile carries LABEL,
and ``sweep`` deletes every labelled profile older than ``--min-age``; the name
is unique, so a leftover never answers a later case's ask. Nothing runs the
sweep yet: it is a manual step until the lease preflight calls it (a hack/
change), so a leftover keeps its warm pool and card until someone sweeps.

The kubectl calls use the context and namespace every other harness kubectl
call uses; nothing here reads a Secret, and the manifest goes to kubectl on
stdin, never to disk.

Subcommands, each exiting 1 with the reason on stderr::

    python -m kube_agents_bench.delegating_profile teardown --name <profile> \\
        [--context <ctx>] [--namespace <ns>]
    python -m kube_agents_bench.delegating_profile sweep [--min-age <seconds>] \\
        [--context <ctx>] [--namespace <ns>]
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

#: The task.yaml key a case declares the profile under, a bare ``true``.
KEY = "delegating_profile"
#: What the prompt's ``[bench:profile name=...]`` names the profile by.
PLACEHOLDER = "{{BENCH_DELEGATING_PROFILE}}"
#: The chart's profile the copy is made from.
SOURCE_PROFILE = "cluster"
#: The run-unique name: the prefix, then NAME_TOKEN_BYTES random bytes in hex.
NAME_PREFIX = "delegating-"
NAME_TOKEN_BYTES = 4
#: A name new_name makes: the prefix, then hex only.
THROWAWAY_NAME = re.compile(r"\A" + re.escape(NAME_PREFIX) + r"[0-9a-f]+\Z")
#: A DNS-1123 label short enough for the profile's bus identity (the operator
#: refuses a name of 56 characters).
NAME_PATTERN = re.compile(r"\A[a-z0-9]([-a-z0-9]{0,40}[a-z0-9])?\Z")
#: Put on every profile made here, so a sweep finds one a killed run left.
LABEL = "bench.kubeagents.x-k8s.io/throwaway-profile"
LABEL_VALUE = "delegating"
DESCRIPTION = (
    "Bench-only copy of the cluster profile that may delegate to another profile and wait "
    "for its result. Made for one eval run and deleted after it."
)
CRD_NAME = "agentprofiles.kubeagents.x-k8s.io"
RESOURCE = "agentprofile"
SPEC_DELEGATES = "delegates"
SPEC_YIELD = "yieldToPinned"
SPEC_IDENTITY = "identity"
SPEC_DESCRIPTION = "description"
#: The condition that is True once the gateway can address the profile.
READY_CONDITION = "CardPublished"
#: The operator's own names: the profile namespace beside the install's, the
#: pool Deployment per (profile, tier), the label pools carry, and the tier a
#: profile without allowedTiers runs at.
PROFILES_NAMESPACE_SUFFIX = "-profiles"
POOL_PREFIX = "agentprofile-"
POOL_PROFILE_LABEL = "kubeagents.x-k8s.io/agentprofile"
DEFAULT_TIERS = ("read",)
#: How long the profile may take to publish its card (a new profile waits
#: for every bus server to load its config), how long the delete may take
#: to clear the profile and its pools, and how often each is read.
READY_SECONDS = 300
TEARDOWN_SECONDS = 300
POLL_SECONDS = 5
#: ``sweep``'s default: older than any run that could still be using it.
SWEEP_MIN_AGE_SECONDS = 6 * 60 * 60
#: Every kubectl call's own bound, and the process's, a little longer.
REQUEST_TIMEOUT = "30s"
PROCESS_TIMEOUT_SECONDS = 60
DEFAULT_NAMESPACE = "kubeagents-system"


class DelegatingProfileError(Exception):
    """Setup or teardown could not do or confirm its work: infrastructure."""


class Interrupted(BaseException):
    """SIGTERM arrived while a profile existed (:func:`signals_raise`)."""


Kubectl = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Profile:
    """A profile :func:`setup` made: its name, namespace, and the tiers it may
    run at (the pools to confirm gone)."""

    name: str
    namespace: str
    tiers: tuple[str, ...]
    delegates: bool


def kubectl_runner(context: str | None = None) -> Kubectl:
    """kubectl on ``context`` (the harness's AGENT_CLUSTER_CONTEXT; the current
    context when unset, as the harness's other calls do)."""

    def run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        cmd = ["kubectl", f"--request-timeout={REQUEST_TIMEOUT}", *args]
        if context:
            cmd.extend(["--context", context])
        try:
            return subprocess.run(
                cmd, input=stdin, capture_output=True, text=True, check=False, timeout=PROCESS_TIMEOUT_SECONDS
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(cmd, 1, "", str(exc))

    return run


def from_env() -> tuple[Kubectl, str]:
    """The kubectl and namespace the harness addresses the install with."""
    return (
        kubectl_runner(os.environ.get("AGENT_CLUSTER_CONTEXT") or None),
        os.environ.get("AGENT_NAMESPACE") or DEFAULT_NAMESPACE,
    )


def new_name(token: str | None = None) -> str:
    """A run-unique profile name: ``delegating-<hex>``."""
    name = NAME_PREFIX + (token if token is not None else secrets.token_hex(NAME_TOKEN_BYTES))
    if not NAME_PATTERN.match(name):
        raise DelegatingProfileError(f"{name!r} is not a profile name the operator takes")
    return name


def is_throwaway_name(name: str) -> bool:
    """Whether ``name`` is one :func:`new_name` makes: the prefix, then hex
    only, so ``delegating-prod`` and the like are not."""
    return bool(THROWAWAY_NAME.match(name)) and bool(NAME_PATTERN.match(name))


def looks_like_throwaway_name(name: str) -> bool:
    """Whether ``name`` has the throwaway prefix at all: what the validator
    refuses as a literal name, broader than :func:`is_throwaway_name` on
    purpose, since no case should name one."""
    return name.startswith(NAME_PREFIX)


def crd_declares_delegates(crd: dict[str, Any]) -> bool:
    """Whether a served version of the AgentProfile CRD has spec.delegates."""
    for version in (crd.get("spec") or {}).get("versions") or []:
        if not version.get("served"):
            continue
        props = ((version.get("schema") or {}).get("openAPIV3Schema") or {}).get("properties") or {}
        spec = (props.get("spec") or {}).get("properties") or {}
        if SPEC_DELEGATES in spec:
            return True
    return False


def build_manifest(source: dict[str, Any], name: str, *, delegates: bool = True) -> dict[str, Any]:
    """The throwaway profile: ``source``'s spec with delegates on (when the API
    has the field), the pin yield off, its own description, and no named
    ServiceAccount (the operator refuses a second profile on another's, and an
    unnamed one gets a ServiceAccount with no RBAC bindings)."""
    spec = copy.deepcopy(source.get("spec") or {})
    if not spec:
        raise DelegatingProfileError(f"{RESOURCE}/{(source.get('metadata') or {}).get('name')} has no spec to copy")
    spec[SPEC_DESCRIPTION] = DESCRIPTION
    spec[SPEC_YIELD] = False
    spec[SPEC_IDENTITY] = {}
    if delegates:
        spec[SPEC_DELEGATES] = True
    else:
        spec.pop(SPEC_DELEGATES, None)
    metadata = source.get("metadata") or {}
    return {
        "apiVersion": source.get("apiVersion") or "kubeagents.x-k8s.io/v1alpha1",
        "kind": source.get("kind") or "AgentProfile",
        "metadata": {
            "name": name,
            "namespace": metadata.get("namespace"),
            "labels": {LABEL: LABEL_VALUE},
        },
        "spec": spec,
    }


def tiers_of(spec: dict[str, Any]) -> tuple[str, ...]:
    tiers = spec.get("allowedTiers")
    return tuple(t for t in tiers if isinstance(t, str)) if isinstance(tiers, list) and tiers else DEFAULT_TIERS


def card_published(profile: dict[str, Any]) -> bool:
    for condition in (profile.get("status") or {}).get("conditions") or []:
        if isinstance(condition, dict) and condition.get("type") == READY_CONDITION:
            return condition.get("status") == "True"
    return False


def _get_json(kubectl: Kubectl, *args: str) -> dict[str, Any]:
    result = kubectl("get", *args, "-o", "json")
    if result.returncode != 0:
        raise DelegatingProfileError(f"kubectl get {' '.join(args)}: {result.stderr.strip() or 'failed'}")
    try:
        doc = json.loads(result.stdout)
    except ValueError as exc:
        raise DelegatingProfileError(f"kubectl get {' '.join(args)}: not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise DelegatingProfileError(f"kubectl get {' '.join(args)}: not a JSON object")
    return doc


def _names(kubectl: Kubectl, *args: str) -> list[str] | None:
    """``kubectl get ... -o name``'s lines; None when the read failed."""
    result = kubectl("get", *args, "--ignore-not-found", "-o", "name")
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.split() if line]


def setup(
    kubectl: Kubectl,
    namespace: str,
    *,
    token: str | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    log: Callable[[str], None] = print,
) -> Profile:
    """Create the throwaway profile and wait until its card is published.

    Raises:
        DelegatingProfileError: The source can't be read, the create failed,
            or the card did not publish in READY_SECONDS. A profile that was
            created is torn down before this raises.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    name = new_name(token)
    source = _get_json(kubectl, RESOURCE, SOURCE_PROFILE, "-n", namespace)
    delegates = crd_declares_delegates(_get_json(kubectl, "crd", CRD_NAME))
    if not delegates:
        log(
            f"Delegating profile: {CRD_NAME} declares no spec.{SPEC_DELEGATES}, so {RESOURCE}/{name} "
            "is created without it and cannot delegate; the case is graded on that install as it is"
        )
    manifest = build_manifest(source, name, delegates=delegates)
    manifest["metadata"]["namespace"] = namespace
    profile = Profile(name=name, namespace=namespace, tiers=tiers_of(manifest["spec"]), delegates=delegates)
    # The create is inside the try: one cut short by a signal, or one that
    # timed out or failed after the server stored it, may have made the
    # profile, and the teardown (an ignore-not-found delete) covers that too.
    try:
        created = kubectl("create", "-n", namespace, "-f", "-", stdin=json.dumps(manifest))
        if created.returncode != 0:
            raise DelegatingProfileError(f"kubectl create {RESOURCE}/{name}: {created.stderr.strip() or 'failed'}")
        log(f"Delegating profile: created {RESOURCE}/{name} in {namespace} from {RESOURCE}/{SOURCE_PROFILE}")
        deadline = clock() + READY_SECONDS
        while True:
            try:
                if card_published(_get_json(kubectl, RESOURCE, name, "-n", namespace)):
                    break
            except DelegatingProfileError:
                pass
            if clock() >= deadline:
                raise DelegatingProfileError(
                    f"{RESOURCE}/{name}'s {READY_CONDITION} is not True after {READY_SECONDS}s"
                )
            sleep(POLL_SECONDS)
    except BaseException as exc:
        try:
            teardown(kubectl, profile, sleep=sleep, clock=clock, log=log)
        except DelegatingProfileError as gone:
            log(f"Delegating profile: teardown after a failed setup: {gone}")
            if isinstance(exc, DelegatingProfileError):
                raise DelegatingProfileError(f"{exc}; and its teardown: {gone}") from exc
        raise
    log(f"Delegating profile: {RESOURCE}/{name} published its card")
    return profile


def gone(kubectl: Kubectl, profile: Profile) -> str:
    """What is left of ``profile``: ``""`` once the profile and every pool is
    gone, else what remains or could not be read."""
    pools_namespace = profile.namespace + PROFILES_NAMESPACE_SUFFIX
    left: list[str] = []
    names = _names(kubectl, RESOURCE, profile.name, "-n", profile.namespace)
    if names is None:
        left.append(f"{RESOURCE}/{profile.name} could not be read")
    elif names:
        left.append(f"{RESOURCE}/{profile.name}")
    # By the operator's label, which finds a pool at any tier, and by the name
    # of each tier's pool, which finds one whatever its labels say.
    pools: set[str] = set()
    labelled = _names(kubectl, "deployments", "-n", pools_namespace, "-l", f"{POOL_PROFILE_LABEL}={profile.name}")
    if labelled is None:
        left.append(f"the deployments in {pools_namespace} could not be read")
    else:
        pools.update(n.rpartition("/")[2] for n in labelled)
    for tier in profile.tiers:
        pool = f"{POOL_PREFIX}{profile.name}-{tier}"
        by_name = _names(kubectl, "deployment", pool, "-n", pools_namespace)
        if by_name is None:
            left.append(f"deployment {pools_namespace}/{pool} could not be read")
        elif by_name:
            pools.add(pool)
    left.extend(f"deployment {pools_namespace}/{p}" for p in sorted(pools))
    return "; ".join(left)


def teardown(
    kubectl: Kubectl,
    profile: Profile,
    *,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    log: Callable[[str], None] = print,
) -> None:
    """Delete ``profile`` and wait until it and its pools are gone.

    Raises:
        DelegatingProfileError: Something is left after TEARDOWN_SECONDS.
    """
    with signals_ignored():
        _teardown(kubectl, profile, sleep=sleep or time.sleep, clock=clock or time.monotonic, log=log)


def _teardown(
    kubectl: Kubectl,
    profile: Profile,
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    log: Callable[[str], None],
) -> None:
    deadline = clock() + TEARDOWN_SECONDS
    deleted = False
    left = ""
    while True:
        if not deleted:
            # --wait=false: the wait is this loop's, which also covers the pools.
            result = kubectl(
                "delete", RESOURCE, profile.name, "-n", profile.namespace, "--ignore-not-found", "--wait=false"
            )
            deleted = result.returncode == 0
            if not deleted:
                left = f"kubectl delete {RESOURCE}/{profile.name}: {result.stderr.strip() or 'failed'}"
        if deleted:
            left = gone(kubectl, profile)
            if not left:
                log(f"Delegating profile: {RESOURCE}/{profile.name} and its pools are gone")
                return
        if clock() >= deadline:
            raise DelegatingProfileError(f"{RESOURCE}/{profile.name} not torn down after {TEARDOWN_SECONDS}s: {left}")
        sleep(POLL_SECONDS)


@contextlib.contextmanager
def signals_ignored() -> Iterator[None]:
    """SIGTERM and SIGINT are ignored for the duration, so a second stop
    can't abort a teardown half done (a teardown is bounded by
    TEARDOWN_SECONDS). Only the main thread can set a handler; elsewhere this
    does nothing."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextlib.contextmanager
def signals_raise() -> Iterator[None]:
    """SIGTERM raises :class:`Interrupted` for the duration, so a run the
    runner stops still reaches its ``finally`` (Ctrl-C already raises
    KeyboardInterrupt). Only the main thread can set a handler; elsewhere
    this does nothing."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum: int, _frame: Any) -> None:
        raise Interrupted(f"signal {signum}")

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _age_seconds(profile: dict[str, Any], now: datetime.datetime) -> float | None:
    stamp = (profile.get("metadata") or {}).get("creationTimestamp")
    if not isinstance(stamp, str):
        return None
    try:
        created = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - created).total_seconds()


def sweep(
    kubectl: Kubectl,
    namespace: str,
    *,
    min_age_seconds: float = SWEEP_MIN_AGE_SECONDS,
    now: datetime.datetime | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    log: Callable[[str], None] = print,
) -> list[str]:
    """Tear down every labelled profile older than ``min_age_seconds``; the
    names torn down. A younger one may belong to a run still going."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    listed = _get_json(kubectl, RESOURCE, "-n", namespace, "-l", f"{LABEL}={LABEL_VALUE}")
    swept: list[str] = []
    failed: list[str] = []
    for item in listed.get("items") or []:
        name = (item.get("metadata") or {}).get("name") or ""
        age = _age_seconds(item, now)
        if not is_throwaway_name(name) or age is None or age < min_age_seconds:
            continue
        profile = Profile(name=name, namespace=namespace, tiers=tiers_of(item.get("spec") or {}), delegates=False)
        try:
            teardown(kubectl, profile, sleep=sleep, clock=clock, log=log)
        except DelegatingProfileError as exc:
            failed.append(str(exc))
            continue
        swept.append(name)
    if failed:
        # Every other leftover was still tried; the failures are reported together.
        raise DelegatingProfileError("; ".join(failed))
    return swept


def main(argv: list[str] | None = None, kubectl: Kubectl | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kube_agents_bench.delegating_profile")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("teardown", "sweep"):
        p = sub.add_parser(command)
        p.add_argument("--context", default=os.environ.get("AGENT_CLUSTER_CONTEXT") or None)
        p.add_argument("--namespace", default=os.environ.get("AGENT_NAMESPACE") or DEFAULT_NAMESPACE)
        if command == "teardown":
            p.add_argument("--name", required=True)
            p.add_argument("--tier", action="append", default=[])
        else:
            p.add_argument("--min-age", type=float, default=SWEEP_MIN_AGE_SECONDS)
    args = parser.parse_args(argv)
    run = kubectl or kubectl_runner(args.context)
    try:
        if args.command == "teardown":
            if not is_throwaway_name(args.name):
                raise DelegatingProfileError(f"{args.name!r} is not a throwaway profile's name ({NAME_PREFIX}<hex>)")
            # And it carries the label setup puts on it: a name alone is not
            # proof this harness made the profile. One that's already gone
            # is fine: the teardown then only confirms its pools are gone.
            found = run("get", RESOURCE, args.name, "-n", args.namespace, "--ignore-not-found", "-o", "json")
            if found.returncode != 0:
                raise DelegatingProfileError(f"reading {RESOURCE}/{args.name}: {found.stderr.strip() or 'failed'}")
            if found.stdout.strip():
                labels = (json.loads(found.stdout).get("metadata") or {}).get("labels") or {}
                if labels.get(LABEL) != LABEL_VALUE:
                    raise DelegatingProfileError(f"{RESOURCE}/{args.name} has no {LABEL}={LABEL_VALUE} label: this harness didn't make it")
            tiers = tuple(args.tier) or DEFAULT_TIERS
            teardown(run, Profile(name=args.name, namespace=args.namespace, tiers=tiers, delegates=False))
        else:
            swept = sweep(run, args.namespace, min_age_seconds=args.min_age)
            print(f"Delegating profile: swept {', '.join(swept) or 'nothing'}")
        return 0
    except DelegatingProfileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
