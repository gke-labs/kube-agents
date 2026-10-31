package dispatch

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nkeys"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	clientfeatures "k8s.io/client-go/features"
	clientfeaturestesting "k8s.io/client-go/features/testing"
	"k8s.io/client-go/kubernetes/fake"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func TestProcessConfigDefaultsAreValid(t *testing.T) {
	if err := DefaultConfig().Validate(); err != nil {
		t.Fatal(err)
	}
	c := DefaultConfig()
	c.Submitter = ""
	if c.Validate() == nil {
		t.Error("no submitter accepted")
	}
}

func rawConfig(t *testing.T, cfg lib.DispatchConfig) []byte {
	t.Helper()
	b, err := json.Marshal(cfg)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// Every loaded config is held to lib.DispatchConfig.Validate: a restart
// grace under two renewals, or a lease under three, is refused and the last
// good config keeps serving.
func TestConfigStoreRefusesUnsafeIntervalsAndKeepsTheLastGood(t *testing.T) {
	s := NewConfigStore(quietLog())
	if s.Ready() || len(s.Config().Profiles) != 0 {
		t.Fatal("a new store serves a config")
	}
	good := testDispatchConfig(6, testProfile("tse", 2))
	good.Version = "v1"
	if err := s.Update(rawConfig(t, good)); err != nil {
		t.Fatal(err)
	}
	for name, edit := range map[string]func(*lib.DispatchConfig){
		"grace under two renewals":   func(c *lib.DispatchConfig) { c.RenewSeconds, c.LeaseSeconds, c.RestartGraceSeconds = 20, 90, 39 },
		"lease under three renewals": func(c *lib.DispatchConfig) { c.RenewSeconds, c.LeaseSeconds, c.RestartGraceSeconds = 20, 59, 60 },
		"no namespace":               func(c *lib.DispatchConfig) { c.Namespace = "" },
	} {
		bad := good
		bad.Version = "bad"
		edit(&bad)
		if err := s.Update(rawConfig(t, bad)); err == nil {
			t.Errorf("%s: accepted", name)
		}
		if s.Config().Version != "v1" || s.LastError() == "" {
			t.Errorf("%s: serving %q, last error %q", name, s.Config().Version, s.LastError())
		}
	}
	if err := s.Update([]byte(`{"ceiling":`)); err == nil || s.Config().Version != "v1" {
		t.Error("a config that doesn't parse replaced the good one")
	}
}

// noWatchList turns off the streaming WatchList protocol the fake
// clientset doesn't serve (see authcallout's store test): the test runs the
// LIST-then-WATCH fallback, a real API server the WatchList path.
func noWatchList(t *testing.T) {
	clientfeaturestesting.SetFeatureDuringTest(t, clientfeatures.WatchListClient, false)
}

// The binary's source: the one ConfigMap, watched through the API, so a
// change lands without a restart.
func TestConfigStoreWatchesTheConfigMap(t *testing.T) {
	noWatchList(t)
	const ns, name = "kube-agents", "agent-a2a-dispatch"
	cfg := testDispatchConfig(6, testProfile("tse", 2))
	cfg.Version = "v1"
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
		Data: map[string]string{lib.DispatchConfigKey: string(rawConfig(t, cfg))}}
	other := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "something-else"},
		Data: map[string]string{lib.DispatchConfigKey: `{"version":"intruder"}`}}
	client := fake.NewSimpleClientset(cm, other)
	s := NewConfigStore(quietLog())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = s.WatchConfigMap(ctx, client, ns, name, lib.DispatchConfigKey) }()
	if err := s.WaitForConfig(ctx, 5*time.Second); err != nil {
		t.Fatal(err)
	}
	if s.Config().Version != "v1" {
		t.Fatalf("serving %q", s.Config().Version)
	}
	cfg.Version, cfg.Ceiling = "v2", 9
	cm.Data[lib.DispatchConfigKey] = string(rawConfig(t, cfg))
	if _, err := client.CoreV1().ConfigMaps(ns).Update(ctx, cm, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 5*time.Second, "the update", func() bool { return s.Config().Version == "v2" && s.Config().Ceiling == 9 })
	cm.Data[lib.DispatchConfigKey] = `{"version":"v3"}`
	if _, err := client.CoreV1().ConfigMaps(ns).Update(ctx, cm, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 5*time.Second, "the refusal", func() bool { return strings.Contains(s.LastError(), "dispatch config") })
	if s.Config().Version != "v2" {
		t.Errorf("serving %q after a broken render, want v2", s.Config().Version)
	}
}

func workerPod(name, profile, tier string, ready bool) *corev1.Pod {
	status := corev1.ConditionFalse
	if ready {
		status = corev1.ConditionTrue
	}
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Namespace: "kube-agents-profiles", Name: name, Labels: map[string]string{
			lib.ProfileComponentLabel: lib.ProfileWorkerComponent, lib.ProfileNameLabel: profile, lib.ProfileTierLabel: tier}},
		Status: corev1.PodStatus{Phase: corev1.PodRunning, Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: status}}},
	}
}

// The pool is the Ready worker pods of a (profile, tier), by label.
func TestPodPoolOffersOnlyReadyWorkersOfThePool(t *testing.T) {
	noWatchList(t)
	notWorker := workerPod("tse-sa-ish", "tse", "read", true)
	notWorker.Labels[lib.ProfileComponentLabel] = "something-else"
	client := fake.NewSimpleClientset(
		workerPod("tse-read-b", "tse", "read", true),
		workerPod("tse-read-a", "tse", "read", true),
		workerPod("tse-read-c", "tse", "read", false),
		workerPod("tse-unattended-a", "tse", "unattended", true),
		workerPod("chat-read-a", "chat", "read", true),
		notWorker,
	)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	p := NewPodPool(ctx, client, "kube-agents-profiles", quietLog())
	go p.Run(ctx)
	waitFor(t, 5*time.Second, "the pool synced", p.Synced)
	if got := strings.Join(p.Pods("tse", "read"), ","); got != "tse-read-a,tse-read-b" {
		t.Errorf("tse/read pool %q", got)
	}
	ready := workerPod("tse-read-c", "tse", "read", true)
	if _, err := client.CoreV1().Pods("kube-agents-profiles").UpdateStatus(ctx, ready, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 5*time.Second, "the pod turning ready", func() bool { return len(p.Pods("tse", "read")) == 3 })
}

// The pool reads a pod's UID off the watched pod object (#3889): only for a
// pod of that (profile, tier), not one being deleted, and the new UID once a
// pod of the same name is re-created.
func TestPodPoolReadsAPodsUIDFromTheWatchedPod(t *testing.T) {
	noWatchList(t)
	withUID := func(p *corev1.Pod, uid string) *corev1.Pod { p.UID = types.UID(uid); return p }
	client := fake.NewSimpleClientset(
		withUID(workerPod("tse-read-a", "tse", "read", true), "uid-a"),
		withUID(workerPod("tse-read-b", "tse", "read", true), "uid-b"),
	)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	p := NewPodPool(ctx, client, "kube-agents-profiles", quietLog())
	go p.Run(ctx)
	waitFor(t, 5*time.Second, "the pool synced", p.Synced)
	for _, tc := range []struct{ profile, tier, pod, want string }{
		{"tse", "read", "tse-read-a", "uid-a"},
		{"tse", "read", "tse-read-b", "uid-b"},
		{"tse", "unattended", "tse-read-a", ""},
		{"chat", "read", "tse-read-a", ""},
		{"tse", "read", "tse-read-z", ""},
	} {
		if got := p.PodUID(tc.profile, tc.tier, tc.pod); got != tc.want {
			t.Errorf("PodUID(%s, %s, %s) = %q, want %q", tc.profile, tc.tier, tc.pod, got, tc.want)
		}
	}
	pods := client.CoreV1().Pods("kube-agents-profiles")
	if err := pods.Delete(ctx, "tse-read-a", metav1.DeleteOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := pods.Create(ctx, withUID(workerPod("tse-read-a", "tse", "read", true), "uid-a2"), metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 5*time.Second, "the re-created pod's UID", func() bool { return p.PodUID("tse", "read", "tse-read-a") == "uid-a2" })
	deleting := withUID(workerPod("tse-read-b", "tse", "read", true), "uid-b")
	now := metav1.Now()
	deleting.DeletionTimestamp = &now
	if _, err := pods.Update(ctx, deleting, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 5*time.Second, "a deleting pod has no UID", func() bool { return p.PodUID("tse", "read", "tse-read-b") == "" })
}

// The task-signing settings (#3889): a key must be a cluster key (the
// callout trusts only those, so any other would mint credentials nothing
// accepts) and the lifetime cap must not be negative; zero is the callout's
// default, and no key at all is valid.
func TestConfigValidateChecksTheTaskSigningSettings(t *testing.T) {
	cluster, err := nkeys.CreateCluster()
	if err != nil {
		t.Fatal(err)
	}
	account, _ := nkeys.CreateAccount()
	user, _ := nkeys.CreateUser()
	operator, _ := nkeys.CreateOperator()
	for name, edit := range map[string]func(*Config){
		"no key":                  func(c *Config) {},
		"a cluster key":           func(c *Config) { c.TaskSigningKey = cluster },
		"a cluster key, zero cap": func(c *Config) { c.TaskSigningKey, c.TaskCredentialMaxLifetime = cluster, 0 },
		"a cluster key, a 2h cap": func(c *Config) { c.TaskSigningKey, c.TaskCredentialMaxLifetime = cluster, 2*time.Hour },
		"no key, a positive cap":  func(c *Config) { c.TaskCredentialMaxLifetime = time.Minute },
	} {
		c := DefaultConfig()
		edit(&c)
		if err := c.Validate(); err != nil {
			t.Errorf("%s: refused: %v", name, err)
		}
	}
	for name, edit := range map[string]func(*Config){
		"an account key":         func(c *Config) { c.TaskSigningKey = account },
		"a user key":             func(c *Config) { c.TaskSigningKey = user },
		"an operator key":        func(c *Config) { c.TaskSigningKey = operator },
		"a negative cap":         func(c *Config) { c.TaskSigningKey, c.TaskCredentialMaxLifetime = cluster, -time.Second },
		"a negative cap, no key": func(c *Config) { c.TaskCredentialMaxLifetime = -time.Hour },
	} {
		c := DefaultConfig()
		edit(&c)
		if err := c.Validate(); err == nil {
			t.Errorf("%s: accepted", name)
		}
		if _, err := New(c, nil, nil, nil, nil, nil); err == nil || !strings.Contains(err.Error(), "dispatch config") {
			t.Errorf("%s: New = %v, want the config refusal", name, err)
		}
	}
}
