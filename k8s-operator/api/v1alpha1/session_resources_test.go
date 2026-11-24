package v1alpha1

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
)

// The session pod's resources resolve per key over the defaults, and a
// request above its resolved limit is lowered to the limit (#2371).
func TestSessionResourcesResolvePerKey(t *testing.T) {
	q := resource.MustParse
	for name, tc := range map[string]struct {
		override           *corev1.ResourceRequirements
		cpuReq, memReq     string
		cpuLimit, memLimit string
	}{
		"none": {nil, DefaultSessionCPURequest, DefaultSessionMemoryRequest, DefaultSessionCPULimit, DefaultSessionMemoryLimit},
		"one request": {&corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceCPU: q("100m")}},
			"100m", DefaultSessionMemoryRequest, DefaultSessionCPULimit, DefaultSessionMemoryLimit},
		"a limit alone keeps the default request": {&corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: q("1Gi")}},
			DefaultSessionCPURequest, DefaultSessionMemoryRequest, DefaultSessionCPULimit, "1Gi"},
		"a limit below the default request lowers it": {&corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: q("256Mi")}},
			DefaultSessionCPURequest, "256Mi", DefaultSessionCPULimit, "256Mi"},
		"a request alone above the default limit raises the limit": {&corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceMemory: q("4Gi")}},
			DefaultSessionCPURequest, "4Gi", DefaultSessionCPULimit, "4Gi"},
		"both set, a request above its limit is lowered": {&corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceCPU: q("3")}, Limits: corev1.ResourceList{corev1.ResourceCPU: q("1")}},
			"1", DefaultSessionMemoryRequest, "1", DefaultSessionMemoryLimit},
		"zero and negative keep their defaults": {&corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceCPU: q("0"), corev1.ResourceMemory: q("-100Mi")},
			Limits:   corev1.ResourceList{corev1.ResourceMemory: q("-1Gi")}},
			DefaultSessionCPURequest, DefaultSessionMemoryRequest, DefaultSessionCPULimit, DefaultSessionMemoryLimit},
		"all four": {&corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceCPU: q("300m"), corev1.ResourceMemory: q("400Mi")},
			Limits:   corev1.ResourceList{corev1.ResourceCPU: q("1"), corev1.ResourceMemory: q("1Gi")}},
			"300m", "400Mi", "1", "1Gi"},
	} {
		got := ResolveSessionResources(tc.override)
		for _, c := range []struct {
			set  corev1.ResourceList
			res  corev1.ResourceName
			want string
		}{
			{got.Requests, corev1.ResourceCPU, tc.cpuReq}, {got.Requests, corev1.ResourceMemory, tc.memReq},
			{got.Limits, corev1.ResourceCPU, tc.cpuLimit}, {got.Limits, corev1.ResourceMemory, tc.memLimit},
		} {
			if v := c.set[c.res]; v.Cmp(q(c.want)) != 0 {
				t.Errorf("%s: %s = %s, want %s", name, c.res, v.String(), c.want)
			}
		}
	}
	// The override passed in is not changed by the lowering.
	o := &corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceCPU: q("3")}, Limits: corev1.ResourceList{corev1.ResourceCPU: q("1")}}
	ResolveSessionResources(o)
	if v := o.Requests[corev1.ResourceCPU]; v.Cmp(q("3")) != 0 {
		t.Errorf("the override was changed to %s", v.String())
	}
}

// Whatever an override holds, the resolved values are what the gateway
// accepts (a2a/gateway sessionResourcesFromEnv): every quantity positive, and
// every request at most its limit. A value the gateway refused would stop it
// from starting on a CR typo.
func TestResolvedSessionResourcesAreAlwaysAcceptable(t *testing.T) {
	q := resource.MustParse
	vals := []string{"-1", "0", "1m", "150m", "0.5", "2", "3", "320Mi", "-1Gi", "4Gi", "1e3", "500000000n"}
	for _, a := range vals {
		for _, b := range vals {
			o := &corev1.ResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceCPU: q(a), corev1.ResourceMemory: q(a)},
				Limits:   corev1.ResourceList{corev1.ResourceCPU: q(b), corev1.ResourceMemory: q(b)},
			}
			got := ResolveSessionResources(o)
			for _, name := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
				req, lim := got.Requests[name], got.Limits[name]
				if req.Sign() <= 0 || lim.Sign() <= 0 || req.Cmp(lim) > 0 {
					t.Errorf("requests %s, limits %s: %s resolved to request %s, limit %s", a, b, name, req.String(), lim.String())
				}
				if _, err := resource.ParseQuantity(req.String()); err != nil {
					t.Errorf("%s request %q doesn't parse back: %v", name, req.String(), err)
				}
			}
		}
	}
}
