"""The band GKE Autopilot admits a requests pair in unchanged, in GiB of memory per vCPU.

Outside it Autopilot raises the smaller request into the band, so the pod it admits is larger
than the manifest and than what the chart's quota preflight sums. One home for the figure, which
this repository does not own; the operator's copy is autopilotMinMemoryBytesPerVCPU and
autopilotMaxMemoryBytesPerVCPU in k8s-operator/internal/controller/credential_proxy_validation.go.
"""

AUTOPILOT_MIN_GIB_PER_VCPU = 1
AUTOPILOT_MAX_GIB_PER_VCPU = 6.5


def gib_per_vcpu_in_band(gib_per_vcpu: float) -> bool:
    """Whether a requests pair at this ratio is admitted unchanged."""
    return AUTOPILOT_MIN_GIB_PER_VCPU <= gib_per_vcpu <= AUTOPILOT_MAX_GIB_PER_VCPU
