package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
)

// The A2A session pod's default requests and limits (#2371). The requests
// are from a heavy-turn run on ka-fleet-dev-1 (2026-11-16): every case peaked
// at 157 to 180Mi of cgroup memory.peak, light asks, heavy ones and a 22-turn
// conversation alike, since the harness dominates, so 320Mi is that × 1.8;
// CPU throttling at 150m barely touches a turn the model dominates. The
// limits are the sandbox's, which a headless harness turn has been observed
// to fit in. On Autopilot without Pod bursting, its per-pod minimums (250m,
// 512Mi) apply instead of the requests, and the limit is rewritten to the
// request. The gateway keeps the
// same values as its fallback (a2a/gateway/spawn.go), held equal by a test.
// spec.harness.tuning.sessionResources overrides each one.
const (
	DefaultSessionCPURequest    = "150m"
	DefaultSessionMemoryRequest = "320Mi"
	DefaultSessionCPULimit      = "2"
	DefaultSessionMemoryLimit   = "2Gi"
	// DefaultSessionEphemeralStorage is the session pod's ephemeral-storage
	// request and limit. It isn't configurable; it is here so the install
	// footprint and the quota preflight read one value.
	DefaultSessionEphemeralStorage = "1088Mi"
)

// ResolveSessionResources is the session pod's CPU and memory requests and
// limits for an override (spec.harness.tuning.sessionResources, nil for
// none). Each of requests.cpu, requests.memory, limits.cpu and limits.memory
// is the override's if set to a positive quantity, else its default,
// independently: an override of one key keeps the other three, and a zero or
// negative key keeps its default rather than reaching a pod the API server
// refuses. Where a request ends up above its limit:
//   - a request set alone, above its default limit, raises the limit to it,
//     so asking for more gets more, not a request quietly cut to the default
//     limit;
//   - otherwise the request is lowered to the limit, so an override of only a
//     lower limit works.
//
// Ephemeral storage is not resolved here (DefaultSessionEphemeralStorage).
func ResolveSessionResources(override *corev1.ResourceRequirements) corev1.ResourceRequirements {
	var requests, limits corev1.ResourceList
	if override != nil {
		requests, limits = override.Requests, override.Limits
	}
	pick := func(set corev1.ResourceList, name corev1.ResourceName, def string) (resource.Quantity, bool) {
		if q, ok := set[name]; ok && q.Sign() > 0 {
			return q.DeepCopy(), true
		}
		return resource.MustParse(def), false
	}
	out := corev1.ResourceRequirements{Requests: corev1.ResourceList{}, Limits: corev1.ResourceList{}}
	for _, r := range []struct {
		name       corev1.ResourceName
		req, limit string
	}{
		{corev1.ResourceCPU, DefaultSessionCPURequest, DefaultSessionCPULimit},
		{corev1.ResourceMemory, DefaultSessionMemoryRequest, DefaultSessionMemoryLimit},
	} {
		req, reqSet := pick(requests, r.name, r.req)
		lim, limSet := pick(limits, r.name, r.limit)
		if req.Cmp(lim) > 0 {
			if reqSet && !limSet {
				lim = req.DeepCopy()
			} else {
				req = lim.DeepCopy()
			}
		}
		out.Requests[r.name], out.Limits[r.name] = req, lim
	}
	return out
}
