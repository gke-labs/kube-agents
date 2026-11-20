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
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"path"
	"slices"
	"strconv"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	credentialProxyControlSecretSuffix = "-credential-proxy-control"           // #nosec G101 -- Secret name suffix, not a credential.
	credentialProxyControlDataKey      = "token"                               // #nosec G101 -- Secret data map key, not a credential.
	credentialProxyControlVolume       = "credential-proxy-control"            // #nosec G101 -- Volume name, not a credential.
	credentialProxyControlMountPath    = "/var/run/secrets/kubeagents/control" // #nosec G101 -- Mount directory path, not a credential.
	credentialProxyControlFileKey      = "token"                               // #nosec G101 -- Projected file path component, not a credential.
	credentialProxyStandbyEnv          = "CREDENTIAL_PROXY_STANDBY"            // #nosec G101 -- Environment variable name, not a credential.
	credentialProxyControlFileEnv      = "CREDENTIAL_PROXY_CONTROL_FILE"       // #nosec G101 -- Environment variable name, not a credential.
	credentialProxyPeerHealthURLEnv    = "CREDENTIAL_PROXY_PEER_HEALTH_URL"    // #nosec G101 -- Environment variable name, not a credential.
	credentialProxyStandbyEnabledValue = "1"

	credentialProxyTemplateHashAnnotation = "kubeagents.x-k8s.io/proxy-template-hash" // #nosec G101 -- Annotation key name, not a credential.
	kubectlRestartedAtAnnotation          = "kubectl.kubernetes.io/restartedAt"

	credentialProxyControlScheme          = "http"
	credentialProxyControlStatusPath      = "/control/status"
	credentialProxyControlActivatePath    = "/control/activate"
	credentialProxyControlDrainPath       = "/control/drain"
	credentialProxyControlBearerPrefix    = "Bearer "
	credentialProxyControlJSONContentType = "application/json"

	credentialProxyStateStandby  = "standby"
	credentialProxyStateActive   = "active"
	credentialProxyStateDraining = "draining"
	credentialProxyStateDrained  = "drained"

	credentialProxyLeaseSuffix          = "-credential-proxy"                          // #nosec G101 -- Kubernetes Lease name suffix, not a credential.
	credentialProxyLeaseLabel           = "kubeagents.x-k8s.io/credential-proxy-lease" // #nosec G101 -- Kubernetes Lease label key, not a credential.
	credentialProxyLeaseLabelValue      = "true"
	credentialProxyLeaseDurationSeconds = int32(30)

	credentialProxyControlHTTPTimeout     = 5 * time.Second
	credentialProxyControlRequeueInterval = 2 * time.Second
	credentialProxyControlRandomBytes     = 32
	credentialProxyControlMaxBodyBytes    = 4 << 20
	credentialProxyControlFileMode        = int32(0400)
)

type brokerTaskWriteBranch struct {
	Repository string  `json:"repository"`
	Branch     string  `json:"branch"`
	Task       string  `json:"task"`
	ExpiresAt  float64 `json:"expires_at"`
}

type brokerTaskWriteProposal struct {
	Repository string  `json:"repository"`
	Number     int     `json:"number"`
	Task       string  `json:"task"`
	ExpiresAt  float64 `json:"expires_at"`
}

type brokerTaskWritesState struct {
	Branches  []brokerTaskWriteBranch   `json:"branches,omitempty"`
	Proposals []brokerTaskWriteProposal `json:"proposals,omitempty"`
}

type brokerControlActivateRequest struct {
	TaskWrites *brokerTaskWritesState `json:"task_writes,omitempty"`
}

type brokerControlSnapshot struct {
	State         string                 `json:"state"`
	Warm          bool                   `json:"warm"`
	Enabled       bool                   `json:"enabled"`
	InFlight      int                    `json:"in_flight"`
	UnsettledChat int                    `json:"unsettled_chat"`
	TaskWrites    *brokerTaskWritesState `json:"task_writes,omitempty"`
	Error         string                 `json:"error,omitempty"`
}

func credentialProxyControlSecretName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + credentialProxyControlSecretSuffix
}

func credentialProxyControlFilePath() string {
	return path.Join(credentialProxyControlMountPath, credentialProxyControlFileKey)
}

const (
	credentialProxyHealthzPath         = "/healthz"
	credentialProxyPeerHealthURLFormat = "http://%s.%s.svc.cluster.local:%d%s"
)

func credentialProxyPeerHealthURL(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf(credentialProxyPeerHealthURLFormat, credentialProxyName(agent), agent.Namespace, credentialProxyPort, credentialProxyHealthzPath)
}

// credentialProxyActiveStandby reports whether the broker uses active-standby
// RollingUpdate (maxSurge=1, maxUnavailable=0) instead of Recreate. Only
// sizingProfile: auto enables it so default and fixed profiles keep Recreate.
func credentialProxyActiveStandby(agent *agentv1alpha1.PlatformAgent) bool {
	return agent != nil && agent.Spec.Deployment != nil && agent.Spec.Deployment.SizingProfile == sizingProfileAuto
}

func credentialProxyControlEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: credentialProxyStandbyEnv, Value: credentialProxyStandbyEnabledValue},
		{Name: credentialProxyControlFileEnv, Value: credentialProxyControlFilePath()},
		{Name: credentialProxyPeerHealthURLEnv, Value: credentialProxyPeerHealthURL(agent)},
	}
}

// canControlBroker reports whether this operator can reach the broker's
// control listener on port 8766. Off-cluster runs (POD_NAMESPACE unset or
// invalid) omit the operator NetworkPolicy peer on 8766 and cannot reach pod
// IPs, so standby mode is left off; tests inject BrokerControlClient directly.
func (r *PlatformAgentReconciler) canControlBroker() bool {
	if r.BrokerControlClient != nil {
		return true
	}
	_, reachable := operatorMetricsIngressRule(r.OperatorNamespace, credentialProxyMetricsPort)
	return reachable
}

// keepCredentialProxyStrategyRecreate switches only the Deployment strategy to
// Recreate without stripping standby env vars from the pod template, so a
// legacy pre-standby ReplicaSet scales to zero before the standby pod boots.
func keepCredentialProxyStrategyRecreate(dep *appsv1.Deployment) {
	if dep == nil {
		return
	}
	dep.Spec.Strategy = appsv1.DeploymentStrategy{
		Type: appsv1.RecreateDeploymentStrategyType,
	}
}

// disableCredentialProxyStandby strips standby env vars, control Secret mount,
// and control Secret volume, and reverts the Deployment strategy to Recreate
// when the operator cannot reach port 8766, so the broker boots directly into
// active mode without mounting an uncreated control Secret.
func disableCredentialProxyStandby(dep *appsv1.Deployment) {
	if dep == nil {
		return
	}
	keepCredentialProxyStrategyRecreate(dep)
	for i := range dep.Spec.Template.Spec.Containers {
		c := &dep.Spec.Template.Spec.Containers[i]
		if c.Name != credentialProxyContainerName {
			continue
		}
		filteredEnv := make([]corev1.EnvVar, 0, len(c.Env))
		for _, env := range c.Env {
			if env.Name != credentialProxyStandbyEnv &&
				env.Name != credentialProxyPeerHealthURLEnv &&
				env.Name != credentialProxyControlFileEnv {
				filteredEnv = append(filteredEnv, env)
			}
		}
		c.Env = filteredEnv
		filteredMounts := make([]corev1.VolumeMount, 0, len(c.VolumeMounts))
		for _, mount := range c.VolumeMounts {
			if mount.Name != credentialProxyControlVolume {
				filteredMounts = append(filteredMounts, mount)
			}
		}
		c.VolumeMounts = filteredMounts
	}
	filteredVolumes := make([]corev1.Volume, 0, len(dep.Spec.Template.Spec.Volumes))
	for _, vol := range dep.Spec.Template.Spec.Volumes {
		if vol.Name != credentialProxyControlVolume {
			filteredVolumes = append(filteredVolumes, vol)
		}
	}
	dep.Spec.Template.Spec.Volumes = filteredVolumes
}

// hasLegacyCredentialProxyPod reports whether any live or terminating broker
// pod still runs without CREDENTIAL_PROXY_STANDBY=1, requiring a Recreate hop
// before RollingUpdate so the old ReplicaSet cannot recreate an active peer.
func (r *PlatformAgentReconciler) hasLegacyCredentialProxyPod(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (bool, error) {
	pods := &corev1.PodList{}
	if err := r.podReader().List(ctx, pods, client.InNamespace(agent.Namespace), client.MatchingLabels(credentialProxySelector(agent))); err != nil {
		return false, fmt.Errorf("listing credential-proxy pods: %w", err)
	}
	for i := range pods.Items {
		pod := &pods.Items[i]
		if pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
			continue
		}
		if !podHasCredentialProxyStandby(pod) {
			return true, nil
		}
	}
	return false, nil
}

func podHasCredentialProxyStandby(pod *corev1.Pod) bool {
	c := brokerPodContainer(pod)
	if c == nil {
		return false
	}
	for _, env := range c.Env {
		if env.Name == credentialProxyStandbyEnv && env.Value == credentialProxyStandbyEnabledValue {
			return true
		}
	}
	return false
}

func credentialProxyControlVolumeSpec(agent *agentv1alpha1.PlatformAgent) corev1.Volume {
	return corev1.Volume{
		Name: credentialProxyControlVolume,
		VolumeSource: corev1.VolumeSource{
			Secret: &corev1.SecretVolumeSource{
				SecretName:  credentialProxyControlSecretName(agent),
				DefaultMode: ptr.To(credentialProxyControlFileMode),
			},
		},
	}
}

func credentialProxyControlMountSpec() corev1.VolumeMount {
	return corev1.VolumeMount{
		Name:      credentialProxyControlVolume,
		MountPath: credentialProxyControlMountPath,
		ReadOnly:  true,
	}
}

func credentialProxyLeaseName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + credentialProxyLeaseSuffix
}

func (r *PlatformAgentReconciler) brokerControlHTTPClient() *http.Client {
	if r.BrokerControlClient != nil {
		return r.BrokerControlClient
	}
	return &http.Client{Timeout: credentialProxyControlHTTPTimeout}
}

// ensureCredentialProxyControlSecret creates and owns the Secret holding the
// bearer credential shared with credential-proxy's /control/* routes.
func (r *PlatformAgentReconciler) ensureCredentialProxyControlSecret(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	name := credentialProxyControlSecretName(agent)
	existing := &corev1.Secret{}
	err := r.a2aReader().Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: name}, existing)
	if err == nil {
		if !metav1.IsControlledBy(existing, agent) {
			return fmt.Errorf("credential-proxy control Secret %s/%s already exists and is not owned by PlatformAgent %s", agent.Namespace, name, agent.Name)
		}
		if len(existing.Data[credentialProxyControlDataKey]) > 0 {
			return nil
		}
	} else if !apierrors.IsNotFound(err) {
		return fmt.Errorf("reading credential-proxy control Secret %s/%s: %w", agent.Namespace, name, err)
	}

	buf := make([]byte, credentialProxyControlRandomBytes)
	if _, randErr := rand.Read(buf); randErr != nil {
		return fmt.Errorf("generating credential-proxy control bearer: %w", randErr)
	}
	encoded := []byte(base64.RawURLEncoding.EncodeToString(buf))
	if apierrors.IsNotFound(err) {
		secret := &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{
				Name:      name,
				Namespace: agent.Namespace,
				Labels:    commonLabels(agent),
			},
			Type: corev1.SecretTypeOpaque,
			Data: map[string][]byte{credentialProxyControlDataKey: encoded},
		}
		if refErr := ctrl.SetControllerReference(agent, secret, r.Scheme); refErr != nil {
			return refErr
		}
		return r.Create(ctx, secret)
	}
	if existing.Data == nil {
		existing.Data = map[string][]byte{}
	}
	existing.Data[credentialProxyControlDataKey] = encoded
	return r.Update(ctx, existing)
}

func (r *PlatformAgentReconciler) readCredentialProxyControlBearer(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, bool, error) {
	sec := &corev1.Secret{}
	err := r.a2aReader().Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: credentialProxyControlSecretName(agent)}, sec)
	if apierrors.IsNotFound(err) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	raw := strings.TrimSpace(string(sec.Data[credentialProxyControlDataKey]))
	if raw == "" {
		return "", false, nil
	}
	return raw, true, nil
}

// ensureCredentialProxyLeaseHolder records the active credential-proxy pod in
// the operator-owned coordination.k8s.io/v1 Lease using live API reads so no
// Lease informer beyond the gateway's is consulted or missed.
func (r *PlatformAgentReconciler) ensureCredentialProxyLeaseHolder(ctx context.Context, agent *agentv1alpha1.PlatformAgent, holder string) error {
	name := credentialProxyLeaseName(agent)
	key := client.ObjectKey{Namespace: agent.Namespace, Name: name}
	now := metav1.NowMicro()
	live := &coordinationv1.Lease{}
	err := r.podReader().Get(ctx, key, live)
	if apierrors.IsNotFound(err) {
		labels := commonLabels(agent)
		labels[credentialProxyLeaseLabel] = credentialProxyLeaseLabelValue
		lease := &coordinationv1.Lease{
			ObjectMeta: metav1.ObjectMeta{
				Name:      name,
				Namespace: agent.Namespace,
				Labels:    labels,
			},
			Spec: coordinationv1.LeaseSpec{
				HolderIdentity:       ptr.To(holder),
				LeaseDurationSeconds: ptr.To(credentialProxyLeaseDurationSeconds),
				AcquireTime:          &now,
				RenewTime:            &now,
			},
		}
		if refErr := ctrl.SetControllerReference(agent, lease, r.Scheme); refErr != nil {
			return refErr
		}
		return r.Create(ctx, lease)
	}
	if err != nil {
		return fmt.Errorf("reading credential-proxy Lease %s: %w", key, err)
	}
	if ptr.Deref(live.Spec.HolderIdentity, "") == holder {
		return nil
	}
	updated := live.DeepCopy()
	if updated.Labels == nil {
		updated.Labels = map[string]string{}
	}
	updated.Labels[credentialProxyLeaseLabel] = credentialProxyLeaseLabelValue
	updated.Spec.HolderIdentity = ptr.To(holder)
	updated.Spec.LeaseDurationSeconds = ptr.To(credentialProxyLeaseDurationSeconds)
	updated.Spec.AcquireTime = &now
	updated.Spec.RenewTime = &now
	transitions := ptr.Deref(updated.Spec.LeaseTransitions, 0) + 1
	updated.Spec.LeaseTransitions = &transitions
	return r.Update(ctx, updated)
}

func mergeBrokerTaskWrites(dst **brokerTaskWritesState, src *brokerTaskWritesState) {
	if src == nil || (len(src.Branches) == 0 && len(src.Proposals) == 0) {
		return
	}
	if *dst == nil {
		*dst = &brokerTaskWritesState{}
	}
	for _, b := range src.Branches {
		idx := slices.IndexFunc((*dst).Branches, func(cur brokerTaskWriteBranch) bool {
			return cur.Repository == b.Repository && cur.Branch == b.Branch
		})
		if idx < 0 {
			(*dst).Branches = append((*dst).Branches, b)
		} else if (*dst).Branches[idx].Task == b.Task && b.ExpiresAt > (*dst).Branches[idx].ExpiresAt {
			(*dst).Branches[idx].ExpiresAt = b.ExpiresAt
		}
	}
	for _, p := range src.Proposals {
		idx := slices.IndexFunc((*dst).Proposals, func(cur brokerTaskWriteProposal) bool {
			return cur.Repository == p.Repository && cur.Number == p.Number
		})
		if idx < 0 {
			(*dst).Proposals = append((*dst).Proposals, p)
		} else if (*dst).Proposals[idx].Task == p.Task && p.ExpiresAt > (*dst).Proposals[idx].ExpiresAt {
			(*dst).Proposals[idx].ExpiresAt = p.ExpiresAt
		}
	}
}

func (r *PlatformAgentReconciler) queryBrokerControl(ctx context.Context, podIP, bearer, method, reqPath string, payload any) (int, *brokerControlSnapshot, error) {
	url := fmt.Sprintf("%s://%s%s", credentialProxyControlScheme, net.JoinHostPort(podIP, strconv.Itoa(int(credentialProxyMetricsPort))), reqPath)
	var bodyReader io.Reader
	if payload != nil {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return 0, nil, err
		}
		bodyReader = bytes.NewReader(encoded)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, bodyReader)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Authorization", credentialProxyControlBearerPrefix+bearer)
	if payload != nil {
		req.Header.Set("Content-Type", credentialProxyControlJSONContentType)
	}
	resp, err := r.brokerControlHTTPClient().Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	body, readErr := io.ReadAll(io.LimitReader(resp.Body, credentialProxyControlMaxBodyBytes))
	if readErr != nil {
		return resp.StatusCode, nil, readErr
	}
	var snap brokerControlSnapshot
	if len(body) > 0 {
		_ = json.Unmarshal(body, &snap)
	}
	return resp.StatusCode, &snap, nil
}

func brokerPodContainer(pod *corev1.Pod) *corev1.Container {
	for i := range pod.Spec.Containers {
		if pod.Spec.Containers[i].Name == credentialProxyContainerName {
			return &pod.Spec.Containers[i]
		}
	}
	return nil
}

func computeProxyTemplateHash(tpl *corev1.PodTemplateSpec) string {
	if tpl == nil {
		return ""
	}
	copyTpl := tpl.DeepCopy()
	if copyTpl.Annotations != nil {
		delete(copyTpl.Annotations, credentialProxyTemplateHashAnnotation)
		delete(copyTpl.Annotations, kubectlRestartedAtAnnotation)
		if len(copyTpl.Annotations) == 0 {
			copyTpl.Annotations = nil
		}
	}
	raw, err := json.Marshal(copyTpl)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func stampCredentialProxyTemplateHash(tpl *corev1.PodTemplateSpec) {
	if tpl == nil {
		return
	}
	hash := computeProxyTemplateHash(tpl)
	if hash == "" {
		return
	}
	if tpl.Annotations == nil {
		tpl.Annotations = map[string]string{}
	}
	tpl.Annotations[credentialProxyTemplateHashAnnotation] = hash
}

func proxyContainerMatchesTierResources(pod *corev1.Pod, want corev1.ResourceRequirements) bool {
	liveC := brokerPodContainer(pod)
	if liveC == nil {
		return false
	}
	for k, wantQty := range want.Requests {
		gotQty, ok := liveC.Resources.Requests[k]
		if !ok || gotQty.Cmp(wantQty) != 0 {
			return false
		}
	}
	for k, wantQty := range want.Limits {
		gotQty, ok := liveC.Resources.Limits[k]
		if !ok || gotQty.Cmp(wantQty) != 0 {
			return false
		}
	}
	return true
}

func proxyContainerMatchesTierResourcesForCluster(pod *corev1.Pod, want corev1.ResourceRequirements, clamped bool) bool {
	if clamped {
		adjusted := want.DeepCopy()
		adjusted.Limits = clampedLimits(want.Limits, effectiveRequests(want), true)
		return proxyContainerMatchesTierResources(pod, *adjusted)
	}
	if pod != nil && pod.Annotations[autopilotResourceAdjustmentAnnotation] != "" {
		if names, err := autopilotClampedLimits(corev1.PodTemplateSpec{ObjectMeta: pod.ObjectMeta}, credentialProxyContainerName); err == nil && len(names) > 0 {
			req := effectiveRequests(want)
			adjusted := want.DeepCopy()
			if adjusted.Limits != nil {
				for name := range names {
					if _, hasLim := adjusted.Limits[name]; hasLim {
						if rQty, hasReq := req[name]; hasReq {
							adjusted.Limits[name] = rQty.DeepCopy()
						}
					}
				}
			}
			return proxyContainerMatchesTierResources(pod, *adjusted)
		}
	}
	return proxyContainerMatchesTierResources(pod, want)
}

func matchesProxyPodTemplate(pod *corev1.Pod, dep *appsv1.Deployment) bool {
	if dep == nil {
		return false
	}
	if wantRestart := dep.Spec.Template.Annotations[kubectlRestartedAtAnnotation]; wantRestart != "" &&
		pod.Annotations[kubectlRestartedAtAnnotation] != wantRestart {
		return false
	}
	wantHash := dep.Spec.Template.Annotations[credentialProxyTemplateHashAnnotation]
	return wantHash != "" && pod.Annotations[credentialProxyTemplateHashAnnotation] == wantHash
}

func isPodReady(pod *corev1.Pod) bool {
	for _, cond := range pod.Status.Conditions {
		if cond.Type == corev1.PodReady {
			return cond.Status == corev1.ConditionTrue
		}
	}
	return false
}

type brokerPodProbe struct {
	pod      *corev1.Pod
	status   int
	snap     *brokerControlSnapshot
	matches  bool
	legacy   bool
	deleting bool
	err      error
}

// reconcileCredentialProxyHandoff coordinates zero-gap active-standby cutover
// for credential-proxy across surge rollouts and cold starts without giving
// the broker pod Kubernetes API credentials.
func (r *PlatformAgentReconciler) reconcileCredentialProxyHandoff(ctx context.Context, agent *agentv1alpha1.PlatformAgent, proxyDep *appsv1.Deployment) (time.Duration, error) {
	if !credentialProxyActiveStandby(agent) || !r.canControlBroker() {
		return 0, nil
	}
	pods := &corev1.PodList{}
	if err := r.podReader().List(ctx, pods, client.InNamespace(agent.Namespace), client.MatchingLabels(credentialProxySelector(agent))); err != nil {
		return 0, fmt.Errorf("listing credential-proxy pods: %w", err)
	}
	if len(pods.Items) == 0 {
		return 0, nil
	}
	bearer, ok, err := r.readCredentialProxyControlBearer(ctx, agent)
	if err != nil {
		return 0, err
	}
	if !ok {
		return 0, nil
	}

	log := logf.FromContext(ctx)
	var live []*corev1.Pod
	var deletingPods []*corev1.Pod
	hasPendingOrWarming := false
	for i := range pods.Items {
		pod := &pods.Items[i]
		if pod.DeletionTimestamp != nil {
			if pod.Status.Phase != corev1.PodSucceeded && pod.Status.Phase != corev1.PodFailed {
				deletingPods = append(deletingPods, pod)
			}
			continue
		}
		if pod.Status.Phase != corev1.PodRunning || pod.Status.PodIP == "" {
			hasPendingOrWarming = true
			continue
		}
		live = append(live, pod)
	}
	if len(live) == 0 {
		if hasPendingOrWarming || len(deletingPods) > 0 {
			return credentialProxyControlRequeueInterval, nil
		}
		return 0, nil
	}

	probed := make([]brokerPodProbe, 0, len(live))
	var targetCandidates []brokerPodProbe
	var retireOnly []brokerPodProbe
	for _, pod := range live {
		status, snap, qerr := r.queryBrokerControl(ctx, pod.Status.PodIP, bearer, http.MethodGet, credentialProxyControlStatusPath, nil)
		p := brokerPodProbe{
			pod:     pod,
			status:  status,
			snap:    snap,
			matches: matchesProxyPodTemplate(pod, proxyDep),
			legacy:  qerr == nil && status == http.StatusNotFound,
			err:     qerr,
		}
		if qerr != nil || (status != http.StatusOK && status != http.StatusNotFound) {
			hasPendingOrWarming = true
		}
		probed = append(probed, p)
		// Draining/drained pods cannot be selected or re-activated as targets
		// (for example on a fast tier rollback while an old drained pod is terminating).
		if snap != nil && (snap.State == credentialProxyStateDraining || snap.State == credentialProxyStateDrained) {
			retireOnly = append(retireOnly, p)
			continue
		}
		targetCandidates = append(targetCandidates, p)
	}
	if len(targetCandidates) == 0 {
		if len(retireOnly) > 0 {
			deletedDrained := false
			for _, p := range retireOnly {
				if p.pod.DeletionTimestamp != nil || p.snap == nil || p.snap.State != credentialProxyStateDrained {
					continue
				}
				// Delete a non-terminating drained pod when no replacement candidate is runnable
				// and either the reverted template matches it or no pending/warming replacement exists.
				if p.matches || !hasPendingOrWarming {
					if delErr := r.Delete(ctx, p.pod); client.IgnoreNotFound(delErr) != nil {
						return 0, delErr
					}
					log.Info("deleting drained credential-proxy pod with no runnable candidate", "pod", p.pod.Name, "namespace", agent.Namespace, "matchesTemplate", p.matches)
					deletedDrained = true
				}
			}
			if deletedDrained {
				return credentialProxyControlRequeueInterval, nil
			}
		}
		if hasPendingOrWarming || len(retireOnly) > 0 || len(deletingPods) > 0 {
			return credentialProxyControlRequeueInterval, nil
		}
		return 0, nil
	}

	// Pick target pod: prefer matching template, then active pod, then newest creation timestamp.
	slices.SortStableFunc(targetCandidates, func(a, b brokerPodProbe) int {
		if a.matches != b.matches {
			if a.matches {
				return -1
			}
			return 1
		}
		aActive := a.snap != nil && a.snap.State == credentialProxyStateActive
		bActive := b.snap != nil && b.snap.State == credentialProxyStateActive
		if aActive != bActive {
			if aActive {
				return -1
			}
			return 1
		}
		if !a.pod.CreationTimestamp.Equal(&b.pod.CreationTimestamp) {
			if a.pod.CreationTimestamp.After(b.pod.CreationTimestamp.Time) {
				return -1
			}
			return 1
		}
		return strings.Compare(a.pod.Name, b.pod.Name)
	})

	target := targetCandidates[0]
	if target.err != nil {
		return credentialProxyControlRequeueInterval, nil
	}
	if !target.legacy && (target.snap == nil || (!target.snap.Warm && target.snap.State != credentialProxyStateActive)) {
		return credentialProxyControlRequeueInterval, nil
	}

	// Probe any still-running deleting pods so we never activate a standby target
	// while a legacy or non-legacy active pod is still draining before exit.
	for _, pod := range deletingPods {
		if pod.Status.PodIP == "" {
			retireOnly = append(retireOnly, brokerPodProbe{pod: pod, deleting: true})
			continue
		}
		status, snap, qerr := r.queryBrokerControl(ctx, pod.Status.PodIP, bearer, http.MethodGet, credentialProxyControlStatusPath, nil)
		retireOnly = append(retireOnly, brokerPodProbe{
			pod:      pod,
			status:   status,
			snap:     snap,
			legacy:   qerr == nil && status == http.StatusNotFound,
			deleting: true,
			err:      qerr,
		})
	}

	// Drain or retire all non-target live and terminating pods before activating a standby target.
	waitingForDrain := false
	var handoffTaskWrites *brokerTaskWritesState
	others := make([]brokerPodProbe, 0, len(targetCandidates)-1+len(retireOnly))
	others = append(others, targetCandidates[1:]...)
	others = append(others, retireOnly...)
	for _, other := range others {
		if other.deleting && other.pod.Status.PodIP == "" {
			waitingForDrain = true
			continue
		}
		if other.legacy {
			if other.pod.DeletionTimestamp == nil {
				if delErr := r.Delete(ctx, other.pod); client.IgnoreNotFound(delErr) != nil {
					return 0, delErr
				}
				log.Info("retiring legacy credential-proxy pod for standby cutover", "pod", other.pod.Name, "target", target.pod.Name)
				waitingForDrain = true
			} else if other.pod.Status.Phase == corev1.PodRunning || isPodReady(other.pod) {
				waitingForDrain = true
			}
			continue
		}
		if other.err != nil || other.snap == nil {
			if isPodReady(other.pod) {
				waitingForDrain = true
			}
			continue
		}
		switch other.snap.State {
		case credentialProxyStateStandby:
			continue
		case credentialProxyStateDrained:
			mergeBrokerTaskWrites(&handoffTaskWrites, other.snap.TaskWrites)
			continue
		case credentialProxyStateDraining:
			waitingForDrain = true
		case credentialProxyStateActive:
			dStatus, dSnap, derr := r.queryBrokerControl(ctx, other.pod.Status.PodIP, bearer, http.MethodPost, credentialProxyControlDrainPath, nil)
			if derr != nil {
				waitingForDrain = true
				continue
			}
			if dStatus == http.StatusNotFound {
				if other.pod.DeletionTimestamp == nil {
					if delErr := r.Delete(ctx, other.pod); client.IgnoreNotFound(delErr) != nil {
						return 0, delErr
					}
				}
				waitingForDrain = true
				continue
			}
			if dSnap == nil || dSnap.State != credentialProxyStateDrained {
				waitingForDrain = true
				continue
			}
			mergeBrokerTaskWrites(&handoffTaskWrites, dSnap.TaskWrites)
		}
	}
	if waitingForDrain {
		return credentialProxyControlRequeueInterval, nil
	}

	if err := r.ensureCredentialProxyLeaseHolder(ctx, agent, target.pod.Name); err != nil {
		return 0, err
	}
	if !target.legacy && target.snap != nil && target.snap.State == credentialProxyStateStandby {
		var activateBody any
		if handoffTaskWrites != nil && (len(handoffTaskWrites.Branches) > 0 || len(handoffTaskWrites.Proposals) > 0) {
			activateBody = brokerControlActivateRequest{TaskWrites: handoffTaskWrites}
		}
		status, _, actErr := r.queryBrokerControl(ctx, target.pod.Status.PodIP, bearer, http.MethodPost, credentialProxyControlActivatePath, activateBody)
		if actErr != nil || status != http.StatusOK {
			log.Info("credential-proxy activation pending retry", "pod", target.pod.Name, "status", status, "err", actErr)
			return credentialProxyControlRequeueInterval, nil
		}
		log.Info("activated credential-proxy pod after standby warmup", "pod", target.pod.Name, "namespace", agent.Namespace)
		return credentialProxyControlRequeueInterval, nil
	}
	hasReadyActive := target.snap != nil && target.snap.State == credentialProxyStateActive && isPodReady(target.pod)
	if !hasReadyActive {
		for _, p := range probed {
			if p.snap != nil && p.snap.State == credentialProxyStateActive && isPodReady(p.pod) {
				hasReadyActive = true
				break
			}
		}
	}
	if hasReadyActive {
		deletedDrained := false
		for _, p := range probed {
			if p.pod.DeletionTimestamp == nil && p.snap != nil && p.snap.State == credentialProxyStateDrained {
				if delErr := r.Delete(ctx, p.pod); client.IgnoreNotFound(delErr) != nil {
					return 0, delErr
				}
				log.Info("deleting drained credential-proxy pod after active replacement is ready", "pod", p.pod.Name, "namespace", agent.Namespace)
				deletedDrained = true
			}
		}
		if deletedDrained {
			return credentialProxyControlRequeueInterval, nil
		}
	}
	if hasPendingOrWarming || len(probed) > 1 || len(deletingPods) > 0 {
		return credentialProxyControlRequeueInterval, nil
	}
	return 0, nil
}
