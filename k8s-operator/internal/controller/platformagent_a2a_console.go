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
	"net"
	"path"
	"strconv"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The console server: serves the console page, hands it the console bus
// credential, and proxies its websocket to the bus on 9222. It is reached with
// `kubectl port-forward svc/<agent>-a2a-console 8080:8080` and nothing else.
// The Service is ClusterIP and the pod's ingress is deny-all, so the only way
// in is the node path the port-forward takes.
const (
	// Release surface, resolved by a2aReleaseImage like the gateway, the
	// worker and the callout (the comment on a2aGatewayImageName says how).
	a2aConsoleImageName   = "a2a-console"
	a2aConsoleImageEnvVar = "A2A_CONSOLE_IMAGE"

	a2aConsoleComponent    = "console"
	a2aConsoleContainer    = "console"
	a2aConsoleNameSuffix   = "-a2a-console"
	a2aConsoleNetpolSuffix = "-a2a-console-netpol"
	// a2aConsoleNetpolComponent labels the console's deny-all NetworkPolicy;
	// derived from a2aConsoleComponent so the two can't drift apart.
	a2aConsoleNetpolComponent = a2aConsoleComponent + "-netpol"

	// Untyped on purpose, like a2aNATSClientPort: the host list wants an int
	// and the container port wants an int32.
	a2aConsolePort       = 8080
	a2aConsolePortName   = "http"
	a2aConsoleHealthPath = "/healthz"

	// The static user the server logs in as. consoleIdentity() renders the
	// same name into nats.conf, and TestTheConsoleServerLogsInAsTheConsoleIdentity
	// holds the two together.
	a2aConsoleConfUser = "console"

	// One key of the creds Secret, mounted as a directory rather than a
	// subPath so a rotated password reaches the pod.
	a2aConsoleCredsVolume   = "console-credential"
	a2aConsoleCredsMountDir = "/var/run/secrets/a2a-console" // #nosec G101 -- a path, not a credential
	// 0440 with FSGroup below: the kubelet makes the file group-owned by the
	// pod's fsGroup, so UID 1000 reads it and nobody else in the pod exists
	// to.
	a2aConsoleCredsFileMode int32 = 0o440
	a2aConsoleFSGroup       int64 = 1000
	// The container's non-root user. Distinct from a2aConsoleFSGroup only in
	// name: both name UID 1000, one as the process owner and one as the
	// group the kubelet grants the mounted file to.
	a2aConsoleRunAsUser int64 = 1000

	// The Host values the server answers to: the browser's own address bar
	// through the documented port-forward. A different local port is refused
	// with a 421 naming this one, which is cheaper to diagnose than a bus 403.
	a2aConsoleLocalHost    = "localhost"
	a2aConsoleLoopbackHost = "127.0.0.1"
	a2aConsoleHostSep      = ","
	// The scheme for both the console server's own origin (as the browser
	// asserts it) and the bus URL the proxy dials. Both are plain http: the
	// browser origin because the port-forward is unencrypted, and the bus
	// dial because the proxy upgrades the connection itself.
	a2aConsoleOriginScheme = "http://"
	a2aConsoleOriginSep    = ", "

	// The binary's environment (a2a/cmd/console).
	a2aConsoleEnvBusURL       = "CONSOLE_BUS_URL"
	a2aConsoleEnvUser         = "CONSOLE_USER"
	a2aConsoleEnvPasswordFile = "CONSOLE_PASSWORD_FILE"
	a2aConsoleEnvAllowedHosts = "CONSOLE_ALLOWED_HOSTS"

	a2aConsoleReadinessPeriod  = 5
	a2aConsoleReadinessFailure = 3
	a2aConsoleLivenessPeriod   = 10
	a2aConsoleLivenessFailure  = 6
	a2aConsoleCPURequest       = "10m"
	a2aConsoleMemoryRequest    = "32Mi"
	a2aConsoleMemoryLimit      = "128Mi"
)

func a2aConsoleImage() string {
	return a2aReleaseImage(a2aConsoleImageEnvVar, a2aConsoleImageName)
}

func a2aConsoleName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + a2aConsoleNameSuffix
}

func a2aConsoleNetpolName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + a2aConsoleNetpolSuffix
}

// a2aConsoleHosts is what the browser puts in Host through the documented
// port-forward, under either loopback name.
func a2aConsoleHosts() []string {
	port := strconv.Itoa(a2aConsolePort)
	return []string{
		net.JoinHostPort(a2aConsoleLocalHost, port),
		net.JoinHostPort(a2aConsoleLoopbackHost, port),
	}
}

// a2aConsoleOriginList is the nats.conf allowed_origins body: the console
// server's origin as the browser asserts it. The proxy forwards Origin
// unchanged, so this is the list the bus checks.
func a2aConsoleOriginList() string {
	hosts := a2aConsoleHosts()
	quoted := make([]string, 0, len(hosts))
	for _, h := range hosts {
		quoted = append(quoted, strconv.Quote(a2aConsoleOriginScheme+h))
	}
	return strings.Join(quoted, a2aConsoleOriginSep)
}

// a2aNATSWebSocketURL is the bus's websocket listener as the proxy dials it.
// http, not ws: the proxy is an HTTP reverse proxy and upgrades on the way
// through.
func a2aNATSWebSocketURL(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("%s%s.%s.svc:%d", a2aConsoleOriginScheme, a2aNATSName(agent), agent.Namespace, a2aNATSWebSocketPort)
}

// buildA2AConsoleDeployment renders the console server.
func buildA2AConsoleDeployment(agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	name := a2aConsoleName(agent)
	selector := map[string]string{"app": name}
	podLabels := a2aLabels(agent, a2aConsoleComponent)
	podLabels["app"] = name

	return &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aLabels(agent, a2aConsoleComponent)},
		Spec: appsv1.DeploymentSpec{
			// One. It serves a page to whoever holds a port-forward, and a
			// port-forward picks one pod anyway.
			Replicas: ptr.To(int32(1)),
			// Recreate, as the gateway is: at one replica the RollingUpdate
			// default needs a surge Pod, which can stall under the namespace
			// pod quota (#977, #1267). Set from the first render, because
			// switching an applied Deployment to Recreate later trips the
			// server-defaulted rollingUpdate block (a2aGatewayRecreateStrategyPatch).
			Strategy: appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType},
			Selector: &metav1.LabelSelector{MatchLabels: selector},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: podLabels},
				Spec: corev1.PodSpec{
					// It never talks to the API server.
					AutomountServiceAccountToken: ptr.To(false),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot:   ptr.To(true),
						RunAsUser:      ptr.To(a2aConsoleRunAsUser),
						FSGroup:        ptr.To(a2aConsoleFSGroup),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Volumes: []corev1.Volume{{
						Name: a2aConsoleCredsVolume,
						VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{
							SecretName: a2aCredsSecretName(agent),
							// One key out of a Secret that holds every static
							// user's password. TestTheConsoleCredentialVolumeProjectsOneKey
							// pins it. Optional, so a Secret missing the key
							// still lets the pod start: the file is then
							// absent and the server answers /config.json with
							// its designed 503 instead of the pod sitting in
							// ContainerCreating.
							Items: []corev1.KeyToPath{{
								Key:  a2aConsolePasswordKey,
								Path: a2aConsolePasswordKey,
								Mode: ptr.To(a2aConsoleCredsFileMode),
							}},
							Optional: ptr.To(true),
						}},
					}},
					Containers: []corev1.Container{{
						Name:  a2aConsoleContainer,
						Image: a2aConsoleImage(),
						// The image's WORKDIR is distroless's /home/nonroot,
						// 0700 owned by 65532, and this pod imposes 1000. The
						// server opens everything by absolute path and writes
						// nothing, so "/" is enough (#1259,
						// hardenedSecurityContext()).
						WorkingDir: "/",
						Env: []corev1.EnvVar{
							{Name: a2aConsoleEnvBusURL, Value: a2aNATSWebSocketURL(agent)},
							{Name: a2aConsoleEnvUser, Value: a2aConsoleConfUser},
							{Name: a2aConsoleEnvPasswordFile, Value: path.Join(a2aConsoleCredsMountDir, a2aConsolePasswordKey)},
							{Name: a2aConsoleEnvAllowedHosts, Value: strings.Join(a2aConsoleHosts(), a2aConsoleHostSep)},
						},
						Ports: []corev1.ContainerPort{{Name: a2aConsolePortName, ContainerPort: a2aConsolePort}},
						VolumeMounts: []corev1.VolumeMount{{
							Name:      a2aConsoleCredsVolume,
							MountPath: a2aConsoleCredsMountDir,
							ReadOnly:  true,
						}},
						// /healthz is the one path the server answers for any
						// Host. The kubelet sends the pod IP, which the Host
						// check would refuse everywhere else.
						ReadinessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
								Path: a2aConsoleHealthPath, Port: intstr.FromInt32(a2aConsolePort),
							}},
							PeriodSeconds:    a2aConsoleReadinessPeriod,
							FailureThreshold: a2aConsoleReadinessFailure,
						},
						LivenessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
								Path: a2aConsoleHealthPath, Port: intstr.FromInt32(a2aConsolePort),
							}},
							PeriodSeconds:    a2aConsoleLivenessPeriod,
							FailureThreshold: a2aConsoleLivenessFailure,
						},
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse(a2aConsoleCPURequest),
								corev1.ResourceMemory: resource.MustParse(a2aConsoleMemoryRequest),
							},
							Limits: corev1.ResourceList{
								corev1.ResourceMemory: resource.MustParse(a2aConsoleMemoryLimit),
							},
						},
						SecurityContext: &corev1.SecurityContext{
							AllowPrivilegeEscalation: ptr.To(false),
							ReadOnlyRootFilesystem:   ptr.To(true),
							Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
							SeccompProfile:           &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
						},
					}},
				},
			},
		},
	}
}

// buildA2AConsoleService is what `kubectl port-forward svc/...` names.
func buildA2AConsoleService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	name := a2aConsoleName(agent)
	return &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aLabels(agent, a2aConsoleComponent)},
		Spec: corev1.ServiceSpec{
			Type:     corev1.ServiceTypeClusterIP,
			Selector: map[string]string{"app": name},
			Ports: []corev1.ServicePort{{
				Name:       a2aConsolePortName,
				Port:       a2aConsolePort,
				TargetPort: intstr.FromInt32(a2aConsolePort),
			}},
		},
	}
}

// buildA2AConsoleNetworkPolicy is deny-all ingress on the console pod. Any pod
// that reached it could GET /config.json and read the console password, so no
// pod reaches it. The port-forward and the kubelet's probes enter from the
// node, which NetworkPolicy doesn't govern, so both still work.
func buildA2AConsoleNetworkPolicy(agent *agentv1alpha1.PlatformAgent) *networkingv1.NetworkPolicy {
	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aConsoleNetpolName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, a2aConsoleNetpolComponent),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: map[string]string{"app": a2aConsoleName(agent)}},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
		},
	}
}

// reconcileA2AConsole applies the console server. Its fence is applied by
// reconcileA2ANetworkFences with the bus's and the sessions', so that it
// holds through the skew freeze like they do.
func (r *PlatformAgentReconciler) reconcileA2AConsole(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	for _, obj := range []client.Object{
		buildA2AConsoleDeployment(agent),
		buildA2AConsoleService(agent),
	} {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return err
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply A2A console %T: %w", obj, err)
		}
	}
	return nil
}
