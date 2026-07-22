import os, time, subprocess, sys, json, signal, re
from datetime import datetime, timezone

def run_kubectl(cmd, input_data=None):
    try:
        proc = subprocess.run(["kubectl"] + cmd, input=input_data, capture_output=True, text=True, check=True)
        return proc.stdout
    except subprocess.CalledProcessError as e:
        if not ("NotFound" in e.stderr and "get" in cmd):
            print(f"[LeaderElect] kubectl error: {e.stderr.strip()}", file=sys.stderr, flush=True)
        return None

def to_k8s_time(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

# Pre-compile the regex once. 
# It matches a dot, exactly 6 digits, and then any trailing digits,
# allowing us to quickly truncate nanoseconds to microseconds.
_NANO_RE = re.compile(r"(\.\d{6})\d+")

def from_k8s_time(time_str: str) -> datetime:
    try:
        # 1. Truncate fractional seconds > 6 digits
        time_str = _NANO_RE.sub(r"\1", time_str)
        
        # 2. Replace Z with +00:00 (fromisoformat strictly prefers the colon)
        time_str = time_str.replace("Z", "+00:00")
        
        # 3. Parse using the C-optimized fromisoformat
        return datetime.fromisoformat(time_str)
    except Exception:
        return datetime.now(timezone.utc)

lease_name = os.environ.get("LEADER_ELECTION_LEASE_NAME")
namespace = os.environ.get("LEADER_ELECTION_NAMESPACE")
pod_name = os.environ.get("HOSTNAME")
process = None
is_shutting_down = False

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
    
    stdout = run_kubectl(["get", "lease", lease_name, "-n", namespace, "-o", "json"])
    if stdout:
        try:
            lease = json.loads(stdout)
            spec = lease.get("spec", {})
            if spec.get("holderIdentity") == pod_name:
                print(f"[{pod_name}] Releasing lease...", flush=True)
                spec["holderIdentity"] = None
                lease["spec"] = spec
                run_kubectl(["replace", "-f", "-"], input_data=json.dumps(lease))
                run_kubectl(["label", "pod", pod_name, "-n", namespace, "kubeagents.io/is-leader-"])
        except Exception as e:
            print(f"[LeaderElect] Error releasing lease: {e}", file=sys.stderr, flush=True)
            
    sys.exit(0)

def main():
    global process, is_shutting_down
    
    if not lease_name or not namespace:
        os.execvp("hermes", ["hermes", "gateway", "run"])
        
    signal.signal(signal.SIGTERM, release_lease_and_exit)
    signal.signal(signal.SIGINT, release_lease_and_exit)
    
    while not is_shutting_down:
        stdout = run_kubectl(["get", "lease", lease_name, "-n", namespace, "-o", "json"])
        now = datetime.now(timezone.utc)
        now_str = to_k8s_time(now)
        
        if not stdout:
            body = {
                "apiVersion": "coordination.k8s.io/v1",
                "kind": "Lease",
                "metadata": {"name": lease_name, "namespace": namespace},
                "spec": {
                    "holderIdentity": pod_name,
                    "leaseDurationSeconds": 15,
                    "acquireTime": now_str,
                    "renewTime": now_str,
                    "leaseTransitions": 0
                }
            }
            holder = pod_name if run_kubectl(["create", "-f", "-"], input_data=json.dumps(body)) else None
        else:
            try:
                lease = json.loads(stdout)
                spec = lease.get("spec", {})
                holder = spec.get("holderIdentity")
                
                if holder == pod_name:
                    spec["renewTime"] = now_str
                    lease["spec"] = spec
                    if not run_kubectl(["replace", "-f", "-"], input_data=json.dumps(lease)):
                        holder = None
                else:
                    renew_time_str = spec.get("renewTime")
                    duration = spec.get("leaseDurationSeconds", 15)
                    
                    is_expired = False
                    if renew_time_str:
                        renew_time = from_k8s_time(renew_time_str)
                        if (now - renew_time).total_seconds() > duration:
                            is_expired = True
                            
                    if holder is None or is_expired:
                        spec["holderIdentity"] = pod_name
                        spec["renewTime"] = now_str
                        spec["acquireTime"] = now_str
                        spec["leaseTransitions"] = spec.get("leaseTransitions", 0) + 1
                        lease["spec"] = spec
                        if run_kubectl(["replace", "-f", "-"], input_data=json.dumps(lease)):
                            holder = pod_name
            except Exception as e:
                print(f"[LeaderElect] Error parsing lease: {e}", file=sys.stderr, flush=True)
                holder = None

        if holder == pod_name:
            if process is None:
                print(f"[{pod_name}] Acquired leadership! Starting Hermes...", flush=True)
                run_kubectl(["label", "pod", pod_name, "-n", namespace, "kubeagents.io/is-leader=true", "--overwrite"])
                process = subprocess.Popen(["hermes", "gateway", "run"])
            elif process.poll() is not None:
                print(f"[{pod_name}] Hermes process crashed with code {process.returncode}. Exiting to trigger pod restart...", flush=True)
                sys.exit(process.returncode)
        else:
            if process is not None:
                print(f"[{pod_name}] Lost leadership! Stopping Hermes...", flush=True)
                run_kubectl(["label", "pod", pod_name, "-n", namespace, "kubeagents.io/is-leader-"])
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    print(f"[{pod_name}] Hermes did not exit in time, killing...", flush=True)
                    process.kill()
                    process.wait()
                process = None
                
        time.sleep(5)

if __name__ == "__main__":
    main()
