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
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
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
		"blank description":        func(p *agentv1alpha1.AgentProfile) { p.Spec.Description = "   " },
		"no persona image":         func(p *agentv1alpha1.AgentProfile) { p.Spec.Persona.Image = "" },
		"no harness image":         func(p *agentv1alpha1.AgentProfile) { p.Spec.Harness.Image = "" },
		"negative maxTurns":        func(p *agentv1alpha1.AgentProfile) { p.Spec.Harness.MaxTurns = -1 },
		"no deadline":              func(p *agentv1alpha1.AgentProfile) { p.Spec.Lifecycle.ActiveDeadlineSeconds = 0 },
		"negative ttl":             func(p *agentv1alpha1.AgentProfile) { p.Spec.Lifecycle.TTLSecondsAfterFinished = -1 },
		"negative queue timeout":   func(p *agentv1alpha1.AgentProfile) { p.Spec.QueueTimeoutSeconds = -1 },
		"no concurrency":           func(p *agentv1alpha1.AgentProfile) { p.Spec.Concurrency = 0 },
		"dotted name":              func(p *agentv1alpha1.AgentProfile) { p.Name = "my.profile" },
		"reserved name platform":   func(p *agentv1alpha1.AgentProfile) { p.Name = "platform" },
		"dotted topic token":       func(p *agentv1alpha1.AgentProfile) { p.Spec.Bus.PublishTopics = []string{"shared.up.grade"} },
		"unscoped topic":           func(p *agentv1alpha1.AgentProfile) { p.Spec.Bus.SubscribeTopics = []string{"blueprint"} },
		"prefixed topic":           func(p *agentv1alpha1.AgentProfile) { p.Spec.Bus.PublishTopics = []string{"a2a.topics.shared.annotations"} },
		"wildcard topic":           func(p *agentv1alpha1.AgentProfile) { p.Spec.Bus.SubscribeTopics = []string{"shared.*"} },
		"clusterRef missing field": func(p *agentv1alpha1.AgentProfile) { p.Spec.ClusterRef.Location = "" },
		"bad serviceAccountName":   func(p *agentv1alpha1.AgentProfile) { p.Spec.Identity.ServiceAccountName = "Not_A_Name" },
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
