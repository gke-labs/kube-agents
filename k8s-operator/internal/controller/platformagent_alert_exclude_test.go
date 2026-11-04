/*
Copyright 2026 Google LLC.

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
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func agentWithAlertExclude(rules ...agentv1alpha1.EventWatcherAlertExcludeRule) *agentv1alpha1.PlatformAgent {
	a := newTestPlatformAgent()
	a.Spec.Harness = &agentv1alpha1.HarnessSpec{EventWatcher: &agentv1alpha1.EventWatcherSpec{AlertExclude: rules}}
	return a
}

// alertExcludeEnv returns every WATCHER_ALERT_EXCLUDE value on the gateway's
// agent-api-auth sidecar.
func alertExcludeEnv(agent *agentv1alpha1.PlatformAgent) []string {
	var found []string
	for _, e := range buildAgentAPIAuthSidecar(agent, "/opt/data").Env {
		if e.Name == watcherAlertExcludeEnv {
			found = append(found, e.Value)
		}
	}
	return found
}

// The list reaches the watcher as the JSON its --alert-exclude reads: the
// watcher's keys, unset parts left out (cmd/k8s-event-watcher/alertexclude.go).
func TestAlertExcludeReachesTheWatcherAsItsJSON(t *testing.T) {
	agent := agentWithAlertExclude(
		agentv1alpha1.EventWatcherAlertExcludeRule{Namespace: "payments"},
		agentv1alpha1.EventWatcherAlertExcludeRule{
			Reason:  "FailedScheduling",
			Cluster: &agentv1alpha1.EventWatcherAlertExcludeCluster{ProjectID: "p", Location: "us-central1", ClusterName: "batch"},
		},
		agentv1alpha1.EventWatcherAlertExcludeRule{Kind: "Pod", Name: "inventory-api-*"},
	)
	const want = `[{"namespace":"payments"},` +
		`{"cluster":{"projectId":"p","location":"us-central1","clusterName":"batch"},"reason":"FailedScheduling"},` +
		`{"kind":"Pod","name":"inventory-api-*"}]`
	if got := alertExcludeEnv(agent); len(got) != 1 || got[0] != want {
		t.Errorf("WATCHER_ALERT_EXCLUDE = %q, want one entry %q", got, want)
	}
}

// With no rules the operator writes nothing, so an install that never sets the
// field gets no new env entry and no pod roll on upgrade.
func TestNoAlertExcludeWritesNoEnv(t *testing.T) {
	for name, agent := range map[string]*agentv1alpha1.PlatformAgent{
		"no harness":       newTestPlatformAgent(),
		"no event watcher": agentWithEventWatcher(nil),
		"an empty list":    agentWithAlertExclude(),
	} {
		t.Run(name, func(t *testing.T) {
			if got := alertExcludeEnv(agent); len(got) != 0 {
				t.Errorf("WATCHER_ALERT_EXCLUDE = %q, want none", got)
			}
		})
	}
}

// Set, the field wins over a same-named spec.deployment.env entry, and there is
// one entry: server-side apply refuses a duplicate key in env.
func TestAlertExcludeReplacesTheDeploymentEnvEntry(t *testing.T) {
	agent := agentWithAlertExclude(agentv1alpha1.EventWatcherAlertExcludeRule{Namespace: "payments"})
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Env: []corev1.EnvVar{{Name: watcherAlertExcludeEnv, Value: `[{"namespace":"shop"}]`}},
	}
	if got := alertExcludeEnv(agent); len(got) != 1 || got[0] != `[{"namespace":"payments"}]` {
		t.Errorf("WATCHER_ALERT_EXCLUDE = %q, want only the field's value", got)
	}
}

// Unset, the field leaves a spec.deployment.env entry alone: the watcher read
// it before the field existed, and a reserved name would drop it silently.
func TestNoAlertExcludeKeepsTheDeploymentEnvEntry(t *testing.T) {
	agent := newTestPlatformAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Env: []corev1.EnvVar{{Name: watcherAlertExcludeEnv, Value: `[{"namespace":"shop"}]`}},
	}
	if got := alertExcludeEnv(agent); len(got) != 1 || got[0] != `[{"namespace":"shop"}]` {
		t.Errorf("WATCHER_ALERT_EXCLUDE = %q, want the spec.deployment.env value", got)
	}
}

// The name is the one start-services.sh reads and passes to the watcher.
func TestAlertExcludeEnvMatchesStartServices(t *testing.T) {
	script, err := os.ReadFile(filepath.Join("..", "..", "..", "deploy", "shared", "start-services.sh"))
	if err != nil {
		t.Fatalf("read start-services.sh: %v", err)
	}
	if want := `--alert-exclude="${` + watcherAlertExcludeEnv + `}"`; !strings.Contains(string(script), want) {
		t.Errorf("start-services.sh does not pass %s", want)
	}
}

// The API server refuses a rule that would silence every alert, and a name with
// a "*" anywhere but at its end; it admits the shapes the watcher reads.
func TestAlertExcludeSchemaRulesEnvtest(t *testing.T) {
	cl, _ := startEnvtest(t)
	ctx := context.Background()

	const namespace = "alert-exclude"
	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	newAgent := func(name string, rules []agentv1alpha1.EventWatcherAlertExcludeRule) *agentv1alpha1.PlatformAgent {
		return &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Harness: &agentv1alpha1.HarnessSpec{
					ProjectID:    envtestHarnessProject,
					Location:     envtestHarnessLocation,
					ClusterName:  envtestHarnessCluster,
					EventWatcher: &agentv1alpha1.EventWatcherSpec{AlertExclude: rules},
				},
			},
		}
	}
	type rule = agentv1alpha1.EventWatcherAlertExcludeRule

	refused := map[string]struct {
		rules   []rule
		message string
	}{
		"empty rule":           {[]rule{{Namespace: "payments"}, {}}, "sets at least one of cluster, namespace, reason, kind or name"},
		"bare wildcard name":   {[]rule{{Name: "*"}}, "sets at least one of cluster, namespace, reason, kind or name"},
		"star inside a name":   {[]rule{{Name: "api-*-x"}}, "spec.harness.eventWatcher.alertExclude[0].name"},
		"cluster with no name": {[]rule{{Cluster: &agentv1alpha1.EventWatcherAlertExcludeCluster{ProjectID: "p"}}}, "clusterName"},
		"bad namespace":        {[]rule{{Namespace: "Payments"}}, "spec.harness.eventWatcher.alertExclude[0].namespace"},
		"too many rules":       {slices.Repeat([]rule{{Namespace: "payments"}}, 65), "must have at most 64 items"},
	}
	for name, tc := range refused {
		t.Run("refused/"+name, func(t *testing.T) {
			err := cl.Create(ctx, newAgent(strings.ReplaceAll(name, " ", "-"), tc.rules))
			if err == nil || !strings.Contains(err.Error(), tc.message) {
				t.Fatalf("want a refusal naming %q, got %v", tc.message, err)
			}
		})
	}

	admitted := map[string][]rule{
		"namespace":       {{Namespace: "payments"}},
		"cluster by name": {{Cluster: &agentv1alpha1.EventWatcherAlertExcludeCluster{ClusterName: "batch"}}},
		"every part": {{
			Cluster:   &agentv1alpha1.EventWatcherAlertExcludeCluster{ProjectID: "p", Location: "us-central1", ClusterName: "batch"},
			Namespace: "shop", Reason: "CrashLoopBackOff", Kind: "Pod", Name: "inventory-api-*",
		}},
		"wildcard with another part": {{Namespace: "shop", Name: "*"}},
		"64 rules":                   slices.Repeat([]rule{{Namespace: "payments"}}, 64),
	}
	for name, rules := range admitted {
		t.Run("admitted/"+name, func(t *testing.T) {
			if err := cl.Create(ctx, newAgent(strings.ReplaceAll(name, " ", "-"), rules)); err != nil {
				t.Fatalf("want admitted, got %v", err)
			}
		})
	}
}
