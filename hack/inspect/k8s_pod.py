import os
import re
import time
import uuid
from typing import Literal, overload

import anyio
import yaml
from inspect_ai.util import (
    ExecResult,
    OutputLimitExceededError,
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxEnvironmentLimits,
    sandboxenv,
    subprocess,
)

CONTEXT = os.environ.get("INSPECT_CONTEXT", "kind-kube-agents")
READY_TIMEOUT = "300s"
# Every pod this process creates carries it: task_cleanup deletes by it, and
# `kubectl delete pod -l inspect-run` sweeps what a killed run left behind.
RUN_LABEL = "inspect-run"
RUN_ID = uuid.uuid4().hex[:12]
# The in-pod timeout kills the command; the host one only catches a hung kubectl.
KILL_AFTER = "5s"
HOST_TIMEOUT_SLACK = 10
TIMED_OUT = 124
# SIGKILL after -k, and SIGTERM: timeouts only once the time is up, as for docker.
KILLED = (137, 143)
# kubectl exec appends this to stderr when the remote command fails.
KUBECTL_EXIT_LINE = re.compile(r"command terminated with exit code \d+\n?$")
ERRORS = {
    "No such file": FileNotFoundError,
    "Is a directory": IsADirectoryError,
    "Permission denied": PermissionError,
}


async def kubectl(
    *args: str,
    input: str | bytes | None = None,
    text: bool = True,
    timeout: int | None = None,
    output_limit: int | None = None,
    concurrency: bool = True,
):
    return await subprocess(
        ["kubectl", "--context", CONTEXT, *args],
        input=input, text=text, timeout=timeout, output_limit=output_limit, concurrency=concurrency,
    )


async def kubectl_ok(*args: str, input: str | None = None) -> str:
    result = await kubectl(*args, input=input)
    if not result.success:
        raise RuntimeError(f"kubectl {' '.join(args)}: {result.stderr}")
    return result.stdout


def manifest(config: SandboxEnvironmentConfigType | None) -> list[dict]:
    if not isinstance(config, str):
        raise ValueError("k8s-pod sandbox: pass the manifest path as the config")
    with open(config) as f:
        return [doc for doc in yaml.safe_load_all(f) if doc]


def raise_for(result: ExecResult, file: str) -> None:
    if result.success:
        return
    stderr = result.stderr if isinstance(result.stderr, str) else result.stderr.decode(errors="replace")
    for text, error in ERRORS.items():
        if text in stderr:
            raise error(file)
    raise RuntimeError(f"{file}: {stderr}")


@sandboxenv(name="k8s-pod")
class K8sPodSandbox(SandboxEnvironment):
    """Runs each sample in a pod created from a plain manifest.

    `sandbox=("k8s-pod", "sandbox.yaml")`: every Pod in the manifest becomes a
    sandbox named after it, created per sample with a generated name and deleted
    afterwards; every other object is applied once per task. The cluster is
    INSPECT_CONTEXT (default kind-kube-agents); the images have to be pullable there.
    """

    def __init__(self, pod: str, namespace: str):
        super().__init__()
        self.target = ["-n", namespace, pod]

    @classmethod
    async def task_init(cls, task_name: str, config: SandboxEnvironmentConfigType | None) -> None:
        shared = [doc for doc in manifest(config) if doc["kind"] != "Pod"]
        if shared:
            await kubectl_ok("apply", "-f", "-", input=yaml.safe_dump_all(shared))

    @classmethod
    async def task_cleanup(cls, task_name: str, config: SandboxEnvironmentConfigType | None, cleanup: bool) -> None:
        if cleanup:
            await kubectl("delete", "pod", "--all-namespaces", "-l", f"{RUN_LABEL}={RUN_ID}", "--wait=false")

    @classmethod
    async def sample_init(
        cls, task_name: str, config: SandboxEnvironmentConfigType | None, metadata: dict[str, str]
    ) -> dict[str, SandboxEnvironment]:
        sandboxes: dict[str, SandboxEnvironment] = {}
        try:
            for pod in (doc for doc in manifest(config) if doc["kind"] == "Pod"):
                name = pod["metadata"].pop("name")
                pod["metadata"]["generateName"] = f"inspect-{name}-"
                pod["metadata"].setdefault("labels", {})[RUN_LABEL] = RUN_ID
                created = await kubectl_ok(
                    "create", "-f", "-", "-o", "jsonpath={.metadata.namespace} {.metadata.name}",
                    input=yaml.safe_dump(pod),
                )
                namespace, generated = created.split()
                sandboxes[name] = cls(generated, namespace)
            for sandbox in sandboxes.values():
                await kubectl_ok("wait", "pod", "--for=condition=Ready", f"--timeout={READY_TIMEOUT}", *sandbox.target)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await cls.sample_cleanup(task_name, config, sandboxes, interrupted=True)
            raise
        return sandboxes

    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        for sandbox in environments.values():
            await kubectl("delete", "pod", "--wait=false", *sandbox.as_type(K8sPodSandbox).target)

    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        if cwd:
            cmd = ["/bin/sh", "-c", 'cd "$0" && exec "$@"', cwd, *cmd]
        if env:
            cmd = ["/usr/bin/env", *(f"{k}={v}" for k, v in env.items()), *cmd]
        if timeout:
            cmd = ["/usr/bin/timeout", "-k", KILL_AFTER, f"{timeout}s", *cmd]
        if user:
            cmd = ["/usr/sbin/runuser", "-u", user, "--", *cmd]
        start = time.monotonic()
        result = await kubectl(
            "exec", "-i", *self.target, "--", *cmd,
            input=input,
            timeout=timeout + HOST_TIMEOUT_SLACK if timeout else None,
            output_limit=SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE,
            concurrency=concurrency,
        )
        if timeout and (
            result.returncode == TIMED_OUT
            or (result.returncode in KILLED and time.monotonic() - start >= timeout)
        ):
            raise TimeoutError(f"Command timed out after {timeout} seconds")
        result.stderr = KUBECTL_EXIT_LINE.sub("", result.stderr)
        return result

    async def write_file(self, file: str, contents: str | bytes) -> None:
        script = 'mkdir -p -- "$(dirname -- "$0")" && cat > "$0"'
        raise_for(await kubectl("exec", "-i", *self.target, "--", "sh", "-c", script, file, input=contents), file)

    @overload
    async def read_file(self, file: str, text: Literal[True] = True) -> str: ...

    @overload
    async def read_file(self, file: str, text: Literal[False]) -> bytes: ...

    async def read_file(self, file: str, text: bool = True) -> str | bytes:
        limit = SandboxEnvironmentLimits.MAX_READ_FILE_SIZE
        # head, not output_limit: Inspect's circular buffer drops bytes as it wraps.
        result = await kubectl("exec", *self.target, "--", "head", "-c", str(limit + 1), "--", file, text=False)
        raise_for(result, file)
        if len(result.stdout) > limit:
            raise OutputLimitExceededError(SandboxEnvironmentLimits.MAX_READ_FILE_SIZE_STR, truncated_output=None)
        return result.stdout.decode() if text else result.stdout
