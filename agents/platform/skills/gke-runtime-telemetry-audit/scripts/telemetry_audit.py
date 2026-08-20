#!/usr/bin/env python3
"""
telemetry_audit.py — GKE Runtime Telemetry & Container OS Health Audit Runner.
Sweeps GKE clusters for CPU throttling, conntrack table configuration, missing preStop hooks,
unbounded ephemeral storage, and ulimit exhaustion risk.
"""

import argparse
import json
import os
import re
import subprocess
import sys

# S1: Canonical system namespace regex
SYS_RE = re.compile(
    r"^(kube-system|kube-public|kube-node-lease|gmp-system|gmp-public|"
    r"gke-gmp-system|cnrm-system|configconnector-operator-system|"
    r"krmapihosting-system|istio-system|asm-system|anthos-identity-service|"
    r"config-management-.*|gatekeeper-system|composer-system|gke-.*|gke-managed-.*)$"
)

# Finding ID constants
ID_EMPTY_SEGMENT = "_"
ID_SEGMENTS = 4
MAX_FINDING_ID = 100

def run_cmd(cmd: list[str], env: dict | None = None) -> tuple[int, str, str]:
    """Runs a shell command and returns (rc, stdout, stderr)."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
        return res.returncode, res.stdout, res.stderr
    except Exception as e:
        return -1, "", str(e)

def run_gcloud_json(cmd: list[str]) -> list[dict] | dict | None:
    """Runs a gcloud command and parses JSON output safely."""
    rc, stdout, stderr = run_cmd(cmd)
    if rc != 0:
        sys.stderr.write(f"gcloud command failed ({rc}): {' '.join(cmd)}\n{stderr}\n")
        return None
    if not stdout.strip():
        return []
    try:
        return json.loads(stdout)
    except Exception as e:
        sys.stderr.write(f"Error parsing gcloud output from {' '.join(cmd)}: {e}\n")
        return None

def get_target_projects(cli_project: str | None = None) -> list[str]:
    """Resolves all target GCP projects to audit."""
    if cli_project:
        return [cli_project]

    projects = set()
    monitored = os.environ.get("MONITORED_PROJECT_IDS", "")
    if monitored:
        for p in monitored.split(","):
            p = p.strip()
            if p:
                projects.add(p)

    for env_var in ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID"):
        val = os.environ.get(env_var, "").strip()
        if val:
            projects.add(val)

    if not projects:
        rc, stdout, _ = run_cmd(["gcloud", "config", "get-value", "project"])
        if rc == 0 and stdout.strip():
            projects.add(stdout.strip())

    return sorted(list(projects))

def _id_segment(value: str) -> str:
    """Sanitizes an interpolated value for use in a finding ID segment."""
    out = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return out or ID_EMPTY_SEGMENT

def derive_finding_id(check: str, cluster: str, namespace: str, obj: str) -> str:
    """Computes a unique stable finding ID matching audit_report.py's implementation."""
    ns = namespace if namespace.strip() else ID_EMPTY_SEGMENT
    fid = ".".join((
        _id_segment(check),
        _id_segment(cluster),
        _id_segment(ns),
        _id_segment(obj)
    ))
    
    if len(fid) <= MAX_FINDING_ID:
        return fid
        
    parts = fid.split(".")
    while len(".".join(parts)) > MAX_FINDING_ID:
        longest = max(
            range(1, ID_SEGMENTS), key=lambda i: (len(parts[i]), i), default=None
        )
        if longest is None or len(parts[longest]) <= 1:
            break
        parts[longest] = parts[longest][:-1].rstrip("-") or ID_EMPTY_SEGMENT
    return ".".join(parts)[:MAX_FINDING_ID].rstrip(".-")

def parse_cpu_to_milli(cpu_str: str) -> int | None:
    """Converts Kubernetes CPU resource string to numeric millicores."""
    if not cpu_str:
        return None
    cpu_str = str(cpu_str).strip()
    if cpu_str.endswith("m"):
        try:
            return int(cpu_str[:-1])
        except ValueError:
            return None
    try:
        return int(float(cpu_str) * 1000)
    except ValueError:
        return None

def get_kubernetes_server_version(env: dict) -> tuple[int, int] | None:
    """Retrieves major and minor Kubernetes server version."""
    rc, stdout, _ = run_cmd(["kubectl", "version", "-o", "json"], env=env)
    if rc != 0 or not stdout.strip():
        return None
    try:
        data = json.loads(stdout)
        server = data.get("serverVersion", {})
        minor = int(server.get("minor", "0").strip("+"))
        major = int(server.get("major", "0").strip("+"))
        return major, minor
    except Exception:
        return None

def is_cpu_burst_supported(env: dict) -> bool:
    """Returns True if cluster version natively supports CPU burst."""
    ver = get_kubernetes_server_version(env)
    if not ver:
        return False
    major, minor = ver
    if major > 1 or minor >= 29:
        return True
    return False

def find_manifest_path(workspace_path: str, resource_kind: str, resource_name: str, namespace: str) -> str:
    """Discovers the GitOps repository file declaring the target resource."""
    if not workspace_path or not os.path.isdir(workspace_path):
        return ""
    
    for root, _, files in os.walk(workspace_path):
        for file in files:
            if file.endswith((".yaml", ".yml")):
                full_path = os.path.join(root, file)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    
                    docs = content.split("\n---")
                    for doc in docs:
                        lines = [line.strip() for line in doc.split("\n")]
                        
                        has_kind = False
                        has_name = False
                        doc_ns = ""
                        
                        for line in lines:
                            if line.startswith("kind:"):
                                k_val = line.split(":", 1)[1].strip().strip("\"'")
                                if k_val.lower() == resource_kind.lower():
                                    has_kind = True
                            elif line.startswith("name:"):
                                n_val = line.split(":", 1)[1].strip().strip("\"'")
                                if n_val == resource_name:
                                    has_name = True
                            elif line.startswith("namespace:"):
                                doc_ns = line.split(":", 1)[1].strip().strip("\"'")
                        
                        if has_kind and has_name:
                            if namespace and doc_ns and doc_ns != namespace:
                                continue
                            return os.path.relpath(full_path, workspace_path)
                except Exception:
                    continue
    return ""

def check_cfs_quota(env: dict, cluster_name: str, workspace: str, findings: list, checks_run: list, limitations: list):
    """CFS CPU throttling check (2.1)."""
    cmd = ["kubectl", "get", "deployments,statefulsets,daemonsets", "-A", "-o", "json"]
    rc, stdout, stderr = run_cmd(cmd, env=env)
    if rc != 0:
        limitations.append(f"cfs-quota-throttling: kubectl failed: {stderr.strip()}")
        return
    
    checks_run.append({
        "check": "cfs-quota-throttling",
        "command": f"kubectl get deployments,statefulsets,daemonsets -A -o json --context={cluster_name}"
    })
    
    if not stdout.strip():
        return
        
    try:
        data = json.loads(stdout)
        items = data.get("items", [])
        
        # Determine CPU burst support
        burst_supported = is_cpu_burst_supported(env)
        if burst_supported:
            return

        for item in items:
            meta = item.get("metadata", {})
            ns = meta.get("namespace", "")
            name = meta.get("name", "")
            kind = item.get("kind", "")
            
            # S1: System namespaces
            if SYS_RE.match(ns):
                continue
            
            # S2: Managed addons
            annotations = meta.get("annotations") or {}
            if "addonmanager.kubernetes.io/mode" in annotations:
                continue
                
            # S4: Scaled to zero
            spec = item.get("spec", {})
            replicas = spec.get("replicas", 1)
            if replicas == 0:
                continue
                
            template = spec.get("template", {})
            pod_spec = template.get("spec", {})
            containers = pod_spec.get("containers", [])
            
            flagged_containers = []
            for c in containers:
                c_name = c.get("name", "")
                resources = c.get("resources", {})
                limits = resources.get("limits", {})
                requests = resources.get("requests", {})
                
                cpu_limit = limits.get("cpu")
                cpu_request = requests.get("cpu")
                
                if cpu_limit and cpu_request == cpu_limit:
                    limit_m = parse_cpu_to_milli(cpu_limit)
                    if limit_m is not None and limit_m < 500:
                        flagged_containers.append(c_name)
                        
            if flagged_containers:
                confirm_cmd = f"kubectl get {kind.lower()} {name} -n {ns} -o json --context={cluster_name}"
                c_rc, c_out, _ = run_cmd(["kubectl", "get", kind.lower(), name, "-n", ns, "-o", "json"], env=env)
                
                excerpt = ""
                if c_rc == 0:
                    try:
                        c_data = json.loads(c_out)
                        c_containers = c_data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                        excerpt_parts = []
                        for cc in c_containers:
                            if cc.get("name") in flagged_containers:
                                excerpt_parts.append(f"container \"{cc.get('name')}\": limits={cc.get('resources', {}).get('limits')}, requests={cc.get('resources', {}).get('requests')}")
                        excerpt = "\n".join(excerpt_parts)
                    except Exception:
                        excerpt = f"containers: {flagged_containers}"
                else:
                    excerpt = f"containers: {flagged_containers}"
                    
                path = find_manifest_path(workspace, kind, name, ns)
                findings.append({
                    "check": "cfs-quota-throttling",
                    "severity": "major",
                    "title": f"{kind} {name} specifies restrictive CPU limits without CPU burst support",
                    "cluster": cluster_name,
                    "namespace": ns,
                    "object": f"{kind}/{name}",
                    "impact": "Containers suffer tail-latency spikes due to severe cgroup CFS CPU throttling.",
                    "evidence": {
                        "command": confirm_cmd,
                        "excerpt": excerpt
                    },
                    "recommendation": {
                        "action": f"Remove CPU limits or enable CPU burst for container(s) {flagged_containers} in {kind.lower()} {name}.",
                        "rationale": "CPU burst allows containers to consume idle CPU cycles beyond limits, mitigating throttling.",
                        "risk": "May increase node CPU utilization if multiple workloads burst concurrently."
                    },
                    "remediation": {
                        "kind": "manifest" if path else "manual",
                        "path": path
                    }
                })
    except Exception as e:
        limitations.append(f"cfs-quota-throttling parsing error: {e}")

def check_conntrack(env: dict, cluster_name: str, workspace: str, findings: list, checks_run: list, limitations: list, checks_not_applicable: list):
    """Conntrack saturation check (2.2)."""
    cmd = ["kubectl", "get", "daemonsets,nodes", "-n", "kube-system", "-o", "json"]
    rc, stdout, stderr = run_cmd(cmd, env=env)
    if rc != 0:
        limitations.append(f"conntrack-saturation: kubectl failed: {stderr.strip()}")
        return
        
    checks_run.append({
        "check": "conntrack-saturation",
        "command": f"kubectl get daemonsets,nodes -n kube-system -o json --context={cluster_name}"
    })
    
    if not stdout.strip():
        return
        
    try:
        data = json.loads(stdout)
        items = data.get("items", [])
        
        nodes = [item for item in items if item.get("kind") == "Node"]
        daemonsets = [item for item in items if item.get("kind") == "DaemonSet"]
        
        is_autopilot = False
        for n in nodes:
            labels = n.get("metadata", {}).get("labels", {})
            if "container.googleapis.com/wle-managed" in labels or "cloud.google.com/gke-autopilot" in labels:
                is_autopilot = True
                break
                
        if is_autopilot:
            checks_not_applicable.append({
                "check": "conntrack-saturation",
                "reason": "Autopilot manages node sysctls directly"
            })
            return
            
        has_conntrack_tuning = False
        suboptimal_val = None
        tuning_ds_name = ""
        
        for ds in daemonsets:
            ds_name = ds.get("metadata", {}).get("name", "")
            spec = ds.get("spec", {})
            template = spec.get("template", {})
            pod_spec = template.get("spec", {})
            
            for c in pod_spec.get("containers", []) + pod_spec.get("initContainers", []):
                args = c.get("args", [])
                cmd_list = c.get("command", [])
                all_tokens = [str(x) for x in cmd_list + args]
                for token in all_tokens:
                    if "nf_conntrack_max" in token:
                        has_conntrack_tuning = True
                        tuning_ds_name = ds_name
                        match = re.search(r"nf_conntrack_max[= ](\d+)", token)
                        if match:
                            val = int(match.group(1))
                            if val < 131072:
                                suboptimal_val = val
                                
        if not has_conntrack_tuning or suboptimal_val is not None:
            path = ""
            if tuning_ds_name:
                path = find_manifest_path(workspace, "DaemonSet", tuning_ds_name, "kube-system")
                
            title = "Cluster nodes lack optimal net.netfilter.nf_conntrack_max sysctl limits"
            impact = "Nodes risk silent packet drops and network connection failures under high connection concurrency."
            
            if suboptimal_val is not None:
                title = f"System tuning DaemonSet {tuning_ds_name} configures sub-optimal net.netfilter.nf_conntrack_max ({suboptimal_val})"
                excerpt = f"DaemonSet {tuning_ds_name} configures nf_conntrack_max={suboptimal_val}"
            else:
                excerpt = "No system-tuning DaemonSet in kube-system configures net.netfilter.nf_conntrack_max"
                
            confirm_cmd = f"kubectl get daemonsets -n kube-system -o json --context={cluster_name}"
            
            findings.append({
                "check": "conntrack-saturation",
                "severity": "major",
                "title": title,
                "cluster": cluster_name,
                "namespace": "kube-system",
                "object": f"DaemonSet/{tuning_ds_name}" if tuning_ds_name else "Node/system-tuning",
                "impact": impact,
                "evidence": {
                    "command": confirm_cmd,
                    "excerpt": excerpt
                },
                "recommendation": {
                    "action": "Configure net.netfilter.nf_conntrack_max to at least 131072 in a node tuning DaemonSet.",
                    "rationale": "High connection density requires larger conntrack tables to prevent package loss.",
                    "risk": "Allocates a small amount of additional kernel memory for tracked connections."
                },
                "remediation": {
                    "kind": "manifest" if path else "manual",
                    "path": path
                }
            })
    except Exception as e:
        limitations.append(f"conntrack-saturation parsing error: {e}")

def check_ingress_drain(env: dict, cluster_name: str, workspace: str, findings: list, checks_run: list, limitations: list):
    """Missing preStop graceful shutdown check (2.3)."""
    cmd_svc = ["kubectl", "get", "svc,deployments", "-A", "-o", "json"]
    rc_svc, out_svc, stderr_svc = run_cmd(cmd_svc, env=env)
    if rc_svc != 0:
        limitations.append(f"ingress-502-drain: kubectl svc,deployments failed: {stderr_svc.strip()}")
        return
        
    cmd_ing = ["kubectl", "get", "ingress,httproute,gateway", "-A", "-o", "json"]
    rc_ing, out_ing, _ = run_cmd(cmd_ing, env=env)
    
    checks_run.append({
        "check": "ingress-502-drain",
        "command": f"kubectl get svc,deployments -A -o json --context={cluster_name}"
    })
    
    service_selectors = []
    deps = []
    if out_svc.strip():
        try:
            items = json.loads(out_svc).get("items", [])
            svcs = [item for item in items if item.get("kind") == "Service"]
            deps = [item for item in items if item.get("kind") == "Deployment"]
            
            for s in svcs:
                ns = s.get("metadata", {}).get("namespace", "")
                if SYS_RE.match(ns):
                    continue
                selector = s.get("spec", {}).get("selector", {})
                if selector:
                    service_selectors.append((ns, s.get("metadata", {}).get("name", ""), selector))
        except Exception as e:
            limitations.append(f"ingress-502-drain Service parsing error: {e}")
            return
            
    exposed_services = set()
    if rc_ing == 0 and out_ing.strip():
        try:
            items = json.loads(out_ing).get("items", [])
            for item in items:
                kind = item.get("kind", "")
                ns = item.get("metadata", {}).get("namespace", "")
                spec = item.get("spec", {})
                if kind == "Ingress":
                    rules = spec.get("rules", [])
                    for rule in rules:
                        http = rule.get("http", {})
                        paths = http.get("paths", [])
                        for path in paths:
                            svc_name = path.get("backend", {}).get("service", {}).get("name")
                            if svc_name:
                                exposed_services.add((ns, svc_name))
                    def_backend = spec.get("defaultBackend", {})
                    svc_name = def_backend.get("service", {}).get("name")
                    if svc_name:
                        exposed_services.add((ns, svc_name))
                elif kind == "HTTPRoute":
                    rules = spec.get("rules", [])
                    for rule in rules:
                        backend_refs = rule.get("backendRefs", [])
                        for ref in backend_refs:
                            ref_kind = ref.get("kind", "Service")
                            if ref_kind == "Service":
                                exposed_services.add((ns, ref.get("name")))
        except Exception as e:
            limitations.append(f"ingress-502-drain ingress/httproute parsing error: {e}")
            
    for d in deps:
        meta = d.get("metadata", {})
        ns = meta.get("namespace", "")
        name = meta.get("name", "")
        if SYS_RE.match(ns):
            continue
            
        if "addonmanager.kubernetes.io/mode" in (meta.get("annotations") or {}):
            continue
            
        spec = d.get("spec", {})
        replicas = spec.get("replicas", 1)
        if replicas == 0:
            continue
            
        template = spec.get("template", {})
        pod_labels = template.get("metadata", {}).get("labels", {})
        
        is_service_exposed = False
        matching_svcs = []
        for s_ns, s_name, s_sel in service_selectors:
            if s_ns == ns and all(pod_labels.get(k) == v for k, v in s_sel.items()):
                is_service_exposed = True
                matching_svcs.append(s_name)
                
        if not is_service_exposed:
            continue
            
        is_ingress_exposed = any((ns, s_name) in exposed_services for s_name in matching_svcs)
        if not is_ingress_exposed:
            continue
            
        pod_spec = template.get("spec", {})
        grace_period = pod_spec.get("terminationGracePeriodSeconds", 30)
        if grace_period > 30:
            continue
            
        containers = pod_spec.get("containers", [])
        missing_containers = []
        for c in containers:
            c_name = c.get("name", "")
            lifecycle = c.get("lifecycle") or {}
            pre_stop = lifecycle.get("preStop") if isinstance(lifecycle, dict) else None
            if not pre_stop or not isinstance(pre_stop, dict) or "exec" not in pre_stop:
                missing_containers.append(c_name)
                
        if missing_containers:
            c_list_str = ", ".join(missing_containers)
            confirm_cmd = f"kubectl get deployment {name} -n {ns} -o json --context={cluster_name}"
            
            c_rc, c_out, _ = run_cmd(["kubectl", "get", "deployment", name, "-n", ns, "-o", "json"], env=env)
            excerpt = ""
            if c_rc == 0:
                try:
                    c_data = json.loads(c_out)
                    c_spec = c_data.get("spec", {}).get("template", {}).get("spec", {})
                    excerpt_containers = []
                    for cc in c_spec.get("containers", []):
                        if cc.get("name") in missing_containers:
                            excerpt_containers.append(f"container \"{cc.get('name')}\": lifecycle={cc.get('lifecycle')}")
                    excerpt = "\n".join(excerpt_containers)
                except Exception:
                    excerpt = f"containers: [{c_list_str}], lifecycle.preStop: null"
            else:
                excerpt = f"containers: [{c_list_str}], lifecycle.preStop: null"
                
            path = find_manifest_path(workspace, "Deployment", name, ns)
            
            findings.append({
                "check": "ingress-502-drain",
                "severity": "major",
                "title": f"Service-exposed deployment {name} lacks preStop hook on container(s): {c_list_str}",
                "cluster": cluster_name,
                "namespace": ns,
                "object": f"Deployment/{name}",
                "impact": "Workload receives in-flight HTTP traffic during rolling pod termination, causing HTTP 502 Bad Gateway drops.",
                "evidence": {
                    "command": confirm_cmd,
                    "excerpt": excerpt
                },
                "recommendation": {
                    "action": f"Add lifecycle.preStop sleep 15 hook to container(s) [{c_list_str}] in deployment {name}.",
                    "rationale": "Gives ingress controllers and network proxies time to deregister the pod endpoint before process termination.",
                    "risk": "Increases graceful shutdown time by 15 seconds."
                },
                "remediation": {
                    "kind": "manifest" if path else "manual",
                    "path": path
                }
            })

def check_ephemeral_storage(env: dict, cluster_name: str, workspace: str, findings: list, checks_run: list, limitations: list):
    """Unbounded ephemeral storage check (2.4)."""
    cmd = ["kubectl", "get", "deployments,statefulsets,daemonsets", "-A", "-o", "json"]
    rc, stdout, stderr = run_cmd(cmd, env=env)
    if rc != 0:
        limitations.append(f"ephemeral-growth-rate: kubectl failed: {stderr.strip()}")
        return
        
    checks_run.append({
        "check": "ephemeral-growth-rate",
        "command": f"kubectl get deployments,statefulsets,daemonsets -A -o json --context={cluster_name}"
    })
    
    if not stdout.strip():
        return
        
    try:
        data = json.loads(stdout)
        items = data.get("items", [])
        for item in items:
            meta = item.get("metadata", {})
            ns = meta.get("namespace", "")
            name = meta.get("name", "")
            kind = item.get("kind", "")
            
            if SYS_RE.match(ns):
                continue
                
            if "addonmanager.kubernetes.io/mode" in (meta.get("annotations") or {}):
                continue
                
            spec = item.get("spec", {})
            replicas = spec.get("replicas", 1)
            if replicas == 0:
                continue
                
            template = spec.get("template", {})
            pod_spec = template.get("spec", {})
            
            volumes = pod_spec.get("volumes", [])
            scratch_volume_names = set()
            for v in volumes:
                v_name = v.get("name", "")
                if "emptyDir" in v or "persistentVolumeClaim" in v:
                    scratch_volume_names.add(v_name)
                    
            containers = pod_spec.get("containers", [])
            flagged_containers = []
            for c in containers:
                c_name = c.get("name", "")
                resources = c.get("resources", {})
                limits = resources.get("limits", {})
                
                ephemeral_limit = limits.get("ephemeral-storage")
                if not ephemeral_limit:
                    volume_mounts = c.get("volumeMounts", [])
                    has_scratch_mount = any(m.get("name") in scratch_volume_names for m in volume_mounts)
                    if not has_scratch_mount:
                        flagged_containers.append(c_name)
                        
            if flagged_containers:
                confirm_cmd = f"kubectl get {kind.lower()} {name} -n {ns} -o json --context={cluster_name}"
                c_rc, c_out, _ = run_cmd(["kubectl", "get", kind.lower(), name, "-n", ns, "-o", "json"], env=env)
                
                excerpt = ""
                if c_rc == 0:
                    try:
                        c_data = json.loads(c_out)
                        c_containers = c_data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                        excerpt_parts = []
                        for cc in c_containers:
                            if cc.get("name") in flagged_containers:
                                excerpt_parts.append(f"container \"{cc.get('name')}\": resources={cc.get('resources')}")
                        excerpt = "\n".join(excerpt_parts)
                    except Exception:
                        excerpt = f"containers: {flagged_containers}"
                else:
                    excerpt = f"containers: {flagged_containers}"
                    
                path = find_manifest_path(workspace, kind, name, ns)
                
                findings.append({
                    "check": "ephemeral-growth-rate",
                    "severity": "minor",
                    "title": f"{kind} {name} has container(s) with unbounded ephemeral-storage growth",
                    "cluster": cluster_name,
                    "namespace": ns,
                    "object": f"{kind}/{name}",
                    "impact": "Node local ephemeral storage exhaustion risk if containers write large amounts of logs or temporary files.",
                    "evidence": {
                        "command": confirm_cmd,
                        "excerpt": excerpt
                    },
                    "recommendation": {
                        "action": f"Configure resources.limits.ephemeral-storage for container(s) {flagged_containers} in {kind.lower()} {name}.",
                        "rationale": "Explicit limits prevent a single container from starving other workloads of node storage.",
                        "risk": "Workload will be evicted if it exceeds the specified storage limit."
                    },
                    "remediation": {
                        "kind": "manifest" if path else "manual",
                        "path": path
                    }
                })
    except Exception as e:
        limitations.append(f"ephemeral-growth-rate parsing error: {e}")

def check_ulimit_exhaustion(env: dict, cluster_name: str, workspace: str, findings: list, checks_run: list, limitations: list):
    """File descriptor limit exhaustion risk check (2.5)."""
    cmd = ["kubectl", "get", "deployments,statefulsets", "-A", "-o", "json"]
    rc, stdout, stderr = run_cmd(cmd, env=env)
    if rc != 0:
        limitations.append(f"ulimit-exhaustion: kubectl failed: {stderr.strip()}")
        return
        
    checks_run.append({
        "check": "ulimit-exhaustion",
        "command": f"kubectl get deployments,statefulsets -A -o json --context={cluster_name}"
    })
    
    if not stdout.strip():
        return
        
    db_proxy_pattern = re.compile(
        r"(postgres|mysql|mariadb|redis|mongo|cassandra|nginx|haproxy|envoy|traefik|apache|httpd|memcached)",
        re.IGNORECASE
    )
    
    try:
        data = json.loads(stdout)
        items = data.get("items", [])
        for item in items:
            meta = item.get("metadata", {})
            ns = meta.get("namespace", "")
            name = meta.get("name", "")
            kind = item.get("kind", "")
            
            if SYS_RE.match(ns):
                continue
                
            if "addonmanager.kubernetes.io/mode" in (meta.get("annotations") or {}):
                continue
                
            spec = item.get("spec", {})
            replicas = spec.get("replicas", 1)
            if replicas == 0:
                continue
                
            template = spec.get("template", {})
            pod_spec = template.get("spec", {})
            
            containers = pod_spec.get("containers", [])
            init_containers = pod_spec.get("initContainers", [])
            
            is_target_workload = False
            target_containers = []
            for c in containers:
                c_name = c.get("name", "")
                c_image = c.get("image", "")
                if db_proxy_pattern.search(c_name) or db_proxy_pattern.search(c_image):
                    is_target_workload = True
                    target_containers.append(c_name)
                    
            if not is_target_workload:
                continue
                
            has_limit_tuning = False
            pod_sc = pod_spec.get("securityContext", {})
            if "sysctls" in pod_sc:
                has_limit_tuning = True
                
            for ic in init_containers:
                args = ic.get("args", [])
                cmd_list = ic.get("command", [])
                all_tokens = [str(x) for x in cmd_list + args]
                for token in all_tokens:
                    if "ulimit" in token or "sysctl" in token or "net.core.somaxconn" in token:
                        has_limit_tuning = True
                        
            if not has_limit_tuning:
                confirm_cmd = f"kubectl get {kind.lower()} {name} -n {ns} -o json --context={cluster_name}"
                c_rc, c_out, _ = run_cmd(["kubectl", "get", kind.lower(), name, "-n", ns, "-o", "json"], env=env)
                
                excerpt = ""
                if c_rc == 0:
                    try:
                        c_data = json.loads(c_out)
                        c_spec = c_data.get("spec", {}).get("template", {}).get("spec", {})
                        excerpt = f"containers: {target_containers}, initContainers: {[ic.get('name') for ic in c_spec.get('initContainers', [])]}"
                    except Exception:
                        excerpt = f"containers: {target_containers}"
                else:
                    excerpt = f"containers: {target_containers}"
                    
                path = find_manifest_path(workspace, kind, name, ns)
                
                findings.append({
                    "check": "ulimit-exhaustion",
                    "severity": "minor",
                    "title": f"High-concurrency workload {name} runs with default low file descriptor limits",
                    "cluster": cluster_name,
                    "namespace": ns,
                    "object": f"{kind}/{name}",
                    "impact": "Workload risks socket / file descriptor limit exhaustion under high traffic loads.",
                    "evidence": {
                        "command": confirm_cmd,
                        "excerpt": excerpt
                    },
                    "recommendation": {
                        "action": f"Add an initContainer to configure ulimits or set securityContext sysctls for {kind.lower()} {name}.",
                        "rationale": "High-concurrency proxies and databases require higher file descriptor limits than the Linux kernel default.",
                        "risk": "Requires container to run with elevated privileges (SYS_RESOURCE capability) during initialization to set ulimits."
                    },
                    "remediation": {
                        "kind": "manifest" if path else "manual",
                        "path": path
                    }
                })
    except Exception as e:
        limitations.append(f"ulimit-exhaustion parsing error: {e}")

def inspect_cluster_telemetry(project_id: str, cluster_name: str, location: str, workspace: str, skipped_clusters: list) -> dict | None:
    """Inspects a GKE cluster running all 5 telemetry checks under an isolated kubeconfig environment."""
    import tempfile
    kc_home = os.environ.get('HERMES_HOME')
    if kc_home:
        kc_dir = os.path.expanduser(f"{kc_home}/.kubeconfigs")
    else:
        kc_dir = os.path.join(tempfile.gettempdir(), ".kubeconfigs")
    os.makedirs(kc_dir, exist_ok=True)
    kc_path = os.path.join(kc_dir, f"kubeconfig_{project_id}_{cluster_name}_{location}.yaml")

    env = os.environ.copy()
    env["KUBECONFIG"] = kc_path

    rc, _, stderr = run_cmd([
        "gcloud", "container", "clusters", "get-credentials", cluster_name,
        f"--location={location}", f"--project={project_id}"
    ], env=env)

    if rc != 0:
        sys.stderr.write(f"Could not get credentials for {cluster_name}: {stderr}\n")
        skipped_clusters.append({
            "name": cluster_name,
            "location": location,
            "project": project_id,
            "reason": f"gcloud get-credentials failed: {stderr.strip()}"
        })
        return None

    findings = []
    checks_run = []
    limitations = []
    checks_not_applicable = []

    check_cfs_quota(env, cluster_name, workspace, findings, checks_run, limitations)
    check_conntrack(env, cluster_name, workspace, findings, checks_run, limitations, checks_not_applicable)
    check_ingress_drain(env, cluster_name, workspace, findings, checks_run, limitations)
    check_ephemeral_storage(env, cluster_name, workspace, findings, checks_run, limitations)
    check_ulimit_exhaustion(env, cluster_name, workspace, findings, checks_run, limitations)

    cluster_scope = {
        "name": cluster_name,
        "location": location,
        "project": project_id,
        "checks_run": checks_run
    }
    if limitations:
        cluster_scope["limitations"] = "; ".join(limitations)
    if checks_not_applicable:
        cluster_scope["checks_not_applicable"] = checks_not_applicable

    return {
        "scope": cluster_scope,
        "findings": findings
    }

def audit_project_telemetry(project_id: str, workspace: str, skipped_clusters: list, active_clusters: list) -> list[dict]:
    """Audits all running GKE clusters in project, returning findings."""
    findings = []
    clusters = run_gcloud_json(["gcloud", "container", "clusters", "list", "--project", project_id, "--format=json"])
    if not isinstance(clusters, list):
        sys.stderr.write(f"Failed to list clusters in project {project_id} or no clusters found.\n")
        return findings

    for c in clusters:
        name = c.get("name", "")
        loc = c.get("location", "")
        status = c.get("status", "")
        if not name:
            continue
        if status != "RUNNING":
            skipped_clusters.append({
                "name": name,
                "location": loc,
                "project": project_id,
                "reason": f"Cluster is not RUNNING (status: {status})"
            })
            continue

        result = inspect_cluster_telemetry(project_id, name, loc, workspace, skipped_clusters)
        if result:
            active_clusters.append(result["scope"])
            findings.extend(result["findings"])

    return findings

def main():
    parser = argparse.ArgumentParser(description="Audit GKE Runtime Telemetry & Container OS Health")
    parser.add_argument("--project-id", help="Optional GCP Project ID")
    parser.add_argument("--workspace", help="Optional GitOps repository workspace root directory path")
    parser.add_argument("--output", help="Optional path to write findings JSON")
    args = parser.parse_args()

    workspace = args.workspace or os.environ.get("FLEET_AUDIT_GITOPS_ROOT") or os.environ.get("GITOPS_WORKSPACE", "")
    target_projects = get_target_projects(args.project_id)
    
    all_findings = []
    skipped_clusters = []
    active_clusters = []

    for proj in target_projects:
        proj_findings = audit_project_telemetry(proj, workspace, skipped_clusters, active_clusters)
        all_findings.extend(proj_findings)

    findings_document = {
        "audit": "gke-runtime-telemetry-audit",
        "scope": {
            "clusters": active_clusters,
            "skipped": skipped_clusters
        },
        "findings": all_findings
    }

    if args.output:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(findings_document, f, indent=2)
        except Exception as e:
            sys.stderr.write(f"Failed to write output to {args.output}: {e}\n")
            sys.exit(1)

    print(f"Wrote {len(all_findings)} telemetry findings across {len(active_clusters)} active GKE clusters. {len(skipped_clusters)} clusters skipped.")

if __name__ == "__main__":
    main()
