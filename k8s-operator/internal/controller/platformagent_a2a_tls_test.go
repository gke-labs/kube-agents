package controller

import (
	"context"
	"crypto/x509"
	"slices"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func a2aTLSTestReconciler(t *testing.T, agent *agentv1alpha1.PlatformAgent) (*PlatformAgentReconciler, client.Client) {
	t.Helper()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent.DeepCopy()).
		WithInterceptorFuncs(interceptor.Funcs{Patch: fakeServerSideApplyInterceptors().Patch}).
		Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl
}

func a2aTLSTestClock(t *testing.T, at time.Time) {
	t.Helper()
	prev := a2aNATSTLSNow
	a2aNATSTLSNow = func() time.Time { return at }
	t.Cleanup(func() { a2aNATSTLSNow = prev })
}

func a2aGetSecret(t *testing.T, cl client.Client, agent *agentv1alpha1.PlatformAgent, name string) *corev1.Secret {
	t.Helper()
	s := &corev1.Secret{}
	if err := cl.Get(context.Background(), types.NamespacedName{Name: name, Namespace: agent.Namespace}, s); err != nil {
		t.Fatalf("get Secret %s: %v", name, err)
	}
	return s
}

// The first pass issues a CA and a serving certificate that verifies against
// it for the NATS Service's names and nothing else, and publishes the CA's
// public half alone. The second pass changes nothing.
func TestEnsureA2ANATSTLSIssuesOnceAndKeeps(t *testing.T) {
	agent := a2aTestAgent()
	r, cl := a2aTLSTestReconciler(t, agent)
	now := time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)
	a2aTLSTestClock(t, now)
	ctx := context.Background()

	first, err := r.ensureA2ANATSTLS(ctx, agent)
	if err != nil {
		t.Fatal(err)
	}
	ca := a2aGetSecret(t, cl, agent, agentv1alpha1.A2ANATSCASecretName(agent.Name))
	serving := a2aGetSecret(t, cl, agent, agentv1alpha1.A2ANATSTLSSecretName(agent.Name))
	if serving.Type != corev1.SecretTypeTLS {
		t.Errorf("serving Secret type = %q, want %q", serving.Type, corev1.SecretTypeTLS)
	}

	caCert, err := parseA2ACert(ca.Data[a2aNATSCACertKey])
	if err != nil || !caCert.IsCA {
		t.Fatalf("CA certificate: %v (IsCA %v)", err, caCert != nil && caCert.IsCA)
	}
	cert, err := parseA2ACert(serving.Data[a2aNATSTLSCertKey])
	if err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(caCert)
	for _, name := range []string{
		"test-agent-a2a-nats",
		"test-agent-a2a-nats.test-ns",
		"test-agent-a2a-nats.test-ns.svc",
		"test-agent-a2a-nats.test-ns.svc.cluster.local",
	} {
		if _, err := cert.Verify(x509.VerifyOptions{DNSName: name, Roots: pool, CurrentTime: now, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}); err != nil {
			t.Errorf("the serving certificate does not verify for %s: %v", name, err)
		}
	}
	for _, name := range []string{"localhost", "attacker.example"} {
		if _, err := cert.Verify(x509.VerifyOptions{DNSName: name, Roots: pool, CurrentTime: now}); err == nil {
			t.Errorf("the serving certificate verifies for %s, which nothing should dial it by", name)
		}
	}
	if len(cert.IPAddresses) != 0 {
		t.Errorf("the serving certificate carries IP SANs %v", cert.IPAddresses)
	}

	bundle := &corev1.ConfigMap{}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aNATSCABundleName(agent), Namespace: agent.Namespace}, bundle); err != nil {
		t.Fatalf("CA bundle: %v", err)
	}
	if got := bundle.Data[a2aNATSCACertKey]; got != string(ca.Data[a2aNATSCACertKey]) {
		t.Error("the bundle is not the CA's public certificate")
	}
	for _, v := range bundle.Data {
		if strings.Contains(v, "PRIVATE KEY") {
			t.Fatal("the bundle ConfigMap carries a private key")
		}
	}

	second, err := r.ensureA2ANATSTLS(ctx, agent)
	if err != nil {
		t.Fatal(err)
	}
	if second.CAFingerprint != first.CAFingerprint || second.ServingFingerprint != first.ServingFingerprint || string(second.CAPEM) != string(first.CAPEM) {
		t.Errorf("a second pass changed the material: %+v -> %+v", first, second)
	}
	if again := a2aGetSecret(t, cl, agent, agentv1alpha1.A2ANATSTLSSecretName(agent.Name)); string(again.Data[a2aNATSTLSCertKey]) != string(serving.Data[a2aNATSTLSCertKey]) {
		t.Error("a second pass reissued the serving certificate")
	}
}

// Inside the renewal window the serving certificate is reissued under the
// same CA, so clients keep trusting it and the rollout hash moves.
func TestEnsureA2ANATSTLSRenewsTheServingCertUnderTheSameCA(t *testing.T) {
	agent := a2aTestAgent()
	r, _ := a2aTLSTestReconciler(t, agent)
	start := time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)
	a2aTLSTestClock(t, start)
	first, err := r.ensureA2ANATSTLS(context.Background(), agent)
	if err != nil {
		t.Fatal(err)
	}

	a2aTLSTestClock(t, start.Add(a2aNATSTLSLifetime-a2aNATSTLSRenewBefore-time.Hour))
	early, err := r.ensureA2ANATSTLS(context.Background(), agent)
	if err != nil {
		t.Fatal(err)
	}
	if early.ServingFingerprint != first.ServingFingerprint {
		t.Error("the serving certificate was reissued before its renewal window")
	}

	a2aTLSTestClock(t, start.Add(a2aNATSTLSLifetime-a2aNATSTLSRenewBefore+time.Hour))
	renewed, err := r.ensureA2ANATSTLS(context.Background(), agent)
	if err != nil {
		t.Fatal(err)
	}
	if renewed.ServingFingerprint == first.ServingFingerprint {
		t.Error("the serving certificate was not reissued inside its renewal window")
	}
	if renewed.CAFingerprint != first.CAFingerprint {
		t.Error("a serving renewal replaced the CA, which every client would then distrust")
	}
}

// A CA that no longer verifies, or a serving pair edited by hand, is replaced
// whole: the reconcile never completes half a keypair.
func TestEnsureA2ANATSTLSReplacesWhatItCannotUse(t *testing.T) {
	agent := a2aTestAgent()
	r, cl := a2aTLSTestReconciler(t, agent)
	a2aTLSTestClock(t, time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC))
	ctx := context.Background()
	first, err := r.ensureA2ANATSTLS(ctx, agent)
	if err != nil {
		t.Fatal(err)
	}

	serving := a2aGetSecret(t, cl, agent, agentv1alpha1.A2ANATSTLSSecretName(agent.Name))
	serving.Data[a2aNATSTLSKeyKey] = []byte("not a key")
	if err := cl.Update(ctx, serving); err != nil {
		t.Fatal(err)
	}
	repaired, err := r.ensureA2ANATSTLS(ctx, agent)
	if err != nil {
		t.Fatal(err)
	}
	if repaired.ServingFingerprint == first.ServingFingerprint || repaired.CAFingerprint != first.CAFingerprint {
		t.Errorf("a broken serving key: got %+v from %+v, want a new serving cert under the same CA", repaired, first)
	}

	ca := a2aGetSecret(t, cl, agent, agentv1alpha1.A2ANATSCASecretName(agent.Name))
	ca.Data[a2aNATSCACertKey] = []byte("not a certificate")
	if err := cl.Update(ctx, ca); err != nil {
		t.Fatal(err)
	}
	reissued, err := r.ensureA2ANATSTLS(ctx, agent)
	if err != nil {
		t.Fatal(err)
	}
	if reissued.CAFingerprint == repaired.CAFingerprint || reissued.ServingFingerprint == repaired.ServingFingerprint {
		t.Errorf("a broken CA: got %+v from %+v, want a new CA and a new serving cert", reissued, repaired)
	}
}

// Every client the operator renders dials tls:// and trusts the bundle: the
// variable naming the file, and the ConfigMap mounted where it says. A client
// missing either cannot verify the bus and connects to nothing.
func TestEveryRenderedBusClientTrustsTheBundle(t *testing.T) {
	agent := a2aTestAgent()
	type podSpec struct {
		name      string
		spec      corev1.PodSpec
		container string
	}
	gateway := buildA2AGatewayDeployment(agent)
	verifier := buildA2AVerifierDeployment(agent)
	callout := buildA2ACalloutDeployment(agent)
	job := buildA2AProvisionJob(agent)
	agentPod := buildPodTemplateSpec(agent, "", "", "", "", nil, renderOptions{})
	for _, tc := range []podSpec{
		{"gateway", gateway.Spec.Template.Spec, ""},
		{"verifier", verifier.Spec.Template.Spec, ""},
		{"callout", callout.Spec.Template.Spec, ""},
		{"agent", agentPod.Spec, "platform-agent"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := tc.spec.Containers[0]
			if tc.container != "" {
				for _, cc := range tc.spec.Containers {
					if cc.Name == tc.container {
						c = cc
					}
				}
			}
			env := map[string]string{}
			for _, e := range c.Env {
				env[e.Name] = e.Value
			}
			if url := env["NATS_URL"]; !strings.HasPrefix(url, "tls://") {
				t.Errorf("NATS_URL = %q, want tls://", url)
			}
			if got := env[a2aNATSCAFileEnv]; got != a2aNATSCAFile() {
				t.Errorf("%s = %q, want %q", a2aNATSCAFileEnv, got, a2aNATSCAFile())
			}
			if !slices.ContainsFunc(c.VolumeMounts, func(m corev1.VolumeMount) bool {
				return m.Name == a2aNATSCABundleVolumeName && m.MountPath == a2aNATSCABundleMountDir && m.ReadOnly
			}) {
				t.Errorf("no read-only bundle mount at %s: %v", a2aNATSCABundleMountDir, c.VolumeMounts)
			}
			if !slices.ContainsFunc(tc.spec.Volumes, func(v corev1.Volume) bool {
				return v.Name == a2aNATSCABundleVolumeName && v.ConfigMap != nil && v.ConfigMap.Name == a2aNATSCABundleName(agent)
			}) {
				t.Errorf("no bundle volume from %s", a2aNATSCABundleName(agent))
			}
		})
	}

	// The provision Job speaks through the nats CLI, so its trust is flags.
	script := strings.Join(job.Spec.Template.Spec.Containers[0].Command, " ")
	if !strings.Contains(script, "--server tls://") || !strings.Contains(script, "--tlsca "+a2aNATSCAFile()) {
		t.Error("the provision Job's nats CLI does not dial tls:// with the bundle as --tlsca")
	}
	if !slices.ContainsFunc(job.Spec.Template.Spec.Volumes, func(v corev1.Volume) bool {
		return v.Name == a2aNATSCABundleVolumeName && v.ConfigMap != nil
	}) {
		t.Error("the provision Job does not mount the bundle")
	}

	// The gateway names the ConfigMap too, so its session pods can mount it.
	var cm string
	for _, e := range gateway.Spec.Template.Spec.Containers[0].Env {
		if e.Name == a2aNATSCAConfigMapEnv {
			cm = e.Value
		}
	}
	if cm != a2aNATSCABundleName(agent) {
		t.Errorf("gateway %s = %q, want %q", a2aNATSCAConfigMapEnv, cm, a2aNATSCABundleName(agent))
	}
}

// The bus mounts the serving pair where nats.conf's tls block names it, and
// the block is there: TLS is required of the client listener.
func TestTheBusServesTLSFromTheMountedPair(t *testing.T) {
	agent := a2aTestAgent()
	conf := renderA2ANATSConf(agent, func(string) string { return "pw" }, &a2aCalloutKeys{IssuerPublic: "A", XKeyPublic: "X"})
	for _, want := range []string{
		"tls {",
		`cert_file: "` + a2aNATSTLSMountDir + "/" + a2aNATSTLSCertKey + `"`,
		`key_file: "` + a2aNATSTLSMountDir + "/" + a2aNATSTLSKeyKey + `"`,
	} {
		if !strings.Contains(conf, want) {
			t.Errorf("nats.conf lacks %q", want)
		}
	}
	if strings.Contains(conf, "verify: true") {
		t.Error("nats.conf requires client certificates; mTLS is not part of this render")
	}

	sts := buildA2ANATSStatefulSet(agent, "hash")
	c := sts.Spec.Template.Spec.Containers[0]
	if !slices.ContainsFunc(c.VolumeMounts, func(m corev1.VolumeMount) bool {
		return m.Name == a2aNATSTLSVolume && m.MountPath == a2aNATSTLSMountDir && m.ReadOnly && m.SubPath == ""
	}) {
		t.Errorf("the serving pair is not mounted read-only as a directory at %s: %v", a2aNATSTLSMountDir, c.VolumeMounts)
	}
	if !slices.ContainsFunc(sts.Spec.Template.Spec.Volumes, func(v corev1.Volume) bool {
		return v.Name == a2aNATSTLSVolume && v.Secret != nil && v.Secret.SecretName == agentv1alpha1.A2ANATSTLSSecretName(agent.Name)
	}) {
		t.Error("the serving pair's volume is not the TLS Secret")
	}
}
