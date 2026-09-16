package gateway

import (
	"context"
	"log/slog"
	"path"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	k8sfake "k8s.io/client-go/kubernetes/fake"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestSpawnSetsPodDeadlineMirroringTheAdapters: the pod-level
// activeDeadlineSeconds the adapter's contract assumes — sized above the
// adapter's own deadline by the fixed grace, with the adapter's half
// rendered from the same number so the two layers cannot drift.
func TestSpawnSetsPodDeadlineMirroringTheAdapters(t *testing.T) {
	cs := k8sfake.NewSimpleClientset()
	cfg := &Config{Namespace: "test-ns", WorkerImage: "img", SessionServiceAccount: "agent-a2a-session",
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
	cfg := &Config{Namespace: "test-ns", WorkerImage: "img", SessionServiceAccount: "agent-a2a-session",
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

// The session pod's credential shape — the pin for gke-labs#1270.
//
// The issue is that the bus password arrives as an environment variable, and
// the model harness the adapter launches is its child at the same UID in the
// same PID namespace, so it can read the adapter's environment through
// /proc/1/environ no matter how carefully the adapter builds the child's env.
// Withholding it from the child was never the fix; not having it in the
// environment at all is. This asserts both halves: what is gone, and what
// replaced it.
func TestSpawnedSessionsCarryNoBusPasswordAndAPodBoundTokenInstead(t *testing.T) {
	cs := k8sfake.NewSimpleClientset()
	cfg := &Config{Namespace: "test-ns", WorkerImage: "img", SessionServiceAccount: "agent-a2a-session",
		TaskDeadline: 15 * time.Minute, NATSURL: "nats://bus:4222"}
	s := &podSpawner{cfg: cfg, client: cs, log: slog.Default()}

	rec := &SessionRecord{Key: "discord:g1/t", ContextID: "ctx-9", BusSession: "chat-otter-1a2b", Addressee: "chat-otter-1a2b"}
	if _, err := s.Spawn(context.Background(), rec, "task-9", ""); err != nil {
		t.Fatal(err)
	}
	pod, err := cs.CoreV1().Pods("test-ns").Get(context.Background(), "chat-otter-1a2b", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	c := pod.Spec.Containers[0]

	// Nothing in the environment carries a credential, by value or by
	// reference. Checked as "no env var reads a Secret" rather than as "no
	// env var is named NATS_PASSWORD", because the hole is the delivery
	// mechanism and renaming the variable would not close it.
	for _, e := range c.Env {
		if e.Name == "NATS_PASSWORD" || e.Name == "NATS_USER" {
			t.Errorf("session pod still carries %s; that is the /proc/1/environ read gke-labs#1270 is about", e.Name)
		}
		if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil {
			t.Errorf("env %s reads Secret %s/%s; a session credential must not arrive in the environment at all",
				e.Name, e.ValueFrom.SecretKeyRef.Name, e.ValueFrom.SecretKeyRef.Key)
		}
	}

	// The replacement: a projected token, audience-bound and pod-bound,
	// mounted at the path the adapter reads.
	if pod.Spec.ServiceAccountName != "agent-a2a-session" {
		t.Errorf("serviceAccountName = %q, want the session account the callout's map is keyed on", pod.Spec.ServiceAccountName)
	}
	// A second, default-audience token would be an API-server credential
	// sitting next to the bus one, which is what this pod is least entitled
	// to hold.
	if pod.Spec.AutomountServiceAccountToken == nil || *pod.Spec.AutomountServiceAccountToken {
		t.Error("automountServiceAccountToken is not false; the pod would get a default-audience token as well")
	}

	var proj *corev1.ServiceAccountTokenProjection
	for _, v := range pod.Spec.Volumes {
		if v.Projected == nil {
			continue
		}
		for _, src := range v.Projected.Sources {
			if src.ServiceAccountToken != nil {
				proj = src.ServiceAccountToken
			}
		}
	}
	if proj == nil {
		t.Fatal("no projected ServiceAccount token on the session pod; it has no way to authenticate to the bus")
	}
	if proj.Audience != lib.BusTokenAudience {
		t.Errorf("token audience = %q, want %q; the wrong audience is refused by the callout, and an EMPTY one is the cluster-wide credential the binding exists to prevent",
			proj.Audience, lib.BusTokenAudience)
	}
	if proj.ExpirationSeconds == nil || *proj.ExpirationSeconds != busTokenExpirationSeconds {
		t.Errorf("token expirationSeconds = %v, want %d", proj.ExpirationSeconds, busTokenExpirationSeconds)
	}

	// Mounted where the adapter looks. A mismatch here fails as "no token
	// file" at connect, which reads like a cluster problem rather than a
	// two-constant disagreement, so it is pinned against lib's own path.
	var mount *corev1.VolumeMount
	for i, m := range c.VolumeMounts {
		if m.MountPath == path.Dir(lib.BusTokenPath) {
			mount = &c.VolumeMounts[i]
		}
	}
	if mount == nil {
		t.Fatalf("no volume mounted at %s; the adapter would find no token file", path.Dir(lib.BusTokenPath))
	}
	if !mount.ReadOnly {
		t.Error("the token mount is writable")
	}

	// And the pod is told its own name by the kubelet, which is what the
	// adapter pins its inbox prefix from.
	var podNameEnv *corev1.EnvVar
	for i, e := range c.Env {
		if e.Name == lib.EnvPodName {
			podNameEnv = &c.Env[i]
		}
	}
	if podNameEnv == nil {
		t.Fatalf("no %s on the session pod; the adapter cannot pin the inbox prefix the callout grants it", lib.EnvPodName)
	}
	if podNameEnv.Value != "" {
		t.Errorf("%s is set to a literal %q; it must come from the downward API, so it is the name the API server will attest rather than the name we believe we used",
			lib.EnvPodName, podNameEnv.Value)
	}
	if podNameEnv.ValueFrom == nil || podNameEnv.ValueFrom.FieldRef == nil ||
		podNameEnv.ValueFrom.FieldRef.FieldPath != "metadata.name" {
		t.Errorf("%s does not come from metadata.name: %+v", lib.EnvPodName, podNameEnv.ValueFrom)
	}
}

// mintSessionName is load-bearing for the callout in a way it was not before,
// and it had no test.
//
// The minted name becomes the pod name, and the pod name is what the API server
// attests and the callout turns into subjects, a consumer name and an inbox
// prefix. A name with a dot in it is a legal pod name and three subject tokens;
// one over 63 characters is refused by the API server at create. The callout
// refuses a bad name rather than issuing wrong grants, so the failure would be
// "every session refused at connect" — pinned at the mint instead.
func TestMintedSessionNamesAreOneSubjectTokenAndALegalPodName(t *testing.T) {
	seen := map[string]bool{}
	for _, profile := range []string{"chat", "platform", "a", "some-longer-profile-name"} {
		for i := 0; i < 200; i++ {
			name := mintSessionName(profile)
			if !lib.ValidSubjectToken(name) {
				t.Fatalf("minted %q, which is not a single dot-free DNS-1123 label; the callout refuses it and the session never connects", name)
			}
			if len(name) > 63 {
				t.Fatalf("minted %q (%d chars); the API server refuses a pod name over 63", name, len(name))
			}
			if !strings.HasPrefix(name, profile+"-") {
				t.Fatalf("minted %q, which does not name its profile", name)
			}
			seen[name] = true
		}
	}
	// Not a collision bound, just a check that the suffix varies at all: a
	// mint that returned one name per profile would make every respawn an
	// AlreadyExists against a terminating predecessor.
	if len(seen) < 100 {
		t.Errorf("only %d distinct names from 800 mints; the suffix is not varying", len(seen))
	}
}
