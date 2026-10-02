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
	"reflect"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func consoleEnv(t *testing.T, c corev1.Container) map[string]string {
	t.Helper()
	env := map[string]string{}
	for _, e := range c.Env {
		if e.ValueFrom != nil {
			t.Errorf("env %s comes from %+v; the console takes its credential from a file, not the environment", e.Name, e.ValueFrom)
		}
		env[e.Name] = e.Value
	}
	return env
}

func TestBuildA2AConsoleDeployment(t *testing.T) {
	agent := a2aTestAgent()
	dep := buildA2AConsoleDeployment(agent)

	if dep.Name != "test-agent-a2a-console" || dep.Namespace != "test-ns" {
		t.Errorf("name = %s/%s", dep.Namespace, dep.Name)
	}
	if got := dep.Spec.Selector.MatchLabels; !reflect.DeepEqual(got, map[string]string{"app": "test-agent-a2a-console"}) {
		t.Errorf("selector = %v", got)
	}
	if got := dep.Spec.Template.Labels["app"]; got != "test-agent-a2a-console" {
		t.Errorf("pod app label = %q; the console fence and the NATS fence's 9222 peer both select on it", got)
	}
	if dep.Spec.Replicas == nil || *dep.Spec.Replicas != 1 {
		t.Errorf("replicas = %v, want 1", dep.Spec.Replicas)
	}

	pod := dep.Spec.Template.Spec
	if pod.AutomountServiceAccountToken == nil || *pod.AutomountServiceAccountToken {
		t.Error("the console pod mounts a ServiceAccount token; it never talks to the API server")
	}
	if pod.SecurityContext == nil || pod.SecurityContext.FSGroup == nil || *pod.SecurityContext.FSGroup != 1000 {
		t.Errorf("pod FSGroup = %+v, want 1000 so UID 1000 can read the 0440 credential file", pod.SecurityContext)
	}
	if len(pod.Containers) != 1 {
		t.Fatalf("containers = %d, want 1", len(pod.Containers))
	}
	c := pod.Containers[0]
	if want := a2aConsoleImage(); c.Image != want {
		t.Errorf("image = %q, want the resolved release image %q", c.Image, want)
	}

	want := map[string]string{
		"CONSOLE_BUS_URL":       "http://test-agent-a2a-nats.test-ns.svc:9222",
		"CONSOLE_USER":          "console",
		"CONSOLE_PASSWORD_FILE": "/var/run/secrets/a2a-console/console-password",
		"CONSOLE_ALLOWED_HOSTS": "localhost:8080,127.0.0.1:8080",
	}
	if got := consoleEnv(t, c); !reflect.DeepEqual(got, want) {
		t.Errorf("env = %v\nwant  %v", got, want)
	}

	if len(c.Ports) != 1 || c.Ports[0].ContainerPort != 8080 || c.Ports[0].Name != "http" {
		t.Errorf("ports = %+v, want one http port 8080", c.Ports)
	}
	for name, probe := range map[string]*corev1.Probe{"readiness": c.ReadinessProbe, "liveness": c.LivenessProbe} {
		if probe == nil || probe.HTTPGet == nil || probe.HTTPGet.Path != "/healthz" || probe.HTTPGet.Port.IntValue() != 8080 {
			t.Errorf("%s probe = %+v, want GET /healthz on 8080 (the one path the server answers for any Host)", name, probe)
		}
	}
}

func TestTheConsoleImageCanBeOverridden(t *testing.T) {
	t.Setenv(a2aConsoleImageEnvVar, "registry.example/console:dev")
	if got := buildA2AConsoleDeployment(a2aTestAgent()).Spec.Template.Spec.Containers[0].Image; got != "registry.example/console:dev" {
		t.Errorf("image = %q, want the %s override", got, a2aConsoleImageEnvVar)
	}
}

// The creds Secret holds every static user's password, sys included. The
// console pod gets exactly one of them, and the only way it gets it is this
// volume, so the volume is what has to be pinned. The sys-password check in
// the manifests tests reads env references, which this pod has none of.
func TestTheConsoleCredentialVolumeProjectsOneKey(t *testing.T) {
	agent := a2aTestAgent()
	pod := buildA2AConsoleDeployment(agent).Spec.Template.Spec

	if len(pod.Volumes) != 1 || pod.Volumes[0].Secret == nil {
		t.Fatalf("volumes = %+v, want one Secret volume", pod.Volumes)
	}
	src := pod.Volumes[0].Secret
	if src.SecretName != a2aCredsSecretName(agent) {
		t.Errorf("secret = %q, want %q", src.SecretName, a2aCredsSecretName(agent))
	}
	if len(src.Items) != 1 || src.Items[0].Key != "console-password" || src.Items[0].Path != "console-password" {
		t.Fatalf("items = %+v, want exactly console-password", src.Items)
	}
	if src.Items[0].Mode == nil || *src.Items[0].Mode != 0o440 {
		t.Errorf("item mode = %v, want 0440", src.Items[0].Mode)
	}
	// Optional, so a Secret missing the key lets the pod start instead of
	// sitting in ContainerCreating; the server answers with its designed 503.
	if src.Optional == nil || !*src.Optional {
		t.Errorf("optional = %v, want true", src.Optional)
	}

	mounts := pod.Containers[0].VolumeMounts
	if len(mounts) != 1 {
		t.Fatalf("mounts = %+v, want one", mounts)
	}
	m := mounts[0]
	if m.Name != pod.Volumes[0].Name || m.MountPath != "/var/run/secrets/a2a-console" || !m.ReadOnly {
		t.Errorf("mount = %+v, want the credential volume read-only at /var/run/secrets/a2a-console", m)
	}
	// A subPath mount never sees a rotated Secret. The server reads the file
	// per request, which only helps if the file can change.
	if m.SubPath != "" {
		t.Errorf("mount uses subPath %q; rotation would never reach the pod", m.SubPath)
	}
}

func TestBuildA2AConsoleService(t *testing.T) {
	svc := buildA2AConsoleService(a2aTestAgent())
	if svc.Name != "test-agent-a2a-console" || svc.Spec.Type != corev1.ServiceTypeClusterIP {
		t.Errorf("service = %s type %s, want test-agent-a2a-console ClusterIP", svc.Name, svc.Spec.Type)
	}
	if !reflect.DeepEqual(svc.Spec.Selector, map[string]string{"app": "test-agent-a2a-console"}) {
		t.Errorf("selector = %v", svc.Spec.Selector)
	}
	if len(svc.Spec.Ports) != 1 || svc.Spec.Ports[0].Port != 8080 ||
		svc.Spec.Ports[0].TargetPort.IntValue() != 8080 || svc.Spec.Ports[0].Name != "http" {
		t.Errorf("ports = %+v, want http 8080 -> 8080", svc.Spec.Ports)
	}
}

// Deny-all ingress on the console pod. The port-forward and the kubelet's
// probes enter from the node, which NetworkPolicy doesn't govern, so both keep
// working. What it refuses is every other pod, which could otherwise GET
// /config.json and read the console password.
func TestBuildA2AConsoleNetworkPolicy(t *testing.T) {
	np := buildA2AConsoleNetworkPolicy(a2aTestAgent())
	if np.Name != "test-agent-a2a-console-netpol" {
		t.Errorf("name = %q", np.Name)
	}
	if !reflect.DeepEqual(np.Spec.PodSelector.MatchLabels, map[string]string{"app": "test-agent-a2a-console"}) {
		t.Errorf("pod selector = %v", np.Spec.PodSelector.MatchLabels)
	}
	if !reflect.DeepEqual(np.Spec.PolicyTypes, []networkingv1.PolicyType{networkingv1.PolicyTypeIngress}) {
		t.Errorf("policy types = %v, want ingress only", np.Spec.PolicyTypes)
	}
	if len(np.Spec.Ingress) != 0 {
		t.Errorf("ingress = %+v, want none (deny all)", np.Spec.Ingress)
	}
	if got := np.Labels[a2aComponentLabel]; got != "console-netpol" {
		t.Errorf("component label = %q, want %q", got, "console-netpol")
	}
}

func TestTheConsoleServerLogsInAsTheConsoleIdentity(t *testing.T) {
	id := consoleIdentity()
	if id.user != a2aConsoleConfUser || id.credsKey != a2aConsolePasswordKey {
		t.Errorf("consoleIdentity() = user %q key %q; the console server logs in as %q with %q",
			id.user, id.credsKey, a2aConsoleConfUser, a2aConsolePasswordKey)
	}
}

func TestTheWebsocketOriginsAreTheConsoleServers(t *testing.T) {
	conf := string(buildA2ANATSConfigSecret(authMapTestAgent(), a2aTestCreds(), a2aTestCalloutKeys(t)).Data["nats.conf"])
	const want = `allowed_origins: ["http://localhost:8080", "http://127.0.0.1:8080"]`
	if !strings.Contains(conf, want) {
		t.Errorf("rendered nats.conf lacks %s", want)
	}
	if strings.Contains(conf, "5173") {
		t.Error("rendered nats.conf still names the Vite port; the install's page is served by the console server")
	}
}

// The console server renders with the rest of the next stack, before the
// gateway's hold (it serves the page whether or not the gateway is up), and
// goes away with the stack on a flip back to today. Its fence goes with it.
func TestReconcileA2ARendersAndRemovesTheConsole(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	// finalizer pass, then the real one
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d failed: %v", i+1, err)
		}
	}

	name := types.NamespacedName{Name: a2aConsoleName(agent), Namespace: agent.Namespace}
	netpol := types.NamespacedName{Name: a2aConsoleNetpolName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, &appsv1.Deployment{}); !errors.IsNotFound(err) {
		t.Fatalf("the gateway rendered before its hold (err=%v); this test needs the hold in place to show the console doesn't wait on it", err)
	}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, name, dep); err != nil {
		t.Fatalf("console Deployment not rendered under next, ahead of the gateway: %v", err)
	}
	if len(dep.OwnerReferences) != 1 || dep.OwnerReferences[0].Name != agent.Name {
		t.Errorf("console Deployment owner refs = %+v, want the PlatformAgent", dep.OwnerReferences)
	}
	if err := cl.Get(ctx, name, &corev1.Service{}); err != nil {
		t.Errorf("console Service not rendered under next: %v", err)
	}
	if err := cl.Get(ctx, netpol, &networkingv1.NetworkPolicy{}); err != nil {
		t.Errorf("console NetworkPolicy not rendered under next: %v", err)
	}

	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	fresh.Spec.Mode = nil
	if err := cl.Update(ctx, fresh); err != nil {
		t.Fatalf("failed to update agent: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after the flip failed: %v", err)
	}
	if err := cl.Get(ctx, name, &appsv1.Deployment{}); !errors.IsNotFound(err) {
		t.Errorf("console Deployment still present under today (err=%v)", err)
	}
	if err := cl.Get(ctx, name, &corev1.Service{}); !errors.IsNotFound(err) {
		t.Errorf("console Service still present under today (err=%v)", err)
	}
	if err := cl.Get(ctx, netpol, &networkingv1.NetworkPolicy{}); !errors.IsNotFound(err) {
		t.Errorf("console NetworkPolicy still present under today (err=%v)", err)
	}
}
