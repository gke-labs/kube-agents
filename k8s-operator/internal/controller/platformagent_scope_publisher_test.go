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
	"strings"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// scopePublisherAgent is a next-mode agent whose scope has a folder, so the
// publisher has something to resolve.
func scopePublisherAgent() *agentv1alpha1.PlatformAgent {
	agent := mcpBrokerAgent()
	agent.Spec.Scope = &agentv1alpha1.ScopeSpec{Folders: []string{"111111111111"}}
	return agent
}

const scopePublisherTestCaller = "system:serviceaccount:test-ns:test-agent-scope-publisher"

// The vector scope_publisher.py's MemberHashTest pins: the same declaration
// has to give the same bytes and the same hash on both sides. The input has
// a missing list, no maxProjects, duplicates, an unsorted order and
// exclude.clusters, which is not hashed.
func TestScopeMemberHashMatchesThePublisherVector(t *testing.T) {
	agent := a2aTestAgent()
	agent.Spec.Scope = &agentv1alpha1.ScopeSpec{
		Projects:       []string{"team-b", "team-a", "team-a"},
		Folders:        []string{"222222222222", "111111111111"},
		Organizations:  []string{},
		SharedVpcHosts: []string{"host-project-1"},
		Exclude: &agentv1alpha1.ScopeExcludeSpec{
			Projects: []string{"legacy-*", "123456789012"},
			Clusters: []agentv1alpha1.ScopeClusterRef{{ProjectID: "team-a", Location: "us-central1", ClusterName: "c1"}},
		},
	}
	wantCanonical := `{"exclude":{"projects":["123456789012","legacy-*"]},"folders":["111111111111","222222222222"],"maxProjects":100,"metricsScopes":[],"organizations":[],"projects":["team-a","team-b"],"sharedVpcHosts":["host-project-1"]}`
	if got := string(scopeMemberCanonical(agent)); got != wantCanonical {
		t.Errorf("canonical bytes:\n got %s\nwant %s", got, wantCanonical)
	}
	const wantHash = "sha256:078c09284502aeccfafc4e819358684d7b3938b6f0e6b8204967114a3eaf8118"
	if got := scopeMemberHash(agent); got != wantHash {
		t.Errorf("memberHash = %s, want %s", got, wantHash)
	}

	// exclude.clusters is applied from scope.json directly, so editing it
	// must not invalidate the document.
	edited := agent.DeepCopy()
	edited.Spec.Scope.Exclude.Clusters = nil
	if scopeMemberHash(edited) != wantHash {
		t.Error("editing exclude.clusters changed memberHash")
	}
	// A membership field does.
	edited.Spec.Scope.MetricsScopes = []string{"metrics-host"}
	if scopeMemberHash(edited) == wantHash {
		t.Error("adding a Metrics Scope left memberHash unchanged")
	}
	// No scope block hashes as an empty declaration and does not panic.
	if got := scopeMemberHash(a2aTestAgent()); !strings.HasPrefix(got, scopeMemberHashPrefix) {
		t.Errorf("memberHash with no scope = %q", got)
	}
}

func TestTheScopePublisherStaticObjects(t *testing.T) {
	agent := scopePublisherAgent()

	cm := buildResolvedScopeConfigMap(agent)
	if cm.Name != "test-agent-resolved-scope" || cm.Data != nil || cm.BinaryData != nil {
		t.Errorf("ConfigMap %s carries data %v: server-side apply would reset the publisher's key", cm.Name, cm.Data)
	}

	sa := buildScopePublisherServiceAccount(agent)
	if sa.Name != "test-agent-scope-publisher" || sa.AutomountServiceAccountToken == nil || *sa.AutomountServiceAccountToken {
		t.Errorf("ServiceAccount %s automount = %v, want false", sa.Name, sa.AutomountServiceAccountToken)
	}

	role := buildScopePublisherRole(agent)
	want := rbacv1.PolicyRule{APIGroups: []string{""}, Resources: []string{"configmaps"},
		ResourceNames: []string{"test-agent-resolved-scope"}, Verbs: []string{"get", "patch"}}
	if len(role.Rules) != 1 || !policyRulesEqual(role.Rules[0], want) {
		t.Errorf("Role rules = %+v, want only %+v", role.Rules, want)
	}

	binding := buildScopePublisherRoleBinding(agent)
	if binding.RoleRef.Name != role.Name || len(binding.Subjects) != 1 ||
		binding.Subjects[0].Name != sa.Name || binding.Subjects[0].Namespace != agent.Namespace {
		t.Errorf("RoleBinding %+v does not bind the publisher's account to its Role", binding)
	}

	np := buildScopePublisherNetworkPolicy(agent, []string{"10.0.0.2/32"}, []string{"10.0.0.10"})
	if len(np.Spec.Ingress) != 0 || len(np.Spec.PolicyTypes) != 2 {
		t.Errorf("policy types %v, ingress %+v: want both types and no ingress", np.Spec.PolicyTypes, np.Spec.Ingress)
	}
	if got := np.Spec.PodSelector.MatchLabels[a2aComponentLabel]; got != scopePublisherComponent {
		t.Errorf("policy selects component %q", got)
	}
	if len(np.Spec.Egress) != 3 {
		t.Fatalf("egress rules = %d, want DNS, the broker and the API server", len(np.Spec.Egress))
	}
	broker := np.Spec.Egress[1]
	if broker.Ports[0].Port.IntVal != credentialProxyPort || broker.To[0].PodSelector == nil {
		t.Errorf("the broker rule %+v does not name the broker's pods on its port", broker)
	}
	api := np.Spec.Egress[2]
	if len(api.To) != 1 || api.To[0].IPBlock == nil || api.To[0].IPBlock.CIDR != "10.0.0.2/32" {
		t.Errorf("the API server rule %+v does not name the resolved address", api)
	}
	// Nothing resolved: the in-cluster VIP, not an open rule.
	fallback := buildScopePublisherNetworkPolicy(agent, nil, nil)
	if to := fallback.Spec.Egress[2].To; len(to) != 1 || to[0].IPBlock == nil || !strings.HasPrefix(to[0].IPBlock.CIDR, defaultAPIServerClusterIP) {
		t.Errorf("API server rule with nothing resolved = %+v", to)
	}
}

func policyRulesEqual(a, b rbacv1.PolicyRule) bool {
	return strings.Join(a.APIGroups, ",") == strings.Join(b.APIGroups, ",") &&
		strings.Join(a.Resources, ",") == strings.Join(b.Resources, ",") &&
		strings.Join(a.ResourceNames, ",") == strings.Join(b.ResourceNames, ",") &&
		strings.Join(a.Verbs, ",") == strings.Join(b.Verbs, ",")
}

func TestTheScopePublisherJob(t *testing.T) {
	agent := scopePublisherAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{ImagePullSecrets: []corev1.LocalObjectReference{{Name: "registry-creds"}}}
	start := time.Date(2026, 10, 29, 12, 0, 0, 0, time.UTC)
	hash := scopeMemberHash(agent)
	job := buildScopePublisherJob(agent, start, hash)

	if !strings.HasPrefix(job.Name, "test-agent-scope-publisher-") || len(job.Name) != len("test-agent-scope-publisher-")+scopeJobSuffixLength {
		t.Errorf("Job name %q", job.Name)
	}
	// A long agent name keeps the Job name within a label value. Two names
	// that differ only past the shortened prefix (52 characters) still get
	// different Job names, from the agent name in the hash.
	long := agent.DeepCopy()
	long.Name = strings.Repeat("a", 60) + "x"
	longName := buildScopePublisherJob(long, start, hash).Name
	if len(longName) != 63 {
		t.Errorf("Job name for a 61-character agent = %q (%d characters), want 63", longName, len(longName))
	}
	other := long.DeepCopy()
	other.Name = strings.Repeat("a", 60) + "y"
	if buildScopePublisherJob(other, start, hash).Name == longName {
		t.Error("two agent names that differ only past the shortened prefix share a Job name")
	}
	// At 51 characters the cut falls on the dash after the agent name,
	// which is trimmed rather than doubled.
	cut := agent.DeepCopy()
	cut.Name = strings.Repeat("a", 51)
	if name := buildScopePublisherJob(cut, start, hash).Name; strings.Contains(name, "--") || len(name) > 63 {
		t.Errorf("Job name for a 51-character agent = %q", name)
	}
	if other := buildScopePublisherJob(agent, start.Add(time.Second), hash); other.Name == job.Name {
		t.Error("two runs at different times share a name")
	}
	if job.Annotations[scopeMemberHashAnnotation] != hash {
		t.Errorf("Job annotation %q, want the memberHash it ran for", job.Annotations[scopeMemberHashAnnotation])
	}
	if *job.Spec.BackoffLimit != 0 || *job.Spec.TTLSecondsAfterFinished != 3600 || *job.Spec.ActiveDeadlineSeconds != 420 {
		t.Errorf("backoff %d, TTL %d, deadline %d; want 0, 3600, 420", *job.Spec.BackoffLimit,
			*job.Spec.TTLSecondsAfterFinished, *job.Spec.ActiveDeadlineSeconds)
	}
	// The deadline grows by the listing budget per 100 projects: 250 is three blocks.
	big := agent.DeepCopy()
	big.Spec.Scope.MaxProjects = ptr.To(int32(250))
	if got := *buildScopePublisherJob(big, start, hash).Spec.ActiveDeadlineSeconds; got != 3*300+120 {
		t.Errorf("deadline at maxProjects 250 = %d, want 1020", got)
	}

	pod := job.Spec.Template.Spec
	if pod.SecurityContext.RunAsUser == nil || *pod.SecurityContext.RunAsUser != sandboxUID ||
		pod.SecurityContext.FSGroup == nil || *pod.SecurityContext.FSGroup != agentFSGroup {
		t.Errorf("pod security context %+v, want the broker's UID and group", pod.SecurityContext)
	}
	if len(pod.ImagePullSecrets) != 1 || pod.ImagePullSecrets[0].Name != "registry-creds" {
		t.Errorf("pull secrets %v, want the agent image's registry-creds", pod.ImagePullSecrets)
	}
	if pod.ServiceAccountName != "test-agent-scope-publisher" || pod.RestartPolicy != corev1.RestartPolicyNever ||
		pod.AutomountServiceAccountToken == nil || *pod.AutomountServiceAccountToken {
		t.Errorf("pod SA %q, restart %q, automount %v", pod.ServiceAccountName, pod.RestartPolicy, pod.AutomountServiceAccountToken)
	}
	if job.Spec.Template.Labels[a2aComponentLabel] != scopePublisherComponent {
		t.Errorf("pod labels %v", job.Spec.Template.Labels)
	}
	var brokerToken *corev1.ServiceAccountTokenProjection
	for _, v := range pod.Volumes {
		if v.Name == scopePublisherBrokerTokenVolume {
			brokerToken = v.Projected.Sources[0].ServiceAccountToken
		}
	}
	if brokerToken == nil || brokerToken.Audience != "kubeagents-credential-proxy-scope" || *brokerToken.ExpirationSeconds != 3600 {
		t.Errorf("broker token projection %+v", brokerToken)
	}

	c := pod.Containers[0]
	if strings.Join(c.Command, " ") != scopePublisherPython+" "+scopePublisherScript {
		t.Errorf("command %v", c.Command)
	}
	wantEnv := map[string]string{
		"KUBEAGENTS_SCOPE_FILE":               "/etc/kube-agents/scope.json",
		"KUBEAGENTS_RESOLVED_SCOPE_CONFIGMAP": "test-agent-resolved-scope",
		"CREDENTIAL_PROXY_URL":                credentialProxyBaseURL(agent),
		"CREDENTIAL_PROXY_TOKEN_FILE":         "/var/run/secrets/kubeagents/credential-proxy/token",
		"KUBEAGENTS_MANAGEMENT_PROJECT":       "proj",
	}
	for name, want := range wantEnv {
		if got, found := brokerEnvValue(c.Env, name); !found || got != want {
			t.Errorf("%s = %q (found %v), want %q", name, got, found, want)
		}
	}
	mounts := map[string]string{}
	for _, m := range c.VolumeMounts {
		mounts[m.Name] = m.MountPath
	}
	if mounts[scopeVolumeName] != "/etc/kube-agents" || mounts[scopePublisherBrokerTokenVolume] != scopePublisherBrokerTokenMountDir ||
		mounts[buildEventWatcherTokenVolume().Name] != kubeAPIAccessMountPath {
		t.Errorf("mounts %v", mounts)
	}
	if c.SecurityContext == nil || c.SecurityContext.AllowPrivilegeEscalation == nil || *c.SecurityContext.AllowPrivilegeEscalation {
		t.Errorf("container security context %+v", c.SecurityContext)
	}
	if c.Resources.Requests.Memory().String() != "192Mi" || c.Resources.Limits.Memory().String() != "512Mi" ||
		c.Resources.Requests.Cpu().String() != "50m" || c.Resources.Limits.Cpu().String() != "500m" {
		t.Errorf("resources %+v", c.Resources)
	}

	// No harness block: the env is rendered empty, as the gateway's is.
	bare := agent.DeepCopy()
	bare.Spec.Harness = nil
	env := buildScopePublisherJob(bare, start, hash).Spec.Template.Spec.Containers[0].Env
	if got, found := brokerEnvValue(env, "KUBEAGENTS_MANAGEMENT_PROJECT"); !found || got != "" {
		t.Errorf("KUBEAGENTS_MANAGEMENT_PROJECT with no harness = %q (found %v)", got, found)
	}
}

func scopeTestJob(created time.Time, hash string, finished bool) batchv1.Job {
	job := batchv1.Job{ObjectMeta: metav1.ObjectMeta{
		CreationTimestamp: metav1.NewTime(created),
		Annotations:       map[string]string{scopeMemberHashAnnotation: hash},
	}}
	job.Spec.ActiveDeadlineSeconds = ptr.To(int64(420))
	if finished {
		job.Status.Conditions = []batchv1.JobCondition{{Type: batchv1.JobComplete, Status: corev1.ConditionTrue}}
	}
	return job
}

func TestTheScopePublishSchedule(t *testing.T) {
	now := time.Date(2026, 10, 29, 12, 0, 0, 0, time.UTC)
	const hash = "sha256:current"
	cases := []struct {
		name        string
		jobs        []batchv1.Job
		wantStart   bool
		wantRequeue time.Duration
	}{
		{"no Job yet starts one", nil, true, 15 * time.Minute},
		{"an active Job blocks a new one until its deadline",
			[]batchv1.Job{scopeTestJob(now.Add(-time.Minute), hash, false)}, false, 6 * time.Minute},
		{"an active Job blocks a new one even for another hash",
			[]batchv1.Job{scopeTestJob(now.Add(-time.Minute), "sha256:old", false)}, false, 6 * time.Minute},
		{"an active Job past its deadline is looked at again soon",
			[]batchv1.Job{scopeTestJob(now.Add(-10*time.Minute), hash, false)}, false, scopeActiveJobRecheck},
		{"a recent run for this hash waits out the interval",
			[]batchv1.Job{scopeTestJob(now.Add(-5*time.Minute), hash, true)}, false, 10 * time.Minute},
		{"a run 15 minutes old starts the next",
			[]batchv1.Job{scopeTestJob(now.Add(-15*time.Minute), hash, true)}, true, 15 * time.Minute},
		{"a changed hash starts one at once",
			[]batchv1.Job{scopeTestJob(now.Add(-time.Minute), "sha256:old", true)}, true, 15 * time.Minute},
		{"the newest Job decides, not an older one for another hash",
			[]batchv1.Job{scopeTestJob(now.Add(-20*time.Minute), "sha256:old", true), scopeTestJob(now.Add(-2*time.Minute), hash, true)},
			false, 13 * time.Minute},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := decideScopePublish(tc.jobs, hash, now)
			if got.start != tc.wantStart || got.requeue != tc.wantRequeue {
				t.Errorf("decision = %+v, want start %v requeue %s", got, tc.wantStart, tc.wantRequeue)
			}
		})
	}
}

func TestTheScopeResolvedCondition(t *testing.T) {
	now := time.Date(2026, 10, 29, 12, 0, 0, 0, time.UTC)
	agent := scopePublisherAgent()
	hash := scopeMemberHash(agent)
	doc := func(state, memberHash string, age time.Duration) map[string]string {
		return map[string]string{scopeResolvedDataKey: `{"v":1,"resolvedAt":"` + now.Add(-age).Format(time.RFC3339) +
			`","memberHash":"` + memberHash + `","state":"` + state + `","projects":[]}`}
	}
	big := agent.DeepCopy()
	big.Spec.Scope.MaxProjects = ptr.To(int32(250))
	cases := []struct {
		name        string
		agent       *agentv1alpha1.PlatformAgent
		data        map[string]string
		wantStatus  metav1.ConditionStatus
		wantReason  string
		wantRequeue time.Duration
	}{
		{"no ConfigMap", agent, nil, metav1.ConditionFalse, "Missing", 0},
		{"no data key", agent, map[string]string{}, metav1.ConditionFalse, "Missing", 0},
		{"not JSON", agent, map[string]string{scopeResolvedDataKey: "{"}, metav1.ConditionFalse, "Missing", 0},
		{"unknown version", agent, map[string]string{scopeResolvedDataKey: `{"v":2,"resolvedAt":"2026-10-29T12:00:00Z","state":"ok"}`},
			metav1.ConditionFalse, "Missing", 0},
		{"unknown state", agent, doc("partial", hash, time.Minute), metav1.ConditionFalse, "Missing", 0},
		{"fresh", agent, doc("ok", hash, 5*time.Minute), metav1.ConditionTrue, "Resolved", 35 * time.Minute},
		{"older than the bound", agent, doc("ok", hash, 41*time.Minute), metav1.ConditionFalse, "Stale", 0},
		{"the bound grows with maxProjects", big, doc("ok", scopeMemberHash(big), 41*time.Minute), metav1.ConditionTrue, "Resolved", 19 * time.Minute},
		{"another hash", agent, doc("ok", "sha256:old", time.Minute), metav1.ConditionFalse, "HashMismatch", 0},
		{"too large", agent, doc("too-large", hash, time.Minute), metav1.ConditionFalse, "TooLarge", 0},
		{"error", agent, doc("error", hash, time.Minute), metav1.ConditionFalse, "Error", 0},
		{"within the clock skew allowance", agent, doc("ok", hash, -30*time.Second), metav1.ConditionTrue, "Resolved", 40*time.Minute + 30*time.Second},
		{"later than the clock", agent, doc("ok", hash, -50*time.Minute), metav1.ConditionFalse, "Missing", 0},
	}
	// A document value the message quotes is cut, so a ConfigMap writer
	// cannot push the message past the CRD's limit and fail every status
	// write.
	huge := strings.Repeat("x", 40<<10)
	// RFC 3339 parses any number of fractional-second digits, so a
	// resolvedAt that parses can be as long as one that does not.
	longFraction := now.Add(-time.Minute).Format("2006-01-02T15:04:05") + "." + strings.Repeat("0", 40<<10) + "Z"
	for name, data := range map[string]map[string]string{
		"state":      doc(huge, hash, time.Minute),
		"resolvedAt": {scopeResolvedDataKey: `{"v":1,"resolvedAt":"` + huge + `","state":"ok"}`},
		"parsable resolvedAt": {scopeResolvedDataKey: `{"v":1,"resolvedAt":"` + longFraction +
			`","memberHash":"` + hash + `","state":"ok"}`},
	} {
		if got, _ := scopeResolvedCondition(agent, data, now); len(got.Message) > 512 {
			t.Errorf("a 40 KiB %s gives a %d-byte message", name, len(got.Message))
		}
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, requeue := scopeResolvedCondition(tc.agent, tc.data, now)
			if got.Type != "ScopeResolved" || got.Status != tc.wantStatus || got.Reason != tc.wantReason || requeue != tc.wantRequeue {
				t.Errorf("condition %s/%s/%s requeue %s, want %s/%s requeue %s", got.Type, got.Status, got.Reason, requeue,
					tc.wantStatus, tc.wantReason, tc.wantRequeue)
			}
		})
	}
	if bound := scopeFreshnessBound(100); bound != 40*time.Minute {
		t.Errorf("bound at the default cap = %s, want 40m", bound)
	}
}

// The broker's side follows two gates. Under mode next it reads the
// declaration (the mount and CREDENTIAL_PROXY_SCOPE_FILE). While the
// operator says the gateway pod admission policy is rendered and the scope
// declares something to resolve, its fence admits the publisher pods; when
// the account is also this PlatformAgent's, it confers the scope role: the
// audience and the publisher's username on both lists.
func TestTheBrokerConfersTheScopeRoleOnlyBehindTheGatewayPodPolicy(t *testing.T) {
	type want struct{ file, role, peer bool }
	cases := []struct {
		name   string
		mode   *string
		policy string
		owned  brokerCallerOwnership
		want   want
	}{
		{"next, policy unset", ptr.To("next"), "", allBrokerCallersOwned, want{file: true}},
		{"next, policy false", ptr.To("next"), "false", allBrokerCallersOwned, want{file: true}},
		{"next, policy TRUE", ptr.To("next"), "TRUE", allBrokerCallersOwned, want{file: true}},
		{"next, policy 1", ptr.To("next"), "1", allBrokerCallersOwned, want{file: true}},
		{"next, policy true", ptr.To("next"), "true", allBrokerCallersOwned, want{file: true, role: true, peer: true}},
		{"next, policy true, account not ours", ptr.To("next"), "true", brokerCallerOwnership{mcp: true}, want{file: true, peer: true}},
		{"next, policy true, nothing to resolve", ptr.To("next"), "true", allBrokerCallersOwned, want{file: true}},
		{"today, policy true", nil, "true", allBrokerCallersOwned, want{}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(a2aGatewayPodPolicyEnvVar, tc.policy)
			agent := scopePublisherAgent()
			agent.Spec.Mode = tc.mode
			if strings.HasSuffix(tc.name, "nothing to resolve") {
				agent.Spec.Scope = &agentv1alpha1.ScopeSpec{Projects: []string{"team-a"}}
			}
			deployment := buildCredentialProxyDeploymentForPlugins(agent, "hash", nil, tc.owned, credentialProxyProfileConfig{})
			c := deployment.Spec.Template.Spec.Containers[0]

			file, found := brokerEnvValue(c.Env, "CREDENTIAL_PROXY_SCOPE_FILE")
			if found != tc.want.file || (found && file != "/etc/kube-agents/scope/scope.json") {
				t.Errorf("CREDENTIAL_PROXY_SCOPE_FILE = %q (found %v), want rendered %v", file, found, tc.want.file)
			}
			mounted := false
			for _, m := range c.VolumeMounts {
				if m.Name == scopeVolumeName && m.MountPath == "/etc/kube-agents/scope" && m.ReadOnly && m.SubPath == "" {
					mounted = true
				}
			}
			volume := false
			for _, v := range deployment.Spec.Template.Spec.Volumes {
				if v.Name == scopeVolumeName {
					volume = true
				}
			}
			if mounted != tc.want.file || volume != tc.want.file {
				t.Errorf("declaration mounted %v, volume %v, want %v", mounted, volume, tc.want.file)
			}
			audience, audienceFound := brokerEnvValue(c.Env, "CREDENTIAL_PROXY_SCOPE_AUDIENCE")
			callers, callersFound := brokerEnvValue(c.Env, "CREDENTIAL_PROXY_SCOPE_CALLERS")
			allowed, _ := brokerEnvValue(c.Env, "CREDENTIAL_PROXY_ALLOWED_CALLERS")
			if audienceFound != tc.want.role || callersFound != tc.want.role ||
				strings.Contains(allowed, scopePublisherTestCaller) != tc.want.role {
				t.Errorf("scope role: audience %v, callers %v, allowed %q; want rendered %v", audienceFound, callersFound, allowed, tc.want.role)
			}
			if tc.want.role && (audience != "kubeagents-credential-proxy-scope" || callers != scopePublisherTestCaller) {
				t.Errorf("audience %q, callers %q", audience, callers)
			}

			peer := false
			for _, rule := range buildCredentialProxyNetworkPolicy(agent).Spec.Ingress {
				for _, from := range rule.From {
					if from.PodSelector != nil && from.PodSelector.MatchLabels[a2aComponentLabel] == scopePublisherComponent {
						peer = true
					}
				}
			}
			if peer != tc.want.peer {
				t.Errorf("broker fence admits the publisher: %v, want %v", peer, tc.want.peer)
			}
		})
	}
}

// The declaration mounts beside the forge CA mounts, never above them: the
// runtime cannot create a mount point inside a read-only ConfigMap volume,
// so a nested mount would stop the broker on every install with a forge CA
// bundle. The fixture renders a real forge CA mount next to the declaration.
func TestTheBrokerDeclarationMountSitsBesideTheForgeCAMounts(t *testing.T) {
	if forgeCADir == brokerScopeDir || strings.HasPrefix(forgeCADir, brokerScopeDir+"/") {
		t.Fatalf("forge CA mounts under %s sit inside the declaration's volume at %s", forgeCADir, brokerScopeDir)
	}
	agent := gitlabAgent("gitlab-forge-token")
	agent.Spec.Integration.Forges[1].Host = "gitlab.internal.example"
	agent.Spec.Integration.Forges[1].CABundleRef = &agentv1alpha1.ForgeCABundleRef{Name: "gitlab-forge-ca", Key: "root.pem"}
	agent.Spec.Mode = ptr.To("next")
	agent.Spec.Scope = &agentv1alpha1.ScopeSpec{Folders: []string{"111111111111"}}
	mounts := buildCredentialProxyDeploymentForPlugins(agent, "hash", nil, allBrokerCallersOwned, credentialProxyProfileConfig{}).Spec.Template.Spec.Containers[0].VolumeMounts

	var declaration, forgeCA bool
	for _, m := range mounts {
		switch {
		case m.Name == scopeVolumeName:
			declaration = true
		case strings.HasPrefix(m.MountPath, forgeCADir+"/"):
			forgeCA = true
		}
		if m.Name != scopeVolumeName && (m.MountPath == brokerScopeDir || strings.HasPrefix(m.MountPath, brokerScopeDir+"/")) {
			t.Errorf("mount %s sits inside the declaration's read-only volume at %s", m.MountPath, brokerScopeDir)
		}
	}
	if !declaration || !forgeCA {
		t.Fatalf("declaration mounted %v, forge CA mounted %v: the fixture does not render both, so this test proves nothing", declaration, forgeCA)
	}
}

// The publisher's ServiceAccount name is reserved in both modes, as the
// other operator-owned accounts are: neither a profile nor the broker's
// override may hold it, or a flip to next would find it already taken.
func TestThePublisherServiceAccountNameIsReserved(t *testing.T) {
	for _, mode := range []*string{ptr.To("next"), nil} {
		agent := scopePublisherAgent()
		agent.Spec.Mode = mode
		sa := scopePublisherName(agent)
		p := testAgentProfile(agent.Namespace, "sneaky", func(p *agentv1alpha1.AgentProfile) {
			p.Spec.Identity.ServiceAccountName = sa
		})
		if got := resolveAgentProfileIdentities(agent, []agentv1alpha1.AgentProfile{p})["sneaky"]; got.refused == nil {
			t.Errorf("mode %v: a profile naming %q resolved to %+v, want refused", mode, sa, got)
		}
		agent.Spec.Security = &agentv1alpha1.SecuritySpec{ServiceAccountName: sa}
		if msg := ReservedServiceAccountOverride(agent); msg == "" {
			t.Errorf("mode %v: an override naming %q is not refused", mode, sa)
		}
	}
}

func TestTheReservedListNamesTheScopeBrokerVariables(t *testing.T) {
	names := []string{"CREDENTIAL_PROXY_SCOPE_AUDIENCE", "CREDENTIAL_PROXY_SCOPE_CALLERS", "CREDENTIAL_PROXY_SCOPE_FILE"}
	var extra []corev1.EnvVar
	for _, name := range names {
		extra = append(extra, corev1.EnvVar{Name: name, Value: "theirs"})
	}
	merged := mergeCredentialProxyEnv(nil, extra)
	for _, name := range names {
		if _, count := envValueCount(merged, name); count != 0 {
			t.Errorf("%s survived the merge from spec.deployment.env", name)
		}
	}
}

func TestTheGatewayReadsTheResolvedScopeUnderNextOnly(t *testing.T) {
	rule := rbacv1.PolicyRule{APIGroups: []string{""}, Resources: []string{"configmaps"},
		ResourceNames: []string{"test-agent-resolved-scope"}, Verbs: []string{"get"}}
	hasRule := func(role *rbacv1.Role) bool {
		for _, r := range role.Rules {
			if policyRulesEqual(r, rule) {
				return true
			}
		}
		return false
	}

	agent := scopePublisherAgent()
	if !hasRule(buildA2AGatewayRole(agent)) {
		t.Errorf("the gateway Role has no get on the resolved scope under next")
	}
	env := buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env
	if got, found := brokerEnvValue(env, "A2A_RESOLVED_SCOPE_CONFIGMAP"); !found || got != "test-agent-resolved-scope" {
		t.Errorf("A2A_RESOLVED_SCOPE_CONFIGMAP = %q (found %v)", got, found)
	}

	today := agent.DeepCopy()
	today.Spec.Mode = nil
	if hasRule(buildA2AGatewayRole(today)) {
		t.Errorf("the gateway Role reads the resolved scope under today")
	}
}

func scopePublisherReconciler(t *testing.T, objs ...client.Object) (*PlatformAgentReconciler, client.Client) {
	t.Helper()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl
}

func scopePublisherJobs(t *testing.T, cl client.Client, agent *agentv1alpha1.PlatformAgent) []batchv1.Job {
	t.Helper()
	var jobs batchv1.JobList
	if err := cl.List(context.Background(), &jobs, client.InNamespace(agent.Namespace),
		client.MatchingLabels(scopePublisherPodLabels(agent))); err != nil {
		t.Fatal(err)
	}
	return jobs.Items
}

func TestReconcileScopePublisher(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 10, 29, 12, 0, 0, 0, time.UTC)
	staticKeys := func(agent *agentv1alpha1.PlatformAgent) map[string]client.Object {
		return map[string]client.Object{
			scopeResolvedConfigMapName(agent): &corev1.ConfigMap{},
			scopePublisherName(agent) + "/sa": &corev1.ServiceAccount{},
			scopePublisherName(agent) + "/r":  &rbacv1.Role{},
			scopePublisherName(agent) + "/rb": &rbacv1.RoleBinding{},
			scopePublisherName(agent) + "/np": &networkingv1.NetworkPolicy{},
		}
	}
	assertNoIdentity := func(t *testing.T, cl client.Client, agent *agentv1alpha1.PlatformAgent) {
		t.Helper()
		if err := cl.Get(ctx, types.NamespacedName{Name: scopeResolvedConfigMapName(agent), Namespace: agent.Namespace}, &corev1.ConfigMap{}); err != nil {
			t.Errorf("the resolved-scope ConfigMap is not rendered: %v", err)
		}
		for _, obj := range []client.Object{&rbacv1.Role{}, &rbacv1.RoleBinding{}, &networkingv1.NetworkPolicy{}} {
			if err := cl.Get(ctx, types.NamespacedName{Name: scopePublisherName(agent), Namespace: agent.Namespace}, obj); !apierrors.IsNotFound(err) {
				t.Errorf("%T rendered while no publisher Job may run (err=%v)", obj, err)
			}
		}
	}
	assertStatic := func(t *testing.T, cl client.Client, agent *agentv1alpha1.PlatformAgent) {
		t.Helper()
		for key, obj := range staticKeys(agent) {
			name, _, _ := strings.Cut(key, "/")
			if err := cl.Get(ctx, types.NamespacedName{Name: name, Namespace: agent.Namespace}, obj); err != nil {
				t.Errorf("%T %s: %v", obj, name, err)
				continue
			}
			if !metav1.IsControlledBy(obj, agent) {
				t.Errorf("%T %s is not controlled by the PlatformAgent", obj, name)
			}
		}
	}

	t.Run("a folder scope starts one Job and reads the ConfigMap", func(t *testing.T) {
		t.Setenv(a2aGatewayPodPolicyEnvVar, "true")
		agent := scopePublisherAgent()
		r, cl := scopePublisherReconciler(t, agent)
		outcome, err := r.reconcileScopePublisher(ctx, agent, now)
		if err != nil {
			t.Fatal(err)
		}
		assertStatic(t, cl, agent)
		if jobs := scopePublisherJobs(t, cl, agent); len(jobs) != 1 || !metav1.IsControlledBy(&jobs[0], agent) {
			t.Fatalf("Jobs after the first pass = %d, want one owned", len(jobs))
		}
		if outcome.condition == nil || outcome.condition.Reason != "Missing" || outcome.requeue != 15*time.Minute {
			t.Errorf("outcome %+v, want Missing and a 15m requeue", outcome)
		}

		// A second pass while that Job runs starts none.
		if _, err := r.reconcileScopePublisher(ctx, agent, now.Add(time.Minute)); err != nil {
			t.Fatal(err)
		}
		if jobs := scopePublisherJobs(t, cl, agent); len(jobs) != 1 {
			t.Errorf("Jobs with one active = %d, want 1", len(jobs))
		}
	})

	t.Run("without the gateway pod policy no Job runs, no grant renders and the condition says why", func(t *testing.T) {
		t.Setenv(a2aGatewayPodPolicyEnvVar, "true")
		agent := scopePublisherAgent()
		r, cl := scopePublisherReconciler(t, agent)
		if _, err := r.reconcileScopePublisher(ctx, agent, now); err != nil {
			t.Fatal(err)
		}
		assertStatic(t, cl, agent)
		// The policy goes away: the grant an earlier pass rendered goes too.
		t.Setenv(a2aGatewayPodPolicyEnvVar, "")
		outcome, err := r.reconcileScopePublisher(ctx, agent, now.Add(time.Minute))
		if err != nil {
			t.Fatal(err)
		}
		assertNoIdentity(t, cl, agent)
		if err := cl.Get(ctx, types.NamespacedName{Name: scopePublisherName(agent), Namespace: agent.Namespace}, &corev1.ServiceAccount{}); !apierrors.IsNotFound(err) {
			t.Errorf("the publisher's ServiceAccount survived (err=%v)", err)
		}
		if jobs := scopePublisherJobs(t, cl, agent); len(jobs) != 1 {
			t.Errorf("Jobs = %d after the policy went, want only the one started before", len(jobs))
		}
		if outcome.condition == nil || outcome.condition.Status != metav1.ConditionFalse || outcome.condition.Reason != "ScopeRoleNotConferred" {
			t.Errorf("condition %+v, want False/ScopeRoleNotConferred", outcome.condition)
		}
	})

	t.Run("a scope with nothing to resolve renders the ConfigMap, no grant, no Job and no condition", func(t *testing.T) {
		t.Setenv(a2aGatewayPodPolicyEnvVar, "true")
		agent := scopePublisherAgent()
		agent.Spec.Scope = &agentv1alpha1.ScopeSpec{Projects: []string{"team-a"}}
		r, cl := scopePublisherReconciler(t, agent)
		outcome, err := r.reconcileScopePublisher(ctx, agent, now)
		if err != nil {
			t.Fatal(err)
		}
		assertNoIdentity(t, cl, agent)
		if jobs := scopePublisherJobs(t, cl, agent); len(jobs) != 0 {
			t.Errorf("Jobs = %d, want none", len(jobs))
		}
		if outcome.condition != nil || outcome.requeue != 0 {
			t.Errorf("outcome %+v, want no condition and no requeue", outcome)
		}
	})

	t.Run("an unowned ServiceAccount under the name is not adopted and runs no Job", func(t *testing.T) {
		t.Setenv(a2aGatewayPodPolicyEnvVar, "true")
		agent := scopePublisherAgent()
		squatter := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: scopePublisherName(agent), Namespace: agent.Namespace}}
		r, cl := scopePublisherReconciler(t, agent, squatter)
		owned, err := r.scopePublisherIdentityOwned(ctx, agent)
		if err != nil || owned {
			t.Fatalf("scopePublisherIdentityOwned = %v, %v; want false", owned, err)
		}
		outcome, err := r.reconcileScopePublisher(ctx, agent, now)
		if err != nil {
			t.Fatal(err)
		}
		if outcome.condition == nil || outcome.condition.Reason != "UnownedServiceAccount" ||
			!strings.Contains(outcome.condition.Message, scopePublisherName(agent)) {
			t.Errorf("condition %+v, want UnownedServiceAccount naming the account", outcome.condition)
		}
		sa := &corev1.ServiceAccount{}
		if err := cl.Get(ctx, client.ObjectKeyFromObject(squatter), sa); err != nil || len(sa.OwnerReferences) != 0 {
			t.Errorf("the unowned ServiceAccount was adopted or removed: %+v, %v", sa.OwnerReferences, err)
		}
		if jobs := scopePublisherJobs(t, cl, agent); len(jobs) != 0 {
			t.Errorf("Jobs = %d under an unowned identity, want none", len(jobs))
		}
		for _, obj := range []client.Object{&rbacv1.Role{}, &rbacv1.RoleBinding{}, &networkingv1.NetworkPolicy{}} {
			if err := cl.Get(ctx, types.NamespacedName{Name: scopePublisherName(agent), Namespace: agent.Namespace}, obj); !apierrors.IsNotFound(err) {
				t.Errorf("%T rendered for an unowned identity (err=%v)", obj, err)
			}
		}

		// The teardown leaves it alone too.
		if err := r.removeScopePublisher(ctx, agent); err != nil {
			t.Fatal(err)
		}
		if err := cl.Get(ctx, client.ObjectKeyFromObject(squatter), &corev1.ServiceAccount{}); err != nil {
			t.Errorf("the teardown removed an unowned ServiceAccount: %v", err)
		}
	})

	t.Run("a ServiceAccount replaced by an unowned one takes the grant with it", func(t *testing.T) {
		t.Setenv(a2aGatewayPodPolicyEnvVar, "true")
		agent := scopePublisherAgent()
		r, cl := scopePublisherReconciler(t, agent)
		if _, err := r.reconcileScopePublisher(ctx, agent, now); err != nil {
			t.Fatal(err)
		}
		assertStatic(t, cl, agent)
		key := types.NamespacedName{Name: scopePublisherName(agent), Namespace: agent.Namespace}
		if err := cl.Delete(ctx, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace}}); err != nil {
			t.Fatal(err)
		}
		if err := cl.Create(ctx, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace}}); err != nil {
			t.Fatal(err)
		}
		if _, err := r.reconcileScopePublisher(ctx, agent, now.Add(time.Minute)); err != nil {
			t.Fatal(err)
		}
		assertNoIdentity(t, cl, agent)
		if err := cl.Get(ctx, key, &corev1.ServiceAccount{}); err != nil {
			t.Errorf("the unowned ServiceAccount was removed: %v", err)
		}
	})

	t.Run("the teardown removes every owned object", func(t *testing.T) {
		t.Setenv(a2aGatewayPodPolicyEnvVar, "true")
		agent := scopePublisherAgent()
		r, cl := scopePublisherReconciler(t, agent)
		if _, err := r.reconcileScopePublisher(ctx, agent, now); err != nil {
			t.Fatal(err)
		}
		if err := r.removeScopePublisher(ctx, agent); err != nil {
			t.Fatal(err)
		}
		for key, obj := range staticKeys(agent) {
			name, _, _ := strings.Cut(key, "/")
			if err := cl.Get(ctx, types.NamespacedName{Name: name, Namespace: agent.Namespace}, obj); !apierrors.IsNotFound(err) {
				t.Errorf("%T %s survived the teardown (err=%v)", obj, name, err)
			}
		}
		if jobs := scopePublisherJobs(t, cl, agent); len(jobs) != 0 {
			t.Errorf("Jobs = %d after the teardown", len(jobs))
		}
	})
}

// Through the stack: reconcileA2A renders the publisher and carries its
// condition out, and the flip to today (cleanupA2A) takes it all away.
func TestTheScopePublisherComesAndGoesWithTheStack(t *testing.T) {
	t.Setenv(a2aGatewayPodPolicyEnvVar, "true")
	ctx := context.Background()
	agent := a2aTestAgent()
	agent.Spec.Scope = &agentv1alpha1.ScopeSpec{Folders: []string{"111111111111"}}
	r, cl, _ := a2aGateTestReconciler(t, agent)
	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatal(err)
	}
	if state.scopeCondition == nil || state.scopeCondition.Reason != "Missing" || state.scopeRequeue <= 0 {
		t.Errorf("state carries condition %+v, requeue %s", state.scopeCondition, state.scopeRequeue)
	}
	key := types.NamespacedName{Name: scopeResolvedConfigMapName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, key, &corev1.ConfigMap{}); err != nil {
		t.Fatalf("the resolved-scope ConfigMap was not rendered: %v", err)
	}
	if len(scopePublisherJobs(t, cl, agent)) != 1 {
		t.Fatal("no publisher Job after the first render")
	}

	if err := r.cleanupA2A(ctx, agent); err != nil {
		t.Fatalf("cleanupA2A: %v", err)
	}
	if err := cl.Get(ctx, key, &corev1.ConfigMap{}); !apierrors.IsNotFound(err) {
		t.Errorf("the resolved-scope ConfigMap survived the flip (err=%v)", err)
	}
	if n := len(scopePublisherJobs(t, cl, agent)); n != 0 {
		t.Errorf("%d publisher Jobs survived the flip", n)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: scopePublisherName(agent), Namespace: agent.Namespace}, &corev1.ServiceAccount{}); !apierrors.IsNotFound(err) {
		t.Errorf("the publisher's ServiceAccount survived the flip (err=%v)", err)
	}
}

func TestTheScopeResolvedConditionIsCarriedAndRemoved(t *testing.T) {
	agent := scopePublisherAgent()
	agent.Generation = 3
	now := metav1.Now()
	want, _ := scopeResolvedCondition(agent, nil, now.Time)

	if scopeResolvedConditionCurrent(agent, &want) {
		t.Error("an absent condition reads as current")
	}
	setScopeResolvedCondition(agent, &want, now)
	if !scopeResolvedConditionCurrent(agent, &want) {
		t.Error("the condition just set does not read as current")
	}
	if scopeResolvedConditionCurrent(agent, nil) {
		t.Error("a set condition reads as current when none is wanted")
	}
	setScopeResolvedCondition(agent, nil, now)
	if !scopeResolvedConditionCurrent(agent, nil) {
		t.Error("the condition was not removed")
	}
}
