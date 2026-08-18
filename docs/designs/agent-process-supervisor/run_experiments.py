#!/usr/bin/env python3
"""Experiments backing ../agent-process-supervisor.md 6.0.

Each one asserts, so this exits non-zero if a claim in the design stops holding.

    python3 run_experiments.py            # all
    python3 run_experiments.py E1 E4      # a subset

E3 is a Go check and is not run from here; see README.md for its recipe.
"""

import json
import os
import signal
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


def probe(ready_file, stale_after=3.0, liveness=False):
    """Run the 3.4 probe over the supervisor's one-line `ready` file."""
    argv = [PY, os.path.join(HERE, "probe.py"), ready_file, str(stale_after)]
    if liveness:
        argv.append("--liveness")
    return subprocess.run(argv).returncode


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
        S.READY = os.path.join(d, "opt.ready")
        table = [
            S.Supervised("session_kv", [PY, "-c", "import sys;sys.exit(1)"], required=False),
            S.Supervised("gateway", [PY, "-c", "import time;time.sleep(300)"], required=True),
        ]
        sup = S.Supervisor(table, mode="solo")
        try:
            sup.run(iterations=12)
            status = json.load(open(S.STATUS))
            assert table[0].state == "gave_up", "the optional process should have given up"
            assert status["ready"] is True, "an optional process down must NOT make the pod unready"
            assert status["degraded"] is True, "it must be reported as degraded"
            assert probe(S.READY) == 0, "the probe should report Ready while merely degraded"
        finally:
            sup.shutdown("e4 optional teardown")

        # -- degraded is keyed on "not running", not on "gave_up" ---------
        # 3.4 has ONE definition. Keyed on gave_up this reads false while the
        # process is merely restarting, 6's e2e check (one pkill, expect
        # degraded, expect it to clear) could never pass, and 3.3's rate floor
        # means a slowly-failing process would never set it at all.
        S.STATUS = os.path.join(d, "deg.json")
        S.READY = os.path.join(d, "deg.ready")
        table = [
            S.Supervised("session_kv", [PY, "-c", "import sys;sys.exit(1)"], required=False),
            S.Supervised("gateway", [PY, "-c", "import time;time.sleep(300)"], required=True),
        ]
        sup = S.Supervisor(table, mode="solo")
        try:
            sup.run(iterations=2)  # failed once; backing off, nowhere near the cap
            st = json.load(open(S.STATUS))
            assert table[0].state == "backoff", (
                f"setup: expected the optional process to be backing off, got {table[0].state}"
            )
            assert st["degraded"] is True, (
                "degraded must be true while an optional process is merely restarting; "
                "keyed on gave_up it would read false here"
            )
            assert st["ready"] is True, "and it must not touch readiness"
        finally:
            sup.shutdown("e4 degraded teardown")

        # -- required past cap: cleanup, then exit ------------------------
        S.STATUS = os.path.join(d, "req.json")
        S.READY = os.path.join(d, "req.ready")
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
        assert probe(S.READY) == 1, "readiness must fail after the supervisor gives up"
        # 3.4: the two probes must DISAGREE here. The loop is fresh, so liveness
        # passes; only readiness knows a required process is down. Sharing one
        # script would restart the container and undo 3.3's restart policy.
        assert probe(S.READY, liveness=True) == 0, (
            "liveness must ignore `ready` -- a down required process is 3.3's "
            "decision to escalate, not a probe's"
        )

    record(
        "E4",
        "the cap diverges on criticality, cleanup tells the truth, and the two probes disagree",
        "HOLDS",
        "optional past cap -> ready:true degraded:true, supervisor alive, probe Ready\n"
        "required past cap -> cleanup ran, exit 1, ready:false\n"
        "same file, same instant: readiness=1 (NotReady), liveness=0 (do NOT restart)",
    )


# ---------------------------------------------------------------- E4c
def e4c():
    """A wedged loop stops refreshing the file, and the probe must catch it."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "stale.ready")
        open(path, "w").write(f"{int(time.time())} 1\n")
        assert probe(path) == 0, "a fresh status should be Ready"
        assert probe(path, liveness=True) == 0, "and alive"
        time.sleep(3.5)  # nothing rewrites it: the loop is blocked
        assert probe(path) == 1, "a stale status must be NotReady even though ready=1"
        # BOTH probes fail on staleness -- that is the one condition they share,
        # and the one liveness exists for (3.4).
        assert probe(path, liveness=True) == 1, "a wedged loop must fail liveness too"

    record(
        "E4c",
        "staleness is detected by both probes: a hung loop cannot report healthy",
        "HOLDS",
        "identical `ready 1` line -> pass at t=0, fail at t=3.5s, readiness and liveness alike",
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
            S.READY = os.path.join(d, f"b{n}.ready")
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
    """The lease inequality, and that it catches table growth.

    First term is the RENEW DEADLINE, not a sleep -- see E11 for why that
    distinction is the whole of the fix rather than a rename. Today's rows keep
    the sleep because that is what today's script has, and they are labelled as
    the best case they are.
    """

    def margin(lease, detect, grace, n):
        return lease - (detect + grace * n)

    cases = {
        "today, sleep 5+2 (1 proc)": margin(15, 7, 10, 1),
        "today, sleep 5+2 (2 proc)": margin(15, 7, 10, 2),
        "A: lease 35, deadline 13 (2 proc)": margin(35, 13, 10, 2),
        "B: grace 4, deadline 9 (2 proc)": margin(15, 9, 4, 2),
        "C: retry 2+1, deadline 9 (2 proc)": margin(15, 9, 10, 2),
        "A+C+D: lease 35, deadline 9 (2 proc)": margin(35, 9, 10, 2),
        "A+C+D (3 proc)": margin(35, 9, 10, 3),
        # 3.5: the CONSTANT is 35, but a challenger reads what is stored on the
        # Lease, and nothing rewrites it on an existing install. See E10.
        "S3 only, STALE lease 15 (1 proc)": margin(15, 9, 10, 1),
        "S3+S4, STALE lease 15 (2 proc)": margin(15, 9, 10, 2),
    }
    expected = {
        "today, sleep 5+2 (1 proc)": -2,
        "today, sleep 5+2 (2 proc)": -12,
        "A: lease 35, deadline 13 (2 proc)": 2,
        "B: grace 4, deadline 9 (2 proc)": -2,
        "C: retry 2+1, deadline 9 (2 proc)": -14,
        "A+C+D: lease 35, deadline 9 (2 proc)": 6,
        "A+C+D (3 proc)": -4,
        "S3 only, STALE lease 15 (1 proc)": -4,
        "S3+S4, STALE lease 15 (2 proc)": -14,
    }
    for k, v in expected.items():
        assert cases[k] == v, f"{k}: design says margin {v}, computed {cases[k]}"
    assert cases["A+C+D (3 proc)"] < 0, "a third process must violate the inequality"
    assert cases["B: grace 4, deadline 9 (2 proc)"] < 0, (
        "3.5 says B no longer even reaches zero once the first term is a real "
        "deadline rather than a sleep"
    )
    assert cases["S3+S4, STALE lease 15 (2 proc)"] < cases["today, sleep 5+2 (1 proc)"], (
        "3.5 claims an unmigrated Lease makes S3+S4 WORSE than today; computed "
        f"{cases['S3+S4, STALE lease 15 (2 proc)']} vs {cases['today, sleep 5+2 (1 proc)']}"
    )

    # 3.5: the deadline is enforceable only if one full RENEW fits inside it, and
    # a renew is TWO calls (read then replace). Budgeting one is what an earlier
    # revision did, and it passes the weaker test while failing the real one.
    RETRY_MAX = 3
    NOW = {"deadline": 9, "call": 2}      # proposed
    WAS = {"deadline": 8, "call": 3}      # the revision the review caught
    for label, c in (("proposed", NOW), ("earlier", WAS)):
        one, two = RETRY_MAX + c["call"], RETRY_MAX + 2 * c["call"]
        fits_wrong, fits_right = c["deadline"] > one, c["deadline"] > two
        if label == "proposed":
            assert fits_right, f"{c} must fit a full read+write retry ({two}s)"
        else:
            assert fits_wrong, "the one-call form is what made the earlier numbers look fine"
            assert not fits_right, (
                f"deadline {c['deadline']}s vs a {two}s read+write round trip is the gap: a "
                "leader whose first renew timed out would demote without ever retrying"
            )

    detail = "\n".join(
        f"{k:38s} margin {cases[k]:+3d}s  {'OK' if cases[k] > 0 else 'VIOLATED -> refuses to start'}"
        for k in cases
    ) + (
        f"\n\nrenew deadline must fit one FULL retry (read+write):"
        f"\n  proposed  9 > 3 + 2x2 = 7   OK"
        f"\n  earlier   8 > 3 + 2x3 = 9   VIOLATED (looked fine against 3+3=6, the one-call form)"
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
            # Count only OUR OWN zombies. A machine-wide `ps` count made this
            # experiment fail on any developer box that already had one.
            "me=os.getpid()\n"
            "z=[l for l in os.popen('ps -o ppid=,stat= -x').read().splitlines()\n"
            "   if len(l.split())>1 and l.split()[0]==str(me) and 'Z' in l.split()[1]]\n"
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


# ---------------------------------------------------------------- E9
def e9():
    """A demoted leader that reacquires the lease must restart its table.

    This is the first case to run in `elected` mode at all: every other
    experiment builds a solo Supervisor, where is_leader() is hardcoded True and
    no leadership transition is ever executed. The gap it covers is a deadlock,
    not a slowdown -- a leader holding the label with nothing under it.
    """
    import supervisor as S

    live = "import time;time.sleep(300)"
    with tempfile.TemporaryDirectory() as d:
        S.STATUS = os.path.join(d, "elected.json")
        S.READY = os.path.join(d, "elected.ready")
        S.LEASE = os.path.join(d, "lease")
        table = [
            S.Supervised("session_kv", [PY, "-c", live], required=False),
            S.Supervised("gateway", [PY, "-c", live], required=True),
        ]
        sup = S.Supervisor(table, mode="elected")
        try:
            open(S.LEASE, "w").close()  # acquire
            sup.run(iterations=2)
            promoted = [p.state for p in table]
            first_pids = [p.proc.pid for p in table]
            assert all(s == "running" for s in promoted), f"leader should run its table: {promoted}"
            assert json.load(open(S.STATUS))["ready"] is True

            os.remove(S.LEASE)  # demote
            sup.run(iterations=2)
            demoted = [p.state for p in table]
            assert all(p.proc is None for p in table), "a follower must run nothing"
            assert json.load(open(S.STATUS))["ready"] is True, "followers stay Ready (3.4)"

            open(S.LEASE, "w").close()  # reacquire -- the case the design missed
            sup.run(iterations=2)
            regained = [p.state for p in table]
            second_pids = [p.proc.pid for p in table if p.proc]
            assert all(s == "running" for s in regained), (
                "3.3: a reacquired lease must restart the table, got "
                f"{regained} -- a leader holding the label and serving nothing"
            )
            assert json.load(open(S.STATUS))["ready"] is True, "a restarted table is Ready again"
            assert second_pids != first_pids, "these should be new processes, not stale handles"
        finally:
            sup.shutdown("e9 teardown")

        assert [p.state for p in table] == ["stopped", "stopped"], (
            "cleanup is terminating, so it may leave entries stopped"
        )

    record(
        "E9",
        "a demoted leader restarts its table on reacquiring the lease",
        "HOLDS",
        f"promoted {promoted} -> demoted {demoted} -> regained {regained}\n"
        f"pids {first_pids} -> {second_pids} (restarted, not resumed)\n"
        "stop() leaves entries `pending`; only cleanup leaves them `stopped`",
    )


# ---------------------------------------------------------------- E10
def e10():
    """lease_duration_seconds reaches the Lease object on the create path only.

    Parsed from the real k8s-operator/internal/controller/leader_elect.py, so
    this starts failing the moment S3 adds the renew-path write 3.5 asks for --
    which is the point: the claim is about today's script.
    """
    import ast

    repo = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
    path = os.path.join(repo, "k8s-operator", "internal", "controller", "leader_elect.py")
    tree = ast.parse(open(path).read())

    FIELD = "lease_duration_seconds"
    # V1LeaseSpec(lease_duration_seconds=...) -- the create path
    kwarg = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             if any(k.arg == FIELD for k in n.keywords)]
    # lease.spec.lease_duration_seconds = ... -- what renew/takeover would need
    stores = [t for n in ast.walk(tree) if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Attribute) and t.attr == FIELD]
    # duration = lease.spec.lease_duration_seconds or ... -- the challenger's read
    loads = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute)
             and n.attr == FIELD and isinstance(n.ctx, ast.Load)]
    # every replace_namespaced_lease call, i.e. every write that is NOT the create
    replaces = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "replace_namespaced_lease"]

    assert len(kwarg) == 1, f"expected one V1LeaseSpec(...) carrying {FIELD}, found {len(kwarg)}"
    assert not stores, (
        f"{FIELD} is assigned on {len(stores)} path(s) -- if renew/takeover now write it, "
        "3.5's migration step is done and this experiment should be retired"
    )
    assert loads, f"expected the challenger to READ the stored {FIELD}"
    assert len(replaces) >= 2, "expected renew and takeover to both replace the lease body"

    record(
        "E10",
        "raising the constant cannot reach an already-created Lease",
        "HOLDS",
        f"{FIELD}: written by 1 create (V1LeaseSpec kwarg, line {kwarg[0].lineno}), "
        f"assigned on 0 other paths,\n"
        f"read back by the expiry test at line {loads[0].lineno}; "
        f"{len(replaces)} replace_namespaced_lease calls carry the server's body unchanged\n"
        "=> an existing install keeps leaseDurationSeconds:15 after S3; see E6's STALE rows",
    )


# ---------------------------------------------------------------- E11
def e11():
    """A sleep does not bound how long a leader takes to notice; a deadline does.

    This is the one that falsified 3.5's FIRST term. The inequality used to read
    `max_poll_interval + shutdown`, with `max_poll_interval` meaning the loop's
    time.sleep -- but an iteration is the sleep PLUS an untimed lease call, so
    the term measured nothing. Here every lease call times out, and the two
    supervisors differ only in whether a renew deadline exists.
    """
    import supervisor as S

    live = "import time;time.sleep(300)"

    def stops_within(renew_deadline, budget, clamp=True):
        with tempfile.TemporaryDirectory() as d:
            S.STATUS = os.path.join(d, "s.json")
            S.READY = os.path.join(d, "s.ready")
            S.LEASE = os.path.join(d, "lease")
            open(S.LEASE, "w").close()
            table = [S.Supervised("gateway", [PY, "-c", live], required=True)]
            sup = S.Supervisor(table, mode="elected", renew_deadline=renew_deadline, clamp=clamp)
            try:
                sup.run(iterations=2)  # acquire and start the table
                assert table[0].state == "running", "setup: the leader should be running"
                # The API server stops answering. Note the LEASE FILE IS STILL
                # THERE -- this pod has not lost the lease, it has lost contact,
                # which is precisely the case a returning call can never report.
                sup.lease_call_delay = S.LEASE_CALL_TIMEOUT * 4
                t0 = time.monotonic()
                while time.monotonic() - t0 < budget and table[0].proc is not None:
                    sup.run(iterations=1)
                return table[0].proc is None, time.monotonic() - t0
            finally:
                sup.shutdown("e11 teardown")

    budget = S.RENEW_DEADLINE * 5
    fixed, t_fixed = stops_within(S.RENEW_DEADLINE, budget)
    loose, t_loose = stops_within(S.RENEW_DEADLINE, budget, clamp=False)
    prefix, t_prefix = stops_within(None, budget)

    assert fixed, (
        f"a renew deadline of {S.RENEW_DEADLINE}s must stop the table inside {budget}s "
        "even though no lease call ever completes"
    )
    assert not prefix, (
        "without a deadline the pre-fix loop should still be leading -- if it stopped, "
        "this experiment is not reproducing the gap it exists for"
    )
    # The middle arm is the review's finding: naming a deadline is not having one.
    assert loose, "the per-iteration form should still stop eventually"
    assert t_loose > t_fixed, (
        "a deadline tested once per pass, with the calls and the sleep free to run past "
        f"it, must overshoot the clamped form; got {t_loose:.2f}s vs {t_fixed:.2f}s"
    )
    assert t_fixed <= S.RENEW_DEADLINE + S.LEASE_CALL_TIMEOUT + 0.6, (
        f"clamped detection should land at about the deadline ({S.RENEW_DEADLINE}s); "
        f"took {t_fixed:.2f}s"
    )
    record(
        "E11",
        "a renew deadline bounds detection only when every blocking step is clamped to it",
        "HOLDS",
        f"every lease call timing out; deadline {S.RENEW_DEADLINE}s, budget {budget:.0f}s:\n"
        f"  calls AND wait clamped to the deadline: STOPPED after {t_fixed:.2f}s\n"
        f"  deadline tested once per iteration:     STOPPED after {t_loose:.2f}s  <- overshoots\n"
        f"  no deadline at all (pre-fix):           still leading after {t_prefix:.2f}s\n"
        "the lease file was present throughout: losing contact is not losing the lease,\n"
        "and only the local clock can tell the difference",
    )


# ---------------------------------------------------------------- E12
def e12():
    """The restart cap is a rate, and fixing the off-by-one is what exposed it."""
    import supervisor as S

    # -- the off-by-one, behaviourally. A cap of N permits N restarts. --------
    entry = S.Supervised("x", ["/nonexistent"], required=False)
    now, restarts = 0.0, 0
    while entry.state != "gave_up":
        now += 1.0
        entry.penalise(now)
        if entry.state != "gave_up":
            restarts += 1
        assert restarts <= S.RESTART_CAP + 1, "penalise() is not converging"
    assert restarts == S.RESTART_CAP, (
        f"a cap of {S.RESTART_CAP} must permit {S.RESTART_CAP} restarts before retiring; "
        f"permitted {restarts}. `>=` instead of `>` gives CAP-1"
    )

    # -- the rate floor, at the DESIGN's constants ----------------------------
    CAP, WINDOW, OLD_WINDOW = 5, 600, 300          # 3.3
    LOCK_WINDOW = 60                                # session-kv-decomposition.md 4.2
    spacings = [LOCK_WINDOW + b for b in (1, 2, 4, 8, 16)]   # + the 3.3 backoff
    span = sum(spacings)                            # 1st failure to the (CAP+1)th
    floor = WINDOW / CAP

    assert max(spacings) < floor, (
        f"the KV server fails every {max(spacings)}s, wider than the {floor:.0f}s floor a "
        f"{CAP}-in-{WINDOW}s cap gives: gave_up and degraded would be unreachable"
    )
    assert span <= WINDOW, f"{CAP + 1} failures span {span}s and must fit in {WINDOW}s"
    assert span > OLD_WINDOW, (
        f"the {OLD_WINDOW}s window is what this experiment retired -- {span}s does not fit, "
        "so the cap was unreachable the moment the off-by-one was corrected"
    )

    record(
        "E12",
        "the restart cap is a rate with a floor, and the 300 s window was below it",
        "HOLDS",
        f"a cap of {S.RESTART_CAP} permits exactly {restarts} restarts (`>` not `>=`)\n"
        f"KV server failure spacings {spacings} -> {CAP + 1} failures span {span}s\n"
        f"  {OLD_WINDOW}s window: floor {OLD_WINDOW / CAP:.0f}s, span {span}s -> UNREACHABLE\n"
        f"  {WINDOW}s window: floor {floor:.0f}s, span {span}s -> reaches the cap",
    )


# ---------------------------------------------------------------- E13
def e13():
    """Stopping must reach a GRANDCHILD, or 3.5's guarantee is about the parent only.

    The gateway shells out constantly. Popen.terminate() signals the direct
    child, so anything it spawned survives the handover, reparents to the
    supervisor, and is reaped (3.7) rather than stopped -- reaped is not the
    same as gone.

    This also pins a subtlety that a first pass at the fix got wrong: SIGTERM to
    the process GROUP is not sufficient either. A grandchild ignoring SIGTERM
    outlives a parent that honours it, wait() returns immediately, and the
    SIGKILL-after-grace branch never runs. stop() has to sweep the group.
    """
    parent = (
        "import os,signal,sys,time\n"
        "if os.fork() == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "    time.sleep(300)\n"
        "time.sleep(300)\n"          # the parent does NOT ignore SIGTERM
    )

    def grandchild_pid(path, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            try:
                return int(open(path).read())
            except (OSError, ValueError):
                time.sleep(0.05)
        raise AssertionError("the grandchild never announced itself")

    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    import supervisor as S

    results = {}
    with tempfile.TemporaryDirectory() as d:
        # -- today's shape: Popen + terminate(), no process group -------------
        marker = os.path.join(d, "gc-today")
        proc = subprocess.Popen([PY, "-c", parent, marker],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        gc = grandchild_pid(marker)
        proc.terminate()
        proc.wait(timeout=5)
        time.sleep(0.3)
        results["today"] = alive(gc)
        if results["today"]:
            os.kill(gc, signal.SIGKILL)

        # -- 3.3's shape: start_new_session + killpg, with the sweep ----------
        marker = os.path.join(d, "gc-fixed")
        entry = S.Supervised("forker", [PY, "-c", parent, marker], required=True, grace=1.0)
        entry.start(time.monotonic())
        gc = grandchild_pid(marker)
        entry.stop(final=True)
        time.sleep(0.3)
        results["fixed"] = alive(gc)
        if results["fixed"]:
            os.kill(gc, signal.SIGKILL)

    assert results["today"], (
        "expected Popen.terminate() to leave the grandchild running -- if it did not, "
        "this experiment is not reproducing the gap it exists for"
    )
    assert not results["fixed"], (
        "start_new_session + killpg + the group sweep must leave nothing behind; "
        "the grandchild survived"
    )
    record(
        "E13",
        "stopping reaches the whole process group, not just the supervised process",
        "HOLDS",
        "grandchild ignoring SIGTERM, parent honouring it:\n"
        "  Popen.terminate()                        -> grandchild SURVIVES\n"
        "  start_new_session + killpg + group sweep -> grandchild gone\n"
        "SIGTERM to the group alone would not do it: the parent exits, wait() returns,\n"
        "and the SIGKILL-after-grace branch is never reached",
    )


# ---------------------------------------------------------------- E14
def e14():
    """A supervisor told "you are not the leader" must not re-promote on a timeout.

    The renew deadline answers "nobody has told me anything". If a definitive
    denial leaves `last_renew` set, the very next timed-out call reads as "still
    inside the deadline" and restarts the whole table -- while the real holder is
    running it. Two supervisors on one table is what R6 and the whole of 3.5 exist
    to prevent, and E9 never crosses this transition because it only ever moves
    between held and not-held with the calls succeeding.
    """
    import supervisor as S

    live = "import time;time.sleep(300)"
    with tempfile.TemporaryDirectory() as d:
        S.STATUS = os.path.join(d, "s.json")
        S.READY = os.path.join(d, "s.ready")
        S.LEASE = os.path.join(d, "lease")
        open(S.LEASE, "w").close()
        table = [S.Supervised("gateway", [PY, "-c", live], required=True)]
        sup = S.Supervisor(table, mode="elected")
        try:
            sup.run(iterations=2)                       # acquire; table running
            assert table[0].state == "running", "setup: the leader should be running"
            last_ok = sup.last_renew                    # capture BEFORE the denial

            os.remove(S.LEASE)                          # a peer takes the lease
            sup.run(iterations=1)                       # ONE pass: definitive denial
            assert table[0].proc is None, "a denied leader must stop its table"
            assert sup.role == "follower"

            # Contact drops. The lease is still NOT ours, so nothing may bring the
            # table back -- but the deadline window has to still be OPEN or the
            # case proves nothing, so assert that before relying on it.
            sup.lease_call_delay = S.LEASE_CALL_TIMEOUT * 4
            elapsed = time.monotonic() - last_ok
            assert elapsed < S.RENEW_DEADLINE, (
                f"vacuous: {elapsed:.2f}s since the last successful renew already exceeds "
                f"the {S.RENEW_DEADLINE}s deadline, so nothing could re-promote regardless "
                "and this experiment is not testing what it claims"
            )
            sup.run(iterations=1)
            revived = table[0].proc is not None
            window = time.monotonic() - last_ok
        finally:
            sup.shutdown("e14 teardown")

    assert not revived, (
        "a timed-out call after a DEFINITIVE denial re-promoted the supervisor and "
        "restarted the table -- last_renew was not invalidated on the denial path"
    )
    record(
        "E14",
        "a definitive denial is not forgotten when the next lease call times out",
        "HOLDS",
        f"held -> not-held (table stopped, role=follower) -> call times out at "
        f"{window:.2f}s,\nstill inside the {S.RENEW_DEADLINE}s deadline window, so the "
        "buggy form re-promotes here\n"
        "table stays stopped: the deadline answers 'no news', never 'I was told no'",
    )


EXPERIMENTS = {"E1": e1, "E1b": e1b, "E2": e2, "E4": e4, "E4c": e4c, "E5": e5, "E6": e6,
               "E7": e7, "E8": e8, "E9": e9, "E10": e10, "E11": e11, "E12": e12, "E13": e13, "E14": e14}


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
