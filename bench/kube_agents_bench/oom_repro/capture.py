"""What the harness records while it drives a container to an OOMKill, and how it reads it back.

The sampler runs inside the watched container and prints one JSON line every SAMPLE_SECONDS: the
cgroup's memory and every process over MIN_RSS_KIB with its command line, itself excepted.
``kills`` reads the Pod status for OOMKilled restarts; ``sample_at_kill`` finds the last sample of
the killed pod before each of them; ``breakdown`` turns a sample into each process class's share.
"""
import collections
import re

SAMPLE_SECONDS = 2
MIN_RSS_KIB = 4096
CMDLINE_CHARS = 600
KIB = 1024
MIB = 2**20
OOM_REASON = "OOMKilled"
CGROUP_CURRENT = "/sys/fs/cgroup/memory.current"
CGROUP_PEAK = "/sys/fs/cgroup/memory.peak"
# A kill's finishedAt is rounded down to the second, so a sample up to this long after it can still
# be the last one before the kill.
FINISHED_AT_ROUNDING_SECONDS = 1
# A sample older than this before the kill says too little about the memory at the kill.
KILL_SAMPLE_MAX_AGE_SECONDS = 15
SAMPLER_SCRIPT = f'''
import os, time, json
def rd(p):
    try: return open(p).read().strip()
    except Exception: return None
me = str(os.getpid())
def own_rss():
    st = rd(f"/proc/{{me}}/status") or ""
    return int(dict(l.split(":", 1) for l in st.splitlines() if ":" in l).get("VmRSS", "0 kB").split()[0])
while True:
    procs = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == me: continue
        st = rd(f"/proc/{{pid}}/status")
        if not st: continue
        f = dict(l.split(":", 1) for l in st.splitlines() if ":" in l)
        rss = int(f.get("VmRSS", "0 kB").split()[0])
        if rss < {MIN_RSS_KIB}: continue
        cmd = (rd(f"/proc/{{pid}}/cmdline") or "").replace("\\0", " ").strip()[:{CMDLINE_CHARS}]
        procs.append([int(pid), int(f.get("PPid", "0")), rss, cmd])
    print(json.dumps({{"ts": time.time(), "cg_current": rd("{CGROUP_CURRENT}"),
                      "cg_peak": rd("{CGROUP_PEAK}"), "sampler_rss_kib": own_rss(),
                      "procs": procs}}), flush=True)
    time.sleep({SAMPLE_SECONDS})
'''
# kubectl flags that take a separate value, skipped with it before the resource names.
KUBECTL_VALUE_FLAGS = ("-n", "--namespace", "--context", "--kubeconfig", "-l", "--selector", "-o", "--output",
                       "--field-selector", "--request-timeout", "--chunk-size", "--sort-by", "-L", "--label-columns",
                       "-f", "--filename", "-c", "--container", "--cluster", "--user", "--server", "-s", "--token",
                       "--template")
ALL_NAMESPACES_FLAGS = ("-A", "--all-namespaces")
HERMES_EXECUTABLE = "/bin/hermes"
# A Python process is named for the script it runs: collect.py, fleet_upgrade_report.py.
PYTHON_SCRIPT = re.compile(r"python[0-9.]*\s+(?:-\S+\s+)*\S*?([A-Za-z0-9_\-]+\.py)\b")
# Checked before gcloud, whose name it contains.
AUTH_PLUGIN = "gke-gcloud-auth-plugin"
# Named processes that are not Python scripts (a script is named by its file, PYTHON_SCRIPT).
NAMED_PROCESSES = ("k8s-event-watcher", "envoy")


def process_class(cmd: str) -> str:
    """Group a process by what it is, keeping what a kubectl child lists."""
    words = cmd.split()
    if words and words[0].endswith("kubectl"):
        return kubectl_class(words)
    if AUTH_PLUGIN in cmd:
        return AUTH_PLUGIN
    if "gcloud" in cmd:
        return "gcloud"
    script = PYTHON_SCRIPT.search(cmd)
    if script:
        return script.group(1)
    if HERMES_EXECUTABLE in cmd:
        return "hermes"
    for name in NAMED_PROCESSES:
        if name in cmd:
            return name
    return "other"


def kubectl_class(words: list[str]) -> str:
    """`kubectl <subcommand> <resource>`, with -A when it lists every namespace, from a kubectl command line.

    Flags before or after the subcommand are skipped, with their value when it is a separate word.
    """
    everywhere, plain, i = False, [], 1
    while i < len(words):
        w = words[i]
        name = w.split("=", 1)[0]
        if name in ALL_NAMESPACES_FLAGS:
            everywhere = everywhere or "=" not in w or w.split("=", 1)[1] != "false"
        elif w.startswith("-"):
            if "=" not in w and name in KUBECTL_VALUE_FLAGS:
                i += 1
        else:
            plain.append(w)
        i += 1
    sub = plain[0] if plain else "?"
    resource = plain[1] if sub == "get" and len(plain) > 1 else ""
    return " ".join(x for x in ("kubectl", sub, resource) if x) + (" -A" if everywhere else "")

def kills(pod: dict) -> list[dict]:
    """One entry per container whose last termination was an OOMKill, from a Pod's JSON."""
    out = []
    statuses = pod.get("status", {})
    for s in statuses.get("containerStatuses", []) + statuses.get("initContainerStatuses", []):
        last = s.get("lastState", {}).get("terminated", {})
        if last.get("reason") == OOM_REASON:
            out.append({"pod": pod["metadata"]["name"], "container": s["name"], "restarts": s.get("restartCount", 0),
                        "exit_code": last.get("exitCode"), "finished": last.get("finishedAt")})
    return out


def sample_at_kill(samples: list[dict], pod: str, finished: float) -> dict | None:
    """The killed pod's last sample at or before ``finished``, if it is recent enough to describe the kill."""
    before = [x for x in samples if x.get("pod") == pod and x["ts"] <= finished + FINISHED_AT_ROUNDING_SECONDS]
    if not before or finished - before[-1]["ts"] > KILL_SAMPLE_MAX_AGE_SECONDS:
        return None
    return before[-1]


def breakdown(sample: dict) -> list[tuple[str, float, int]]:
    """Each process class's share of the summed RSS in one sample: (class, percent, MiB), largest first.

    Shares are of the summed RSS, not of the cgroup: processes that map the same library pages each
    count them, so RSS adds up to a little more than the cgroup and shares of it add up to 100.
    """
    totals = collections.Counter()
    for _pid, _ppid, rss, cmd in sample["procs"]:
        totals[process_class(cmd)] += rss
    whole = sum(totals.values()) or 1
    return [(k, 100 * v / whole, v // KIB) for k, v in totals.most_common()]
