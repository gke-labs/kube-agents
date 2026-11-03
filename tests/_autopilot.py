"""The band GKE Autopilot admits a requests pair in unchanged, in GiB of memory per vCPU.

Outside it Autopilot raises the smaller request into the band, so the pod it admits is larger
than the manifest and than what the chart's quota preflight sums. One home for the figure, which
this repository does not own; the operator's copy is autopilotMinMemoryBytesPerVCPU and
autopilotMaxMemoryBytesPerVCPU in k8s-operator/internal/controller/credential_proxy_validation.go.

The band tests (the operator's, the web console's and hindsight-api's) read their pair through
the parsers here, which are the footprint generator's: it reads every form the chart's own
kube-agents.parseBytes and parseCpuMillis read (Ki..Ei, k..E, exponents, fractions, bare
counts), so a request re-spelt in a manifest is the same number here as in the chart, and a
string that is not a quantity raises rather than being sliced into a wrong one. Loaded by path,
as tests/test_chart_values_fleet_example.py and tests/test_generate_chart_footprint.py do.
"""

import importlib.util
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_GENERATOR_PATH = _REPO_ROOT / "scripts" / "generate_chart_footprint.py"
_GENERATOR_MODULE = "generate_chart_footprint"
_generator_spec = importlib.util.spec_from_file_location(_GENERATOR_MODULE, _GENERATOR_PATH)
_generator = importlib.util.module_from_spec(_generator_spec)
sys.modules.setdefault(_GENERATOR_MODULE, _generator)
_generator_spec.loader.exec_module(_generator)

MILLIS_PER_CORE = 1000
_GIB = 1024**3

AUTOPILOT_MIN_GIB_PER_VCPU = 1
AUTOPILOT_MAX_GIB_PER_VCPU = 6.5


def cpu_millis(quantity) -> int:
    """A CPU quantity as integer millicores: `500m` is 500, `1` is 1000, `0.5` is 500."""
    return _generator.parse_cpu_millis(quantity)


def memory_gib(quantity) -> float:
    """A memory quantity as GiB: `256Mi` is 0.25, `2Gi` is 2.0, `2048Mi` is 2.0."""
    return _generator.parse_bytes(quantity) / _GIB


def gib_per_vcpu(cpu, memory) -> float:
    """The ratio Autopilot judges a requests pair by, from the two quantities a manifest holds."""
    return memory_gib(memory) / (cpu_millis(cpu) / MILLIS_PER_CORE)


def gib_per_vcpu_in_band(ratio: float) -> bool:
    """Whether a requests pair at this ratio is admitted unchanged."""
    return AUTOPILOT_MIN_GIB_PER_VCPU <= ratio <= AUTOPILOT_MAX_GIB_PER_VCPU


def requests_in_band(cpu, memory) -> bool:
    """Whether the requests pair, as the two quantities a manifest holds, is admitted unchanged."""
    return gib_per_vcpu_in_band(gib_per_vcpu(cpu, memory))
