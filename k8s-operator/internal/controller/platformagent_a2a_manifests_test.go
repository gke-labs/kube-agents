/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"fmt"
	rbacv1 "k8s.io/api/rbac/v1"
	"reflect"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"slices"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func a2aTestAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec:       agentv1alpha1.PlatformAgentSpec{Mode: ptr.To("next")},
	}
}

func a2aTestCreds() *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent-a2a-nats-creds", Namespace: "test-ns"},
		Data: map[string][]byte{
			"gateway-password": []byte("pw-gateway"),
			"worker-password":  []byte("pw-worker"),
			"seed-password":    []byte("pw-seed"),
			"web-password":     []byte("pw-web"),
		},
	}
}

// The deployment spec's connect-time property: the bus decides who may say
// what before a message is read. Static users are the playground stand-in for
// the auth callout, but the deny-by-default subject lists are the real shape.
func TestBuildA2ANATSConfig(t *testing.T) {
	agent := a2aTestAgent()
	secret := buildA2ANATSConfigSecret(agent, a2aTestCreds())

	if secret.Name != "test-agent-a2a-nats-config" {
		t.Errorf("config secret name = %q", secret.Name)
	}
	if got := secret.Labels["app.kubernetes.io/part-of"]; got != a2aPartOf {
		t.Errorf("part-of label = %q, want %q", got, a2aPartOf)
	}

	conf := string(secret.Data["nats.conf"])
	if !strings.Contains(conf, "PLAYGROUND POSTURE") {
		t.Error("nats.conf is missing the playground-posture comment block")
	}
	if !strings.Contains(conf, "jetstream") {
		t.Error("nats.conf does not enable jetstream")
	}

	// Per-user inbox prefixes: without them any agent can subscribe to any
	// inbox and the connect-time property leaks through the reply path.
	for _, user := range []string{"gateway", "worker", "seed"} {
		if !strings.Contains(conf, "user: "+user) {
			t.Errorf("nats.conf missing user %q", user)
		}
		if !strings.Contains(conf, "_INBOX."+user+".>") {
			t.Errorf("nats.conf missing the _INBOX prefix for %q", user)
		}
	}

	// Passwords come from the creds Secret, not from literals invented here.
	for _, pw := range []string{"pw-gateway", "pw-worker", "pw-seed"} {
		if !strings.Contains(conf, pw) {
			t.Errorf("nats.conf does not carry the generated password %q", pw)
		}
	}

	// Spot the load-bearing grants: the gateway owns the session registry and
	// the task plane; the seed writes exactly the three starter topics.
	for _, grant := range []string{
		"a2a.tasks.*.*.in",
		"a2a.tasks.*.*.events",
		"$KV.session-state.>",
		"a2a.topics.agent.platform.upgrade-readiness",
		"a2a.topics.shared.blueprint",
		"a2a.topics.shared.annotations",
		// The delivery path's reply subjects: without an ack grant an
		// explicit ack is a permissions violation and every consumer
		// redelivers forever while TCP health stays green. Scoped per
		// stream — the exact surface is
		// TestSystemUsersAckGrantsAreScopedPerStream's to pin.
		"$JS.ACK.TASKS.>",
		"$JS.FC.>",
	} {
		if !strings.Contains(conf, grant) {
			t.Errorf("nats.conf missing grant %q", grant)
		}
	}

	// No app user authenticates into $SYS.
	if strings.Contains(conf, "account: SYS") && !strings.Contains(conf, "system_account") {
		t.Error("nats.conf wires an app user into $SYS")
	}
}

// TestSystemUsersAckGrantsAreScopedPerStream pins each user's ack surface
// exactly. An ack subject names a stream and a CONSUMER, never the caller,
// so an unscoped $JS.ACK.> lets its holder publish +TERM onto another
// principal's in-flight delivery and destroy it. gateway and worker ack only
// the TASKS deliveries they consume with explicit ack; seed and web create
// no acking consumer and hold no ack grant at all. Within the shared TASKS
// stream the grant cannot distinguish consumers (NATS wildcards match whole
// tokens), so any widening of this list is a review conversation, not a
// diff.
func TestSystemUsersAckGrantsAreScopedPerStream(t *testing.T) {
	conf := string(buildA2ANATSConfigSecret(a2aTestAgent(), a2aTestCreds()).Data["nats.conf"])

	want := map[string][]string{
		"gateway": {"$JS.ACK.TASKS.>"},
		"worker":  {"$JS.ACK.TASKS.>"},
		"seed":    nil,
		"web":     nil,
	}
	for user, wantAcks := range want {
		start := strings.Index(conf, "user: "+user)
		if start < 0 {
			t.Fatalf("nats.conf has no %s user", user)
		}
		block := conf[start:]
		if next := strings.Index(block[1:], "user: "); next >= 0 {
			block = block[:next+1]
		}
		pubStart, subStart := strings.Index(block, "publish"), strings.Index(block, "subscribe")
		if pubStart < 0 || subStart < 0 {
			t.Fatalf("%s: could not slice the publish block", user)
		}
		var got []string
		for _, line := range strings.Split(block[pubStart:subStart], "\n") {
			entry := strings.Trim(strings.TrimSuffix(strings.TrimSpace(line), ","), `"`)
			if strings.HasPrefix(entry, "$JS.ACK") {
				got = append(got, entry)
			}
		}
		if !reflect.DeepEqual(got, wantAcks) {
			t.Errorf("%s ack grants = %q, want %q", user, got, wantAcks)
		}
	}

	// The unscoped form is gone from the whole config, not just relocated.
	if strings.Contains(conf, `"$JS.ACK.>"`) {
		t.Error("nats.conf still grants unscoped $JS.ACK.> to someone")
	}
}

func TestBuildA2ANATSStatefulSet(t *testing.T) {
	agent := a2aTestAgent()
	sts := buildA2ANATSStatefulSet(agent, "conf-hash")

	if sts.Name != "test-agent-a2a-nats" {
		t.Errorf("statefulset name = %q", sts.Name)
	}
	if got := *sts.Spec.Replicas; got != 1 {
		t.Errorf("replicas = %d, want 1 (single node R1 is the dev posture)", got)
	}
	if len(sts.Spec.VolumeClaimTemplates) != 1 {
		t.Fatalf("expected one volumeClaimTemplate (JetStream file store on a PV), got %d", len(sts.Spec.VolumeClaimTemplates))
	}
	if got := sts.Spec.Template.Spec.Containers[0].Image; got != defaultA2ANATSImage {
		t.Errorf("image = %q, want %q", got, defaultA2ANATSImage)
	}
	if got := sts.Labels["app.kubernetes.io/part-of"]; got != a2aPartOf {
		t.Errorf("part-of label = %q, want %q", got, a2aPartOf)
	}

	t.Setenv(a2aNATSImageEnvVar, "example.com/nats:pinned")
	if got := buildA2ANATSStatefulSet(agent, "conf-hash").Spec.Template.Spec.Containers[0].Image; got != "example.com/nats:pinned" {
		t.Errorf("env override ignored, image = %q", got)
	}
}

// The provisioning payload: four streams, three KV buckets, three starter
// topics, the deployment spec's retention numbers verbatim.
func TestBuildA2AProvisionJob(t *testing.T) {
	agent := a2aTestAgent()
	job := buildA2AProvisionJob(agent)

	if !strings.HasPrefix(job.Name, "test-agent-a2a-provision") {
		t.Errorf("job name = %q", job.Name)
	}
	script := ""
	for _, c := range job.Spec.Template.Spec.Containers {
		for _, e := range c.Args {
			script += e + "\n"
		}
		for _, e := range c.Command {
			script += e + "\n"
		}
	}

	for _, want := range []string{
		// TASKS: a2a.tasks.>, 72h, 20GiB
		"TASKS", "a2a.tasks.>", "--max-age=72h", "21474836480",
		// DIRECTORY: last-value, 1GiB
		"DIRECTORY", "a2a.agents.>", "--max-msgs-per-subject=1",
		// TOPICS-STATE: 8-deep, no age, the two state topics
		"TOPICS-STATE", "--max-msgs-per-subject=8",
		"a2a.topics.agent.platform.upgrade-readiness", "a2a.topics.shared.blueprint",
		// TOPICS-JOURNAL: 30d, 5GiB, the journal topic
		"TOPICS-JOURNAL", "--max-age=720h", "5368709120", "a2a.topics.shared.annotations",
		// max_bytes discipline
		"1073741824", "--discard=old",
		// KV buckets, capped like the streams
		"runtime-state", "session-state", "--max-bucket-size",
		// Every stream/kv call is a $JS.API request answered on an inbox, and
		// seed may only subscribe under _INBOX.seed.> — without the prefix
		// override every CLI call times out and the Job can never succeed.
		"--inbox-prefix=_INBOX.seed",
		// posture
		"PLAYGROUND POSTURE",
	} {
		if !strings.Contains(script, want) {
			t.Errorf("provision script missing %q", want)
		}
	}
	// The reserved capability bucket ("cap", capability envelope design) —
	// checked as a distinct word so "cap" inside another token cannot satisfy it.
	if !strings.Contains(script, "kv add cap") {
		t.Error("provision script missing the reserved capability bucket")
	}
}

// mode: next renders the A2A stack; flipping back to today removes it. This is
// the reconciler-level gate — builders are covered above.
func TestReconcileA2AGatedByMode(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	// finalizer pass, then the real one
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}

	sts := &appsv1.StatefulSet{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-nats", Namespace: "test-ns"}, sts); err != nil {
		t.Errorf("NATS StatefulSet not rendered under next: %v", err)
	}
	svc := &corev1.Service{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-nats", Namespace: "test-ns"}, svc); err != nil {
		t.Errorf("NATS Service not rendered under next: %v", err)
	}
	creds := &corev1.Secret{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-nats-creds", Namespace: "test-ns"}, creds); err != nil {
		t.Fatalf("creds Secret not rendered under next: %v", err)
	}
	for _, key := range []string{"gateway-password", "worker-password", "seed-password"} {
		if len(creds.Data[key]) < 24 {
			t.Errorf("creds key %q missing or too short", key)
		}
	}
	gen1 := string(creds.Data["gateway-password"])

	jobs := &batchv1.JobList{}
	if err := cl.List(ctx, jobs); err != nil || len(jobs.Items) == 0 {
		t.Errorf("provision Job not rendered under next (err=%v, n=%d)", err, len(jobs.Items))
	}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-gateway", Namespace: "test-ns"}, dep); err != nil {
		t.Errorf("A2A gateway Deployment not rendered under next: %v", err)
	}

	// Reconcile again: the creds Secret must be generated once and kept, not
	// re-rolled — re-rolling would invalidate every connected client on every
	// reconcile.
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 3 failed: %v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-nats-creds", Namespace: "test-ns"}, creds); err != nil {
		t.Fatalf("creds Secret vanished on re-reconcile: %v", err)
	}
	if string(creds.Data["gateway-password"]) != gen1 {
		t.Error("creds Secret was regenerated on re-reconcile")
	}

	// Flip to today: the dark stack goes back to dark.
	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	fresh.Spec.Mode = nil
	if err := cl.Update(ctx, fresh); err != nil {
		t.Fatalf("failed to update agent: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 4 failed: %v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-nats", Namespace: "test-ns"}, sts); !errors.IsNotFound(err) {
		t.Errorf("NATS StatefulSet still present under today (err=%v)", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-gateway", Namespace: "test-ns"}, dep); !errors.IsNotFound(err) {
		t.Errorf("A2A gateway Deployment still present under today (err=%v)", err)
	}
	// Today's own stack is untouched by the cleanup.
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-gateway", Namespace: "test-ns"}, &appsv1.Deployment{}); err != nil {
		t.Errorf("today's Deployment missing after A2A cleanup: %v", err)
	}
}

// Version skew must not touch the A2A branch in either direction: renderMode
// fails closed to today, and letting that reach cleanupA2A would have a
// one-version operator rollback tear down a live bus a newer CRD legitimately
// rendered. Skew is a status problem, not a rendering instruction.
func TestUnrecognizedModePreservesRunningNextStack(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	// Render the next stack first.
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}
	sts := &appsv1.StatefulSet{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-nats", Namespace: "test-ns"}, sts); err != nil {
		t.Fatalf("next stack did not render: %v", err)
	}

	// Now the skew: a mode this binary does not know.
	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	fresh.Spec.Mode = ptr.To("next2")
	if err := cl.Update(ctx, fresh); err != nil {
		t.Fatalf("failed to update agent: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 3 failed: %v", err)
	}

	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-nats", Namespace: "test-ns"}, sts); err != nil {
		t.Errorf("skew tore down the running NATS StatefulSet: %v", err)
	}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-gateway", Namespace: "test-ns"}, dep); err != nil {
		t.Errorf("skew tore down the running A2A gateway: %v", err)
	}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	if fresh.Status.Phase != "Degraded" {
		t.Errorf("skew must still be reported: phase %q, want Degraded", fresh.Status.Phase)
	}
}

// A creds Secret missing a key would render `password: ""` into nats.conf — a
// user anyone can log in as — so the shape is repaired, while intact keys are
// never re-rolled. "Intact" means the exact shape randomA2APassword emits:
// these values are interpolated into nats.conf inside double quotes, so a
// value carrying a quote and a newline is a config injection (a new user, a
// widened grant) that the operator would re-render on every reconcile —
// malformed keys are therefore re-rolled exactly like missing ones.
func TestEnsureA2ACredsSecretRepairsMissingKeys(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	const intact = "0123456789abcdef0123456789abcdef"
	injected := "x\"}\nusers [ { user: evil, password: \"pw\" } ]"
	partial := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent-a2a-nats-creds", Namespace: "test-ns"},
		Data: map[string][]byte{
			"gateway-password": []byte(intact),
			"worker-password":  []byte(injected),
		},
	}

	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, partial).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	got, err := r.ensureA2ACredsSecret(context.Background(), agent)
	if err != nil {
		t.Fatalf("ensureA2ACredsSecret failed: %v", err)
	}
	if string(got.Data["gateway-password"]) != intact {
		t.Error("an intact key was re-rolled")
	}
	if string(got.Data["worker-password"]) == injected {
		t.Error("a malformed key survived repair; its value reaches nats.conf inside quotes")
	}
	for _, key := range a2aCredsKeys {
		if !a2aCredsValueRe.Match(got.Data[key]) {
			t.Errorf("key %q was not repaired to the generated shape: %q", key, got.Data[key])
		}
	}
}

// The JetStream PVC comes from a volumeClaimTemplate, so it has no owner
// reference and the finalizer deletes it by name — but a name is not
// ownership. The instance label the claim template stamps is the guard: a
// squatter PVC wearing the exact name is left alone.
func TestHandleDeletionReapsOnlyTheLabeledJetStreamPVC(t *testing.T) {
	scheme := setupScheme()

	run := func(t *testing.T, pvcLabels map[string]string, wantDeleted bool) {
		t.Helper()
		agent := a2aTestAgent()
		agent.Finalizers = []string{platformAgentFinalizer}
		now := metav1.Now()
		agent.DeletionTimestamp = &now
		pvc := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{
			Name: "data-test-agent-a2a-nats-0", Namespace: "test-ns", Labels: pvcLabels,
		}}
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, pvc).Build()
		r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

		if _, err := r.handleDeletion(context.Background(), agent); err != nil {
			t.Fatalf("handleDeletion failed: %v", err)
		}
		err := cl.Get(context.Background(), types.NamespacedName{Name: pvc.Name, Namespace: pvc.Namespace}, &corev1.PersistentVolumeClaim{})
		if wantDeleted && !errors.IsNotFound(err) {
			t.Errorf("labeled JetStream PVC survived deletion (err=%v)", err)
		}
		if !wantDeleted && err != nil {
			t.Errorf("an unlabeled squatter PVC was deleted (err=%v)", err)
		}
	}

	t.Run("labeled PVC is reaped", func(t *testing.T) {
		run(t, buildA2ANATSStatefulSet(a2aTestAgent(), "h").Spec.VolumeClaimTemplates[0].Labels, true)
	})
	t.Run("unlabeled squatter is left alone", func(t *testing.T) {
		run(t, nil, false)
	})
}

// The web read surface: a websocket listener (plain ws is the stated
// playground posture; production terminates TLS in front), a ClusterIP port
// for it, and a `web` user that can watch everything and say nothing —
// subscribe on a2a.>, the JetStream read API, its own inbox, and no publish
// reach beyond those. The read-only web rail is the consumer; kubectl
// port-forward is the demo transport, which is why ClusterIP is enough.
func TestBuildA2ANATSConfigWebsocketAndWebUser(t *testing.T) {
	conf := string(buildA2ANATSConfigSecret(a2aTestAgent(), a2aTestCreds()).Data["nats.conf"])

	if !strings.Contains(conf, "websocket {") {
		t.Fatal("nats.conf has no websocket block")
	}
	if !strings.Contains(conf, "no_tls: true") {
		t.Error("websocket block does not state plain ws (no_tls: true)")
	}
	if !strings.Contains(conf, "port: 9222") {
		t.Error("websocket listener is not on 9222")
	}

	// Slice out the web user's entry so the assertions below cannot pass off
	// another user's grants as web's. The entry runs from `user: web` to the
	// next user or the end of the users list.
	start := strings.Index(conf, "user: web")
	if start < 0 {
		t.Fatal("nats.conf has no web user")
	}
	rest := conf[start:]
	if next := strings.Index(rest[1:], "user: "); next >= 0 {
		rest = rest[:next+1]
	}

	if !strings.Contains(rest, "pw-web") {
		t.Error("web's password does not come from the creds Secret")
	}

	// The publish list is pinned EXACTLY, not scanned for banned words. The
	// first version of this test used a banned-substring loop and passed
	// against a grant that let web read the session-state KV bucket and
	// destroy another principal's in-flight delivery: `CONSUMER.CREATE.>`
	// contains none of the words a blocklist would think to name, because the
	// reach lives in request bodies and in wildcards matching other
	// principals' resources. An exact list means any widening is a review
	// conversation, which is the only control that actually holds here.
	pub := rest[strings.Index(rest, "publish"):strings.Index(rest, "subscribe")]
	var got []string
	for _, line := range strings.Split(pub, "\n") {
		line = strings.TrimSpace(line)
		line = strings.TrimSuffix(line, ",")
		if strings.HasPrefix(line, `"`) {
			got = append(got, strings.Trim(line, `"`))
		}
	}
	want := []string{
		"$JS.API.INFO",
		"$JS.API.STREAM.INFO.TASKS",
		"$JS.API.STREAM.INFO.DIRECTORY",
		"$JS.API.STREAM.INFO.TOPICS-STATE",
		"$JS.API.STREAM.INFO.TOPICS-JOURNAL",
		"$JS.API.CONSUMER.CREATE.TASKS.>",
		"$JS.API.CONSUMER.CREATE.DIRECTORY.>",
		"$JS.API.CONSUMER.CREATE.TOPICS-STATE.>",
		"$JS.API.CONSUMER.CREATE.TOPICS-JOURNAL.>",
		"$JS.API.CONSUMER.INFO.TASKS.*",
		"$JS.API.CONSUMER.INFO.DIRECTORY.*",
		"$JS.API.CONSUMER.INFO.TOPICS-STATE.*",
		"$JS.API.CONSUMER.INFO.TOPICS-JOURNAL.*",
		"$JS.API.CONSUMER.MSG.NEXT.TASKS.*",
		"$JS.API.CONSUMER.MSG.NEXT.DIRECTORY.*",
		"$JS.API.CONSUMER.MSG.NEXT.TOPICS-STATE.*",
		"$JS.API.CONSUMER.MSG.NEXT.TOPICS-JOURNAL.*",
		"_INBOX.web.>",
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("web publish allow-list changed.\n got: %q\nwant: %q", got, want)
	}

	// The three that were live findings, named so a regression reads as the
	// thing it is rather than as a diff in a long list.
	for _, gone := range []string{"$JS.ACK.", "$JS.FC.", "$JS.API.CONSUMER.CREATE.>", "STREAM.NAMES", "STREAM.LIST", "CONSUMER.NAMES", "CONSUMER.LIST", "$JS.API.>"} {
		if strings.Contains(pub, gone) {
			t.Errorf("web regained %q — see the web user's comment for what each one costs", gone)
		}
	}
	// No KV bucket is reachable: the consumer-create grants name the four a2a
	// message streams, and a KV bucket is a stream called KV_<bucket>.
	if strings.Contains(pub, "KV_") || strings.Contains(pub, "$KV.") {
		t.Error("web can address a KV bucket stream")
	}
}

func TestBuildA2ANATSServiceExposesWebsocket(t *testing.T) {
	svc := buildA2ANATSService(a2aTestAgent())
	ports := map[string]int32{}
	for _, p := range svc.Spec.Ports {
		ports[p.Name] = p.Port
	}
	if ports["client"] != 4222 || ports["websocket"] != 9222 {
		t.Errorf("service ports = %v, want client 4222 and websocket 9222", ports)
	}
}

// A nats.conf change must reach the running server. The Secret updates in
// place but the nats container only reads it at boot, so the StatefulSet pod
// template carries a hash of the rendered config — same mechanism as the
// agent Deployment's config-hash — and a changed render rolls the pod.
func TestBuildA2ANATSStatefulSetRollsOnConfigChange(t *testing.T) {
	agent := a2aTestAgent()
	a := buildA2ANATSStatefulSet(agent, "hash-one")
	b := buildA2ANATSStatefulSet(agent, "hash-two")
	annA := a.Spec.Template.Annotations["kubeagents.x-k8s.io/a2a-config-hash"]
	annB := b.Spec.Template.Annotations["kubeagents.x-k8s.io/a2a-config-hash"]
	if annA == "" || annA == annB {
		t.Errorf("config hash annotation missing or inert: %q vs %q", annA, annB)
	}

	var ws *corev1.ContainerPort
	for i, p := range a.Spec.Template.Spec.Containers[0].Ports {
		if p.Name == "websocket" {
			ws = &a.Spec.Template.Spec.Containers[0].Ports[i]
		}
	}
	if ws == nil || ws.ContainerPort != 9222 {
		t.Errorf("nats container does not expose websocket 9222: %+v", a.Spec.Template.Spec.Containers[0].Ports)
	}
}

// The gateway's identity and owner wiring. The Role is the WHOLE grant —
// one get on one named Deployment, so the gateway can resolve its own UID at
// boot and stamp an ownerReference onto the pods it will spawn once the
// worker PR arms spawning. Anything more here is a finding; the pod-lifecycle
// verbs land with their consumer in the worker PR.
func TestBuildA2AGatewayIdentityAndOwnerWiring(t *testing.T) {
	agent := a2aTestAgent()

	dep := buildA2AGatewayDeployment(agent)
	pod := dep.Spec.Template.Spec
	if pod.ServiceAccountName != "test-agent-a2a-gateway" {
		t.Errorf("ServiceAccountName = %q", pod.ServiceAccountName)
	}
	if pod.AutomountServiceAccountToken == nil || !*pod.AutomountServiceAccountToken {
		t.Error("the gateway needs the ServiceAccount token automounted for its owner-resolution get")
	}
	env := map[string]corev1.EnvVar{}
	for _, e := range pod.Containers[0].Env {
		env[e.Name] = e
	}
	// Spawning stays dark until the worker PR renders the arming: the switch
	// env must not appear here at all.
	if _, armed := env["A2A_SPAWN_SESSIONS"]; armed {
		t.Error("A2A_SPAWN_SESSIONS rendered before the worker PR arms spawning")
	}
	ns := env["POD_NAMESPACE"]
	if ns.ValueFrom == nil || ns.ValueFrom.FieldRef == nil || ns.ValueFrom.FieldRef.FieldPath != "metadata.namespace" {
		t.Errorf("POD_NAMESPACE = %+v", ns)
	}
	// The salt: the SAME Secret key the platform agent hashes session
	// metadata with, through the same resolver — the cross-surface join is
	// the property.
	salt := env["SESSION_KV_SALT"]
	if salt.ValueFrom == nil || salt.ValueFrom.SecretKeyRef == nil ||
		salt.ValueFrom.SecretKeyRef.Name != "platform-agent-secrets" ||
		salt.ValueFrom.SecretKeyRef.Key != "SESSION_KV_SALT" {
		t.Errorf("SESSION_KV_SALT = %+v", salt)
	}
	if env["A2A_OWNER_DEPLOYMENT"].Value != "test-agent-a2a-gateway" {
		t.Errorf("A2A_OWNER_DEPLOYMENT = %+v", env["A2A_OWNER_DEPLOYMENT"])
	}

	role := buildA2AGatewayRole(agent)
	if len(role.Rules) != 1 {
		t.Fatalf("gateway Role has %d rules, want exactly the pinned owner get", len(role.Rules))
	}
	// The owner rule is one verb on one named object — a deployments read
	// would be a finding.
	owner := role.Rules[0]
	if len(owner.APIGroups) != 1 || owner.APIGroups[0] != "apps" ||
		len(owner.Resources) != 1 || owner.Resources[0] != "deployments" ||
		len(owner.Verbs) != 1 || owner.Verbs[0] != "get" ||
		len(owner.ResourceNames) != 1 || owner.ResourceNames[0] != "test-agent-a2a-gateway" {
		t.Errorf("owner rule = %+v", owner)
	}

	rb := buildA2AGatewayRoleBinding(agent)
	if rb.RoleRef.Name != "test-agent-a2a-gateway" || rb.Subjects[0].Name != "test-agent-a2a-gateway" {
		t.Errorf("RoleBinding wiring: roleRef=%q subject=%q", rb.RoleRef.Name, rb.Subjects[0].Name)
	}
}

// TestGatewaySaltRefFollowsTheAgents: the join property itself. A CR that
// overrides sessionKVSaltSecretRef must steer BOTH renders — the agent pod
// and the a2a gateway — to the identical selector, or one human hashes to
// two values and the cross-surface audit join silently yields nothing.
// Inlining the default into either render keeps every other test green and
// breaks exactly this.
func TestGatewaySaltRefFollowsTheAgents(t *testing.T) {
	agent := a2aTestAgent()
	override := &corev1.SecretKeySelector{
		LocalObjectReference: corev1.LocalObjectReference{Name: "customer-salts"},
		Key:                  "chat-hmac",
	}
	if agent.Spec.Harness == nil {
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{}
	}
	agent.Spec.Harness.Hermes = &agentv1alpha1.HermesSpec{SessionKVSaltSecretRef: override}

	saltRef := func(envs []corev1.EnvVar, surface string) *corev1.SecretKeySelector {
		t.Helper()
		for _, e := range envs {
			if e.Name == "SESSION_KV_SALT" {
				if e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil {
					t.Fatalf("%s SESSION_KV_SALT is not secret-backed: %+v", surface, e)
				}
				return e.ValueFrom.SecretKeyRef
			}
		}
		t.Fatalf("%s renders no SESSION_KV_SALT", surface)
		return nil
	}

	gw := saltRef(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env, "gateway")

	pt := buildPodTemplateSpec(agent, "", "", "", "", nil, renderOptions{})
	var agentRef *corev1.SecretKeySelector
	for _, c := range pt.Spec.Containers {
		if c.Name == "platform-agent" {
			agentRef = saltRef(c.Env, "agent pod")
		}
	}
	if agentRef == nil {
		t.Fatal("no platform-agent container in the pod template")
	}

	for surface, ref := range map[string]*corev1.SecretKeySelector{"gateway": gw, "agent pod": agentRef} {
		if ref.Name != "customer-salts" || ref.Key != "chat-hmac" {
			t.Errorf("%s salt ref did not follow the CR override: %+v", surface, ref)
		}
	}
}

// subjectMatches implements NATS subject matching so the probe test asks the
// question the server would ask, rather than the question a substring scan can
// answer. `*` matches exactly one token; `>` matches one or more trailing
// tokens and may only be last.
func subjectMatches(pattern, subject string) bool {
	p := strings.Split(pattern, ".")
	s := strings.Split(subject, ".")
	for i, tok := range p {
		if tok == ">" {
			return i < len(s)
		}
		if i >= len(s) {
			return false
		}
		if tok != "*" && tok != s[i] {
			return false
		}
	}
	return len(p) == len(s)
}

func TestSubjectMatches(t *testing.T) {
	cases := []struct {
		pattern, subject string
		want             bool
	}{
		{"a2a.topics.shared.probe", "a2a.topics.shared.probe", true},
		{"a2a.topics.>", "a2a.topics.shared.probe", true},
		{"a2a.>", "a2a.topics.shared.probe", true},
		{"a2a.topics.shared.*", "a2a.topics.shared.probe", true},
		{"a2a.topics.*.probe", "a2a.topics.shared.probe", true},
		{"a2a.topics.shared.blueprint", "a2a.topics.shared.probe", false},
		{"a2a.tasks.>", "a2a.topics.shared.probe", false},
		{"a2a.topics.shared", "a2a.topics.shared.probe", false},
		{"a2a.topics.shared.probe.x", "a2a.topics.shared.probe", false},
		// The trap the literal-substring version of this test fell into: a
		// wildcard grants the subject without ever naming it.
		{"a2a.topics.shared.pro*", "a2a.topics.shared.probe", false}, // NATS has no partial-token globbing
	}
	for _, c := range cases {
		if got := subjectMatches(c.pattern, c.subject); got != c.want {
			t.Errorf("subjectMatches(%q, %q) = %v, want %v", c.pattern, c.subject, got, c.want)
		}
	}
}

// The probe subject is provisioned so an authorization refusal has a real
// subject to land on, and it has NO writer on purpose — the one deliberate
// exception to "a topic's subject list and its writer's grant travel
// together". If any user ever gains publish on it, the probe stops being a
// refusal test and becomes a way to write a state-class topic.
//
// Asked by SUBJECT MATCHING, not by substring: measured on a live dev bus,
// a seed user holding `a2a.>` as a convenience covered the probe subject
// without ever naming it. Nothing here holds such a wildcard today — the
// worker's topic grants name the exact provisioned list — and this test is
// what keeps that true: re-widening any publish grant to `a2a.topics.>`
// fails here rather than silently making the probe writable.
func TestProbeTopicIsProvisionedAndWriterless(t *testing.T) {
	const probe = "a2a.topics.shared.probe"
	agent := a2aTestAgent()

	script := strings.Join(buildA2AProvisionJob(agent).Spec.Template.Spec.Containers[0].Command, "\n")
	if !strings.Contains(script, probe) {
		t.Error("probe subject is not provisioned; a refusal against it would only prove the subject is missing")
	}

	conf := string(buildA2ANATSConfigSecret(agent, a2aTestCreds()).Data["nats.conf"])
	for _, user := range []string{"gateway", "worker", "seed", "web"} {
		start := strings.Index(conf, "user: "+user)
		if start < 0 {
			t.Fatalf("no %s user in nats.conf", user)
		}
		entry := conf[start:]
		if next := strings.Index(entry[1:], "user: "); next >= 0 {
			entry = entry[:next+1]
		}
		pub := entry[strings.Index(entry, "publish"):strings.Index(entry, "subscribe")]
		for _, line := range strings.Split(pub, "\n") {
			line = strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(line), ","))
			if !strings.HasPrefix(line, `"`) {
				continue
			}
			if grant := strings.Trim(line, `"`); subjectMatches(grant, probe) {
				t.Errorf("user %q can publish the probe subject via grant %q; it must have no writer", user, grant)
			}
		}
	}
}

// Without this policy every pod in the cluster reaches 4222/8222/9222 while
// the bus grants do the real refusing. The network layer now agrees with
// them: 4222 from exactly the enumerated bus clients, and no pod-network
// peer at all for 8222 (monitor) or 9222 (ws) — the demo port-forward and
// the kubelet readiness probe both enter via the node, which NetworkPolicy
// does not govern, and that is the decided posture rather than an accident.
func TestBuildA2ANATSNetworkPolicy(t *testing.T) {
	np := buildA2ANATSNetworkPolicy(a2aTestAgent())

	if np.Name != "test-agent-a2a-nats-netpol" {
		t.Errorf("unexpected name %q", np.Name)
	}
	if np.Spec.PodSelector.MatchLabels["app"] != "test-agent-a2a-nats" {
		t.Errorf("pod selector = %v, want app=test-agent-a2a-nats", np.Spec.PodSelector.MatchLabels)
	}
	if !reflect.DeepEqual(np.Spec.PolicyTypes, []networkingv1.PolicyType{networkingv1.PolicyTypeIngress}) {
		t.Errorf("policy types = %v, want ingress only", np.Spec.PolicyTypes)
	}

	if len(np.Spec.Ingress) != 1 {
		t.Fatalf("expected exactly one ingress rule, got %d: %+v", len(np.Spec.Ingress), np.Spec.Ingress)
	}
	rule := np.Spec.Ingress[0]
	if len(rule.Ports) != 1 || rule.Ports[0].Port.IntVal != 4222 || *rule.Ports[0].Protocol != corev1.ProtocolTCP {
		t.Errorf("ingress rule is not exactly TCP 4222: %+v", rule.Ports)
	}

	// The client list, pinned exactly: the agent pod (whose sidecars share
	// its labels), the A2A gateway, session pods, the provision Job, and the
	// hand-applied seed tooling. All same-namespace pod selectors — no
	// namespace-crossing, no IPBlock.
	wantPeers := []map[string]string{
		{"app": "test-agent-gateway"},
		{"app": "test-agent-a2a-gateway"},
		{labelPartOf: a2aPartOf, "app.kubernetes.io/component": "a2a-session"},
		{labelPartOf: a2aPartOf, a2aComponentLabel: "provision"},
		{labelPartOf: a2aPartOf, a2aComponentLabel: "seed"},
	}
	if len(rule.From) != len(wantPeers) {
		t.Fatalf("expected %d peers, got %d: %+v", len(wantPeers), len(rule.From), rule.From)
	}
	for i, want := range wantPeers {
		peer := rule.From[i]
		if peer.IPBlock != nil || peer.NamespaceSelector != nil {
			t.Errorf("peer %d is not a same-namespace pod selector: %+v", i, peer)
			continue
		}
		if peer.PodSelector == nil || !reflect.DeepEqual(peer.PodSelector.MatchLabels, want) {
			t.Errorf("peer %d = %+v, want %v", i, peer.PodSelector, want)
		}
	}
}

// The bus fence rides the mode switch exactly like the rest of the next
// stack: rendered by reconcileA2A under next, torn down by cleanupA2A on the
// flip back, absent from a today render entirely.
func TestA2ANATSNetworkPolicyGatedByMode(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	// finalizer pass, then the real one
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}

	nats := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-nats-netpol", Namespace: "test-ns"}, nats); err != nil {
		t.Errorf("NATS NetworkPolicy not rendered under next: %v", err)
	}

	// Flip to today: the fence goes back to dark, and the agent's own netpol
	// stays.
	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	fresh.Spec.Mode = nil
	if err := cl.Update(ctx, fresh); err != nil {
		t.Fatalf("failed to update agent: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 3 failed: %v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-nats-netpol", Namespace: "test-ns"}, nats); !errors.IsNotFound(err) {
		t.Errorf("NATS NetworkPolicy still present under today (err=%v)", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-gateway-netpol", Namespace: "test-ns"}, &networkingv1.NetworkPolicy{}); err != nil {
		t.Errorf("the agent's own NetworkPolicy vanished with the A2A cleanup: %v", err)
	}
}

// The quota is the enforcement half of the session-pod bound (the gateway's
// cap is the usability half): a namespace-wide pod count a compromised or
// buggy gateway cannot ignore, sized above the gateway's cap so users hit
// the honest chat refusal before anything hits an opaque admission failure.
func TestBuildA2ASessionQuota(t *testing.T) {
	agent := a2aTestAgent()
	quota := buildA2ASessionQuota(agent)
	if quota.Name != "test-agent-a2a-session-quota" {
		t.Fatalf("quota name %q", quota.Name)
	}
	if quota.Namespace != "test-ns" {
		t.Fatalf("quota namespace %q", quota.Namespace)
	}

	// `pods` counts non-terminal pods only - a finished worker awaiting
	// sweep must not hold a slot - and it is the ONLY key: a compute-resource
	// key (requests.*) would force requests onto every pod in the namespace,
	// which is not this bound's mandate.
	hard := quota.Spec.Hard
	if len(hard) != 1 {
		t.Fatalf("quota bounds %d resources, want exactly pods: %v", len(hard), hard)
	}
	pods, ok := hard[corev1.ResourcePods]
	if !ok {
		t.Fatalf("quota does not bound pods: %v", hard)
	}
	// Default gateway cap 10 + the fixed headroom for the rest of the
	// namespace (base stack, rollout surge, race overshoot).
	if pods.Value() != 25 {
		t.Fatalf("default quota = %d, want 25 (cap 10 + headroom 15)", pods.Value())
	}

	two := 2
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: &two}}
	pods = buildA2ASessionQuota(agent).Spec.Hard[corev1.ResourcePods]
	if pods.Value() != 17 {
		t.Fatalf("tuned quota = %d, want 17 (cap 2 + headroom 15)", pods.Value())
	}
}

// The CR field reaches the gateway as an explicit env value even when unset:
// the rendered number is the one a `kubectl describe` reader and the quota
// sizing both see, so the two halves cannot drift apart silently.
func TestGatewayDeploymentRendersMaxSessions(t *testing.T) {
	find := func(dep *appsv1.Deployment) string {
		for _, env := range dep.Spec.Template.Spec.Containers[0].Env {
			if env.Name == "A2A_MAX_SESSIONS" {
				return env.Value
			}
		}
		return ""
	}
	agent := a2aTestAgent()
	if got := find(buildA2AGatewayDeployment(agent)); got != "10" {
		t.Fatalf("default A2A_MAX_SESSIONS = %q, want \"10\"", got)
	}
	two := 2
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: &two}}
	if got := find(buildA2AGatewayDeployment(agent)); got != "2" {
		t.Fatalf("tuned A2A_MAX_SESSIONS = %q, want \"2\"", got)
	}
}

// The quota rides the mode switch like every other next-stack object: a
// today install must not carry a pod quota it never asked for.
func TestA2ASessionQuotaGatedByMode(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	// finalizer pass, then the real one
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}

	quota := &corev1.ResourceQuota{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-session-quota", Namespace: "test-ns"}, quota); err != nil {
		t.Errorf("session quota not rendered under next: %v", err)
	}

	// Flip to today: the quota goes back to dark with the stack it bounds.
	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	fresh.Spec.Mode = nil
	if err := cl.Update(ctx, fresh); err != nil {
		t.Fatalf("failed to update agent: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 3 failed: %v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-session-quota", Namespace: "test-ns"}, quota); !errors.IsNotFound(err) {
		t.Errorf("session quota still present under today (err=%v)", err)
	}
}

// TestEveryA2AContainerHasAHardenedSecurityContext is the mode-next half of
// TestEveryContainerHasAHardenedSecurityContext, which walks the agent Pod and
// stops there. These three containers are rendered by their own builders and
// sit outside that walk, which is how all three shipped without the helper --
// the provision container with no SecurityContext at all. One list, so a fourth
// A2A container has somewhere to be added and fails here until it is.
func TestEveryA2AContainerHasAHardenedSecurityContext(t *testing.T) {
	agent := newTestPlatformAgent()
	sts := buildA2ANATSStatefulSet(agent, "deadbeefdeadbeef")
	job := buildA2AProvisionJob(agent)
	dep := buildA2AGatewayDeployment(agent)

	for _, tc := range []struct {
		render string
		spec   corev1.PodSpec
	}{
		{"nats", sts.Spec.Template.Spec},
		{"provision", job.Spec.Template.Spec},
		{"gateway", dep.Spec.Template.Spec},
	} {
		t.Run(tc.render, func(t *testing.T) {
			all := append(append([]corev1.Container{}, tc.spec.InitContainers...), tc.spec.Containers...)
			if len(all) == 0 {
				t.Fatalf("%s: no containers, so this test would pass vacuously", tc.render)
			}
			for _, c := range all {
				sc := c.SecurityContext
				if sc == nil {
					t.Errorf("container %s: no SecurityContext; want hardenedSecurityContext()", c.Name)
					continue
				}
				if sc.ReadOnlyRootFilesystem == nil || !*sc.ReadOnlyRootFilesystem {
					t.Errorf("container %s: ReadOnlyRootFilesystem is not true", c.Name)
				}
				if sc.AllowPrivilegeEscalation == nil || *sc.AllowPrivilegeEscalation {
					t.Errorf("container %s: AllowPrivilegeEscalation is not false", c.Name)
				}
				if sc.Capabilities == nil || !slices.Contains(sc.Capabilities.Drop, corev1.Capability("ALL")) {
					t.Errorf("container %s: capabilities do not drop ALL, got %v", c.Name, sc.Capabilities)
				}
			}
			// Pod level: the same floor the NATS and gateway pods already set.
			// A restricted-PSA namespace rejects the pod without these.
			psc := tc.spec.SecurityContext
			if psc == nil || psc.RunAsNonRoot == nil || !*psc.RunAsNonRoot {
				t.Errorf("%s pod: RunAsNonRoot is not true", tc.render)
			}
			if psc == nil || psc.SeccompProfile == nil ||
				psc.SeccompProfile.Type != corev1.SeccompProfileTypeRuntimeDefault {
				t.Errorf("%s pod: seccomp profile is not RuntimeDefault", tc.render)
			}
		})
	}
}

// TestA2AProvisionJobConditionsDriveStatus exercises the three branches the Job
// condition scan feeds: done, failed (which the controller turns into a Degraded
// phase with A2AProvisionFailed), and neither (which requeues). All three shipped
// unexercised -- nothing seeded a Job condition -- so dropping the JobFailed case
// or inverting the ConditionTrue guard left a stream-less bus reporting Ready
// with the suite green.
func TestA2AProvisionJobConditionsDriveStatus(t *testing.T) {
	for _, tc := range []struct {
		name       string
		cond       *batchv1.JobCondition
		wantDone   bool
		wantFailed bool
	}{
		{"pending", nil, false, false},
		{"complete", &batchv1.JobCondition{
			Type: batchv1.JobComplete, Status: corev1.ConditionTrue,
		}, true, false},
		{"failed", &batchv1.JobCondition{
			Type: batchv1.JobFailed, Status: corev1.ConditionTrue,
			Reason: "BackoffLimitExceeded", Message: "Job has reached the specified backoff limit",
		}, false, true},
		// A condition present but False is not the event: the scan must skip it
		// rather than read the type alone.
		{"failed-but-false", &batchv1.JobCondition{
			Type: batchv1.JobFailed, Status: corev1.ConditionFalse,
		}, false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			agent := a2aTestAgent()
			job := buildA2AProvisionJob(agent)
			withCommonLabels(job, agent)
			if tc.cond != nil {
				job.Status.Conditions = []batchv1.JobCondition{*tc.cond}
			}
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(agent, job).
				WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
				WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
				Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

			state, err := r.reconcileA2A(context.Background(), agent)
			if err != nil {
				t.Fatalf("reconcileA2A: %v", err)
			}
			if state.done != tc.wantDone {
				t.Errorf("done = %v, want %v", state.done, tc.wantDone)
			}
			if state.failed != tc.wantFailed {
				t.Errorf("failed = %v, want %v", state.failed, tc.wantFailed)
			}
			if tc.wantFailed {
				if !strings.Contains(state.message, job.Name) {
					t.Errorf("message does not name the Job to inspect: %q", state.message)
				}
				if !strings.Contains(state.message, "BackoffLimitExceeded") {
					t.Errorf("message drops the condition reason: %q", state.message)
				}
			}
		})
	}
}

// TestCleanupA2AResumesAfterAMidPassError is the safety proof for cleanupA2A's
// early exit. The exit reads three sentinels and returns when all are absent,
// which is only sound while nothing it deletes can outlive them: the
// StatefulSet is deleted last, the gateway Deployment first, and the config
// Secret is the one object the render creates before the StatefulSet.
//
// The failure this pins is the one the optimisation invites — a pass that dies
// partway leaves objects behind, and the NEXT pass steps over them because its
// sentinels have already gone. So: fail a delete in the middle, then let a
// clean pass run, and require the tree to be empty at the end rather than
// merely error-free.
func TestCleanupA2AResumesAfterAMidPassError(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()

	failRole := true
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: fakeServerSideApplyInterceptors().Patch,
			Delete: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
				if _, isRole := obj.(*rbacv1.Role); isRole && failRole {
					return fmt.Errorf("injected: API server said no")
				}
				return c.Delete(ctx, obj, opts...)
			},
		}).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()

	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatalf("render: %v", err)
	}

	// Pass one dies on the Role. The gateway Deployment (deleted first) is
	// already gone by then; the StatefulSet (deleted last) is untouched.
	if err := r.cleanupA2A(ctx, agent); err == nil {
		t.Fatal("cleanup pass 1: want the injected error, got nil")
	}
	sts := &appsv1.StatefulSet{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-nats", Namespace: "test-ns"}, sts); err != nil {
		t.Fatalf("the sentinel must survive a failed pass, or this test proves nothing: %v", err)
	}

	// Pass two, unobstructed, must run the whole list rather than exit early.
	failRole = false
	if err := r.cleanupA2A(ctx, agent); err != nil {
		t.Fatalf("cleanup pass 2: %v", err)
	}

	for _, tc := range []struct {
		what string
		obj  client.Object
		name string
	}{
		{"gateway Deployment", &appsv1.Deployment{}, "test-agent-a2a-gateway"},
		{"gateway Role", &rbacv1.Role{}, "test-agent-a2a-gateway"},
		{"gateway RoleBinding", &rbacv1.RoleBinding{}, "test-agent-a2a-gateway"},
		{"gateway ServiceAccount", &corev1.ServiceAccount{}, "test-agent-a2a-gateway"},
		{"NATS Service", &corev1.Service{}, "test-agent-a2a-nats"},
		{"NATS config Secret", &corev1.Secret{}, "test-agent-a2a-nats-config"},
		{"NATS StatefulSet", &appsv1.StatefulSet{}, "test-agent-a2a-nats"},
	} {
		err := cl.Get(ctx, types.NamespacedName{Name: tc.name, Namespace: "test-ns"}, tc.obj)
		if !errors.IsNotFound(err) {
			t.Errorf("%s survived the resumed cleanup (err=%v) — the early exit stepped over it", tc.what, err)
		}
	}

	// A third pass on the now-empty tree is the exit doing its job.
	if err := r.cleanupA2A(ctx, agent); err != nil {
		t.Errorf("cleanup pass 3 on an empty tree: %v", err)
	}
}

// TestCleanupA2ACostsThreeReadsWhenThereIsNothingToClean measures the thing the
// change was for. Counting is the only honest check here: the early exit is a
// cost optimisation, and a correctness test passes just as well with the reads
// still happening one object at a time.
func TestCleanupA2ACostsThreeReadsWhenThereIsNothingToClean(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()

	gets, lists := 0, 0
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithInterceptorFuncs(interceptor.Funcs{
			Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
				gets++
				return c.Get(ctx, key, obj, opts...)
			},
			List: func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
				lists++
				return c.List(ctx, list, opts...)
			},
		}).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.cleanupA2A(context.Background(), agent); err != nil {
		t.Fatalf("cleanupA2A on a never-rendered install: %v", err)
	}
	// Three sentinel Gets and nothing else: no per-object walk, and in
	// particular no Job List, which is the uncached one that ran every
	// reconcile of every today install before this.
	if gets != 3 {
		t.Errorf("Gets = %d, want 3 (the sentinels); the per-object walk is running on a no-op", gets)
	}
	if lists != 0 {
		t.Errorf("Lists = %d, want 0; the provision-Job sweep is still running on a no-op", lists)
	}
}
