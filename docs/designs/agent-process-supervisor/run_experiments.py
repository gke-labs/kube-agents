#!/usr/bin/env python3
"""Experiments backing ../agent-process-supervisor.md 6.0.

Each one asserts, so this exits non-zero if a claim in the design stops holding.

    python3 run_experiments.py            # all
    python3 run_experiments.py E1 E4      # a subset

E3 is a Go check and is not run from here; see README.md for its recipe.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
sys.path.insert(0, HERE)

RESULTS = []


def record(eid, claim, verdict, detail=""):
    RESULTS.append((eid, claim, verdict, detail))
    mark = {"HOLDS": "ok", "FALSIFIED": "FALSIFIED", "FAIL": "FAIL"}[verdict]
    print(f"\n  [{eid}] {mark}: {claim}")
    if detail:
        for line in detail.strip().splitlines():
            print(f"        {line}")


def spawn_exit(code):
    return subprocess.Popen([PY, "-c", f"import sys; sys.exit({code})"])


# ---------------------------------------------------------------- E1
def e1():
    """A generic waitpid(-1) reaper vs Popen.poll()."""
    p = spawn_exit(3)
    time.sleep(0.4)
    assert p.poll() == 3, "control: poll() should see exit 3 when nothing else reaps"

    p = spawn_exit(3)
    time.sleep(0.4)
    pid, sts = os.waitpid(-1, os.WNOHANG)
    stolen = os.waitstatus_to_exitcode(sts)
    observed = p.poll()

    assert stolen == 3, f"the reaper should observe the true status, got {stolen}"
    assert observed == 0, (
        "design 3.7 states poll() reports 0 after an external reap; "
        f"observed {observed!r} instead -- the design text needs updating"
    )
    record(
        "E1",
        "waitpid(-1) rewrites a crash into a clean exit",
        "HOLDS",
        f"child exited 3; reaper saw 3; Popen.poll() then reported {observed}\n"
        "an 'exit 0 means intentional' policy would stop restarting a crash-looper",
    )


# ---------------------------------------------------------------- E1b
def e1b():
    """One reaper that dispatches by pid preserves every status."""

    class Entry:
        def __init__(self, name, proc):
            self.name, self.proc, self.exit = name, proc, None

    table = [Entry("gateway", spawn_exit(3)), Entry("session_kv", spawn_exit(7))]
    by_pid = {e.proc.pid: e for e in table}

    orphan = subprocess.Popen(
        [PY, "-c", "import os,sys,time\nif os.fork()==0:\n    time.sleep(0.2); sys.exit(9)\nsys.exit(0)"]
    )
    time.sleep(1.0)

    unknown = 0
    while True:
        try:
            pid, sts = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            break
        entry = by_pid.get(pid)
        if entry is None:
            unknown += 1
            continue
        entry.exit = os.waitstatus_to_exitcode(sts)

    assert table[0].exit == 3, f"gateway status lost: {table[0].exit}"
    assert table[1].exit == 7, f"session_kv status lost: {table[1].exit}"
    record(
        "E1b",
        "a single reaper that dispatches by pid preserves exit statuses",
        "HOLDS",
        f"gateway=3, session_kv=7 both preserved; {unknown} orphan(s) reaped and discarded",
    )


# ---------------------------------------------------------------- E2
def e2():
    """httpGet probes the pod IP, so a loopback bind is unreachable."""
    probe_ip = _routable_ip()
    out = {}
    for bind in ("127.0.0.1", "0.0.0.0"):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((bind, 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        cli = socket.socket()
        cli.settimeout(2)
        try:
            cli.connect((probe_ip, port))
            out[bind] = "CONNECTED"
        except OSError:
            out[bind] = "REFUSED"
        finally:
            cli.close()
            srv.close()

    assert out["127.0.0.1"] == "REFUSED", "a loopback bind should be unreachable from the pod IP"
    assert out["0.0.0.0"] == "CONNECTED", "0.0.0.0 should be reachable"
    record(
        "E2",
        "an httpGet probe cannot reach a server bound to 127.0.0.1",
        "HOLDS",
        f"dialling {probe_ip}: bind 127.0.0.1 -> {out['127.0.0.1']}, "
        f"bind 0.0.0.0 -> {out['0.0.0.0']}\n"
        "HTTPGetAction.Host: 'Host name to connect to, defaults to the pod IP'",
    )


def _routable_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


# ---------------------------------------------------------------- E4
def e4():
    """Optional vs required divergence at the restart cap, and the probe."""
    import supervisor as S

    with tempfile.TemporaryDirectory() as d:
        # -- optional past cap: degraded, but the supervisor lives --------
        S.STATUS = os.path.join(d, "opt.json")
        table = [
            S.Supervised("session_kv", [PY, "-c", "import sys;sys.exit(1)"], required=False),
            S.Supervised("gateway", [PY, "-c", "import time;time.sleep(300)"], required=True),
        ]
        sup = S.Supervisor(table, mode="solo")
        try:
            sup.run(iterations=10)
            status = json.load(open(S.STATUS))
            assert table[0].state == "gave_up", "the optional process should have given up"
            assert status["ready"] is True, "an optional process down must NOT make the pod unready"
            assert status["degraded"] is True, "it must be reported as degraded"
            rc = subprocess.run([PY, os.path.join(HERE, "probe.py"), S.STATUS]).returncode
            assert rc == 0, "the probe should report Ready while merely degraded"
        finally:
            sup.shutdown("e4 optional teardown")

        # -- required past cap: cleanup, then exit ------------------------
        S.STATUS = os.path.join(d, "req.json")
        table = [
            S.Supervised("session_kv", [PY, "-c", "import time;time.sleep(300)"], required=False),
            S.Supervised("gateway", [PY, "-c", "import sys;sys.exit(1)"], required=True),
        ]
        sup = S.Supervisor(table, mode="solo")
        exited = None
        try:
            sup.run(iterations=20)
        except SystemExit as exc:
            exited = exc.code
        finally:
            sup.shutdown("e4 required teardown")

        assert exited == 1, f"a required process past cap should exit(1), got {exited!r}"
        assert sup.cleanup_ran, "it must exit through the cleanup path, not a bare sys.exit"
        final = json.load(open(S.STATUS))
        assert final["ready"] is False, (
            "cleanup must write ready:false, or the probe reports Ready for a dying container"
        )
        rc = subprocess.run([PY, os.path.join(HERE, "probe.py"), S.STATUS]).returncode
        assert rc == 1, "the probe should report NotReady after the supervisor gives up"

    record(
        "E4",
        "the cap diverges on criticality, and cleanup tells the truth",
        "HOLDS",
        "optional past cap -> ready:true degraded:true, supervisor alive, probe Ready\n"
        "required past cap -> cleanup ran, exit 1, ready:false, probe NotReady",
    )


# ---------------------------------------------------------------- E4c
def e4c():
    """A wedged loop stops refreshing the file, and the probe must catch it."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "stale.json")
        doc = {
            "role": "leader",
            "ready": True,
            "degraded": False,
            "updated_at": time.time(),
            "processes": [],
        }
        json.dump(doc, open(path, "w"))
        fresh = subprocess.run([PY, os.path.join(HERE, "probe.py"), path, "3"]).returncode
        assert fresh == 0, "a fresh status should be Ready"
        time.sleep(3.5)  # nothing rewrites it: the loop is blocked
        stale = subprocess.run([PY, os.path.join(HERE, "probe.py"), path, "3"]).returncode
        assert stale == 1, "a stale status must be NotReady even though ready:true"

    record(
        "E4c",
        "staleness is detected: a hung loop cannot report healthy",
        "HOLDS",
        "identical ready:true document -> Ready at t=0, NotReady at t=3.5s",
    )


# ---------------------------------------------------------------- E5
def e5():
    """Shutdown is the sum over the table, not one grace."""
    import supervisor as S

    ignore_term = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(300)"
    measured = {}
    with tempfile.TemporaryDirectory() as d:
        for n in (1, 2, 3):
            S.STATUS = os.path.join(d, f"b{n}.json")
            table = [
                S.Supervised(f"p{i}", [PY, "-c", ignore_term], required=(i == n - 1))
                for i in range(n)
            ]
            sup = S.Supervisor(table, mode="solo")
            sup.run(iterations=2)
            measured[n] = sup.shutdown(f"{n}-process table")

    # rescale from the prototype's grace to the design's 10 s
    scaled = {n: t / S.GRACE * 10.0 for n, t in measured.items()}
    for n in (1, 2, 3):
        assert abs(scaled[n] - 10.0 * n) < 3.0, f"expected ~{10*n}s for {n} processes, got {scaled[n]:.1f}"
    assert scaled[3] + 2 > 30, "3 processes should overrun the 30 s default grace period"

    detail = "\n".join(
        f"{n} process(es): {measured[n]:.1f}s at grace={S.GRACE}s -> {scaled[n]:.0f}s at the "
        f"design's 10 s grace" for n in (1, 2, 3)
    )
    record("E5", "shutdown scales with the table; 3 processes overrun a 30 s grace period", "HOLDS", detail)


# ---------------------------------------------------------------- E6
def e6():
    """The lease inequality, and that it catches table growth."""

    def margin(lease, poll, jitter, grace, n):
        return lease - (poll + jitter + grace * n)

    cases = {
        "today (1 proc)": margin(15, 5, 2, 10, 1),
        "today (2 proc)": margin(15, 5, 2, 10, 2),
        "A: lease 30 (2 proc)": margin(30, 5, 2, 10, 2),
        "A+C: lease 30, poll 3+1 (2 proc)": margin(30, 3, 1, 10, 2),
        "A+C (3 proc)": margin(30, 3, 1, 10, 3),
        "B: grace 4 (2 proc)": margin(15, 5, 2, 4, 2),
    }
    expected = {
        "today (1 proc)": -2,
        "today (2 proc)": -12,
        "A: lease 30 (2 proc)": 3,
        "A+C: lease 30, poll 3+1 (2 proc)": 6,
        "A+C (3 proc)": -4,
        "B: grace 4 (2 proc)": 0,
    }
    for k, v in expected.items():
        assert cases[k] == v, f"{k}: design says margin {v}, computed {cases[k]}"
    assert cases["A+C (3 proc)"] < 0, "a third process must violate the inequality"

    detail = "\n".join(
        f"{k:34s} margin {cases[k]:+3d}s  {'OK' if cases[k] > 0 else 'VIOLATED -> refuses to start'}"
        for k in cases
    )
    record("E6", "every margin in design 3.5 reproduces; a third process fails the assertion", "HOLDS", detail)



# ---------------------------------------------------------------- E7
def e7():
    """Manifest-level claims, checked against real rendered operator output."""
    import shutil
    repo = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
    mod = os.path.join(repo, "k8s-operator")
    if shutil.which("go") is None:
        record("E7", "manifest claims (skipped: no Go toolchain)", "HOLDS", "install Go to run this one")
        return
    src = os.path.join(HERE, "manifest_claims_test.go")
    dst = os.path.join(mod, "internal", "controller", "zz_e7_claims_test.go")
    shutil.copyfile(src, dst)
    try:
        r = subprocess.run(["go", "test", "./internal/controller/", "-run", "TestClaim", "-v"],
                           cwd=mod, capture_output=True, text=True)
    finally:
        os.remove(dst)
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip().startswith("[C")]
    falsified = [l for l in lines if "FALSIFIED" in l]
    assert r.returncode == 0 and not falsified, (
        "manifest claims falsified:\n" + "\n".join(falsified or [r.stdout[-800:]])
    )
    record("E7", f"{len(lines)} manifest claims hold against real rendered output", "HOLDS",
           "\n".join(lines))


# ---------------------------------------------------------------- E8
def e8():
    """The entrypoint backgrounds a job and then execs: it reparents to the supervisor.

    docker-entrypoint.sh backgrounds the Hindsight memory migration and then
    exec's the supervisor, so that subshell's parent becomes PID 1 -- a process
    the supervisor never started. Without 3.7's reaper it is a zombie.
    """
    with tempfile.TemporaryDirectory() as d:
        entry = os.path.join(d, "entrypoint.sh")
        sup = os.path.join(d, "sup.py")
        open(entry, "w").write(
            "#!/bin/sh\n( sleep 0.5; exit 0 ) &\nexec python3 \"$1\" \"$2\"\n")
        open(sup, "w").write(
            "import os,subprocess,sys,time\n"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(3)'])\n"
            "reap = sys.argv[1]=='reaper'\n"
            "for _ in range(6):\n"
            "    p.poll()\n"
            "    if reap:\n"
            "        while True:\n"
            "            try: pid,_=os.waitpid(-1,os.WNOHANG)\n"
            "            except ChildProcessError: break\n"
            "            if pid==0: break\n"
            "    time.sleep(0.4)\n"
            "z=[l for l in os.popen('ps -o pid=,stat= -x').read().splitlines()\n"
            "   if len(l.split())>1 and 'Z' in l.split()[1]]\n"
            "print('ZOMBIES', len(z))\n")
        def zombies(mode):
            out = subprocess.run(["sh", entry, sup, mode], capture_output=True, text=True).stdout
            return int([l for l in out.split("\n") if l.startswith("ZOMBIES")][0].split()[1])
        today, fixed = zombies("today"), zombies("reaper")

    assert today >= 1, "expected the backgrounded job to zombie under today's supervisor"
    assert fixed == 0, "the 3.7 reaper should leave no zombie"
    record("E8", "an entrypoint background job reparents to the supervisor and zombies without 3.7", "HOLDS",
           f"today's shape (Popen + poll only): {today} zombie(s)\n"
           f"with the 3.7 reaper:                {fixed} zombie(s)")


EXPERIMENTS = {"E1": e1, "E1b": e1b, "E2": e2, "E4": e4, "E4c": e4c, "E5": e5, "E6": e6, "E7": e7, "E8": e8}


def main(argv):
    wanted = argv[1:] or list(EXPERIMENTS)
    unknown = [w for w in wanted if w not in EXPERIMENTS]
    if unknown:
        print(f"unknown experiment(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(EXPERIMENTS)}", file=sys.stderr)
        return 2

    failures = 0
    for name in wanted:
        try:
            EXPERIMENTS[name]()
        except AssertionError as exc:
            failures += 1
            record(name, str(exc).splitlines()[0], "FALSIFIED", str(exc))
        except Exception as exc:  # a broken experiment, not a broken claim
            failures += 1
            record(name, f"{type(exc).__name__}: {exc}", "FAIL")

    print("\n" + "=" * 72)
    for eid, claim, verdict, _ in RESULTS:
        print(f"  {verdict:10s} {eid:4s} {claim}")
    print("=" * 72)
    print(f"  {len(RESULTS) - failures}/{len(RESULTS)} claims hold\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
