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
	"strings"
	"testing"

	"github.com/nats-io/nkeys"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The task-signing key (#3889): generated once beside the callout's keys, its
// public half rendered into the callout's environment, and its seed in a
// Secret nothing the operator renders mounts or reads.

func taskSigningTestReconciler(t *testing.T) (*PlatformAgentReconciler, client.Client, *agentv1alpha1.PlatformAgent) {
	t.Helper()
	scheme := setupScheme()
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl, agent
}

func TestTheTaskSigningKeyIsGeneratedOnceAndKept(t *testing.T) {
	r, cl, agent := taskSigningTestReconciler(t)
	ctx := context.Background()

	first, err := r.ensureA2ATaskSigningKeySecret(ctx, agent)
	if err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if !nkeys.IsValidPublicClusterKey(first.Public) || !strings.HasPrefix(first.Seed, "SC") {
		t.Errorf("the key is not a cluster key: public %q, seed prefix %q", first.Public, first.Seed[:2])
	}
	for i := 0; i < 3; i++ {
		again, err := r.ensureA2ATaskSigningKeySecret(ctx, agent)
		if err != nil {
			t.Fatalf("ensure %d: %v", i+2, err)
		}
		if *again != *first {
			t.Fatalf("the key changed on reconcile %d; a new key refuses every credential already handed out", i+2)
		}
	}

	secret := &corev1.Secret{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-task-signing-key", Namespace: "test-ns"}, secret); err != nil {
		t.Fatalf("the task-signing Secret was not created: %v", err)
	}
	if !metav1.IsControlledBy(secret, agent) {
		t.Error("the task-signing Secret is not owned by the PlatformAgent, so nothing reclaims it")
	}
	if string(secret.Data[a2aTaskSigningSeedKey]) != first.Seed {
		t.Error("the Secret's seed is not the one returned")
	}
}

// A corrupt Secret, or one holding a key of another type, is regenerated as a
// whole; a rigged public entry is ignored in favour of the seed's.
func TestTheTaskSigningSecretIsReadFromItsSeedAlone(t *testing.T) {
	good, data, err := generateA2ATaskSigningKey()
	if err != nil {
		t.Fatal(err)
	}
	data[a2aTaskSigningPubKey] = []byte("Cnot-the-real-public-key\"\nx")
	key, ok := readA2ATaskSigningKey(&corev1.Secret{Data: data})
	if !ok || key.Public != good.Public {
		t.Fatalf("read = %+v, %v; want the public half derived from the seed (%q)", key, ok, good.Public)
	}

	acct, _ := nkeys.CreateAccount()
	acctSeed, _ := acct.Seed()
	for name, seed := range map[string][]byte{"an account seed": acctSeed, "garbage": []byte("SCnope"), "empty": nil} {
		if _, ok := readA2ATaskSigningKey(&corev1.Secret{Data: map[string][]byte{a2aTaskSigningSeedKey: seed}}); ok {
			t.Errorf("%s was accepted as a task-signing seed", name)
		}
	}

	// An OWNED corrupt Secret is repaired as a set.
	r, cl, agent := taskSigningTestReconciler(t)
	ctx := context.Background()
	owned := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent-a2a-task-signing-key", Namespace: "test-ns"},
		Data:       map[string][]byte{a2aTaskSigningSeedKey: acctSeed},
	}
	if err := ctrl.SetControllerReference(agent, owned, r.Scheme); err != nil {
		t.Fatal(err)
	}
	if err := cl.Create(ctx, owned); err != nil {
		t.Fatal(err)
	}
	repaired, err := r.ensureA2ATaskSigningKeySecret(ctx, agent)
	if err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if !nkeys.IsValidPublicClusterKey(repaired.Public) {
		t.Errorf("the repair produced %q, not a cluster key", repaired.Public)
	}
}

// A Secret at the name that this PlatformAgent doesn't control is refused
// before it is read: neither a valid seed in it (which would let its author
// choose the key) nor a corrupt one (which a repair would overwrite) is used.
func TestAnUnownedTaskSigningSecretIsRefused(t *testing.T) {
	_, valid, err := generateA2ATaskSigningKey()
	if err != nil {
		t.Fatal(err)
	}
	for name, data := range map[string]map[string][]byte{
		"a valid seed":   valid,
		"a corrupt seed": {a2aTaskSigningSeedKey: []byte("garbage")},
	} {
		t.Run(name, func(t *testing.T) {
			r, cl, agent := taskSigningTestReconciler(t)
			ctx := context.Background()
			planted := &corev1.Secret{
				ObjectMeta: metav1.ObjectMeta{Name: "test-agent-a2a-task-signing-key", Namespace: "test-ns"},
				Data:       data,
			}
			if err := cl.Create(ctx, planted); err != nil {
				t.Fatal(err)
			}
			key, err := r.ensureA2ATaskSigningKeySecret(ctx, agent)
			if err == nil || !strings.Contains(err.Error(), "refusing to adopt unowned Secret test-ns/test-agent-a2a-task-signing-key") {
				t.Fatalf("ensure = %+v, %v; want the unowned-Secret refusal", key, err)
			}
			after := &corev1.Secret{}
			if err := cl.Get(ctx, client.ObjectKeyFromObject(planted), after); err != nil {
				t.Fatal(err)
			}
			if string(after.Data[a2aTaskSigningSeedKey]) != string(data[a2aTaskSigningSeedKey]) {
				t.Error("the unowned Secret was rewritten")
			}
		})
	}
}

// After a full reconcile under next: the callout carries the public half as a
// plain value, and nothing else the operator rendered references the
// task-signing Secret or carries the seed. Until the dispatcher exists,
// nothing mounts it at all.
func TestTheTaskSigningKeyReachesTheCalloutAsItsPublicHalfOnly(t *testing.T) {
	// The MCP door renders only with its env var set.
	t.Setenv(a2aMCPDoorEnvVar, "true")
	r, cl, agent := taskSigningTestReconciler(t)
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	// The gateway Deployment and the provision Job are held until a callout
	// replica reports serving, which the fake client never fills in. Without
	// this the walk below would pass on their absence.
	letTheGatewayThrough(t, ctx, cl, r, req, agent)

	// The workloads the walk is about must exist before their absence of a
	// reference means anything.
	for what, obj := range map[string]client.Object{
		"A2A gateway Deployment": &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: a2aGatewayName(agent), Namespace: agent.Namespace}},
		"MCP Deployment":         &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: a2aMCPName(agent), Namespace: agent.Namespace}},
		"provision Job":          &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: buildA2AProvisionJob(agent).Name, Namespace: agent.Namespace}},
	} {
		if err := cl.Get(ctx, client.ObjectKeyFromObject(obj), obj); err != nil {
			t.Fatalf("the %s was not rendered, so the walk below would pass on its absence: %v", what, err)
		}
	}

	secret := &corev1.Secret{}
	secretName := "test-agent-a2a-task-signing-key"
	if err := cl.Get(ctx, types.NamespacedName{Name: secretName, Namespace: "test-ns"}, secret); err != nil {
		t.Fatalf("the task-signing Secret was not rendered under next: %v", err)
	}
	key, ok := readA2ATaskSigningKey(secret)
	if !ok {
		t.Fatal("the rendered task-signing Secret does not read back")
	}

	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-callout", Namespace: "test-ns"}, dep); err != nil {
		t.Fatalf("callout Deployment: %v", err)
	}
	var rendered *corev1.EnvVar
	for i, e := range dep.Spec.Template.Spec.Containers[0].Env {
		if e.Name == a2aCalloutTaskSigningKeysEnvVar {
			rendered = &dep.Spec.Template.Spec.Containers[0].Env[i]
		}
	}
	if rendered == nil {
		t.Fatalf("the callout has no %s", a2aCalloutTaskSigningKeysEnvVar)
	}
	if rendered.Value != key.Public || rendered.ValueFrom != nil {
		t.Errorf("%s = %q (valueFrom %v), want the plain public key %q", a2aCalloutTaskSigningKeysEnvVar, rendered.Value, rendered.ValueFrom, key.Public)
	}

	// Every workload and every config object in the namespace, serialized:
	// none names the Secret, and none but the Secret holds the seed.
	var lists = []client.ObjectList{
		&appsv1.DeploymentList{}, &appsv1.StatefulSetList{}, &batchv1.JobList{}, &corev1.PodList{},
		&corev1.ConfigMapList{}, &corev1.SecretList{},
	}
	for _, list := range lists {
		if err := cl.List(ctx, list, client.InNamespace("test-ns")); err != nil {
			t.Fatalf("list %T: %v", list, err)
		}
		raw, err := json.Marshal(list)
		if err != nil {
			t.Fatal(err)
		}
		if _, isSecrets := list.(*corev1.SecretList); isSecrets {
			for _, s := range list.(*corev1.SecretList).Items {
				if s.Name == secretName {
					continue
				}
				// Data marshals as base64, so the values are read raw.
				for k, v := range s.Data {
					if strings.Contains(string(v), key.Seed) || strings.Contains(string(v), secretName) {
						t.Errorf("Secret %s key %s carries the task-signing seed or names its Secret", s.Name, k)
					}
				}
			}
			continue
		}
		if strings.Contains(string(raw), secretName) {
			t.Errorf("a rendered %T references %s; only the dispatcher may mount it", list, secretName)
		}
		if strings.Contains(string(raw), key.Seed) {
			t.Errorf("a rendered %T carries the task-signing seed", list)
		}
	}
}
