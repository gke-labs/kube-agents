package gateway

import (
	"context"
	"log/slog"
	"os"
	"regexp"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sfake "k8s.io/client-go/kubernetes/fake"
)

// The session pod's requests and limits come from the four env vars the
// operator renders (#2371), each defaulting to spawn.go's value; an
// unparsable quantity or a request above its limit stops the gateway.
func TestTheGatewayReadsTheSessionPodsResources(t *testing.T) {
	for _, k := range []string{"A2A_SESSION_CPU_REQUEST", "A2A_SESSION_MEMORY_REQUEST", "A2A_SESSION_CPU_LIMIT", "A2A_SESSION_MEMORY_LIMIT"} {
		t.Setenv(k, "")
	}
	got, err := sessionResourcesFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		set  corev1.ResourceList
		name corev1.ResourceName
		want string
	}{
		{got.Requests, corev1.ResourceCPU, workerCPURequest}, {got.Requests, corev1.ResourceMemory, workerMemoryRequest},
		{got.Limits, corev1.ResourceCPU, workerCPULimit}, {got.Limits, corev1.ResourceMemory, workerMemoryLimit},
	} {
		if v := c.set[c.name]; v.Cmp(resource.MustParse(c.want)) != 0 {
			t.Errorf("unset: %s = %s, want %s", c.name, v.String(), c.want)
		}
	}

	t.Setenv("A2A_SESSION_MEMORY_REQUEST", "384Mi")
	t.Setenv("A2A_SESSION_CPU_LIMIT", "1")
	got, err = sessionResourcesFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if v := got.Requests[corev1.ResourceMemory]; v.Cmp(resource.MustParse("384Mi")) != 0 {
		t.Errorf("memory request %s, want 384Mi", v.String())
	}
	if v := got.Limits[corev1.ResourceCPU]; v.Cmp(resource.MustParse("1")) != 0 {
		t.Errorf("cpu limit %s, want 1", v.String())
	}

	for name, env := range map[string][2]string{
		"not a quantity":         {"A2A_SESSION_CPU_REQUEST", "lots"},
		"zero":                   {"A2A_SESSION_MEMORY_REQUEST", "0"},
		"request over its limit": {"A2A_SESSION_MEMORY_REQUEST", "3Gi"},
	} {
		t.Run(name, func(t *testing.T) {
			t.Setenv(env[0], env[1])
			if _, err := sessionResourcesFromEnv(); err == nil {
				t.Errorf("%s=%s was accepted", env[0], env[1])
			}
		})
	}
}

// A spawned session pod requests what the gateway was configured with, and
// falls back to the defaults for a Config built without them.
func TestASpawnedSessionPodRequestsTheConfiguredResources(t *testing.T) {
	spawn := func(cfg *Config) corev1.ResourceRequirements {
		t.Helper()
		cs := k8sfake.NewSimpleClientset()
		cfg.Namespace, cfg.WorkerImage, cfg.SessionServiceAccount = "test-ns", "img", "agent-a2a-session"
		cfg.TaskDeadline, cfg.NATSURL = 15*time.Minute, "nats://bus:4222"
		s := &podSpawner{cfg: cfg, client: cs, log: slog.Default()}
		rec := &SessionRecord{Key: "discord:g1/t", ContextID: "ctx-9", BusSession: "chat-otter-1a2b", Addressee: "chat-otter-1a2b"}
		if _, err := s.Spawn(context.Background(), rec, "task-9", "", 1); err != nil {
			t.Fatal(err)
		}
		pod, err := cs.CoreV1().Pods("test-ns").Get(context.Background(), "chat-otter-1a2b", metav1.GetOptions{})
		if err != nil {
			t.Fatal(err)
		}
		return pod.Spec.Containers[0].Resources
	}
	set := &Config{SessionResources: corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("200m"), corev1.ResourceMemory: resource.MustParse("384Mi")},
		Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("1Gi")},
	}}
	got := spawn(set)
	for _, c := range []struct {
		v    resource.Quantity
		want string
	}{
		{got.Requests[corev1.ResourceCPU], "200m"}, {got.Requests[corev1.ResourceMemory], "384Mi"},
		{got.Limits[corev1.ResourceCPU], "1"}, {got.Limits[corev1.ResourceMemory], "1Gi"},
		{got.Requests[corev1.ResourceEphemeralStorage], workerEphemeralStorageRequest},
	} {
		if c.v.Cmp(resource.MustParse(c.want)) != 0 {
			t.Errorf("configured pod: %s, want %s", c.v.String(), c.want)
		}
	}
	def := spawn(&Config{})
	if v := def.Requests[corev1.ResourceMemory]; v.Cmp(resource.MustParse(workerMemoryRequest)) != 0 {
		t.Errorf("unconfigured pod memory request %s, want the default %s", v.String(), workerMemoryRequest)
	}
}

// spawn.go's fallbacks are the operator's defaults, so a gateway the operator
// didn't render the env for (a hand run, an older operator) requests what an
// operator-rendered one would. The operator is another module, so this reads
// its constants from the source.
func TestSessionResourceDefaultsMatchTheOperators(t *testing.T) {
	src, err := os.ReadFile("../../k8s-operator/api/v1alpha1/session_resources.go")
	if err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{
		"DefaultSessionCPURequest":       workerCPURequest,
		"DefaultSessionMemoryRequest":    workerMemoryRequest,
		"DefaultSessionCPULimit":         workerCPULimit,
		"DefaultSessionMemoryLimit":      workerMemoryLimit,
		"DefaultSessionEphemeralStorage": workerEphemeralStorageRequest,
	} {
		m := regexp.MustCompile(name + `\s*=\s*"([^"]+)"`).FindStringSubmatch(string(src))
		if m == nil || strings.TrimSpace(m[1]) != want {
			t.Errorf("the operator's %s is %v; the gateway's fallback is %q", name, m, want)
		}
	}
}
