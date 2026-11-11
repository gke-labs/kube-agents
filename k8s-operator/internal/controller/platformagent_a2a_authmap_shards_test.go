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
	"slices"
	"strconv"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The fleet-scale render: 2,500 bound AgentProfiles, each at two tiers with
// topic grants, which is the shape that put the unsharded map past the
// ConfigMap limit (configMapSizeLimit, declared with the fleet-scale gate in
// platformagent_fleet_scale_test.go, which the unsharded map crossed near 640
// profiles). Every shard stays under the budget, and under the limit by
// a quarter; the identities land in the shards exactly once and in render
// order; every shard carries the whole map's version and stamps that agree
// with its name and its count; and the Role's resourceNames and the callout's
// environment name the same shards, since the callout can watch only what the
// Role lets it and only as many as it is told.
//
// The consumer of these shards is a2a-authcallout, not the gateway: the
// gateway authenticates through the callout like every other bus client and
// never reads the map.
func TestTheIdentityMapShardsStayUnderTheConfigMapLimitAtFleetScale(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	const profileCount = 2500
	profiles := make([]agentv1alpha1.AgentProfile, 0, profileCount)
	for i := 0; i < profileCount; i++ {
		name := fmt.Sprintf("cluster-%04d", i)
		profiles = append(profiles, testAgentProfile(agent.Namespace, name, func(p *agentv1alpha1.AgentProfile) {
			p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
			p.Spec.Bus.PublishTopics = []string{"agent." + name + ".findings", "agent." + name + ".topology"}
			p.Spec.Bus.SubscribeTopics = []string{"shared.blueprint", "shared.annotations"}
		}))
	}

	whole, err := renderA2AAuthMap(agent, profiles)
	if err != nil {
		t.Fatalf("renderA2AAuthMap: %v", err)
	}
	wholeBody, err := encodeA2AJSON(whole, a2aAuthMapIndent)
	if err != nil {
		t.Fatalf("encoding the whole map: %v", err)
	}
	if len(wholeBody) <= configMapSizeLimit {
		t.Fatalf("the unsharded map at %d profiles is %d bytes, under the %d-byte limit; this test would pass without sharding", profileCount, len(wholeBody), configMapSizeLimit)
	}

	cms, version, err := buildA2AAuthMapConfigMaps(agent, profiles, nil)
	if err != nil {
		t.Fatalf("buildA2AAuthMapConfigMaps: %v", err)
	}
	if len(cms) < 2 {
		t.Fatalf("a %d-byte map rendered as %d shard(s); the budget is not cutting", len(wholeBody), len(cms))
	}
	if version != whole.Version {
		t.Errorf("the sharded render's version is %q, the whole map's %q; shards must carry the whole map's version", version, whole.Version)
	}

	var users []string
	for i, cm := range cms {
		body := cm.Data[a2aAuthMapKey]
		if len(body) > a2aAuthMapShardByteBudget {
			t.Errorf("shard %d is %d bytes, over the %d-byte budget", i, len(body), a2aAuthMapShardByteBudget)
		}
		if len(body) >= configMapSizeLimit {
			t.Errorf("shard %d is %d bytes, at or past the ConfigMap limit; the API server refuses it", i, len(body))
		}
		if want := a2aAuthMapShardName(agent, i); cm.Name != want {
			t.Errorf("shard %d is named %q, want %q", i, cm.Name, want)
		}
		var doc a2aAuthMapDocument
		if err := json.Unmarshal([]byte(body), &doc); err != nil {
			t.Fatalf("shard %d does not parse: %v", i, err)
		}
		if doc.Version != version {
			t.Errorf("shard %d carries version %q, want the whole map's %q", i, doc.Version, version)
		}
		if doc.Shard != i || doc.Shards != len(cms) {
			t.Errorf("shard %d is stamped %d of %d, want %d of %d", i, doc.Shard, doc.Shards, i, len(cms))
		}
		if cm.Annotations[a2aAuthMapVersionAnnotation] != version ||
			cm.Annotations[a2aAuthMapShardAnnotation] != strconv.Itoa(i) ||
			cm.Annotations[a2aAuthMapShardsAnnotation] != strconv.Itoa(len(cms)) {
			t.Errorf("shard %d annotations %v disagree with its document (version %s, %d of %d)", i, cm.Annotations, version, i, len(cms))
		}
		if len(doc.Identities) == 0 {
			t.Errorf("shard %d carries no identities; the callout refuses an empty document", i)
		}
		for _, id := range doc.Identities {
			users = append(users, id.User)
		}
	}
	wantUsers := make([]string, 0, len(whole.Identities))
	for _, id := range whole.Identities {
		wantUsers = append(wantUsers, id.User)
	}
	if !slices.Equal(users, wantUsers) {
		t.Errorf("the shards carry %d identities and the whole map %d, or not in render order; every identity must land exactly once", len(users), len(wantUsers))
	}

	// What the callout is allowed to read and told to read: the shards, all
	// of them, and nothing else.
	role := buildA2ACalloutRole(agent, len(cms))
	if len(role.Rules) != 1 || !slices.Equal(role.Rules[0].ResourceNames, a2aAuthMapShardNames(agent, len(cms))) {
		t.Errorf("the callout Role names %v, want every shard %v", role.Rules, a2aAuthMapShardNames(agent, len(cms)))
	}
	env := map[string]string{}
	for _, e := range buildA2ACalloutDeployment(agent, "", len(cms)).Spec.Template.Spec.Containers[0].Env {
		env[e.Name] = e.Value
	}
	if env[a2aCalloutAuthMapShardsEnvVar] != strconv.Itoa(len(cms)) {
		t.Errorf("%s = %q on the callout, want %d", a2aCalloutAuthMapShardsEnvVar, env[a2aCalloutAuthMapShardsEnvVar], len(cms))
	}
	if env[a2aCalloutAuthMapNameEnvVar] != a2aAuthMapName(agent) {
		t.Errorf("%s = %q on the callout, want the shard name stem %q", a2aCalloutAuthMapNameEnvVar, env[a2aCalloutAuthMapNameEnvVar], a2aAuthMapName(agent))
	}
}

// The same inputs cut the same way: a reconcile of an unchanged deployment
// must not move an identity between shards, or every reconcile would rewrite
// two shards and the callout would re-assemble a map that did not change.
func TestTheShardPartitionIsStable(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	profiles := make([]agentv1alpha1.AgentProfile, 0, 1800)
	for i := 0; i < 1800; i++ {
		profiles = append(profiles, testAgentProfile(agent.Namespace, fmt.Sprintf("cluster-%04d", i), func(p *agentv1alpha1.AgentProfile) {
			p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
		}))
	}
	first, _, err := buildA2AAuthMapConfigMaps(agent, profiles, nil)
	if err != nil {
		t.Fatalf("first render: %v", err)
	}
	second, _, err := buildA2AAuthMapConfigMaps(agent, profiles, nil)
	if err != nil {
		t.Fatalf("second render: %v", err)
	}
	if len(first) < 2 {
		t.Fatalf("rendered %d shard(s); stability across one shard proves nothing", len(first))
	}
	if len(first) != len(second) {
		t.Fatalf("two renders of one deployment gave %d and %d shards", len(first), len(second))
	}
	for i := range first {
		if first[i].Data[a2aAuthMapKey] != second[i].Data[a2aAuthMapKey] {
			t.Errorf("shard %d differs between two renders of one deployment", i)
		}
	}
}

// The condition's message names the version of the shard set, read back when
// a pass parked before rendering: shard 0 carries it with the count, and every
// shard the count names agrees with it. A set with a shard missing, or split
// across two versions because a pass failed between two applies, is reported
// as unknown rather than as the version shard 0 carries, which the callout is
// not serving while it waits for the rest.
func TestTheRenderedVersionIsReadOffTheWholeShardSet(t *testing.T) {
	const (
		v1 = "0123456789abcdef"
		v2 = "fedcba9876543210"
	)
	agent := a2aTestAgent()
	shard := func(i int, version, count string) *corev1.ConfigMap {
		return &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
			Name: a2aAuthMapShardName(agent, i), Namespace: agent.Namespace,
			Annotations: map[string]string{
				a2aAuthMapVersionAnnotation: version,
				a2aAuthMapShardAnnotation:   strconv.Itoa(i),
				a2aAuthMapShardsAnnotation:  count,
			},
		}}
	}
	cases := map[string]struct {
		objects []client.Object
		want    string
	}{
		"one shard":                        {[]client.Object{shard(0, v1, "1")}, v1},
		"two shards of one version":        {[]client.Object{shard(0, v1, "2"), shard(1, v1, "2")}, v1},
		"shard 1 not yet applied":          {[]client.Object{shard(0, v1, "2")}, a2aAuthMapVersionUnknown},
		"shard 0 moved on, shard 1 behind": {[]client.Object{shard(0, v2, "2"), shard(1, v1, "2")}, a2aAuthMapVersionUnknown},
		"the count changed under shard 1":  {[]client.Object{shard(0, v1, "2"), shard(1, v1, "3")}, a2aAuthMapVersionUnknown},
		"shard 0 without a count":          {[]client.Object{&corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: a2aAuthMapShardName(agent, 0), Namespace: agent.Namespace, Annotations: map[string]string{a2aAuthMapVersionAnnotation: v1}}}}, a2aAuthMapVersionUnknown},
		"no shard 0":                       {nil, a2aAuthMapVersionUnknown},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			scheme := setupScheme()
			cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent.DeepCopy()}, tc.objects...)...).Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
			if got := r.renderedAuthMapVersion(context.Background(), agent); got != tc.want {
				t.Errorf("renderedAuthMapVersion = %q, want %q", got, tc.want)
			}
		})
	}
}

// After a render the only identity-map objects standing are this pass's
// shards: the unsharded object a release before sharding left, and the shards
// a larger previous render left above this count, are deleted, including one
// stranded above a gap by an interrupted earlier prune; an object under a
// shard name this CR does not own is left alone.
func TestPruningRemovesTheLegacyMapAndTheShardsAboveTheCount(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	ctx := context.Background()
	owned := func(name string) *corev1.ConfigMap {
		cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aLabels(agent, a2aAuthMapComponent)}}
		if err := ctrl.SetControllerReference(agent, cm, scheme); err != nil {
			t.Fatalf("owner reference: %v", err)
		}
		return cm
	}
	squatted := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: a2aAuthMapShardName(agent, 5), Namespace: agent.Namespace, Labels: a2aLabels(agent, a2aAuthMapComponent)}}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent,
		owned(a2aAuthMapName(agent)),
		owned(a2aAuthMapShardName(agent, 0)), owned(a2aAuthMapShardName(agent, 1)),
		owned(a2aAuthMapShardName(agent, 2)), owned(a2aAuthMapShardName(agent, 3)),
		// Shard 4 is gone and 7 stands: what a prune that died between
		// two deletes leaves. A walk up from the count would stop at 4.
		owned(a2aAuthMapShardName(agent, 7)),
		squatted,
	).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.pruneA2AAuthMapShards(ctx, agent, 2); err != nil {
		t.Fatalf("pruneA2AAuthMapShards: %v", err)
	}
	exists := func(name string) bool {
		return cl.Get(ctx, types.NamespacedName{Name: name, Namespace: agent.Namespace}, &corev1.ConfigMap{}) == nil
	}
	for name, want := range map[string]bool{
		a2aAuthMapName(agent):         false,
		a2aAuthMapShardName(agent, 0): true,
		a2aAuthMapShardName(agent, 1): true,
		a2aAuthMapShardName(agent, 2): false,
		a2aAuthMapShardName(agent, 3): false,
		a2aAuthMapShardName(agent, 5): true,
		a2aAuthMapShardName(agent, 7): false,
	} {
		if got := exists(name); got != want {
			t.Errorf("after pruning to 2 shards, %s exists = %v, want %v", name, got, want)
		}
	}

	if err := r.deleteA2AAuthMapShards(ctx, agent); err != nil {
		t.Fatalf("deleteA2AAuthMapShards: %v", err)
	}
	for _, i := range []int{0, 1} {
		if exists(a2aAuthMapShardName(agent, i)) {
			t.Errorf("shard %d survives the teardown", i)
		}
	}
	if !exists(a2aAuthMapShardName(agent, 5)) {
		t.Error("the teardown deleted a ConfigMap under a shard name that this CR does not own")
	}
}
