package controller

import (
	"context"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// envtestTierParams creates every param the operator renders for profiles,
// through its own builders: each (profile, tier)'s template, defaulted by
// the API server, under its profile's one hash, then the index.
func envtestTierParams(ctx context.Context, t *testing.T, admin client.Client, agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) map[string]profileJobTemplates {
	t.Helper()
	templates := dispatchJobTemplates(agent, profiles, false)
	r := &PlatformAgentReconciler{Client: admin}
	for _, set := range templates {
		hash := dispatchJobTemplatesHash(set)
		for name, tmpl := range set {
			defaulted, err := r.defaultedJobTemplate(ctx, profileNamespaceName(agent), name, tmpl)
			if err != nil {
				t.Fatal(err)
			}
			param, err := buildDispatchJobTemplate(agent, name, hash, defaulted)
			if err != nil {
				t.Fatal(err)
			}
			mustCreate(ctx, t, admin, param)
		}
	}
	mustCreate(ctx, t, admin, buildDispatchJobIndex(agent, templates))
	return templates
}

// envtestJobPolicySetup starts an API server with the shipped dispatcher Job
// policy, the profile namespace as the chart labels it, and the dispatcher's
// Jobs Role, and returns an admin client and one acting as the dispatcher.
func envtestJobPolicySetup(t *testing.T) (context.Context, client.Client, client.Client, *agentv1alpha1.PlatformAgent) {
	t.Helper()
	cfg, scheme := startEnvtestConfig(t)
	admin, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	agent := a2aTestAgent()
	mustCreate(ctx, t, admin, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}})
	mustCreate(ctx, t, admin, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: profileNamespaceName(agent),
		Labels: map[string]string{profileNamespaceComponentLabel: profileNamespaceComponent, labelPartOf: partOfKubeAgents}}})
	mustCreate(ctx, t, admin, buildA2ADispatcherJobsRole(agent))
	mustCreate(ctx, t, admin, buildA2ADispatcherJobsRoleBinding(agent))
	applyDispatcherJobPolicy(ctx, t, admin)
	return ctx, admin, impersonatedClient(t, cfg, agent.Namespace, a2aDispatcherName(agent)), agent
}

// waitAdmitted creates the Job as the dispatcher until the policy, whose
// params come from an informer, admits it.
func waitAdmitted(ctx context.Context, t *testing.T, dispatcher client.Client, name string, tmpl *corev1.PodTemplateSpec) time.Duration {
	t.Helper()
	start := time.Now()
	for n := 0; ; n++ {
		err := dispatcher.Create(ctx, dispatcherJob(fmt.Sprintf("%s-%d", name, n), tmpl, nil))
		if err == nil {
			return time.Since(start)
		}
		if time.Since(start) > 2*gatewayPodPolicyActivation {
			t.Fatalf("%s was not admitted within %s: %v", name, 2*gatewayPodPolicyActivation, err)
		}
		time.Sleep(200 * time.Millisecond)
	}
}

// The policy is unchanged by per-tier templates (#5646): it holds a Job to
// the template of the ServiceAccount it runs as, and ServiceAccounts are
// unique per (profile, tier). So a read-tier Job is admitted exactly as its
// read template, and every way of borrowing another tier's template is
// denied: the read ServiceAccount under the unattended tier's labels, and
// the read template with its tier label naming the unattended tier.
func TestTheJobPolicyHoldsAReadTierJobToItsOwnTemplate(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	ctx, admin, dispatcher, agent := envtestJobPolicySetup(t)
	templates := envtestTierParams(ctx, t, admin, agent, []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")})
	read := templates["tse"][dispatchJobTierTemplatePrefix+"tse.read"]
	unattended := templates["tse"][dispatchJobTemplatePrefix+"tse"]
	if read == nil || unattended == nil {
		t.Fatalf("rendered %v, want both tiers", paramsOf(templates["tse"]))
	}
	waitAdmitted(ctx, t, dispatcher, "read", read)
	waitAdmitted(ctx, t, dispatcher, "unattended", unattended)

	for name, c := range map[string]struct {
		tmpl func() *corev1.PodTemplateSpec
		rule string
	}{
		"the read ServiceAccount under the unattended tier's template and labels": {func() *corev1.PodTemplateSpec {
			t := unattended.DeepCopy()
			t.Spec.ServiceAccountName = read.Spec.ServiceAccountName
			return t
		}, "must have exactly the pod spec"},
		"the read template with its tier label naming the unattended tier": {func() *corev1.PodTemplateSpec {
			t := read.DeepCopy()
			t.Labels[agentProfileTierLabel] = agentv1alpha1.AgentProfileTierUnattended
			return t
		}, "must carry exactly the pod labels"},
		"the read template running as an account no template runs as": {func() *corev1.PodTemplateSpec {
			t := read.DeepCopy()
			t.Spec.ServiceAccountName = "agentprofile-tse-triage"
			return t
		}, "must run as a profile ServiceAccount the operator rendered"},
	} {
		err := dispatcher.Create(ctx, dispatcherJob("borrow", c.tmpl(), nil))
		if !apierrors.IsForbidden(err) && !apierrors.IsInvalid(err) {
			t.Errorf("%s: admitted (%v)", name, err)
			continue
		}
		if !strings.Contains(err.Error(), c.rule) {
			t.Errorf("%s: refused for %q, want the rule %q", name, err, c.rule)
		}
	}
}

// L6 of the #5646 design: the policy evaluates every param on each Job, so
// per-tier templates double what it reads at a two-tier fleet. Measured with
// 2,500 two-tier profiles' params when KUBE_AGENTS_JOB_POLICY_SCALE is set
// (slow: it writes 5,000 params); the time to admit one Job is logged.
func TestTheJobPolicyAtTwentyFiveHundredTwoTierProfiles(t *testing.T) {
	n := 2500
	if os.Getenv("KUBE_AGENTS_JOB_POLICY_SCALE") == "" {
		t.Skip("KUBE_AGENTS_JOB_POLICY_SCALE is unset: it writes 5,000 params")
	}
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	withOperatorBusPrincipal(t)
	t.Setenv(a2aProfileDispatcherEnvVar, "false")
	ctx, admin, dispatcher, agent := envtestJobPolicySetup(t)
	// KUBE_AGENTS_JOB_POLICY_SCALE=unattended measures the shape before
	// per-tier templates, one param per profile, for comparison.
	oneTier := os.Getenv("KUBE_AGENTS_JOB_POLICY_SCALE") == "unattended"
	profiles := make([]agentv1alpha1.AgentProfile, 0, n)
	for i := range n {
		p := unattendedProfile(agent, fmt.Sprintf("team-%03d-cluster-%02d", i/25, i%25))
		if oneTier {
			p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierUnattended}
		}
		profiles = append(profiles, p)
	}
	templates := envtestTierParams(ctx, t, admin, agent, profiles)
	params := 0
	for _, set := range templates {
		params += len(set)
	}
	var last *corev1.PodTemplateSpec
	for _, tmpl := range templates[profiles[n-1].Name] {
		last = tmpl
	}
	took := waitAdmitted(ctx, t, dispatcher, "scale", last)
	start := time.Now()
	if err := dispatcher.Create(ctx, dispatcherJob("scale-steady", last, func(j *batchv1.Job) { j.Spec.TTLSecondsAfterFinished = ptr.To(int32(300)) })); err != nil {
		t.Fatalf("a read-tier Job at %d two-tier profiles: %v", n, err)
	}
	t.Logf("%d profiles (%d params): first admitted after %s, a Job admitted in %s", n, params, took.Round(time.Millisecond), time.Since(start).Round(time.Millisecond))
}
