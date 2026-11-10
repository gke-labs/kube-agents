import os, time, subprocess, sys, signal, random
import urllib.request, urllib.error
from datetime import datetime, timezone
from kubernetes import client, config
from kubernetes.client.rest import ApiException

lease_name = os.environ.get("LEADER_ELECTION_LEASE_NAME")
namespace = os.environ.get("LEADER_ELECTION_NAMESPACE")
pod_name = os.environ.get("HOSTNAME")
process = None
# label_state is whether this pod carries the leader label as far as the API
# server has confirmed: True, False, or None before the first patch of this
# process. Every loop reconciles it to what the label should say, so a refused
# patch is retried; a crash clears the label before the wrapper exits, and a
# wrapper that restarts on a pod still labelled, because that clear was refused,
# clears it before anything selects on it.
label_state = None
is_shutting_down = False
# lease_renewed_at is the time.monotonic() reading taken when the last write
# confirmed this pod holds the lease, or None before the first. A draining
# Hermes is killed once LEASE_RENEW_DEADLINE_SECONDS pass from it with no newer one.
lease_renewed_at = None
# The label means "this pod holds the lease and its Hermes answers": the
# Service routes on it, and the operator's usage poller and the chart's chat
# PodMonitoring read the chat listener from the pod that carries it. Hermes
# binds its API port only once the entrypoint has scaffolded the profiles,
# minutes after the lease is taken, so the label follows the first answer on
# that port rather than the lease alone.
HERMES_API_URL = "http://127.0.0.1:8642/api/sessions?limit=1"
HERMES_PROBE_TIMEOUT_SECONDS = 3
# ProxyHandler({}) rather than urlopen's default opener, which reads http_proxy
# from the environment: a proxy set on the container (an AgentPlugin's env, a
# base image) would carry a loopback probe off the pod, and its answer would
# then say nothing about this pod's Hermes.
HERMES_PROBE_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# The gateway command this wrapper supervises.
#
# At one replica the operator puts the whole command line in the container's args and
# this file never runs; above one it runs instead of that command line, so anything the
# args would have carried has to arrive as an environment variable. The operator always
# sets HERMES_GATEWAY_PROFILE — empty unless spec.harness.experimental.platformFrontDoor
# names a profile, so that an AgentPlugin's spec.env cannot become its only writer — and
# empty means the default profile, which is the `hermes gateway run` this file has always
# supervised.
#
# `--profile` is a GLOBAL flag and its position is load-bearing: hermes_cli/main.py
# pre-parses it out of argv before any import and re-points HERMES_HOME at that profile's
# home. `hermes gateway run --profile platform` is not the same command — the subcommand
# has no such flag — so it must sit ahead of `gateway`.
_gateway_profile = (os.environ.get("HERMES_GATEWAY_PROFILE") or "").strip()
HERMES_COMMAND = ["hermes"] + (["--profile", _gateway_profile] if _gateway_profile else []) + ["gateway", "run"]

# The most the supervisor waits between process.terminate() (SIGTERM) and
# process.kill() (SIGKILL) while this pod still holds the lease; it kills sooner
# when another pod holds the lease, which on a lost-leadership pass means at
# once, or when no renewal is confirmed for LEASE_RENEW_DEADLINE_SECONDS (see
# stop_hermes_holding_lease). Kubernetes gives the gateway pod
# terminationGracePeriodSeconds (60s), during which Hermes drains in-flight cron
# runs (agent.cron_drain_timeout, default 30s) and keeps a 10s cleanup reserve
# (40s total). Sizing this at 50s gives Hermes its full 40s stop budget with a
# 10s margin before leader_elect.py forces termination. Of the 10s left before
# the kubelet SIGKILLs the container, clearing the label and releasing the lease
# take at most three LEASE_API_TIMEOUT_SECONDS calls. A plain constant,
# not an environment read: the operator never sets such a variable, so an
# AgentPlugin's spec.env would be its only writer, and a value that failed to
# parse would stop every replica at import.
GATEWAY_SHUTDOWN_TIMEOUT_SECONDS = 50

# The lease. A standby takes it once renew_time is LEASE_DURATION_SECONDS old,
# and starts Hermes in the same loop. The leader renews it every loop, which
# sleeps LEASE_POLL_INTERVAL_SECONDS plus up to LEASE_POLL_JITTER_SECONDS.
LEASE_DURATION_SECONDS = 15
LEASE_POLL_INTERVAL_SECONDS = 5
LEASE_POLL_JITTER_SECONDS = 2
# Per-request timeout on the API calls made while Hermes drains and at the
# release after it, on clients built with retries off so that one call is one
# attempt (urllib3 otherwise retries three times, each with a fresh timeout).
# Without it a hung call would block the drain loop past the lease's expiry, and
# Hermes would still be running when a standby starts its own.
LEASE_API_TIMEOUT_SECONDS = 2
# A renewal is a read and then a replace; an unconfirmed one is retried after
# LEASE_RENEWAL_RETRY_SECONDS (client-go's RetryPeriod) rather than a full poll
# interval, so a transient error mid-drain gets multiple attempts before the
# renew deadline.
LEASE_RENEWAL_MAX_SECONDS = 2 * LEASE_API_TIMEOUT_SECONDS
LEASE_RENEWAL_RETRY_SECONDS = LEASE_API_TIMEOUT_SECONDS
# Clock skew between nodes that the margin before a standby's takeover absorbs.
LEASE_CLOCK_SKEW_SECONDS = 1
# While Hermes drains, it is SIGKILLed once this long passes with no confirmed
# renewal, and an entry renewal is attempted past this deadline only while the
# renewal plus LEASE_CLOCK_SKEW_SECONDS can still finish inside
# LEASE_DURATION_SECONDS. The LEASE_DURATION_SECONDS - LEASE_RENEW_DEADLINE_SECONDS
# left before a standby may take the lease covers a renewal already in flight
# (LEASE_RENEWAL_MAX_SECONDS) and LEASE_CLOCK_SKEW_SECONDS of clock skew between
# nodes, the same split client-go makes between its renew deadline and lease
# duration.
LEASE_RENEW_DEADLINE_SECONDS = 10

# Tradeoff documented explicitly per review:
# This active/passive failover blackholes traffic during leader transition.
# The Service selector requires 'kubeagents.io/is-leader=true', which is only carried by the active leader.
# During failover, there is a window with zero ready endpoints until the new leader acquires the lease and labels itself.
# This introduces a single point of failure during failovers, rather than request-continuous HA.
# On SIGTERM the leader keeps renewing the lease until its Hermes exits, then releases it. The
# hand-off waits for the drain, so a slow drain delays the standby by up to
# GATEWAY_SHUTDOWN_TIMEOUT_SECONDS, and a standby cannot take the lease while Hermes drains,
# short of clock skew between nodes larger than the margin LEASE_RENEW_DEADLINE_SECONDS leaves.
# The steady-state loop's own lease calls carry no timeout, so a hung call there can still
# outlast the lease while Hermes runs; only the drain and release are bounded.

def update_pod_label(v1, is_leader, request_timeout=None):
    """Stamp or clear the leader label on this pod; True when the API accepted it."""
    try:
        body = {"metadata": {"labels": {"kubeagents.io/is-leader": "true" if is_leader else None}}}
        bound = {} if request_timeout is None else {"_request_timeout": request_timeout}
        v1.patch_namespaced_pod(name=pod_name, namespace=namespace, body=body, **bound)
        return True
    except ApiException as e:
        print(f"[LeaderElect] Error patching pod label: {e}", file=sys.stderr, flush=True)
        return False


def hermes_answers(opener=None):
    """Whether this pod's Hermes answers on its API port: any HTTP answer counts,
    a 401 included, since the probe carries no key; a refused connection or a
    timeout does not."""
    request = urllib.request.Request(HERMES_API_URL)
    try:
        (opener or HERMES_PROBE_OPENER.open)(request, timeout=HERMES_PROBE_TIMEOUT_SECONDS)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001 -- refused, timed out, not resolvable: not answering
        return False


def desired_label(holder, running, answers):
    """What the leader label should say: this pod holds the lease, its Hermes
    process is running, and that process answers on its API port."""
    return holder == pod_name and running and answers


def exit_on_crash(v1, state, code):
    """Clear the label, then exit so the pod restarts: the re-scaffold between
    this exit and the next wrapper's first loop is minutes, and a pod labelled
    for that long with no Hermes behind it is read as the leader by the operator
    and the chat PodMonitoring. A refused clear is left to the restarted wrapper,
    which starts with label_state None and reconciles before it leads again."""
    reconcile_label(v1, False, state)
    sys.exit(code)


def reconcile_label(v1, desired, state):
    """Bring the pod's label to `desired` when the confirmed state differs or is
    unknown, and return the confirmed state: unchanged when the API refused,
    so the next loop tries again."""
    if state is desired:
        return state
    if update_pod_label(v1, desired):
        return desired
    return state

def bounded_api_clients():
    """Coordination and core clients for the drain and the release, with retries
    off, so each call's _request_timeout bounds the whole call."""
    configuration = client.Configuration.get_default_copy()
    configuration.retries = 0
    api_client = client.ApiClient(configuration)
    return client.CoordinationV1Api(api_client), client.CoreV1Api(api_client)


def renew_held_lease(coordination_v1):
    """Renew the lease if this pod still holds it: True when the API accepted
    the renewal, False when another pod holds it (or nobody does), None when
    the API could not say. The replace carries the resourceVersion the read
    returned, so it cannot overwrite a standby that took the lease in between."""
    try:
        lease = coordination_v1.read_namespaced_lease(
            name=lease_name, namespace=namespace, _request_timeout=LEASE_API_TIMEOUT_SECONDS)
        if lease.spec.holder_identity != pod_name:
            return False
        lease.spec.renew_time = datetime.now(timezone.utc)
        coordination_v1.replace_namespaced_lease(
            name=lease_name, namespace=namespace, body=lease, _request_timeout=LEASE_API_TIMEOUT_SECONDS)
        return True
    except Exception as e:  # noqa: BLE001 -- refused, timed out, unreachable: unconfirmed, and the drain decides
        print(f"[LeaderElect] Error renewing lease while Hermes drains: {e}", file=sys.stderr, flush=True)
        return None


def stop_hermes_holding_lease(coordination_v1):
    """SIGTERM Hermes and wait for it to exit, renewing the lease every
    LEASE_POLL_INTERVAL_SECONDS meanwhile, so no standby starts a second gateway
    while this one drains. The wait stays on the main thread and the main loop
    does not run during it, so this is the only renewal there is.

    Hermes is SIGKILLed instead of waited on when the shutdown budget runs out,
    when the lease no longer names this pod, or when LEASE_RENEW_DEADLINE_SECONDS
    pass with no confirmed renewal, before a standby may take the lease. In
    each case two gateways would otherwise run against the same home.

    `coordination_v1` should come from bounded_api_clients()."""
    global lease_renewed_at
    process.terminate()
    deadline = time.monotonic() + GATEWAY_SHUTDOWN_TIMEOUT_SECONDS
    first_slice = True
    while True:
        now = time.monotonic()
        if now >= deadline:
            print(f"[{pod_name}] Hermes did not exit in time, killing...", flush=True)
            break
        # On entry, a 3s probe plus a 5-7s sleep after the main loop's renewal
        # can already reach LEASE_RENEW_DEADLINE_SECONDS while the 15s lease is
        # still valid, so the first slice tries a renewal provided the renewal
        # and clock skew still fit inside LEASE_DURATION_SECONDS (which is
        # LEASE_RENEW_DEADLINE_SECONDS).
        if lease_renewed_at is None or (
            now + LEASE_RENEWAL_MAX_SECONDS + LEASE_CLOCK_SKEW_SECONDS > lease_renewed_at + LEASE_DURATION_SECONDS
            if first_slice else
            now >= lease_renewed_at + LEASE_RENEW_DEADLINE_SECONDS
        ):
            print(f"[{pod_name}] Lease renewal unconfirmed past its deadline, killing Hermes...", flush=True)
            break
        first_slice = False
        # A renewal starts only if it can finish inside the budget, so the kill
        # and the release after it stay inside the pod's grace period.
        renewed = None
        if now + LEASE_RENEWAL_MAX_SECONDS <= deadline:
            renewed = renew_held_lease(coordination_v1)
            if renewed is False:
                print(f"[{pod_name}] This pod no longer holds the lease, killing Hermes...", flush=True)
                break
            if renewed:
                lease_renewed_at = now
        if time.monotonic() >= lease_renewed_at + LEASE_RENEW_DEADLINE_SECONDS:
            print(f"[{pod_name}] Lease renewal unconfirmed past its deadline, killing Hermes...", flush=True)
            break
        # Wake for the next renewal (or retry after an unconfirmed one), the
        # budget, or the renew deadline, whichever comes first; a timeout of 0
        # still sees an exit.
        next_attempt = LEASE_POLL_INTERVAL_SECONDS if renewed else LEASE_RENEWAL_RETRY_SECONDS
        wake_at = min(now + next_attempt, deadline, lease_renewed_at + LEASE_RENEW_DEADLINE_SECONDS)
        try:
            process.wait(timeout=max(wake_at - time.monotonic(), 0))
            return
        except subprocess.TimeoutExpired:
            continue
    process.kill()
    process.wait()


def release_lease_and_exit(signum, frame):
    global is_shutting_down
    if is_shutting_down:
        return
    is_shutting_down = True
    print(f"[{pod_name}] Received signal {signum}, shutting down...", flush=True)

    coordination_v1 = v1 = None
    try:
        config.load_incluster_config()
        coordination_v1, v1 = bounded_api_clients()
    except Exception as e:  # noqa: BLE001 -- Hermes still has to be stopped; the drain treats the lease as unconfirmed
        print(f"[LeaderElect] Error building API clients: {e}", file=sys.stderr, flush=True)

    # This pod keeps the lease and the leader label until Hermes has exited, so
    # a standby cannot start a second gateway while this one drains.
    if process is not None:
        stop_hermes_holding_lease(coordination_v1)

    # Hermes is gone: clear the label, then hand the lease back so a standby
    # takes it on its next loop rather than after LEASE_DURATION_SECONDS. The
    # label goes whoever holds the lease, since it means this pod's Hermes
    # answers.
    try:
        update_pod_label(v1, False, request_timeout=LEASE_API_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001 -- the lease release below must still run
        print(f"[LeaderElect] Error clearing pod label: {e}", file=sys.stderr, flush=True)
    try:
        lease = coordination_v1.read_namespaced_lease(
            name=lease_name, namespace=namespace, _request_timeout=LEASE_API_TIMEOUT_SECONDS)
        if lease.spec.holder_identity == pod_name:
            print(f"[{pod_name}] Releasing lease...", flush=True)
            lease.spec.holder_identity = None
            coordination_v1.replace_namespaced_lease(
                name=lease_name, namespace=namespace, body=lease, _request_timeout=LEASE_API_TIMEOUT_SECONDS)
    except Exception as e:
        print(f"[LeaderElect] Error releasing lease: {e}", file=sys.stderr, flush=True)

    sys.exit(0)

def main():
    global process, is_shutting_down, label_state, lease_renewed_at

    # Every replica > 1 deployment reaches Hermes through this file instead of
    # agent-entrypoint: the operator sets a container `command`, which overrides
    # the image ENTRYPOINT, so the umask that script sets never runs here. It has
    # to be set again, or the sandbox writes 0755 directories on the shared PVC
    # and the credential sidecar — a different UID in the same group since the
    # UID split — cannot clone into the GitOps workspace the sandbox just made.
    # Both ways out of this function start Hermes, so it belongs above both.
    os.umask(0o002)

    if not lease_name or not namespace:
        os.execvp("hermes", HERMES_COMMAND)
        
    signal.signal(signal.SIGTERM, release_lease_and_exit)
    signal.signal(signal.SIGINT, release_lease_and_exit)
    
    config.load_incluster_config()
    coordination_v1 = client.CoordinationV1Api()
    v1 = client.CoreV1Api()
    drain_coordination_v1, _ = bounded_api_clients()
    
    while not is_shutting_down:
        now = datetime.now(timezone.utc)
        holder = None
        lease_error = False
        
        try:
            lease = coordination_v1.read_namespaced_lease(name=lease_name, namespace=namespace)
            holder = lease.spec.holder_identity
            
            if holder == pod_name:
                lease.spec.renew_time = datetime.now(timezone.utc)
                stamped_at = time.monotonic()
                try:
                    coordination_v1.replace_namespaced_lease(name=lease_name, namespace=namespace, body=lease)
                    lease_renewed_at = stamped_at
                except ApiException:
                    holder = None
                    lease_error = True
            else:
                renew_time = lease.spec.renew_time
                duration = lease.spec.lease_duration_seconds or LEASE_DURATION_SECONDS
                
                is_expired = False
                if renew_time:
                    # Kubernetes client automatically parses ISO times into datetime objects
                    if (now - renew_time).total_seconds() > duration:
                        is_expired = True
                else:
                    # Fail-safe: if renew_time is somehow unparseable/missing, treat as expired
                    is_expired = True
                        
                if holder is None or is_expired:
                    write_now = datetime.now(timezone.utc)
                    lease.spec.holder_identity = pod_name
                    lease.spec.renew_time = write_now
                    lease.spec.acquire_time = write_now
                    lease.spec.lease_transitions = (lease.spec.lease_transitions or 0) + 1
                    stamped_at = time.monotonic()
                    try:
                        coordination_v1.replace_namespaced_lease(name=lease_name, namespace=namespace, body=lease)
                        lease_renewed_at = stamped_at
                        holder = pod_name
                    except ApiException:
                        holder = None
                        
        except ApiException as e:
            if e.status == 404:
                # Create lease
                write_now = datetime.now(timezone.utc)
                body = client.V1Lease(
                    api_version="coordination.k8s.io/v1",
                    kind="Lease",
                    metadata=client.V1ObjectMeta(name=lease_name, namespace=namespace),
                    spec=client.V1LeaseSpec(
                        holder_identity=pod_name,
                        lease_duration_seconds=LEASE_DURATION_SECONDS,
                        acquire_time=write_now,
                        renew_time=write_now,
                        lease_transitions=0
                    )
                )
                stamped_at = time.monotonic()
                try:
                    coordination_v1.create_namespaced_lease(namespace=namespace, body=body)
                    lease_renewed_at = stamped_at
                    holder = pod_name
                except ApiException:
                    holder = None
            else:
                print(f"[LeaderElect] Error reading lease: {e}", file=sys.stderr, flush=True)
                lease_error = True

        # A transient error reading or renewing the lease while leading has not
        # observed another holder, so keep Hermes running until the renew
        # deadline passes without a confirmed renewal.
        if (
            lease_error
            and process is not None
            and lease_renewed_at is not None
            and time.monotonic() < lease_renewed_at + LEASE_RENEW_DEADLINE_SECONDS
        ):
            holder = pod_name

        if holder == pod_name:
            if process is None:
                print(f"[{pod_name}] Acquired leadership! Starting Hermes...", flush=True)
                process = subprocess.Popen(HERMES_COMMAND)
            elif process.poll() is not None:
                print(f"[{pod_name}] Hermes process crashed with code {process.returncode}. Exiting to trigger pod restart...", flush=True)
                exit_on_crash(v1, label_state, process.returncode)
        else:
            if process is not None:
                print(f"[{pod_name}] Lost leadership! Stopping Hermes...", flush=True)
                stop_hermes_holding_lease(drain_coordination_v1)
                process = None

        # The label, every loop: set once this pod leads and its Hermes answers,
        # cleared otherwise, a refused patch retried next time round.
        running = process is not None and process.poll() is None
        label_state = reconcile_label(v1, desired_label(holder, running, running and hermes_answers()), label_state)

        # Jitter to avoid flapping: sleep for the interval plus up to the jitter
        time.sleep(LEASE_POLL_INTERVAL_SECONDS + random.uniform(0, LEASE_POLL_JITTER_SECONDS))

if __name__ == "__main__":
    main()
