import os, time, subprocess, sys, signal, random
from datetime import datetime, timezone
from kubernetes import client, config
from kubernetes.client.rest import ApiException

lease_name = os.environ.get("LEADER_ELECTION_LEASE_NAME")
namespace = os.environ.get("LEADER_ELECTION_NAMESPACE")
pod_name = os.environ.get("HOSTNAME")
process = None
is_shutting_down = False

# Tradeoff documented explicitly per review:
# This active/passive failover blackholes traffic during leader transition.
# The Service selector requires 'kubeagents.io/is-leader=true', which is only carried by the active leader.
# During failover, there is a window with zero ready endpoints until the new leader acquires the lease and labels itself.
# This introduces a single point of failure during failovers, rather than request-continuous HA.

def update_pod_label(v1, is_leader):
    try:
        body = {"metadata": {"labels": {"kubeagents.io/is-leader": "true" if is_leader else None}}}
        v1.patch_namespaced_pod(name=pod_name, namespace=namespace, body=body)
    except ApiException as e:
        print(f"[LeaderElect] Error patching pod label: {e}", file=sys.stderr, flush=True)

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
    global process, is_shutting_down
    
    if not lease_name or not namespace:
        os.execvp("hermes", ["hermes", "gateway", "run"])
        
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
                update_pod_label(v1, True)
                process = subprocess.Popen(["hermes", "gateway", "run"])
            elif process.poll() is not None:
                print(f"[{pod_name}] Hermes process crashed with code {process.returncode}. Exiting to trigger pod restart...", flush=True)
                sys.exit(process.returncode)
        else:
            if process is not None:
                print(f"[{pod_name}] Lost leadership! Stopping Hermes...", flush=True)
                update_pod_label(v1, False)
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    print(f"[{pod_name}] Hermes did not exit in time, killing...", flush=True)
                    process.kill()
                    process.wait()
                process = None
                
        # Jitter to avoid flapping: sleep for base + 0 to 2 seconds
        time.sleep(base_poll_interval + random.uniform(0, 2))

if __name__ == "__main__":
    main()
