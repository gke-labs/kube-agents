"""The post-merge check's adapters to the install's declared scope and to the fleet audit's re-check.

:func:`install_scope` reads the declared scope the way the fleet collectors are
handed it, and :func:`scope_refusal_for` applies ``recheck.scope_refusal`` to a
record's cluster, so the post-merge check never reads a cluster the audit
would not. :func:`sandbox_recheck` re-runs the audit's check for one record,
its ``kubectl`` sent into the shell sandbox. ``recheck`` is imported from the
image's copy of the fleet-audit skill, never the profile's copy on the data
volume, which the agent can write.
"""

from __future__ import annotations

from collections.abc import Callable

#: The image's copy of the fleet-audit scripts, tried first.
IMAGE_FLEET_AUDIT_DIR = "/opt/platform-template/skills/fleet-audit/scripts"
#: A refusal that starts with this means the scope cannot say (a project it
#: could not read): the cluster is not read, and the record waits.
UNKNOWN_PREFIX = "unknown: "
#: Why the declared scope leaves ``record``'s cluster out, or None.
ScopeRefusal = Callable[[dict], "str | None"]
#: Re-checks one finding: ``recheck(record)`` -> recheck.py's answer (``outcome``
#: ``clean``, ``reproduces`` or ``unchecked``, with a ``reason``).
Recheck = Callable[[dict], dict]
#: recheck.py's reasons for an ``unchecked`` answer whose read did not happen.
RECHECK_UNREAD_REASONS = ("the read failed its gate", "get-credentials for")


class RecheckUnread(RuntimeError):
    """The re-check's read did not happen (credentials or the kubectl read failed): try again later."""


class RecheckDeferred(RuntimeError):
    """This run has spent its re-check reads: the record waits for the next one."""


def _fleet_audit_dirs() -> list[str]:
    from pathlib import Path

    return [IMAGE_FLEET_AUDIT_DIR, str(Path(__file__).resolve().parents[1] / "skills" / "fleet-audit" / "scripts")]


def _fleet_audit_module(name: str):
    import importlib
    import os
    import sys

    for directory in _fleet_audit_dirs():
        if os.path.isdir(directory) and directory not in sys.path:
            sys.path.append(directory)
    return importlib.import_module(name)


def install_scope():
    """The install's declared scope as the collectors are handed it (``fleet_scope_targets``)."""
    import fleet_scope_args
    import fleet_scope_targets

    scope = fleet_scope_args.DeclaredScope()
    targets = fleet_scope_targets.declared_scope_targets()
    if targets is None:
        scope.set(None, None)
        return scope
    argv = list(targets.collector_argv())
    flags = dict(zip(argv[::2], argv[1::2]))
    scope.set(flags.get(fleet_scope_args.SCOPE_PROJECTS_FLAG, ""), flags.get(fleet_scope_args.SCOPE_UNREAD_FLAG),
              flags.get(fleet_scope_args.SCOPE_EXCLUDE_CLUSTERS_FLAG))
    return scope


def scope_refusal_for(scope) -> ScopeRefusal:
    """``recheck.scope_refusal`` against ``scope``, per record: the rule the audit and the re-check apply.

    Only a cluster ``spec.scope.exclude.clusters`` names, or a project a fully
    resolved declared scope (``sweep_is_declared``) neither sweeps nor lists as
    unread, is out of scope. A project the reconcile could not read
    (``--scope-unread``), a scope with the unresolved row, or a scope declared
    without its flags is unknown (:data:`UNKNOWN_PREFIX`): the cluster is not
    read and the record waits, since the project may still be in scope.
    """
    import post_merge_check

    recheck = _fleet_audit_module("recheck")

    def refused(record: dict) -> str | None:
        project, name, location = post_merge_check.cluster_coordinates(record)
        if not scope.drop_excluded(project, [{"name": name, "location": location}]):
            # Named in the exclusion: out of scope, whatever is known about the project.
            return f"{project}/{location}/{name} is excluded by spec.scope.exclude.clusters"
        reason = recheck.scope_refusal(scope, project, location, name)
        if not reason:
            return None
        known = set(scope.projects or []) | {unread for unread, _ in scope.unread}
        # A project number is never in ``known`` (it lists ids), and the scope cannot say which id it
        # names, so it is unknown, as ``override_error`` itself says: never out of scope.
        if scope.sweep_is_declared() and not project.isdigit() and project not in known:
            return reason
        return UNKNOWN_PREFIX + reason

    return refused


def sandbox_recheck(scope, kubeconfigs: dict[tuple[str, str, str], str] | None = None) -> Recheck:
    """``recheck.recheck`` for one record, its kubectl run in the shell sandbox with the stall watch's
    kubeconfig. ``kubeconfigs`` is the run's cache, shared with ``post_merge_check.SandboxKubectl`` so a
    cluster's credentials are fetched once per run.

    Raises :class:`RecheckUnread` when the read itself failed, which the caller retries.
    """
    cache = {} if kubeconfigs is None else kubeconfigs

    def run_one(record: dict) -> dict:
        import collector_run
        import post_merge_check
        import stall_watch

        recheck = _fleet_audit_module("recheck")
        project, name, location = key = post_merge_check.cluster_coordinates(record)
        try:
            if key not in cache:
                cache[key] = stall_watch.fetch_credentials(*key)
        except Exception as exc:  # noqa: BLE001 — no credentials is no read
            raise RecheckUnread(f"get-credentials failed: {exc}") from None
        kubeconfig = cache[key]

        def run(argv, env=None, timeout=None):
            done = stall_watch.run_sandbox(list(argv), timeout=timeout or post_merge_check.KUBECTL_HOP_TIMEOUT_SECONDS,
                                           kubeconfig=(env or {}).get("KUBECONFIG") or kubeconfig)
            return collector_run.Run(list(argv), done.returncode, done.stdout or "", done.stderr or "", 0.0)

        target = recheck.Target(check=record["kind"], cluster=f"{project}/{location}/{name}",
                                namespace=record["namespace"], object=record["object"])
        answer = recheck.recheck(target, run=run, kubeconfig=kubeconfig, scope=scope)
        if answer.get("outcome") == "unchecked" and str(answer.get("reason") or "").startswith(RECHECK_UNREAD_REASONS):
            raise RecheckUnread(answer["reason"])
        return answer

    return run_one
