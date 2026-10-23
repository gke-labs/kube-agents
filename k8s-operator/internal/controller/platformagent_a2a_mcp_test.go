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
	"fmt"
	"path"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Anything but an exact "true" leaves the door shut, the inject backend's
// rule: a typo must not open a door.
func TestTheMCPDoorFlagFailsShut(t *testing.T) {
	for value, want := range map[string]bool{"true": true, "": false, "TRUE": false, "1": false, "yes": false, " true": false} {
		t.Setenv(a2aMCPDoorEnvVar, value)
		if got := a2aMCPDoorEnabled(); got != want {
			t.Errorf("%s=%q: enabled = %v, want %v", a2aMCPDoorEnvVar, value, got, want)
		}
	}
}

// With the flag off the bus fence admits exactly what it did before the MCP
// server existed, and with it on it admits the MCP pod on 4222 and nowhere
// else: never on the websocket listener, whose one peer is the console.
func TestTheBusFenceAdmitsTheMCPServerOnlyUnderItsFlag(t *testing.T) {
	agent := a2aTestAgent()
	admits := func(np *networkingv1.NetworkPolicy, port int32) bool {
		for _, rule := range np.Spec.Ingress {
			onPort := false
			for _, p := range rule.Ports {
				if p.Port != nil && p.Port.IntVal == port {
					onPort = true
				}
			}
			if !onPort {
				continue
			}
			for _, peer := range rule.From {
				if peer.PodSelector != nil && peer.PodSelector.MatchLabels["app"] == a2aMCPName(agent) {
					return true
				}
			}
		}
		return false
	}

	t.Setenv(a2aMCPDoorEnvVar, "")
	off := buildA2ANATSNetworkPolicy(agent)
	if admits(off, a2aNATSClientPort) || admits(off, a2aNATSWebSocketPort) {
		t.Error("the bus fence admits the MCP pod with the flag off")
	}

	t.Setenv(a2aMCPDoorEnvVar, "true")
	on := buildA2ANATSNetworkPolicy(agent)
	if !admits(on, a2aNATSClientPort) {
		t.Error("the bus fence does not admit the MCP pod on 4222 with the flag on")
	}
	if admits(on, a2aNATSWebSocketPort) {
		t.Error("the bus fence admits the MCP pod on 9222; the console is that listener's one peer")
	}
}

func TestBuildA2AMCPDeployment(t *testing.T) {
	agent := a2aTestAgent()
	dep := buildA2AMCPDeployment(agent)
	pod := dep.Spec.Template.Spec

	if pod.AutomountServiceAccountToken == nil || *pod.AutomountServiceAccountToken {
		t.Error("the MCP pod mounts a ServiceAccount token; it never talks to the API server")
	}
	if got := dep.Spec.Template.Labels["app"]; got != a2aMCPName(agent) {
		t.Errorf("pod app label = %q; the bus fence's 4222 peer and the MCP fence select on it", got)
	}
	if len(pod.Containers) != 1 {
		t.Fatalf("containers = %d, want 1", len(pod.Containers))
	}
	c := pod.Containers[0]
	if c.Image != a2aConsoleImage() || len(c.Command) != 1 || c.Command[0] != a2aMCPCommand {
		t.Errorf("image %q command %v, want the console image running %s", c.Image, c.Command, a2aMCPCommand)
	}

	env := map[string]string{}
	for _, e := range c.Env {
		if e.ValueFrom != nil {
			t.Errorf("env %s reads %v; every credential here is a mounted file", e.Name, e.ValueFrom)
		}
		env[e.Name] = e.Value
	}
	if !strings.HasPrefix(env["NATS_URL"], a2aNATSTLSScheme) {
		t.Errorf("NATS_URL = %q, want %s: the bus requires TLS", env["NATS_URL"], a2aNATSTLSScheme)
	}
	if env[a2aNATSCAFileEnv] != a2aNATSCAFile() {
		t.Errorf("%s = %q, want %q", a2aNATSCAFileEnv, env[a2aNATSCAFileEnv], a2aNATSCAFile())
	}
	if env[a2aMCPEnvBusUser] != a2aConsoleConfUser {
		t.Errorf("bus user = %q, want the console principal", env[a2aMCPEnvBusUser])
	}

	// Every file the server reads sits in a mount of this pod.
	mounts := map[string]string{}
	for _, m := range c.VolumeMounts {
		if !m.ReadOnly || m.SubPath != "" {
			t.Errorf("mount %+v, want read-only directory mounts so rotations reach the pod", m)
		}
		mounts[m.MountPath] = m.Name
	}
	for _, key := range []string{a2aMCPEnvAccessFile, a2aMCPEnvPasswordFile, a2aNATSCAFileEnv} {
		if _, ok := mounts[path.Dir(env[key])]; !ok {
			t.Errorf("%s = %q is not under any mount %v", key, env[key], mounts)
		}
	}

	// The server retries its first connect to the bus rather than exiting,
	// and the startup probe holds liveness off for that long: a fresh
	// install's bus comes up after this pod.
	sp := c.StartupProbe
	if sp == nil || sp.HTTPGet == nil || sp.HTTPGet.Path != a2aMCPHealthPath {
		t.Fatalf("startup probe = %+v, want one on %s", sp, a2aMCPHealthPath)
	}
	if window := sp.PeriodSeconds * sp.FailureThreshold; window < 120 {
		t.Errorf("the startup probe allows %ds for the first bus connect, want minutes", window)
	}
}

// The creds Secret holds every static user's password. The MCP pod gets the
// console's and nothing else, and its only other Secret is its own token.
// With broker access on (A2A_MCP_DOOR=true on a next install) it also holds
// exactly one projected token, for the broker's mcp audience and nothing else;
// with it off, none.
func TestTheMCPPodHoldsTheConsolePasswordAndItsOwnTokenOnly(t *testing.T) {
	for _, brokerOn := range []bool{false, true} {
		t.Run(map[bool]string{false: "broker access off", true: "broker access on"}[brokerOn], func(t *testing.T) {
			t.Setenv(a2aMCPDoorEnvVar, map[bool]string{false: "", true: "true"}[brokerOn])
			agent := a2aTestAgent()
			pod := buildA2AMCPDeployment(agent).Spec.Template.Spec
			secrets := map[string]*corev1.SecretVolumeSource{}
			var projected []corev1.Volume
			for _, v := range pod.Volumes {
				if v.Projected != nil {
					projected = append(projected, v)
				}
				if v.Secret != nil {
					secrets[v.Secret.SecretName] = v.Secret
				}
			}
			if len(secrets) != 2 {
				t.Fatalf("secret volumes = %v, want the creds Secret and the token Secret", secrets)
			}
			for name, key := range map[string]string{a2aCredsSecretName(agent): a2aConsolePasswordKey, a2aMCPAccessName(agent): a2aMCPAccessKey} {
				src := secrets[name]
				if src == nil {
					t.Errorf("no volume from Secret %s", name)
					continue
				}
				if len(src.Items) != 1 || src.Items[0].Key != key {
					t.Errorf("Secret %s projects %+v, want exactly %s", name, src.Items, key)
				}
				if src.Optional != nil && *src.Optional {
					t.Errorf("Secret %s is optional; the server must not start without it", name)
				}
			}

			if !brokerOn {
				if len(projected) != 0 {
					t.Errorf("projected volumes %v: the MCP pod holds no projected token without broker access", projected)
				}
				return
			}
			if len(projected) != 1 {
				t.Fatalf("projected volumes = %d, want exactly the broker token", len(projected))
			}
			vol := projected[0]
			sources := vol.Projected.Sources
			if len(sources) != 1 {
				t.Fatalf("projected volume %s has %d sources, want only the broker token", vol.Name, len(sources))
			}
			src := sources[0]
			if src.ServiceAccountToken == nil || src.Secret != nil || src.ConfigMap != nil || src.DownwardAPI != nil || src.ClusterTrustBundle != nil {
				t.Fatalf("projected source %+v, want a ServiceAccount token and nothing else", src)
			}
			if src.ServiceAccountToken.Audience != credentialProxyMCPAudience {
				t.Errorf("token audience = %q, want the broker's mcp audience %q", src.ServiceAccountToken.Audience, credentialProxyMCPAudience)
			}

			// Mounted read-only, in the one container, at exactly the path
			// the MCP binary is told to read.
			c := pod.Containers[0]
			tokenFile := ""
			for _, e := range c.Env {
				if e.Name == a2aMCPBrokerTokenFileEnvVar {
					tokenFile = e.Value
				}
			}
			if tokenFile == "" {
				t.Fatalf("%s is not set on the MCP container", a2aMCPBrokerTokenFileEnvVar)
			}
			var mounts []corev1.VolumeMount
			for _, m := range c.VolumeMounts {
				if m.Name == vol.Name {
					mounts = append(mounts, m)
				}
			}
			if len(mounts) != 1 {
				t.Fatalf("volume %s mounted %d times, want once", vol.Name, len(mounts))
			}
			m := mounts[0]
			if !m.ReadOnly || m.SubPath != "" {
				t.Errorf("token mount %+v, want a read-only directory mount", m)
			}
			if want := path.Join(m.MountPath, src.ServiceAccountToken.Path); tokenFile != want {
				t.Errorf("%s = %q, but the token is mounted at %q", a2aMCPBrokerTokenFileEnvVar, tokenFile, want)
			}
		})
	}
}

func TestTheMCPServiceAndFence(t *testing.T) {
	agent := a2aTestAgent()
	svc := buildA2AMCPService(agent)
	if svc.Spec.Type != corev1.ServiceTypeClusterIP {
		t.Errorf("service type = %q, want ClusterIP: the way in is a port-forward", svc.Spec.Type)
	}
	np := buildA2AMCPNetworkPolicy(agent)
	if len(np.Spec.Ingress) != 0 || len(np.Spec.PolicyTypes) != 1 || np.Spec.PolicyTypes[0] != networkingv1.PolicyTypeIngress {
		t.Errorf("fence = %+v, want deny-all ingress", np.Spec)
	}
	if np.Spec.PodSelector.MatchLabels["app"] != a2aMCPName(agent) {
		t.Errorf("fence selects %v, want the MCP pod", np.Spec.PodSelector.MatchLabels)
	}
}

func mcpTestReconciler(t *testing.T, objs ...client.Object) (*PlatformAgentReconciler, client.Client) {
	t.Helper()
	scheme := setupScheme()
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent}, objs...)...).
		WithInterceptorFuncs(ssaApplyInterceptor()).Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl
}

func readMCPToken(t *testing.T, cl client.Client) (*corev1.Secret, error) {
	t.Helper()
	agent := a2aTestAgent()
	s := &corev1.Secret{}
	err := cl.Get(context.Background(), types.NamespacedName{Name: a2aMCPAccessName(agent), Namespace: agent.Namespace}, s)
	return s, err
}

// The token is minted once and never rotated by the operator, so a person can
// rotate it by editing the Secret; an empty one is filled; an unowned one is
// refused rather than adopted.
func TestTheMCPTokenIsMintedOnceAndNeverAdopted(t *testing.T) {
	ctx := context.Background()
	agent := a2aTestAgent()

	t.Run("minted, then kept", func(t *testing.T) {
		r, cl := mcpTestReconciler(t)
		if err := r.ensureA2AMCPToken(ctx, agent); err != nil {
			t.Fatal(err)
		}
		first, err := readMCPToken(t, cl)
		if err != nil || len(first.Data[a2aMCPAccessKey]) == 0 {
			t.Fatalf("no token minted: %v %v", err, first.Data)
		}
		if !metav1.IsControlledBy(first, agent) {
			t.Error("the minted token is not owned by the PlatformAgent")
		}
		if err := r.ensureA2AMCPToken(ctx, agent); err != nil {
			t.Fatal(err)
		}
		second, _ := readMCPToken(t, cl)
		if string(second.Data[a2aMCPAccessKey]) != string(first.Data[a2aMCPAccessKey]) {
			t.Error("the operator rotated a token that was there")
		}
	})

	// Empty as the server reads it: a whitespace-only value is refused there
	// (ReadToken trims), so it is minted over here, not kept.
	for name, value := range map[string]string{"empty": "", "whitespace-only": " \n\t"} {
		t.Run("an owned Secret with an "+name+" token is filled", func(t *testing.T) {
			empty := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: a2aMCPAccessName(agent), Namespace: agent.Namespace},
				Data: map[string][]byte{a2aMCPAccessKey: []byte(value)}}
			if err := ctrl.SetControllerReference(agent, empty, setupScheme()); err != nil {
				t.Fatal(err)
			}
			r, cl := mcpTestReconciler(t, empty)
			if err := r.ensureA2AMCPToken(ctx, agent); err != nil {
				t.Fatal(err)
			}
			filled, err := readMCPToken(t, cl)
			if err != nil || strings.TrimSpace(string(filled.Data[a2aMCPAccessKey])) == "" {
				t.Fatalf("the owned, %s token Secret was not filled: %v", name, err)
			}
		})
	}

	// A value no Authorization header can carry, a control byte inside it,
	// is minted over; a printable value a person wrote is kept, its ends
	// trimmed by the server.
	for value, kept := range map[string]bool{
		"ab\ncd": false, "ab\rcd": false, "ab\x00cd": false, "ab\x7fcd": false, "ab\tcd": true,
		" valid-token \n": true, "with space inside": true,
	} {
		t.Run(fmt.Sprintf("an owned Secret holding %q is kept: %v", value, kept), func(t *testing.T) {
			held := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: a2aMCPAccessName(agent), Namespace: agent.Namespace},
				Data: map[string][]byte{a2aMCPAccessKey: []byte(value)}}
			if err := ctrl.SetControllerReference(agent, held, setupScheme()); err != nil {
				t.Fatal(err)
			}
			r, cl := mcpTestReconciler(t, held)
			if err := r.ensureA2AMCPToken(ctx, agent); err != nil {
				t.Fatal(err)
			}
			got, err := readMCPToken(t, cl)
			if err != nil {
				t.Fatal(err)
			}
			v := got.Data[a2aMCPAccessKey]
			if kept && string(v) != value {
				t.Errorf("the token %q was replaced with %q", value, v)
			}
			if !kept && (string(v) == value || !a2aMCPTokenUsable(v)) {
				t.Errorf("the token %q was not minted over: now %q", value, v)
			}
		})
	}

	// With the flag off, an unowned Secret under the reserved name is not
	// ours: the removal skips it rather than failing the whole reconcile,
	// and leaves it alone.
	t.Run("with the flag off an unowned Secret is skipped, not fatal", func(t *testing.T) {
		t.Setenv(a2aMCPDoorEnvVar, "")
		squatter := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: a2aMCPAccessName(agent), Namespace: agent.Namespace},
			Data: map[string][]byte{a2aMCPAccessKey: []byte("chosen-by-someone-else")}}
		r, cl := mcpTestReconciler(t, squatter)
		if err := r.reconcileA2AMCP(ctx, agent); err != nil {
			t.Fatalf("an unowned Secret under the reserved name stopped the reconcile: %v", err)
		}
		if got, err := readMCPToken(t, cl); err != nil || string(got.Data[a2aMCPAccessKey]) != "chosen-by-someone-else" {
			t.Errorf("the unowned Secret was touched: %v", err)
		}
	})

	t.Run("an unowned Secret is refused", func(t *testing.T) {
		squatter := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: a2aMCPAccessName(agent), Namespace: agent.Namespace},
			Data: map[string][]byte{a2aMCPAccessKey: []byte("chosen-by-someone-else")}}
		r, _ := mcpTestReconciler(t, squatter)
		if err := r.ensureA2AMCPToken(ctx, agent); err == nil {
			t.Error("adopted an unowned Secret as the MCP token")
		}
	})
}

// On, the server, its Service, its fence and its token exist; off again, all
// four are gone, the token included.
func TestTheMCPServerComesAndGoesWithItsFlag(t *testing.T) {
	ctx := context.Background()
	agent := a2aTestAgent()
	r, cl := mcpTestReconciler(t)
	key := func(name string) types.NamespacedName {
		return types.NamespacedName{Name: name, Namespace: agent.Namespace}
	}
	present := func(obj client.Object, name string) bool {
		err := cl.Get(ctx, key(name), obj)
		if err != nil && !apierrors.IsNotFound(err) {
			t.Fatal(err)
		}
		return err == nil
	}

	t.Setenv(a2aMCPDoorEnvVar, "true")
	if err := r.reconcileA2AMCP(ctx, agent); err != nil {
		t.Fatal(err)
	}
	// The fence is reconcileA2ANetworkFences's; applied here as that pass
	// would, so the removal below has it to remove.
	np := buildA2AMCPNetworkPolicy(agent)
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, key(agent.Name), stored); err != nil {
		t.Fatal(err)
	}
	if err := ctrl.SetControllerReference(stored, np, r.Scheme); err != nil {
		t.Fatal(err)
	}
	if err := r.Create(ctx, np); err != nil {
		t.Fatal(err)
	}
	if !present(&appsv1.Deployment{}, a2aMCPName(agent)) || !present(&corev1.Service{}, a2aMCPName(agent)) || !present(&corev1.Secret{}, a2aMCPAccessName(agent)) {
		t.Fatal("the MCP server's objects are not all there with the flag on")
	}

	t.Setenv(a2aMCPDoorEnvVar, "")
	if err := r.reconcileA2AMCP(ctx, agent); err != nil {
		t.Fatal(err)
	}
	for name, obj := range map[string]client.Object{
		"Deployment": &appsv1.Deployment{}, "Service": &corev1.Service{},
	} {
		if present(obj, a2aMCPName(agent)) {
			t.Errorf("the MCP %s survived the flag going off", name)
		}
	}
	if present(&networkingv1.NetworkPolicy{}, a2aMCPNetpolName(agent)) {
		t.Error("the MCP fence survived the flag going off")
	}
	if present(&corev1.Secret{}, a2aMCPAccessName(agent)) {
		t.Error("the MCP token survived the flag going off")
	}
}

// The fence outlives the pod it fences: removal leaves it while an MCP pod is
// still terminating and takes it on a later pass.
func TestTheMCPFenceWaitsForThePodToGo(t *testing.T) {
	ctx := context.Background()
	agent := a2aTestAgent()
	r, cl := mcpTestReconciler(t)
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}, stored); err != nil {
		t.Fatal(err)
	}
	np := buildA2AMCPNetworkPolicy(agent)
	if err := ctrl.SetControllerReference(stored, np, r.Scheme); err != nil {
		t.Fatal(err)
	}
	if err := cl.Create(ctx, np); err != nil {
		t.Fatal(err)
	}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: a2aMCPName(agent) + "-x", Namespace: agent.Namespace,
		Labels: map[string]string{"app": a2aMCPName(agent)}}}
	if err := cl.Create(ctx, pod); err != nil {
		t.Fatal(err)
	}
	fenceKey := types.NamespacedName{Name: a2aMCPNetpolName(agent), Namespace: agent.Namespace}

	if err := r.removeA2AMCP(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, fenceKey, &networkingv1.NetworkPolicy{}); err != nil {
		t.Fatalf("the fence went while an MCP pod was still there: %v", err)
	}

	if err := cl.Delete(ctx, pod); err != nil {
		t.Fatal(err)
	}
	if err := r.removeA2AMCP(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, fenceKey, &networkingv1.NetworkPolicy{}); !apierrors.IsNotFound(err) {
		t.Errorf("the fence survived its pod: %v", err)
	}
}

// With the flag off, an unowned NetworkPolicy under the fence's name is not
// ours either: with no MCP pod left, the removal skips it rather than
// failing the reconcile, and leaves it standing.
func TestTheFlagOffMCPRemovalSkipsAnUnownedFence(t *testing.T) {
	t.Setenv(a2aMCPDoorEnvVar, "")
	ctx := context.Background()
	agent := a2aTestAgent()
	squatter := buildA2AMCPNetworkPolicy(agent)
	r, cl := mcpTestReconciler(t, squatter)
	if err := r.reconcileA2AMCP(ctx, agent); err != nil {
		t.Fatalf("an unowned NetworkPolicy under the fence's name stopped the reconcile: %v", err)
	}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(squatter), &networkingv1.NetworkPolicy{}); err != nil {
		t.Errorf("the unowned NetworkPolicy was touched: %v", err)
	}
}

// getCounter counts the reads of each object through it.
type getCounter struct {
	client.Client
	gets map[string]int
}

func (c *getCounter) Get(ctx context.Context, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
	c.gets[fmt.Sprintf("%T %s", obj, key)]++
	return c.Client.Get(ctx, key, obj, opts...)
}

// The removal reads each of its objects once a pass, owned or not: the
// token Secret's read is uncached, and runs on every flag-off reconcile.
func TestTheMCPRemovalReadsEachObjectOnce(t *testing.T) {
	ctx := context.Background()
	agent := a2aTestAgent()
	unowned := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: a2aMCPAccessName(agent), Namespace: agent.Namespace}}
	owned := buildA2AMCPService(agent)
	if err := ctrl.SetControllerReference(agent, owned, setupScheme()); err != nil {
		t.Fatal(err)
	}
	r, _ := mcpTestReconciler(t, unowned, owned, buildA2AMCPNetworkPolicy(agent))
	counter := &getCounter{Client: r.Client, gets: map[string]int{}}
	r.Client = counter
	if err := r.removeA2AMCP(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if len(counter.gets) != len(r.a2aMCPObjects(agent)) {
		t.Errorf("reads = %v, want one of each of the %d objects", counter.gets, len(r.a2aMCPObjects(agent)))
	}
	for obj, n := range counter.gets {
		if n != 1 {
			t.Errorf("%s was read %d times, want once", obj, n)
		}
	}
}

// Nothing else carries a credential into the pod: no env from a Secret, no
// second container, no init container. Without broker access it runs as the
// namespace default with no token mounted; with it, as its own ServiceAccount,
// whose only token is the projected broker one the test above pins.
func TestTheMCPPodCarriesNoOtherCredential(t *testing.T) {
	for _, brokerOn := range []bool{false, true} {
		t.Run(map[bool]string{false: "broker access off", true: "broker access on"}[brokerOn], func(t *testing.T) {
			t.Setenv(a2aMCPDoorEnvVar, map[bool]string{false: "", true: "true"}[brokerOn])
			agent := a2aTestAgent()
			pod := buildA2AMCPDeployment(agent).Spec.Template.Spec
			if len(pod.Containers) != 1 || len(pod.InitContainers) != 0 || len(pod.EphemeralContainers) != 0 {
				t.Errorf("containers %d, init %d, ephemeral %d; want one container", len(pod.Containers), len(pod.InitContainers), len(pod.EphemeralContainers))
			}
			if pod.AutomountServiceAccountToken == nil || *pod.AutomountServiceAccountToken {
				t.Error("the default-audience ServiceAccount token is mounted; the pod never talks to the API server")
			}
			wantSA := ""
			if brokerOn {
				wantSA = a2aMCPName(agent)
			}
			if pod.ServiceAccountName != wantSA {
				t.Errorf("serviceAccountName = %q, want %q", pod.ServiceAccountName, wantSA)
			}
			if pod.DeprecatedServiceAccount != "" {
				t.Errorf("serviceAccount = %q, want none", pod.DeprecatedServiceAccount)
			}
			for _, c := range pod.Containers {
				if len(c.EnvFrom) != 0 {
					t.Errorf("container %s has envFrom %v; every credential here is one mounted file", c.Name, c.EnvFrom)
				}
			}
		})
	}
}

// The MCP server's turns go through the gateway's console door. Every next
// gateway runs on the console door (#3747), so the MCP flag adds nothing to
// the gateway; what it must not do is add the console door a second time.
func TestTheMCPDoorLeavesTheGatewaysConsoleDoorAlone(t *testing.T) {
	agent := a2aTestAgent()
	for _, flag := range []string{"", "true"} {
		t.Setenv(a2aMCPDoorEnvVar, flag)
		n, value := 0, ""
		for _, c := range buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers {
			for _, e := range c.Env {
				if e.Name == a2aConsoleDoorEnvVar {
					n++
					value = e.Value
				}
			}
		}
		if n != 1 || value != "true" {
			t.Errorf("MCP flag %q: the gateway carries %s %d times (value %q), want once, true", flag, a2aConsoleDoorEnvVar, n, value)
		}
	}
}
