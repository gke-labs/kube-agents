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

# Tradeoff documented explicitly per review:
# This active/passive failover blackholes traffic during leader transition.
# The Service selector requires 'kubeagents.io/is-leader=true', which is only carried by the active leader.
# During failover, there is a window with zero ready endpoints until the new leader acquires the lease and labels itself.
# This introduces a single point of failure during failovers, rather than request-continuous HA.

def update_pod_label(v1, is_leader):
    """Stamp or clear the leader label on this pod; True when the API accepted it."""
    try:
        body = {"metadata": {"labels": {"kubeagents.io/is-leader": "true" if is_leader else None}}}
        v1.patch_namespaced_pod(name=pod_name, namespace=namespace, body=body)
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

def release_lease_and_exit(signum, frame):
    global is_shutting_down, process
    if is_shutting_down:
        return
    is_shutting_down = True
    print(f"[{pod_name}] Received signal {signum}, shutting down...", flush=True)
    
    if process is not None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(f"[{pod_name}] Hermes did not exit in time, killing...", flush=True)
            process.kill()
            process.wait()
    
    try:
        config.load_incluster_config()
        coordination_v1 = client.CoordinationV1Api()
        v1 = client.CoreV1Api()
        
        lease = coordination_v1.read_namespaced_lease(name=lease_name, namespace=namespace)
        if lease.spec.holder_identity == pod_name:
            print(f"[{pod_name}] Releasing lease...", flush=True)
            lease.spec.holder_identity = None
            coordination_v1.replace_namespaced_lease(name=lease_name, namespace=namespace, body=lease)
            update_pod_label(v1, False)
    except Exception as e:
        print(f"[LeaderElect] Error releasing lease: {e}", file=sys.stderr, flush=True)
            
    sys.exit(0)

def main():
    global process, is_shutting_down, label_state

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
    
    lease_duration_seconds = 15
    base_poll_interval = 5
    
    while not is_shutting_down:
        now = datetime.now(timezone.utc)
        holder = None
        
        try:
            lease = coordination_v1.read_namespaced_lease(name=lease_name, namespace=namespace)
            holder = lease.spec.holder_identity
            
            if holder == pod_name:
                lease.spec.renew_time = now
                try:
                    coordination_v1.replace_namespaced_lease(name=lease_name, namespace=namespace, body=lease)
                except ApiException:
                    holder = None
            else:
                renew_time = lease.spec.renew_time
                duration = lease.spec.lease_duration_seconds or lease_duration_seconds
                
                is_expired = False
                if renew_time:
                    # Kubernetes client automatically parses ISO times into datetime objects
                    if (now - renew_time).total_seconds() > duration:
                        is_expired = True
                else:
                    # Fail-safe: if renew_time is somehow unparseable/missing, treat as expired
                    is_expired = True
                        
                if holder is None or is_expired:
                    lease.spec.holder_identity = pod_name
                    lease.spec.renew_time = now
                    lease.spec.acquire_time = now
                    lease.spec.lease_transitions = (lease.spec.lease_transitions or 0) + 1
                    try:
                        coordination_v1.replace_namespaced_lease(name=lease_name, namespace=namespace, body=lease)
                        holder = pod_name
                    except ApiException:
                        holder = None
                        
        except ApiException as e:
            if e.status == 404:
                # Create lease
                body = client.V1Lease(
                    api_version="coordination.k8s.io/v1",
                    kind="Lease",
                    metadata=client.V1ObjectMeta(name=lease_name, namespace=namespace),
                    spec=client.V1LeaseSpec(
                        holder_identity=pod_name,
                        lease_duration_seconds=lease_duration_seconds,
                        acquire_time=now,
                        renew_time=now,
                        lease_transitions=0
                    )
                )
                try:
                    coordination_v1.create_namespaced_lease(namespace=namespace, body=body)
                    holder = pod_name
                except ApiException:
                    holder = None
            else:
                print(f"[LeaderElect] Error reading lease: {e}", file=sys.stderr, flush=True)

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
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    print(f"[{pod_name}] Hermes did not exit in time, killing...", flush=True)
                    process.kill()
                    process.wait()
                process = None

        # The label, every loop: set once this pod leads and its Hermes answers,
        # cleared otherwise, a refused patch retried next time round.
        running = process is not None and process.poll() is None
        label_state = reconcile_label(v1, desired_label(holder, running, running and hermes_answers()), label_state)

        # Jitter to avoid flapping: sleep for base + 0 to 2 seconds
        time.sleep(base_poll_interval + random.uniform(0, 2))

if __name__ == "__main__":
    main()
