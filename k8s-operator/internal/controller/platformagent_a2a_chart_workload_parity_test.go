/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"bufio"
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	"k8s.io/apimachinery/pkg/labels"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"
	"sigs.k8s.io/yaml"
)

// chartRendersEnv names the directory of chart renders this test reads, one
// subdirectory per release shape holding manifest.yaml (a helm template
// output) and expected (the chart-pods count the chart stamped for it).
// tests/test_quota_preflight.py writes it and runs this test, failing unless
// it passes, so the skip below only spares a plain go test without renders.
const chartRendersEnv = "KUBE_AGENTS_CHART_RENDERS"

// The operator's count from the chart's workloads (a2aChartWorkloadsCount,
// behind a2aChartWorkloadSelector) equals what the chart stamps as the
// chart-pods annotation or ConfigMap for the same values, over every shape
// #5002's rendered-manifest oracle checks (#5006).
func TestChartWorkloadsCountMatchesTheRenderedChart(t *testing.T) {
	dir := os.Getenv(chartRendersEnv)
	if dir == "" {
		t.Skipf("%s is unset; tests/test_quota_preflight.py renders the chart and runs this test", chartRendersEnv)
	}
	cases, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(cases) == 0 {
		t.Fatalf("no renders in %s", dir)
	}
	selector := labels.SelectorFromSet(labels.Set(a2aChartWorkloadSelector))
	for _, c := range cases {
		t.Run(c.Name(), func(t *testing.T) {
			raw, err := os.ReadFile(filepath.Join(dir, c.Name(), "expected"))
			if err != nil {
				t.Fatal(err)
			}
			want, err := strconv.ParseInt(strings.TrimSpace(string(raw)), 10, 64)
			if err != nil {
				t.Fatal(err)
			}
			manifest, err := os.ReadFile(filepath.Join(dir, c.Name(), "manifest.yaml"))
			if err != nil {
				t.Fatal(err)
			}
			var deployments []appsv1.Deployment
			var statefulSets []appsv1.StatefulSet
			reader := utilyaml.NewYAMLReader(bufio.NewReader(bytes.NewReader(manifest)))
			for {
				doc, err := reader.Read()
				if errors.Is(err, io.EOF) {
					break
				}
				if err != nil {
					t.Fatal(err)
				}
				var head struct {
					Kind     string `json:"kind"`
					Metadata struct {
						Namespace string            `json:"namespace"`
						Labels    map[string]string `json:"labels"`
					} `json:"metadata"`
				}
				if err := yaml.Unmarshal(doc, &head); err != nil {
					t.Fatal(err)
				}
				// helm template renders the release namespace as "default".
				if ns := head.Metadata.Namespace; (ns != "" && ns != "default") || !selector.Matches(labels.Set(head.Metadata.Labels)) {
					continue
				}
				switch head.Kind {
				case "Deployment":
					var d appsv1.Deployment
					if err := yaml.Unmarshal(doc, &d); err != nil {
						t.Fatal(err)
					}
					deployments = append(deployments, d)
				case "StatefulSet":
					var s appsv1.StatefulSet
					if err := yaml.Unmarshal(doc, &s); err != nil {
						t.Fatal(err)
					}
					statefulSets = append(statefulSets, s)
				}
			}
			if got := a2aChartWorkloadsCount(deployments, statefulSets); got != want {
				t.Errorf("the operator counts %d chart pods from %d Deployments and %d StatefulSets, the chart stamps %d", got, len(deployments), len(statefulSets), want)
			}
		})
	}
}
