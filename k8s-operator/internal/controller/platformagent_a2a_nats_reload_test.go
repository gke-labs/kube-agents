package controller

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// a2aReloadFixture is a running bus as the reload sees it: the live config
// Secret, the StatefulSet on hash "h1", and its pod.
func a2aReloadFixture(t *testing.T, liveConf string, annotations map[string]string) (*PlatformAgentReconciler, *agentv1alpha1.PlatformAgent, *appsv1.StatefulSet, *record.FakeRecorder) {
	t.Helper()
	agent := a2aTestAgent()
	sts := buildA2ANATSStatefulSet(agent, "h1")
	sts.UID = "sts-uid"
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSConfigSecretName(agent), Namespace: agent.Namespace, Annotations: annotations},
		Data:       map[string][]byte{a2aNATSConfKey: []byte(liveConf)},
	}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Name: sts.Name + a2aNATSPodOrdinalSuffix, Namespace: agent.Namespace, UID: "pod-uid",
		OwnerReferences: []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "StatefulSet", Name: sts.Name, UID: sts.UID, Controller: ptr.To(true)}},
	}}
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent, sts, secret, pod).Build()
	rec := record.NewFakeRecorder(10)
	return &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), Recorder: rec}, agent, sts, rec
}

func a2aDesiredConfig(agent *agentv1alpha1.PlatformAgent, conf string) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSConfigSecretName(agent), Namespace: agent.Namespace},
		Data:       map[string][]byte{a2aNATSConfKey: []byte(conf)},
	}
}

func a2aBusPodExists(t *testing.T, r *PlatformAgentReconciler, sts *appsv1.StatefulSet) bool {
	t.Helper()
	err := r.Get(context.Background(), types.NamespacedName{Name: sts.Name + a2aNATSPodOrdinalSuffix, Namespace: sts.Namespace}, &corev1.Pod{})
	if err != nil && !apierrors.IsNotFound(err) {
		t.Fatal(err)
	}
	return err == nil
}

// A config change the pod template's hash doesn't see is a reload: the plan
// records when it was asked for and the load time it has to move off.
func TestANewConfigUnderTheSameHashIsAReload(t *testing.T) {
	r, agent, sts, _ := a2aReloadFixture(t, "old", nil)
	r.busConfigLoadTime = func(context.Context, *agentv1alpha1.PlatformAgent) (string, error) { return "t0", nil }
	config := a2aDesiredConfig(agent, "new")
	now := time.Date(2026, 11, 9, 22, 0, 0, 0, time.UTC)
	plan, err := r.planA2ABusReload(context.Background(), agent, config, sts.DeepCopy(), now)
	if err != nil {
		t.Fatal(err)
	}
	if plan.restartPod || plan.reloaded || plan.requeue != a2aBusReloadRecheck {
		t.Fatalf("plan = %+v, want a pending reload rechecked in %s", plan, a2aBusReloadRecheck)
	}
	if config.Annotations[a2aBusReloadBaselineAnnotation] != "t0" || config.Annotations[a2aBusReloadRequestedAnnotation] != now.Format(time.RFC3339Nano) {
		t.Fatalf("pending reload not stamped: %v", config.Annotations)
	}
}

// With a roll coming, the new config reaches the bus on the new pod, and
// there's no reload to track.
func TestAChangedHashIsARollNotAReload(t *testing.T) {
	r, agent, _, _ := a2aReloadFixture(t, "old", nil)
	config := a2aDesiredConfig(agent, "new")
	plan, err := r.planA2ABusReload(context.Background(), agent, config, buildA2ANATSStatefulSet(agent, "h2"), time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if plan != (a2aBusReload{}) || len(config.Annotations) != 0 {
		t.Fatalf("plan = %+v, annotations %v; want nothing tracked", plan, config.Annotations)
	}
}

// A reload whose config_load_time never moves is given up on at the bound:
// the bus pod is deleted, so the StatefulSet restarts it onto the new
// config, and a Warning says so. Inside the bound it stays pending.
func TestAReloadThatDoesntLandRestartsTheBusPod(t *testing.T) {
	asked := time.Date(2026, 11, 9, 22, 0, 0, 0, time.UTC)
	pending := map[string]string{
		a2aBusReloadRequestedAnnotation: asked.Format(time.RFC3339Nano),
		a2aBusReloadBaselineAnnotation:  "t0",
	}
	r, agent, sts, rec := a2aReloadFixture(t, "new", pending)
	r.busConfigLoadTime = func(context.Context, *agentv1alpha1.PlatformAgent) (string, error) { return "t0", nil }
	ctx := context.Background()

	config := a2aDesiredConfig(agent, "new")
	plan, err := r.planA2ABusReload(ctx, agent, config, sts.DeepCopy(), asked.Add(a2aBusReloadBound-time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if plan.restartPod || plan.requeue <= 0 || config.Annotations[a2aBusReloadRequestedAnnotation] != pending[a2aBusReloadRequestedAnnotation] {
		t.Fatalf("inside the bound: plan = %+v, annotations %v; want still pending", plan, config.Annotations)
	}

	config = a2aDesiredConfig(agent, "new")
	plan, err = r.planA2ABusReload(ctx, agent, config, sts.DeepCopy(), asked.Add(a2aBusReloadBound))
	if err != nil {
		t.Fatal(err)
	}
	if !plan.restartPod || len(config.Annotations) != 0 {
		t.Fatalf("at the bound: plan = %+v, annotations %v; want a restart and no pending record", plan, config.Annotations)
	}
	if err := r.finishA2ABusReload(ctx, agent, sts, plan); err != nil {
		t.Fatal(err)
	}
	if a2aBusPodExists(t, r, sts) {
		t.Fatal("the bus pod wasn't restarted after the reload failed")
	}
	select {
	case ev := <-rec.Events:
		if !strings.HasPrefix(ev, corev1.EventTypeWarning+" "+a2aBusReloadFailedReason) {
			t.Fatalf("event = %q, want a %s Warning", ev, a2aBusReloadFailedReason)
		}
	default:
		t.Fatal("no Event for the failed reload")
	}
}

// A load time that can't be read before the write leaves no baseline, so the
// reload can't be confirmed and ends in a restart, never a false success.
func TestAnUnreadableLoadTimeEndsInARestart(t *testing.T) {
	r, agent, sts, _ := a2aReloadFixture(t, "old", nil)
	readable := false
	r.busConfigLoadTime = func(context.Context, *agentv1alpha1.PlatformAgent) (string, error) {
		if !readable {
			return "", errors.New("connection refused")
		}
		return "t1", nil
	}
	ctx := context.Background()
	asked := time.Date(2026, 11, 9, 22, 0, 0, 0, time.UTC)
	config := a2aDesiredConfig(agent, "new")
	if _, err := r.planA2ABusReload(ctx, agent, config, sts.DeepCopy(), asked); err != nil {
		t.Fatal(err)
	}
	live := &corev1.Secret{}
	if err := r.Get(ctx, client.ObjectKeyFromObject(config), live); err != nil {
		t.Fatal(err)
	}
	live.Data = config.Data
	live.Annotations = config.Annotations
	if err := r.Update(ctx, live); err != nil {
		t.Fatal(err)
	}
	readable = true
	plan, err := r.planA2ABusReload(ctx, agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if plan.reloaded {
		t.Fatal("a reload with no baseline was taken as landed")
	}
	plan, err = r.planA2ABusReload(ctx, agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(a2aBusReloadBound))
	if err != nil {
		t.Fatal(err)
	}
	if !plan.restartPod {
		t.Fatalf("plan = %+v, want a restart at the bound", plan)
	}
}

// The reloader holds the config and the PID file, both read-only, runs with
// no capabilities on a read-only root filesystem, and the pod mounts no
// service account token. nats-server writes its PID where the reloader reads
// it, and the two share a PID namespace.
func TestTheReloaderHoldsOnlyTheConfigAndThePID(t *testing.T) {
	agent := a2aTestAgent()
	pod := buildA2ANATSStatefulSet(agent, "h").Spec.Template.Spec
	if pod.AutomountServiceAccountToken == nil || *pod.AutomountServiceAccountToken {
		t.Fatal("the bus pod mounts a service account token")
	}
	if pod.ShareProcessNamespace == nil || !*pod.ShareProcessNamespace {
		t.Fatal("the bus pod doesn't share one PID namespace, so the reloader can't signal nats-server")
	}
	var reloader *corev1.Container
	for i := range pod.Containers {
		if pod.Containers[i].Name == a2aNATSReloaderContainer {
			reloader = &pod.Containers[i]
		}
	}
	if reloader == nil {
		t.Fatal("no reloader container")
	}
	if !strings.Contains(reloader.Image, "@sha256:") || !strings.HasPrefix(reloader.Image, "natsio/nats-server-config-reloader:") {
		t.Fatalf("reloader image %q isn't nats-io's, pinned by digest", reloader.Image)
	}
	sc := reloader.SecurityContext
	if sc == nil || sc.ReadOnlyRootFilesystem == nil || !*sc.ReadOnlyRootFilesystem || sc.Capabilities == nil ||
		len(sc.Capabilities.Drop) != 1 || sc.Capabilities.Drop[0] != "ALL" || len(sc.Capabilities.Add) != 0 ||
		sc.AllowPrivilegeEscalation == nil || *sc.AllowPrivilegeEscalation {
		t.Fatalf("reloader security context = %+v", sc)
	}
	mounts := map[string]corev1.VolumeMount{}
	for _, m := range reloader.VolumeMounts {
		mounts[m.Name] = m
	}
	if len(mounts) != 2 || !mounts["config"].ReadOnly || !mounts[a2aNATSPIDVolume].ReadOnly {
		t.Fatalf("reloader mounts = %+v, want the config and the PID dir, read-only", reloader.VolumeMounts)
	}
	if len(reloader.Env) != 0 || len(reloader.EnvFrom) != 0 {
		t.Fatal("the reloader carries environment")
	}
	conf := renderA2ANATSConf(agent, func(string) string { return "pw" }, &a2aCalloutKeys{IssuerPublic: "A", XKeyPublic: "X"}, nil)
	if !strings.Contains(conf, `pid_file: "`+a2aNATSPIDFile+`"`) {
		t.Fatal("nats.conf doesn't write the PID file the reloader reads")
	}
}

// The monitoring port admits the operator's pod and nothing else, for the
// load time a reload is confirmed by.
func TestTheMonitorPortAdmitsTheOperatorAlone(t *testing.T) {
	t.Setenv(operatorNamespaceEnvVar, "kube-agents-system")
	t.Setenv(operatorServiceAccountEnvVar, "kube-agents-controller-manager")
	policy := buildA2ANATSNetworkPolicy(a2aTestAgent())
	var monitor []networkingv1.NetworkPolicyIngressRule
	for _, rule := range policy.Spec.Ingress {
		for _, p := range rule.Ports {
			if p.Port != nil && p.Port.IntValue() == a2aNATSMonitorPort {
				monitor = append(monitor, rule)
			}
		}
	}
	if len(monitor) != 1 {
		t.Fatalf("%d ingress rules name the monitor port, want one", len(monitor))
	}
	if len(monitor[0].From) != 1 || monitor[0].From[0].PodSelector == nil ||
		monitor[0].From[0].PodSelector.MatchLabels[a2aOperatorBusClientLabel] != a2aOperatorBusClientLabelValue {
		t.Fatalf("monitor port peers = %+v, want the operator's pod alone", monitor[0].From)
	}
}
