#!/usr/bin/env python3
"""Admit a large `kubectl get` only when the shell has the memory for it.

`/usr/local/bin/kubectl` execs this with the session's arguments. A `get` across the
cluster (`-A`, or a cluster-scoped kind) is charged the peak memory that the cost table
(`kubectl-costs.json`) lists for it, in the install's sizing tier: its command key's
entry, else the sum of its kinds' entries, else the tier's `other` figure. Everything
else (other verbs, one namespace, named objects, watches, `--raw`) runs unchanged; the
memory guard bounds it. Nothing is queried or estimated at run time: the table is
written ahead of time by `scripts/dev/profile_kubectl_costs.py`, which runs each command
alone against the tier's reference cluster (`.agents/skills/update-kubectl-cost-table`).
A command charged `other` is recorded in the event log, so the next profile run can add it.

The charge is booked as a reservation: a file named by this process's pid in a directory
every session user shares, written atomically under an exclusive lock, so two large
reads cannot both see free memory and both start. The free budget is the memory guard's
high watermark, less what is in use outside admitted reads, less every reservation at
full size. A read that fits runs as a child, and its reservation lasts until the child
exits. A read that does not fit now waits up to `ADMIT_WAIT_SECONDS`, then fails with
exit 75. A read larger than one read may ever take fails at once with exit 3. Both print
a `capacity:` reason with the alternatives. A read with a label or field selector narrows
by an amount no table knows, so it is never refused for its size: it reserves what is free,
up to its charge, and waits like any read only while nothing is free.

Every command that reads objects (`SLOTTED_VERBS`; not a watch or `logs -f`) also takes a
slot for as long as it runs, the charged reads and those that pass through alike: the
shell runs at most one read's capacity / `SLOT_BYTES` such commands at once. A command past that waits, then fails with exit 75.
A command that passes through keeps its pid across the exec, so its slot lasts exactly as
long as kubectl does.

The admission is advisory: a session can run `/usr/local/libexec/kubectl` directly, and
the memory guard (`shell-memory-guard`) is the boundary that holds either way. Any error
in this admission runs the command unchanged. A lock it cannot take is not an error: the
command waits as if it found no room, and is refused with exit 75.
"""

from __future__ import annotations

import fcntl
import importlib.machinery
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time

KUBECTL_NEXT = "/usr/local/libexec/kubectl"
GUARD_PATH = "/usr/local/libexec/shell-memory-guard"
COST_TABLE = "/usr/local/share/kube-agents/kubectl-costs.json"
RUN_DIR = "/run/kube-agents-sandbox"
TIER_FILE = f"{RUN_DIR}/kubectl-cost-tier"
LIMIT_FILE = f"{RUN_DIR}/memory-limit-bytes"
STATE_DIR = f"{RUN_DIR}/kubectl-admission"
LEDGER_DIR = f"{STATE_DIR}/reservations"
LEDGER_LOCK = f"{STATE_DIR}/ledger.lock"
SLOT_DIR = f"{STATE_DIR}/slots"
EVENTS_LOG = f"{STATE_DIR}/events.log"
DEFAULT_TIER = "default"
CLUSTER_SCOPE = "cluster"
NAMESPACE_SCOPE = "namespace"
OBJECT_SCOPE = "object"
MIB = 1024 * 1024
KIB = 1024
# Memory the shell keeps for everything that is not an admitted read: sshd, terminals,
# scripts. One read may take at most the guard's watermark less this.
SHELL_FLOOR_BYTES = 256 * MIB
# kubectl decodes the full objects for these outputs (`-o name` included); the plain
# table is built by the API server and costs about a third as much.
HEAVY_FORMATS = ("json", "yaml", "jsonpath", "custom-columns", "go-template", "template", "name")
# A command that reads objects takes a slot for as long as it runs: a burst of commands each
# under the guard's size floor, all growing at once, outruns any per-process rule. The shell
# takes one read's capacity / SLOT_BYTES of them at once, and at least MIN_SLOTS.
SLOT_BYTES = 96 * MIB
MIN_SLOTS = 4
# Only verbs that decode objects take a slot. Local verbs read none, and a session holds a
# stream (`exec`, `attach`, `logs -f`, `rollout status`, `wait`) open as long as it likes, so a
# slot there would let a few open streams refuse every other session's reads.
SLOTTED_VERBS = ("get", "describe", "events", "top", "logs", "diff", "apply", "delete", "api-resources")
FOLLOW_FLAGS = ("-f", "--follow")
ADMIT_WAIT_SECONDS = 30.0
ADMIT_POLL_SECONDS = 0.5
# How long to try for the ledger lock before running the command unchanged: a session
# that holds the lock must not stall every other session's reads.
LOCK_WAIT_SECONDS = 5.0
REFUSED_NOW = 3
REFUSED_BUSY = 75  # EX_TEMPFAIL
SIGNAL_EXIT_BASE = 128
SHARED_FILE_MODE = 0o644  # reservations are read by every session user
# The event log lives on the memory-backed /run; past this size it records nothing more.
EVENTS_LOG_MAX_BYTES = 1024 * 1024
TEMP_SUFFIX = ".tmp"
FALSE_VALUES = ("false", "0", "f")
# Short value flags kubectl also takes glued to their value (`-ojson`, `-lapp=x`, `-nteam`).
GLUED_SHORT_FLAGS = ("-o", "-l", "-n")
PROC_ROOT = "/proc"
# The guard's module once loaded; `rss` reads processes through it.
GUARD = None
# kind (every spelling) -> (canonical name, cluster-scoped)
KINDS: dict[str, tuple[str, bool]] = {}
for _names, _cluster in (
    (("pods", "pod", "po"), False),
    (("nodes", "node", "no"), True),
    (("events", "event", "ev"), False),
    (("services", "service", "svc"), False),
    (("configmaps", "configmap", "cm"), False),
    (("secrets", "secret"), False),
    (("endpoints", "ep"), False),
    (("serviceaccounts", "serviceaccount", "sa"), False),
    (("persistentvolumeclaims", "persistentvolumeclaim", "pvc"), False),
    (("persistentvolumes", "persistentvolume", "pv"), True),
    (("namespaces", "namespace", "ns"), True),
    (("deployments", "deployment", "deploy"), False),
    (("statefulsets", "statefulset", "sts"), False),
    (("daemonsets", "daemonset", "ds"), False),
    (("replicasets", "replicaset", "rs"), False),
    (("jobs", "job"), False),
    (("cronjobs", "cronjob", "cj"), False),
    (("networkpolicies", "networkpolicy", "netpol"), False),
    (("roles", "role"), False),
    (("rolebindings", "rolebinding"), False),
    (("clusterroles", "clusterrole"), True),
    (("clusterrolebindings", "clusterrolebinding"), True),
):
    for _n in _names:
        KINDS[_n] = (_names[0], _cluster)
# What `kubectl get all` lists.
ALL_KINDS = ("pods", "services", "deployments", "replicasets", "statefulsets", "daemonsets", "jobs", "cronjobs")
# Where a canonical kind's collection lives, for the count the refusal suggests.
API_GROUP_PATHS = {"deployments": "/apis/apps/v1", "statefulsets": "/apis/apps/v1", "daemonsets": "/apis/apps/v1",
                   "replicasets": "/apis/apps/v1", "jobs": "/apis/batch/v1", "cronjobs": "/apis/batch/v1",
                   "networkpolicies": "/apis/networking.k8s.io/v1", "roles": "/apis/rbac.authorization.k8s.io/v1",
                   "rolebindings": "/apis/rbac.authorization.k8s.io/v1",
                   "clusterroles": "/apis/rbac.authorization.k8s.io/v1",
                   "clusterrolebindings": "/apis/rbac.authorization.k8s.io/v1"}
CORE_API_PATH = "/api/v1"
UNKNOWN_COLLECTION = "/apis/<group>/<version>/<kind>"
VALUE_FLAGS = {"-n", "--namespace", "--context", "--kubeconfig", "-l", "--selector", "-o", "--output",
               "--field-selector", "--request-timeout", "--chunk-size", "--sort-by", "-L", "--label-columns",
               "--cluster", "--user", "--server", "-s", "--token", "--template", "--as", "--as-group", "--as-uid",
               "--certificate-authority", "--client-certificate", "--client-key", "--tls-server-name",
               "--username", "--password", "-v", "--v", "--raw", "--subresource", "--timeout", "--cache-dir",
               "--profile", "--profile-output", "-f", "--filename", "-c", "--container", "-k", "--kustomize",
               "--log-flush-frequency", "--vmodule", "--kuberc"}
SLOT_ALTERNATIVES = "Options: wait for the commands already running to finish and retry, or run fewer at once."
ALTERNATIVES = ("Options: to count, ask the API server for one item and read the rest from its metadata: "
                "`kubectl get --raw '{collection}?limit=1'` returns one item, and the total is that 1 plus "
                "`metadata.remainingItemCount`; to read the objects, read one namespace at a time (`kubectl get ns "
                "-o name`, then `kubectl get {kinds} -n <namespace>` for each, handling each before the next); a "
                "selector helps only when it matches few objects; or ask for a batch of at most 10 clusters.")


def parse(argv: list[str]) -> dict:
    """The parts of a kubectl command line the admission needs."""
    words, flags, i = [], {}, 0
    while i < len(argv):
        w = argv[i]
        if w[:2] in GLUED_SHORT_FLAGS and not w.startswith("--") and len(w) > 2:
            flags.setdefault(w[:2], []).append(w[2:].lstrip("="))
        elif w.startswith("-") and w != "-":
            name, eq, value = w.partition("=")
            if name in VALUE_FLAGS and not eq:
                value = argv[i + 1] if i + 1 < len(argv) else ""
                i += 1
            flags.setdefault(name, []).append(value if (eq or name in VALUE_FLAGS) else "true")
        else:
            words.append(w)
        i += 1

    def last(*names: str) -> str | None:
        for n in names:
            if n in flags:
                return flags[n][-1]
        return None

    def on(*names: str) -> bool:
        value = last(*names)
        return value is not None and value.lower() not in FALSE_VALUES

    return {
        "verb": words[0] if words else "",
        "args": words[1:],
        "all_namespaces": on("-A", "--all-namespaces"),
        "selector": bool(last("-l", "--selector") or last("--field-selector")),
        "output": last("-o", "--output") or "",
        "raw": last("--raw") is not None,
        "watch": on("-w", "--watch", "--watch-only"),
        "files": last("-f", "--filename", "-k", "--kustomize") is not None,
    }


def canonical(kind: str) -> str:
    """A kind's canonical plural (`pods.v1`, `deployments.apps` and `pod/name` included), else itself."""
    base = kind.lower().split("/", 1)[0]
    if base in KINDS:
        return KINDS[base][0]
    head = base.split(".", 1)[0]
    return KINDS[head][0] if head in KINDS else base


def kinds_of(cmd: dict) -> list[str] | None:
    """The canonical kinds a `get` lists, or None when the call is not a list."""
    if cmd["verb"] != "get" or cmd["raw"] or cmd["watch"] or cmd["files"] or not cmd["args"]:
        return None
    out: list[str] = []
    for k in cmd["args"][0].split(","):
        out += list(ALL_KINDS) if k.lower() == "all" else [canonical(k)]
    return sorted(set(out))


def heavy(output: str) -> bool:
    return output.split("=", 1)[0].lower().startswith(HEAVY_FORMATS)


def scope_of(cmd: dict, kinds: list[str]) -> str:
    if len(cmd["args"]) > 1 or "/" in cmd["args"][0]:
        return OBJECT_SCOPE
    clustered = {name for name, cluster in KINDS.values() if cluster}
    if cmd["all_namespaces"] or all(k in clustered for k in kinds):
        return CLUSTER_SCOPE
    return NAMESPACE_SCOPE


def command_key(cmd: dict) -> str | None:
    """The cost table's key: `get <kinds> <cluster|namespace|object> <json|table>`, or None if not a list."""
    kinds = kinds_of(cmd)
    if not kinds:
        return None
    return f"get {','.join(kinds)} {scope_of(cmd, kinds)} {'json' if heavy(cmd['output']) else 'table'}"


def cost(table: dict, tier: str, cmd: dict) -> tuple[int, bool]:
    """(bytes, listed) for a list: its key's entry, else the sum of its kinds' entries, else `other`."""
    column = table.get("tiers", {}).get(tier) or table.get("tiers", {}).get(DEFAULT_TIER, {})
    commands = column.get("commands", {})
    key = command_key(cmd)
    entry = commands.get(key)
    if entry is not None:
        return int(entry["peak_mib"]) * MIB, True
    kinds = kinds_of(cmd) or []
    suffix = " ".join(key.rsplit(" ", 2)[1:])  # scope and format
    listed = sum(int(commands[f"get {k} {suffix}"]["peak_mib"]) for k in kinds if f"get {k} {suffix}" in commands)
    if listed:
        return listed * MIB, True
    return int(column.get("other", {}).get("peak_mib", 0)) * MIB, False


def collection(kinds: list[str]) -> str:
    """The API path of the first kind, for a count by `?limit=1`."""
    name = kinds[0] if kinds else ""
    if name not in {n for n, _cluster in KINDS.values()}:
        return UNKNOWN_COLLECTION
    return f"{API_GROUP_PATHS.get(name, CORE_API_PATH)}/{name}"


def load_guard(path: str = GUARD_PATH):
    """The memory guard's module, for its memory figure and watermark: one formula for both."""
    loader = importlib.machinery.SourceFileLoader("shell_memory_guard", path)
    spec = importlib.util.spec_from_loader("shell_memory_guard", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def write_shared(path: str, record: dict) -> None:
    """Write a JSON file every session user can read, atomically: a reader sees the old or the new, never half."""
    temp = f"{path}{TEMP_SUFFIX}"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SHARED_FILE_MODE)
    try:
        os.fchmod(fd, SHARED_FILE_MODE)  # the session's umask must not narrow it
        os.write(fd, json.dumps(record).encode())
    finally:
        os.close(fd)
    os.replace(temp, path)


def rss(pid: int) -> int:
    """A process's memory as the guard counts it (anonymous plus shared), so the two figures agree."""
    proc = GUARD.process(PROC_ROOT, str(pid)) if GUARD else None
    return proc[2] if proc else 0


def alive(pid: int) -> bool:
    """Whether a process exists; a permission error means it does, under another user."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_reservations(ledger_dir: str) -> list[dict]:
    """Every reservation whose wrapper still runs; a dead one is skipped (and removed where permitted)."""
    out = []
    for name in os.listdir(ledger_dir):
        path = os.path.join(ledger_dir, name)
        if not name.isdecimal() or not alive(int(name)):
            if not name.endswith(TEMP_SUFFIX):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            continue
        try:
            out.append(json.loads(read(path) or "{}"))
        except ValueError:
            continue
    return out


def free_budget(high: int, used: int, reservations: list[dict], rss_of=None) -> int:
    """The watermark less memory in use outside admitted reads, less every reservation at full size."""
    rss_of = rss_of or rss
    held = sum(min(rss_of(r["child"]), r["bytes"]) for r in reservations if r.get("child"))
    return high - max(used - held, 0) - sum(r.get("bytes", 0) for r in reservations)


def log(record: dict) -> None:
    """One line in the event log, never a command line (it can hold a token); nothing past its size cap."""
    try:
        if os.path.exists(EVENTS_LOG) and os.path.getsize(EVENTS_LOG) >= EVENTS_LOG_MAX_BYTES:
            return
        with open(EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time(), 3), "uid": os.getuid(), **record}) + "\n")
    except OSError:
        pass


def pass_through(argv: list[str]) -> None:
    """Run kubectl in this process as if the admission were not there."""
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # Python ignores it; a pipe reader closing must end kubectl
    os.execv(KUBECTL_NEXT, [KUBECTL_NEXT, *argv])


def exit_code(rc: int) -> int:
    return SIGNAL_EXIT_BASE - rc if rc < 0 else rc


def advice(cmd: dict) -> str:
    kinds = kinds_of(cmd) or []
    return ALTERNATIVES.format(kinds=cmd["args"][0], collection=collection(kinds))


def run(argv: list[str], reservation: dict, cmd: dict) -> int:
    """Run kubectl as a child under the reservation. Nothing after the spawn may raise: the
    caller's fail-open path would run the command a second time."""
    child = subprocess.Popen([KUBECTL_NEXT, *argv])
    path = os.path.join(LEDGER_DIR, str(os.getpid()))
    try:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda s, _f: child.send_signal(s))
        write_shared(path, {**reservation, "child": child.pid})
    except (OSError, ValueError):
        pass  # the reservation stays at its full size without the child's pid
    rc = child.wait()
    try:
        os.unlink(path)
    except OSError:
        pass
    if rc == -signal.SIGKILL:
        sys.stderr.write(f"capacity: this read was stopped (SIGKILL), most likely by the shell's memory guard "
                         f"because the shell reached its memory limit; the install cannot take it now. "
                         f"{advice(cmd)}\n")
    return exit_code(rc)


class LockTimeout(Exception):
    pass


def _lock_timeout(_signum, _frame):
    raise LockTimeout


def take_lock(lock) -> bool:
    """Wait for the ledger lock in the kernel, not by polling, for at most LOCK_WAIT_SECONDS.

    An alarm that lands after flock returns reads as a timeout; the caller closes the file,
    which releases the lock, and tries again."""
    previous = signal.signal(signal.SIGALRM, _lock_timeout)
    try:
        try:
            signal.setitimer(signal.ITIMER_REAL, LOCK_WAIT_SECONDS)
            fcntl.flock(lock, fcntl.LOCK_EX)
            return True
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    except LockTimeout:
        return False
    finally:
        signal.signal(signal.SIGALRM, previous)


def max_slots(high: int) -> int:
    """How many kubectl commands the shell runs at once: one read's capacity in SLOT_BYTES."""
    return max(MIN_SLOTS, (high - SHELL_FLOOR_BYTES) // SLOT_BYTES)


def admit(argv: list[str], guard, table: dict, tier: str, limit: int | None) -> int:
    """Run the command if a slot is free and its charge fits, else refuse with a capacity reason."""
    cmd = parse(argv)
    used = guard.memory_in_use()
    follows = cmd["verb"] == "logs" and any(a in FOLLOW_FLAGS or a.startswith("--follow=t") for a in argv)
    if cmd["verb"] not in SLOTTED_VERBS or cmd["watch"] or follows or limit is None or used is None:
        pass_through(argv)
    kinds = kinds_of(cmd)
    charged = bool(kinds) and scope_of(cmd, kinds) == CLUSTER_SCOPE
    gvisor = guard.on_gvisor()
    high = guard.effective_watermark(limit, gvisor, guard.process_count() if gvisor else 0)
    cap = high - SHELL_FLOOR_BYTES
    slots_max = max_slots(high)
    key = command_key(cmd) if charged else None
    need = 0
    if charged:
        need, listed = cost(table, tier, cmd)
        if not listed:
            log({"event": "other", "tier": tier, "key": key})
        if need > cap and not cmd["selector"]:
            sys.stderr.write(f"capacity: `kubectl get {cmd['args'][0]}` across this fleet is measured at about "
                             f"{need // MIB} MiB, and the shell can give one read at most {cap // MIB} MiB; "
                             f"the install cannot take this read. {advice(cmd)}\n")
            log({"event": "refused-size", "tier": tier, "key": key, "need": need, "cap": cap})
            return REFUSED_NOW
    deadline = time.monotonic() + ADMIT_WAIT_SECONDS
    mine = str(os.getpid())
    slots, free, reservations = [], 0, []
    while True:
        with open(LEDGER_LOCK, "a", encoding="utf-8") as lock:
            # A command that cannot take the lock waits like one that finds no room: running it
            # unslotted is how a starved burst outgrew the shell.
            locked = take_lock(lock)
            if not locked:
                log({"event": "lock-timeout", "tier": tier, "key": key})
            else:
                slots = live_reservations(SLOT_DIR)
                free, reservations, book = 0, [], 0
            if locked and len(slots) < slots_max:
                if not charged:
                    write_shared(os.path.join(SLOT_DIR, mine), {"pid": os.getpid()})
                    break
                reservations = live_reservations(LEDGER_DIR)
                free = free_budget(high, guard.memory_in_use() or 0, reservations)
                book = min(need, free) if cmd["selector"] else need
                if book <= free and (book > 0 or not cmd["selector"]):
                    write_shared(os.path.join(SLOT_DIR, mine), {"pid": os.getpid()})
                    reservation = {"pid": os.getpid(), "bytes": book, "key": key}
                    write_shared(os.path.join(LEDGER_DIR, mine), reservation)
                    break
        if time.monotonic() >= deadline:
            if not locked:
                sys.stderr.write("capacity: the shell is too busy to admit this kubectl command now; the install "
                                 f"cannot take it now. {SLOT_ALTERNATIVES}\n")
                log({"event": "refused-lock", "tier": tier, "key": key})
            elif len(slots) >= slots_max or not charged:
                sys.stderr.write(f"capacity: the shell is already running {len(slots)} kubectl commands, the most it "
                                 f"takes at once; the install cannot take this one now. {SLOT_ALTERNATIVES}\n")
                log({"event": "refused-slots", "tier": tier, "key": key, "slots": len(slots)})
            else:
                sys.stderr.write(f"capacity: this read needs about {need // MIB} MiB and the shell has "
                                 f"{max(free, 0) // MIB} MiB free beside {len(reservations)} running reads; the "
                                 f"install cannot take it now. Retry in a minute, or narrow it. {advice(cmd)}\n")
                log({"event": "refused-busy", "tier": tier, "key": key, "need": need, "free": free})
            return REFUSED_BUSY
        time.sleep(ADMIT_POLL_SECONDS)
    if not charged:
        pass_through(argv)  # the same pid, so the slot lasts exactly as long as kubectl
    log({"event": "admitted", "tier": tier, "key": key, "need": book, "free": free})
    try:
        return run(argv, reservation, cmd)
    except OSError:
        # No process to start kubectl in: refuse, rather than let the fail-open path run it unslotted.
        sys.stderr.write(f"capacity: the shell could not start this read; the install cannot take it now. "
                         f"{SLOT_ALTERNATIVES}\n")
        log({"event": "refused-spawn", "tier": tier, "key": key})
        return REFUSED_BUSY
    finally:
        for path in (os.path.join(SLOT_DIR, mine), os.path.join(LEDGER_DIR, mine)):
            try:
                os.unlink(path)
            except OSError:
                pass


def main(argv: list[str]) -> int:
    try:
        global GUARD
        guard = GUARD = load_guard()
        with open(COST_TABLE, encoding="utf-8") as f:
            table = json.load(f)
        tier = read(TIER_FILE) or DEFAULT_TIER
        text = read(LIMIT_FILE) or ""
        limit = int(text) if text.isdecimal() and int(text) > 0 else guard.memory_limit({})
        return admit(argv, guard, table, tier, limit)
    except Exception as exc:  # noqa: BLE001 -- an admission failure must not block the command
        sys.stderr.write(f"kubectl admission skipped ({type(exc).__name__}: {exc})\n")
        pass_through(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
