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
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const dispatchConfigDrainingFixturePath = "../../../a2a/lib/testdata/rendered-dispatch-config-draining.json"

// dispatcherMapDrainingFixturePath is the identity map with a draining
// profile, which the callout suite runs a real server against.
const dispatcherMapDrainingFixturePath = "../../../a2a/authcallout/testdata/rendered-identity-map-dispatcher-draining.json"

// A profile the applied dispatch config lists and the render no longer does
// drains (#5074): from now for a2aDrainingWindow (the longest task deadline
// plus the margin), with no durables. One already draining keeps its time
// until that passes, and a profile the render has is never draining. Only
// what the render could itself produce is carried: entries it couldn't are
// dropped, and a time or a limit past its range is clamped. With no applied
// config nothing drains.
func TestADroppedProfileDrainsForItsDeadlineAndThenGoes(t *testing.T) {
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	until := now.Add(time.Hour)
	applied := &a2aDispatchConfig{Profiles: []a2aDispatchProfile{
		{Name: "gone", Concurrency: 2, Tiers: []string{"read"}, ActiveDeadlineSeconds: 1800,
			Durables: []a2aDispatchDurable{{Tier: "read", Kind: "top", Name: profileDurableName("gone", "read", "top")}}},
		{Name: "draining", Concurrency: 1, Tiers: []string{"read"}, ActiveDeadlineSeconds: 600, Durables: []a2aDispatchDurable{}, DrainingUntil: until},
		{Name: "kept", Concurrency: 1, Tiers: []string{"read"}, ActiveDeadlineSeconds: 600,
			Durables: []a2aDispatchDurable{{Tier: "read", Kind: "top", Name: profileDurableName("kept", "read", "top")}}},
		// Not what the render produces: a ConfigMap edit, which must grant nothing.
		{Name: "*", Concurrency: 1, Tiers: []string{"read"}},
		{Name: "bad-tier", Concurrency: 1, Tiers: []string{"read", "a.b"}},
		{Name: "no-tier", Concurrency: 1},
		// Listed twice, and with a time no drain this operator writes has.
		{Name: "gone", Concurrency: 9, Tiers: []string{"read"}},
		{Name: "far", Concurrency: 1, Tiers: []string{"read"}, Durables: []a2aDispatchDurable{}, DrainingUntil: now.AddDate(10, 0, 0),
			// Limits no render produces, which the dispatcher's Validate refuses.
			ActiveDeadlineSeconds: 3000000000, QueueTimeoutSeconds: -5},
	}}
	live := map[string]bool{"kept": true}

	got := drainingDispatchProfiles(applied, live, now)
	names := map[string]a2aDispatchProfile{}
	for _, p := range got {
		names[p.Name] = p
	}
	if _, ok := names["kept"]; ok || len(got) != 3 {
		t.Fatalf("draining %v, want gone, draining and far only", got)
	}
	far := names["far"]
	if !far.DrainingUntil.Equal(now.Add(a2aDrainingWindow)) {
		t.Errorf("a time ten years out was kept as %v, want clamped to %v", far.DrainingUntil, now.Add(a2aDrainingWindow))
	}
	if far.ActiveDeadlineSeconds != agentv1alpha1.DefaultProfileMaxTaskDeadlineSeconds || far.QueueTimeoutSeconds != 0 {
		t.Errorf("limits carried as deadline %d, queue timeout %d; want clamped to %d and 0",
			far.ActiveDeadlineSeconds, far.QueueTimeoutSeconds, agentv1alpha1.DefaultProfileMaxTaskDeadlineSeconds)
	}
	gone := names["gone"]
	// The window is the longest deadline any task can carry, not the
	// profile's current one: a task keeps the deadline it was confirmed with.
	if want := now.Add(a2aDrainingWindow); !gone.DrainingUntil.Equal(want) || len(gone.Durables) != 0 || gone.Concurrency != 2 {
		t.Errorf("a newly dropped profile drains as %+v, want until %v with no durables and its limits kept", gone, want)
	}
	if a2aDrainingWindow < time.Duration(agentv1alpha1.DefaultProfileMaxTaskDeadlineSeconds)*time.Second {
		t.Errorf("the drain window %v is shorter than the longest task deadline", a2aDrainingWindow)
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
	if !strings.Contains(cm.Data[a2aDispatchConfigKey], `"drainingUntil":"2026-11-06T12:45:00Z"`) {
		t.Errorf("drainingUntil is not rendered in RFC 3339:\n%s", cm.Data[a2aDispatchConfigKey])
	}

	shards, _, err := buildA2AAuthMapConfigMaps(agent, []agentv1alpha1.AgentProfile{tse}, draining)
	if err != nil || len(shards) != 1 {
		t.Fatalf("the identity map with a draining profile: %d shards, %v", len(shards), err)
	}
	checkFixture(t, dispatcherMapDrainingFixturePath, shards[0].Data[a2aAuthMapKey])
	doc, err := renderA2AAuthMapDraining(agent, []agentv1alpha1.AgentProfile{tse}, draining)
	if err != nil {
		t.Fatal(err)
	}
	var publish, subscribe []string
	for _, id := range doc.Identities {
		if id.User == a2aDispatcherUser {
			publish, subscribe = id.Grants.Publish, id.Grants.Subscribe
		}
	}
	for _, want := range []string{"a2a.tasks.gone.*.supervisor", "$JS.API.DIRECT.GET." + a2aDispatchStopStream + "." + a2aDispatchStopPrefix + "gone.*"} {
		if !slices.Contains(publish, want) {
			t.Errorf("the dispatcher lost the draining profile's grant %q: %v", want, publish)
		}
	}
	if !slices.Contains(subscribe, a2aDispatchStopPrefix+"gone.*") {
		t.Errorf("the dispatcher lost the draining profile's stop subscription: %v", subscribe)
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
	rec := record.NewFakeRecorder(8)
	r.Recorder = rec
	ctx := context.Background()
	if got, err := r.dispatchDraining(ctx, agent, nil); got != nil || err != nil {
		t.Fatalf("with no applied config, draining %v, %v", got, err)
	}

	applied := a2aDispatchConfig{Profiles: []a2aDispatchProfile{{Name: "gone", Concurrency: 1, Tiers: []string{"read"}, ActiveDeadlineSeconds: 600,
		Durables: []a2aDispatchDurable{{Tier: "read", Kind: "top", Name: profileDurableName("gone", "read", "top")}}}}}
	body, _ := json.Marshal(applied)
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: a2aDispatchConfigName(agent), Namespace: agent.Namespace},
		Data: map[string]string{a2aDispatchConfigKey: string(body)}}
	if err := cl.Create(ctx, cm); err != nil {
		t.Fatal(err)
	}
	got, err := r.dispatchDraining(ctx, agent, nil)
	if err != nil || len(got) != 1 || got[0].Name != "gone" || got[0].DrainingUntil.IsZero() || len(got[0].Durables) != 0 {
		t.Errorf("draining %+v, %v; want gone, draining with no durables", got, err)
	}

	// A read that fails, other than NotFound, fails the pass: rendering
	// without the set would end every drain for good.
	failing := &PlatformAgentReconciler{Scheme: r.Scheme, Recorder: rec}
	failing.Client = interceptor.NewClient(cl.(client.WithWatch), interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if key.Name == a2aDispatchConfigName(agent) {
				return errors.New("the API server is unavailable")
			}
			return c.Get(ctx, key, obj, opts...)
		},
	})
	if got, err := failing.dispatchDraining(ctx, agent, nil); err == nil {
		t.Errorf("a failed read drained %v with no error; want the pass to fail", got)
	}

	// A body that doesn't decode can't be read on any pass: a Warning, and
	// nothing drains, rather than stop the stack for good.
	cm.Data[a2aDispatchConfigKey] = "{not json"
	if err := cl.Update(ctx, cm); err != nil {
		t.Fatal(err)
	}
	if got, err := r.dispatchDraining(ctx, agent, nil); got != nil || err != nil {
		t.Errorf("an undecodable applied config: draining %v, %v", got, err)
	}
	if events := drainEvents(rec); len(events) != 1 || !strings.Contains(events[0], reasonDispatchConfigUnreadable) {
		t.Errorf("events %q, want one %s warning", events, reasonDispatchConfigUnreadable)
	}
}

// Through the reconcile: an applied dispatch config that lists a profile the
// install no longer has makes both renders keep it draining, the dispatch
// config with no durables and the identity map with its .supervisor grant,
// from the one set read for the pass.
func TestTheReconcileRendersADroppedProfileDrainingInBothPlaces(t *testing.T) {
	withDispatcher(t)
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	ctx := context.Background()
	applied := a2aDispatchConfig{Profiles: []a2aDispatchProfile{{Name: "gone", Concurrency: 1, Tiers: []string{"read"}, ActiveDeadlineSeconds: 600,
		Durables: []a2aDispatchDurable{{Tier: "read", Kind: "top", Name: profileDurableName("gone", "read", "top")}}}}}
	body, _ := json.Marshal(applied)
	if err := cl.Create(ctx, &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: a2aDispatchConfigName(agent), Namespace: agent.Namespace},
		Data: map[string]string{a2aDispatchConfigKey: string(body)}}); err != nil {
		t.Fatal(err)
	}
	reconcileTwice(t, ctx, r, req)

	cm := &corev1.ConfigMap{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}, cm); err != nil {
		t.Fatal(err)
	}
	var rendered a2aDispatchConfig
	if err := json.Unmarshal([]byte(cm.Data[a2aDispatchConfigKey]), &rendered); err != nil {
		t.Fatal(err)
	}
	if i := slices.IndexFunc(rendered.Profiles, func(p a2aDispatchProfile) bool { return p.Name == "gone" }); i < 0 ||
		rendered.Profiles[i].DrainingUntil.IsZero() || len(rendered.Profiles[i].Durables) != 0 {
		t.Fatalf("the reconciled dispatch config does not keep gone draining: %+v", rendered.Profiles)
	}
	shard := &corev1.ConfigMap{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aAuthMapShardName(agent, 0)}, shard); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(shard.Data[a2aAuthMapKey], `"a2a.tasks.gone.*.supervisor"`) {
		t.Errorf("the reconciled identity map dropped gone's .supervisor grant")
	}
	if strings.Contains(shard.Data[a2aAuthMapKey], "profile-gone-") {
		t.Errorf("the reconciled identity map keeps a durable grant of gone")
	}
}
