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
	"errors"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// errForbidden stands in for the RBAC denial a restricted install would return: not a
// NotFound, so the probe must treat it as inconclusive rather than as "no collector".
func errForbidden() error {
	return apierrors.NewForbidden(schema.GroupResource{Resource: "services"}, "otel-collector", errors.New("no permission"))
}

// noReadsAllowed fails the test if discovery touches the API server at all. Used to prove
// that an explicitly configured rung short-circuits the probe.
func noReadsAllowed(t *testing.T) interceptor.Funcs {
	t.Helper()
	return interceptor.Funcs{
		Get: func(_ context.Context, _ client.WithWatch, key client.ObjectKey, _ client.Object, _ ...client.GetOption) error {
			t.Errorf("unexpected Get for %s: the configured endpoint should short-circuit discovery", key)
			return nil
		},
		List: func(_ context.Context, _ client.WithWatch, _ client.ObjectList, _ ...client.ListOption) error {
			t.Errorf("unexpected List: the configured endpoint should short-circuit discovery")
			return nil
		},
	}
}

// collectorService builds a Service exposing a single named port.
func collectorService(namespace, name, portName string, port int32, labels map[string]string) *corev1.Service {
	return &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name, Labels: labels},
		Spec: corev1.ServiceSpec{
			Ports: []corev1.ServicePort{{Name: portName, Port: port, Protocol: corev1.ProtocolTCP}},
		},
	}
}

func TestOTLPHTTPEndpointForService(t *testing.T) {
	tests := []struct {
		name   string
		svc    *corev1.Service
		want   string
		wantOK bool
	}{
		{
			name:   "nil service",
			svc:    nil,
			wantOK: false,
		},
		{
			name:   "named otlp-http",
			svc:    collectorService("obs", "col", "otlp-http", 4318, nil),
			want:   "http://col.obs.svc.cluster.local:4318",
			wantOK: true,
		},
		{
			name:   "named http-otlp",
			svc:    collectorService("obs", "col", "http-otlp", 8080, nil),
			want:   "http://col.obs.svc.cluster.local:8080",
			wantOK: true,
		},
		{
			name:   "unnamed 4318",
			svc:    collectorService("obs", "col", "", 4318, nil),
			want:   "http://col.obs.svc.cluster.local:4318",
			wantOK: true,
		},
		{
			// A gRPC-only collector must be rejected: the exporters speak http/protobuf,
			// so selecting 4317 would fail on every span while looking configured.
			name:   "grpc only is rejected",
			svc:    collectorService("obs", "col", "otlp-grpc", 4317, nil),
			wantOK: false,
		},
		{
			name: "http wins over grpc",
			svc: &corev1.Service{
				ObjectMeta: metav1.ObjectMeta{Namespace: "obs", Name: "col"},
				Spec: corev1.ServiceSpec{Ports: []corev1.ServicePort{
					{Name: "otlp-grpc", Port: 4317},
					{Name: "otlp-http", Port: 4318},
				}},
			},
			want:   "http://col.obs.svc.cluster.local:4318",
			wantOK: true,
		},
		{
			name: "named port wins over the conventional number",
			svc: &corev1.Service{
				ObjectMeta: metav1.ObjectMeta{Namespace: "obs", Name: "col"},
				Spec: corev1.ServiceSpec{Ports: []corev1.ServicePort{
					{Name: "legacy", Port: 4318},
					{Name: "otlp-http", Port: 9090},
				}},
			},
			want:   "http://col.obs.svc.cluster.local:9090",
			wantOK: true,
		},
		{
			name:   "UDP port ignored",
			svc:    &corev1.Service{ObjectMeta: metav1.ObjectMeta{Namespace: "obs", Name: "col"}, Spec: corev1.ServiceSpec{Ports: []corev1.ServicePort{{Name: "otlp-http", Port: 4318, Protocol: corev1.ProtocolUDP}}}},
			wantOK: false,
		},
		{
			name: "ExternalName rejected",
			svc: &corev1.Service{
				ObjectMeta: metav1.ObjectMeta{Namespace: "obs", Name: "col"},
				Spec: corev1.ServiceSpec{
					Type:  corev1.ServiceTypeExternalName,
					Ports: []corev1.ServicePort{{Name: "otlp-http", Port: 4318}},
				},
			},
			wantOK: false,
		},
		{
			name:   "no ports",
			svc:    &corev1.Service{ObjectMeta: metav1.ObjectMeta{Namespace: "obs", Name: "col"}},
			wantOK: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, ok := otlpHTTPEndpointForService(tt.svc)
			if ok != tt.wantOK {
				t.Fatalf("expected ok=%v, got %v (endpoint %q)", tt.wantOK, ok, got)
			}
			if ok && got != tt.want {
				t.Errorf("expected %q, got %q", tt.want, got)
			}
		})
	}
}

func TestDiscoverCollectorEndpoint(t *testing.T) {
	scheme := setupScheme()
	labels := map[string]string{"app.kubernetes.io/name": "opentelemetry-collector"}

	tests := []struct {
		name    string
		objects []client.Object
		want    string
	}{
		{
			name: "no collector at all",
			want: "",
		},
		{
			name:    "managed collector",
			objects: []client.Object{collectorService("gke-managed-otel", "opentelemetry-collector", "otlp-http", 4318, nil)},
			want:    managedOTelEndpoint,
		},
		{
			name:    "well-known custom collector",
			objects: []client.Object{collectorService("otel-collector", "otel-collector", "otlp-http", 4318, nil)},
			want:    "http://otel-collector.otel-collector.svc.cluster.local:4318",
		},
		{
			// Well-known names are probed before labels, and the managed collector is
			// probed first, so a GKE cluster keeps the endpoint it always had.
			name: "managed collector wins over a labelled custom one",
			objects: []client.Object{
				collectorService("gke-managed-otel", "opentelemetry-collector", "otlp-http", 4318, nil),
				collectorService("custom", "collector", "otlp-http", 4318, labels),
			},
			want: managedOTelEndpoint,
		},
		{
			name:    "label fallback",
			objects: []client.Object{collectorService("custom", "collector", "otlp-http", 4318, labels)},
			want:    "http://collector.custom.svc.cluster.local:4318",
		},
		{
			name:    "label match with no HTTP port is skipped",
			objects: []client.Object{collectorService("custom", "collector", "otlp-grpc", 4317, labels)},
			want:    "",
		},
		{
			name: "alternate label selector",
			objects: []client.Object{
				collectorService("custom", "collector", "otlp-http", 4318, map[string]string{"app": "opentelemetry-collector"}),
			},
			want: "http://collector.custom.svc.cluster.local:4318",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(tt.objects...).Build()

			endpoint, determined := discoverCollectorEndpoint(context.Background(), cl)
			if !determined {
				t.Fatalf("expected an authoritative probe, got determined=false")
			}
			if endpoint != tt.want {
				t.Errorf("expected %q, got %q", tt.want, endpoint)
			}
		})
	}
}

// TestDiscoverCollectorEndpointIsDeterministic pins the tie-break. A non-deterministic
// pick would rewrite the agent Deployment's env on alternating reconciles and roll the
// pod forever, so the lowest (namespace, name) must win every single time.
func TestDiscoverCollectorEndpointIsDeterministic(t *testing.T) {
	scheme := setupScheme()
	labels := map[string]string{"app.kubernetes.io/name": "opentelemetry-collector"}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(
		collectorService("zzz", "collector", "otlp-http", 4318, labels),
		collectorService("aaa", "collector", "otlp-http", 4318, labels),
		collectorService("mmm", "collector", "otlp-http", 4318, labels),
	).Build()

	const want = "http://collector.aaa.svc.cluster.local:4318"
	for i := range 5 {
		endpoint, determined := discoverCollectorEndpoint(context.Background(), cl)
		if !determined || endpoint != want {
			t.Fatalf("run %d: expected %q (determined), got %q (determined=%v)", i, want, endpoint, determined)
		}
	}
}

// TestDiscoverCollectorEndpointInconclusive covers the ("", false) contract: a probe that
// could not complete must not be mistaken for "there is no collector".
func TestDiscoverCollectorEndpointInconclusive(t *testing.T) {
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithInterceptorFuncs(interceptor.Funcs{
		Get: func(_ context.Context, _ client.WithWatch, _ client.ObjectKey, _ client.Object, _ ...client.GetOption) error {
			return errForbidden()
		},
	}).Build()

	if endpoint, determined := discoverCollectorEndpoint(context.Background(), cl); determined || endpoint != "" {
		t.Errorf("expected an inconclusive probe, got %q (determined=%v)", endpoint, determined)
	}

	if endpoint, determined := discoverCollectorEndpoint(context.Background(), nil); determined || endpoint != "" {
		t.Errorf("expected a nil reader to be inconclusive, got %q (determined=%v)", endpoint, determined)
	}
}

// expireOTelCache makes the next call probe again. Both stamps have to move: the TTL is
// what makes the answer stale, and the retry floor is what would otherwise swallow the
// probe that staleness is supposed to trigger.
func expireOTelCache(r *PlatformAgentReconciler) {
	past := time.Now().Add(-otelDiscoveryTTL - time.Second)
	r.otelResolvedAt = past
	r.otelProbedAt = past
}

func TestDiscoveredOTLPEndpointCaching(t *testing.T) {
	scheme := setupScheme()
	gets := 0
	cl := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(collectorService("otel-collector", "otel-collector", "otlp-http", 4318, nil)).
		WithInterceptorFuncs(interceptor.Funcs{
			Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
				gets++
				return c.Get(ctx, key, obj, opts...)
			},
		}).Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	const want = "http://otel-collector.otel-collector.svc.cluster.local:4318"

	if got, outcome := r.discoveredOTLPEndpoint(context.Background(), ""); got != want || outcome != otlpDiscoveryFound {
		t.Fatalf("expected (%q, found), got (%q, %v)", want, got, outcome)
	}
	afterFirst := gets

	if got, outcome := r.discoveredOTLPEndpoint(context.Background(), ""); got != want || outcome != otlpDiscoveryFound {
		t.Fatalf("expected the cached (%q, found), got (%q, %v)", want, got, outcome)
	}
	if gets != afterFirst {
		t.Errorf("expected the second call to be served from cache, saw %d extra reads", gets-afterFirst)
	}

	// Expire the cache: a collector can appear or move at any time, so the probe must run again.
	expireOTelCache(r)
	if got, outcome := r.discoveredOTLPEndpoint(context.Background(), ""); got != want || outcome != otlpDiscoveryFound {
		t.Fatalf("expected (%q, found) after expiry, got (%q, %v)", want, got, outcome)
	}
	if gets <= afterFirst {
		t.Errorf("expected the probe to re-run once the TTL expired")
	}
}

// TestDiscoveredOTLPEndpointCachesNegative proves "probed, found nothing" is remembered.
// It is the common case on a cluster without managed OTel, and re-probing six namespaces
// on every reconcile of every agent to re-learn it would be pure waste.
func TestDiscoveredOTLPEndpointCachesNegative(t *testing.T) {
	scheme := setupScheme()
	gets := 0
	cl := fake.NewClientBuilder().WithScheme(scheme).WithInterceptorFuncs(interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			gets++
			return c.Get(ctx, key, obj, opts...)
		},
	}).Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if got, outcome := r.discoveredOTLPEndpoint(context.Background(), ""); got != "" || outcome != otlpDiscoveryNone {
		t.Fatalf("expected (\"\", none), got (%q, %v)", got, outcome)
	}
	afterFirst := gets
	if got, outcome := r.discoveredOTLPEndpoint(context.Background(), ""); got != "" || outcome != otlpDiscoveryNone {
		t.Fatalf("expected the cached (\"\", none), got (%q, %v)", got, outcome)
	}
	if gets != afterFirst {
		t.Errorf("expected the negative result to be cached, saw %d extra reads", gets-afterFirst)
	}
}

// TestDiscoveredOTLPEndpointNoneOutlivesItsTTL is the flap guard for the negative answer,
// and the reason staleOrLastKnown prefers an expired probe to the default. Once the TTL
// lapses and the next probe cannot complete, reporting Unknown would resolve the agent to
// the managed collector, put OTEL_EXPORTER_OTLP_ENDPOINT back on the pod, roll it, and roll
// it back on the next successful probe — for a cluster that still has no collector.
func TestDiscoveredOTLPEndpointNoneOutlivesItsTTL(t *testing.T) {
	scheme := setupScheme()
	fail := false
	cl := fake.NewClientBuilder().WithScheme(scheme).WithInterceptorFuncs(interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if fail {
				return errForbidden()
			}
			return c.Get(ctx, key, obj, opts...)
		},
	}).Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if _, outcome := r.discoveredOTLPEndpoint(context.Background(), ""); outcome != otlpDiscoveryNone {
		t.Fatalf("expected none on a cluster with no collector, got %v", outcome)
	}

	fail = true
	expireOTelCache(r)
	r.otelProbedAt = time.Time{} // let the probe run rather than hit the retry floor
	if got, outcome := r.discoveredOTLPEndpoint(context.Background(), ""); got != "" || outcome != otlpDiscoveryNone {
		t.Errorf("expected the expired-but-authoritative none to survive an API error, got (%q, %v)", got, outcome)
	}
}

// TestDiscoveredOTLPEndpointKeepsLastKnownGood covers the failure mode that matters most:
// a transient API error must not flap the endpoint back to the default and roll the pod.
func TestDiscoveredOTLPEndpointKeepsLastKnownGood(t *testing.T) {
	scheme := setupScheme()
	fail := false
	cl := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(collectorService("otel-collector", "otel-collector", "otlp-http", 4318, nil)).
		WithInterceptorFuncs(interceptor.Funcs{
			Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
				if fail {
					return errForbidden()
				}
				return c.Get(ctx, key, obj, opts...)
			},
		}).Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	const want = "http://otel-collector.otel-collector.svc.cluster.local:4318"
	if got, outcome := r.discoveredOTLPEndpoint(context.Background(), ""); got != want || outcome != otlpDiscoveryFound {
		t.Fatalf("expected (%q, found), got (%q, %v)", want, got, outcome)
	}

	fail = true
	expireOTelCache(r)
	if got, outcome := r.discoveredOTLPEndpoint(context.Background(), ""); got != want || outcome != otlpDiscoveryFound {
		t.Errorf("expected the last known good (%q, found) to survive an API error, got (%q, %v)", want, got, outcome)
	}
}

// TestDiscoveredOTLPEndpointRateLimitsFailedProbes proves an inconclusive probe is not
// retried on every reconcile. Nothing is cached when a probe fails, so the TTL cannot
// throttle it, and an API outage would otherwise turn every reconcile of every agent into
// six Gets and three Lists against an API server that is already in trouble.
func TestDiscoveredOTLPEndpointRateLimitsFailedProbes(t *testing.T) {
	scheme := setupScheme()
	gets := 0
	cl := fake.NewClientBuilder().WithScheme(scheme).WithInterceptorFuncs(interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			gets++
			return errForbidden()
		},
	}).Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	// Unknown, not none: the probe never completed, so nothing was established. Reporting
	// none here would switch telemetry off across the fleet on an API outage.
	if got, outcome := r.discoveredOTLPEndpoint(context.Background(), ""); got != "" || outcome != otlpDiscoveryUnknown {
		t.Fatalf("expected (\"\", unknown), got (%q, %v)", got, outcome)
	}
	afterFirst := gets
	if afterFirst == 0 {
		t.Fatal("expected the first probe to reach the API")
	}

	if got, outcome := r.discoveredOTLPEndpoint(context.Background(), ""); got != "" || outcome != otlpDiscoveryUnknown {
		t.Fatalf("expected (\"\", unknown), got (%q, %v)", got, outcome)
	}
	if gets != afterFirst {
		t.Errorf("expected the retry floor to suppress an immediate re-probe, saw %d extra reads", gets-afterFirst)
	}

	// The floor is a delay, not a cache: once it lapses the probe must run again, since
	// a failed probe is never an answer.
	r.otelProbedAt = time.Now().Add(-otelProbeRetryAfter - time.Second)
	if got, outcome := r.discoveredOTLPEndpoint(context.Background(), ""); got != "" || outcome != otlpDiscoveryUnknown {
		t.Fatalf("expected (\"\", unknown), got (%q, %v)", got, outcome)
	}
	if gets <= afterFirst {
		t.Error("expected the probe to re-run once the retry floor lapsed")
	}

	// A rate-limited call must still serve the last known good endpoint rather than
	// falling through to the default — the floor must not reintroduce the flap.
	const known = "http://otel-collector.otel-collector.svc.cluster.local:4318"
	if got, outcome := r.discoveredOTLPEndpoint(context.Background(), known); got != known || outcome != otlpDiscoveryFound {
		t.Errorf("expected the rate-limited call to keep (%q, found), got (%q, %v)", known, got, outcome)
	}
}

// TestResolveOTLPEndpointSurvivesRestart covers the same flap across an operator restart,
// which the in-memory cache cannot: a fresh process has no last known good value, so
// without the status seed one API error on the first probe resolves every agent to the
// default, rolls its pod onto a collector that may not exist, and rolls it back when the
// next probe succeeds.
func TestResolveOTLPEndpointSurvivesRestart(t *testing.T) {
	scheme := setupScheme()
	const discovered = "http://otel-collector.otel-collector.svc.cluster.local:4318"

	cl := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(collectorService("otel-collector", "otel-collector", "otlp-http", 4318, nil)).
		WithInterceptorFuncs(interceptor.Funcs{
			Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
				return errForbidden()
			},
		}).Build()

	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "a", Namespace: "ns"}}
	agent.Status.Telemetry = agentv1alpha1.TelemetryStatus{
		OTLPEndpoint:       discovered,
		OTLPEndpointSource: otlpSourceDiscovered,
	}

	// A brand-new reconciler is the restart: no cache, and the probe fails.
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	endpoint, source := r.resolveOTLPEndpoint(context.Background(), agent)
	if endpoint != discovered || source != otlpSourceDiscovered {
		t.Errorf("expected the recorded (%q, %s) to survive a restart, got (%q, %s)",
			discovered, otlpSourceDiscovered, endpoint, source)
	}

	// Only a *discovered* endpoint is replayed. Status also records the rungs above, and
	// replaying one of those would launder a removed override into a discovery result.
	agent.Status.Telemetry.OTLPEndpointSource = otlpSourceSpec
	fresh := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	endpoint, source = fresh.resolveOTLPEndpoint(context.Background(), agent)
	if endpoint != managedOTelEndpoint || source != otlpSourceDefault {
		t.Errorf("expected a non-discovered status not to be replayed, got (%q, %s)", endpoint, source)
	}
}

func TestDiscoveredOTLPEndpointCanBeDisabled(t *testing.T) {
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(collectorService("otel-collector", "otel-collector", "otlp-http", 4318, nil)).
		WithInterceptorFuncs(noReadsAllowed(t)).Build()

	t.Setenv(otelDiscoveryEnvVar, "false")
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	// Off rather than none. Nobody probed, so nothing was established about the cluster,
	// and the documented purpose of this switch is to fall straight through to the
	// managed default — reporting none would silently disable telemetry instead.
	if got, outcome := r.discoveredOTLPEndpoint(context.Background(), ""); got != "" || outcome != otlpDiscoveryOff {
		t.Errorf("expected discovery to be disabled and report (\"\", off), got (%q, %v)", got, outcome)
	}
}

// TestResolveOTLPEndpointDiscoveryOffOverridesRecordedNone is the kill switch actually
// working. An agent that has already resolved to None is the population most likely to be
// setting OTEL_COLLECTOR_DISCOVERY=false, and replaying its recorded None over the switch
// would make the switch a no-op for exactly those agents — permanently, since nothing
// else ever clears the recorded value.
func TestResolveOTLPEndpointDiscoveryOffOverridesRecordedNone(t *testing.T) {
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).
		WithInterceptorFuncs(noReadsAllowed(t)).Build()

	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "a", Namespace: "ns"}}
	agent.Status.Telemetry = agentv1alpha1.TelemetryStatus{OTLPEndpointSource: otlpSourceNone}

	t.Setenv(otelDiscoveryEnvVar, "false")
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	endpoint, source := r.resolveOTLPEndpoint(context.Background(), agent)
	if endpoint != managedOTelEndpoint || source != otlpSourceDefault {
		t.Errorf("expected switching discovery off to return (%q, %s) even for an agent recorded as None, got (%q, %s)",
			managedOTelEndpoint, otlpSourceDefault, endpoint, source)
	}
}

// TestResolveOTLPEndpointNoCollector is #831 item 5: on a cluster with no collector the
// ladder used to hand back the GKE managed endpoint anyway, and the agent's exporter then
// retried a name that never resolves for the life of the pod.
func TestResolveOTLPEndpointNoCollector(t *testing.T) {
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "a", Namespace: "ns"}}

	endpoint, source := r.resolveOTLPEndpoint(context.Background(), agent)
	if endpoint != "" || source != otlpSourceNone {
		t.Errorf("expected (\"\", %s) when discovery finds no collector, got (%q, %s)", otlpSourceNone, endpoint, source)
	}

	// Switching discovery off is not the same statement, and must keep the old behaviour.
	t.Setenv(otelDiscoveryEnvVar, "false")
	off := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	endpoint, source = off.resolveOTLPEndpoint(context.Background(), agent)
	if endpoint != managedOTelEndpoint || source != otlpSourceDefault {
		t.Errorf("expected (%q, %s) with discovery off, got (%q, %s)",
			managedOTelEndpoint, otlpSourceDefault, endpoint, source)
	}
}

// TestResolveOTLPEndpointNoCollectorYieldsToConfiguration proves the None rung is only ever
// reached when nothing above it answered. An operator who names an endpoint on a cluster
// with no discoverable collector gets that endpoint, not a disabled SDK.
func TestResolveOTLPEndpointNoCollectorYieldsToConfiguration(t *testing.T) {
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).Build()

	const pinned = "http://collector.example.svc.cluster.local:4318"
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "a", Namespace: "ns"}}
	agent.Spec.Telemetry = &agentv1alpha1.TelemetrySpec{OTLPEndpoint: pinned}

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	endpoint, source := r.resolveOTLPEndpoint(context.Background(), agent)
	if endpoint != pinned || source != otlpSourceSpec {
		t.Errorf("expected (%q, %s), got (%q, %s)", pinned, otlpSourceSpec, endpoint, source)
	}
}

// TestUpdateStatusReadyReportsTelemetry covers the one thing status is for here: the
// endpoint alone cannot tell "we discovered the managed collector" from "we fell back to
// it", and that distinction is the whole support question when spans do not arrive.
func TestUpdateStatusReadyReportsTelemetry(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "a", Namespace: "ns"},
	}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	const endpoint = "http://otel-collector.otel-collector.svc.cluster.local:4318"
	if _, err := r.updateStatusReady(context.Background(), agent, endpoint, otlpSourceDiscovered); err != nil {
		t.Fatalf("updateStatusReady failed: %v", err)
	}

	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatalf("failed to read the agent back: %v", err)
	}
	if stored.Status.Telemetry.OTLPEndpoint != endpoint {
		t.Errorf("expected status endpoint %q, got %q", endpoint, stored.Status.Telemetry.OTLPEndpoint)
	}
	if stored.Status.Telemetry.OTLPEndpointSource != otlpSourceDiscovered {
		t.Errorf("expected source %s, got %s", otlpSourceDiscovered, stored.Status.Telemetry.OTLPEndpointSource)
	}

	// A moved endpoint must be written back: leaving it out of the change detection
	// would freeze status at the first value it ever saw.
	const moved = "http://otel-collector.observability.svc.cluster.local:4318"
	if _, err := r.updateStatusReady(context.Background(), stored, moved, otlpSourceSpec); err != nil {
		t.Fatalf("updateStatusReady failed: %v", err)
	}
	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatalf("failed to read the agent back: %v", err)
	}
	if stored.Status.Telemetry.OTLPEndpoint != moved || stored.Status.Telemetry.OTLPEndpointSource != otlpSourceSpec {
		t.Errorf("expected status to follow the endpoint, got (%q, %s)",
			stored.Status.Telemetry.OTLPEndpoint, stored.Status.Telemetry.OTLPEndpointSource)
	}

	// Clearing back to None has to reach status too. This is the field the CRD
	// description, the telemetry docs and the observability skill all tell an operator to
	// read, and it is also what carries None across an operator restart — see
	// resolveOTLPEndpoint. An empty endpoint is easy to mistake for "no change".
	if _, err := r.updateStatusReady(context.Background(), stored, "", otlpSourceNone); err != nil {
		t.Fatalf("updateStatusReady failed: %v", err)
	}
	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatalf("failed to read the agent back: %v", err)
	}
	if stored.Status.Telemetry.OTLPEndpoint != "" || stored.Status.Telemetry.OTLPEndpointSource != otlpSourceNone {
		t.Errorf("expected (\"\", %s), got (%q, %s)", otlpSourceNone,
			stored.Status.Telemetry.OTLPEndpoint, stored.Status.Telemetry.OTLPEndpointSource)
	}
}

// TestResolveOTLPEndpointNoneSurvivesRestart is the cross-restart half of the flap guard.
// The in-memory cache dies with the operator process, so a recorded None is the only
// thing that survives it — without the replay, one API error on the first probe after a
// restart resolves every agent on a collector-less cluster to the managed default, puts
// OTEL_EXPORTER_OTLP_ENDPOINT back on the pod, and rolls it.
func TestResolveOTLPEndpointNoneSurvivesRestart(t *testing.T) {
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithInterceptorFuncs(interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			return errForbidden()
		},
	}).Build()

	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "a", Namespace: "ns"}}
	agent.Status.Telemetry = agentv1alpha1.TelemetryStatus{
		OTLPEndpoint:       "",
		OTLPEndpointSource: otlpSourceNone,
	}

	// A brand-new reconciler is the restart: no cache, and the probe fails.
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	endpoint, source := r.resolveOTLPEndpoint(context.Background(), agent)
	if endpoint != "" || source != otlpSourceNone {
		t.Errorf("expected the recorded (\"\", %s) to survive a restart, got (%q, %s)",
			otlpSourceNone, endpoint, source)
	}

	// An agent with no recorded telemetry still falls to the default on an inconclusive
	// probe — None is replayed, not invented.
	fresh := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	blank := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "b", Namespace: "ns"}}
	endpoint, source = fresh.resolveOTLPEndpoint(context.Background(), blank)
	if endpoint != managedOTelEndpoint || source != otlpSourceDefault {
		t.Errorf("expected (%q, %s) for an agent with no recorded telemetry, got (%q, %s)",
			managedOTelEndpoint, otlpSourceDefault, endpoint, source)
	}
}

func TestResolveOTLPEndpointPrecedence(t *testing.T) {
	scheme := setupScheme()
	const (
		discovered = "http://otel-collector.otel-collector.svc.cluster.local:4318"
		fromSpec   = "http://from-spec:4318"
		fromEnv    = "http://from-deployment-env:4318"
		fromOp     = "http://from-operator-env:4318"
	)

	withSpec := func(agent *agentv1alpha1.PlatformAgent, endpoint string) *agentv1alpha1.PlatformAgent {
		agent.Spec.Telemetry = &agentv1alpha1.TelemetrySpec{OTLPEndpoint: endpoint}
		return agent
	}
	withDeploymentEnv := func(agent *agentv1alpha1.PlatformAgent, endpoint string) *agentv1alpha1.PlatformAgent {
		agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
			Env: []corev1.EnvVar{{Name: "OTEL_EXPORTER_OTLP_ENDPOINT", Value: endpoint}},
		}
		return agent
	}
	newAgent := func() *agentv1alpha1.PlatformAgent {
		return &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "a", Namespace: "ns"}}
	}

	tests := []struct {
		name        string
		agent       *agentv1alpha1.PlatformAgent
		operatorEnv string
		collector   bool
		// noReads asserts the rung short-circuits discovery entirely.
		noReads bool
		// discoveryOff sets OTEL_COLLECTOR_DISCOVERY=false, which is what separates
		// "nobody looked" (Default) from "looked and found none" (None).
		discoveryOff bool
		want         string
		wantSource   string
	}{
		{
			name:        "deployment env beats everything",
			agent:       withSpec(withDeploymentEnv(newAgent(), fromEnv), fromSpec),
			operatorEnv: fromOp,
			collector:   true,
			noReads:     true,
			want:        fromEnv,
			wantSource:  otlpSourceDeploymentEnv,
		},
		{
			name:        "spec beats the operator env",
			agent:       withSpec(newAgent(), fromSpec),
			operatorEnv: fromOp,
			collector:   true,
			noReads:     true,
			want:        fromSpec,
			wantSource:  otlpSourceSpec,
		},
		{
			name:        "operator env beats discovery",
			agent:       newAgent(),
			operatorEnv: fromOp,
			collector:   true,
			noReads:     true,
			want:        fromOp,
			wantSource:  otlpSourceOperatorEnv,
		},
		{
			name:       "discovery beats the default",
			agent:      newAgent(),
			collector:  true,
			want:       discovered,
			wantSource: otlpSourceDiscovered,
		},
		{
			// Nothing configured and the probe comes back empty. Not the managed
			// default: discovery established there is no collector here.
			name:       "none when nothing is set and no collector is found",
			agent:      newAgent(),
			want:       "",
			wantSource: otlpSourceNone,
		},
		{
			// The managed default is what is left when nobody established anything.
			// Discovery switched off is the reachable way to produce that.
			name:         "default when discovery is switched off",
			agent:        newAgent(),
			discoveryOff: true,
			collector:    true,
			noReads:      true,
			want:         managedOTelEndpoint,
			wantSource:   otlpSourceDefault,
		},
		{
			// An empty string is a set-but-unset field: it must fall through rather than
			// pin the agent to "".
			name:       "empty spec field falls through",
			agent:      withSpec(newAgent(), "  "),
			want:       "",
			wantSource: otlpSourceNone,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv(otelEndpointEnvVar, tt.operatorEnv)
			if tt.discoveryOff {
				t.Setenv(otelDiscoveryEnvVar, "false")
			}

			builder := fake.NewClientBuilder().WithScheme(scheme)
			if tt.collector {
				builder = builder.WithObjects(collectorService("otel-collector", "otel-collector", "otlp-http", 4318, nil))
			}
			if tt.noReads {
				builder = builder.WithInterceptorFuncs(noReadsAllowed(t))
			}

			r := &PlatformAgentReconciler{Client: builder.Build(), Scheme: scheme}
			endpoint, source := r.resolveOTLPEndpoint(context.Background(), tt.agent)
			if endpoint != tt.want || source != tt.wantSource {
				t.Errorf("expected (%q, %s), got (%q, %s)", tt.want, tt.wantSource, endpoint, source)
			}
		})
	}
}
