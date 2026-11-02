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
	"bytes"
	"context"
	"fmt"
	"os"
	"path"
	"strconv"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The MCP server (a2a/cmd/mcp): a Model Context Protocol endpoint for an
// external coding agent, as a console client of the bus. It is opt-in per
// install with the operator flag below, like the inject backend and the
// agent door, and while it is off nothing of it is rendered. It runs in its
// own pod, not the gateway's, so the only bus credential within its reach is
// the console password, and the only way in is a kubectl port-forward and
// the server's own bearer token. A CR field can replace the flag if the
// server becomes a supported, documented feature.
const (
	// a2aMCPDoorEnvVar is the operator flag. Anything but "true" is off, the
	// rule a2aInjectBackendEnabled gives: a typo leaves the door shut.
	a2aMCPDoorEnvVar = "A2A_MCP_DOOR"

	a2aMCPComponent    = "mcp"
	a2aMCPContainer    = "mcp"
	a2aMCPNameSuffix   = "-a2a-mcp"
	a2aMCPNetpolSuffix = "-a2a-mcp-netpol"
	a2aMCPAccessSuffix = "-a2a-mcp-token"
	// a2aMCPNetpolComponent labels the MCP pod's deny-all NetworkPolicy.
	a2aMCPNetpolComponent = a2aMCPComponent + "-netpol"

	// The binary ships in the console image; the pod runs it by command.
	a2aMCPCommand = "/mcp"

	a2aMCPPort       = 8089
	a2aMCPPortName   = "http"
	a2aMCPHealthPath = "/healthz"

	// The bearer token callers present, minted by the operator into its own
	// Secret and mounted as a directory so a rotated token reaches the pod.
	a2aMCPAccessKey      = "token"
	a2aMCPAccessVolume   = "mcp-token"
	a2aMCPAccessMountDir = "/var/run/secrets/a2a-mcp"
	// The console password, one key of the creds Secret, mounted the way the
	// console server mounts it.
	a2aMCPBusKeyVolume   = "bus-key"
	a2aMCPBusKeyMountDir = "/var/run/secrets/a2a-mcp-bus"

	// The binary's environment (a2a/cmd/mcp).
	a2aMCPEnvAccessFile   = "MCP_TOKEN_FILE"
	a2aMCPEnvBusUser      = "MCP_BUS_USER"
	a2aMCPEnvPasswordFile = "MCP_BUS_PASSWORD_FILE"
	a2aMCPEnvAgent        = "MCP_AGENT_NAME"
	a2aMCPEnvNamespace    = "MCP_NAMESPACE"
	a2aMCPEnvListen       = "MCP_LISTEN"
	// GOMEMLIMIT, the Go runtime's soft heap limit, set to
	// a2aMCPGoMemLimitPercent of a2aMCPMemoryLimit (a2aMCPGoMemLimit), so
	// the collector works harder as the heap nears the container's limit
	// instead of letting garbage push the pod into an OOM kill.
	a2aMCPEnvGoMemLimit     = "GOMEMLIMIT"
	a2aMCPGoMemLimitPercent = 85

	a2aMCPReadinessPeriod  = 5
	a2aMCPReadinessFailure = 3
	a2aMCPLivenessPeriod   = 10
	a2aMCPLivenessFailure  = 6
	// The startup probe holds liveness off while the server retries its
	// first connect to the bus (five minutes), which on a fresh install
	// comes up after this pod.
	a2aMCPStartupPeriod  = 5
	a2aMCPStartupFailure = 60

	// Sized like the console: GKE Autopilot raises a CPU request under 50m.
	a2aMCPCPURequest    = "50m"
	a2aMCPMemoryRequest = "64Mi"
	a2aMCPMemoryLimit   = "128Mi"
)

// a2aMCPGoMemLimit is the MCP container's GOMEMLIMIT: a2aMCPGoMemLimitPercent
// of its memory limit, in bytes (114085068 for 128Mi), derived from
// a2aMCPMemoryLimit so a change to the limit carries over. The MCP server's
// reads are sized against it (readMaxBytes in a2a/mcp/reader.go).
func a2aMCPGoMemLimit() string {
	limit := resource.MustParse(a2aMCPMemoryLimit)
	return strconv.FormatInt(limit.Value()*a2aMCPGoMemLimitPercent/100, 10)
}

// a2aMCPFenceIndex and a2aMCPTokenIndex are the fence's and the token's
// places in a2aMCPObjects.
const (
	a2aMCPFenceIndex = 2
	a2aMCPTokenIndex = 3
)

// a2aMCPDoorEnabled reports whether the operator was deployed with the MCP
// flag.
func a2aMCPDoorEnabled() bool {
	return os.Getenv(a2aMCPDoorEnvVar) == "true"
}

func a2aMCPName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + a2aMCPNameSuffix
}

func a2aMCPNetpolName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + a2aMCPNetpolSuffix
}

func a2aMCPAccessName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + a2aMCPAccessSuffix
}

// a2aMCPNATSPeers is the MCP pod as a 4222 peer of the bus fence, under the
// flag. Off, it is nothing, so the fence admits exactly what it did.
func a2aMCPNATSPeers(agent *agentv1alpha1.PlatformAgent) []networkingv1.NetworkPolicyPeer {
	if !a2aMCPDoorEnabled() {
		return nil
	}
	return []networkingv1.NetworkPolicyPeer{{PodSelector: &metav1.LabelSelector{
		MatchLabels: map[string]string{"app": a2aMCPName(agent)},
	}}}
}

// buildA2AMCPDeployment renders the MCP server.
func buildA2AMCPDeployment(agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	name := a2aMCPName(agent)
	selector := map[string]string{"app": name}
	podLabels := a2aLabels(agent, a2aMCPComponent)
	podLabels["app"] = name

	probe := func(period, failure int32) *corev1.Probe {
		return &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
				Path: a2aMCPHealthPath, Port: intstr.FromInt32(a2aMCPPort),
			}},
			PeriodSeconds:    period,
			FailureThreshold: failure,
		}
	}

	return &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aLabels(agent, a2aMCPComponent)},
		Spec: appsv1.DeploymentSpec{
			// One, and Recreate, for the console's reasons
			// (buildA2AConsoleDeployment).
			Replicas: ptr.To(int32(1)),
			Strategy: appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType},
			Selector: &metav1.LabelSelector{MatchLabels: selector},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: podLabels},
				Spec: withA2AMCPBrokerAccess(agent, corev1.PodSpec{
					// It never talks to the API server.
					AutomountServiceAccountToken: ptr.To(false),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot:   ptr.To(true),
						RunAsUser:      ptr.To(a2aConsoleRunAsUser),
						FSGroup:        ptr.To(a2aConsoleFSGroup),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Volumes: []corev1.Volume{{
						// The console password and nothing else from the creds
						// Secret. Not optional, unlike the console's: without
						// it the server cannot reach the bus and refuses to
						// start, which is the honest failure.
						Name: a2aMCPBusKeyVolume,
						VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{
							SecretName: a2aCredsSecretName(agent),
							Items: []corev1.KeyToPath{{
								Key:  a2aConsolePasswordKey,
								Path: a2aConsolePasswordKey,
								Mode: ptr.To(a2aConsoleCredsFileMode),
							}},
						}},
					}, {
						Name: a2aMCPAccessVolume,
						VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{
							SecretName: a2aMCPAccessName(agent),
							Items: []corev1.KeyToPath{{
								Key:  a2aMCPAccessKey,
								Path: a2aMCPAccessKey,
								Mode: ptr.To(a2aConsoleCredsFileMode),
							}},
						}},
					}, a2aNATSCABundleVolume(agent)},
					Containers: []corev1.Container{{
						Name:    a2aMCPContainer,
						Image:   a2aConsoleImage(),
						Command: []string{a2aMCPCommand},
						// Distroless, run as 1000: see buildA2AConsoleDeployment.
						WorkingDir: "/",
						Env: []corev1.EnvVar{
							{Name: "NATS_URL", Value: a2aNATSClientURL(agent)},
							a2aNATSCAFileEnvVar(),
							{Name: a2aMCPEnvBusUser, Value: a2aConsoleConfUser},
							{Name: a2aMCPEnvPasswordFile, Value: path.Join(a2aMCPBusKeyMountDir, a2aConsolePasswordKey)},
							{Name: a2aMCPEnvAccessFile, Value: path.Join(a2aMCPAccessMountDir, a2aMCPAccessKey)},
							{Name: a2aMCPEnvListen, Value: fmt.Sprintf(":%d", a2aMCPPort)},
							{Name: a2aMCPEnvAgent, Value: agent.Name},
							{Name: a2aMCPEnvNamespace, Value: agent.Namespace},
							{Name: a2aMCPEnvGoMemLimit, Value: a2aMCPGoMemLimit()},
						},
						Ports: []corev1.ContainerPort{{Name: a2aMCPPortName, ContainerPort: a2aMCPPort}},
						VolumeMounts: []corev1.VolumeMount{
							{Name: a2aMCPBusKeyVolume, MountPath: a2aMCPBusKeyMountDir, ReadOnly: true},
							{Name: a2aMCPAccessVolume, MountPath: a2aMCPAccessMountDir, ReadOnly: true},
							a2aNATSCABundleMount(),
						},
						// /healthz is unauthenticated and says nothing but
						// that the process serves.
						StartupProbe:   probe(a2aMCPStartupPeriod, a2aMCPStartupFailure),
						ReadinessProbe: probe(a2aMCPReadinessPeriod, a2aMCPReadinessFailure),
						LivenessProbe:  probe(a2aMCPLivenessPeriod, a2aMCPLivenessFailure),
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse(a2aMCPCPURequest),
								corev1.ResourceMemory: resource.MustParse(a2aMCPMemoryRequest),
							},
							Limits: corev1.ResourceList{
								corev1.ResourceMemory: resource.MustParse(a2aMCPMemoryLimit),
							},
						},
						SecurityContext: &corev1.SecurityContext{
							AllowPrivilegeEscalation: ptr.To(false),
							ReadOnlyRootFilesystem:   ptr.To(true),
							Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
							SeccompProfile:           &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
						},
					}},
				}),
			},
		},
	}
}

// buildA2AMCPService is what `kubectl port-forward svc/...` names.
func buildA2AMCPService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	name := a2aMCPName(agent)
	return &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aLabels(agent, a2aMCPComponent)},
		Spec: corev1.ServiceSpec{
			Type:     corev1.ServiceTypeClusterIP,
			Selector: map[string]string{"app": name},
			Ports: []corev1.ServicePort{{
				Name:       a2aMCPPortName,
				Port:       a2aMCPPort,
				TargetPort: intstr.FromInt32(a2aMCPPort),
			}},
		},
	}
}

// buildA2AMCPNetworkPolicy is deny-all ingress on the MCP pod, as on the
// console's: the port-forward and the kubelet's probes enter from the node,
// which NetworkPolicy does not govern, and no pod has a reason to reach it.
func buildA2AMCPNetworkPolicy(agent *agentv1alpha1.PlatformAgent) *networkingv1.NetworkPolicy {
	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aMCPNetpolName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, a2aMCPNetpolComponent),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: map[string]string{"app": a2aMCPName(agent)}},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
		},
	}
}

// ensureA2AMCPToken mints the bearer token into its Secret when it is absent
// or unusable (a2aMCPTokenUsable), and never rotates one that is there: a
// person rotates it by editing the Secret, and the server reads it per
// request. An unowned Secret of that name is not adopted, as the door
// token's is not; unlike the door token's it is not an error either, for
// the reason reconcileA2AMCPBrokerIdentity gives for the account (#3871):
// the return says whether the token is ours and the server may render, and
// false is reported on the CR with a2aMCPTokenUnownedReason.
func (r *PlatformAgentReconciler) ensureA2AMCPToken(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (bool, error) {
	name := types.NamespacedName{Name: a2aMCPAccessName(agent), Namespace: agent.Namespace}
	existing := &corev1.Secret{}
	err := r.a2aReader().Get(ctx, name, existing)
	if err == nil {
		if !metav1.IsControlledBy(existing, agent) {
			logf.FromContext(ctx).Info("not adopting an unowned Secret as the MCP server's token; the MCP server is not rendered",
				"namespace", name.Namespace, "name", name.Name)
			return false, nil
		}
		if a2aMCPTokenUsable(existing.Data[a2aMCPAccessKey]) {
			return true, nil
		}
		token, err := randomA2AInjectToken()
		if err != nil {
			return false, fmt.Errorf("mint the MCP server's token: %w", err)
		}
		if existing.Data == nil {
			existing.Data = map[string][]byte{}
		}
		existing.Data[a2aMCPAccessKey] = []byte(token)
		return true, r.Update(ctx, existing)
	}
	if !errors.IsNotFound(err) {
		return false, err
	}
	token, err := randomA2AInjectToken()
	if err != nil {
		return false, fmt.Errorf("mint the MCP server's token: %w", err)
	}
	secret := &corev1.Secret{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{Name: name.Name, Namespace: name.Namespace, Labels: a2aLabels(agent, a2aMCPComponent)},
		Data:       map[string][]byte{a2aMCPAccessKey: []byte(token)},
	}
	if err := ctrl.SetControllerReference(agent, secret, r.Scheme); err != nil {
		return false, err
	}
	return true, r.Create(ctx, secret)
}

// a2aMCPTokenUsable reports whether a caller could present v as the token.
// The server trims the token file and refuses an empty one, so a
// whitespace-only value is empty here too; and it compares the rest byte for
// byte with an Authorization header's value, which cannot carry a control
// byte other than a tab (a CR or LF ends the header line, and Go's server
// refuses the rest), so a value holding one inside, from a two-line file
// given to --from-file, is a token nobody can present. Either is minted over.
func a2aMCPTokenUsable(v []byte) bool {
	v = bytes.TrimSpace(v)
	if len(v) == 0 {
		return false
	}
	for _, c := range v {
		// A tab is the one control byte a header value carries.
		if (c < 0x20 && c != '\t') || c == 0x7f {
			return false
		}
	}
	return true
}

// reconcileA2AMCP applies the MCP server under its flag and removes it
// without. Its fence is reconcileA2ANetworkFences's, applied before this
// runs, so the pod never runs unfenced. The return is the reason the server
// is not rendered with the flag on (a2aMCPTokenUnownedReason), or "": a
// token Secret under the reserved name that is not ours is reported, and
// the server removed, rather than an error (#3871). The fence stays: the
// fences pass re-applies it every pass while the flag is on, so removing
// it here would create and delete it on each pass, each an event that
// wakes the next (NetworkPolicy is an Owns() kind). A fence over nothing
// denies nothing.
func (r *PlatformAgentReconciler) reconcileA2AMCP(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, error) {
	if !a2aMCPDoorEnabled() {
		return "", r.removeA2AMCP(ctx, agent)
	}
	tokenOwned, err := r.ensureA2AMCPToken(ctx, agent)
	if err != nil {
		return "", fmt.Errorf("failed to ensure the MCP server's token: %w", err)
	}
	if !tokenOwned {
		return a2aMCPTokenUnownedReason, r.removeA2AMCPServer(ctx, agent)
	}
	for _, obj := range []client.Object{
		buildA2AMCPDeployment(agent),
		buildA2AMCPService(agent),
	} {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return "", err
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return "", fmt.Errorf("failed to apply the MCP server's %T: %w", obj, err)
		}
	}
	return "", nil
}

// removeA2AMCPServer is removeA2AMCP for a pass that keeps the flag on but
// may not render the server over an object it does not own: the Deployment
// and its Service go. The fence stands (see reconcileA2AMCP), and so does a
// token of ours: removing it would rotate the bearer token every client
// holds the moment the server renders again, a rotation nobody asked for,
// and a token with no server behind it is inert.
func (r *PlatformAgentReconciler) removeA2AMCPServer(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	for i, entry := range r.a2aMCPObjects(agent) {
		if i == a2aMCPFenceIndex || i == a2aMCPTokenIndex {
			continue
		}
		if err := r.deleteA2AObjectIfOwned(ctx, agent, entry.obj, entry.reader, nil); err != nil {
			return fmt.Errorf("failed to remove the MCP server's %T: %w", entry.obj, err)
		}
	}
	return nil
}

// removeA2AMCP takes the MCP server away: the Deployment, its Service and
// its token (a token left behind for a server that no longer exists is a
// credential nobody is reading), and the fence once no pod of it is left.
// Delete returns before the pod exits, so the fence waits for a later pass
// rather than leaving the terminating pod unfenced, the console fence's
// reason (a2aBusTeardown); a fence over nothing denies nothing and costs
// nothing in the meantime. The pod list runs only while a fence of ours
// stands, so an install that never set the flag pays a cached read for it.
//
// With the flag off this runs on every next reconcile, so an object under a
// reserved name that this PlatformAgent does not own, the fence included, is
// not ours to remove and must not stop the rest of the reconcile: it is
// skipped. With the flag on, an unowned token or account is reported on the
// CR and the server is not rendered (ensureA2AMCPToken,
// reconcileA2AMCPBrokerIdentity).
func (r *PlatformAgentReconciler) removeA2AMCP(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	noPodLeft := func() (bool, error) {
		pods := &corev1.PodList{}
		if err := r.podReader().List(ctx, pods, client.InNamespace(agent.Namespace),
			client.MatchingLabels{"app": a2aMCPName(agent)}); err != nil {
			return false, fmt.Errorf("failed to list the MCP server's pods: %w", err)
		}
		return len(pods.Items) == 0, nil
	}
	for i, entry := range r.a2aMCPObjects(agent) {
		var ready func() (bool, error)
		if i == a2aMCPFenceIndex {
			ready = noPodLeft
		}
		if err := r.deleteA2AObjectIfOwned(ctx, agent, entry.obj, entry.reader, ready); err != nil {
			return fmt.Errorf("failed to remove the MCP server's %T: %w", entry.obj, err)
		}
	}
	return nil
}

// deleteA2AObjectIfOwned removes one A2A object this agent owns, for a
// removal that runs whether or not the object was ever rendered: it reads
// obj once through reader (the cache for kinds the manager watches, a2aReader
// for a Secret; see removeA2AInjectBackend), and an object that is absent,
// or that this agent does not control, is left alone without an error --
// not ours to delete, and not a reason to stop the reconcile (#3871). ready,
// when set, is asked only of an object that is ours, and holds the delete
// for a later pass when it says not yet.
func (r *PlatformAgentReconciler) deleteA2AObjectIfOwned(ctx context.Context, agent *agentv1alpha1.PlatformAgent, obj client.Object, reader client.Reader, ready func() (bool, error)) error {
	if err := reader.Get(ctx, client.ObjectKeyFromObject(obj), obj); err != nil {
		return client.IgnoreNotFound(err)
	}
	if !metav1.IsControlledBy(obj, agent) {
		logf.FromContext(ctx).V(1).Info("skipping unowned A2A object under a reserved name",
			"kind", fmt.Sprintf("%T", obj), "namespace", obj.GetNamespace(), "name", obj.GetName())
		return nil
	}
	if ready != nil {
		if ok, err := ready(); err != nil || !ok {
			return err
		}
	}
	return client.IgnoreNotFound(r.Delete(ctx, obj))
}

// a2aMCPObjects names the MCP server's objects: the server, its Service, its
// fence (at a2aMCPFenceIndex), then its token (at a2aMCPTokenIndex), read
// uncached like every Secret here.
func (r *PlatformAgentReconciler) a2aMCPObjects(agent *agentv1alpha1.PlatformAgent) []a2aTeardownEntry {
	ns := agent.Namespace
	return []a2aTeardownEntry{
		{&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: a2aMCPName(agent), Namespace: ns}}, r.Client},
		{&corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: a2aMCPName(agent), Namespace: ns}}, r.Client},
		{&networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: a2aMCPNetpolName(agent), Namespace: ns}}, r.Client},
		{&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: a2aMCPAccessName(agent), Namespace: ns}}, r.a2aReader()},
	}
}
