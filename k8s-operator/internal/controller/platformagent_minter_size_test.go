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
	"context"
	"fmt"
	"os"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// minterChartTemplatePath is the chart's rule ConfigMap, whose default.yaml is
// the policy every rendered key derives from on a real install. The test reads
// the real text so the size it measures is the size an install renders.
const minterChartTemplatePath = "../../../charts/kube-agents/templates/github-minter.yaml"

// chartMinterDefaultPolicy returns the chart's default.yaml with its two Helm
// values substituted.
func chartMinterDefaultPolicy(t *testing.T) string {
	t.Helper()
	raw, err := os.ReadFile(minterChartTemplatePath)
	if err != nil {
		t.Fatalf("reading the chart's minter template: %v", err)
	}
	const (
		blockStart  = "  default.yaml: |\n"
		blockIndent = "    "
	)
	_, after, found := strings.Cut(string(raw), blockStart)
	if !found {
		t.Fatalf("%s has no %q block", minterChartTemplatePath, strings.TrimSpace(blockStart))
	}
	var sb strings.Builder
	for _, line := range strings.Split(after, "\n") {
		if !strings.HasPrefix(line, blockIndent) {
			break
		}
		sb.WriteString(strings.TrimPrefix(line, blockIndent))
		sb.WriteString("\n")
	}
	policy := strings.NewReplacer(
		"{{ $allowedSa }}", "agent@example.iam.gserviceaccount.com",
		"{{ $repo }}", "gitops",
	).Replace(sb.String())
	if strings.Contains(policy, "{{") {
		t.Fatalf("the chart's default.yaml carries a template value this test does not substitute:\n%s", policy)
	}
	return policy
}

// Five hundred managed repositories render under the ConfigMap limit. Each
// key used to list every same-organisation repository, so the ConfigMap grew
// with the square of the count and crossed 1 MiB near 119 repositories; from
// there the API server refused the update and no repository could be
// onboarded. The quadratic rendering is measured beside the real one so the
// test is known to fail on it.
func TestTheMinterConfigMapStaysUnderTheLimitAtFiveHundredRepositories(t *testing.T) {
	const (
		repositories = 500
		sizeLimit    = 1 << 20
	)
	policy := chartMinterDefaultPolicy(t)
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				IntegrationSpec: agentv1alpha1.IntegrationSpec{GitHub: &agentv1alpha1.GitHubSpec{Org: "test-org"}},
			},
		},
	}
	minterCM := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: minterConfigMapName, Namespace: "test-ns"},
		Data:       map[string]string{minterBaseTemplateKey: policy},
	}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, minterCM).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()

	entries := make([]string, 0, repositories)
	bare := make([]string, 0, repositories)
	for i := 0; i < repositories; i++ {
		name := fmt.Sprintf("repo-%03d", i)
		bare = append(bare, name)
		entries = append(entries, fmt.Sprintf(`{"type":"github","url":"https://github.com/test-org/%s"}`, name))
	}
	if err := r.syncGithubTokenMinterConfigMap(ctx, agent, "["+strings.Join(entries, ",")+"]", ""); err != nil {
		t.Fatalf("syncGithubTokenMinterConfigMap: %v", err)
	}
	if err := cl.Get(ctx, client.ObjectKey{Name: minterConfigMapName, Namespace: "test-ns"}, minterCM); err != nil {
		t.Fatalf("get: %v", err)
	}

	size := 0
	for key, value := range minterCM.Data {
		size += len(key) + len(value)
	}
	if size >= sizeLimit {
		t.Errorf("the minter ConfigMap at %d repositories is %d bytes, at or past the %d-byte limit", repositories, size, sizeLimit)
	}
	if got := len(minterCM.Data); got != repositories+1 {
		t.Errorf("the ConfigMap holds %d keys, want %d policies and default.yaml", got, repositories)
	}
	// One repository per key, which is what keeps the size linear.
	last := minterCM.Data[bare[repositories-1]+minterPolicyKeySuffix]
	if strings.Count(last, "- '") != strings.Count(policy, "- '") || !strings.Contains(last, "- '"+bare[repositories-1]+"'") || strings.Contains(last, "- '"+bare[0]+"'") {
		t.Errorf("the last repository's policy does not list that repository alone:\n%s", last)
	}

	// The rendering this replaced, measured so the limit above is known to
	// bite: every key carrying the whole list.
	quadratic := len(renderRepoPolicy(policy, bare)) * repositories
	if quadratic < sizeLimit {
		t.Fatalf("the whole-list rendering at %d repositories is %d bytes, under the limit; this test would not catch it coming back", repositories, quadratic)
	}
}
