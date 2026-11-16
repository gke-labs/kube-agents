package controller

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync/atomic"
	"testing"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const testCustomUniverseDomain = "universe.example.test"

func TestProbeUniverseDomainURL(t *testing.T) {
	t.Run("valid domain with whitespace and mixed case", func(t *testing.T) {
		var sawHeader string
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			sawHeader = r.Header.Get(gceMetadataFlavorHeader)
			_, _ = w.Write([]byte("  Universe.Example.Test \n"))
		}))
		defer srv.Close()

		got, ok := probeUniverseDomainURL(context.Background(), srv.URL, srv.Client())
		if !ok || got != testCustomUniverseDomain {
			t.Fatalf("probeUniverseDomainURL = (%q, %v), want (%q, true)", got, ok, testCustomUniverseDomain)
		}
		if sawHeader != gceMetadataFlavorGoogle {
			t.Fatalf("Metadata-Flavor header = %q, want %q", sawHeader, gceMetadataFlavorGoogle)
		}
	})

	t.Run("404 is authoritative empty domain", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNotFound)
		}))
		defer srv.Close()

		got, ok := probeUniverseDomainURL(context.Background(), srv.URL, srv.Client())
		if !ok || got != "" {
			t.Fatalf("probeUniverseDomainURL on 404 = (%q, %v), want (\"\", true)", got, ok)
		}
	})

	t.Run("503 and malformed payloads are non-authoritative", func(t *testing.T) {
		srv503 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusServiceUnavailable)
		}))
		defer srv503.Close()
		if got, ok := probeUniverseDomainURL(context.Background(), srv503.URL, srv503.Client()); ok || got != "" {
			t.Fatalf("probeUniverseDomainURL on 503 = (%q, %v), want (\"\", false)", got, ok)
		}

		srvBad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte("not a domain!"))
		}))
		defer srvBad.Close()
		if got, ok := probeUniverseDomainURL(context.Background(), srvBad.URL, srvBad.Client()); ok || got != "" {
			t.Fatalf("probeUniverseDomainURL on bad body = (%q, %v), want (\"\", false)", got, ok)
		}
	})
}

func TestHostUniverseDomainCachesAuthoritativeAnswersOnly(t *testing.T) {
	var calls atomic.Int32
	r := &PlatformAgentReconciler{
		UniverseDomainDetector: func(_ context.Context) (string, bool) {
			n := calls.Add(1)
			if n == 1 {
				return "", false
			}
			return testCustomUniverseDomain, true
		},
	}

	if got, det := r.hostUniverseDomain(context.Background()); got != "" || det {
		t.Fatalf("first transient call = (%q, %v), want (empty, false)", got, det)
	}
	if got, det := r.hostUniverseDomain(context.Background()); got != testCustomUniverseDomain || !det {
		t.Fatalf("second authoritative call = (%q, %v), want (%q, true)", got, det, testCustomUniverseDomain)
	}
	if got, det := r.hostUniverseDomain(context.Background()); got != testCustomUniverseDomain || !det {
		t.Fatalf("third cached call = (%q, %v), want (%q, true)", got, det, testCustomUniverseDomain)
	}
	if calls.Load() != 2 {
		t.Fatalf("detector calls = %d, want 2 (1 transient + 1 cached)", calls.Load())
	}
}

func TestValidateUniverseDomainMismatchAndShape(t *testing.T) {
	r := &PlatformAgentReconciler{
		UniverseDomainDetector: func(_ context.Context) (string, bool) {
			return testCustomUniverseDomain, true
		},
	}

	base := func(domain, location string) *agentv1alpha1.PlatformAgent {
		return &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: "kubeagents-system"},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Harness: &agentv1alpha1.HarnessSpec{
					ProjectID:      "sample-project",
					Location:       location,
					ClusterName:    "host-cluster",
					UniverseDomain: domain,
				},
			},
		}
	}

	// Unset spec domain adopts discovered domain without mismatch.
	if reason, _, transient := r.validateUniverseDomain(context.Background(), base("", "us-central1")); reason != "" || transient {
		t.Fatalf("unexpected mismatch/transient for empty spec.harness.universeDomain: %s / %v", reason, transient)
	}
	domain, src := r.resolvedUniverseDomainPair(context.Background(), base("", "us-central1"))
	if domain != testCustomUniverseDomain || src != universeDomainSourceDiscovered {
		t.Fatalf("resolvedUniverseDomainPair = (%q, %q), want (%q, %q)", domain, src, testCustomUniverseDomain, universeDomainSourceDiscovered)
	}

	// Matching explicit spec domain passes with Spec source.
	if reason, _, transient := r.validateUniverseDomain(context.Background(), base(testCustomUniverseDomain, "us-central1")); reason != "" || transient {
		t.Fatalf("unexpected mismatch/transient for matching spec.harness.universeDomain: %s / %v", reason, transient)
	}
	domain, src = r.resolvedUniverseDomainPair(context.Background(), base(testCustomUniverseDomain, "us-central1"))
	if domain != testCustomUniverseDomain || src != universeDomainSourceSpec {
		t.Fatalf("resolvedUniverseDomainPair = (%q, %q), want (%q, %q)", domain, src, testCustomUniverseDomain, universeDomainSourceSpec)
	}

	// Mismatched explicit spec domain fails closed before rendering.
	reason, msg, transient := r.validateUniverseDomain(context.Background(), base(agentv1alpha1.DefaultUniverseDomain, "us-central1"))
	if transient || reason != reasonUniverseDomainMismatch {
		t.Fatalf("validateUniverseDomain = (%q, transient=%v), want %q", reason, transient, reasonUniverseDomainMismatch)
	}
	if !strings.Contains(msg, testCustomUniverseDomain) || !strings.Contains(msg, agentv1alpha1.DefaultUniverseDomain) {
		t.Fatalf("unexpected mismatch message: %q", msg)
	}

	// Malformed domain fails closed even when webhooks are disabled.
	reason, _, transient = r.validateUniverseDomain(context.Background(), base("https://bad/domain", "us-central1"))
	if transient || reason != reasonUniverseDomainMismatch {
		t.Fatalf("malformed universeDomain = (%q, transient=%v), want %q", reason, transient, reasonUniverseDomainMismatch)
	}

	// Kind clusters (location: kind) skip host metadata comparison.
	if reason, _, transient := r.validateUniverseDomain(context.Background(), base(agentv1alpha1.DefaultUniverseDomain, kindLocation)); reason != "" || transient {
		t.Fatalf("kind cluster should not compare against GCE metadata, got (%q, %v)", reason, transient)
	}

	// Transient metadata probe failure refuses unverified cross-universe rendering
	// and preserves an already-discovered status.resolvedUniverseDomain.
	flakyReconciler := &PlatformAgentReconciler{
		UniverseDomainDetector: func(_ context.Context) (string, bool) {
			return "", false
		},
	}
	if reason, _, transient := flakyReconciler.validateUniverseDomain(context.Background(), base(agentv1alpha1.DefaultUniverseDomain, "us-central1")); !transient || reason != "" {
		t.Fatalf("transient metadata failure must return transient=true without Degraded reason, got (%q, %v)", reason, transient)
	}
	existingDiscovered := base("", "us-central1")
	existingDiscovered.Status.ResolvedUniverseDomain = testCustomUniverseDomain
	existingDiscovered.Status.UniverseDomainSource = universeDomainSourceDiscovered
	keptDomain, keptSource := flakyReconciler.resolvedUniverseDomainPair(context.Background(), existingDiscovered)
	if keptDomain != testCustomUniverseDomain || keptSource != universeDomainSourceDiscovered {
		t.Fatalf("transient metadata failure rolled back discovered status to (%q, %q)", keptDomain, keptSource)
	}
}

func TestCustomUniversePropagatesToManifestsAndStripsExtraEnvOverrides(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: "kubeagents-system"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:      "sample-project",
				Location:       "us-central1",
				ClusterName:    "host-cluster",
				UniverseDomain: testCustomUniverseDomain,
			},
		},
	}

	envMap := func(vars []corev1.EnvVar) map[string]string {
		out := make(map[string]string, len(vars))
		for _, v := range vars {
			out[v.Name] = v.Value
		}
		return out
	}

	overrideEnv := []corev1.EnvVar{
		{Name: agentv1alpha1.GoogleCloudUniverseDomainEnv, Value: "evil.example.com"},
		{Name: kubeProxyUniverseDomainEnv, Value: "evil.example.com"},
	}
	cpEnv := envMap(mergeCredentialProxyEnv(buildCredentialProxyEnv(agent), overrideEnv))
	if got := cpEnv[agentv1alpha1.GoogleCloudUniverseDomainEnv]; got != testCustomUniverseDomain {
		t.Fatalf("credential-proxy %s = %q, want %q", agentv1alpha1.GoogleCloudUniverseDomainEnv, got, testCustomUniverseDomain)
	}
	if _, leaked := cpEnv[kubeProxyUniverseDomainEnv]; leaked {
		t.Fatalf("credential-proxy ExtraEnv smuggled %s through mergeCredentialProxyEnv", kubeProxyUniverseDomainEnv)
	}

	kpDeploy := buildKubeProxyDeployment(agent, "kube-proxy:test", false, "ca-hash")
	kpEnv := envMap(kpDeploy.Spec.Template.Spec.Containers[0].Env)
	if got := kpEnv[kubeProxyUniverseDomainEnv]; got != testCustomUniverseDomain {
		t.Fatalf("kube-proxy %s = %q, want %q", kubeProxyUniverseDomainEnv, got, testCustomUniverseDomain)
	}
	if got := kpEnv[agentv1alpha1.GoogleCloudUniverseDomainEnv]; got != testCustomUniverseDomain {
		t.Fatalf("kube-proxy %s = %q, want %q", agentv1alpha1.GoogleCloudUniverseDomainEnv, got, testCustomUniverseDomain)
	}

	ss := buildShellSandboxStatefulSet(agent, "sandbox:test", "", "keys-hash")
	ssEnv := envMap(ss.Spec.Template.Spec.Containers[0].Env)
	if got := ssEnv[agentv1alpha1.GoogleCloudUniverseDomainEnv]; got != testCustomUniverseDomain {
		t.Fatalf("shell-sandbox %s = %q, want %q", agentv1alpha1.GoogleCloudUniverseDomainEnv, got, testCustomUniverseDomain)
	}

	fqdnPolicy := buildFQDNNetworkPolicy(agent)
	rawJSON, err := json.Marshal(fqdnPolicy.Object)
	if err != nil {
		t.Fatalf("marshal FQDNNetworkPolicy: %v", err)
	}
	var parsed struct {
		Spec struct {
			Egress []struct {
				Matches []struct {
					Pattern string `json:"pattern"`
				} `json:"matches"`
			} `json:"egress"`
		} `json:"spec"`
	}
	if err := json.Unmarshal(rawJSON, &parsed); err != nil {
		t.Fatalf("unmarshal FQDNNetworkPolicy: %v", err)
	}
	var fqdnPatterns []string
	for _, rule := range parsed.Spec.Egress {
		for _, m := range rule.Matches {
			fqdnPatterns = append(fqdnPatterns, m.Pattern)
		}
	}
	matchesAny := func(host string) bool {
		for _, p := range fqdnPatterns {
			if fqdnPatternToRegexp(t, p).MatchString(host) {
				return true
			}
		}
		return false
	}
	for _, wantAllowed := range []string{
		"container." + testCustomUniverseDomain,
		"sts." + testCustomUniverseDomain,
		"u-region-north1-docker.pkg." + testCustomUniverseDomain,
		"gke-0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b-123456789012.u-region-north1.gke." + testCustomUniverseDomain,
	} {
		if !matchesAny(wantAllowed) {
			t.Fatalf("FQDN NetworkPolicy patterns %v do not match %q", fqdnPatterns, wantAllowed)
		}
	}
	for _, commercialHost := range []string{
		"container.googleapis.com",
		"accounts.google.com",
		"gke-0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b-123456789012.us-central1.gke.goog",
		"us-central1-docker.pkg.dev",
	} {
		if matchesAny(commercialHost) {
			t.Fatalf("FQDN NetworkPolicy patterns %v leaked commercial host %q in custom universe", fqdnPatterns, commercialHost)
		}
	}

	apisAgent := agent.DeepCopy()
	apisAgent.Spec.Harness.UniverseDomain = "apis-" + testCustomUniverseDomain
	apisPolicy := buildFQDNNetworkPolicy(apisAgent)
	apisRawJSON, err := json.Marshal(apisPolicy.Object)
	if err != nil {
		t.Fatalf("marshal apis- FQDNNetworkPolicy: %v", err)
	}
	if err := json.Unmarshal(apisRawJSON, &parsed); err != nil {
		t.Fatalf("unmarshal apis- FQDNNetworkPolicy: %v", err)
	}
	var apisPatterns []string
	for _, rule := range parsed.Spec.Egress {
		for _, m := range rule.Matches {
			apisPatterns = append(apisPatterns, m.Pattern)
		}
	}
	matchesApisAny := func(host string) bool {
		for _, p := range apisPatterns {
			if fqdnPatternToRegexp(t, p).MatchString(host) {
				return true
			}
		}
		return false
	}
	for _, wantApisAllowed := range []string{
		"container.apis-" + testCustomUniverseDomain,
		"pkg-" + testCustomUniverseDomain,
		"u-region-north1-docker.pkg-" + testCustomUniverseDomain,
		"gke-0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b-123456789012.u-region-north1.gke-" + testCustomUniverseDomain,
	} {
		if !matchesApisAny(wantApisAllowed) {
			t.Fatalf("apis- FQDN NetworkPolicy patterns %v do not match %q", apisPatterns, wantApisAllowed)
		}
	}
}

func TestExternalModelEgressConditionLifecycle(t *testing.T) {
	now := metav1.Now()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: "kubeagents-system", Generation: 3},
	}

	// Custom universe + external providers (gemini, sap, bedrock) set ExternalModelEgress=True.
	for _, provider := range []string{"gemini", "sap", "bedrock"} {
		msg := externalModelEgressMessage(testCustomUniverseDomain, &agentv1alpha1.InferenceStatus{Provider: provider})
		if msg == "" {
			t.Fatalf("expected non-empty ExternalModelEgress message for %s in custom universe", provider)
		}
		setExternalModelEgressCondition(agent, msg, now)
		cond := meta.FindStatusCondition(agent.Status.Conditions, externalModelEgressConditionType)
		if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != externalModelEgressReason {
			t.Fatalf("unexpected ExternalModelEgress condition for %s: %#v", provider, cond)
		}
	}

	// Switching to openai_compatible or vertex_ai clears ExternalModelEgress.
	for _, inUniverse := range []string{"openai_compatible", "vertex_ai"} {
		clearMsg := externalModelEgressMessage(testCustomUniverseDomain, &agentv1alpha1.InferenceStatus{Provider: inUniverse})
		if clearMsg != "" {
			t.Fatalf("expected empty message for %s, got %q", inUniverse, clearMsg)
		}
		setExternalModelEgressCondition(agent, clearMsg, now)
		if meta.FindStatusCondition(agent.Status.Conditions, externalModelEgressConditionType) != nil {
			t.Fatalf("expected ExternalModelEgress condition to be removed for %s", inUniverse)
		}
	}

	// Default googleapis.com never sets ExternalModelEgress even with gemini.
	if got := externalModelEgressMessage(agentv1alpha1.DefaultUniverseDomain, &agentv1alpha1.InferenceStatus{Provider: "gemini"}); got != "" {
		t.Fatalf("expected empty message in googleapis.com, got %q", got)
	}
}

func TestA2APodSpecsPropagateImagePullSecrets(t *testing.T) {
	refs := []corev1.LocalObjectReference{{Name: "mirror-pull"}, {Name: "extra-pull"}}
	want := []string{"mirror-pull", "extra-pull"}
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: "kubeagents-system"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Deployment: &agentv1alpha1.DeploymentSpec{ImagePullSecrets: refs},
		},
	}

	names := func(got []corev1.LocalObjectReference) []string {
		out := make([]string, len(got))
		for i, r := range got {
			out[i] = r.Name
		}
		return out
	}

	cases := []struct {
		name string
		spec corev1.PodSpec
	}{
		{"a2a-callout", buildA2ACalloutDeployment(agent, "pubkey", 1).Spec.Template.Spec},
		{"a2a-console", buildA2AConsoleDeployment(agent).Spec.Template.Spec},
		{"a2a-dispatcher", buildA2ADispatcherDeployment(agent).Spec.Template.Spec},
		{"a2a-nats", buildA2ANATSStatefulSet(agent, "confhash").Spec.Template.Spec},
		{"a2a-provision", buildA2AProvisionJob(agent).Spec.Template.Spec},
		{"a2a-gateway", buildA2AGatewayDeployment(agent).Spec.Template.Spec},
		{"a2a-mcp", buildA2AMCPDeployment(agent).Spec.Template.Spec},
		{"a2a-verifier", buildA2AVerifierDeployment(agent).Spec.Template.Spec},
	}
	for _, tc := range cases {
		if got := names(tc.spec.ImagePullSecrets); !slices.Equal(got, want) {
			t.Fatalf("%s ImagePullSecrets = %v, want %v", tc.name, got, want)
		}
	}

	gwDep := buildA2AGatewayDeployment(agent)
	var gwPullEnv string
	for _, ev := range gwDep.Spec.Template.Spec.Containers[0].Env {
		if ev.Name == a2aImagePullRefsEnvVar {
			gwPullEnv = ev.Value
		}
	}
	if gwPullEnv != "mirror-pull,extra-pull" {
		t.Fatalf("a2a-gateway %s = %q, want %q", a2aImagePullRefsEnvVar, gwPullEnv, "mirror-pull,extra-pull")
	}

	// Also verify validateUniverseDomain triggers transient retry on cold start when spec.harness.universeDomain is omitted.
	rec := &PlatformAgentReconciler{
		UniverseDomainDetector: func(context.Context) (string, bool) { return "", false },
	}
	coldAgent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: "kubeagents-system"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{ProjectID: "sample-project", Location: "us-central1", ClusterName: "host-a"},
		},
	}
	if _, _, transient := rec.validateUniverseDomain(context.Background(), coldAgent); !transient {
		t.Fatal("expected transient=true on cold start when metadata probe fails and spec.harness.universeDomain is empty")
	}

	// Guardrail copy on transient cold start skips FQDN reconciliation when status is empty.
	if _, skipFQDN := rec.guardrailAgentForUniverse(context.Background(), coldAgent); !skipFQDN {
		t.Fatal("expected skipFQDN=true when metadata probe is transient and status.resolvedUniverseDomain is empty")
	}

	// Guardrail copy on UniverseDomainMismatch or malformed spec clears rejected spec and uses host universe.
	mismatchRec := &PlatformAgentReconciler{
		UniverseDomainDetector: func(context.Context) (string, bool) { return testCustomUniverseDomain, true },
	}
	for _, badSpec := range []string{agentv1alpha1.DefaultUniverseDomain, "https://bad/domain"} {
		badAgent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:        "platform-agent",
				Namespace:   "kubeagents-system",
				Annotations: map[string]string{"kubeagents.x-k8s.io/enable-fqdn-network-policy": "true"},
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Harness: &agentv1alpha1.HarnessSpec{
					ProjectID:      "sample-project",
					Location:       "us-central1",
					ClusterName:    "host-a",
					UniverseDomain: badSpec,
				},
			},
		}
		safeCopy, skipFQDN := mismatchRec.guardrailAgentForUniverse(context.Background(), badAgent)
		if skipFQDN {
			t.Fatalf("expected skipFQDN=false when host metadata answered for badSpec=%q", badSpec)
		}
		if got := effectiveUniverseDomain(safeCopy); got != testCustomUniverseDomain {
			t.Fatalf("guardrail effectiveUniverseDomain(%q) = %q, want %q", badSpec, got, testCustomUniverseDomain)
		}
	}

	// Reserved env checks cover KUBE_PROXY_UNIVERSE_DOMAIN, CLOUDSDK_API_ENDPOINT_OVERRIDES_*, and AgentPlugin spec.env.
	for _, envName := range []string{
		agentv1alpha1.GoogleCloudUniverseDomainEnv,
		agentv1alpha1.CloudSDKUniverseDomainEnv,
		agentv1alpha1.KubeProxyUniverseDomainEnv,
		"CLOUDSDK_API_ENDPOINT_OVERRIDES_CONTAINER",
	} {
		if !agentv1alpha1.IsSensitiveEnvVar(envName) {
			t.Fatalf("expected IsSensitiveEnvVar(%q) = true", envName)
		}
	}
	pluginWithSpoof := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "spoofplugin", Namespace: "kubeagents-system"},
		Spec: agentv1alpha1.AgentPluginSpec{
			Env: []corev1.EnvVar{
				{Name: agentv1alpha1.GoogleCloudUniverseDomainEnv, Value: "evil.example.test"},
				{Name: agentv1alpha1.CloudSDKUniverseDomainEnv, Value: "evil.example.test"},
				{Name: agentv1alpha1.KubeProxyUniverseDomainEnv, Value: "evil.example.test"},
				{Name: "CLOUDSDK_API_ENDPOINT_OVERRIDES_STORAGE", Value: "https://evil.example.test/"},
				{Name: "PLUGIN_CUSTOM_VAR", Value: "kept"},
			},
		},
	}
	podTpl := buildPodTemplateSpec(coldAgent, "cmhash", "fbhash", "sethash", "polhash", []*agentv1alpha1.AgentPlugin{pluginWithSpoof}, renderOptions{})
	for _, c := range podTpl.Spec.Containers {
		if c.Name != "platform-agent" {
			continue
		}
		var sawCustom bool
		for _, ev := range c.Env {
			if ev.Name == agentv1alpha1.GoogleCloudUniverseDomainEnv || ev.Name == agentv1alpha1.CloudSDKUniverseDomainEnv ||
				ev.Name == agentv1alpha1.KubeProxyUniverseDomainEnv || strings.HasPrefix(ev.Name, agentv1alpha1.CloudSDKEndpointOverridesEnvPrefix) {
				t.Fatalf("platform-agent container leaked reserved plugin env %s=%q", ev.Name, ev.Value)
			}
			if ev.Name == "PLUGIN_CUSTOM_VAR" && ev.Value == "kept" {
				sawCustom = true
			}
		}
		if !sawCustom {
			t.Fatal("expected non-reserved PLUGIN_CUSTOM_VAR from AgentPlugin spec.env to be preserved")
		}
	}
}

func TestSkipFQDNReconcileFailsClosedOnNotFound(t *testing.T) {
	scheme := setupScheme()
	fqdnAgent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "platform-agent",
			Namespace: "kubeagents-system",
			UID:       "uid-platform-agent",
			Annotations: map[string]string{
				AnnotationEnableFQDNNetworkPolicy: "true",
			},
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   "sample-project",
				Location:    "us-central1",
				ClusterName: "host-a",
			},
		},
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(fqdnAgent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	rec := &PlatformAgentReconciler{
		Client:      cl,
		APIReader:   cl,
		Scheme:      scheme,
		APIServerIP: "10.96.0.1",
	}
	if err := rec.reconcileNetworkPolicyWithFQDN(context.Background(), fqdnAgent, netpolProfile{Generated: true}, "", true, true); err != nil {
		t.Fatalf("reconcileNetworkPolicyWithFQDN(skipFQDNReconcile=true) unexpected error: %v", err)
	}
	gwNetpol := &networkingv1.NetworkPolicy{}
	if err := cl.Get(context.Background(), types.NamespacedName{Namespace: "kubeagents-system", Name: "platform-agent-gateway-netpol"}, gwNetpol); err != nil {
		t.Fatalf("expected gateway NetworkPolicy to be created: %v", err)
	}
	for _, rule := range gwNetpol.Spec.Egress {
		for _, peer := range rule.To {
			if peer.IPBlock != nil && (peer.IPBlock.CIDR == "0.0.0.0/0" || peer.IPBlock.CIDR == "::/0") {
				t.Fatalf("skipFQDNReconcile failed open with blanket egress CIDR %q when FQDNNetworkPolicy was NotFound", peer.IPBlock.CIDR)
			}
		}
	}

	// When CRD is absent (NoResourceMatchError), skipFQDNReconcile falls back to blanket egress.
	funcs := fakeServerSideApplyInterceptors()
	funcs.List = func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
		if list.GetObjectKind().GroupVersionKind().Group == "networking.gke.io" {
			return &meta.NoResourceMatchError{
				PartialResource: schema.GroupVersionResource{Group: "networking.gke.io", Version: "v1alpha1", Resource: "fqdnnetworkpolicies"},
			}
		}
		return c.List(ctx, list, opts...)
	}
	noCRDClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(fqdnAgent).
		WithInterceptorFuncs(funcs).
		Build()
	noCRDRec := &PlatformAgentReconciler{
		Client:      noCRDClient,
		APIReader:   noCRDClient,
		Scheme:      scheme,
		APIServerIP: "10.96.0.1",
	}
	if err := noCRDRec.reconcileNetworkPolicyWithFQDN(context.Background(), fqdnAgent, netpolProfile{Generated: true}, "", true, true); err != nil {
		t.Fatalf("reconcileNetworkPolicyWithFQDN(no CRD) unexpected error: %v", err)
	}
	noCRDNetpol := &networkingv1.NetworkPolicy{}
	if err := noCRDClient.Get(context.Background(), types.NamespacedName{Namespace: "kubeagents-system", Name: "platform-agent-gateway-netpol"}, noCRDNetpol); err != nil {
		t.Fatalf("expected gateway NetworkPolicy when CRD absent: %v", err)
	}
	var sawBlanket bool
	for _, rule := range noCRDNetpol.Spec.Egress {
		for _, peer := range rule.To {
			if peer.IPBlock != nil && peer.IPBlock.CIDR == "0.0.0.0/0" {
				sawBlanket = true
			}
		}
	}
	if !sawBlanket {
		t.Fatal("expected blanket 0.0.0.0/0 egress fallback when FQDNNetworkPolicy CRD is not installed")
	}
}
