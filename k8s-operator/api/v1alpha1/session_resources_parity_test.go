package v1alpha1

import (
	"encoding/json"
	"os"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
)

// sessionResourcesCase is one row of testdata/session_resources_cases.json,
// which the chart's quota preflight test (tests/test_quota_preflight.py) reads
// too, so the preflight resolves every override as the operator does.
type sessionResourcesCase struct {
	Name     string                       `json:"name"`
	Override map[string]map[string]string `json:"override"`
	Want     struct {
		CPURequestMillis   int64 `json:"cpuRequestMillis"`
		CPULimitMillis     int64 `json:"cpuLimitMillis"`
		MemoryRequestBytes int64 `json:"memoryRequestBytes"`
		MemoryLimitBytes   int64 `json:"memoryLimitBytes"`
	} `json:"want"`
}

func TestResolveSessionResourcesMatchesTheSharedCases(t *testing.T) {
	raw, err := os.ReadFile("testdata/session_resources_cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var table struct {
		Cases []sessionResourcesCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatal(err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("no cases in testdata/session_resources_cases.json")
	}
	for _, c := range table.Cases {
		t.Run(c.Name, func(t *testing.T) {
			var override *corev1.ResourceRequirements
			if len(c.Override) > 0 {
				override = &corev1.ResourceRequirements{Requests: corev1.ResourceList{}, Limits: corev1.ResourceList{}}
				for side, keys := range c.Override {
					list := override.Requests
					if side == "limits" {
						list = override.Limits
					}
					for name, q := range keys {
						list[corev1.ResourceName(name)] = resource.MustParse(q)
					}
				}
			}
			got := ResolveSessionResources(override)
			cpuReq, cpuLim := got.Requests[corev1.ResourceCPU], got.Limits[corev1.ResourceCPU]
			memReq, memLim := got.Requests[corev1.ResourceMemory], got.Limits[corev1.ResourceMemory]
			if cpuReq.MilliValue() != c.Want.CPURequestMillis || cpuLim.MilliValue() != c.Want.CPULimitMillis ||
				memReq.Value() != c.Want.MemoryRequestBytes || memLim.Value() != c.Want.MemoryLimitBytes {
				t.Errorf("got cpu %dm/%dm memory %d/%d, want cpu %dm/%dm memory %d/%d",
					cpuReq.MilliValue(), cpuLim.MilliValue(), memReq.Value(), memLim.Value(),
					c.Want.CPURequestMillis, c.Want.CPULimitMillis, c.Want.MemoryRequestBytes, c.Want.MemoryLimitBytes)
			}
		})
	}
}
