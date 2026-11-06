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
	"slices"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const dispatchConfigDrainingFixturePath = "../../../a2a/lib/testdata/rendered-dispatch-config-draining.json"

// A profile the applied dispatch config lists and the render no longer does
// drains (#5074): from now for its active deadline plus the margin, with no
// durables. One already draining keeps its time until that passes, and a
// profile the render has is never draining. With no applied config nothing
// drains.
func TestADroppedProfileDrainsForItsDeadlineAndThenGoes(t *testing.T) {
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	until := now.Add(time.Hour)
	applied := &a2aDispatchConfig{Profiles: []a2aDispatchProfile{
		{Name: "gone", Concurrency: 2, Tiers: []string{"read"}, ActiveDeadlineSeconds: 1800,
			Durables: []a2aDispatchDurable{{Tier: "read", Kind: "top", Name: profileDurableName("gone", "read", "top")}}},
		{Name: "draining", Concurrency: 1, Tiers: []string{"read"}, ActiveDeadlineSeconds: 600, Durables: []a2aDispatchDurable{}, DrainingUntil: until},
		{Name: "kept", Concurrency: 1, Tiers: []string{"read"}, ActiveDeadlineSeconds: 600,
			Durables: []a2aDispatchDurable{{Tier: "read", Kind: "top", Name: profileDurableName("kept", "read", "top")}}},
	}}
	live := map[string]bool{"kept": true}

	got := drainingDispatchProfiles(applied, live, now)
	names := map[string]a2aDispatchProfile{}
	for _, p := range got {
		names[p.Name] = p
	}
	if _, ok := names["kept"]; ok || len(got) != 2 {
		t.Fatalf("draining %v, want gone and draining only", got)
	}
	gone := names["gone"]
	if want := now.Add(1800*time.Second + a2aDrainingMargin); !gone.DrainingUntil.Equal(want) || len(gone.Durables) != 0 || gone.Concurrency != 2 {
		t.Errorf("a newly dropped profile drains as %+v, want until %v with no durables and its limits kept", gone, want)
	}
	if d := names["draining"]; !d.DrainingUntil.Equal(until) {
		t.Errorf("an already draining profile's time moved: %v, want %v", d.DrainingUntil, until)
	}

	after := drainingDispatchProfiles(applied, live, until)
	if slices.ContainsFunc(after, func(p a2aDispatchProfile) bool { return p.Name == "draining" }) {
		t.Errorf("a profile past its DrainingUntil still drains: %v", after)
	}
	if got := drainingDispatchProfiles(nil, live, now); got != nil {
		t.Errorf("with no applied config, draining %v", got)
	}
}

// A draining profile is in the dispatch config with no durables and in the
// dispatcher's identity with its .supervisor and stop grants and no durable
// grant; the render the lib side decodes is written as a cross-module
// fixture, which lib validates strictly.
func TestADrainingProfileKeepsItsTerminalGrantAndNoDurable(t *testing.T) {
	withDispatcher(t)
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	tse := testAgentProfile(agent.Namespace, "tse")
	draining := []a2aDispatchProfile{{Name: "gone", Concurrency: 2, Tiers: []string{"read"}, ActiveDeadlineSeconds: 1800,
		Durables: []a2aDispatchDurable{}, DrainingUntil: time.Date(2026, 11, 6, 12, 45, 0, 0, time.UTC)}}

	cm, err := buildA2ADispatchConfigMap(agent, []agentv1alpha1.AgentProfile{tse}, draining)
	if err != nil {
		t.Fatal(err)
	}
	checkFixture(t, dispatchConfigDrainingFixturePath, cm.Data[a2aDispatchConfigKey])
	var cfg a2aDispatchConfig
	if err := json.Unmarshal([]byte(cm.Data[a2aDispatchConfigKey]), &cfg); err != nil {
		t.Fatal(err)
	}
	var gone *a2aDispatchProfile
	for i := range cfg.Profiles {
		if cfg.Profiles[i].Name == "gone" {
			gone = &cfg.Profiles[i]
		}
	}
	if gone == nil || len(gone.Durables) != 0 || gone.DrainingUntil.IsZero() {
		t.Fatalf("the dispatch config's draining profile is %+v", gone)
	}
	if !strings.Contains(cm.Data[a2aDispatchConfigKey], `"drainingUntil": "2026-11-06T12:45:00Z"`) {
		t.Errorf("drainingUntil is not rendered in RFC 3339:\n%s", cm.Data[a2aDispatchConfigKey])
	}

	doc, err := renderA2AAuthMapDraining(agent, []agentv1alpha1.AgentProfile{tse}, draining)
	if err != nil {
		t.Fatal(err)
	}
	var publish []string
	for _, id := range doc.Identities {
		if id.User == a2aDispatcherUser {
			publish = id.Grants.Publish
		}
	}
	if !slices.Contains(publish, "a2a.tasks.gone.*.supervisor") {
		t.Errorf("the dispatcher lost the draining profile's .supervisor grant: %v", publish)
	}
	for _, g := range publish {
		if strings.Contains(g, "profile-gone-") {
			t.Errorf("the dispatcher keeps a draining profile's durable grant %q", g)
		}
	}
	if !slices.Contains(publish, "a2a.tasks.tse.*.supervisor") {
		t.Errorf("the live profile's grant went missing: %v", publish)
	}
}

// The reconcile reads the applied dispatch config for the profiles to
// drain: one it lists that the render no longer has drains; none applied,
// or one it can't read, drains nothing.
func TestTheReconcileDrainsWhatTheAppliedConfigListed(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	r, cl, _ := a2aGateTestReconciler(t, agent)
	ctx := context.Background()
	if got := r.dispatchDraining(ctx, agent, nil); got != nil {
		t.Fatalf("with no applied config, draining %v", got)
	}

	applied := a2aDispatchConfig{Profiles: []a2aDispatchProfile{{Name: "gone", Concurrency: 1, Tiers: []string{"read"}, ActiveDeadlineSeconds: 600,
		Durables: []a2aDispatchDurable{{Tier: "read", Kind: "top", Name: profileDurableName("gone", "read", "top")}}}}}
	body, _ := json.Marshal(applied)
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: a2aDispatchConfigName(agent), Namespace: agent.Namespace},
		Data: map[string]string{a2aDispatchConfigKey: string(body)}}
	if err := cl.Create(ctx, cm); err != nil {
		t.Fatal(err)
	}
	got := r.dispatchDraining(ctx, agent, nil)
	if len(got) != 1 || got[0].Name != "gone" || got[0].DrainingUntil.IsZero() || len(got[0].Durables) != 0 {
		t.Errorf("draining %+v, want gone, draining with no durables", got)
	}

	cm.Data[a2aDispatchConfigKey] = "{not json"
	if err := cl.Update(ctx, cm); err != nil {
		t.Fatal(err)
	}
	if got := r.dispatchDraining(ctx, agent, nil); got != nil {
		t.Errorf("an unreadable applied config drained %v", got)
	}
}
