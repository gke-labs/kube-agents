#!/usr/bin/env python3
"""Profile each kubectl command alone and write its peak memory into deploy/sandbox/kubectl-costs.json.

The sandbox shell's kubectl admission (deploy/sandbox/kubectl-admit.py) charges a `get` the
peak this table lists for its command key, for the install's sizing tier; it estimates
nothing at run time. This script is how the table is filled: it runs every command in
COMMANDS (plus any keys given with --key-example) once, alone, against the tier's reference
cluster, measures the child's peak resident memory with getrusage, keys it with the
admission's own `command_key`, and writes the tier's column. Run it with the kubectl the
sandbox image ships, on Linux, against a cluster whose size the tier stands for:
`.agents/skills/update-kubectl-cost-table/SKILL.md` has the procedure.

Usage:
  profile_kubectl_costs.py --tier fleet --reference "2 clusters of 105,000 Pods" \\
      [--kubectl /usr/local/libexec/kubectl] [--table deploy/sandbox/kubectl-costs.json] \\
      [--key-example 'get widgets -A -o json']...
KUBECONFIG must point at the reference cluster.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import platform
import resource
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ADMIT_PATH = os.path.join(REPO, "deploy", "sandbox", "kubectl-admit.py")
DEFAULT_TABLE = os.path.join(REPO, "deploy", "sandbox", "kubectl-costs.json")
OOM_EXIT = 137
TIMEOUT_SECONDS = 1800
MIB = 1024 * 1024
KIB = 1024
# The charge for a command the table does not list: above every small command measured,
# far below the fleet-wide lists, which the table lists by name.
OTHER_MIB = 256
# Every fleet-wide `get` the agents' skills and terminals were seen to run, and the other
# cluster-wide lists that grow with the fleet. The admission charges only cluster-scope reads,
# so one namespace and named objects are not profiled; nor are Secrets, which the profiling
# Job's role cannot read.
COMMANDS = [
    "get deploy,sts,ds,cronjob,pod -A -o json",
    "get pods -A -o json",
    "get pods -A -o name",
    "get pods -A",
    "get nodes -o json",
    "get nodes",
    "get rs -A -o json",
    "get deploy -A -o json",
    "get sts -A -o json",
    "get ds -A -o json",
    "get jobs -A -o json",
    "get cronjobs -A -o json",
    "get events -A -o json",
    "get events -A",
    "get cm -A -o json",
    "get ep -A -o json",
    "get svc -A -o json",
    "get netpol,ns -A -o json",
    "get clusterroles,roles,clusterrolebindings,rolebindings -A -o json",
    "get sa -A -o json",
    "get pvc -A -o json",
    "get pv -o json",
    "get ns -o json",
]


def load_admission():
    loader = importlib.machinery.SourceFileLoader("kubectl_admit", ADMIT_PATH)
    spec = importlib.util.spec_from_loader("kubectl_admit", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def peak_bytes(kubectl: str, args: list[str]) -> tuple[int, float, int]:
    """(peak resident bytes, seconds, exit code) of one kubectl run, measured alone."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    start = time.monotonic()
    rc = subprocess.run([kubectl, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=TIMEOUT_SECONDS, check=False).returncode
    after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # ru_maxrss is the largest child so far, in KiB on Linux and bytes on macOS; commands run
    # largest-first would hide later ones, so each run is taken in its own process.
    unit = 1 if sys.platform == "darwin" else KIB
    return max(after, before) * unit, time.monotonic() - start, rc


def measure_alone(kubectl: str, args: list[str]) -> tuple[int, float, int]:
    """Run peak_bytes in a fresh interpreter, so ru_maxrss covers this command only."""
    code = ("import json,sys; sys.path.insert(0, %r); import profile_kubectl_costs as p; "
            "print(json.dumps(p.peak_bytes(%r, %r)))" % (os.path.dirname(os.path.abspath(__file__)), kubectl, args))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=TIMEOUT_SECONDS + 60,
                         check=True).stdout
    peak, seconds, rc = json.loads(out)
    return int(peak), float(seconds), int(rc)


def kubectl_version(kubectl: str) -> str:
    r = subprocess.run([kubectl, "version", "--client", "-o", "json"], capture_output=True, text=True, check=False)
    try:
        return json.loads(r.stdout)["clientVersion"]["gitVersion"]
    except (ValueError, KeyError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tier", required=True)
    ap.add_argument("--reference", required=True, help="what the reference cluster holds")
    ap.add_argument("--kubectl", default="kubectl")
    ap.add_argument("--table", default=DEFAULT_TABLE)
    ap.add_argument("--key-example", action="append", default=[], help="another command to profile")
    ap.add_argument("--keep-higher", action="store_true",
                    help="keep each entry's higher peak of this run and the tier's current column; the largest "
                         "lists vary by about 20%% between runs, so a single run can charge too little")
    args = ap.parse_args(argv)
    admit = load_admission()
    with open(args.table, encoding="utf-8") as f:
        table = json.load(f)
    commands: dict[str, dict] = {}
    killed: list[str] = []
    for line in COMMANDS + args.key_example:
        words = line.split()
        key = admit.command_key(admit.parse(words))
        peak, seconds, rc = measure_alone(args.kubectl, words)
        mib = -(-peak // MIB)
        print(f"{mib:7d} MiB {seconds:7.1f}s rc={rc}  {key} | kubectl {' '.join(words)}", flush=True)
        if rc < 0 or rc == OOM_EXIT:
            killed.append(line)  # out of memory or killed: its peak is not known, and must not be dropped
            continue
        if key is None or rc != 0:
            continue  # not a list the admission charges, or a kind the reference cluster does not serve
        entry = commands.setdefault(key, {"peak_mib": 0, "example": "kubectl " + " ".join(words)})
        if mib > entry["peak_mib"]:
            entry.update(peak_mib=mib, example="kubectl " + " ".join(words))
    if killed:
        print(f"not written: {len(killed)} command(s) were killed before they finished, so their peaks are "
              f"unknown (give the run more memory): {killed}", file=sys.stderr)
        return 1
    if args.keep_higher:
        for name, entry in table.get("tiers", {}).get(args.tier, {}).get("commands", {}).items():
            if name not in commands or entry["peak_mib"] > commands[name]["peak_mib"]:
                commands[name] = entry  # a key this run lost (a transient failure) keeps its figure
    table.setdefault("tiers", {})[args.tier] = {
        "reference": args.reference,
        "measured": time.strftime("%Y-%m-%d"),
        "kubectl": f"{kubectl_version(args.kubectl)} {platform.system().lower()}/{platform.machine()}",
        "commands": dict(sorted(commands.items())),
        "other": {"peak_mib": OTHER_MIB},
    }
    with open(args.table, "w", encoding="utf-8") as f:
        json.dump(table, f, indent=2)
        f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
