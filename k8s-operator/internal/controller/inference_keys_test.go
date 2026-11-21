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
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/go-logr/logr/funcr"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// fakeInferenceKeyProber answers through answer, counting the probes.
type fakeInferenceKeyProber struct {
	answer func(host, apiKey string) (int, error)
	probes atomic.Int32
}

func (f *fakeInferenceKeyProber) Probe(_ context.Context, addr, apiKey string) (int, error) {
	f.probes.Add(1)
	host, _, _ := net.SplitHostPort(addr)
	return f.answer(host, apiKey)
}

func admitEverything() *fakeInferenceKeyProber {
	return &fakeInferenceKeyProber{answer: func(string, string) (int, error) { return http.StatusOK, nil }}
}

func inferenceKeysTestAgent() *agentv1alpha1.PlatformAgent {
	agent := a2aTestAgent()
	agent.UID = "agent-uid"
	return agent
}

const (
	litellmTestDeploymentUID = types.UID("uid-litellm-deployment")
	litellmTestReplicaSet    = "litellm-5d8f"
	litellmTestReplicaSetUID = types.UID("uid-litellm-rs")
)

func litellmGatewayDeployment(agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	return &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: litellmDeploymentName, Namespace: agent.Namespace, UID: litellmTestDeploymentUID}}
}

func controllerRef(kind, name string, uid types.UID) metav1.OwnerReference {
	return metav1.OwnerReference{APIVersion: appsv1.SchemeGroupVersion.String(), Kind: kind, Name: name, UID: uid, Controller: ptr.To(true)}
}

// litellmGatewayReplicaSet is the gateway Deployment's ReplicaSet.
func litellmGatewayReplicaSet(agent *agentv1alpha1.PlatformAgent) *appsv1.ReplicaSet {
	return &appsv1.ReplicaSet{ObjectMeta: metav1.ObjectMeta{
		Name: litellmTestReplicaSet, Namespace: agent.Namespace, UID: litellmTestReplicaSetUID,
		OwnerReferences: []metav1.OwnerReference{controllerRef("Deployment", litellmDeploymentName, litellmTestDeploymentUID)},
	}}
}

// litellmGatewayPod is a Ready pod of the gateway's ReplicaSet.
func litellmGatewayPod(agent *agentv1alpha1.PlatformAgent, name, ip string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: agent.Namespace, Labels: litellmPodSelector(), UID: types.UID("uid-" + name),
			OwnerReferences: []metav1.OwnerReference{controllerRef("ReplicaSet", litellmTestReplicaSet, litellmTestReplicaSetUID)},
		},
		Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: litellmContainerName}}},
		Status: corev1.PodStatus{
			Phase:             corev1.PodRunning,
			PodIP:             ip,
			ContainerStatuses: []corev1.ContainerStatus{{Name: litellmContainerName, Ready: true}},
		},
	}
}

// platformAgentCRDObject is the served PlatformAgent CRD as the operator
// reads it, its v1alpha1 schema with or without status.inferenceKeys.
func platformAgentCRDObject(withField bool) *unstructured.Unstructured {
	statusProps := map[string]any{"phase": map[string]any{"type": "string"}}
	if withField {
		statusProps["inferenceKeys"] = map[string]any{"type": "object"}
	}
	crd := &unstructured.Unstructured{Object: map[string]any{
		"spec": map[string]any{"versions": []any{map[string]any{
			"name": agentv1alpha1.GroupVersion.Version,
			"schema": map[string]any{"openAPIV3Schema": map[string]any{"properties": map[string]any{
				"status": map[string]any{"properties": statusProps},
			}}},
		}}},
	}}
	crd.SetGroupVersionKind(crdGVK)
	crd.SetName(servedPlatformAgentCRDName)
	return crd
}

// inferenceKeysFixture is a reconciler over an agent whose namespace runs
// the gateway with two Ready pods, a profile namespace, and a profile at
// the read and unattended tiers.
func inferenceKeysFixture(t *testing.T, extra ...client.Object) (*PlatformAgentReconciler, client.Client, *agentv1alpha1.PlatformAgent) {
	t.Helper()
	agent := inferenceKeysTestAgent()
	profile := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	})
	objs := append([]client.Object{
		platformAgentCRDObject(true),
		litellmGatewayDeployment(agent),
		litellmGatewayReplicaSet(agent),
		litellmGatewayPod(agent, "litellm-a", "10.1.0.1"),
		litellmGatewayPod(agent, "litellm-b", "10.1.0.2"),
		profileNamespaceObject(agent),
		&profile,
	}, extra...)
	r, cl, _ := a2aGateTestReconciler(t, agent, objs...)
	r.inferenceKeyProbe = admitEverything()
	return r, cl, agent
}

func readInferenceKey(t *testing.T, cl client.Client, key types.NamespacedName) *corev1.Secret {
	t.Helper()
	s := &corev1.Secret{}
	if err := cl.Get(context.Background(), key, s); err != nil {
		t.Fatalf("Secret %s: %v", key, err)
	}
	return s
}

func readCallerKeysMap(t *testing.T, cl client.Client, agent *agentv1alpha1.PlatformAgent) (map[string]string, []byte) {
	t.Helper()
	s := readInferenceKey(t, cl, types.NamespacedName{Namespace: agent.Namespace, Name: callerKeysSecretName})
	doc := s.Data[callerKeysFileKey]
	var entries map[string]string
	if err := json.Unmarshal(doc, &entries); err != nil {
		t.Fatalf("keys.json does not parse: %v", err)
	}
	return entries, doc
}

// allInferenceKeys is every class's key, the probe key included, read from
// its Secret.
func allInferenceKeys(t *testing.T, cl client.Client, agent *agentv1alpha1.PlatformAgent, tiers ...string) map[string]string {
	t.Helper()
	out := map[string]string{}
	for _, class := range append(append([]string(nil), installInferenceKeyClasses...), inferenceKeyClassProbe) {
		c := installInferenceKeyClass(agent, class)
		out[class] = string(readInferenceKey(t, cl, c.secret).Data[inferenceKeyDataKey])
	}
	for _, tier := range tiers {
		c := tierInferenceKeyClass(agent, tier)
		out[c.name] = string(readInferenceKey(t, cl, c.secret).Data[inferenceKeyDataKey])
	}
	return out
}

// Each class gets one key, created once: the install-namespace Secrets are
// controlled by the PlatformAgent, the tiers' carry its labels in the
// profile namespace, and a second pass rewrites nothing.
func TestInferenceKeysAreMintedOnceAndNeverRegenerated(t *testing.T) {
	r, cl, agent := inferenceKeysFixture(t)
	rec := record.NewFakeRecorder(32)
	r.Recorder = rec
	ctx := context.Background()

	out := r.reconcileInferenceKeys(ctx, agent)
	if out.condition == nil || out.condition.Status != metav1.ConditionTrue || out.condition.Reason != inferenceKeysReasonServed {
		t.Fatalf("first pass: %+v", out.condition)
	}
	first := allInferenceKeys(t, cl, agent, "read", "unattended")
	for class, apiKey := range first {
		if !wellFormedInferenceKey(apiKey) {
			t.Errorf("class %s holds a malformed key", class)
		}
	}
	if len(first) != 6 {
		t.Fatalf("want 5 caller classes and the probe key, got %d", len(first))
	}
	seen := map[string]bool{}
	for _, apiKey := range first {
		if seen[apiKey] {
			t.Fatal("two classes share one key")
		}
		seen[apiKey] = true
	}
	for _, class := range installInferenceKeyClasses {
		s := readInferenceKey(t, cl, installInferenceKeyClass(agent, class).secret)
		if !metav1.IsControlledBy(s, agent) || s.Labels[inferenceKeyClassLabel] != class || s.Labels[a2aComponentLabel] != inferenceKeyComponent {
			t.Errorf("class %s: owner %v, labels %v", class, s.OwnerReferences, s.Labels)
		}
	}
	probe := readInferenceKey(t, cl, types.NamespacedName{Namespace: agent.Namespace, Name: "test-agent-inference-key-gateway-probe"})
	if !metav1.IsControlledBy(probe, agent) || !wellFormedInferenceKey(string(probe.Data["api-key"])) {
		t.Errorf("the probe key: owners %v", probe.OwnerReferences)
	}
	session := readInferenceKey(t, cl, types.NamespacedName{Namespace: agent.Namespace, Name: "test-agent-inference-key-a2a-session"})
	if _, ok := session.Data["api-key"]; !ok {
		t.Error("the session key isn't at the name and data key the gateway pod policy pins")
	}
	for _, tier := range []string{"read", "unattended"} {
		c := tierInferenceKeyClass(agent, tier)
		s := readInferenceKey(t, cl, c.secret)
		if len(s.OwnerReferences) != 0 || !renderedByAgent(s, agent, inferenceKeyComponent) || s.Namespace != "test-ns-profiles" {
			t.Errorf("tier %s: namespace %s, owners %v, labels %v", tier, s.Namespace, s.OwnerReferences, s.Labels)
		}
	}
	if n := len(drainEvents(rec)); n != 6 {
		t.Errorf("%d minted events, want 6", n)
	}

	r.reconcileInferenceKeys(ctx, agent)
	if second := allInferenceKeys(t, cl, agent, "read", "unattended"); !mapsEqual(first, second) {
		t.Error("a second pass changed a key")
	}
	if n := len(drainEvents(rec)); n != 0 {
		t.Errorf("a second pass wrote %d events", n)
	}
}

func mapsEqual(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}

// A key Secret this agent made that holds no well-formed key is repaired
// in place; a Secret at a key's name this agent didn't make is replaced.
func TestAMalformedKeyIsRepairedAndAForeignOneIsReplaced(t *testing.T) {
	agent := inferenceKeysTestAgent()
	foreign := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: inferenceKeySecretName(agent, inferenceKeyClassCurator), Namespace: agent.Namespace, UID: "uid-foreign"},
		Data:       map[string][]byte{inferenceKeyDataKey: []byte("someone-elses-value")},
	}
	r, cl, agent := inferenceKeysFixture(t, foreign)
	rec := record.NewFakeRecorder(64)
	r.Recorder = rec
	ctx := context.Background()
	r.reconcileInferenceKeys(ctx, agent)
	assertReplaced(t, cl, rec, agent, foreign, "someone-elses-value")

	agentKey := installInferenceKeyClass(agent, inferenceKeyClassAgent).secret
	s := readInferenceKey(t, cl, agentKey)
	uid := s.UID
	s.Data[inferenceKeyDataKey] = []byte("sk-short")
	if err := cl.Update(ctx, s); err != nil {
		t.Fatal(err)
	}
	r.reconcileInferenceKeys(ctx, agent)
	got := readInferenceKey(t, cl, agentKey)
	if !wellFormedInferenceKey(string(got.Data[inferenceKeyDataKey])) || got.UID != uid {
		t.Error("a malformed key of the operator's own was not repaired in place")
	}
}

// assertReplaced checks that planted, a Secret at a reserved name, was
// replaced: the Secret there now has another UID, the one the status
// records; plantedValue's digest is not in the map; and a Warning Event
// names the Secret without carrying the value.
func assertReplaced(t *testing.T, cl client.Client, rec *record.FakeRecorder, agent *agentv1alpha1.PlatformAgent, planted *corev1.Secret, plantedValue string) {
	t.Helper()
	ctx := context.Background()
	now := readInferenceKey(t, cl, client.ObjectKeyFromObject(planted))
	if now.UID == planted.UID {
		t.Errorf("Secret %s/%s was adopted, not replaced", planted.Namespace, planted.Name)
	}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatal(err)
	}
	if recordedSecretUIDs(stored)[planted.Name] != now.UID {
		t.Errorf("the record for %s isn't the replacement's UID", planted.Name)
	}
	entries, doc := readCallerKeysMap(t, cl, agent)
	if _, ok := entries[inferenceKeyDigest(plantedValue)]; ok || strings.Contains(string(doc), plantedValue) {
		t.Errorf("the planted value of %s reached the map", planted.Name)
	}
	want := planted.Namespace + "/" + planted.Name
	warned := false
	for _, e := range drainEvents(rec) {
		if strings.Contains(e, plantedValue) {
			t.Errorf("an Event carries the planted value: %s", e)
		}
		if strings.HasPrefix(e, corev1.EventTypeWarning) && strings.Contains(e, reasonInferenceKeySecretReplaced) && strings.Contains(e, want) {
			warned = true
		}
	}
	if !warned {
		t.Errorf("no %s Warning Event names %s", reasonInferenceKeySecretReplaced, want)
	}
}

// keys.json: a sha256 per key, lowercase hex, to its class, in the form the
// gateway's hook takes; the same bytes on every pass; and no key in it.
func TestTheCallerKeyMapHoldsDigestsOnly(t *testing.T) {
	r, cl, agent := inferenceKeysFixture(t)
	ctx := context.Background()
	r.reconcileInferenceKeys(ctx, agent)
	keys := allInferenceKeys(t, cl, agent, "read", "unattended")
	entries, doc := readCallerKeysMap(t, cl, agent)
	if len(entries) != len(keys) {
		t.Fatalf("map has %d entries for %d keys", len(entries), len(keys))
	}
	for digest, class := range entries {
		if !keyDigestPattern.MatchString(digest) || !validCallerClass(class) {
			t.Errorf("entry %q -> %q is not a digest and a class the gateway takes", digest, class)
		}
		if inferenceKeyDigest(keys[class]) != digest {
			t.Errorf("class %s's digest isn't the sha256 of its key", class)
		}
	}
	for class, apiKey := range keys {
		if strings.Contains(string(doc), apiKey) || strings.Contains(string(doc), strings.TrimPrefix(apiKey, inferenceKeyPrefix)) {
			t.Errorf("the map carries the %s key", class)
		}
	}
	m := readInferenceKey(t, cl, types.NamespacedName{Namespace: agent.Namespace, Name: callerKeysSecretName})
	if !metav1.IsControlledBy(m, agent) {
		t.Error("the map isn't controlled by the PlatformAgent")
	}
	r.inferenceKeysServedFor.Delete(client.ObjectKeyFromObject(agent))
	r.reconcileInferenceKeys(ctx, agent)
	if _, again := readCallerKeysMap(t, cl, agent); string(again) != string(doc) {
		t.Error("a second pass wrote different bytes")
	}
	if got, err := callerKeysDocument(map[string]string{"b": "y", "a": "x"}); err != nil || string(got) != `{"a":"x","b":"y"}` {
		t.Errorf("the document isn't sorted: %s", got)
	}
}

// No gateway Deployment, or another PlatformAgent in the namespace, or a
// map Secret this agent didn't write: no key is minted and no map written.
func TestNoKeysWithoutTheirOnlyGatewayAndAgent(t *testing.T) {
	ctx := context.Background()
	t.Run("no gateway", func(t *testing.T) {
		agent := inferenceKeysTestAgent()
		r, cl, _ := a2aGateTestReconciler(t, agent)
		out := r.reconcileInferenceKeys(ctx, agent)
		if out.condition != inferenceKeysRemoved {
			t.Errorf("condition %+v, want removed", out.condition)
		}
		assertNoInferenceKeys(t, cl, agent)
	})
	t.Run("two PlatformAgents", func(t *testing.T) {
		agent := inferenceKeysTestAgent()
		other := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "other", Namespace: agent.Namespace}}
		r, cl, _ := inferenceKeysFixture(t, other)
		out := r.reconcileInferenceKeys(ctx, agent)
		if out.condition == nil || out.condition.Reason != inferenceKeysReasonNotWritten || !strings.Contains(out.condition.Message, "2 PlatformAgents") {
			t.Errorf("condition %+v", out.condition)
		}
		assertNoInferenceKeys(t, cl, agent)
	})
	t.Run("a foreign map", func(t *testing.T) {
		agent := inferenceKeysTestAgent()
		handMade := &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{Name: callerKeysSecretName, Namespace: agent.Namespace, UID: "uid-hand-made"},
			Data:       map[string][]byte{callerKeysFileKey: []byte(`{"` + strings.Repeat("1", 64) + `":"agent"}`)},
		}
		r, cl, agent := inferenceKeysFixture(t, handMade)
		rec := record.NewFakeRecorder(64)
		r.Recorder = rec
		if out := r.reconcileInferenceKeys(ctx, agent); out.condition.Status != metav1.ConditionTrue {
			t.Errorf("condition %+v", out.condition)
		}
		assertReplaced(t, cl, rec, agent, handMade, strings.Repeat("1", 64))
	})
}

func assertNoInferenceKeys(t *testing.T, cl client.Client, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	names := []types.NamespacedName{{Namespace: agent.Namespace, Name: callerKeysSecretName}}
	for _, class := range installInferenceKeyClasses {
		names = append(names, installInferenceKeyClass(agent, class).secret)
	}
	for _, name := range names {
		if err := cl.Get(context.Background(), name, &corev1.Secret{}); !apierrors.IsNotFound(err) {
			t.Errorf("Secret %s: %v, want absent", name, err)
		}
	}
}

// The probe gate: True only when every Ready gateway pod answers 200 to
// the probe key. One pod refusing it is False and names the pod, never the
// key; a pod that doesn't answer is Unknown; both look again. No Ready pod
// is Unknown. A pass with the same map and pods as the last Served one
// sends nothing. No caller key is ever sent, only the probe key.
func TestTheProbeGateNeedsEveryPodToAdmitTheProbeKey(t *testing.T) {
	ctx := context.Background()
	t.Run("all admit", func(t *testing.T) {
		r, cl, agent := inferenceKeysFixture(t)
		var mu sync.Mutex
		var sent []string
		prober := &fakeInferenceKeyProber{answer: func(_, apiKey string) (int, error) {
			mu.Lock()
			sent = append(sent, apiKey)
			mu.Unlock()
			return http.StatusOK, nil
		}}
		r.inferenceKeyProbe = prober
		out := r.reconcileInferenceKeys(ctx, agent)
		// The Served pass marks the keys and asks for the pass that renders
		// their callers (inference_key_callers.go).
		if out.condition.Status != metav1.ConditionTrue || out.requeue != inferenceKeysMarkedRecheck {
			t.Fatalf("outcome %+v", out)
		}
		if got := prober.probes.Load(); got != 2 {
			t.Errorf("%d probes, want one per pod (2)", got)
		}
		keys := allInferenceKeys(t, cl, agent, "read", "unattended")
		for _, apiKey := range sent {
			for class, k := range keys {
				if k == apiKey && class != inferenceKeyClassProbe {
					t.Errorf("a probe sent the %s caller key", class)
				}
			}
			if apiKey != keys[inferenceKeyClassProbe] {
				t.Error("a probe sent something other than the probe key")
			}
		}
		r.reconcileInferenceKeys(ctx, agent)
		if got := prober.probes.Load(); got != 2 {
			t.Errorf("an unchanged pass probed again (%d probes)", got)
		}
	})
	t.Run("one pod refuses the probe key", func(t *testing.T) {
		r, cl, agent := inferenceKeysFixture(t)
		r.reconcileInferenceKeys(ctx, agent)
		probeKey := allInferenceKeys(t, cl, agent)[inferenceKeyClassProbe]
		r.inferenceKeyProbe = &fakeInferenceKeyProber{answer: func(host, apiKey string) (int, error) {
			if host == "10.1.0.2" && apiKey == probeKey {
				return http.StatusUnauthorized, nil
			}
			return http.StatusOK, nil
		}}
		r.inferenceKeysServedFor.Delete(client.ObjectKeyFromObject(agent))
		out := r.reconcileInferenceKeys(ctx, agent)
		c := out.condition
		if c.Status != metav1.ConditionFalse || c.Reason != inferenceKeysReasonNotServed || out.requeue != inferenceKeysRecheck {
			t.Fatalf("outcome %+v", out)
		}
		if !strings.Contains(c.Message, "litellm-b") || !strings.Contains(c.Message, inferenceKeyClassProbe) || !strings.Contains(c.Message, "401") {
			t.Errorf("message %q doesn't name the pod, the probe class and the status", c.Message)
		}
		if strings.Contains(c.Message, probeKey) || strings.Contains(c.Message, inferenceKeyDigest(probeKey)) {
			t.Error("the message carries the key or its digest")
		}
	})
	t.Run("a pod doesn't answer", func(t *testing.T) {
		r, _, agent := inferenceKeysFixture(t)
		r.inferenceKeyProbe = &fakeInferenceKeyProber{answer: func(host, _ string) (int, error) {
			if host == "10.1.0.1" {
				return 0, errors.New(usageScrapeKindConnect)
			}
			return http.StatusOK, nil
		}}
		out := r.reconcileInferenceKeys(ctx, agent)
		if out.condition.Status != metav1.ConditionUnknown || out.condition.Reason != inferenceKeysReasonProbeFailed || out.requeue != inferenceKeysUnreachableRecheck {
			t.Errorf("outcome %+v", out)
		}
	})
	t.Run("no Ready pod", func(t *testing.T) {
		agent := inferenceKeysTestAgent()
		notReady := litellmGatewayPod(agent, "litellm-a", "10.1.0.1")
		notReady.Status.ContainerStatuses[0].Ready = false
		shadow := litellmGatewayPod(agent, "replay-shadow", "10.1.0.9")
		shadow.Spec.Containers[0].Name = "replay-proxy"
		shadow.Status.ContainerStatuses[0].Name = "replay-proxy"
		r, _, _ := a2aGateTestReconciler(t, agent, platformAgentCRDObject(true), litellmGatewayDeployment(agent), litellmGatewayReplicaSet(agent), notReady, shadow)
		prober := admitEverything()
		r.inferenceKeyProbe = prober
		out := r.reconcileInferenceKeys(ctx, agent)
		if out.condition.Status != metav1.ConditionUnknown || out.condition.Reason != inferenceKeysReasonNoReadyGateway || prober.probes.Load() != 0 ||
			out.requeue != inferenceKeysUnreachableRecheck {
			t.Errorf("outcome %+v after %d probes", out, prober.probes.Load())
		}
	})
}

// The HTTP prober sends the key as a bearer token to /v1/models and reports
// the status; a redirect is the answer, not followed, so the key goes
// nowhere else.
func TestTheHTTPProberSendsTheKeyToThePodAlone(t *testing.T) {
	var mu sync.Mutex
	var gotAuth, gotPath string
	var elsewhere atomic.Bool
	other := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { elsewhere.Store(true) }))
	defer other.Close()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		gotAuth, gotPath = r.Header.Get("Authorization"), r.URL.Path
		mu.Unlock()
		if r.Header.Get("Authorization") == "Bearer sk-redirect" {
			http.Redirect(w, r, other.URL, http.StatusFound)
			return
		}
		if r.Header.Get("Authorization") != "Bearer sk-good" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		_, _ = w.Write([]byte(`{"data":[]}`))
	}))
	defer srv.Close()
	addr := strings.TrimPrefix(srv.URL, "http://")
	p := newHTTPInferenceKeyProber()
	ctx := context.Background()
	if status, err := p.Probe(ctx, addr, "sk-good"); err != nil || status != http.StatusOK {
		t.Fatalf("good key: %d, %v", status, err)
	}
	mu.Lock()
	if gotAuth != "Bearer sk-good" || gotPath != inferenceKeyProbePath {
		t.Errorf("sent %q to %q", gotAuth, gotPath)
	}
	mu.Unlock()
	if status, err := p.Probe(ctx, addr, "sk-bad"); err != nil || status != http.StatusUnauthorized {
		t.Errorf("bad key: %d, %v", status, err)
	}
	if status, err := p.Probe(ctx, addr, "sk-redirect"); err != nil || status != http.StatusFound || elsewhere.Load() {
		t.Errorf("redirect: %d, %v, followed %v", status, err, elsewhere.Load())
	}
	srv.Close()
	if _, err := p.Probe(ctx, addr, "sk-good"); err == nil || strings.Contains(err.Error(), "sk-good") {
		t.Errorf("a closed listener: %v", err)
	}
}

// A tier no bound profile lists any more loses its key, after the map
// stops naming it; the PlatformAgent's deletion removes every tier key,
// which no owner reference would.
func TestATiersKeyGoesWithTheTierAndTheAgent(t *testing.T) {
	r, cl, agent := inferenceKeysFixture(t)
	ctx := context.Background()
	r.reconcileInferenceKeys(ctx, agent)
	unattended := tierInferenceKeyClass(agent, "unattended").secret
	readTier := tierInferenceKeyClass(agent, "read").secret

	profile := &agentv1alpha1.AgentProfile{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: "auditor"}, profile); err != nil {
		t.Fatal(err)
	}
	profile.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead}
	if err := cl.Update(ctx, profile); err != nil {
		t.Fatal(err)
	}
	r.reconcileInferenceKeys(ctx, agent)
	if err := cl.Get(ctx, unattended, &corev1.Secret{}); !apierrors.IsNotFound(err) {
		t.Errorf("the dropped tier's key survived: %v", err)
	}
	entries, _ := readCallerKeysMap(t, cl, agent)
	for _, class := range entries {
		if class == "tier-unattended" {
			t.Error("the map still names the dropped tier")
		}
	}
	if err := cl.Get(ctx, readTier, &corev1.Secret{}); err != nil {
		t.Errorf("the live tier's key went: %v", err)
	}

	r.removeInferenceKeysOnDeletion(ctx, agent)
	if err := cl.Get(ctx, readTier, &corev1.Secret{}); !apierrors.IsNotFound(err) {
		t.Errorf("a tier key survived the deletion: %v", err)
	}
}

// A tier key of the same name that this agent didn't label as its own is
// left alone on deletion.
func TestAForeignTierSecretSurvivesTheDeletion(t *testing.T) {
	agent := inferenceKeysTestAgent()
	foreign := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
		Name: inferenceKeySecretName(agent, "tier-read"), Namespace: profileNamespaceName(agent),
	}}
	r, cl, _ := a2aGateTestReconciler(t, agent, foreign)
	r.removeInferenceKeysOnDeletion(context.Background(), agent)
	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(foreign), &corev1.Secret{}); err != nil {
		t.Errorf("a foreign Secret was deleted: %v", err)
	}
}

// Through the reconcile: the condition lands on the status, the requeue
// follows a refusal, and no key or digest reaches a log line, an Event or
// a condition.
func TestNoKeyReachesTheLogsEventsOrConditions(t *testing.T) {
	agent := inferenceKeysTestAgent()
	profile := testAgentProfile(agent.Namespace, "auditor")
	r, cl, req := a2aGateTestReconciler(t, agent,
		platformAgentCRDObject(true), litellmGatewayDeployment(agent), litellmGatewayReplicaSet(agent), litellmGatewayPod(agent, "litellm-a", "10.1.0.1"), profileNamespaceObject(agent), &profile)
	rec := record.NewFakeRecorder(256)
	r.Recorder = rec
	r.inferenceKeyProbe = &fakeInferenceKeyProber{answer: func(string, string) (int, error) { return http.StatusUnauthorized, nil }}
	var logs strings.Builder
	var logMu sync.Mutex
	ctx := logf.IntoContext(context.Background(), funcr.New(func(prefix, args string) {
		logMu.Lock()
		defer logMu.Unlock()
		logs.WriteString(prefix + " " + args + "\n")
	}, funcr.Options{Verbosity: 10}))

	reconcileTwice(t, ctx, r, req)
	result, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if result.RequeueAfter > inferenceKeysRecheck {
		t.Errorf("requeue %s while a pod refuses a key, want at most %s", result.RequeueAfter, inferenceKeysRecheck)
	}
	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
		t.Fatal(err)
	}
	c := meta.FindStatusCondition(got.Status.Conditions, inferenceKeysServedCondition)
	if c == nil || c.Status != metav1.ConditionFalse || c.Reason != inferenceKeysReasonNotServed {
		t.Fatalf("condition %+v, want False/%s", c, inferenceKeysReasonNotServed)
	}

	keys := allInferenceKeys(t, cl, agent, "read")
	var conditions []string
	for _, cond := range got.Status.Conditions {
		conditions = append(conditions, cond.Message)
	}
	surfaces := map[string]string{
		"logs":       logs.String(),
		"events":     strings.Join(drainEvents(rec), "\n"),
		"conditions": strings.Join(conditions, "\n"),
	}
	if !strings.Contains(surfaces["logs"], "inference gateway caller keys") {
		t.Error("the log capture saw nothing of this reconcile step")
	}
	for class, apiKey := range keys {
		for surface, text := range surfaces {
			if strings.Contains(text, strings.TrimPrefix(apiKey, inferenceKeyPrefix)) {
				t.Errorf("the %s key reached the %s", class, surface)
			}
			if strings.Contains(text, inferenceKeyDigest(apiKey)) {
				t.Errorf("the %s key's digest reached the %s", class, surface)
			}
		}
	}

	// The gateway leaves: the condition goes with it.
	if err := cl.Delete(ctx, litellmGatewayDeployment(agent)); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
		t.Fatal(err)
	}
	if c := meta.FindStatusCondition(got.Status.Conditions, inferenceKeysServedCondition); c != nil {
		t.Errorf("the condition outlived the gateway: %+v", c)
	}
}

// The key form: sk- and 32 random bytes, base64url without padding, and a
// digest the hook would compute (sha256 of the key's bytes, lowercase hex).
func TestTheKeyFormAndItsDigest(t *testing.T) {
	apiKey, err := newInferenceKey()
	if err != nil {
		t.Fatal(err)
	}
	if !regexp.MustCompile(`^sk-[A-Za-z0-9_-]{43}$`).MatchString(apiKey) || !wellFormedInferenceKey(apiKey) {
		t.Errorf("key form %d chars, prefix %q", len(apiKey), apiKey[:3])
	}
	for _, bad := range []string{"", "sk-", "none", "sk-" + strings.Repeat("A", 42), "pk-" + apiKey[3:], apiKey + "="} {
		if wellFormedInferenceKey(bad) {
			t.Errorf("%q passed as well-formed", bad)
		}
	}
	// sha256("sk-test"), as hashlib computes it.
	if got := inferenceKeyDigest("sk-test"); got != "f3abf2a6cc4f00987743db5f544ba345b4899ae31f326d8ee9c4816de153c9e0" || !keyDigestPattern.MatchString(got) {
		t.Errorf("digest %q", got)
	}
	for _, class := range []string{"agent", "a2a-session", "curator", "tier-read", "tier-unattended"} {
		if !validCallerClass(class) {
			t.Errorf("class %q fails the gateway's pattern", class)
		}
	}
	for _, class := range []string{"keyless", "Tier-read", "tier_read", "-x", strings.Repeat("a", 64)} {
		if validCallerClass(class) {
			t.Errorf("class %q passed", class)
		}
	}
}

// The operator's copy of the hook's rules matches the hook: the class
// pattern, the digest pattern, the reserved class and the map's file name.
// The hook ships with the chart; on a tree without it there is nothing to
// compare against.
func TestTheMapRulesMatchTheGatewayHook(t *testing.T) {
	hookPath := filepath.Join("..", "..", "..", "charts", "kube-agents", "files", "litellm_caller_auth.py")
	src, err := os.ReadFile(hookPath)
	if errors.Is(err, os.ErrNotExist) {
		t.Skipf("%s is not in this tree", hookPath)
	}
	if err != nil {
		t.Fatal(err)
	}
	find := func(re string) string {
		t.Helper()
		m := regexp.MustCompile(re).FindSubmatch(src)
		if m == nil {
			t.Fatalf("the hook has no match for %s", re)
		}
		return string(m[1])
	}
	if got := "^" + find(`CLASS_NAME_PATTERN = re\.compile\(r"([^"]+)"\)`) + "$"; got != callerClassPattern.String() {
		t.Errorf("class pattern: hook %q, operator %q", got, callerClassPattern.String())
	}
	if got := "^" + find(`KEY_DIGEST_PATTERN = re\.compile\(r"([^"]+)"\)`) + "$"; got != keyDigestPattern.String() {
		t.Errorf("digest pattern: hook %q, operator %q", got, keyDigestPattern.String())
	}
	if got := find(`KEYLESS_CLASS = "([^"]+)"`); !slices.Contains(reservedCallerClasses, got) {
		t.Errorf("the hook's keyless class %q isn't reserved here", got)
	}
	if got := find(`PROBE_CLASS = "([^"]+)"`); got != inferenceKeyClassProbe || !validCallerClass(got) {
		t.Errorf("probe class: hook %q, operator %q", got, inferenceKeyClassProbe)
	}
	if got := find(`PROBE_ROUTE = "([^"]+)"`); got != inferenceKeyProbePath {
		t.Errorf("probe route: hook %q, operator %q", got, inferenceKeyProbePath)
	}
	if got := find(`PROBE_METHOD = "([^"]+)"`); got != http.MethodGet {
		t.Errorf("probe method: hook %q", got)
	}
	if got := filepath.Base(find(`DEFAULT_CALLER_KEYS_FILE = "([^"]+)"`)); got != callerKeysFileKey {
		t.Errorf("map file: hook %q, operator %q", got, callerKeysFileKey)
	}
}

// Only the litellm Deployment's own pods get a probe, and so a key: a pod
// with the gateway's labels and container but no owner, or owned by a
// ReplicaSet the Deployment doesn't control, or on the host network, is
// never sent one.
func TestAPodThatOnlyLooksLikeTheGatewayGetsNoKey(t *testing.T) {
	agent := inferenceKeysTestAgent()
	unowned := litellmGatewayPod(agent, "lookalike-unowned", "10.2.0.1")
	unowned.OwnerReferences = nil
	strayRS := &appsv1.ReplicaSet{ObjectMeta: metav1.ObjectMeta{Name: "stray", Namespace: agent.Namespace, UID: "uid-stray"}}
	strayOwned := litellmGatewayPod(agent, "lookalike-stray", "10.2.0.2")
	strayOwned.OwnerReferences = []metav1.OwnerReference{controllerRef("ReplicaSet", "stray", "uid-stray")}
	wrongUID := litellmGatewayPod(agent, "lookalike-uid", "10.2.0.3")
	wrongUID.OwnerReferences = []metav1.OwnerReference{controllerRef("ReplicaSet", litellmTestReplicaSet, "uid-another")}
	hostNet := litellmGatewayPod(agent, "lookalike-hostnet", "10.2.0.4")
	hostNet.Spec.HostNetwork = true
	r, _, agent := inferenceKeysFixture(t, unowned, strayRS, strayOwned, wrongUID, hostNet)
	var mu sync.Mutex
	asked := map[string]bool{}
	r.inferenceKeyProbe = &fakeInferenceKeyProber{answer: func(host, _ string) (int, error) {
		mu.Lock()
		asked[host] = true
		mu.Unlock()
		return http.StatusOK, nil
	}}
	out := r.reconcileInferenceKeys(context.Background(), agent)
	if out.condition.Status != metav1.ConditionTrue || !strings.Contains(out.condition.Message, "(2)") {
		t.Errorf("outcome %+v, want Served over the two gateway pods", out)
	}
	for _, ip := range []string{"10.2.0.1", "10.2.0.2", "10.2.0.3", "10.2.0.4"} {
		if asked[ip] {
			t.Errorf("look-alike pod %s was sent a key", ip)
		}
	}
	if !asked["10.1.0.1"] || !asked["10.1.0.2"] {
		t.Errorf("the gateway's own pods weren't probed: %v", asked)
	}
}

// A Secret planted in the profile namespace at a tier key's name, with this
// agent's labels, is replaced, not adopted: its key never reaches the map,
// and a Warning Event names it. A tier Secret the operator created survives
// later passes. A labelled Secret at a tier's name that the record doesn't
// hold is not deleted by the tier cleanup or the PlatformAgent's deletion.
func TestAPlantedTierSecretIsReplacedNotAdopted(t *testing.T) {
	agent := inferenceKeysTestAgent()
	labelled := func(class string) map[string]string {
		l := a2aLabels(agent, inferenceKeyComponent)
		l[inferenceKeyClassLabel] = class
		return l
	}
	plantedKey := "sk-" + strings.Repeat("A", 43)
	planted := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name: inferenceKeySecretName(agent, "tier-unattended"), Namespace: profileNamespaceName(agent), UID: "uid-planted",
			Labels: labelled("tier-unattended"),
		},
		Data: map[string][]byte{inferenceKeyDataKey: []byte(plantedKey)},
	}
	// A tier no profile lists: outside the cleanup's reach, since the record
	// doesn't hold it.
	idle := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name: inferenceKeySecretName(agent, "tier-idle"), Namespace: profileNamespaceName(agent), UID: "uid-idle",
			Labels: labelled("tier-idle"),
		},
	}
	r, cl, agent := inferenceKeysFixture(t, planted, idle)
	rec := record.NewFakeRecorder(64)
	r.Recorder = rec
	ctx := context.Background()
	if out := r.reconcileInferenceKeys(ctx, agent); out.condition.Status != metav1.ConditionTrue {
		t.Fatalf("condition %+v", out.condition)
	}
	assertReplaced(t, cl, rec, agent, planted, plantedKey)

	readTier := tierInferenceKeyClass(agent, "read").secret
	before := readInferenceKey(t, cl, readTier)
	r.reconcileInferenceKeys(ctx, agent)
	r.reconcileInferenceKeys(ctx, agent)
	after := readInferenceKey(t, cl, readTier)
	if after.UID != before.UID || string(after.Data[inferenceKeyDataKey]) != string(before.Data[inferenceKeyDataKey]) {
		t.Error("the operator's own tier key didn't survive later passes")
	}
	for _, e := range drainEvents(rec) {
		if strings.Contains(e, reasonInferenceKeySecretReplaced) {
			t.Errorf("a later pass replaced something: %s", e)
		}
	}

	r.removeInferenceKeysOnDeletion(ctx, agent)
	if err := cl.Get(ctx, client.ObjectKeyFromObject(idle), &corev1.Secret{}); err != nil {
		t.Errorf("an unrecorded Secret was deleted by the cleanup: %v", err)
	}
	if err := cl.Get(ctx, readTier, &corev1.Secret{}); !apierrors.IsNotFound(err) {
		t.Errorf("the operator's own tier key survived the deletion: %v", err)
	}
}

// A Secret that names the PlatformAgent as its controller is still not the
// operator's: whoever creates a Secret writes its owner references. A
// pre-created map or class key Secret with a forged controller reference is
// replaced, and its contents never reach the map.
func TestAForgedControllerReferenceIsReplaced(t *testing.T) {
	ctx := context.Background()
	forge := func(agent *agentv1alpha1.PlatformAgent, name string, data map[string][]byte) *corev1.Secret {
		return &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{
				Name: name, Namespace: agent.Namespace, UID: types.UID("uid-forged-" + name),
				Labels: a2aLabels(agent, inferenceKeyComponent),
				OwnerReferences: []metav1.OwnerReference{{
					APIVersion: agentv1alpha1.GroupVersion.String(), Kind: "PlatformAgent", Name: agent.Name, UID: agent.UID, Controller: ptr.To(true),
				}},
			},
			Data: data,
		}
	}
	t.Run("the map", func(t *testing.T) {
		agent := inferenceKeysTestAgent()
		forgedDigest := strings.Repeat("0", 64)
		forged := forge(agent, callerKeysSecretName, map[string][]byte{callerKeysFileKey: []byte(`{"` + forgedDigest + `":"agent"}`)})
		r, cl, agent := inferenceKeysFixture(t, forged)
		rec := record.NewFakeRecorder(64)
		r.Recorder = rec
		r.reconcileInferenceKeys(ctx, agent)
		assertReplaced(t, cl, rec, agent, forged, forgedDigest)
	})
	t.Run("a class key", func(t *testing.T) {
		agent := inferenceKeysTestAgent()
		plantedKey := "sk-" + strings.Repeat("B", 43)
		forged := forge(agent, inferenceKeySecretName(agent, inferenceKeyClassAgent), map[string][]byte{inferenceKeyDataKey: []byte(plantedKey)})
		forged.Labels[inferenceKeyClassLabel] = inferenceKeyClassAgent
		r, cl, agent := inferenceKeysFixture(t, forged)
		rec := record.NewFakeRecorder(64)
		r.Recorder = rec
		r.reconcileInferenceKeys(ctx, agent)
		assertReplaced(t, cl, rec, agent, forged, plantedKey)
	})
}

// A rollback round-trip drops the record: every Secret is then replaced
// once, the record is rebuilt with the new UIDs, the map holds only the new
// digests, the condition is Served again, and the pass after replaces
// nothing.
func TestALostRecordReplacesEveryKeyOnce(t *testing.T) {
	r, cl, agent := inferenceKeysFixture(t)
	ctx := context.Background()
	r.reconcileInferenceKeys(ctx, agent)
	oldKeys := allInferenceKeys(t, cl, agent, "read", "unattended")
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatal(err)
	}
	oldUIDs := stored.Status.InferenceKeys.SecretUIDs
	stored.Status.InferenceKeys = nil
	if err := cl.Status().Update(ctx, stored); err != nil {
		t.Fatal(err)
	}

	rec := record.NewFakeRecorder(64)
	restarted := &PlatformAgentReconciler{Client: cl, Scheme: r.Scheme, Recorder: rec, inferenceKeyProbe: admitEverything()}
	if out := restarted.reconcileInferenceKeys(ctx, stored); out.condition.Status != metav1.ConditionTrue {
		t.Fatalf("after the round-trip: %+v", out.condition)
	}
	replaced := 0
	for _, e := range drainEvents(rec) {
		if strings.Contains(e, reasonInferenceKeySecretReplaced) {
			replaced++
		}
	}
	if replaced != len(oldUIDs) {
		t.Errorf("%d Secrets replaced, want every one (%d)", replaced, len(oldUIDs))
	}
	newKeys := allInferenceKeys(t, cl, agent, "read", "unattended")
	for class, k := range newKeys {
		if k == oldKeys[class] {
			t.Errorf("the %s key didn't rotate", class)
		}
	}
	rebuilt := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), rebuilt); err != nil {
		t.Fatal(err)
	}
	newUIDs := rebuilt.Status.InferenceKeys.SecretUIDs
	if len(newUIDs) != len(oldUIDs) {
		t.Errorf("%d records rebuilt, want %d", len(newUIDs), len(oldUIDs))
	}
	for name, uid := range newUIDs {
		if oldUIDs[name] == uid {
			t.Errorf("%s kept its old UID", name)
		}
	}
	entries, _ := readCallerKeysMap(t, cl, agent)
	if len(entries) != len(newKeys) {
		t.Errorf("map has %d entries for %d keys", len(entries), len(newKeys))
	}
	for class, k := range newKeys {
		if entries[inferenceKeyDigest(k)] != class {
			t.Errorf("the map lacks the new %s digest", class)
		}
		if _, ok := entries[inferenceKeyDigest(oldKeys[class])]; ok {
			t.Errorf("the map still holds the old %s digest", class)
		}
	}

	restarted.reconcileInferenceKeys(ctx, rebuilt)
	for _, e := range drainEvents(rec) {
		if strings.Contains(e, reasonInferenceKeySecretReplaced) {
			t.Errorf("the pass after the recovery replaced something: %s", e)
		}
	}
}

// The replacement deletes exactly the object it read: the delete carries
// that Secret's UID as a precondition.
func TestTheReplacementDeleteCarriesTheUIDPrecondition(t *testing.T) {
	agent := inferenceKeysTestAgent()
	planted := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: inferenceKeySecretName(agent, inferenceKeyClassSession), Namespace: agent.Namespace, UID: "uid-planted-session"}}
	r, cl, agent := inferenceKeysFixture(t, planted)
	var mu sync.Mutex
	preconditions := map[string]*types.UID{}
	r.Client = interceptor.NewClient(cl.(client.WithWatch), interceptor.Funcs{
		Delete: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
			do := &client.DeleteOptions{}
			do.ApplyOptions(opts)
			mu.Lock()
			if do.Preconditions != nil {
				preconditions[obj.GetName()] = do.Preconditions.UID
			} else {
				preconditions[obj.GetName()] = nil
			}
			mu.Unlock()
			return c.Delete(ctx, obj, opts...)
		},
	})
	r.reconcileInferenceKeys(context.Background(), agent)
	got, deleted := preconditions[planted.Name]
	if !deleted || got == nil || *got != planted.UID {
		t.Errorf("the replacement delete's UID precondition: deleted %v, uid %v, want %s", deleted, got, planted.UID)
	}
}

// The record lives in the PlatformAgent's status: every Secret the operator
// created is there by name and UID, with no key and no digest, and a fresh
// reconciler (an operator restart) reading the stored object trusts the same
// Secrets and changes none.
func TestTheStatusRecordSurvivesARestart(t *testing.T) {
	r, cl, agent := inferenceKeysFixture(t)
	ctx := context.Background()
	if out := r.reconcileInferenceKeys(ctx, agent); out.condition.Status != metav1.ConditionTrue {
		t.Fatalf("first pass: %+v", out.condition)
	}
	before := allInferenceKeys(t, cl, agent, "read", "unattended")

	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatal(err)
	}
	if stored.Status.InferenceKeys == nil {
		t.Fatal("no status.inferenceKeys after the first pass")
	}
	uids := stored.Status.InferenceKeys.SecretUIDs
	if len(uids) != 7 {
		t.Errorf("%d records, want 6 key Secrets and the map", len(uids))
	}
	for name, uid := range uids {
		ns := agent.Namespace
		if strings.Contains(name, "-tier-") {
			ns = profileNamespaceName(agent)
		}
		s := readInferenceKey(t, cl, types.NamespacedName{Namespace: ns, Name: name})
		if string(s.UID) != uid {
			t.Errorf("record for %s is %s, the Secret's UID is %s", name, uid, s.UID)
		}
	}
	raw, _ := json.Marshal(stored.Status.InferenceKeys)
	for class, apiKey := range before {
		if strings.Contains(string(raw), strings.TrimPrefix(apiKey, inferenceKeyPrefix)) || strings.Contains(string(raw), inferenceKeyDigest(apiKey)) {
			t.Errorf("the status record carries the %s key or its digest", class)
		}
	}

	restarted := &PlatformAgentReconciler{Client: cl, Scheme: r.Scheme, inferenceKeyProbe: admitEverything()}
	out := restarted.reconcileInferenceKeys(ctx, stored)
	if out.condition.Status != metav1.ConditionTrue {
		t.Fatalf("after a restart: %+v", out.condition)
	}
	if after := allInferenceKeys(t, cl, agent, "read", "unattended"); !mapsEqual(before, after) {
		t.Error("a key changed across the restart")
	}
	again := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), again); err != nil {
		t.Fatal(err)
	}
	if !mapsEqual(again.Status.InferenceKeys.SecretUIDs, uids) {
		t.Error("the record changed across the restart")
	}
}

// A served CRD without status.inferenceKeys drops the record: the operator
// then creates nothing, deletes the Secret whose record didn't hold, and
// says so.
func TestAPrunedStatusFieldCreatesNoKeys(t *testing.T) {
	_, base, _ := inferenceKeysFixture(t)
	cl := interceptor.NewClient(base.(client.WithWatch), interceptor.Funcs{
		SubResourcePatch: func(ctx context.Context, c client.Client, sub string, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
			if err := c.SubResource(sub).Patch(ctx, obj, patch, opts...); err != nil {
				return err
			}
			if pa, ok := obj.(*agentv1alpha1.PlatformAgent); ok && sub == "status" {
				pa.Status.InferenceKeys = nil
				if err := c.Status().Update(ctx, pa); err != nil {
					return err
				}
			}
			return nil
		},
	})
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), inferenceKeyProbe: admitEverything()}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(context.Background(), types.NamespacedName{Namespace: "test-ns", Name: "test-agent"}, stored); err != nil {
		t.Fatal(err)
	}
	out := r.reconcileInferenceKeys(context.Background(), stored)
	if out.condition.Reason != inferenceKeysReasonNotWritten || !strings.Contains(out.condition.Message, "status.inferenceKeys") || out.requeue != 0 {
		t.Errorf("outcome %+v", out)
	}
	assertNoInferenceKeys(t, cl, stored)
}

// The pass's copy of the agent can lag a record the operator just wrote.
// With a stale copy that lacks the record while the API server holds it,
// nothing is deleted or replaced: the uncached read decides.
func TestAStaleCachedRecordReplacesNothing(t *testing.T) {
	r, cl, agent := inferenceKeysFixture(t)
	ctx := context.Background()
	r.reconcileInferenceKeys(ctx, agent)
	before := allInferenceKeys(t, cl, agent, "read", "unattended")

	stale := agent.DeepCopy()
	stale.Status.InferenceKeys = nil
	rec := record.NewFakeRecorder(64)
	deletes := 0
	var mu sync.Mutex
	inner := cl.(client.WithWatch)
	r.Client = interceptor.NewClient(inner, interceptor.Funcs{
		Delete: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
			mu.Lock()
			deletes++
			mu.Unlock()
			return c.Delete(ctx, obj, opts...)
		},
	})
	r.APIReader = inner
	r.Recorder = rec
	r.inferenceKeysServedFor.Delete(client.ObjectKeyFromObject(agent))
	if out := r.reconcileInferenceKeys(ctx, stale); out.condition.Status != metav1.ConditionTrue {
		t.Errorf("condition %+v", out.condition)
	}
	if deletes != 0 {
		t.Errorf("%d deletes on a stale copy", deletes)
	}
	for _, e := range drainEvents(rec) {
		if strings.Contains(e, reasonInferenceKeySecretReplaced) {
			t.Errorf("replaced on a stale copy: %s", e)
		}
	}
	if after := allInferenceKeys(t, cl, agent, "read", "unattended"); !mapsEqual(before, after) {
		t.Error("a key changed on a stale copy")
	}
}

// When the uncached read fails, a Secret the pass's copy doesn't record is
// not replaced: the condition says so, fail-closed, and the pass looks
// again.
func TestAFailedUncachedReadReplacesNothing(t *testing.T) {
	agent := inferenceKeysTestAgent()
	planted := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: inferenceKeySecretName(agent, inferenceKeyClassAgent), Namespace: agent.Namespace, UID: "uid-planted-agent"},
		Data:       map[string][]byte{inferenceKeyDataKey: []byte("sk-" + strings.Repeat("C", 43))},
	}
	r, cl, agent := inferenceKeysFixture(t, planted)
	r.APIReader = interceptor.NewClient(cl.(client.WithWatch), interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
				return apierrors.NewServiceUnavailable("unavailable")
			}
			return c.Get(ctx, key, obj, opts...)
		},
	})
	out := r.reconcileInferenceKeys(context.Background(), agent)
	if out.condition.Status != metav1.ConditionFalse || out.condition.Reason != inferenceKeysReasonNotWritten || out.requeue == 0 ||
		!strings.Contains(out.condition.Message, "nothing is replaced") {
		t.Errorf("outcome %+v", out)
	}
	if got := readInferenceKey(t, cl, client.ObjectKeyFromObject(planted)); got.UID != planted.UID {
		t.Error("a Secret was replaced although the record couldn't be confirmed")
	}
	if err := cl.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: callerKeysSecretName}, &corev1.Secret{}); !apierrors.IsNotFound(err) {
		t.Errorf("a map was written on a pass that confirmed nothing: %v", err)
	}
}

// writeCounter counts every write a client makes.
func writeCounter(inner client.WithWatch, writes *atomic.Int32) client.WithWatch {
	return interceptor.NewClient(inner, interceptor.Funcs{
		Create: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
			writes.Add(1)
			return c.Create(ctx, obj, opts...)
		},
		Update: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.UpdateOption) error {
			writes.Add(1)
			return c.Update(ctx, obj, opts...)
		},
		Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			writes.Add(1)
			return c.Patch(ctx, obj, patch, opts...)
		},
		Delete: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
			writes.Add(1)
			return c.Delete(ctx, obj, opts...)
		},
		SubResourcePatch: func(ctx context.Context, c client.Client, sub string, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
			writes.Add(1)
			return c.SubResource(sub).Patch(ctx, obj, patch, opts...)
		},
	})
}

// An older served CRD, the normal state after a Helm upgrade that doesn't
// move CRDs: nothing is created, replaced, deleted or written, a foreign
// Secret at a reserved name survives, and the condition is CRDOutdated with
// no early requeue.
func TestAnOlderCRDWritesNothing(t *testing.T) {
	agent := inferenceKeysTestAgent()
	foreign := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: inferenceKeySecretName(agent, inferenceKeyClassAgent), Namespace: agent.Namespace, UID: "uid-foreign-agent"}}
	r, cl, agent := inferenceKeysFixture(t, foreign)
	ctx := context.Background()
	older := &unstructured.Unstructured{}
	older.SetGroupVersionKind(crdGVK)
	if err := cl.Get(ctx, types.NamespacedName{Name: servedPlatformAgentCRDName}, older); err != nil {
		t.Fatal(err)
	}
	older.Object["spec"] = platformAgentCRDObject(false).Object["spec"]
	if err := cl.Update(ctx, older); err != nil {
		t.Fatal(err)
	}
	var writes atomic.Int32
	r.Client = writeCounter(cl.(client.WithWatch), &writes)
	for range 2 {
		out := r.reconcileInferenceKeys(ctx, agent)
		if out.condition.Status != metav1.ConditionFalse || out.condition.Reason != inferenceKeysReasonCRDOutdated || out.requeue != 0 {
			t.Errorf("outcome %+v", out)
		}
	}
	if n := writes.Load(); n != 0 {
		t.Errorf("%d writes with an older CRD", n)
	}
	if got := readInferenceKey(t, cl, client.ObjectKeyFromObject(foreign)); got.UID != foreign.UID {
		t.Error("a Secret at a reserved name was replaced under an older CRD")
	}
	assertNoInferenceKeysBut(t, cl, agent, inferenceKeyClassAgent)
}

// assertNoInferenceKeysBut is assertNoInferenceKeys with one class's Secret
// allowed, for a pre-existing one the test planted.
func assertNoInferenceKeysBut(t *testing.T, cl client.Client, agent *agentv1alpha1.PlatformAgent, planted string) {
	t.Helper()
	names := []types.NamespacedName{{Namespace: agent.Namespace, Name: callerKeysSecretName}}
	for _, class := range append(append([]string(nil), installInferenceKeyClasses...), inferenceKeyClassProbe) {
		if class != planted {
			names = append(names, installInferenceKeyClass(agent, class).secret)
		}
	}
	for _, name := range names {
		if err := cl.Get(context.Background(), name, &corev1.Secret{}); !apierrors.IsNotFound(err) {
			t.Errorf("Secret %s: %v, want absent", name, err)
		}
	}
}

// A CRD read that fails writes nothing on that pass and looks again.
func TestAFailedCRDReadWritesNothing(t *testing.T) {
	r, cl, agent := inferenceKeysFixture(t)
	var writes atomic.Int32
	r.Client = writeCounter(cl.(client.WithWatch), &writes)
	r.APIReader = interceptor.NewClient(cl.(client.WithWatch), interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if obj.GetObjectKind().GroupVersionKind().Kind == crdGVK.Kind {
				return apierrors.NewServiceUnavailable("unavailable")
			}
			return c.Get(ctx, key, obj, opts...)
		},
	})
	out := r.reconcileInferenceKeys(context.Background(), agent)
	if out.condition.Reason != inferenceKeysReasonNotWritten || out.requeue == 0 || writes.Load() != 0 {
		t.Errorf("outcome %+v after %d writes", out, writes.Load())
	}
}

// A steady install reads the CRD's metadata each pass and its schema only
// when the resourceVersion moved.
func TestTheCRDSchemaIsReadOncePerResourceVersion(t *testing.T) {
	r, cl, agent := inferenceKeysFixture(t)
	var full, head atomic.Int32
	r.APIReader = interceptor.NewClient(cl.(client.WithWatch), interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			switch obj.(type) {
			case *unstructured.Unstructured:
				full.Add(1)
			case *metav1.PartialObjectMetadata:
				head.Add(1)
			}
			return c.Get(ctx, key, obj, opts...)
		},
	})
	ctx := context.Background()
	for range 3 {
		r.reconcileInferenceKeys(ctx, agent)
	}
	if full.Load() != 1 || head.Load() != 3 {
		t.Errorf("%d schema reads and %d metadata reads over three passes, want 1 and 3", full.Load(), head.Load())
	}
}

// mapServingGateway is a fake gateway whose pods each serve a loaded map:
// a pod admits a key only when its digest is in the map that pod loaded.
type mapServingGateway struct {
	mu     sync.Mutex
	loaded map[string]map[string]string
}

func (g *mapServingGateway) load(host string, doc []byte) {
	var entries map[string]string
	_ = json.Unmarshal(doc, &entries)
	g.mu.Lock()
	defer g.mu.Unlock()
	g.loaded[host] = entries
}

func (g *mapServingGateway) Probe(_ context.Context, addr, apiKey string) (int, error) {
	host, _, _ := net.SplitHostPort(addr)
	g.mu.Lock()
	defer g.mu.Unlock()
	if _, ok := g.loaded[host][inferenceKeyDigest(apiKey)]; ok {
		return http.StatusOK, nil
	}
	return http.StatusUnauthorized, nil
}

// A new map carries a new probe key: from Served, adding a tier rewrites the
// map, and a pod still serving the old map refuses the new probe key, so the
// condition reads NotServed until that pod loads the new map. The old probe
// digest is gone from the new map.
func TestANewMapIsServedOnlyOnceThePodsLoadIt(t *testing.T) {
	r, cl, agent := inferenceKeysFixture(t)
	ctx := context.Background()
	profile := &agentv1alpha1.AgentProfile{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: "auditor"}, profile); err != nil {
		t.Fatal(err)
	}
	profile.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead}
	if err := cl.Update(ctx, profile); err != nil {
		t.Fatal(err)
	}
	gw := &mapServingGateway{loaded: map[string]map[string]string{}}
	r.inferenceKeyProbe = gw
	loadEverywhere := func() {
		_, doc := readCallerKeysMap(t, cl, agent)
		gw.load("10.1.0.1", doc)
		gw.load("10.1.0.2", doc)
	}

	r.reconcileInferenceKeys(ctx, agent)
	loadEverywhere()
	if out := r.reconcileInferenceKeys(ctx, agent); out.condition.Status != metav1.ConditionTrue {
		t.Fatalf("with the map loaded: %+v", out.condition)
	}
	oldProbe := allInferenceKeys(t, cl, agent)[inferenceKeyClassProbe]

	profile.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	if err := cl.Update(ctx, profile); err != nil {
		t.Fatal(err)
	}
	out := r.reconcileInferenceKeys(ctx, agent)
	if out.condition.Status != metav1.ConditionFalse || out.condition.Reason != inferenceKeysReasonNotServed {
		t.Fatalf("pods on the old map read as %+v, want NotServed", out.condition)
	}
	newProbe := allInferenceKeys(t, cl, agent)[inferenceKeyClassProbe]
	if newProbe == oldProbe {
		t.Fatal("the probe key didn't change with the map")
	}
	entries, _ := readCallerKeysMap(t, cl, agent)
	if _, ok := entries[inferenceKeyDigest(oldProbe)]; ok {
		t.Error("the new map still holds the old probe digest")
	}
	if entries[inferenceKeyDigest(newProbe)] != inferenceKeyClassProbe {
		t.Error("the new map lacks the new probe digest")
	}

	loadEverywhere()
	if out := r.reconcileInferenceKeys(ctx, agent); out.condition.Status != metav1.ConditionTrue {
		t.Errorf("with the new map loaded: %+v", out.condition)
	}
	if again := allInferenceKeys(t, cl, agent)[inferenceKeyClassProbe]; again != newProbe {
		t.Error("an unchanged map rotated the probe key again")
	}
}

// A map write that fails after the probe key was replaced converges on the
// next pass: the map is rewritten with the current probe digest.
func TestAFailedMapWriteAfterARotationConverges(t *testing.T) {
	r, cl, agent := inferenceKeysFixture(t)
	ctx := context.Background()
	r.reconcileInferenceKeys(ctx, agent)
	profile := &agentv1alpha1.AgentProfile{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: "auditor"}, profile); err != nil {
		t.Fatal(err)
	}
	profile.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead}
	if err := cl.Update(ctx, profile); err != nil {
		t.Fatal(err)
	}
	failMap := true
	inner := cl.(client.WithWatch)
	r.Client = interceptor.NewClient(inner, interceptor.Funcs{
		Update: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.UpdateOption) error {
			if obj.GetName() == callerKeysSecretName && failMap {
				return apierrors.NewServiceUnavailable("unavailable")
			}
			return c.Update(ctx, obj, opts...)
		},
	})
	if out := r.reconcileInferenceKeys(ctx, agent); out.condition.Reason != inferenceKeysReasonNotWritten {
		t.Fatalf("a failed map write read as %+v", out.condition)
	}
	failMap = false
	r.reconcileInferenceKeys(ctx, agent)
	probeKey := allInferenceKeys(t, cl, agent)[inferenceKeyClassProbe]
	entries, _ := readCallerKeysMap(t, cl, agent)
	if entries[inferenceKeyDigest(probeKey)] != inferenceKeyClassProbe {
		t.Error("the map doesn't carry the current probe digest after the retry")
	}
	probes := 0
	for _, class := range entries {
		if class == inferenceKeyClassProbe {
			probes++
		}
	}
	if probes != 1 {
		t.Errorf("%d probe digests in the map, want 1", probes)
	}
}

// A failure confined to the profile namespace doesn't cost the rest of the
// map: with a tier key's Get or Create refused, the map is still written for
// agent, a2a-session and curator, the failure is a ProfileNamespaceFailed
// Warning, and a tier that had an entry keeps it.
func TestATierFailureDoesNotBlockTheMap(t *testing.T) {
	ctx := context.Background()
	inProfiles := func(obj client.Object, key client.ObjectKey) bool {
		_, isSecret := obj.(*corev1.Secret)
		return isSecret && strings.HasSuffix(key.Namespace, profileNamespaceSuffix)
	}
	assertCallers := func(t *testing.T, cl client.Client, agent *agentv1alpha1.PlatformAgent, rec *record.FakeRecorder) map[string]string {
		t.Helper()
		entries, _ := readCallerKeysMap(t, cl, agent)
		have := map[string]bool{}
		for _, class := range entries {
			have[class] = true
		}
		for _, class := range installInferenceKeyClasses {
			if !have[class] {
				t.Errorf("the map lacks %s after a tier failure", class)
			}
		}
		warned := false
		for _, e := range drainEvents(rec) {
			if strings.HasPrefix(e, corev1.EventTypeWarning) && strings.Contains(e, reasonProfileNamespaceFailed) {
				warned = true
			}
		}
		if !warned {
			t.Errorf("no %s Warning for the tier failure", reasonProfileNamespaceFailed)
		}
		return entries
	}
	t.Run("a refused Get keeps the tier's entry", func(t *testing.T) {
		r, cl, agent := inferenceKeysFixture(t)
		r.reconcileInferenceKeys(ctx, agent)
		readKey := allInferenceKeys(t, cl, agent, "read")["tier-read"]
		rec := record.NewFakeRecorder(64)
		r.Recorder = rec
		r.APIReader = interceptor.NewClient(cl.(client.WithWatch), interceptor.Funcs{
			Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
				if inProfiles(obj, key) {
					return apierrors.NewForbidden(corev1.Resource("secrets"), key.Name, errors.New("denied"))
				}
				return c.Get(ctx, key, obj, opts...)
			},
		})
		r.reconcileInferenceKeys(ctx, agent)
		entries := assertCallers(t, cl, agent, rec)
		if entries[inferenceKeyDigest(readKey)] != "tier-read" {
			t.Error("the tier's stored entry wasn't carried over")
		}
	})
	t.Run("a refused Create drops a tier that had no entry", func(t *testing.T) {
		r, cl, agent := inferenceKeysFixture(t)
		rec := record.NewFakeRecorder(64)
		r.Recorder = rec
		r.Client = interceptor.NewClient(cl.(client.WithWatch), interceptor.Funcs{
			Create: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
				if inProfiles(obj, client.ObjectKeyFromObject(obj)) {
					return apierrors.NewForbidden(corev1.Resource("secrets"), obj.GetName(), errors.New("denied"))
				}
				return c.Create(ctx, obj, opts...)
			},
		})
		r.reconcileInferenceKeys(ctx, agent)
		entries := assertCallers(t, cl, agent, rec)
		for _, class := range entries {
			if strings.HasPrefix(class, inferenceKeyTierClassPrefix) {
				t.Errorf("the map names %s, whose key was never created", class)
			}
		}
	})
}

// With AgentProfiles unreadable, the kept tiers are written only into a
// profile namespace that is there: once it's gone, no tier Secret is
// created and the tiers leave the map.
func TestUnreadableProfilesCreateNoTierKeyWithoutTheNamespace(t *testing.T) {
	r, cl, agent := inferenceKeysFixture(t)
	ctx := context.Background()
	r.reconcileInferenceKeys(ctx, agent)
	ns := profileNamespaceObject(agent)
	if err := cl.Delete(ctx, ns); err != nil {
		t.Fatal(err)
	}
	for _, tier := range []string{"read", "unattended"} {
		if err := cl.Delete(ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: inferenceKeySecretName(agent, inferenceKeyTierClassPrefix+tier), Namespace: profileNamespaceName(agent)}}); err != nil {
			t.Fatal(err)
		}
	}
	r.agentProfilesUnreadable = true
	r.reconcileInferenceKeys(ctx, agent)
	for _, tier := range []string{"read", "unattended"} {
		if err := cl.Get(ctx, tierInferenceKeyClass(agent, tier).secret, &corev1.Secret{}); !apierrors.IsNotFound(err) {
			t.Errorf("a %s tier key was created without the profile namespace: %v", tier, err)
		}
	}
	entries, _ := readCallerKeysMap(t, cl, agent)
	for _, class := range entries {
		if strings.HasPrefix(class, inferenceKeyTierClassPrefix) {
			t.Errorf("the map still names %s", class)
		}
	}
}

// A Secret at a reserved name that is already being deleted is waited for:
// no delete, no Warning, and the operator creates its own once it's gone. A
// tier's doesn't hold up the map.
func TestATerminatingReservedSecretIsWaitedFor(t *testing.T) {
	agent := inferenceKeysTestAgent()
	terminating := func(name, ns string) *corev1.Secret {
		return &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: ns, UID: types.UID("uid-terminating-" + name),
			DeletionTimestamp: &metav1.Time{Time: metav1.Now().Time}, Finalizers: []string{"example.com/hold"},
		}}
	}
	t.Run("a class key", func(t *testing.T) {
		leaving := terminating(inferenceKeySecretName(agent, inferenceKeyClassAgent), agent.Namespace)
		r, cl, agent := inferenceKeysFixture(t, leaving)
		rec := record.NewFakeRecorder(64)
		r.Recorder = rec
		var writes atomic.Int32
		r.Client = writeCounter(cl.(client.WithWatch), &writes)
		out := r.reconcileInferenceKeys(context.Background(), agent)
		if out.condition.Reason != inferenceKeysReasonNotWritten || out.requeue == 0 || !strings.Contains(out.condition.Message, "being deleted") {
			t.Errorf("outcome %+v", out)
		}
		if got := readInferenceKey(t, cl, client.ObjectKeyFromObject(leaving)); got.UID != leaving.UID {
			t.Error("a terminating Secret was replaced")
		}
		for _, e := range drainEvents(rec) {
			if strings.Contains(e, reasonInferenceKeySecretReplaced) {
				t.Errorf("a terminating Secret was reported: %s", e)
			}
		}
	})
	t.Run("a tier key", func(t *testing.T) {
		leaving := terminating(inferenceKeySecretName(agent, "tier-read"), profileNamespaceName(agent))
		r, cl, agent := inferenceKeysFixture(t, leaving)
		rec := record.NewFakeRecorder(64)
		r.Recorder = rec
		r.reconcileInferenceKeys(context.Background(), agent)
		if got := readInferenceKey(t, cl, client.ObjectKeyFromObject(leaving)); got.UID != leaving.UID {
			t.Error("a terminating tier Secret was replaced")
		}
		entries, _ := readCallerKeysMap(t, cl, agent)
		for _, class := range entries {
			if class == "tier-read" {
				t.Error("the map names the tier whose Secret is being deleted")
			}
		}
		for _, e := range drainEvents(rec) {
			if strings.HasPrefix(e, corev1.EventTypeWarning) {
				t.Errorf("a Warning for a terminating Secret: %s", e)
			}
		}
	})
}

// The tier cleanup reads only the recorded tiers' Secrets: a built-in tier
// no profile uses costs a steady pass no Secret read.
func TestAnUnusedTierCostsNoRead(t *testing.T) {
	r, cl, agent := inferenceKeysFixture(t)
	ctx := context.Background()
	profile := &agentv1alpha1.AgentProfile{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: "auditor"}, profile); err != nil {
		t.Fatal(err)
	}
	profile.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead}
	if err := cl.Update(ctx, profile); err != nil {
		t.Fatal(err)
	}
	r.reconcileInferenceKeys(ctx, agent)
	unused := tierInferenceKeyClass(agent, agentv1alpha1.AgentProfileTierUnattended).secret
	var reads atomic.Int32
	r.APIReader = interceptor.NewClient(cl.(client.WithWatch), interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, ok := obj.(*corev1.Secret); ok && key == unused {
				reads.Add(1)
			}
			return c.Get(ctx, key, obj, opts...)
		},
	})
	r.reconcileInferenceKeys(ctx, agent)
	if n := reads.Load(); n != 0 {
		t.Errorf("%d reads of an unused tier's Secret on a steady pass", n)
	}
}

// Step 12a's record write moves the stored version on the render copy only.
// When the pass then fails before the instance takes that version, the
// condition is still written, against the PlatformAgent read past the
// cache, with no conflict returned, the record kept, and the instance's
// version left as it was.
func TestTheConditionIsWrittenAfterTheRecordMovedTheVersion(t *testing.T) {
	r, cl, _ := inferenceKeysFixture(t)
	ctx := context.Background()
	instance := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: "test-ns", Name: "test-agent"}, instance); err != nil {
		t.Fatal(err)
	}
	staleVersion := instance.ResourceVersion
	render := instance.DeepCopy()
	out := r.reconcileInferenceKeys(ctx, render)
	if render.ResourceVersion == staleVersion {
		t.Fatal("the record write didn't move the version; the test sets up nothing")
	}
	if err := r.syncInferenceKeysServed(ctx, instance, out.condition); err != nil {
		t.Fatalf("the condition write returned %v", err)
	}
	if instance.ResourceVersion != staleVersion {
		t.Error("the instance's version was moved")
	}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(instance), stored); err != nil {
		t.Fatal(err)
	}
	c := meta.FindStatusCondition(stored.Status.Conditions, inferenceKeysServedCondition)
	if c == nil || c.Reason != out.condition.Reason {
		t.Errorf("stored condition %+v, want %s", c, out.condition.Reason)
	}
	if len(recordedSecretUIDs(stored)) == 0 {
		t.Error("the record was lost")
	}
}
