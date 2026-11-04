package gateway

import (
	"context"
	"encoding/json"
	"flag"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sfake "k8s.io/client-go/kubernetes/fake"
)

var updateSessionPods = flag.Bool("update-session-pods", false, "rewrite the session pod fixture the operator's admission envtest admits")

// The operator's envtest for the gateway's pod admission policy
// (k8s-operator/config/admission/a2a-gateway-pod-policy.yaml) admits every
// pod in this file. Generated here, from the real Spawn, so a spawn change the
// policy would refuse fails one of the two tests instead of every delegation
// on an install.
var sessionPodFixture = filepath.Join("..", "..", "k8s-operator", "internal", "controller", "testdata", "a2a-gateway-session-pods.json")

// TestSpawnedSessionPodsMatchTheAdmissionFixture spawns the session pod in
// each shape Spawn has (plain, with the bus CA, reused for its conversation,
// with the cluster view) under the namespace and account names the envtest
// uses, and compares them with the fixture. Run with -update-session-pods to
// rewrite it, then run the operator's TestTheGatewayPodPolicy envtest.
func TestSpawnedSessionPodsMatchTheAdmissionFixture(t *testing.T) {
	const ns = "a2a-pod-policy"
	base := Config{Namespace: ns, WorkerImage: "worker", SessionServiceAccount: "pa-a2a-session", TaskDeadline: 15 * time.Minute}
	shapes := []struct {
		name   string
		mutate func(*Config, *SessionRecord)
	}{
		{"plain", func(*Config, *SessionRecord) {}},
		{"bus-ca", func(c *Config, _ *SessionRecord) {
			c.NATSURL = "tls://pa-a2a-nats." + ns + ".svc:4222"
			c.NATSCAFile = "/etc/a2a/nats-ca/ca.crt"
			c.NATSCAConfigMap = "pa-a2a-nats-ca-bundle"
		}},
		{"reused", func(c *Config, rec *SessionRecord) {
			c.SessionReuse = true
			rec.SessionRouted = true
		}},
		{"cluster-view", func(c *Config, _ *SessionRecord) {
			c.SessionClusterView = true
			c.CredentialProxyURL = "http://pa-credential-proxy." + ns + ".svc.cluster.local:8765"
		}},
	}
	var pods []corev1.Pod
	for _, shape := range shapes {
		cfg := base
		rec := &SessionRecord{Key: "fixture:" + shape.name, ContextID: "ctx", BusSession: shape.name, Addressee: shape.name}
		shape.mutate(&cfg, rec)
		cs := k8sfake.NewSimpleClientset()
		s := &podSpawner{cfg: &cfg, client: cs, log: slog.Default()}
		if _, err := s.Spawn(context.Background(), rec, "task-1", "", 1); err != nil {
			t.Fatalf("%s: %v", shape.name, err)
		}
		pod, err := cs.CoreV1().Pods(ns).Get(context.Background(), shape.name, metav1.GetOptions{})
		if err != nil {
			t.Fatalf("%s: %v", shape.name, err)
		}
		pods = append(pods, corev1.Pod{
			TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Pod"},
			ObjectMeta: metav1.ObjectMeta{Name: pod.Name, Namespace: pod.Namespace, Labels: pod.Labels, Annotations: pod.Annotations},
			Spec:       pod.Spec,
		})
	}
	got, err := json.MarshalIndent(pods, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	got = append(got, '\n')
	if *updateSessionPods {
		if err := os.WriteFile(sessionPodFixture, got, 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(sessionPodFixture)
	if err != nil {
		t.Fatal(err)
	}
	if string(want) != string(got) {
		t.Fatalf("the session pods Spawn builds no longer match %s: rerun with -update-session-pods, then the operator's TestTheGatewayPodPolicy envtest, which admits them", sessionPodFixture)
	}
}
