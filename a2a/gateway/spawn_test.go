package gateway

import (
	"context"
	"log/slog"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	k8sfake "k8s.io/client-go/kubernetes/fake"
)

// TestSpawnSetsPodDeadlineMirroringTheAdapters: the pod-level
// activeDeadlineSeconds the adapter's contract assumes — sized above the
// adapter's own deadline by the fixed grace, with the adapter's half
// rendered from the same number so the two layers cannot drift.
func TestSpawnSetsPodDeadlineMirroringTheAdapters(t *testing.T) {
	cs := k8sfake.NewSimpleClientset()
	cfg := &Config{Namespace: "test-ns", WorkerImage: "img", NATSCredsSecret: "creds",
		TaskDeadline: 15 * time.Minute}
	s := &podSpawner{cfg: cfg, client: cs, log: slog.Default()}

	rec := &SessionRecord{Key: "discord:g1/t", ContextID: "ctx-1", BusSession: "chat-otter-abcd", Addressee: "chat-otter-abcd"}
	if _, err := s.Spawn(context.Background(), rec, "task-1", ""); err != nil {
		t.Fatal(err)
	}
	pod, err := cs.CoreV1().Pods("test-ns").Get(context.Background(), "chat-otter-abcd", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if pod.Spec.ActiveDeadlineSeconds == nil {
		t.Fatal("spawned pod has no activeDeadlineSeconds; a wedged adapter has no owner")
	}
	want := int64((15*time.Minute + podDeadlineGrace) / time.Second)
	if *pod.Spec.ActiveDeadlineSeconds != want {
		t.Fatalf("activeDeadlineSeconds = %d, want %d (deadline + grace)", *pod.Spec.ActiveDeadlineSeconds, want)
	}
	found := ""
	for _, e := range pod.Spec.Containers[0].Env {
		if e.Name == "A2A_TASK_DEADLINE_SECONDS" {
			found = e.Value
		}
	}
	if found != "900" {
		t.Fatalf("worker env A2A_TASK_DEADLINE_SECONDS = %q, want 900 (the same number the pod deadline is sized from)", found)
	}
}

// TestSpawnCarriesOwnerReference: spawned pods are owned by the gateway's
// Deployment, so Kubernetes GC reaps sessions when it goes — cleanupA2A or
// any other deletion — with no operator exception to IsControlledBy.
func TestSpawnCarriesOwnerReference(t *testing.T) {
	dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{
		Name: "agent-a2a-gateway", Namespace: "test-ns", UID: types.UID("uid-123"),
	}}
	cs := k8sfake.NewSimpleClientset(dep)
	cfg := &Config{Namespace: "test-ns", WorkerImage: "img", NATSCredsSecret: "creds",
		TaskDeadline: 30 * time.Minute, OwnerDeployment: "agent-a2a-gateway"}
	s := &podSpawner{cfg: cfg, client: cs, log: slog.Default()}
	if err := s.resolveOwner(context.Background()); err != nil {
		t.Fatal(err)
	}

	rec := &SessionRecord{Key: "discord:g1/t", ContextID: "ctx-2", BusSession: "chat-lynx-ef01", Addressee: "chat-lynx-ef01"}
	if _, err := s.Spawn(context.Background(), rec, "task-2", ""); err != nil {
		t.Fatal(err)
	}
	pod, err := cs.CoreV1().Pods("test-ns").Get(context.Background(), "chat-lynx-ef01", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(pod.OwnerReferences) != 1 {
		t.Fatalf("ownerReferences = %v, want exactly the gateway Deployment", pod.OwnerReferences)
	}
	or := pod.OwnerReferences[0]
	if or.Kind != "Deployment" || or.Name != "agent-a2a-gateway" || or.UID != types.UID("uid-123") ||
		or.Controller == nil || !*or.Controller {
		t.Fatalf("ownerReference = %+v", or)
	}
}

// TestResolveOwner: unset spawns unowned pods (playground); a configured
// owner that cannot be read refuses rather than quietly reopening the
// orphaned-session window.
func TestResolveOwner(t *testing.T) {
	cs := k8sfake.NewSimpleClientset()
	s := &podSpawner{cfg: &Config{Namespace: "test-ns"}, client: cs, log: slog.Default()}
	if err := s.resolveOwner(context.Background()); err != nil || s.owner != nil {
		t.Fatalf("unset owner: err=%v owner=%+v", err, s.owner)
	}

	s = &podSpawner{cfg: &Config{Namespace: "test-ns", OwnerDeployment: "missing"}, client: cs, log: slog.Default()}
	if err := s.resolveOwner(context.Background()); err == nil {
		t.Fatal("missing owner deployment accepted")
	}
}
