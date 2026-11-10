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
	"reflect"
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

// alertExcludeEnv returns every entry of the named variable on the gateway's
// agent-api-auth sidecar.
func alertExcludeEnv(agent *agentv1alpha1.PlatformAgent, name string) []corev1.EnvVar {
	var found []corev1.EnvVar
	for _, e := range buildAgentAPIAuthSidecar(agent, "/opt/data").Env {
		if e.Name == name {
			found = append(found, e)
		}
	}
	return found
}

// alertExcludeRules is the rules file the operator renders for agent.
func alertExcludeRules(agent *agentv1alpha1.PlatformAgent) string {
	return buildEventWatcherAlertExcludeConfigMap(agent).Data[eventWatcherAlertExcludeKey]
}

// assertAlertExcludeFromTheConfigMap checks that the sidecar takes the rules
// from the ConfigMap: WATCHER_ALERT_EXCLUDE through a key reference, for an
// older entrypoint, and WATCHER_ALERT_EXCLUDE_FILE naming the mounted file.
func assertAlertExcludeFromTheConfigMap(t *testing.T, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	ref := &corev1.EnvVarSource{ConfigMapKeyRef: &corev1.ConfigMapKeySelector{
		LocalObjectReference: corev1.LocalObjectReference{Name: agent.Name + "-event-watcher-alert-exclude"},
		Key:                  "rules.json",
	}}
	if got := alertExcludeEnv(agent, watcherAlertExcludeEnv); len(got) != 1 || got[0].Value != "" || !reflect.DeepEqual(got[0].ValueFrom, ref) {
		t.Errorf("WATCHER_ALERT_EXCLUDE = %+v, want one entry from the ConfigMap's key", got)
	}
	if got := alertExcludeEnv(agent, watcherAlertExcludeFileEnv); len(got) != 1 || got[0].Value != "/etc/kube-agents/event-watcher/rules.json" {
		t.Errorf("WATCHER_ALERT_EXCLUDE_FILE = %+v, want the mounted file", got)
	}
}

// The list reaches the watcher as the JSON its --alert-exclude-file reads: the
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
	if got := alertExcludeRules(agent); got != want {
		t.Errorf("rules.json = %q, want %q", got, want)
	}
	assertAlertExcludeFromTheConfigMap(t, agent)
}

// With no rules the file is empty, and the sidecar still reads it, so setting
// the first rule changes no pod template.
func TestNoAlertExcludeRendersAnEmptyFile(t *testing.T) {
	for name, agent := range map[string]*agentv1alpha1.PlatformAgent{
		"no harness":       newTestPlatformAgent(),
		"no event watcher": agentWithEventWatcher(nil),
		"an empty list":    agentWithAlertExclude(),
	} {
		t.Run(name, func(t *testing.T) {
			if got := alertExcludeRules(agent); got != "" {
				t.Errorf("rules.json = %q, want empty", got)
			}
			assertAlertExcludeFromTheConfigMap(t, agent)
		})
	}
}

// gatewayTemplate renders the gateway's pod template with the hashes the
// reconciler computes for agent.
func gatewayTemplate(t *testing.T, agent *agentv1alpha1.PlatformAgent) corev1.PodTemplateSpec {
	t.Helper()
	var hashes []string
	for _, cm := range []*corev1.ConfigMap{
		buildConfigMap(agent, nil),
		buildFluentBitConfigMap(agent),
		buildSettingsConfigMap(agent),
		gatewayPolicyView(buildCredentialProxyPolicyConfigMap(agent)),
	} {
		h, err := getConfigMapHash(cm)
		if err != nil {
			t.Fatal(err)
		}
		hashes = append(hashes, h)
	}
	return buildPodTemplateSpec(agent, hashes[0], hashes[1], hashes[2], hashes[3], nil, renderOptions{})
}

// Setting, changing or removing rules changes the ConfigMap and not the pod
// template, so the gateway pod is not rolled; the watcher reads the file again.
func TestAlertExcludeChangeLeavesThePodTemplate(t *testing.T) {
	noBlock := newTestPlatformAgent()
	noBlock.Spec.Harness = &agentv1alpha1.HarnessSpec{}
	none := gatewayTemplate(t, noBlock)
	for name, agent := range map[string]*agentv1alpha1.PlatformAgent{
		"an empty list": agentWithAlertExclude(),
		"one rule":      agentWithAlertExclude(agentv1alpha1.EventWatcherAlertExcludeRule{Namespace: "payments"}),
		"two rules":     agentWithAlertExclude(agentv1alpha1.EventWatcherAlertExcludeRule{Namespace: "shop"}, agentv1alpha1.EventWatcherAlertExcludeRule{Reason: "OOMKilled"}),
	} {
		t.Run(name, func(t *testing.T) {
			if got := gatewayTemplate(t, agent); !reflect.DeepEqual(got, none) {
				t.Error("the pod template differs from the one with no eventWatcher block")
			}
		})
	}
}

// The file is the ConfigMap's, mounted read-only and without subPath: the
// kubelet never updates a subPath mount when the ConfigMap changes.
func TestAlertExcludeIsMountedWithoutSubPath(t *testing.T) {
	agent := newTestPlatformAgent()
	var mounts []corev1.VolumeMount
	for _, m := range buildAgentAPIAuthSidecar(agent, "/opt/data").VolumeMounts {
		if m.Name == eventWatcherAlertExcludeVolume {
			mounts = append(mounts, m)
		}
	}
	if len(mounts) != 1 || mounts[0].MountPath != "/etc/kube-agents/event-watcher" || mounts[0].SubPath != "" || !mounts[0].ReadOnly {
		t.Errorf("mounts = %+v, want one read-only mount of the directory with no subPath", mounts)
	}
	var volumes []corev1.Volume
	for _, v := range buildAgentAPIAuthVolumes(agent) {
		if v.Name == eventWatcherAlertExcludeVolume {
			volumes = append(volumes, v)
		}
	}
	if len(volumes) != 1 || volumes[0].ConfigMap == nil || volumes[0].ConfigMap.Name != agent.Name+"-event-watcher-alert-exclude" || len(volumes[0].ConfigMap.Items) != 0 {
		t.Errorf("volumes = %+v, want the whole rules ConfigMap", volumes)
	}
}

// Set, the field wins over a same-named spec.deployment.env entry, and there is
// one entry: server-side apply refuses a duplicate key in env.
func TestAlertExcludeReplacesTheDeploymentEnvEntry(t *testing.T) {
	agent := agentWithAlertExclude(agentv1alpha1.EventWatcherAlertExcludeRule{Namespace: "payments"})
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Env: []corev1.EnvVar{{Name: watcherAlertExcludeEnv, Value: `[{"namespace":"shop"}]`}},
	}
	if got := alertExcludeRules(agent); got != `[{"namespace":"payments"}]` {
		t.Errorf("rules.json = %q, want only the field's value", got)
	}
	assertAlertExcludeFromTheConfigMap(t, agent)
}

// Unset, the field leaves a spec.deployment.env entry alone: the watcher read
// it before the field existed, and a reserved name would drop it silently. No
// file is named then, so the entrypoint passes that entry as --alert-exclude.
func TestNoAlertExcludeKeepsTheDeploymentEnvEntry(t *testing.T) {
	agent := newTestPlatformAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Env: []corev1.EnvVar{{Name: watcherAlertExcludeEnv, Value: `[{"namespace":"shop"}]`}},
	}
	if got := alertExcludeEnv(agent, watcherAlertExcludeEnv); len(got) != 1 || got[0].Value != `[{"namespace":"shop"}]` {
		t.Errorf("WATCHER_ALERT_EXCLUDE = %+v, want the spec.deployment.env value", got)
	}
	if got := alertExcludeEnv(agent, watcherAlertExcludeFileEnv); len(got) != 0 {
		t.Errorf("WATCHER_ALERT_EXCLUDE_FILE = %+v, want none", got)
	}
}

// The largest list the CRD admits renders far under the kernel's 128 KiB limit
// on one environment string (MAX_ARG_STRLEN), which the WATCHER_ALERT_EXCLUDE
// reference still meets; past it, the agent-api-auth container could not start
// at all. Every string part is at its length bound, in characters its pattern
// admits and JSON writes as themselves.
func TestTheLargestAlertExcludeListFitsOneEnvString(t *testing.T) {
	const maxArgStrlen = 128 * 1024
	rule := agentv1alpha1.EventWatcherAlertExcludeRule{
		Cluster: &agentv1alpha1.EventWatcherAlertExcludeCluster{
			ProjectID:   strings.Repeat("a", 100) + ":p" + strings.Repeat("a", 26),
			Location:    strings.Repeat("a", 63),
			ClusterName: strings.Repeat("a", 63),
		},
		Namespace: strings.Repeat("a", 63),
		Reason:    "R" + strings.Repeat("a", 127),
		Kind:      "K" + strings.Repeat("a", 62),
		Name:      strings.Repeat("a", 252) + "*",
	}
	agent := agentWithAlertExclude(slices.Repeat([]agentv1alpha1.EventWatcherAlertExcludeRule{rule}, 64)...)
	if n := len(alertExcludeRules(agent)); n >= maxArgStrlen/2 {
		t.Errorf("the largest admitted list renders %d bytes; want well under %d", n, maxArgStrlen)
	}
}

// The names are the ones start-services.sh reads and passes to the watcher.
func TestAlertExcludeEnvMatchesStartServices(t *testing.T) {
	script, err := os.ReadFile(filepath.Join("..", "..", "..", "deploy", "shared", "start-services.sh"))
	if err != nil {
		t.Fatalf("read start-services.sh: %v", err)
	}
	for _, want := range []string{
		`--alert-exclude="${` + watcherAlertExcludeEnv + `}"`,
		`--alert-exclude-file="${` + watcherAlertExcludeFileEnv + `}"`,
	} {
		if !strings.Contains(string(script), want) {
			t.Errorf("start-services.sh does not pass %s", want)
		}
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
		"empty rule":                 {[]rule{{Namespace: "payments"}, {}}, "sets at least one of cluster, namespace, reason, kind or name"},
		"bare wildcard name":         {[]rule{{Name: "*"}}, "sets at least one of cluster, namespace, reason, kind or name"},
		"star inside a name":         {[]rule{{Name: "api-*-x"}}, "spec.harness.eventWatcher.alertExclude[0].name"},
		"cluster with no name":       {[]rule{{Cluster: &agentv1alpha1.EventWatcherAlertExcludeCluster{ProjectID: "p"}}}, "clusterName"},
		"bad namespace":              {[]rule{{Namespace: "Payments"}}, "spec.harness.eventWatcher.alertExclude[0].namespace"},
		"too many rules":             {slices.Repeat([]rule{{Namespace: "payments"}}, 65), "must have at most 64 items"},
		"long cluster name":          {[]rule{{Cluster: &agentv1alpha1.EventWatcherAlertExcludeCluster{ClusterName: strings.Repeat("a", 64)}}}, "clusterName"},
		"long project":               {[]rule{{Cluster: &agentv1alpha1.EventWatcherAlertExcludeCluster{ProjectID: "p" + strings.Repeat("a", 128), ClusterName: "batch"}}}, "projectId"},
		"escaped reason":             {[]rule{{Reason: "<Back&Off>"}}, "spec.harness.eventWatcher.alertExclude[0].reason"},
		"upper-case name":            {[]rule{{Name: "Payments-*"}}, "spec.harness.eventWatcher.alertExclude[0].name"},
		"wildcard with another part": {[]rule{{Namespace: "shop", Name: "*"}}, "spec.harness.eventWatcher.alertExclude[0].name"},
		"name ending in a dash":      {[]rule{{Name: "payments-api-"}}, "spec.harness.eventWatcher.alertExclude[0].name"},
		"name with two dots":         {[]rule{{Name: "a..b"}}, "spec.harness.eventWatcher.alertExclude[0].name"},
		"location ending in a dash":  {[]rule{{Cluster: &agentv1alpha1.EventWatcherAlertExcludeCluster{Location: "us-central1-", ClusterName: "batch"}}}, "location"},
		"project ending in a dash":   {[]rule{{Cluster: &agentv1alpha1.EventWatcherAlertExcludeCluster{ProjectID: "my-project-", ClusterName: "batch"}}}, "projectId"},
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
		"64 rules":                slices.Repeat([]rule{{Namespace: "payments"}}, 64),
		"domain-scoped project":   {{Cluster: &agentv1alpha1.EventWatcherAlertExcludeCluster{ProjectID: "example.com:my-project", ClusterName: "batch"}}},
		"dotted name":             {{Name: "node-1.example"}},
		"prefix ending in a dash": {{Name: "inventory-api-*"}},
	}
	for name, rules := range admitted {
		t.Run("admitted/"+name, func(t *testing.T) {
			if err := cl.Create(ctx, newAgent(strings.ReplaceAll(name, " ", "-"), rules)); err != nil {
				t.Fatalf("want admitted, got %v", err)
			}
		})
	}
}
