package gateway

import (
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sfake "k8s.io/client-go/kubernetes/fake"
)

// The worker image bakes the session persona and skills into the harness's
// config directory (a2a/Dockerfile.worker, a2a/cmd/session-persona). They
// reach the harness only if the pod leaves that directory alone: a volume
// mounted at it or above it would hide them, and a HOME or CLAUDE_CONFIG_DIR
// in the pod env would point Claude Code somewhere else. Checked with the
// cluster view off and on, since the view adds mounts and env.
func TestSpawnedPodLeavesTheHarnessHomeToTheImage(t *testing.T) {
	const harnessHome = "/home/node/.claude"
	for _, view := range []bool{false, true} {
		cs := k8sfake.NewSimpleClientset()
		cfg := &Config{Namespace: "test-ns", WorkerImage: "img", SessionServiceAccount: "agent-a2a-session",
			TaskDeadline: 15 * time.Minute, NATSURL: "nats://bus:4222", SessionClusterView: view,
			CredentialProxyURL: "http://agent-credential-proxy.test-ns.svc.cluster.local:8765"}
		s := &podSpawner{cfg: cfg, client: cs, log: slog.Default()}
		rec := &SessionRecord{Key: "discord:g1/t", ContextID: "ctx-h", BusSession: "chat-otter-h0me", Addressee: "chat-otter-h0me"}
		if _, err := s.Spawn(context.Background(), rec, "task-h", "", 1); err != nil {
			t.Fatal(err)
		}
		pod, err := cs.CoreV1().Pods("test-ns").Get(context.Background(), "chat-otter-h0me", metav1.GetOptions{})
		if err != nil {
			t.Fatal(err)
		}
		c := pod.Spec.Containers[0]
		if len(c.VolumeMounts) == 0 {
			t.Fatalf("view=%v: no volume mounts at all; the check below would pass on nothing", view)
		}
		for _, m := range c.VolumeMounts {
			p := strings.TrimSuffix(m.MountPath, "/")
			if p == "" || harnessHome == p || strings.HasPrefix(harnessHome, p+"/") || strings.HasPrefix(p, harnessHome+"/") {
				t.Errorf("view=%v: %q is mounted at %s, over the image's %s", view, m.Name, m.MountPath, harnessHome)
			}
		}
		for _, e := range c.Env {
			if e.Name == "HOME" || e.Name == "CLAUDE_CONFIG_DIR" {
				t.Errorf("view=%v: the pod sets %s=%q, overriding the image's harness home", view, e.Name, e.Value)
			}
		}
	}
}
