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
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// envtestProfileNamespace holds the admission cases.
const envtestProfileNamespace = "agentprofile-admission"

// The CRD is the admission validation the file loader used to do by hand
// (a2a/profiles' Validate, retired with it). Every refusal it made is made here
// by the API server serving the generated schema, so a marker that silently
// stops validating fails this test rather than admitting the profile.
func TestTheAgentProfileCRDRefusesWhatTheLoaderRefused(t *testing.T) {
	cfg, scheme := startEnvtestConfig(t)
	c, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: envtestProfileNamespace}}); err != nil {
		t.Fatal(err)
	}

	valid := testAgentProfile(envtestProfileNamespace, "valid", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Bus.PublishTopics = []string{"agent.valid.findings"}
		p.Spec.Bus.SubscribeTopics = []string{"shared.blueprint"}
		p.Spec.ClusterRef = &agentv1alpha1.AgentProfileClusterRef{ProjectID: "p", Cluster: "c", Location: "l"}
	})
	valid.Generation = 0
	if err := c.Create(ctx, &valid); err != nil {
		t.Fatalf("a valid AgentProfile was refused: %v", err)
	}

	cases := map[string]func(*agentv1alpha1.AgentProfile){
		"blank description":                  func(p *agentv1alpha1.AgentProfile) { p.Spec.Description = "   " },
		"no persona image":                   func(p *agentv1alpha1.AgentProfile) { p.Spec.Persona.Image = "" },
		"no harness image":                   func(p *agentv1alpha1.AgentProfile) { p.Spec.Harness.Image = "" },
		"negative maxTurns":                  func(p *agentv1alpha1.AgentProfile) { p.Spec.Harness.MaxTurns = -1 },
		"no deadline":                        func(p *agentv1alpha1.AgentProfile) { p.Spec.Lifecycle.ActiveDeadlineSeconds = 0 },
		"negative ttl":                       func(p *agentv1alpha1.AgentProfile) { p.Spec.Lifecycle.TTLSecondsAfterFinished = -1 },
		"negative queue timeout":             func(p *agentv1alpha1.AgentProfile) { p.Spec.QueueTimeoutSeconds = -1 },
		"no concurrency":                     func(p *agentv1alpha1.AgentProfile) { p.Spec.Concurrency = 0 },
		"dotted name":                        func(p *agentv1alpha1.AgentProfile) { p.Name = "my.profile" },
		"reserved name platform":             func(p *agentv1alpha1.AgentProfile) { p.Name = "platform" },
		"name too long for its bus identity": func(p *agentv1alpha1.AgentProfile) { p.Name = strings.Repeat("a", 56) },
		"dotted topic token":                 func(p *agentv1alpha1.AgentProfile) { p.Spec.Bus.PublishTopics = []string{"shared.up.grade"} },
		"unscoped topic":                     func(p *agentv1alpha1.AgentProfile) { p.Spec.Bus.SubscribeTopics = []string{"blueprint"} },
		"prefixed topic": func(p *agentv1alpha1.AgentProfile) {
			p.Spec.Bus.PublishTopics = []string{"a2a.topics.shared.annotations"}
		},
		"wildcard topic":           func(p *agentv1alpha1.AgentProfile) { p.Spec.Bus.SubscribeTopics = []string{"shared.*"} },
		"clusterRef missing field": func(p *agentv1alpha1.AgentProfile) { p.Spec.ClusterRef.Location = "" },
		"bad serviceAccountName":   func(p *agentv1alpha1.AgentProfile) { p.Spec.Identity.ServiceAccountName = "Not_A_Name" },
		"another profile's SA":     func(p *agentv1alpha1.AgentProfile) { p.Spec.Identity.ServiceAccountName = "agentprofile-other" },
		"oversize description":     func(p *agentv1alpha1.AgentProfile) { p.Spec.Description = strings.Repeat("x", 4097) },
	}
	// The loader's resources check (cpu and memory, requests and limits) is
	// required fields in the schema. A typed client cannot omit them (a zero
	// Quantity is sent as "0"), so the missing-field case goes in
	// unstructured.
	raw, err := runtime.DefaultUnstructuredConverter.ToUnstructured(valid.DeepCopy())
	if err != nil {
		t.Fatal(err)
	}
	u := &unstructured.Unstructured{Object: raw}
	u.SetName("no-requests")
	u.SetResourceVersion("")
	unstructured.RemoveNestedField(u.Object, "spec", "resources", "requests")
	unstructured.RemoveNestedField(u.Object, "status")
	if err := c.Create(ctx, u); err == nil {
		t.Error("an AgentProfile with no resources.requests was admitted; want refused")
	}
	for name, path := range map[string][]string{
		"no-limits":     {"spec", "resources", "limits"},
		"no-limits-cpu": {"spec", "resources", "limits", "cpu"},
		"no-spec":       {"spec"},
	} {
		v := &unstructured.Unstructured{Object: runtime.DeepCopyJSON(raw)}
		v.SetName(name)
		v.SetResourceVersion("")
		unstructured.RemoveNestedField(v.Object, "status")
		unstructured.RemoveNestedField(v.Object, path...)
		if err := c.Create(ctx, v); err == nil {
			t.Errorf("%s: admitted; want refused", name)
		}
	}

	for name, mutate := range cases {
		p := valid.DeepCopy()
		p.ResourceVersion = ""
		p.Name = "case"
		mutate(p)
		if err := c.Create(ctx, p); err == nil {
			t.Errorf("%s: admitted; want refused", name)
			_ = c.Delete(ctx, p)
		}
	}
}

// TestTheAgentProfileCRDHoldsScriptNamesToTheTriggerList holds
// spec.scripts[].name to the names AgentSchedule.spec.script and
// lib.ValidateTrigger take, on the one case list both of their tests read
// (triggerScriptNamesPath): a profile can list exactly the scripts a schedule
// can name.
func TestTheAgentProfileCRDHoldsScriptNamesToTheTriggerList(t *testing.T) {
	raw, err := os.ReadFile(triggerScriptNamesPath)
	if err != nil {
		t.Fatal(err)
	}
	var names struct {
		Accepted []string `json:"accepted"`
		Refused  []string `json:"refused"`
	}
	if err := json.Unmarshal(raw, &names); err != nil {
		t.Fatal(err)
	}
	if len(names.Accepted) == 0 || len(names.Refused) == 0 {
		t.Fatalf("the case list is empty: %+v", names)
	}
	cfg, scheme := startEnvtestConfig(t)
	c, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	const ns = "agentprofile-script-names"
	if err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}); err != nil {
		t.Fatal(err)
	}
	n := 0
	mk := func(script string) *agentv1alpha1.AgentProfile {
		n++
		p := testAgentProfile(ns, fmt.Sprintf("p-%d", n), func(p *agentv1alpha1.AgentProfile) {
			p.Spec.Scripts = []agentv1alpha1.AgentProfileScript{
				{Name: script, Path: agentv1alpha1.AgentProfileScriptRoot + "helper.py", TimeoutSeconds: 300},
			}
		})
		p.Generation = 0
		return &p
	}
	for _, name := range names.Accepted {
		p := mk(name)
		if err := c.Create(ctx, p); err != nil {
			t.Errorf("script name %q refused: %v", name, err)
			continue
		}
		var back agentv1alpha1.AgentProfile
		if err := c.Get(ctx, client.ObjectKeyFromObject(p), &back); err != nil ||
			len(back.Spec.Scripts) != 1 || back.Spec.Scripts[0].Name != name {
			t.Errorf("script name %q read back as %+v (%v)", name, back.Spec.Scripts, err)
		}
	}
	for _, name := range names.Refused {
		if err := c.Create(ctx, mk(name)); err == nil || !apierrors.IsInvalid(err) {
			t.Errorf("script name %q: create = %v, want refused as invalid", name, err)
		}
	}
}
