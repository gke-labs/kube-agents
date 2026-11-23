package gateway

import (
	"context"
	"log/slog"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sfake "k8s.io/client-go/kubernetes/fake"
)

// TestAnUnknownSessionHarnessKeepsTheGatewayServing: A2A_SESSION_HARNESS
// unset or claude-code is taken as is. Any other value (antigravity from a
// newer operator, a typo, a case variant, padding) still starts the gateway,
// which is shared by every conversation, on the default harness and records
// the value for main to log, rather than refusing to start and taking the
// whole chat surface down.
func TestAnUnknownSessionHarnessKeepsTheGatewayServing(t *testing.T) {
	setBaseEnv(t)
	for _, ok := range []string{"", "claude-code"} {
		t.Setenv(envSessionHarness, ok)
		cfg, err := FromEnv()
		if err != nil {
			t.Fatalf("%q refused: %v", ok, err)
		}
		if cfg.SessionHarness != ok || cfg.SessionHarnessRefused != "" {
			t.Errorf("%q: SessionHarness %q refused %q", ok, cfg.SessionHarness, cfg.SessionHarnessRefused)
		}
	}
	for _, bad := range []string{"antigravity", "claude", "Claude-Code", " claude-code"} {
		t.Setenv(envSessionHarness, bad)
		cfg, err := FromEnv()
		if err != nil {
			t.Fatalf("%q stopped the gateway: %v", bad, err)
		}
		if cfg.SessionHarness != "" || cfg.SessionHarnessRefused != bad {
			t.Errorf("%q: SessionHarness %q refused %q, want the default and the value recorded", bad, cfg.SessionHarness, cfg.SessionHarnessRefused)
		}
	}
}

// TestASessionPodCarriesTheHarnessOnlyWhenNamedAndTheWaiverEmptyAlways: a
// session pod gets A2A_HARNESS only when the gateway has a session harness,
// and A2A_HARNESS_UNISOLATED empty on every pod, so the image's ENV can't set
// the waiver.
func TestASessionPodCarriesTheHarnessOnlyWhenNamedAndTheWaiverEmptyAlways(t *testing.T) {
	const ns = "harness"
	for _, c := range []struct {
		harness string
		set     bool
	}{
		{"", false},
		{"claude-code", true},
		// A Config built without FromEnv is checked again at spawn.
		{"antigravity", false},
	} {
		cfg := Config{Namespace: ns, WorkerImage: "worker", SessionServiceAccount: "sa", TaskDeadline: 15 * time.Minute, SessionHarness: c.harness}
		cs := k8sfake.NewSimpleClientset()
		s := &podSpawner{cfg: &cfg, client: cs, log: slog.Default()}
		rec := &SessionRecord{Key: "k", ContextID: "ctx", BusSession: "pod", Addressee: "pod"}
		if _, err := s.Spawn(context.Background(), rec, "task-1", "", 1); err != nil {
			t.Fatal(err)
		}
		pod, err := cs.CoreV1().Pods(ns).Get(context.Background(), "pod", metav1.GetOptions{})
		if err != nil {
			t.Fatal(err)
		}
		env := map[string]corev1.EnvVar{}
		for _, e := range pod.Spec.Containers[0].Env {
			env[e.Name] = e
		}
		h, hasHarness := env[envHarness]
		want := ""
		if c.set {
			want = c.harness
		}
		if hasHarness != c.set || h.Value != want {
			t.Errorf("harness %q: A2A_HARNESS present=%v %q", c.harness, hasHarness, h.Value)
		}
		w, hasWaiver := env[envHarnessUnisolated]
		if !hasWaiver || w.Value != "" || w.ValueFrom != nil {
			t.Errorf("harness %q: A2A_HARNESS_UNISOLATED %+v present=%v, want present and empty", c.harness, w, hasWaiver)
		}
	}
}
