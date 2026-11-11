"""The sized fleet run's records: each window's measurements, and the run's cost record.

docs/designs/sized-test-fleet.md, "Logs and records", is the design. The cost
record is computed by the run's own code, with no billing export: the inputs are
measured (the run's instances, disks and clusters, found by their
``kube-agents-scale-run`` label, and LiteLLM's counters) and priced from the
Cloud Billing Catalog API's list prices, which need no billing account access.
The record keeps every input, its price and its cost, so the sum can be checked
by hand.
"""

from __future__ import annotations

import http.client
import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from kube_agents_bench.scale import collect, kube

CATALOG = "https://cloudbilling.googleapis.com/v1/services/{service}/skus?currencyCode=USD&pageSize=5000"
QUOTA_HEADER = "x-goog-user-project"
COMPUTE_SERVICE = "6F81-5844-456A"
GKE_SERVICE = "CCD8-9BF1-090E"
GLOBAL_REGION = "global"
DEFAULT_REGION = "us-central1"
# The catalog names N1 SKUs by continent ("running in Americas"); a region's prefix says which.
CONTINENTS = (
    (("us-", "northamerica-", "southamerica-"), "Americas"),
    (("europe-", "me-", "africa-"), "EMEA"),
    (("asia-", "australia-"), "APAC"),
)


def skus(region: str) -> tuple[tuple[str, str, str, str], ...]:
    """(record key, service, SKU description, region) for each price the record uses, in ``region``."""
    continent = next((name for prefixes, name in CONTINENTS if region.startswith(prefixes)), None)
    if continent is None:
        raise LookupError(f"no catalog continent for region {region!r}")
    return (
        ("n1_core_hour", COMPUTE_SERVICE, f"N1 Predefined Instance Core running in {continent}", region),
        ("n1_ram_gib_hour", COMPUTE_SERVICE, f"N1 Predefined Instance Ram running in {continent}", region),
        ("pd_standard_gib_month", COMPUTE_SERVICE, "Storage PD Capacity", region),
        ("zonal_cluster_hour", GKE_SERVICE, "Zonal Kubernetes Clusters", GLOBAL_REGION),
    )
# n1-standard-1's shape, as the price is per core and per GiB.
N1_SHAPES = {"n1-standard-1": (1, 3.75), "n1-standard-2": (2, 7.5), "n1-standard-4": (4, 15.0)}
HOURS_PER_MONTH = 730
SECONDS_PER_HOUR = 3600
NANOS = 1e9
ON_DEMAND = "OnDemand"
SPEND_FAMILIES = ("litellm_spend_metric_total", "litellm_spend_metric")
RUN_LABEL = "kube-agents-scale-run"
ROUND = 4
GET_ATTEMPTS = 3
GET_RETRY_SECONDS = 5


def _token() -> str:
    # Application-default credentials: the Catalog API refuses some gcloud user tokens.
    return kube.run([kube.GCLOUD, "auth", "application-default", "print-access-token"]).strip()


def _get(url: str, project: str) -> dict[str, Any]:
    """One catalog page, retried on a dropped connection: a 5,000-SKU page is a few megabytes."""
    for attempt in range(1, GET_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {_token()}", QUOTA_HEADER: project})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 -- a fixed Google API URL
                return json.loads(response.read())
        # IncompleteRead (an HTTPException) is the body cut off part-way, the usual way a large page fails.
        except (ConnectionError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
            if attempt == GET_ATTEMPTS:
                raise
            time.sleep(GET_RETRY_SECONDS * attempt)
    raise AssertionError("unreachable")


def _unit_price(sku: dict[str, Any]) -> float:
    rate = sku["pricingInfo"][0]["pricingExpression"]["tieredRates"][-1]["unitPrice"]
    return int(rate.get("units", "0")) + rate.get("nanos", 0) / NANOS


def pick_prices(skus_by_service: dict[str, list[dict[str, Any]]],
                region: str = DEFAULT_REGION) -> dict[str, dict[str, Any]]:
    """The record's prices from the catalog's SKUs: each by its description, region and on-demand use."""
    prices = {}
    for key, service, description, sku_region in skus(region):
        match = next(
            (s for s in skus_by_service.get(service, [])
             if s["description"] == description and sku_region in s.get("serviceRegions", [])
             and s.get("category", {}).get("usageType", ON_DEMAND) == ON_DEMAND),
            None,
        )
        if match is None:
            raise LookupError(f"no {description!r} SKU in {sku_region}")
        prices[key] = {"usd": _unit_price(match), "sku": match["skuId"], "description": description}
    return prices


def catalog_prices(project: str, region: str = DEFAULT_REGION,
                   get: Callable[[str, str], dict[str, Any]] = _get) -> dict[str, Any]:
    by_service: dict[str, list[dict[str, Any]]] = {}
    for service in {s[1] for s in skus(region)}:
        url, skus = CATALOG.format(service=service), []
        while url:
            page = get(url, project)
            skus += page.get("skus", [])
            token = page.get("nextPageToken")
            url = f"{CATALOG.format(service=service)}&pageToken={token}" if token else ""
        by_service[service] = skus
    return {"source": "Cloud Billing Catalog API", "read": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "region": region, "prices": pick_prices(by_service, region)}


@dataclass
class Instance:
    machine_type: str
    disk_gib: float
    hours: float


def _hours(created: str, until: float) -> float:
    started = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    return max(0.0, (until - started) / SECONDS_PER_HOUR)


def instances(project: str, run_id: str, until: float) -> list[Instance]:
    out = kube.gcloud("compute", "instances", "list", f"--filter=labels.{RUN_LABEL}={run_id}", "--format=json",
                      project=project)
    found = []
    for item in json.loads(out or "[]"):
        disks = sum(float(d.get("diskSizeGb", 0)) for d in item.get("disks", []))
        found.append(Instance(item["machineType"].rsplit("/", 1)[-1], disks, _hours(item["creationTimestamp"], until)))
    return found


def cluster_hours(project: str, run_id: str, until: float) -> list[float]:
    out = kube.gcloud("container", "clusters", "list", f"--filter=resourceLabels.{RUN_LABEL}={run_id}",
                      "--format=json", project=project)
    return [_hours(c["createTime"], until) for c in json.loads(out or "[]")]


def fleet_cost(nodes: list[Instance], clusters: list[float], prices: dict[str, Any]) -> dict[str, Any]:
    """Node, disk and cluster-fee cost: each input, its price and its cost."""
    p = {k: v["usd"] for k, v in prices["prices"].items()}
    core_hours = ram_gib_hours = 0.0
    for node in nodes:
        cores, ram = N1_SHAPES.get(node.machine_type, (0, 0.0))
        if not cores:
            raise ValueError(f"no price shape for {node.machine_type}")
        core_hours += cores * node.hours
        ram_gib_hours += ram * node.hours
    disk_gib_hours = sum(n.disk_gib * n.hours for n in nodes)
    lines = {
        "node_cores": {"quantity": core_hours, "unit": "core-hours", "price": p["n1_core_hour"]},
        "node_ram": {"quantity": ram_gib_hours, "unit": "GiB-hours", "price": p["n1_ram_gib_hour"]},
        "node_disks": {"quantity": disk_gib_hours, "unit": "GiB-hours", "price": p["pd_standard_gib_month"] / HOURS_PER_MONTH},
        "cluster_fees": {"quantity": sum(clusters), "unit": "cluster-hours", "price": p["zonal_cluster_hour"]},
    }
    for line in lines.values():
        line["usd"] = round(line["quantity"] * line["price"], ROUND)
        line["quantity"] = round(line["quantity"], ROUND)
    return {
        "nodes": len(nodes),
        "node_hours": round(sum(n.hours for n in nodes), ROUND),
        "lines": lines,
        "usd": round(sum(line["usd"] for line in lines.values()), 2),
    }


def spend_growth(before: collect.Snapshot, after: collect.Snapshot) -> float:
    """LiteLLM's own spend counter's growth: its price map's cost of the window's model calls."""
    names = {sample for sample, _, _ in after.samples("litellm")}
    family = next((name for name in SPEND_FAMILIES if name in names), None)
    return collect._growth(before, after, "litellm", family) if family else 0.0


def window(install: collect.Install, start: float, end: float,
           before: collect.Snapshot, after: collect.Snapshot) -> dict[str, Any]:
    """One window's measurements: container peaks against their limits, and the model tokens and spend."""
    limits = install.limits()
    containers = {}
    for container, peaks in install.container_peaks(start, end).items():
        limit = limits.get(container, {})
        entry = {k: round(v, ROUND) for k, v in peaks.items()}
        if limit.get("memory_bytes") and peaks.get("memory_peak_bytes"):
            entry["memory_peak_of_limit"] = round(peaks["memory_peak_bytes"] / limit["memory_bytes"], ROUND)
        if limit.get("cpu_cores") and peaks.get("cpu_peak_cores"):
            entry["cpu_peak_of_limit"] = round(peaks["cpu_peak_cores"] / limit["cpu_cores"], ROUND)
        containers[container] = entry
    tokens = {m.name: m.value for m in collect.counter_metrics(before, after) if m.name.startswith("tokens_")}
    return {
        "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "seconds": round(end - start),
        "containers": containers,
        "tokens": tokens,
        "model_spend_usd": round(spend_growth(before, after), ROUND),
    }


def cost_record(project: str, run_id: str, windows: dict[str, dict[str, Any]],
                until: float | None = None, region: str = DEFAULT_REGION) -> dict[str, Any]:
    """The run's cost: the fleet's infrastructure, priced from the catalog in its region, and its model spend."""
    until = until or time.time()
    prices = catalog_prices(project, region)
    infra = fleet_cost(instances(project, run_id, until), cluster_hours(project, run_id, until), prices)
    model = {name: w.get("model_spend_usd", 0.0) for name, w in windows.items()}
    return {
        "run_id": run_id,
        "measured_until": datetime.fromtimestamp(until, timezone.utc).isoformat(),
        "prices": prices,
        "infrastructure": infra,
        "model_spend_usd": model,
        "usd": round(infra["usd"] + sum(model.values()), 2),
        "note": "Model spend is LiteLLM's spend counter, priced by its own price map.",
    }


def safe(fn: Callable[[], dict[str, Any]], errors: list[str], what: str) -> dict[str, Any]:
    """Run one record step; a failure is noted in the run's errors instead of losing the run."""
    try:
        return fn()
    except (kube.CommandError, LookupError, ValueError, OSError, subprocess.SubprocessError,
            http.client.HTTPException) as exc:
        errors.append(f"{what}: {exc}")
        return {}
