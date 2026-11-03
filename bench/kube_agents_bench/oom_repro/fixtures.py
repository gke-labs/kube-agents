"""Pure renderers for the OOM reproduction fleet: KWOK nodes, workloads, Events and the KWOK config.

Nothing here reaches a cluster, so every shape the harness applies is covered by unit tests.
"""
import ipaddress
import json

import yaml

# The zones and node pools the fake nodes are spread over, so zone- and pool-shaped reads see a fleet.
ZONES = ("us-central1-f", "us-central1-b", "us-central1-c")
NODE_POOLS = ("pool-a", "pool-b", "pool-c")
REGION = "us-central1"
# Capacity each fake node advertises: room for the workload Pods and nothing else to account for.
NODE_INSTANCE_TYPE = "n2-standard-32"
NODE_CPU = "32"
NODE_MEMORY = "128Gi"
NODE_PODS = "110"
NODE_NAME_FORMAT = "kwok-node-{:05d}"
# KWOK manages nodes carrying this annotation; the taint keeps real Pods off them.
KWOK_ANNOTATION = "kwok.x-k8s.io/node"
KWOK_ANNOTATION_VALUE = "fake"
KWOK_TAINT_KEY = "kwok.x-k8s.io/node"
NODE_ARCH = "amd64"
NODE_OS = "linux"
NO_SCHEDULE = "NoSchedule"
FAKE_VERSION = "fake"
KWOK_NODE_LABEL = "type"
KWOK_NODE_LABEL_VALUE = "kwok"
# Workload Pods: the pause image, small requests, scheduled onto the fake nodes only.
PAUSE_IMAGE = "registry.k8s.io/pause:3.10"
POD_CPU_REQUEST = "100m"
POD_MEMORY_REQUEST = "128Mi"
POD_CPU_LIMIT = "500m"
POD_MEMORY_LIMIT = "512Mi"
SERVICE_PORT = 80
SERVICE_TARGET_PORT = 8080
NAMESPACE_FORMAT = "team-{:03d}"
DEPLOYMENT_FORMAT = "svc-{:02d}"
# Warning Events of the kinds a large cluster carries, against Pods of the workload namespaces.
EVENT_REASONS = (
    ("FailedScheduling", "default-scheduler",
     "0/{nodes} nodes are available: {nodes} Insufficient cpu. preemption: 0/{nodes} nodes are available: "
     "{nodes} No preemption victims found for incoming pod."),
    ("BackOff", "kubelet", "Back-off restarting failed container app in pod {pod}_{ns}({uid})"),
    ("Unhealthy", "kubelet",
     "Readiness probe failed: Get \"http://10.64.12.7:8080/healthz\": dial tcp 10.64.12.7:8080: connect: connection refused"),
    ("FailedMount", "kubelet", "MountVolume.SetUp failed for volume \"config\" : configmap \"svc-config\" not found"),
)
EVENT_TYPE = "Warning"
EVENT_MAX_COUNT = 40
# What the KWOK release ships, and what this harness changes in it. GKE refuses a FlowSchema that
# names the cluster-critical "exempt" priority level, so that document is dropped. The default
# lease and stage parallelism of 4 cannot keep 1,500 node leases fresh: nodes go NotReady and GKE's
# node lifecycle controller deletes them. The default Pod CIDR (/24) has 254 addresses.
KWOK_CONFIG_MAP = "kwok"
KWOK_CONFIG_KEY = "kwok.yaml"
KWOK_PARALLELISM = {"nodeLeaseParallelism": 32, "podPlayStageParallelism": 32, "nodePlayStageParallelism": 16}
KWOK_POD_CIDR = "100.64.0.1/10"
DROPPED_KINDS = ("FlowSchema",)
# Subnets for the test clusters: /16 blocks below the auto-mode range of the default VPC.
SUBNET_BLOCK_FIRST = 100
SUBNET_BLOCK_LAST = 127
SUBNET_PREFIX = 16
AUTO_MODE_RANGE = "10.128.0.0/9"
NETWORK_PREFIX = "10."


def node(index: int) -> dict:
    """One fake node, spread across ZONES and NODE_POOLS by index."""
    name = NODE_NAME_FORMAT.format(index)
    capacity = {"cpu": NODE_CPU, "memory": NODE_MEMORY, "pods": NODE_PODS}
    return {
        "apiVersion": "v1", "kind": "Node",
        "metadata": {
            "name": name,
            "annotations": {KWOK_ANNOTATION: KWOK_ANNOTATION_VALUE},
            "labels": {
                KWOK_NODE_LABEL: KWOK_NODE_LABEL_VALUE,
                "kubernetes.io/arch": NODE_ARCH, "kubernetes.io/os": NODE_OS, "kubernetes.io/hostname": name,
                "node.kubernetes.io/instance-type": NODE_INSTANCE_TYPE,
                "topology.kubernetes.io/zone": ZONES[index % len(ZONES)],
                "topology.kubernetes.io/region": REGION,
                "cloud.google.com/gke-nodepool": NODE_POOLS[index % len(NODE_POOLS)],
            },
        },
        "spec": {"taints": [{"key": KWOK_TAINT_KEY, "value": KWOK_ANNOTATION_VALUE, "effect": NO_SCHEDULE}]},
        "status": {"allocatable": capacity, "capacity": capacity, "phase": "Running",
                   "nodeInfo": {"architecture": NODE_ARCH, "operatingSystem": NODE_OS,
                                "kubeletVersion": FAKE_VERSION, "kubeProxyVersion": FAKE_VERSION}},
    }


def namespace(index: int) -> dict:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE_FORMAT.format(index)}}


def deployment(ns: str, index: int, replicas: int) -> dict:
    name = DEPLOYMENT_FORMAT.format(index)
    labels = {"app": name}
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "nodeSelector": {KWOK_NODE_LABEL: KWOK_NODE_LABEL_VALUE},
                    "tolerations": [{"key": KWOK_TAINT_KEY, "operator": "Exists", "effect": NO_SCHEDULE}],
                    "containers": [{"name": "app", "image": PAUSE_IMAGE, "resources": {
                        "requests": {"cpu": POD_CPU_REQUEST, "memory": POD_MEMORY_REQUEST},
                        "limits": {"cpu": POD_CPU_LIMIT, "memory": POD_MEMORY_LIMIT}}}],
                },
            },
        },
    }


def service(ns: str, index: int) -> dict:
    name = DEPLOYMENT_FORMAT.format(index)
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns},
            "spec": {"selector": {"app": name}, "ports": [{"port": SERVICE_PORT, "targetPort": SERVICE_TARGET_PORT}]}}


def workloads(namespaces: int, per_namespace: int, replicas: int) -> list[dict]:
    """Every namespace first, then each namespace's Deployments and Services."""
    docs = [namespace(n) for n in range(namespaces)]
    for n in range(namespaces):
        ns = NAMESPACE_FORMAT.format(n)
        for d in range(per_namespace):
            docs += [deployment(ns, d, replicas), service(ns, d)]
    return docs


def event(index: int, namespaces: int, per_namespace: int, nodes: int, run_id: str, now: str,
          pad_to: int = 0) -> tuple[str, dict]:
    """One Warning Event against a Pod name in the workload namespaces: (namespace, body).

    ``pad_to`` lengthens the message to that many characters, as a large cluster's scheduler
    messages are; the API server stores what it is given, so the watcher holds it too.
    """
    ns = NAMESPACE_FORMAT.format(index % namespaces)
    pod = f"{DEPLOYMENT_FORMAT.format((index // namespaces) % per_namespace)}-{index:08x}"
    reason, source, template = EVENT_REASONS[index % len(EVENT_REASONS)]
    uid = f"{index:012x}"
    message = template.format(nodes=nodes, pod=pod, ns=ns, uid=uid)
    if len(message) < pad_to:
        message = (message + " ") + ("x" * (pad_to - len(message) - 1))
    return ns, {
        "apiVersion": "v1", "kind": "Event",
        "metadata": {"name": f"{pod}.{run_id}{index:08x}", "namespace": ns},
        "involvedObject": {"kind": "Pod", "namespace": ns, "name": pod, "apiVersion": "v1",
                           "uid": f"00000000-0000-0000-0000-{uid}"},
        "reason": reason, "type": EVENT_TYPE, "message": message,
        "source": {"component": source}, "reportingComponent": source,
        "count": 1 + index % EVENT_MAX_COUNT, "firstTimestamp": now, "lastTimestamp": now,
    }


def kwok_manifest(release_yaml: str) -> str:
    """The KWOK release manifest without the documents GKE refuses."""
    docs = [d for d in yaml.safe_load_all(release_yaml) if d and d.get("kind") not in DROPPED_KINDS]
    return yaml.safe_dump_all(docs, sort_keys=False)


def kwok_config(config_map: dict) -> dict:
    """The kwok ConfigMap with the parallelism and Pod CIDR this fleet needs, ready to apply."""
    config = yaml.safe_load(config_map["data"][KWOK_CONFIG_KEY])
    config.setdefault("options", {}).update(KWOK_PARALLELISM)
    config["options"]["cidr"] = KWOK_POD_CIDR
    out = {"apiVersion": "v1", "kind": "ConfigMap",
           "metadata": {"name": config_map["metadata"]["name"], "namespace": config_map["metadata"]["namespace"]},
           "data": {KWOK_CONFIG_KEY: yaml.safe_dump(config, sort_keys=False)}}
    return out


def free_blocks(subnets: list[dict], count: int) -> list[int]:
    """Second octets of ``count`` free 10.N.0.0/16 blocks, given ``gcloud compute networks subnets list`` JSON."""
    used = [ipaddress.ip_network(AUTO_MODE_RANGE)]
    for s in subnets:
        used.append(ipaddress.ip_network(s["ipCidrRange"]))
        used += [ipaddress.ip_network(r["ipCidrRange"]) for r in s.get("secondaryIpRanges", [])]
    free = [b for b in range(SUBNET_BLOCK_FIRST, SUBNET_BLOCK_LAST + 1)
            if not any(ipaddress.ip_network(f"{NETWORK_PREFIX}{b}.0.0/{SUBNET_PREFIX}").overlaps(u) for u in used)]
    if len(free) < count:
        raise ValueError(f"only {len(free)} free /{SUBNET_PREFIX} blocks between 10.{SUBNET_BLOCK_FIRST} "
                         f"and 10.{SUBNET_BLOCK_LAST}; {count} needed")
    return free[:count]


def chunks(docs: list[dict], parts: int) -> list[str]:
    """``docs`` dealt round-robin into ``parts`` multi-document YAML strings, for parallel applies."""
    return ["".join("---\n" + json.dumps(d) + "\n" for d in docs[k::parts]) for k in range(parts) if docs[k::parts]]
