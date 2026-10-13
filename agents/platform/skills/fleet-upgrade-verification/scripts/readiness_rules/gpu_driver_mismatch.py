"""
Entry 18 of docs/designs/upgrade-failure-catalogue.md, GPU driver mismatch: the target node
image ships a driver, the container ships a CUDA build, and a driver below what the build
needs cannot open the device.

For every template requesting `nvidia.com/gpu`, the CUDA version is read from the image tag
or from the two env values that name a toolkit version (`CUDA_VERSION`, `NVIDIA_REQUIRE_CUDA`),
and the driver from the pools carrying an accelerator the template selects: the pool's
`imageType` and `gpuDriverVersion` (DEFAULT or LATEST) pick a branch from GKE's per-minor
table at the target's minor. On Autopilot GKE provisions the node itself, on
Container-Optimized OS with the driver the template's `gke-gpu-driver-version` selector names
(`default` when absent), so the same table answers with no pool. A driver below the CUDA
major's floor is `blocking`; one below the toolkit's own minimum is a `risk` (the build runs
under minor version compatibility and loses the features newer than the driver); an image
naming no CUDA version is a `risk`, as is a Standard template whose accelerator no pool
carries; a target outside the driver table, an operator-installed driver, or a record with no
install mode is `unknown`. A pin the driver serves is a note, with the reminder that
forward-compatibility packages inside the image are not readable from the tag. The rule can
block, so `CAN_BLOCK` is True and its unknowns move the member's verdict. The rule needs the
CronJobs of the workload read; when that read failed it grades what it has and says so.

Sources: docs.cloud.google.com/kubernetes-engine/docs/how-to/gpus (the driver per minor);
docs.nvidia.com/cuda/cuda-toolkit-release-notes/ (the driver each toolkit needs);
docs.cloud.google.com/kubernetes-engine/docs/how-to/autopilot-gpus (the Autopilot selector).
"""

import readiness_rules as rules
import upgrade_shape_tables as tables

RULE_ID = "gpu-driver"
ENTRY = 18
CAN_BLOCK = True
# The reads this rule needs beyond the PDB read, as `read_errors` keys them.
TEMPLATE_READ_KINDS = ("daemonset", "cronjob")
CUDA_FORMAT = "{major}.{minor}"
DRIVER_FORMAT = "R{branch}"
PIN_IMAGE = "image {image}"
PIN_ENV = "env {name}={value}"
NO_PIN_DETAIL = "requests {resource} and its image {image} names no CUDA version; the driver it needs cannot be read from the tag"
NO_POOL_DETAIL = "requests {resource}{accelerator} and no pool in the cluster carries {what}; the target driver cannot be read, and the pin {pins} stays unchecked"
ACCELERATOR_TEXT = " on accelerator {accelerator}"
WHAT_THAT_ACCELERATOR = "that accelerator"
WHAT_ANY_ACCELERATOR = "an accelerator"
UNKNOWN_DRIVER_DETAIL = "requests {resource} with {pins} on {pool}: {reason}"
REASON_NO_TARGET = "no target to read the driver at"
REASON_DISABLED = "the pool installs no driver (INSTALLATION_DISABLED); the operator-installed driver is not in the record"
REASON_NO_MODE = "the pool record carries no gpuDriverVersion"
REASON_NO_IMAGE_TABLE = "no driver table for node image type {image_type}"
REASON_NO_TARGET_ROW = "the {image_type} driver table covers {first} to {last}, not the target {target}"
REASON_UNKNOWN_CUDA = "CUDA {cuda} is outside the table (majors {majors})"
BLOCKING_DETAIL = "requests {resource} with {pin} on {pool}; the target {target} node image ships driver {driver} ({mode}), below the {floor} floor for CUDA {major}.x; the device does not open"
RISK_DETAIL = "requests {resource} with {pin} on {pool}; the target {target} node image ships driver {driver} ({mode}), below the {minimum} CUDA {cuda} names; the build runs under minor version compatibility and features newer than the driver fail"
NOTE_SERVED = "{kind} {object} requests {resource} with {pin} on {pool}; the target {target} node image ships driver {driver} ({mode}), which serves CUDA {cuda}; forward-compatibility packages inside the image are not readable from the tag"
MODE_TEXT = "gpuDriverVersion {mode}"
POOL_TEXT = "pool {pool}"
AUTOPILOT_POOL_TEXT = "the node Autopilot provisions"
AUTOPILOT_MODE_TEXT = "{label} {value}"
AUTOPILOT_MODE_DEFAULT = "default"
WORKLOADS_UNREAD_DETAIL = "DaemonSets and CronJobs not read ({reason}); Deployments and StatefulSets graded"


def cuda_pins(spec: dict) -> list[tuple[tuple[int, int], str]]:
    """((major, minor), where) for every CUDA version the template's containers name."""
    pins = []
    for container in rules.containers(spec):
        image = container.get("image") or ""
        m = tables.CUDA_IMAGE_PIN_RE.search(image)
        if m:
            pins.append((_version(m.group(1)), PIN_IMAGE.format(image=image)))
        for env in container.get("env") or []:
            if not isinstance(env, dict):
                continue
            name = env.get("name") or ""
            value = str(env.get("value") or "")
            pattern = {tables.CUDA_VERSION_ENV: tables.CUDA_VERSION_VALUE_RE, tables.NVIDIA_REQUIRE_CUDA_ENV: tables.NVIDIA_REQUIRE_CUDA_RE}.get(name)
            v = pattern.search(value) if pattern else None
            if v:
                pins.append((_version(v.group(1)), PIN_ENV.format(name=name, value=value)))
    return pins


def _version(text: str) -> tuple[int, int]:
    major, _, minor = text.partition(".")
    return int(major), int(minor)


def requests_gpu(spec: dict) -> list[str]:
    out = []
    for container in rules.containers(spec):
        resources = container.get("resources") or {}
        if tables.GPU_RESOURCE in (resources.get("limits") or {}) or tables.GPU_RESOURCE in (resources.get("requests") or {}):
            out.append(container.get("name", ""))
    return out


def accelerator_pools(spec: dict, pools: list[dict]) -> tuple[list[dict], list[str]]:
    """The pools carrying an accelerator the template can land on, and the accelerators it selects."""
    wanted = rules.selected_values(spec, tables.ACCELERATOR_LABEL)
    out = []
    for pool in rules.pools_for_template(spec, pools):
        accelerators = [a for a in rules.pool_config(pool).get("accelerators") or [] if isinstance(a, dict)]
        if not accelerators:
            continue
        if wanted and not any(a.get("acceleratorType") in wanted for a in accelerators):
            continue
        out.append(pool)
    return out, wanted


def _table_driver(image_type: str, mode: str, target) -> tuple[int | None, str]:
    """(driver branch, reason when None) from the per-image-type table at the target's minor."""
    if target is None:
        return None, REASON_NO_TARGET
    table = tables.GKE_GPU_DRIVERS_BY_IMAGE_TYPE.get(image_type)
    if table is None:
        return None, REASON_NO_IMAGE_TABLE.format(image_type=image_type or "?")
    row = table.get(tuple(target[:2]))
    if row is None:
        minors = sorted(table)
        return None, REASON_NO_TARGET_ROW.format(image_type=image_type, first=rules.minor_text(minors[0]), last=rules.minor_text(minors[-1]), target=rules.minor_text(target))
    return row[0] if mode == tables.GPU_DRIVER_DEFAULT else row[1], ""


def driver_at_target(pool: dict, target) -> tuple[int | None, str, str]:
    """(driver branch, install mode text, reason when None) for `pool` at the target's minor."""
    config = rules.pool_config(pool)
    modes = {(a.get("gpuDriverInstallationConfig") or {}).get("gpuDriverVersion") for a in config.get("accelerators") or [] if isinstance(a, dict)}
    mode = next((m for m in modes if m), None)
    mode_text = MODE_TEXT.format(mode=mode)
    if mode == tables.GPU_DRIVER_INSTALLATION_DISABLED:
        return None, mode_text, REASON_DISABLED
    if mode not in (tables.GPU_DRIVER_DEFAULT, tables.GPU_DRIVER_LATEST):
        return None, mode_text, REASON_NO_MODE
    driver, reason = _table_driver(config.get("imageType") or "", mode, target)
    return driver, mode_text, reason


def autopilot_driver(spec: dict, target) -> tuple[int | None, str, str]:
    """The driver Autopilot installs on the node it provisions: the COS table at the target,
    in the mode the template's gke-gpu-driver-version selector names (`default` when absent)."""
    selected = rules.selected_values(spec, tables.AUTOPILOT_GPU_DRIVER_LABEL)
    latest = tables.AUTOPILOT_GPU_DRIVER_LATEST in selected
    mode = tables.GPU_DRIVER_LATEST if latest else tables.GPU_DRIVER_DEFAULT
    mode_text = AUTOPILOT_MODE_TEXT.format(label=tables.AUTOPILOT_GPU_DRIVER_LABEL, value=tables.AUTOPILOT_GPU_DRIVER_LATEST if latest else AUTOPILOT_MODE_DEFAULT)
    driver, reason = _table_driver(tables.IMAGE_TYPE_COS_CONTAINERD, mode, target)
    return driver, mode_text, reason


def _unknown(obj: dict, detail: str, **extra) -> dict:
    finding = rules.new_finding(RULE_ID, ENTRY, rules.GRADE_UNKNOWN, obj, detail, **extra)
    finding["reason"] = rules.describe(finding)
    return finding


def _grade_pins(result: dict, obj: dict, pins: list, pool_text: str, driver: int, mode_text: str, target) -> None:
    for (major, minor), where in pins:
        cuda = CUDA_FORMAT.format(major=major, minor=minor)
        fields = {"resource": tables.GPU_RESOURCE, "pin": where, "pool": pool_text, "target": rules.minor_text(target), "driver": DRIVER_FORMAT.format(branch=driver), "mode": mode_text, "cuda": cuda, "major": major}
        floor = tables.CUDA_MAJOR_MIN_DRIVER.get(major)
        if floor is None:
            detail = UNKNOWN_DRIVER_DETAIL.format(resource=tables.GPU_RESOURCE, pins=where, pool=pool_text, reason=REASON_UNKNOWN_CUDA.format(cuda=cuda, majors=rules.LIST_SEPARATOR.join(str(m) for m in sorted(tables.CUDA_MAJOR_MIN_DRIVER))))
            result["unknown"].append(_unknown(obj, detail, pool=pool_text, cuda=cuda))
            continue
        minimum = tables.CUDA_TOOLKIT_MIN_DRIVER.get((major, minor))
        if driver < floor:
            detail = BLOCKING_DETAIL.format(floor=DRIVER_FORMAT.format(branch=floor), **fields)
            result["blocking"].append(rules.new_finding(RULE_ID, ENTRY, rules.GRADE_BLOCKING, obj, detail, pool=pool_text, cuda=cuda, driver=driver))
        elif minimum is not None and driver < minimum:
            detail = RISK_DETAIL.format(minimum=DRIVER_FORMAT.format(branch=minimum), **fields)
            result["risks"].append(rules.new_finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, detail, pool=pool_text, cuda=cuda, driver=driver))
        else:
            result["notes"].append(NOTE_SERVED.format(kind=obj["kind"], object=obj["object"], **fields))


def evaluate(cluster: dict, member: dict, read, target, context: dict) -> dict:
    result = rules.new_result()
    items = rules.read_items(read)
    unread = rules.unread_kinds(read, TEMPLATE_READ_KINDS)
    if unread:
        result["unknown"].append(rules.rule_unknown(RULE_ID, ENTRY, WORKLOADS_UNREAD_DETAIL.format(reason=next(iter(unread.values())))))
    autopilot = bool(context.get(rules.CONTEXT_AUTOPILOT))
    for obj, spec, _ in rules.templates(items):
        gpu_containers = requests_gpu(spec)
        if not gpu_containers:
            continue
        pins = cuda_pins(spec)
        if not pins:
            images = rules.LIST_SEPARATOR.join(c.get("image") or "" for c in rules.containers(spec) if c.get("name") in gpu_containers)
            result["risks"].append(rules.new_finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, NO_PIN_DETAIL.format(resource=tables.GPU_RESOURCE, image=images)))
            continue
        pins_text = rules.LIST_SEPARATOR.join(where for _, where in pins)
        if autopilot:
            driver, mode_text, reason = autopilot_driver(spec, target)
            if driver is None:
                detail = UNKNOWN_DRIVER_DETAIL.format(resource=tables.GPU_RESOURCE, pins=pins_text, pool=AUTOPILOT_POOL_TEXT, reason=reason)
                result["unknown"].append(_unknown(obj, detail, pool=None))
            else:
                _grade_pins(result, obj, pins, AUTOPILOT_POOL_TEXT, driver, mode_text, target)
            continue
        pools, wanted = accelerator_pools(spec, context.get(rules.CONTEXT_POOLS) or [])
        if not pools:
            accelerator = ACCELERATOR_TEXT.format(accelerator=rules.LIST_SEPARATOR.join(wanted)) if wanted else ""
            what = WHAT_THAT_ACCELERATOR if wanted else WHAT_ANY_ACCELERATOR
            detail = NO_POOL_DETAIL.format(resource=tables.GPU_RESOURCE, accelerator=accelerator, what=what, pins=pins_text)
            result["risks"].append(rules.new_finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, detail, cuda=[CUDA_FORMAT.format(major=v[0], minor=v[1]) for v, _ in pins]))
            continue
        for pool in pools:
            pool_text = POOL_TEXT.format(pool=pool.get("name", ""))
            driver, mode_text, reason = driver_at_target(pool, target)
            if driver is None:
                detail = UNKNOWN_DRIVER_DETAIL.format(resource=tables.GPU_RESOURCE, pins=pins_text, pool=pool_text, reason=reason)
                result["unknown"].append(_unknown(obj, detail, pool=pool.get("name", "")))
                continue
            _grade_pins(result, obj, pins, pool_text, driver, mode_text, target)
    return result


def describe(entry: dict) -> str:
    return rules.describe(entry)
