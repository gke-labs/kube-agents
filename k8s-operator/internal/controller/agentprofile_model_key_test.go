package controller

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The AI workload audit is the source of truth for which env names hold a
// model credential. This test reads its patterns from the audit's source, so
// the two cannot drift apart.
var fleetAuditCollectPath = filepath.Join("..", "..", "..", "agents", "platform", "skills", "fleet-audit", "scripts", "collect.py")

// auditPattern reads one `NAME = re.compile(r"..." r"...", re.IGNORECASE)`
// from collect.py and compiles it as Go RE2. The audit uses re.search, which
// is unanchored, as MatchString is.
func auditPattern(t *testing.T, source, name string) *regexp.Regexp {
	t.Helper()
	decl := regexp.MustCompile(`(?m)^` + regexp.QuoteMeta(name) + `\s*=\s*re\.compile\(\s*((?:r"[^"]*"\s*)+),\s*re\.IGNORECASE\s*,?\s*\)`)
	m := decl.FindStringSubmatch(source)
	if m == nil {
		t.Fatalf("%s: no %s = re.compile(r\"...\", re.IGNORECASE); update this test to the audit's new form", fleetAuditCollectPath, name)
	}
	var pattern strings.Builder
	for _, piece := range regexp.MustCompile(`r"([^"]*)"`).FindAllStringSubmatch(m[1], -1) {
		pattern.WriteString(piece[1])
	}
	re, err := regexp.Compile("(?i)" + pattern.String())
	if err != nil {
		t.Fatalf("%s from %s does not compile as RE2: %v", name, fleetAuditCollectPath, err)
	}
	return re
}

// No AgentProfile worker container, in a warm pool or in a Job, may set a
// literal value on an env name that the AI workload audit reads as a model
// credential. AI_PROVIDER_CREDENTIAL_ENV_RE puts the workload in the audit's
// scope, and check 3.5 (model-credential-plaintext-env) then flags the
// literal value as major, also when the value is only a placeholder. A
// valueFrom reference is correct and the test permits it.
func TestNoProfileWorkerContainerSetsALiteralModelCredential(t *testing.T) {
	raw, err := os.ReadFile(fleetAuditCollectPath)
	if err != nil {
		t.Fatalf("reading the audit's patterns: %v", err)
	}
	source := string(raw)
	scope := auditPattern(t, source, "AI_PROVIDER_CREDENTIAL_ENV_RE")
	named := auditPattern(t, source, "AI_CREDENTIAL_ENV_NAME_RE")
	safe := auditPattern(t, source, "AI_CREDENTIAL_ENV_NAME_SAFE_SUFFIX_RE")
	if !scope.MatchString("ANTHROPIC_API_KEY") {
		t.Fatalf("precondition: the scope pattern %q does not match ANTHROPIC_API_KEY", scope)
	}

	check := func(t *testing.T, where string, spec corev1.PodSpec) {
		t.Helper()
		containers := append(append([]corev1.Container(nil), spec.InitContainers...), spec.Containers...)
		for _, c := range containers {
			for _, e := range c.Env {
				if e.ValueFrom != nil || e.Value == "" {
					continue
				}
				if scope.MatchString(e.Name) || (named.MatchString(e.Name) && !safe.MatchString(e.Name)) {
					t.Errorf("%s: container %s sets a literal %s=%q; the AI workload audit flags it (model-credential-plaintext-env)", where, c.Name, e.Name, e.Value)
				}
			}
		}
	}

	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	profile := unattendedProfile(agent, "tse")
	for _, imageVolumes := range []bool{false, true} {
		check(t, "warm pool", buildProfilePool(agent, &profile, agentv1alpha1.AgentProfileTierRead, agentProfileServiceAccountName(&profile, agentv1alpha1.AgentProfileTierRead), imageVolumes).Spec.Template.Spec)
		templates := dispatchJobTemplates(agent, []agentv1alpha1.AgentProfile{profile}, imageVolumes)
		if templates[profile.Name] == nil {
			t.Fatalf("precondition: no Job template for %s (imageVolumes %v)", profile.Name, imageVolumes)
		}
		check(t, "Job template", templates[profile.Name].Spec)
		// The dispatcher builds each Job from the DispatchJobTemplate, not
		// from the rendering, so check the template that the param holds too.
		param, err := buildDispatchJobTemplate(agent, profile.Name, templates[profile.Name], templates[profile.Name])
		if err != nil {
			t.Fatal(err)
		}
		held, ok := dispatchJobParamTemplate(param)
		if !ok {
			t.Fatalf("precondition: the DispatchJobTemplate of %s holds no pod template (imageVolumes %v)", profile.Name, imageVolumes)
		}
		check(t, "DispatchJobTemplate", held.Spec)
	}
}
