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
	"fmt"
	"slices"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// These pin the properties the shell sandbox exists to have, in the order the
// design doc argues for them. They are not coverage for the builders' plumbing:
// a StatefulSet whose replica count or image is wrong announces itself, while a
// StatefulSet that mounts a ServiceAccount token or throws its host keys away on
// a scale-down works perfectly right up until it matters.

func shellSandboxTestAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
}

func TestShellSandboxStatefulSetHasNoKubernetesCredential(t *testing.T) {
	sts := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "", "settings-hash")
	pod := sts.Spec.Template.Spec

	if pod.AutomountServiceAccountToken == nil || *pod.AutomountServiceAccountToken {
		t.Error("the sandbox must not mount a ServiceAccount token: it is the boundary this workload exists to draw")
	}
	// Its own ServiceAccount, never the agent's. The agent's carries
	// iam.gke.io/gcp-service-account, and Workload Identity resolves by pod IP —
	// so borrowing it would hand the shell container a full GSA token from
	// 169.254.169.254 whatever this pod does about token projection.
	agent := shellSandboxTestAgent()
	if pod.ServiceAccountName != shellSandboxName(agent) {
		t.Errorf("the sandbox must run under its own ServiceAccount %q, got %q",
			shellSandboxName(agent), pod.ServiceAccountName)
	}
	if pod.ShareProcessNamespace == nil || *pod.ShareProcessNamespace {
		t.Error("shareProcessNamespace must be explicitly false: /proc/<pid>/{environ,root} routes around the mount namespace that separates the shell from the credential proxy")
	}
	sa := buildShellSandboxServiceAccount(agent)
	if len(sa.Annotations) != 0 {
		t.Errorf("the sandbox ServiceAccount must carry no annotations — iam.gke.io/gcp-service-account there undoes the whole design — got %#v", sa.Annotations)
	}
	if pod.EnableServiceLinks == nil || *pod.EnableServiceLinks {
		t.Error("the sandbox must not get service-link env vars: they hand it a map of the namespace it has no use for")
	}
	// The whole list, by name, rather than a count: every volume here is a way to
	// put bytes into the pod the agent can run arbitrary commands in, so adding one
	// should be a decision someone makes on purpose. Exactly four are allowed —
	// the authorized-keys Secret, the SETTINGS.md ConfigMap, the token the shell
	// presents to the credential runtime, and the empty mount over ~/.hermes.
	// Anything else fails here and gets argued about in review.
	//
	// The third one is a credential, unlike the others, and it is here because
	// the alternative is not "the sandbox holds nothing" but "the sandbox cannot
	// run a command": the broker authenticates its callers at every placement the
	// sandbox exists in. What keeps it from being the thing this test is named
	// after is the audience, asserted below.
	//
	// The fourth carries nothing in either direction — it exists so the path
	// cannot be replaced with a writable directory, which is the one write channel
	// the sandbox has back into the agent pod. See shellSandboxHermesHomePath;
	// that it is empty and read-only is asserted below.
	allowed := map[string]bool{
		shellSandboxKeysVolume:                 true,
		shellSandboxSettingsVolume:             true,
		shellSandboxCredentialProxyTokenVolume: true,
		shellSandboxHermesHomeVolume:           true,
	}
	byName := map[string]corev1.Volume{}
	for _, v := range pod.Volumes {
		if !allowed[v.Name] {
			t.Errorf("unexpected volume %q in the sandbox pod: %#v", v.Name, v.VolumeSource)
		}
		byName[v.Name] = v
	}
	keys, ok := byName[shellSandboxKeysVolume]
	if !ok {
		t.Fatalf("expected the %q volume, got %#v", shellSandboxKeysVolume, pod.Volumes)
	}
	// One Secret, one key from it, and it is a public key.
	secret := keys.Secret
	if secret == nil {
		t.Fatalf("expected the authorized-keys Secret volume, got %#v", keys.VolumeSource)
	}
	if len(secret.Items) != 1 || secret.Items[0].Key != "authorized_keys" {
		t.Errorf("expected only the authorized_keys item from the Secret, got %#v", secret.Items)
	}
	// The other one is a ConfigMap, which is the part that matters: a Secret named
	// here would be a credential arriving by the same route.
	if settings, ok := byName[shellSandboxSettingsVolume]; ok && settings.ConfigMap == nil {
		t.Errorf("expected %q to be a ConfigMap, got %#v", shellSandboxSettingsVolume, settings.VolumeSource)
	}
	// The broker token, and the audience is the whole of why mounting it does not
	// contradict AutomountServiceAccountToken: false above. A token minted for
	// this audience is refused by the Kubernetes API server, so what the shell
	// holds opens the broker and nothing else.
	token, ok := byName[shellSandboxCredentialProxyTokenVolume]
	if !ok {
		t.Fatalf("expected the %q volume, got %#v", shellSandboxCredentialProxyTokenVolume, pod.Volumes)
	}
	if token.Projected == nil || len(token.Projected.Sources) != 1 ||
		token.Projected.Sources[0].ServiceAccountToken == nil {
		t.Fatalf("expected a single projected ServiceAccount token, got %#v", token.VolumeSource)
	}
	projection := token.Projected.Sources[0].ServiceAccountToken
	if projection.Audience != credentialProxyAudience {
		t.Errorf("expected the token to be minted for %q, got %q — an unaudienced token is a Kubernetes API credential",
			credentialProxyAudience, projection.Audience)
	}
	if projection.ExpirationSeconds == nil || *projection.ExpirationSeconds > 3600 {
		t.Errorf("expected an expiry of at most an hour, got %v", projection.ExpirationSeconds)
	}
	// The sync block. An EmptyDir with nothing behind it, mounted read-only:
	// anything else here would be bytes crossing at a path whose whole purpose is
	// that nothing does. Read-only is the half that matters — a writable mount
	// leaves Hermes' file sync copying the sandbox's ~/.hermes back onto the agent
	// pod, which is what shellSandboxHermesHomePath exists to stop.
	block, ok := byName[shellSandboxHermesHomeVolume]
	if !ok {
		t.Fatalf("expected the %q volume, got %#v", shellSandboxHermesHomeVolume, pod.Volumes)
	}
	if block.EmptyDir == nil {
		t.Errorf("expected %q to be an EmptyDir, got %#v", shellSandboxHermesHomeVolume, block.VolumeSource)
	}
	var mount *corev1.VolumeMount
	for i, m := range pod.Containers[0].VolumeMounts {
		if m.Name == shellSandboxHermesHomeVolume {
			mount = &pod.Containers[0].VolumeMounts[i]
		}
	}
	if mount == nil {
		t.Fatalf("expected %q to be mounted, got %#v", shellSandboxHermesHomeVolume, pod.Containers[0].VolumeMounts)
	}
	if mount.MountPath != shellSandboxHermesHomePath {
		t.Errorf("expected the mount at %q, got %q", shellSandboxHermesHomePath, mount.MountPath)
	}
	if !mount.ReadOnly {
		t.Errorf("expected %q to be mounted read-only", shellSandboxHermesHomeVolume)
	}
}

func TestShellSandboxPresentsATokenTheBrokerWillAccept(t *testing.T) {
	// The sandbox is where every credentialed command runs, and the broker
	// authenticates its callers whenever it is off the agent's pod — which the
	// sandbox being on already guarantees. Three things have to line up or every
	// wrapper gets a 401 from a listener it can reach: the token is projected, the
	// shell is told where to read it, and the broker's allowlist names the
	// identity that minted it. They live in three files, so assert them together.
	agent := shellSandboxAgent(true)
	url := "http://test-agent-credential-proxy:8765"
	sts := buildShellSandboxStatefulSet(agent, "sandbox-ssh", url, "settings-hash")

	shell := sts.Spec.Template.Spec.Containers[0]
	var tokenFile string
	for _, env := range shell.Env {
		if env.Name == "CREDENTIAL_PROXY_TOKEN_FILE" {
			tokenFile = env.Value
		}
	}
	want := credentialProxyTokenMountPath + "/token"
	if tokenFile != want {
		t.Errorf("expected the shell to read its token from %q, got %q", want, tokenFile)
	}

	var mounted bool
	for _, mount := range shell.VolumeMounts {
		if mount.Name == shellSandboxCredentialProxyTokenVolume {
			mounted = true
			if mount.MountPath != credentialProxyTokenMountPath {
				t.Errorf("expected the token at %q, got %q", credentialProxyTokenMountPath, mount.MountPath)
			}
			if !mount.ReadOnly {
				t.Error("the token mount must be read-only")
			}
		}
	}
	if !mounted {
		t.Errorf("the shell container mounts no token, so the path in its env names nothing: %#v", shell.VolumeMounts)
	}

	// Readable by the login the model's commands run as. The agent pod projects
	// the same token 0400 and gets away with it; here 0400 would leave the file
	// unreadable by uid 1000 and the failure would look like a broker problem.
	for _, volume := range sts.Spec.Template.Spec.Volumes {
		if volume.Name != shellSandboxCredentialProxyTokenVolume {
			continue
		}
		if volume.Projected == nil || volume.Projected.DefaultMode == nil ||
			*volume.Projected.DefaultMode&0044 == 0 {
			t.Errorf("expected a mode uid %d can read, got %#v", shellSandboxUID, volume.Projected)
		}
	}

	callers := allowedBrokerCallers(agent)
	sandboxCaller := "system:serviceaccount:" + agent.Namespace + ":" + shellSandboxServiceAccountName(agent)
	if !strings.Contains(callers, sandboxCaller) {
		t.Errorf("the broker must serve the sandbox's identity %q, got %q", sandboxCaller, callers)
	}
	// And still the agent's: the gateway's chat relays call the same listener.
	agentCaller := "system:serviceaccount:" + agent.Namespace + ":" + agentServiceAccountName(agent)
	if !strings.Contains(callers, agentCaller) {
		t.Errorf("the broker must still serve the agent's identity %q, got %q", agentCaller, callers)
	}
}

func TestShellSandboxRetainsItsVolumesOnDeleteAndScale(t *testing.T) {
	// Hermes connects with StrictHostKeyChecking=accept-new and the host keys
	// live on this volume, so a reclaimed claim is not a lost cache — it is every
	// subsequent command failing until known_hosts is edited by hand.
	sts := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "", "settings-hash")
	policy := sts.Spec.PersistentVolumeClaimRetentionPolicy
	if policy == nil {
		t.Fatal("expected an explicit PersistentVolumeClaimRetentionPolicy; the default is Retain today and is not guaranteed to stay so")
	}
	if policy.WhenDeleted != appsv1.RetainPersistentVolumeClaimRetentionPolicyType {
		t.Errorf("expected WhenDeleted=Retain, got %s", policy.WhenDeleted)
	}
	if policy.WhenScaled != appsv1.RetainPersistentVolumeClaimRetentionPolicyType {
		t.Errorf("expected WhenScaled=Retain, got %s", policy.WhenScaled)
	}
	claims := map[string]bool{}
	for _, c := range sts.Spec.VolumeClaimTemplates {
		claims[c.Name] = true
	}
	// Two, and the split is the point: the host keys must not sit on the volume
	// whose mount point uid 1000 owns. See shellSandboxSshdPath.
	if len(claims) != 2 || !claims[shellSandboxDataVolume] || !claims[shellSandboxSshdVolume] {
		t.Fatalf("expected %q and %q volumeClaimTemplates, got %#v",
			shellSandboxDataVolume, shellSandboxSshdVolume, sts.Spec.VolumeClaimTemplates)
	}
}

func TestShellSandboxMountsMatchTheImage(t *testing.T) {
	// deploy/sandbox/entrypoint.sh reads both paths and exits if either is wrong.
	// The failure is loud, but it is loud in a pod's logs rather than in CI.
	sts := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "", "settings-hash")
	containers := sts.Spec.Template.Spec.Containers
	if len(containers) != 1 {
		t.Fatalf("expected a single container, got %d", len(containers))
	}
	mounts := map[string]corev1.VolumeMount{}
	for _, m := range containers[0].VolumeMounts {
		mounts[m.Name] = m
	}
	if got := mounts[shellSandboxKeysVolume]; got.MountPath != shellSandboxKeysPath || !got.ReadOnly {
		t.Errorf("expected %s mounted read-only at %s, got %#v", shellSandboxKeysVolume, shellSandboxKeysPath, got)
	}
	if got := mounts[shellSandboxDataVolume]; got.MountPath != shellSandboxDataPath {
		t.Errorf("expected %s mounted at %s, got %#v", shellSandboxDataVolume, shellSandboxDataPath, got)
	}
	if got := mounts[shellSandboxSshdVolume]; got.MountPath != shellSandboxSshdPath {
		t.Errorf("expected %s mounted at %s, got %#v", shellSandboxSshdVolume, shellSandboxSshdPath, got)
	}
	// A regression guard with a security consequence rather than a cosmetic one:
	// nested under the data path, the host keys are back on a volume the model
	// can rename entries in, and the pinned host key stops meaning anything.
	if strings.HasPrefix(shellSandboxSshdPath, shellSandboxDataPath+"/") {
		t.Errorf("the sshd state path %s is inside the model's data path %s", shellSandboxSshdPath, shellSandboxDataPath)
	}
	if containers[0].Command != nil || containers[0].Args != nil {
		t.Error("the image's entrypoint owns startup; a command or args here bypasses the volume-dependent setup")
	}
	// The baseline quota in kubeagents-system rejects a pod that omits either,
	// and the rejection surfaces as a StatefulSet that never creates a pod.
	if containers[0].Resources.Requests == nil || containers[0].Resources.Limits == nil {
		t.Error("expected both resource requests and limits")
	}
}

func TestShellSandboxGetsTheSameSettingsFileAsTheAgent(t *testing.T) {
	// Six skills read SETTINGS.md by path, and reading a file is a shell tool now.
	// The image cannot carry it — the content is per-install, rendered from the CR
	// — so it is the one part of the delivery set that arrives as a mount. Everything
	// else is baked at /opt/defaults and synced by deploy/sandbox/entrypoint.sh.
	agent := shellSandboxTestAgent()
	sts := buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "settings-hash")

	var mount *corev1.VolumeMount
	for i, m := range sts.Spec.Template.Spec.Containers[0].VolumeMounts {
		if m.Name == shellSandboxSettingsVolume {
			mount = &sts.Spec.Template.Spec.Containers[0].VolumeMounts[i]
		}
	}
	if mount == nil {
		t.Fatalf("expected a %s mount on the sandbox container", shellSandboxSettingsVolume)
	}
	// The path the skills name, and subPath so the ConfigMap lands as one file
	// rather than replacing the data volume's whole directory.
	if want := shellSandboxDataPath + "/" + settingsFileName; mount.MountPath != want {
		t.Errorf("expected SETTINGS.md at %s, got %s", want, mount.MountPath)
	}
	if mount.SubPath != settingsFileName {
		t.Errorf("expected subPath %s, got %q — a directory mount here hides the synced tree", settingsFileName, mount.SubPath)
	}
	if !mount.ReadOnly {
		t.Error("expected the settings mount to be read-only")
	}

	var vol *corev1.Volume
	for i, v := range sts.Spec.Template.Spec.Volumes {
		if v.Name == shellSandboxSettingsVolume {
			vol = &sts.Spec.Template.Spec.Volumes[i]
		}
	}
	if vol == nil || vol.ConfigMap == nil {
		t.Fatalf("expected a ConfigMap volume named %s, got %#v", shellSandboxSettingsVolume, vol)
	}
	// The same object the agent container mounts, so the two sides cannot disagree
	// about what the install's scope is.
	if vol.ConfigMap.Name != settingsConfigMapName(agent) {
		t.Errorf("expected the agent's settings ConfigMap %q, got %q", settingsConfigMapName(agent), vol.ConfigMap.Name)
	}
	if vol.ConfigMap.Name != buildSettingsConfigMap(agent).Name {
		t.Errorf("the sandbox mounts %q but the reconciler writes %q", vol.ConfigMap.Name, buildSettingsConfigMap(agent).Name)
	}
	// Optional, unlike the agent container's copy. The reconciler writes the
	// ConfigMap before the StatefulSet, but they are separate objects: a sandbox
	// that will not start because one is briefly missing takes the whole shell down,
	// while a skill reading an absent SETTINGS.md fails on its own terms.
	if vol.ConfigMap.Optional == nil || !*vol.ConfigMap.Optional {
		t.Error("expected the settings ConfigMap to be optional for the sandbox")
	}
}

func TestShellSandboxRollsWhenSettingsChange(t *testing.T) {
	// A subPath mount is resolved once at pod start, so a ConfigMap edit alone does
	// not reach a running sandbox. The agent's Deployment carries the same hash
	// annotation for the same reason; without it here, editing the CR's scope rolls
	// the agent onto the new SETTINGS.md and leaves the sandbox — where the shell
	// actually reads it — serving the old one indefinitely.
	agent := shellSandboxTestAgent()
	const key = "kubeagents.x-k8s.io/settings-config-hash"

	first := buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "hash-one")
	if got := first.Spec.Template.Annotations[key]; got != "hash-one" {
		t.Fatalf("expected %s=hash-one on the pod template, got %q", key, got)
	}
	second := buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "hash-two")
	if first.Spec.Template.Annotations[key] == second.Spec.Template.Annotations[key] {
		t.Error("a different settings hash must change the pod template, or nothing restarts")
	}
}

func TestShellSandboxCredentialProxyURLIsOptional(t *testing.T) {
	// Empty is the state until #737 Part C, and it has to be a working state: the
	// entrypoint warns and starts, so file and code-execution tools function while
	// the credentialed wrappers report that they are unconfigured.
	withoutURL := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "", "settings-hash")
	for _, env := range withoutURL.Spec.Template.Spec.Containers[0].Env {
		if env.Name == "CREDENTIAL_PROXY_URL" {
			t.Errorf("expected no CREDENTIAL_PROXY_URL when none was resolved, got %q", env.Value)
		}
	}

	withURL := buildShellSandboxStatefulSet(shellSandboxTestAgent(), "sandbox-ssh", "http://test-agent-credential-proxy:8765", "settings-hash")
	var found string
	for _, env := range withURL.Spec.Template.Spec.Containers[0].Env {
		if env.Name == "CREDENTIAL_PROXY_URL" {
			found = env.Value
		}
	}
	if found != "http://test-agent-credential-proxy:8765" {
		t.Errorf("expected the resolved credential proxy URL in the pod env, got %q", found)
	}
}

func TestShellSandboxServiceIsHeadlessAndPublishesTheStableName(t *testing.T) {
	agent := shellSandboxTestAgent()
	svc := buildShellSandboxService(agent)

	if svc.Spec.ClusterIP != corev1.ClusterIPNone {
		t.Errorf("the governing Service must be headless or the per-pod DNS record does not exist, got %q", svc.Spec.ClusterIP)
	}
	if !svc.Spec.PublishNotReadyAddresses {
		t.Error("expected PublishNotReadyAddresses: the pod is addressable while sshd generates host keys on a first start")
	}
	if svc.Name != buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "settings-hash").Spec.ServiceName {
		t.Errorf("the StatefulSet's serviceName must be this Service, got %q vs %q",
			buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "settings-hash").Spec.ServiceName, svc.Name)
	}
	// The host Hermes dials has to be resolvable by this Service, which means
	// <pod>.<service>.<namespace>.svc and nothing else.
	host := shellSandboxHost(agent)
	if want := "test-agent-shell-0.test-agent-shell.test-ns.svc.cluster.local"; host != want {
		t.Errorf("expected %q, got %q", want, host)
	}
	if !strings.Contains(host, "."+svc.Name+".") {
		t.Errorf("host %q does not route through Service %q", host, svc.Name)
	}
}

func TestShellSandboxNetworkPolicyDeniesByDefault(t *testing.T) {
	np := buildShellSandboxNetworkPolicy(shellSandboxTestAgent(), nil)

	types := map[networkingv1.PolicyType]bool{}
	for _, t := range np.Spec.PolicyTypes {
		types[t] = true
	}
	if !types[networkingv1.PolicyTypeIngress] || !types[networkingv1.PolicyTypeEgress] {
		t.Fatalf("both policy types must be named or the unnamed direction is unrestricted, got %v", np.Spec.PolicyTypes)
	}

	// Ingress: the agent pod, on sshd's port, and nothing else. A rule with an
	// empty From or empty Ports is an open door that looks like a closed one.
	if len(np.Spec.Ingress) != 1 {
		t.Fatalf("expected exactly one ingress rule, got %d", len(np.Spec.Ingress))
	}
	in := np.Spec.Ingress[0]
	if len(in.From) != 1 || in.From[0].PodSelector == nil ||
		in.From[0].PodSelector.MatchLabels["app"] != "test-agent-gateway" {
		t.Errorf("expected ingress only from the gateway pod, got %#v", in.From)
	}
	if len(in.Ports) != 1 || in.Ports[0].Port.IntValue() != shellSandboxPort {
		t.Errorf("expected ingress only on %d, got %#v", shellSandboxPort, in.Ports)
	}

	// Egress: DNS and the credential proxy. Anything else reachable from here is
	// a path out of the sandbox that the incident this design answers used.
	if len(np.Spec.Egress) != 2 {
		t.Fatalf("expected exactly two egress rules (DNS, credential proxy), got %d", len(np.Spec.Egress))
	}
	for i, rule := range np.Spec.Egress {
		if len(rule.To) == 0 {
			t.Errorf("egress rule %d has no peers, which permits egress to everywhere", i)
		}
		if len(rule.Ports) == 0 {
			t.Errorf("egress rule %d has no ports, which permits every port on its peers", i)
		}
	}
	proxy := np.Spec.Egress[1]
	if proxy.Ports[0].Port.IntValue() != credentialProxyPort {
		t.Errorf("expected the credential proxy port %d, got %#v", credentialProxyPort, proxy.Ports[0].Port)
	}
}

func TestShellSandboxDNSEgressNamesEveryClusterDNSPeer(t *testing.T) {
	// This rule named the kube-dns podSelector alone, and on a live cluster running
	// NodeLocal DNSCache every lookup from the sandbox failed with "Temporary failure
	// in name resolution" — reported as the credential proxy being down, because the
	// proxy is the first name the sandbox resolves. The four peers below are what the
	// gateway policy has always had; asserting them here is what keeps the two from
	// drifting apart again, since only one of them is exercised by a golden file.
	const resolvedVIP = "10.4.0.10"
	np := buildShellSandboxNetworkPolicy(shellSandboxTestAgent(), []string{resolvedVIP})

	dns := np.Spec.Egress[0]
	var podSelectors, cidrs []string
	for _, peer := range dns.To {
		if peer.PodSelector != nil {
			podSelectors = append(podSelectors, peer.PodSelector.MatchLabels["k8s-app"])
		}
		if peer.IPBlock != nil {
			cidrs = append(cidrs, peer.IPBlock.CIDR)
		}
	}
	for _, want := range []string{"kube-dns", "node-local-dns"} {
		if !slices.Contains(podSelectors, want) {
			t.Errorf("DNS egress does not select the %s pods, got %v", want, podSelectors)
		}
	}
	for _, want := range []string{nodeLocalDNSCacheIP, resolvedVIP + "/32"} {
		if !slices.Contains(cidrs, want) {
			t.Errorf("DNS egress does not reach %s, got %v", want, cidrs)
		}
	}
}

func TestShellSandboxObjectsShareOneSelector(t *testing.T) {
	// Three objects, one label set. A Service that selects nothing and a
	// NetworkPolicy that constrains nothing both look healthy in `kubectl get`.
	agent := shellSandboxTestAgent()
	sts := buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "settings-hash")
	svc := buildShellSandboxService(agent)
	np := buildShellSandboxNetworkPolicy(agent, nil)

	podLabels := sts.Spec.Template.ObjectMeta.Labels
	for name, selector := range map[string]map[string]string{
		"StatefulSet.spec.selector": sts.Spec.Selector.MatchLabels,
		"Service.spec.selector":     svc.Spec.Selector,
		"NetworkPolicy.podSelector": np.Spec.PodSelector.MatchLabels,
	} {
		for k, v := range selector {
			if podLabels[k] != v {
				t.Errorf("%s wants %s=%s, which the pod template does not carry (%v)", name, k, v, podLabels)
			}
		}
		if len(selector) == 0 {
			t.Errorf("%s is empty, which selects every pod in the namespace", name)
		}
	}
}

func TestSandboxDataClaimIsNoSmallerThanTheAgentsOwn(t *testing.T) {
	// sandbox_mirror.py copies a subset of the agent's /opt/data into the
	// sandbox's on upgrade. Destination >= source is what makes that fit by
	// construction, and it is the reason the mirror carries no byte cap of its
	// own — size these apart again and the migration starts truncating silently.
	agent := shellSandboxTestAgent()
	sts := buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "settings-hash")

	var claim *corev1.PersistentVolumeClaim
	for i := range sts.Spec.VolumeClaimTemplates {
		if sts.Spec.VolumeClaimTemplates[i].Name == shellSandboxDataVolume {
			claim = &sts.Spec.VolumeClaimTemplates[i]
		}
	}
	if claim == nil {
		t.Fatalf("no %q volumeClaimTemplate on the sandbox StatefulSet", shellSandboxDataVolume)
	}

	sandbox := claim.Spec.Resources.Requests[corev1.ResourceStorage]
	agentData := buildPVC(agent).Spec.Resources.Requests[corev1.ResourceStorage]
	if sandbox.Cmp(agentData) < 0 {
		t.Errorf("sandbox data claim is %s against the agent's %s; the migration cannot be guaranteed to fit",
			sandbox.String(), agentData.String())
	}
}

func TestSandboxDataClaimNameMatchesWhatTheStatefulSetCreates(t *testing.T) {
	// The operator patches this claim by name to widen it on upgrade. A name
	// that does not exist is a Get that returns NotFound, which this code reads
	// as "first install, nothing to do" — so a wrong name fails as a silent
	// no-op rather than an error.
	agent := shellSandboxTestAgent()
	sts := buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "settings-hash")

	want := shellSandboxDataVolume + "-" + sts.Name + "-0"
	if got := shellSandboxDataClaimName(agent); got != want {
		t.Errorf("shellSandboxDataClaimName = %q, want %q", got, want)
	}
	if got := *sts.Spec.Replicas; got != 1 {
		t.Errorf("replicas = %d; the claim name above assumes the single ordinal 0", got)
	}
}

func TestResolveShellSandboxImageHonoursTheMirrorOverride(t *testing.T) {
	agent := shellSandboxTestAgent()

	t.Setenv(shellSandboxImageEnvVar, "registry.example.com/mirror/agent-sandbox:v1.2.3")
	if got := resolveShellSandboxImage(agent); got != "registry.example.com/mirror/agent-sandbox:v1.2.3" {
		t.Errorf("expected the %s override to win, got %q", shellSandboxImageEnvVar, got)
	}

	// A per-agent image beats the controller-wide one: the override exists for an
	// install mirroring every image, the CR field for one agent being moved.
	withImage := shellSandboxTestAgent()
	withImage.Spec.Harness = &agentv1alpha1.HarnessSpec{
		Experimental: &agentv1alpha1.ExperimentalSpec{
			ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Image: "registry.example.com/team/agent-sandbox:dev"},
		},
	}
	if got := resolveShellSandboxImage(withImage); got != "registry.example.com/team/agent-sandbox:dev" {
		t.Errorf("expected the CR image to win over %s, got %q", shellSandboxImageEnvVar, got)
	}

	t.Setenv(shellSandboxImageEnvVar, "")
	// The default must track the agent's version, not float on :latest: the two
	// images are built from one commit by one workflow.
	got := resolveShellSandboxImage(agent)
	if !strings.HasSuffix(got, ":"+DefaultPlatformAgentVersion) {
		t.Errorf("expected the default sandbox image to carry the build version %q, got %q", DefaultPlatformAgentVersion, got)
	}
	if !strings.Contains(got, "/agent-sandbox:") {
		t.Errorf("expected the agent-sandbox repository from images.json, got %q", got)
	}
}

// TestResolveShellSandboxImageFollowsTheOperatorsRegistry covers the install that
// mirrors images privately and configures only the operator's own reference,
// because that is the only image its Deployment names. Without this rung the
// sandbox is the one pod that reaches ghcr.io anyway, and fails at ImagePull on a
// cluster whose point may be that it cannot.
func TestResolveShellSandboxImageFollowsTheOperatorsRegistry(t *testing.T) {
	agent := shellSandboxTestAgent()
	t.Setenv(shellSandboxImageEnvVar, "")
	t.Setenv(operatorImageEnvVar, "mirror.corp.internal:5000/kube-agents/k8s-operator:0.2.0")

	if got, want := resolveShellSandboxImage(agent),
		"mirror.corp.internal:5000/kube-agents/agent-sandbox:0.2.0"; got != want {
		t.Errorf("resolveShellSandboxImage = %q, want %q", got, want)
	}

	// A digest cannot name a different repository's manifest, so the derivation
	// drops to the tag beside it, exactly as the agent image does.
	t.Setenv(operatorImageEnvVar, "mirror.corp.internal:5000/kube-agents/k8s-operator@sha256:"+strings.Repeat("1", 64))
	if got, want := resolveShellSandboxImage(agent),
		"mirror.corp.internal:5000/kube-agents/agent-sandbox:latest"; got != want {
		t.Errorf("resolveShellSandboxImage on a digest pin = %q, want %q", got, want)
	}

	// AGENT_SANDBOX_IMAGE is the explicit answer and outranks the inferred one.
	t.Setenv(shellSandboxImageEnvVar, "registry.example.com/mirror/agent-sandbox:v1.2.3")
	if got, want := resolveShellSandboxImage(agent),
		"registry.example.com/mirror/agent-sandbox:v1.2.3"; got != want {
		t.Errorf("expected %s to outrank %s, got %q", shellSandboxImageEnvVar, operatorImageEnvVar, got)
	}
}

// TestTheShellSandboxContainerIsHardenedWhereItCanBe pins the two settings that
// hold with sshd's privilege separation. The rest of the capability audit is
// deferred; these two are not part of that question and this is the pod where
// every model-authored command runs.
func TestTheShellSandboxContainerIsHardenedWhereItCanBe(t *testing.T) {
	container := buildShellSandboxContainer(shellSandboxTestAgent(), nil)

	security := container.SecurityContext
	if security == nil {
		t.Fatal("expected a container securityContext on the sandbox")
	}
	if security.SeccompProfile == nil ||
		security.SeccompProfile.Type != corev1.SeccompProfileTypeRuntimeDefault {
		t.Errorf("expected the RuntimeDefault seccomp profile, got %+v", security.SeccompProfile)
	}
	if security.Capabilities == nil {
		t.Fatal("expected NET_RAW to be dropped")
	}
	var dropped bool
	for _, capability := range security.Capabilities.Drop {
		if capability == "NET_RAW" {
			dropped = true
		}
	}
	if !dropped {
		t.Errorf("expected NET_RAW in the drop list, got %v", security.Capabilities.Drop)
	}
	// Not asserted as absent by accident: dropping every capability, or setting
	// runAsNonRoot, breaks sshd's privilege separation at login. See the pod-level
	// comment in buildShellSandboxStatefulSet.
	if len(security.Capabilities.Add) != 0 {
		t.Errorf("the sandbox adds no capability, got %v", security.Capabilities.Add)
	}
}

// The failure this guards against is silent: a Secret volume's files are
// root-owned, the agent pod runs as uid 10000, and `ssh -i` refuses any key with
// a group or other permission bit set. 0400 is unreadable and 0440 is refused, so
// the key has to be copied to a file the agent's own uid owns. If someone
// "simplifies" this to a single Secret mount it will fail at connection time with
// a permissions error that reads like a bad key.
func TestShellSandboxClientKeyIsStagedRatherThanMountedDirectly(t *testing.T) {
	volumes := buildShellSandboxClientKeyVolumes()
	if len(volumes) != 2 {
		t.Fatalf("expected a Secret volume and a writable staging volume, got %d", len(volumes))
	}

	var secretVol, stagingVol *corev1.Volume
	for i := range volumes {
		switch volumes[i].Name {
		case shellSandboxClientKeySecretVolume:
			secretVol = &volumes[i]
		case shellSandboxClientKeyVolume:
			stagingVol = &volumes[i]
		}
	}
	if secretVol == nil || stagingVol == nil {
		t.Fatalf("expected both %q and %q volumes, got %+v", shellSandboxClientKeySecretVolume, shellSandboxClientKeyVolume, volumes)
	}
	if stagingVol.EmptyDir == nil {
		t.Errorf("the staging volume must be writable, so the init container's copy is owned by the pod's uid")
	}
	if secretVol.Secret == nil {
		t.Fatalf("expected %q to be backed by a Secret", shellSandboxClientKeySecretVolume)
	}
	if secretVol.Secret.SecretName != defaultPlatformAgentSecrets {
		t.Errorf("expected the private key to come from %q, got %q", defaultPlatformAgentSecrets, secretVol.Secret.SecretName)
	}
	if secretVol.Secret.Optional == nil || !*secretVol.Secret.Optional {
		t.Errorf("the mount must be optional: an install predating the keypair has to keep starting")
	}
	if mode := secretVol.Secret.DefaultMode; mode == nil || *mode&0444 == 0 {
		t.Errorf("the Secret mount must be readable by the init container's non-root uid, got mode %v", mode)
	}

	// Only the private half. The public half sits in the same Secret and has no
	// business in the agent pod.
	if len(secretVol.Secret.Items) != 1 {
		t.Fatalf("expected exactly one projected item, got %+v", secretVol.Secret.Items)
	}
	if got := secretVol.Secret.Items[0].Key; got != shellSandboxPrivateKeySecretKey {
		t.Errorf("expected only %q to be projected, got %q", shellSandboxPrivateKeySecretKey, got)
	}
}

func TestShellSandboxClientKeyInitContainerStagesWithPrivateMode(t *testing.T) {
	init := buildShellSandboxClientKeyInitContainer("example.com/agent:v1")
	script := strings.Join(init.Args, "\n")

	// 0600 is the only mode ssh accepts; anything with a group bit is refused.
	if !strings.Contains(script, "install -m 0600") {
		t.Errorf("expected the key to be staged with mode 0600, got script:\n%s", script)
	}
	// A missing key must not crash-loop the agent pod over a feature that is off.
	if !strings.Contains(script, "if [ -r ") {
		t.Errorf("expected a missing key to be tolerated, got script:\n%s", script)
	}
	if !strings.Contains(script, shellSandboxClientKeyFilePath()) {
		t.Errorf("expected the staged path %q to match what the Hermes config will point at, got script:\n%s",
			shellSandboxClientKeyFilePath(), script)
	}

	var writable bool
	for _, m := range init.VolumeMounts {
		if m.Name == shellSandboxClientKeyVolume {
			writable = !m.ReadOnly
		}
		if m.Name == shellSandboxClientKeySecretVolume && !m.ReadOnly {
			t.Errorf("the Secret mount must be read-only in the init container")
		}
	}
	if !writable {
		t.Errorf("the init container needs to write to %q", shellSandboxClientKeyVolume)
	}
}

// The agent container sees the staged copy and not the Secret it came from.
func TestShellSandboxClientKeyMountHidesTheSecretFromTheAgent(t *testing.T) {
	mount := buildShellSandboxClientKeyMount()
	if mount.Name != shellSandboxClientKeyVolume {
		t.Errorf("expected the agent to mount the staged copy %q, got %q", shellSandboxClientKeyVolume, mount.Name)
	}
	if !mount.ReadOnly {
		t.Errorf("the agent only reads the key; the init container is what writes it")
	}
}

// The sandbox mounts a Secret that holds one public key and nothing else. Naming
// platform-agent-secrets here — even with an items selector — would put every
// model API key one edit away from the pod this design keeps credential-free.
func TestShellSandboxAuthorizedKeysSecretIsNotTheCredentialSecret(t *testing.T) {
	agent := shellSandboxTestAgent()
	name := shellSandboxAuthorizedKeysSecretName(agent)
	if name == defaultPlatformAgentSecrets {
		t.Fatalf("the sandbox must not mount the agent credential Secret")
	}
	if !strings.HasPrefix(name, shellSandboxName(agent)) {
		t.Errorf("expected the Secret to be named after the sandbox, got %q", name)
	}

	sts := buildShellSandboxStatefulSet(agent, name, "", "settings-hash")
	for _, v := range sts.Spec.Template.Spec.Volumes {
		if v.Secret != nil && v.Secret.SecretName == defaultPlatformAgentSecrets {
			t.Errorf("the sandbox pod must not reference %q, found volume %q", defaultPlatformAgentSecrets, v.Name)
		}
	}
}

// shellSandboxAgent returns a test agent with the sandbox toggle set.
func shellSandboxAgent(enabled bool) *agentv1alpha1.PlatformAgent {
	agent := shellSandboxTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		Experimental: &agentv1alpha1.ExperimentalSpec{
			ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: ptr.To(enabled)},
		},
	}
	return agent
}

// Saying nothing means the sandbox, and each of these shapes is an install that
// says nothing — a nil check missed anywhere in the four-level path is a panic in
// the reconcile loop, not a default. `enabled: true` is the same answer reached
// the long way; only an explicit false is refused.
func TestOnlyAnExplicitFalseIsRefused(t *testing.T) {
	accepted := map[string]*agentv1alpha1.PlatformAgent{
		"nil agent":           nil,
		"no harness":          shellSandboxTestAgent(),
		"no experimental":     {Spec: agentv1alpha1.PlatformAgentSpec{Harness: &agentv1alpha1.HarnessSpec{}}},
		"no sandbox block":    {Spec: agentv1alpha1.PlatformAgentSpec{Harness: &agentv1alpha1.HarnessSpec{Experimental: &agentv1alpha1.ExperimentalSpec{}}}},
		"sandbox without set": {Spec: agentv1alpha1.PlatformAgentSpec{Harness: &agentv1alpha1.HarnessSpec{Experimental: &agentv1alpha1.ExperimentalSpec{ShellSandbox: &agentv1alpha1.ShellSandboxSpec{}}}}},
		"explicitly true":     shellSandboxAgent(true),
	}
	for name, agent := range accepted {
		if reason, _ := validateShellSandbox(agent); reason != "" {
			t.Errorf("%s was refused with reason %q", name, reason)
		}
	}

	reason, msg := validateShellSandbox(shellSandboxAgent(false))
	if reason != reasonShellSandboxCannotBeDisabled {
		t.Errorf("reason = %q, want %q", reason, reasonShellSandboxCannotBeDisabled)
	}
	// The operator surfaces this on the CR's status, where it is the only
	// explanation the person who set the field will see.
	if msg == "" {
		t.Error("the refusal carries no message")
	}
}

// The managed scope is what makes the backend something the agent cannot write its
// way out of. An agent that saves `backend: local` into its own config.yaml has not
// changed a preference, it has left the sandbox — so these keys have to be in the
// rendering that Hermes treats as immutable, and absent from it entirely when the
// feature is off so that no existing install sees a new key.
func TestManagedConfigCarriesTheTerminalBackend(t *testing.T) {
	agent := shellSandboxAgent(true)
	got := renderConfigYAML(agent, nil)
	for _, want := range []string{
		"backend: ssh",
		"ssh_host: " + shellSandboxHost(agent),
		"ssh_user: " + shellSandboxUser,
		fmt.Sprintf("ssh_port: %d", shellSandboxPort),
		"ssh_key: " + shellSandboxClientKeyFilePath(),
	} {
		if !strings.Contains(got, want) {
			t.Errorf("expected the managed terminal block to carry %q:\n%s", want, got)
		}
	}
	// cwd is the profile-shaped part of the block, and a leaf here REPLACES each
	// profile's own value rather than merging with it.
	if strings.Contains(got, "cwd:") {
		t.Errorf("the managed scope must not pin terminal.cwd:\n%s", got)
	}
}

// The builders were tested in isolation long before anything called them. This is
// the join: the agent pod has to carry the init container, both volumes and the
// read-only mount, because without the key it cannot log into the sandbox and the
// sandbox is where every command it runs executes.
func TestAgentPodStagesTheClientKey(t *testing.T) {
	has := func(pod corev1.PodSpec) (init, volume, staged, mount bool) {
		for _, c := range pod.InitContainers {
			if c.Name == "sandbox-ssh-key" {
				init = true
			}
		}
		for _, v := range pod.Volumes {
			switch v.Name {
			case shellSandboxClientKeySecretVolume:
				volume = true
			case shellSandboxClientKeyVolume:
				staged = true
			}
		}
		for _, c := range pod.Containers {
			for _, m := range c.VolumeMounts {
				if m.Name == shellSandboxClientKeyVolume {
					mount = m.ReadOnly && m.MountPath == shellSandboxClientKeyPath
				}
				// The Secret mount is the init container's alone: it is the
				// world-readable copy, and the agent container reads the staged one.
				if m.Name == shellSandboxClientKeySecretVolume {
					t.Errorf("container %q must not see the raw Secret volume", c.Name)
				}
			}
		}
		return
	}

	on := buildPodTemplateSpec(shellSandboxAgent(true), "", "", "", "", nil, renderOptions{})
	init, volume, staged, mount := has(on.Spec)
	if !init {
		t.Error("expected the sandbox-ssh-key init container")
	}
	if !volume || !staged {
		t.Errorf("expected both key volumes, got secret=%v staged=%v", volume, staged)
	}
	if !mount {
		t.Errorf("expected the staged key mounted read-only at %s in the agent container", shellSandboxClientKeyPath)
	}
}

// The Hermes base image ships HERMES_WRITE_SAFE_ROOT=/opt/data. Left alone with the
// sandbox on, agent/file_safety.py refuses every sandbox path and permits only one
// that does not exist there, so write_file and patch fail for everything — observed
// on a live install before this was added.
func TestSandboxRepointsTheWriteSafeRoot(t *testing.T) {
	safeRoot := func(pod corev1.PodSpec) (string, bool) {
		for _, c := range pod.Containers {
			if c.Name != "platform-agent" {
				continue
			}
			for _, e := range c.Env {
				if e.Name == "HERMES_WRITE_SAFE_ROOT" {
					return e.Value, true
				}
			}
		}
		return "", false
	}

	on := buildPodTemplateSpec(shellSandboxAgent(true), "", "", "", "", nil, renderOptions{})
	got, found := safeRoot(on.Spec)
	if !found {
		t.Fatal("expected HERMES_WRITE_SAFE_ROOT on the sandboxed agent container")
	}
	want := shellSandboxDataPath + ":" + shellSandboxHomePath
	if got != want {
		t.Errorf("write safe root = %q, want %q", got, want)
	}
	// The sandbox's data volume carries the agent pod's /opt/data path on purpose,
	// so the old check — that the safe root no longer names /opt/data — no longer
	// distinguishes anything. What still has to hold is that every entry resolves
	// inside the sandbox: file_safety.py compares the prefix in the agent process,
	// and a path that exists only in the agent pod would let write_file accept a
	// write the ssh backend then makes on the far side, or refuse one it should
	// allow.
	for _, p := range strings.Split(got, ":") {
		if p != shellSandboxDataPath && p != shellSandboxHomePath {
			t.Errorf("write safe root entry %q is not a sandbox path", p)
		}
	}
}

// TERMINAL_CWD is the difference between the model's work surviving a pod recycle
// and not. Hermes' ssh backend defaults cwd to `~` (tools/terminal_tool.py), which
// is the sandbox's ephemeral home, so without this every relative path the model
// wrote was on the container overlay while the volume beside it stayed empty —
// observed on a live install, 44K on a five-day-old PVC.
func TestSandboxPointsTheTerminalAtTheDataVolume(t *testing.T) {
	cwd := func(pod corev1.PodSpec) (string, bool) {
		for _, c := range pod.Containers {
			if c.Name != "platform-agent" {
				continue
			}
			for _, e := range c.Env {
				if e.Name == "TERMINAL_CWD" {
					return e.Value, true
				}
			}
		}
		return "", false
	}

	on := buildPodTemplateSpec(shellSandboxAgent(true), "", "", "", "", nil, renderOptions{})
	got, found := cwd(on.Spec)
	if !found {
		t.Fatal("expected TERMINAL_CWD on the sandboxed agent container")
	}
	if got != shellSandboxDataPath {
		t.Errorf("TERMINAL_CWD = %q, want the sandbox data volume %q", got, shellSandboxDataPath)
	}
	// The home is the failure this exists to prevent, and it is a silent one: the
	// shell works, the files are written, and they are gone on the next restart.
	if got == shellSandboxHomePath {
		t.Error("TERMINAL_CWD points at the ephemeral home; model writes will not survive a restart")
	}
}

// The credential proxy is never a container of this pod. These are the
// properties that keep it out, and each one is a single field away from being
// silently untrue.

// federatedTestAgent is the sandbox with workload identity federation configured
// on the broker. Federation is optional hardening rather than a placement
// switch — see credentialProxyFederation — so this agent differs from
// shellSandboxTestAgent in what the broker's pod mints with, and in nothing else.
func federatedTestAgent() *agentv1alpha1.PlatformAgent {
	agent := shellSandboxTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		Experimental: &agentv1alpha1.ExperimentalSpec{
			ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: ptr.To(true)},
		},
	}
	agent.Spec.Security = &agentv1alpha1.SecuritySpec{
		WorkloadIdentityFederation: &agentv1alpha1.WorkloadIdentityFederationSpec{
			Audience: "//iam.googleapis.com/projects/123456789012/locations/global/" +
				"workloadIdentityPools/kubeagents/providers/test-cluster",
			ServiceAccountEmail: "kubeagents-platform-gsa@example.iam.gserviceaccount.com",
		},
	}
	return agent
}

func TestSandboxPodHoldsNothingOfTheBrokers(t *testing.T) {
	// The property the separate pod exists for. A broker container here would
	// share the shell's network namespace, so the model would reach it on
	// loopback and reach everything it can reach; a broker volume here would put
	// its kubeconfig, gcloud configuration or federated token a mount away.
	// Nothing in the reconcile fails if one creeps back — the pod just quietly
	// becomes the layout this design replaced.
	for name, agent := range map[string]*agentv1alpha1.PlatformAgent{
		"federation off": shellSandboxTestAgent(),
		"federation on":  federatedTestAgent(),
	} {
		sts := buildShellSandboxStatefulSet(agent, "sandbox-ssh", credentialProxySandboxURL(agent), "settings-hash")
		pod := sts.Spec.Template.Spec

		if len(pod.Containers) != 1 || pod.Containers[0].Name != "shell" {
			t.Fatalf("%s: expected the shell alone, got %#v", name, pod.Containers)
		}
		brokerVolumes := map[string]bool{credentialProxyWIFTokenVolume: true}
		for vol := range credentialProxyRuntimeVolumeNames {
			brokerVolumes[vol] = true
		}
		for _, vol := range pod.Volumes {
			if brokerVolumes[vol.Name] {
				t.Errorf("%s: the sandbox pod declares the broker's volume %q", name, vol.Name)
			}
		}
		for _, m := range pod.Containers[0].VolumeMounts {
			if m.MountPath == credentialProxyWIFTokenPath {
				t.Errorf("%s: the shell mounts the federated token at %q", name, m.MountPath)
			}
		}
	}
}

func TestBrokerTakesTheFederatedIdentityWhenConfigured(t *testing.T) {
	// Federation narrows what the broker's pod can mint. It is not what moves the
	// broker — that is unconditional — so the env, the projection and the mount
	// all key on the field alone, and all three have to agree: a mount naming a
	// volume the pod does not declare is a pod that never starts.
	proxy := buildCredentialProxyContainer(federatedTestAgent())

	federation := map[string]string{}
	for _, e := range proxy.Env {
		switch e.Name {
		case "CREDENTIAL_PROXY_WIF_AUDIENCE", "CREDENTIAL_PROXY_WIF_SERVICE_ACCOUNT",
			"CREDENTIAL_PROXY_WIF_TOKEN_FILE", "GOOGLE_APPLICATION_CREDENTIALS",
			"CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE":
			federation[e.Name] = e.Value
		case "CREDENTIAL_PROXY_WORKSPACE_ROOT":
			t.Errorf("the broker must not be pointed at a shared tree: %q is in another pod", e.Value)
		}
	}
	if len(federation) != 5 {
		t.Errorf("expected the full federation environment, got %#v", federation)
	}
	// gcloud keeps its own credential store and ignores ADC, so this one is what
	// stops `gcloud container clusters get-credentials` falling through to the
	// metadata server.
	if federation["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] != credentialProxyWIFCredentialFile {
		t.Errorf("gcloud must be pinned to the federated credential file, got %q",
			federation["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"])
	}

	var mounted bool
	for _, m := range proxy.VolumeMounts {
		if m.Name == credentialProxyWIFTokenVolume {
			mounted = true
		}
	}
	if !mounted {
		t.Error("the broker declares the federation environment but mounts no token to satisfy it")
	}

	// Without the field, none of the three appears — the broker falls back to the
	// metadata server, which is already out of the shell's reach.
	plain := buildCredentialProxyContainer(shellSandboxTestAgent())
	for _, m := range plain.VolumeMounts {
		if m.Name == credentialProxyWIFTokenVolume {
			t.Error("no federation configured, but the broker mounts a token volume nothing projects")
		}
	}
	for _, e := range plain.Env {
		if e.Name == "GOOGLE_APPLICATION_CREDENTIALS" {
			t.Error("no federation configured, but ADC is pointed at a file that is not written")
		}
	}
}

func TestFederatedTokenProjectionIsAudienceScoped(t *testing.T) {
	agent := federatedTestAgent()
	volumes := buildCredentialProxyRuntimeVolumes(agent)

	var projected *corev1.Volume
	for i := range volumes {
		if volumes[i].Name == credentialProxyWIFTokenVolume {
			projected = &volumes[i]
		}
	}
	if projected == nil {
		t.Fatalf("expected the federated token volume, got %#v", volumes)
	}
	sources := projected.VolumeSource.Projected.Sources
	if len(sources) != 1 || sources[0].ServiceAccountToken == nil {
		t.Fatalf("expected exactly one ServiceAccountToken projection, got %#v", sources)
	}
	// Scoped to the federation provider, and not to github-token-minter's
	// audience: one token cannot satisfy both verifiers, and widening either
	// audience lets a token minted for one be replayed at the other.
	if got := sources[0].ServiceAccountToken.Audience; got != agent.Spec.Security.WorkloadIdentityFederation.Audience {
		t.Errorf("token audience = %q, want the federation provider", got)
	}

	// A half-filled block is absent, not a partial opt-in: a pool with nothing to
	// impersonate cannot produce a token, and projecting one would leave the
	// broker configured for an exchange that fails on every credentialed command.
	half := federatedTestAgent()
	half.Spec.Security.WorkloadIdentityFederation.ServiceAccountEmail = ""
	for _, v := range buildCredentialProxyRuntimeVolumes(half) {
		if v.Name == credentialProxyWIFTokenVolume {
			t.Error("a federation block with no impersonation target must read as absent")
		}
	}
}

func TestSandboxWrappersPostToTheBrokerService(t *testing.T) {
	// Never loopback. A loopback endpoint is a broker in the shell's own pod, and
	// it is also what credential_proxy_client used to read as "we share a
	// filesystem" — so the address is both the placement and the contract that
	// paths do not cross it.
	for name, agent := range map[string]*agentv1alpha1.PlatformAgent{
		"federation off": shellSandboxTestAgent(),
		"federation on":  federatedTestAgent(),
	} {
		if got := credentialProxySandboxURL(agent); got != credentialProxyURL(agent) {
			t.Errorf("%s: sandbox URL = %q, want the broker Service", name, got)
		}
	}
	// The Service selects the broker's own pod, which is what the gateway's relay
	// clients and the sandbox's wrapped CLIs both dial.
	if got := buildCredentialProxyService(federatedTestAgent()).Spec.Selector["app"]; got != "test-agent-credential-proxy" {
		t.Errorf("the proxy Service must select the broker pod, got %q", got)
	}
}

func TestCredentialProxyNetworkPolicyAdmitsOnlyTheSandboxAndTheGateway(t *testing.T) {
	// The standalone pod holds every credential the install has, and its endpoint
	// authenticates no caller — so this policy is the whole boundary in front of
	// it. Untested, a refactor can widen it back to the namespace and nothing
	// fails.
	agent := shellSandboxTestAgent()
	np := buildCredentialProxyNetworkPolicy(agent)

	if len(np.Spec.PolicyTypes) != 1 || np.Spec.PolicyTypes[0] != networkingv1.PolicyTypeIngress {
		t.Fatalf("expected ingress-only, got %v — egress is #720's, but naming it here without rules would cut the proxy off from GKE and the token broker", np.Spec.PolicyTypes)
	}
	if len(np.Spec.PodSelector.MatchLabels) == 0 {
		t.Fatal("an empty podSelector applies the policy to every pod in the namespace")
	}

	if len(np.Spec.Ingress) != 1 {
		t.Fatalf("expected exactly one ingress rule, got %d", len(np.Spec.Ingress))
	}
	in := np.Spec.Ingress[0]
	if len(in.From) != 2 {
		t.Fatalf("expected exactly two peers (sandbox, gateway), got %#v", in.From)
	}
	// A peer with a nil PodSelector matches every pod, and a NamespaceSelector
	// widens it past this namespace. Either reads as a peer list in `kubectl get`.
	for i, peer := range in.From {
		if peer.PodSelector == nil || len(peer.PodSelector.MatchLabels) == 0 {
			t.Fatalf("peer %d has no pod selector, which admits every pod", i)
		}
		if peer.NamespaceSelector != nil || peer.IPBlock != nil {
			t.Errorf("peer %d reaches outside the namespace: %#v", i, peer)
		}
	}
	if got := in.From[0].PodSelector.MatchLabels; got["app"] != shellSandboxSelector(agent)["app"] {
		t.Errorf("first peer = %v, want the sandbox pod", got)
	}
	if got := in.From[1].PodSelector.MatchLabels["app"]; got != "test-agent-gateway" {
		t.Errorf("second peer app = %q, want the gateway pod", got)
	}

	if len(in.Ports) != 1 || in.Ports[0].Port.IntValue() != credentialProxyPort {
		t.Errorf("expected ingress only on %d, got %#v", credentialProxyPort, in.Ports)
	}
}

func TestCredentialProxyNetworkPolicySelectsItsOwnPod(t *testing.T) {
	// The policy, the Service, and the Deployment agree on one label set, or the
	// policy constrains a pod that does not exist while the real one is open.
	agent := shellSandboxTestAgent()
	deploy := buildCredentialProxyDeployment(agent, "policy-hash")
	podLabels := deploy.Spec.Template.ObjectMeta.Labels

	for name, selector := range map[string]map[string]string{
		"NetworkPolicy.podSelector": buildCredentialProxyNetworkPolicy(agent).Spec.PodSelector.MatchLabels,
		"Service.spec.selector":     buildCredentialProxyService(agent).Spec.Selector,
		"Deployment.spec.selector":  deploy.Spec.Selector.MatchLabels,
	} {
		if len(selector) == 0 {
			t.Errorf("%s is empty, which selects every pod in the namespace", name)
		}
		for k, v := range selector {
			if podLabels[k] != v {
				t.Errorf("%s wants %s=%s, which the pod template does not carry (%v)", name, k, v, podLabels)
			}
		}
	}
}

func TestSandboxEgressReachesNothingButDNSAndTheBroker(t *testing.T) {
	// The narrow egress is what the broker's separate pod buys, and it is the
	// half of the policy that is easy to widen by accident: a rule added for one
	// skill's API is a rule the model's own code can use.
	np := buildShellSandboxNetworkPolicy(federatedTestAgent(), nil)
	for _, rule := range np.Spec.Egress {
		// A CIDR peer is allowed on the DNS rule alone, where the two of them are
		// the NodeLocal DNSCache listener and the kube-dns VIP — addresses a
		// resolver reaches, on port 53, not addresses the model's code can use.
		// Anywhere else a CIDR is a hole straight past the broker.
		dnsOnly := len(rule.Ports) > 0
		for _, p := range rule.Ports {
			if p.Port.IntValue() != 53 {
				dnsOnly = false
			}
		}
		for _, peer := range rule.To {
			if peer.IPBlock != nil && !dnsOnly {
				t.Errorf("the sandbox may reach %s directly; every remote address belongs to the broker", peer.IPBlock.CIDR)
			}
		}
		for _, p := range rule.Ports {
			switch p.Port.IntValue() {
			case 53, credentialProxyPort:
			default:
				t.Errorf("unexpected egress port %v — the sandbox needs cluster DNS and the broker, nothing else", p.Port)
			}
		}
	}
	// Ingress is sshd from the gateway and nothing else. The broker port is not
	// admitted here: no process in this pod listens on it.
	for _, rule := range np.Spec.Ingress {
		for _, p := range rule.Ports {
			if p.Port.IntValue() != shellSandboxPort {
				t.Errorf("the sandbox admits traffic on %v; only sshd listens in this pod", p.Port)
			}
		}
	}
}

func TestSandboxPodTemplateCarriesTheSelectorLabels(t *testing.T) {
	// ObjectMeta.Labels and Selector.MatchLabels are one map, and withCommonLabels
	// merges into it in place — so the selector the API server stores carries the
	// recommended labels whether or not the template does. A template narrower
	// than that selector is rejected outright: `selector` does not match template
	// `labels`, on every reconcile, with the StatefulSet left as it was.
	agent := federatedTestAgent()
	sts := buildShellSandboxStatefulSet(agent, "sandbox-ssh", credentialProxySandboxURL(agent), "settings-hash")
	withCommonLabels(sts, agent)
	for k, v := range sts.Spec.Selector.MatchLabels {
		if sts.Spec.Template.Labels[k] != v {
			t.Errorf("pod template is missing selector label %s=%s (%#v); the API server rejects the StatefulSet", k, v, sts.Spec.Template.Labels)
		}
	}
}

// The sandbox's runtime is its own field, and the default install must render as
// though the field did not exist. Both halves matter: an install that never asks
// for gVisor and starts emitting `runtimeClassName` gets an object diff on every
// reconcile, and an install that does ask for it and does not get the field runs
// the model's code on the host kernel while the CR says otherwise.
func TestShellSandboxRuntimeClassIsOptOnly(t *testing.T) {
	runtimeOf := func(agent *agentv1alpha1.PlatformAgent) *string {
		return buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "settings-hash").
			Spec.Template.Spec.RuntimeClassName
	}

	if got := runtimeOf(shellSandboxAgent(true)); got != nil {
		t.Errorf("a sandbox that names no RuntimeClass must leave the field out, got %q", *got)
	}

	// An empty string is the value Helm sends for an unset chart key, and
	// Kubernetes reads it as the default runtime — the same thing nil means. Only
	// nil keeps it out of the rendered object, so it is what an empty string has
	// to become.
	blank := shellSandboxAgent(true)
	blank.Spec.Harness.Experimental.ShellSandbox.RuntimeClassName = ptr.To("")
	if got := runtimeOf(blank); got != nil {
		t.Errorf("an empty runtimeClassName must render as absent, got %q", *got)
	}

	named := shellSandboxAgent(true)
	named.Spec.Harness.Experimental.ShellSandbox.RuntimeClassName = ptr.To("gvisor")
	if got := runtimeOf(named); got == nil || *got != "gvisor" {
		t.Errorf("expected the sandbox pod to run under gvisor, got %v", got)
	}
}

// The pre-flight check is what turns a missing RuntimeClass into a Degraded CR
// instead of a pod that sits Pending with nothing to read. It has to see both
// pods' fields, and it has to name each class once: the message it feeds joins
// the list, and `gvisor, gvisor` reads like two different problems.
func TestRequestedRuntimeClassesCoversBothPodsAndDeduplicates(t *testing.T) {
	agentPodOnly := shellSandboxAgent(true)
	agentPodOnly.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Availability: &agentv1alpha1.AvailabilitySpec{RuntimeClassName: ptr.To("gvisor")},
	}
	if got := requestedRuntimeClasses(agentPodOnly); len(got) != 1 || got[0] != "gvisor" {
		t.Errorf("the agent pod's runtime must be checked on its own, got %v", got)
	}

	sandboxOnly := shellSandboxAgent(true)
	sandboxOnly.Spec.Harness.Experimental.ShellSandbox.RuntimeClassName = ptr.To("gvisor")
	if got := requestedRuntimeClasses(sandboxOnly); len(got) != 1 || got[0] != "gvisor" {
		t.Errorf("the sandbox's runtime must be checked on its own, got %v", got)
	}

	both := shellSandboxAgent(true)
	both.Spec.Harness.Experimental.ShellSandbox.RuntimeClassName = ptr.To("gvisor")
	both.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Availability: &agentv1alpha1.AvailabilitySpec{RuntimeClassName: ptr.To("gvisor")},
	}
	if got := requestedRuntimeClasses(both); len(got) != 1 {
		t.Errorf("one name asked for by both pods is one name to check, got %v", got)
	}

	differing := shellSandboxAgent(true)
	differing.Spec.Harness.Experimental.ShellSandbox.RuntimeClassName = ptr.To("gvisor")
	differing.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Availability: &agentv1alpha1.AvailabilitySpec{RuntimeClassName: ptr.To("kata")},
	}
	if got := requestedRuntimeClasses(differing); len(got) != 2 {
		t.Errorf("two different runtimes are two checks, got %v", got)
	}

	if got := requestedRuntimeClasses(shellSandboxAgent(true)); len(got) != 0 {
		t.Errorf("an install that asks for no runtime must skip the check entirely, got %v", got)
	}
}

func TestExtraVolumeMountsCannotNameTheBrokersVolumes(t *testing.T) {
	// The CR-driven analogue of TestSandboxPodHoldsNothingOfTheBrokers above.
	// That one asserts the sandbox pod carries none of the broker's volumes;
	// nothing asserted the same for a CR that asks for one by name, and
	// spec.deployment.extraVolumeMounts is appended to the agent container with
	// no validation at all.
	for _, name := range []string{
		"credential-proxy-state",
		"credential-proxy-policy",
		"credential-proxy-runtime",
		"credential-proxy-ksa-token",
	} {
		t.Run(name, func(t *testing.T) {
			agent := federatedTestAgent()
			agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
				ExtraVolumeMounts: []corev1.VolumeMount{
					{Name: name, MountPath: "/opt/data/state"},
				},
			}
			msg := validateExtraVolumeMounts(agent)
			if msg == "" {
				t.Fatalf("mounting %q into the agent container was accepted", name)
			}
			if !strings.Contains(msg, name) {
				t.Errorf("the degraded message must name the offending volume; got %q", msg)
			}
		})
	}
}

func TestExtraVolumeMountsAllowsAnOrdinaryVolume(t *testing.T) {
	// The field is a documented extension point and stays one. A guard that
	// refuses more than the broker's own volumes is a regression dressed as a
	// control.
	agent := federatedTestAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		ExtraVolumeMounts: []corev1.VolumeMount{
			{Name: "customer-ca-bundle", MountPath: "/etc/ssl/extra"},
			{Name: "credential-proxy-state-of-my-own", MountPath: "/opt/data/mine"},
		},
	}
	if msg := validateExtraVolumeMounts(agent); msg != "" {
		t.Fatalf("an ordinary extra mount was refused: %s", msg)
	}
}

func TestExtraVolumeMountsGuardToleratesAnEmptyDeployment(t *testing.T) {
	agent := federatedTestAgent()
	agent.Spec.Deployment = nil
	if msg := validateExtraVolumeMounts(agent); msg != "" {
		t.Fatalf("a CR with no deployment block was refused: %s", msg)
	}
}

// The event watcher stays in this Pod under the sandbox layout — it posts to the
// Session KV server on loopback and has nowhere else to deliver — so the switch
// that turns it off has to reach the container that now hosts it.
func TestShellSandboxCarriesTheEventWatcherSwitch(t *testing.T) {
	for _, tc := range []struct {
		name    string
		enabled *bool
		want    string
	}{
		{"unset", nil, "true"},
		{"emergency stop", ptr.To(false), "false"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := shellSandboxAgent(true)
			agent.Spec.Harness.EventWatcher = &agentv1alpha1.EventWatcherSpec{Enabled: tc.enabled}

			var found []string
			for _, e := range buildAgentAPIAuthSidecar(agent, "/opt/data").Env {
				if e.Name == "EVENT_WATCHER_ENABLED" {
					found = append(found, e.Value)
				}
			}
			if len(found) != 1 || found[0] != tc.want {
				t.Errorf("EVENT_WATCHER_ENABLED = %v, want exactly one %q", found, tc.want)
			}
		})
	}
}
