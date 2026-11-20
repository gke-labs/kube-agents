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
	"maps"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
)

type fakeBrokerPodServer struct {
	mu       sync.Mutex
	state    string
	warm     bool
	inFlight int
	legacy   bool
	wantAuth string
	calls    []string
	server   *httptest.Server
	port     string
	host     string
}

func newFakeBrokerPodServer(wantAuth, initialState string, warm bool, inFlight int, legacy bool) *fakeBrokerPodServer {
	f := &fakeBrokerPodServer{
		state:    initialState,
		warm:     warm,
		inFlight: inFlight,
		legacy:   legacy,
		wantAuth: wantAuth,
	}
	f.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if f.legacy {
			http.NotFound(w, r)
			return
		}
		if r.Header.Get("Authorization") != "Bearer "+f.wantAuth {
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte(`{"error":"unauthorized"}`))
			return
		}
		f.mu.Lock()
		defer f.mu.Unlock()
		f.calls = append(f.calls, r.Method+" "+r.URL.Path)
		switch r.Method + " " + r.URL.Path {
		case "GET " + credentialProxyControlStatusPath:
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(brokerControlSnapshot{
				State:    f.state,
				Warm:     f.warm,
				Enabled:  true,
				InFlight: f.inFlight,
			})
		case "POST " + credentialProxyControlActivatePath:
			if f.state == credentialProxyStateDrained || f.state == credentialProxyStateDraining {
				w.WriteHeader(http.StatusConflict)
				_ = json.NewEncoder(w).Encode(brokerControlSnapshot{State: f.state})
				return
			}
			f.state = credentialProxyStateActive
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(brokerControlSnapshot{State: f.state, Warm: f.warm, Enabled: true})
		case "POST " + credentialProxyControlDrainPath:
			if f.inFlight == 0 {
				f.state = credentialProxyStateDrained
			} else {
				f.state = credentialProxyStateDraining
			}
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(brokerControlSnapshot{
				State:    f.state,
				Warm:     f.warm,
				Enabled:  true,
				InFlight: f.inFlight,
			})
		default:
			http.NotFound(w, r)
		}
	}))
	u, _ := url.Parse(f.server.URL)
	f.host = u.Hostname()
	f.port = u.Port()
	return f
}

func (f *fakeBrokerPodServer) close() {
	f.server.Close()
}

func (f *fakeBrokerPodServer) finishDrain() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.inFlight = 0
	f.state = credentialProxyStateDrained
}

func (f *fakeBrokerPodServer) getCalls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...)
}

func (f *fakeBrokerPodServer) currentState() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.state
}

func TestCredentialProxyControlSecretLifecycle(t *testing.T) {
	t.Parallel()
	scheme := setupScheme()
	agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil)
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}

	if err := r.ensureCredentialProxyControlSecret(context.Background(), agent); err != nil {
		t.Fatalf("first ensure: %v", err)
	}
	sec := &corev1.Secret{}
	key := types.NamespacedName{Namespace: agent.Namespace, Name: credentialProxyControlSecretName(agent)}
	if err := cl.Get(context.Background(), key, sec); err != nil {
		t.Fatalf("Secret not created: %v", err)
	}
	firstToken := append([]byte(nil), sec.Data[credentialProxyControlDataKey]...)
	if len(firstToken) == 0 {
		t.Fatal("control Secret data empty")
	}
	if !metav1.IsControlledBy(sec, agent) {
		t.Fatal("control Secret missing PlatformAgent controller ownerReference")
	}

	// Second call keeps identical token without rotating/re-writing.
	if err := r.ensureCredentialProxyControlSecret(context.Background(), agent); err != nil {
		t.Fatalf("second ensure: %v", err)
	}
	sec2 := &corev1.Secret{}
	if err := cl.Get(context.Background(), key, sec2); err != nil {
		t.Fatal(err)
	}
	if string(sec2.Data[credentialProxyControlDataKey]) != string(firstToken) {
		t.Fatalf("token rotated unexpectedly on idempotent ensure")
	}

	// Unowned pre-existing Secret is refused rather than overwritten.
	foreignAgent := sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil)
	foreignAgent.Name = "foreign"
	foreignSec := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: foreignAgent.Namespace,
			Name:      credentialProxyControlSecretName(foreignAgent),
		},
	}
	cl2 := fake.NewClientBuilder().WithScheme(scheme).WithObjects(foreignAgent, foreignSec).Build()
	r2 := &PlatformAgentReconciler{Client: cl2, APIReader: cl2, Scheme: scheme}
	if err := r2.ensureCredentialProxyControlSecret(context.Background(), foreignAgent); err == nil {
		t.Fatal("expected error when control Secret is not owned by PlatformAgent")
	}
}

func TestCredentialProxyHandoffZeroGapActiveToStandbyAndLegacy(t *testing.T) {
	t.Parallel()

	t.Run("old pod drains while busy before warm standby pod activates and takes lease", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, nil, nil)
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).Build()
		rSeed := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
		if err := rSeed.ensureCredentialProxyControlSecret(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		bearer, ok, err := rSeed.readCredentialProxyControlBearer(context.Background(), agent)
		if err != nil || !ok {
			t.Fatalf("bearer read failed: ok=%v err=%v", ok, err)
		}

		oldSrv := newFakeBrokerPodServer(bearer, credentialProxyStateActive, true, 2, false)
		defer oldSrv.close()
		newSrv := newFakeBrokerPodServer(bearer, credentialProxyStateStandby, true, 0, false)
		defer newSrv.close()

		// Route unique synthetic PodIPs 127.0.0.10:8766 / 127.0.0.11:8766 to each test server.
		portMap := map[string]string{
			"127.0.0.10": oldSrv.host + ":" + oldSrv.port,
			"127.0.0.11": newSrv.host + ":" + newSrv.port,
		}
		rt := roundTripFunc(func(req *http.Request) (*http.Response, error) {
			cloned := req.Clone(req.Context())
			if target, found := portMap[req.URL.Hostname()]; found {
				cloned.URL.Host = target
			}
			return http.DefaultTransport.RoundTrip(cloned)
		})

		targetDep := buildCredentialProxyDeployment(agent, "hash-medium")
		tOld := metav1.NewTime(time.Now().Add(-10 * time.Minute))
		tNew := metav1.NewTime(time.Now())
		oldDep := buildCredentialProxyDeployment(sizingAgent(sizingProfileSmall, sizingProfileSmall, nil, nil), "hash-small")
		oldPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-old",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				Annotations:       maps.Clone(oldDep.Spec.Template.Annotations),
				CreationTimestamp: tOld,
			},
			Spec: *oldDep.Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{
				Phase: corev1.PodRunning,
				PodIP: "127.0.0.10",
				Conditions: []corev1.PodCondition{{
					Type:   corev1.PodReady,
					Status: corev1.ConditionTrue,
				}},
			},
		}
		newPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:              credentialProxyName(agent) + "-new",
				Namespace:         agent.Namespace,
				Labels:            credentialProxySelector(agent),
				Annotations:       maps.Clone(targetDep.Spec.Template.Annotations),
				CreationTimestamp: tNew,
			},
			Spec: *targetDep.Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{
				Phase: corev1.PodRunning,
				PodIP: "127.0.0.11",
			},
		}

		// Poison cached client for Pod/Lease reads to prove live APIReader is used.
		var cachedReads int
		poisonedCache := interceptor.NewClient(cl, interceptor.Funcs{
			Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
				if _, isLease := obj.(*coordinationv1.Lease); isLease {
					cachedReads++
				}
				return c.Get(ctx, key, obj, opts...)
			},
			List: func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
				if _, isPodList := list.(*corev1.PodList); isPodList {
					cachedReads++
				}
				return c.List(ctx, list, opts...)
			},
		})
		if err := cl.Create(context.Background(), oldPod); err != nil {
			t.Fatal(err)
		}
		if err := cl.Create(context.Background(), newPod); err != nil {
			t.Fatal(err)
		}

		r := &PlatformAgentReconciler{
			Client:              poisonedCache,
			APIReader:           cl,
			Scheme:              scheme,
			BrokerControlClient: &http.Client{Transport: rt, Timeout: 5 * time.Second},
		}

		// Pass 1: OldPod begins draining (2 in-flight), NewPod stays standby until OldPod drains.
		req1, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, targetDep)
		if err != nil {
			t.Fatalf("pass 1 handoff: %v", err)
		}
		if req1 != credentialProxyControlRequeueInterval {
			t.Fatalf("pass 1 requeue = %v, want %v while old pod drains", req1, credentialProxyControlRequeueInterval)
		}
		if oldSrv.currentState() != credentialProxyStateDraining {
			t.Fatalf("oldSrv state = %q, want draining", oldSrv.currentState())
		}
		if newSrv.currentState() != credentialProxyStateStandby {
			t.Fatalf("newSrv state = %q, want standby while old pod is still draining", newSrv.currentState())
		}
		if cachedReads != 0 {
			t.Fatalf("expected 0 cached Pod/Lease reads, got %d", cachedReads)
		}

		// Finish drain on OldPod -> Pass 2 acquires Lease for NewPod and activates NewPod.
		oldSrv.finishDrain()
		req2, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, targetDep)
		if err != nil {
			t.Fatalf("pass 2 handoff: %v", err)
		}
		if req2 != credentialProxyControlRequeueInterval {
			t.Fatalf("pass 2 requeue = %v, want %v", req2, credentialProxyControlRequeueInterval)
		}
		if newSrv.currentState() != credentialProxyStateActive {
			t.Fatalf("newSrv state = %q, want active after oldSrv drained", newSrv.currentState())
		}

		lease := &coordinationv1.Lease{}
		if err := cl.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: credentialProxyLeaseName(agent)}, lease); err != nil {
			t.Fatalf("Lease not created: %v", err)
		}
		if ptr.Deref(lease.Spec.HolderIdentity, "") != newPod.Name {
			t.Fatalf("Lease holder = %q, want %q", ptr.Deref(lease.Spec.HolderIdentity, ""), newPod.Name)
		}
		if lease.Labels[credentialProxyLeaseLabel] != credentialProxyLeaseLabelValue {
			t.Fatalf("Lease missing %s label: %+v", credentialProxyLeaseLabel, lease.Labels)
		}

		// Delete old pod -> Pass 3 settles cleanly with zero requeue.
		if err := cl.Delete(context.Background(), oldPod); err != nil {
			t.Fatal(err)
		}
		req3, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, targetDep)
		if err != nil {
			t.Fatalf("pass 3 handoff: %v", err)
		}
		if req3 != 0 {
			t.Fatalf("pass 3 requeue = %v, want 0 when single active pod matches template", req3)
		}
	})

	t.Run("not-yet-warm standby pod does not drain active pod", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, nil, nil)
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).Build()
		rSeed := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
		if err := rSeed.ensureCredentialProxyControlSecret(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		bearer, _, _ := rSeed.readCredentialProxyControlBearer(context.Background(), agent)

		oldSrv := newFakeBrokerPodServer(bearer, credentialProxyStateActive, true, 0, false)
		defer oldSrv.close()
		coldNewSrv := newFakeBrokerPodServer(bearer, credentialProxyStateStandby, false, 0, false)
		defer coldNewSrv.close()

		portMap := map[string]string{
			"127.0.0.12": oldSrv.host + ":" + oldSrv.port,
			"127.0.0.13": coldNewSrv.host + ":" + coldNewSrv.port,
		}
		rt := roundTripFunc(func(req *http.Request) (*http.Response, error) {
			cloned := req.Clone(req.Context())
			if target, found := portMap[req.URL.Hostname()]; found {
				cloned.URL.Host = target
			}
			return http.DefaultTransport.RoundTrip(cloned)
		})

		targetDep := buildCredentialProxyDeployment(agent, "hash-medium")
		oldDep := buildCredentialProxyDeployment(sizingAgent(sizingProfileSmall, sizingProfileSmall, nil, nil), "hash-small")
		oldPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:        credentialProxyName(agent) + "-old",
				Namespace:   agent.Namespace,
				Labels:      credentialProxySelector(agent),
				Annotations: maps.Clone(oldDep.Spec.Template.Annotations),
			},
			Spec:   *oldDep.Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "127.0.0.12"},
		}
		newPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:        credentialProxyName(agent) + "-new",
				Namespace:   agent.Namespace,
				Labels:      credentialProxySelector(agent),
				Annotations: maps.Clone(targetDep.Spec.Template.Annotations),
			},
			Spec:   *targetDep.Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "127.0.0.13"},
		}
		_ = cl.Create(context.Background(), oldPod)
		_ = cl.Create(context.Background(), newPod)

		r := &PlatformAgentReconciler{
			Client:              cl,
			APIReader:           cl,
			Scheme:              scheme,
			BrokerControlClient: &http.Client{Transport: rt, Timeout: 5 * time.Second},
		}
		req, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, targetDep)
		if err != nil {
			t.Fatal(err)
		}
		if req != credentialProxyControlRequeueInterval {
			t.Fatalf("requeue = %v, want %v while cold standby pod warms", req, credentialProxyControlRequeueInterval)
		}
		if oldSrv.currentState() != credentialProxyStateActive {
			t.Fatalf("oldSrv was drained (%q) before new pod finished warming!", oldSrv.currentState())
		}
	})

	t.Run("legacy 404 pod is retired before warm standby pod activates", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, nil, nil)
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).Build()
		rSeed := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
		if err := rSeed.ensureCredentialProxyControlSecret(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		bearer, _, _ := rSeed.readCredentialProxyControlBearer(context.Background(), agent)

		legacySrv := newFakeBrokerPodServer(bearer, "", false, 0, true)
		defer legacySrv.close()
		newSrv := newFakeBrokerPodServer(bearer, credentialProxyStateStandby, true, 0, false)
		defer newSrv.close()

		portMap := map[string]string{
			"127.0.0.14": legacySrv.host + ":" + legacySrv.port,
			"127.0.0.15": newSrv.host + ":" + newSrv.port,
		}
		rt := roundTripFunc(func(req *http.Request) (*http.Response, error) {
			cloned := req.Clone(req.Context())
			if target, found := portMap[req.URL.Hostname()]; found {
				cloned.URL.Host = target
			}
			return http.DefaultTransport.RoundTrip(cloned)
		})

		targetDep := buildCredentialProxyDeployment(agent, "hash-medium")
		legacyPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:       credentialProxyName(agent) + "-legacy",
				Namespace:  agent.Namespace,
				Labels:     credentialProxySelector(agent),
				Finalizers: []string{"test.kubeagents.x-k8s.io/hold"},
			},
			Spec: buildCredentialProxyDeployment(sizingAgent(sizingProfileSmall, sizingProfileSmall, nil, nil), "hash-small").Spec.Template.Spec,
			Status: corev1.PodStatus{
				Phase: corev1.PodRunning,
				PodIP: "127.0.0.14",
				Conditions: []corev1.PodCondition{{
					Type:   corev1.PodReady,
					Status: corev1.ConditionTrue,
				}},
			},
		}
		newPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:        credentialProxyName(agent) + "-new",
				Namespace:   agent.Namespace,
				Labels:      credentialProxySelector(agent),
				Annotations: maps.Clone(targetDep.Spec.Template.Annotations),
			},
			Spec:   *targetDep.Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "127.0.0.15"},
		}
		_ = cl.Create(context.Background(), legacyPod)
		_ = cl.Create(context.Background(), newPod)

		r := &PlatformAgentReconciler{
			Client:              cl,
			APIReader:           cl,
			Scheme:              scheme,
			BrokerControlClient: &http.Client{Transport: rt, Timeout: 5 * time.Second},
		}

		// Pass 1: legacy Ready pod gets Delete requested; new pod stays standby while legacy is Ready.
		req1, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, targetDep)
		if err != nil {
			t.Fatal(err)
		}
		if req1 != credentialProxyControlRequeueInterval {
			t.Fatalf("requeue = %v, want %v", req1, credentialProxyControlRequeueInterval)
		}
		if newSrv.currentState() != credentialProxyStateStandby {
			t.Fatalf("newSrv activated prematurely (%q) while legacy Ready pod existed", newSrv.currentState())
		}
		storedLegacy := &corev1.Pod{}
		if err := cl.Get(context.Background(), client.ObjectKeyFromObject(legacyPod), storedLegacy); err != nil {
			t.Fatal(err)
		}
		if storedLegacy.DeletionTimestamp == nil {
			t.Fatalf("expected legacy pod to be deleted for cutover")
		}

		// Pass 2 while legacy pod still has DeletionTimestamp and Phase=PodRunning:
		// must NOT activate NewPod prematurely.
		reqHold, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, targetDep)
		if err != nil {
			t.Fatal(err)
		}
		if reqHold != credentialProxyControlRequeueInterval {
			t.Fatalf("pass 2 requeue = %v, want %v while deleting legacy pod still Running", reqHold, credentialProxyControlRequeueInterval)
		}
		if newSrv.currentState() != credentialProxyStateStandby {
			t.Fatalf("newSrv activated prematurely (%q) while deleting legacy pod was still Running", newSrv.currentState())
		}

		// Remove finalizer so legacy pod disappears -> Pass 3 activates NewPod!
		storedLegacy.Finalizers = nil
		_ = cl.Update(context.Background(), storedLegacy)
		req2, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, targetDep)
		if err != nil {
			t.Fatal(err)
		}
		if req2 == 0 || newSrv.currentState() != credentialProxyStateActive {
			t.Fatalf("expected NewPod activation after legacy pod removal; req=%v state=%q", req2, newSrv.currentState())
		}
	})

	t.Run("non-resource rollout with LimitRanger pod mutation cuts over and rollback ignores drained pod", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		agent := sizingAgent(sizingProfileSmall, sizingProfileSmall, nil, nil)
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).Build()
		rSeed := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
		if err := rSeed.ensureCredentialProxyControlSecret(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		bearer, _, _ := rSeed.readCredentialProxyControlBearer(context.Background(), agent)

		oldSrv := newFakeBrokerPodServer(bearer, credentialProxyStateActive, true, 0, false)
		defer oldSrv.close()
		newSrv := newFakeBrokerPodServer(bearer, credentialProxyStateStandby, true, 0, false)
		defer newSrv.close()

		portMap := map[string]string{
			"127.0.0.16": oldSrv.host + ":" + oldSrv.port,
			"127.0.0.17": newSrv.host + ":" + newSrv.port,
		}
		rt := roundTripFunc(func(req *http.Request) (*http.Response, error) {
			cloned := req.Clone(req.Context())
			if target, found := portMap[req.URL.Hostname()]; found {
				cloned.URL.Host = target
			}
			return http.DefaultTransport.RoundTrip(cloned)
		})

		oldDep := buildCredentialProxyDeployment(agent, "policy-v1")
		newDep := buildCredentialProxyDeployment(agent, "policy-v2")
		newDep.Spec.Template.Annotations[kubectlRestartedAtAnnotation] = "2026-11-19T05:00:00Z"

		// Simulate LimitRanger mutating requests.ephemeral-storage on Pod creation.
		mutateWithLimitRanger := func(pod *corev1.Pod) {
			for i := range pod.Spec.Containers {
				if pod.Spec.Containers[i].Name == credentialProxyContainerName {
					if pod.Spec.Containers[i].Resources.Requests == nil {
						pod.Spec.Containers[i].Resources.Requests = corev1.ResourceList{}
					}
					pod.Spec.Containers[i].Resources.Requests[corev1.ResourceEphemeralStorage] = resource.MustParse("1Gi")
				}
			}
		}

		oldPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:        credentialProxyName(agent) + "-v1",
				Namespace:   agent.Namespace,
				Labels:      credentialProxySelector(agent),
				Annotations: maps.Clone(oldDep.Spec.Template.Annotations),
			},
			Spec:   *oldDep.Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "127.0.0.16"},
		}
		mutateWithLimitRanger(oldPod)

		newPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:        credentialProxyName(agent) + "-v2",
				Namespace:   agent.Namespace,
				Labels:      credentialProxySelector(agent),
				Annotations: maps.Clone(newDep.Spec.Template.Annotations),
			},
			Spec:   *newDep.Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "127.0.0.17"},
		}
		mutateWithLimitRanger(newPod)

		_ = cl.Create(context.Background(), oldPod)
		_ = cl.Create(context.Background(), newPod)
		r := &PlatformAgentReconciler{
			Client:              cl,
			APIReader:           cl,
			Scheme:              scheme,
			BrokerControlClient: &http.Client{Transport: rt, Timeout: 5 * time.Second},
		}

		req, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, newDep)
		if err != nil {
			t.Fatal(err)
		}
		if req != credentialProxyControlRequeueInterval {
			t.Fatalf("requeue = %v, want %v", req, credentialProxyControlRequeueInterval)
		}
		if oldSrv.currentState() != credentialProxyStateDrained || newSrv.currentState() != credentialProxyStateActive {
			t.Fatalf("states old=%q new=%q, want drained/active on non-resource template roll", oldSrv.currentState(), newSrv.currentState())
		}

		// Fast rollback while drained oldPod is still in the PodList:
		// must NOT select or re-activate oldPod (which is drained) or drain newPod.
		reqBack, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, oldDep)
		if err != nil {
			t.Fatal(err)
		}
		if reqBack == 0 {
			t.Fatalf("expected requeue while drained oldPod still exists alongside active newPod")
		}
		if newSrv.currentState() != credentialProxyStateActive {
			t.Fatalf("newSrv state = %q after rollback pass before replacement pod exists, want active", newSrv.currentState())
		}

		// Once an active replacement pod is Ready, any stale non-terminating drained
		// pod is explicitly deleted so fast rollbacks cannot leave a drained pod Ready.
		var latestNew corev1.Pod
		if err := cl.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: newPod.Name}, &latestNew); err != nil {
			t.Fatal(err)
		}
		latestNew.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}
		if err := cl.Status().Update(context.Background(), &latestNew); err != nil {
			t.Fatal(err)
		}
		reqCleanup, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, newDep)
		if err != nil {
			t.Fatal(err)
		}
		if reqCleanup != credentialProxyControlRequeueInterval {
			t.Fatalf("cleanup requeue = %v, want %v", reqCleanup, credentialProxyControlRequeueInterval)
		}
		var drainedCheck corev1.Pod
		err = cl.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: oldPod.Name}, &drainedCheck)
		if err == nil && drainedCheck.DeletionTimestamp == nil {
			t.Fatalf("expected stale drained pod %s to be deleted once active pod is Ready", oldPod.Name)
		}
	})

	t.Run("rollback after drain with no active candidate deletes drained pod instead of deadlocking", func(t *testing.T) {
		t.Parallel()
		scheme := setupScheme()
		agent := sizingAgent(sizingProfileSmall, sizingProfileSmall, nil, nil)
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).Build()
		rSeed := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
		if err := rSeed.ensureCredentialProxyControlSecret(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
		bearer, ok, err := rSeed.readCredentialProxyControlBearer(context.Background(), agent)
		if err != nil || !ok {
			t.Fatalf("bearer read failed: ok=%v err=%v", ok, err)
		}

		oldSrv := newFakeBrokerPodServer(bearer, credentialProxyStateDraining, true, 1, false)
		defer oldSrv.close()
		rt := roundTripFunc(func(req *http.Request) (*http.Response, error) {
			cloned := req.Clone(req.Context())
			if req.URL.Hostname() == "127.0.0.18" {
				cloned.URL.Host = oldSrv.host + ":" + oldSrv.port
			}
			return http.DefaultTransport.RoundTrip(cloned)
		})

		revertedDep := buildCredentialProxyDeployment(agent, brokerTestPolicyHash)
		newDep := buildCredentialProxyDeployment(sizingAgent(sizingProfileMedium, sizingProfileMedium, nil, nil), brokerTestPolicyHash)
		// OldPod matches the reverted template (Deployment will NOT replace it while it looks Ready),
		// while the rolled-back surge pod has DeletionTimestamp set.
		oldPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:        credentialProxyName(agent) + "-old",
				Namespace:   agent.Namespace,
				Labels:      credentialProxySelector(agent),
				Annotations: maps.Clone(revertedDep.Spec.Template.Annotations),
			},
			Spec:   *revertedDep.Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "127.0.0.18"},
		}
		terminatingNewPod := &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:        credentialProxyName(agent) + "-new",
				Namespace:   agent.Namespace,
				Labels:      credentialProxySelector(agent),
				Annotations: maps.Clone(newDep.Spec.Template.Annotations),
				Finalizers:  []string{"test/hold"},
			},
			Spec:   *newDep.Spec.Template.Spec.DeepCopy(),
			Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "127.0.0.19"},
		}
		if err := cl.Create(context.Background(), oldPod); err != nil {
			t.Fatal(err)
		}
		if err := cl.Create(context.Background(), terminatingNewPod); err != nil {
			t.Fatal(err)
		}
		if err := cl.Delete(context.Background(), terminatingNewPod); err != nil {
			t.Fatal(err)
		}
		r := &PlatformAgentReconciler{
			Client:              cl,
			APIReader:           cl,
			Scheme:              scheme,
			BrokerControlClient: &http.Client{Transport: rt, Timeout: 5 * time.Second},
		}

		// Pass 1: while still DRAINING, requeues without deleting in-flight pod.
		req1, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, revertedDep)
		if err != nil || req1 != credentialProxyControlRequeueInterval {
			t.Fatalf("while draining: req=%v err=%v", req1, err)
		}
		var stillThere corev1.Pod
		if err := cl.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: oldPod.Name}, &stillThere); err != nil || stillThere.DeletionTimestamp != nil {
			t.Fatalf("pod should stay alive while DRAINING: err=%v del=%v", err, stillThere.DeletionTimestamp)
		}

		// Pass 2: once DRAINED with no runnable candidate, oldPod is deleted to unblock Deployment.
		oldSrv.finishDrain()
		req2, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, revertedDep)
		if err != nil || req2 != credentialProxyControlRequeueInterval {
			t.Fatalf("once drained: req=%v err=%v", req2, err)
		}
		var afterDrain corev1.Pod
		err = cl.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: oldPod.Name}, &afterDrain)
		if err == nil && afterDrain.DeletionTimestamp == nil {
			t.Fatalf("expected matching DRAINED pod %s with no candidate to be deleted", oldPod.Name)
		}
	})
}

func TestCredentialProxyOffClusterOperatorSkipsStandbyAndHandoff(t *testing.T) {
	t.Parallel()

	for _, tc := range []struct {
		name              string
		operatorNamespace string
		wantStandby       bool
	}{
		{name: "unset operator namespace", operatorNamespace: "", wantStandby: false},
		{name: "invalid DNS-1123 operator namespace", operatorNamespace: "Bad_Namespace", wantStandby: false},
		{name: "valid in-cluster operator namespace", operatorNamespace: "kubeagents-system", wantStandby: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			scheme := setupScheme()
			agent := sizingAgent(sizingProfileSmall, sizingProfileSmall, nil, nil)
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(agent).
				WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
				Build()
			r := &PlatformAgentReconciler{
				Client:            cl,
				APIReader:         cl,
				Scheme:            scheme,
				OperatorNamespace: tc.operatorNamespace,
			}

			req, err := r.reconcileCredentialProxy(context.Background(), agent, brokerTestPolicyHash, nil, allBrokerCallersOwned)
			if err != nil {
				t.Fatalf("reconcileCredentialProxy: %v", err)
			}
			dep := &appsv1.Deployment{}
			if err := cl.Get(context.Background(), types.NamespacedName{Name: credentialProxyName(agent), Namespace: agent.Namespace}, dep); err != nil {
				t.Fatalf("reading proxy Deployment: %v", err)
			}
			hasStandby := false
			for _, env := range dep.Spec.Template.Spec.Containers[0].Env {
				if env.Name == credentialProxyStandbyEnv && env.Value == credentialProxyStandbyEnabledValue {
					hasStandby = true
				}
			}
			if hasStandby != tc.wantStandby {
				t.Fatalf("CREDENTIAL_PROXY_STANDBY=%v, want %v (Strategy=%s)", hasStandby, tc.wantStandby, dep.Spec.Strategy.Type)
			}
			wantStrategy := appsv1.RecreateDeploymentStrategyType
			if tc.wantStandby {
				wantStrategy = appsv1.RollingUpdateDeploymentStrategyType
			}
			if dep.Spec.Strategy.Type != wantStrategy {
				t.Fatalf("Deployment strategy = %s, want %s", dep.Spec.Strategy.Type, wantStrategy)
			}

			// When off-cluster (!tc.wantStandby), even a live pod must not trigger
			// 2s handoff requeues or standby probing; in-cluster with a valid
			// namespace proceeds to probe the standby pod via BrokerControlClient.
			livePod := &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Name:        credentialProxyName(agent) + "-live",
					Namespace:   agent.Namespace,
					Labels:      credentialProxySelector(agent),
					Annotations: maps.Clone(dep.Spec.Template.Annotations),
				},
				Spec:   *dep.Spec.Template.Spec.DeepCopy(),
				Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "127.0.0.21"},
			}
			if err := cl.Create(context.Background(), livePod); err != nil {
				t.Fatal(err)
			}
			if tc.wantStandby {
				if !r.canControlBroker() {
					t.Fatalf("expected canControlBroker() == true for valid operatorNamespace %q", tc.operatorNamespace)
				}
				bearer, _, err := r.readCredentialProxyControlBearer(context.Background(), agent)
				if err != nil {
					t.Fatal(err)
				}
				srv := newFakeBrokerPodServer(bearer, credentialProxyStateStandby, true, 0, false)
				defer srv.close()
				rt := roundTripFunc(func(httpReq *http.Request) (*http.Response, error) {
					cloned := httpReq.Clone(httpReq.Context())
					if httpReq.URL.Hostname() == "127.0.0.21" {
						cloned.URL.Host = srv.host + ":" + srv.port
					}
					return http.DefaultTransport.RoundTrip(cloned)
				})
				r.BrokerControlClient = &http.Client{Transport: rt, Timeout: 5 * time.Second}
				handoffReq, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, dep)
				if err != nil {
					t.Fatalf("reconcileCredentialProxyHandoff: %v", err)
				}
				if handoffReq != credentialProxyControlRequeueInterval {
					t.Fatalf("handoff requeue = %v, want %v", handoffReq, credentialProxyControlRequeueInterval)
				}
				if srv.currentState() != credentialProxyStateActive {
					t.Fatalf("in-cluster warm standby pod state = %q, want active", srv.currentState())
				}
			} else {
				if r.canControlBroker() {
					t.Fatalf("expected canControlBroker() == false for off-cluster operatorNamespace %q", tc.operatorNamespace)
				}
				handoffReq, err := r.reconcileCredentialProxyHandoff(context.Background(), agent, dep)
				if err != nil {
					t.Fatalf("reconcileCredentialProxyHandoff: %v", err)
				}
				if handoffReq != 0 || req != 0 {
					t.Fatalf("off-cluster requeues handoff=%v initial=%v, want 0", handoffReq, req)
				}
			}
		})
	}
}
