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
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	rbacv1 "k8s.io/api/rbac/v1"
	"maps"
	"net"
	"path"
	"reflect"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
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
			"bridge-password":  []byte("pw-bridge"),
			"seed-password":    []byte("pw-seed"),
			"web-password":     []byte("pw-web"),
			"sys-password":     []byte("pw-sys"),
			"callout-password": []byte("pw-callout"),
		},
	}
}

// The deployment spec's connect-time property: the bus decides who may say
// what before a message is read. Static users are the playground stand-in for
// the auth callout, but the deny-by-default subject lists are the real shape.
func TestBuildA2ANATSConfig(t *testing.T) {
	agent := a2aTestAgent()
	keys := a2aTestCalloutKeys(t)
	secret := buildA2ANATSConfigSecret(agent, a2aTestCreds(), keys)

	if secret.Name != "test-agent-a2a-nats-config" {
		t.Errorf("config secret name = %q", secret.Name)
	}
	if got := secret.Labels["app.kubernetes.io/part-of"]; got != a2aPartOf {
		t.Errorf("part-of label = %q, want %q", got, a2aPartOf)
	}

	conf := string(secret.Data["nats.conf"])
	// Matched as whole lines, not substrings. `Contains(conf, "timeout: 2")`
	// is satisfied by `timeout: 20` - ten times the budget the comment beside
	// it calls a ceiling - and `Contains(conf, "max_control_line:")` is
	// satisfied by a value BELOW the default it exists to raise. Both were
	// mutation-tested and both passed while wrong.
	confLine := func(want string) bool {
		for _, line := range strings.Split(conf, "\n") {
			if strings.TrimSpace(line) == want {
				return true
			}
		}
		return false
	}
	if !strings.Contains(conf, "PLAYGROUND POSTURE") {
		t.Error("nats.conf is missing the playground-posture comment block")
	}
	if !strings.Contains(conf, "jetstream") {
		t.Error("nats.conf does not enable jetstream")
	}

	// The static principals, with their inbox prefixes and their generated
	// passwords. Per-user inbox prefixes are what stop the connect-time
	// property leaking through the reply path.
	for _, user := range []string{a2aBridgeUser, "web", "gateway"} {
		if !strings.Contains(conf, "user: "+user) {
			t.Errorf("nats.conf missing static user %q", user)
		}
		if !strings.Contains(conf, "_INBOX."+user+".>") {
			t.Errorf("nats.conf missing the _INBOX prefix for %q", user)
		}
	}
	for _, pw := range []string{"pw-bridge", "pw-web", "pw-gateway"} {
		if !strings.Contains(conf, pw) {
			t.Errorf("nats.conf does not carry the generated password %q", pw)
		}
	}

	// The principals the callout issues must NOT be here. A user present in
	// both renders is authenticated by whichever path the client happened to
	// take, and the config copy would still carry a password - which is the
	// whole thing this change removes.
	for _, user := range calloutIdentities(agent) {
		if strings.Contains(conf, "user: "+user.user+"\n") {
			t.Errorf("nats.conf still carries a static block for %q, which the callout now issues", user.user)
		}
	}

	// The callout wiring itself.
	if !strings.Contains(conf, "auth_callout {") {
		t.Fatal("nats.conf has no auth_callout block")
	}
	if !strings.Contains(conf, "account: AUTH") {
		t.Error("the callout is not scoped to its own account")
	}
	// The exact keys, not their first letter. `Contains(conf, "issuer: A")`
	// matches any account key at all, including one unrelated to the callout's
	// seed - which is the mismatch that refuses every connection with a bare
	// Authorization Violation naming nothing.
	if !confLine("issuer: " + keys.IssuerPublic) {
		t.Errorf("auth_callout.issuer is not this callout's issuer public key (%s)", keys.IssuerPublic)
	}
	if !confLine("xkey: " + keys.XKeyPublic) {
		t.Errorf("auth_callout.xkey is not this callout's curve public key (%s)", keys.XKeyPublic)
	}
	// And no seed reaches the config: it would hand whoever can read the
	// config Secret the key that mints every grant on the bus.
	for _, seed := range []string{keys.IssuerSeed, keys.XKeySeed} {
		if strings.Contains(conf, seed) {
			t.Error("nats.conf carries a SEED; it must hold only public halves")
		}
	}

	// Above roughly two seconds the connect failure stops being an
	// Authorization Violation and becomes "expected 'PONG', got 'PING'".
	if !confLine("timeout: 2") {
		t.Error("authorization.timeout is not exactly 2; above it the client-side failure names nothing about authorization")
	}
	// A ServiceAccount token rides in the CONNECT frame, and the 4096 default
	// leaves under 4KB for the whole thing.
	if !confLine("max_control_line: 65536") {
		t.Error("max_control_line is not raised to 65536; below it a projected token is a hard connect refusal before authentication")
	}

	// Whole-line again: `Contains(conf, "web")` matches `websocket` and
	// `Contains(conf, "sys")` matches `system_account`, so the obvious form of
	// this check cannot fail.
	for _, id := range staticIdentities(agent) {
		if !confLine("user: " + id.user) {
			t.Errorf("static principal %q has no user block in nats.conf", id.user)
		}
	}
	// Reported rather than panicked: a slice on Index would panic with -1 if
	// the block went missing, which is a failure mode worth a message.
	authStart := strings.Index(conf, "auth_users:")
	if authStart < 0 {
		t.Fatal("nats.conf has no auth_users list; every static principal would be handed to the callout and refused")
	}
	authUsers := conf[authStart:]
	authEnd := strings.Index(authUsers, "]")
	if authEnd < 0 {
		t.Fatal("the auth_users list is unterminated")
	}
	authUsers = authUsers[:authEnd]
	for _, id := range staticIdentities(agent) {
		if !strings.Contains(authUsers, id.user) {
			t.Errorf("static principal %q is not in auth_users; it would be refused at connect", id.user)
		}
	}
	if !strings.Contains(authUsers, a2aCalloutConfUser) {
		t.Error("the callout's own user is not exempt; it could not connect to serve the subject it exists to serve")
	}
	for _, id := range calloutIdentities(agent) {
		if strings.Contains(authUsers, id.user) {
			t.Errorf("%q is exempt from the callout but is issued BY the callout", id.user)
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
// principal's in-flight delivery and destroy it. gateway and bridge ack only
// the TASKS deliveries they consume with explicit ack; seed and web create
// no acking consumer and hold no ack grant at all. Within the shared TASKS
// stream the grant cannot distinguish consumers (NATS wildcards match whole
// tokens), so any widening of this list is a review conversation, not a
// diff.
func TestSystemUsersAckGrantsAreScopedPerStream(t *testing.T) {
	agent := a2aTestAgent()

	// Asserted against the principal list, which spans both renders: gateway
	// and bridge are static entries in nats.conf, while the agent and session
	// principals are served by the callout. The agent's grants are listed
	// here; the session's are derived at mint time. An unscoped ack grant is
	// exactly as dangerous in any of them.
	want := map[string][]string{
		"gateway":       {"$JS.ACK.TASKS.>"},
		a2aBridgeUser:   {"$JS.ACK.TASKS.>"},
		a2aAgentBusUser: nil,
		"provision":     nil,
		// The session's grants are derived, so it holds no listed grant of
		// any kind, ack included. What it actually gets at mint time is
		// also ack-free: its three consumers are ack-none pulls, and an ack
		// grant on the shared TASKS stream cannot distinguish consumers, so
		// granting one would let a session +TERM the gateway's deliveries.
		"session": nil,
		"seed":    nil,
		"web":     nil,
		"sys":     nil,
	}

	for _, id := range a2aIdentities(agent) {
		wantAcks, known := want[id.user]
		if !known {
			t.Errorf("principal %q has no expected ack surface; add one rather than letting a new principal inherit silence", id.user)
			continue
		}
		var got []string
		for _, grant := range id.publish {
			if strings.HasPrefix(grant, "$JS.ACK") {
				got = append(got, grant)
			}
		}
		if !reflect.DeepEqual(got, wantAcks) {
			t.Errorf("%s ack grants = %q, want %q", id.user, got, wantAcks)
		}
		// The unscoped form is a cross-principal +TERM whoever holds it.
		if slices.Contains(got, "$JS.ACK.>") {
			t.Errorf("%s holds unscoped $JS.ACK.>", id.user)
		}
	}

	conf := string(buildA2ANATSConfigSecret(agent, a2aTestCreds(), a2aTestCalloutKeys(t)).Data["nats.conf"])
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
		// this principal may only subscribe under _INBOX.provision.> — without
		// the prefix override every CLI call times out and the Job can never
		// succeed.
		"--inbox-prefix=_INBOX.provision",
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
	for _, key := range []string{"gateway-password", a2aBridgePasswordKey, "seed-password"} {
		if len(creds.Data[key]) < 24 {
			t.Errorf("creds key %q missing or too short", key)
		}
	}
	gen1 := string(creds.Data["gateway-password"])

	jobs := &batchv1.JobList{}
	if err := cl.List(ctx, jobs); err != nil || len(jobs.Items) == 0 {
		t.Errorf("provision Job not rendered under next (err=%v, n=%d)", err, len(jobs.Items))
	}
	// The gateway waits on BusCredentialsReady (see the gate tests); under
	// next with a serving callout it renders like the rest.
	letTheGatewayThrough(t, ctx, cl, r, req, agent)
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
	letTheGatewayThrough(t, ctx, cl, r, req, agent)
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
			"bridge-password":  []byte(injected),
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
	if string(got.Data[a2aBridgePasswordKey]) == injected {
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
	conf := string(buildA2ANATSConfigSecret(a2aTestAgent(), a2aTestCreds(), a2aTestCalloutKeys(t)).Data["nats.conf"])

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
	// Recreate, not the RollingUpdate default: at one replica the default
	// resolves maxUnavailable to 0 and the roll stalls under a full quota
	// (#1506). The credential proxy makes the same choice.
	if dep.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType {
		t.Errorf("gateway Deployment strategy = %q, want %q", dep.Spec.Strategy.Type, appsv1.RecreateDeploymentStrategyType)
	}
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
	if len(role.Rules) != 2 {
		t.Fatalf("gateway Role has %d rules, want the session-pod rule plus the pinned owner get", len(role.Rules))
	}
	// The owner rule is one verb on one named object — a deployments read
	// would be a finding.
	owner := role.Rules[1]
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
// agent's topic grants name the exact provisioned list — and this test is
// what keeps that true: re-widening any publish grant to `a2a.topics.>`
// fails here rather than silently making the probe writable.
func TestProbeTopicIsProvisionedAndWriterless(t *testing.T) {
	const probe = "a2a.topics.shared.probe"
	agent := a2aTestAgent()

	script := strings.Join(buildA2AProvisionJob(agent).Spec.Template.Spec.Containers[0].Command, "\n")
	if !strings.Contains(script, probe) {
		t.Error("probe subject is not provisioned; a refusal against it would only prove the subject is missing")
	}

	// Checked against the principal list rather than by parsing nats.conf,
	// because since the callout armed there are two renders and the conf is
	// only one of them. A grant that made the probe writable from the
	// callout's identity map would be just as fatal to the probe's meaning
	// and would not appear in the config at all.
	for _, id := range a2aIdentities(agent) {
		for _, grant := range id.publish {
			if subjectMatches(grant, probe) {
				t.Errorf("principal %q can publish the probe subject via grant %q; it must have no writer", id.user, grant)
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

	// The client list, pinned exactly: the auth callout, the agent pod
	// (whose sidecars share its labels), the A2A gateway, session pods, the
	// provision Job, and the hand-applied seed tooling. All same-namespace
	// pod selectors — no namespace-crossing, no IPBlock.
	//
	// Pinned exactly, and the count is load-bearing rather than tidy: this
	// fence sits in front of every connection to the bus, and the callout
	// peer in particular is the one whose absence is invisible. Without it
	// the callout cannot reach 4222, so it answers no authorization
	// request, so no NEW connection succeeds — while every established one
	// keeps working and the bus looks healthy. A peer added without
	// updating this list is a peer nobody decided on; a peer removed is the
	// fabric going dark somewhere it takes a reconnect to notice.
	wantPeers := []map[string]string{
		{"app": "test-agent-a2a-callout"},
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
// The three halves of arming, asserted together because shipping any one
// without the others is the failure this PR's scoping exists to avoid: the
// flag without the verbs is a gateway that refuses every delegation, and the
// verbs without the flag are a standing pod-lifecycle grant with no caller.
func TestBuildA2AGatewaySpawnArming(t *testing.T) {
	agent := a2aTestAgent()

	env := map[string]corev1.EnvVar{}
	for _, e := range buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env {
		env[e.Name] = e
	}
	if env["A2A_SPAWN_SESSIONS"].Value != "true" {
		t.Errorf("A2A_SPAWN_SESSIONS = %+v, want the spawner armed", env["A2A_SPAWN_SESSIONS"])
	}
	// Rendered unconditionally, so that an install pulling from a mirror can
	// redirect the image the arming just started pulling.
	if env["A2A_WORKER_IMAGE"].Value != a2aWorkerImage() {
		t.Errorf("A2A_WORKER_IMAGE = %+v, want the resolved worker image", env["A2A_WORKER_IMAGE"])
	}
	t.Setenv(a2aWorkerImageEnvVar, "registry.example/mirror/worker:pinned")
	for _, e := range buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env {
		if e.Name == "A2A_WORKER_IMAGE" && e.Value != "registry.example/mirror/worker:pinned" {
			t.Errorf("the operator override did not reach the spawner: %+v", e)
		}
	}

	// The session identity the spawner runs pods as. The gateway has no
	// default for it and refuses to boot without it, so an unrendered value
	// here is a CrashLoopBackOff rather than a silent fallback — but the
	// name still has to be this CR's, because the callout's map is keyed on
	// exactly it.
	if env["A2A_SESSION_SERVICE_ACCOUNT"].Value != "test-agent-a2a-session" {
		t.Errorf("A2A_SESSION_SERVICE_ACCOUNT = %+v, want this CR's session ServiceAccount", env["A2A_SESSION_SERVICE_ACCOUNT"])
	}
	// And no bus password reaches the spawner any more. It projected the
	// static `worker` credential into every session pod, where the model
	// harness could read it back out of /proc/1/environ (gke-labs#1270);
	// sessions now mint their own. A re-added reference here is that hole
	// returning by way of the render.
	if _, ok := env["A2A_NATS_CREDS_SECRET"]; ok {
		t.Error("the gateway is still told the bus credentials Secret; the spawner has no use for it and naming it invites the env-injected password back")
	}

	pods := buildA2AGatewayRole(agent).Rules[0]
	if len(pods.APIGroups) != 1 || pods.APIGroups[0] != "" ||
		len(pods.Resources) != 1 || pods.Resources[0] != "pods" {
		t.Fatalf("session-pod rule targets %v/%v, want the core group's pods", pods.APIGroups, pods.Resources)
	}
	// Exactly the spawn-and-reap verbs. patch and update would let the
	// gateway edit a running session pod; pods/exec would be a route into
	// one. Neither is something the spawner does, so neither is granted.
	wantVerbs := []string{"create", "get", "list", "watch", "delete"}
	if !reflect.DeepEqual(pods.Verbs, wantVerbs) {
		t.Errorf("session-pod verbs = %v, want exactly %v", pods.Verbs, wantVerbs)
	}
	if len(pods.ResourceNames) != 0 {
		t.Errorf("session-pod rule is pinned by name (%v) — the pods do not exist yet when it creates them", pods.ResourceNames)
	}
}

func TestBuildA2ASessionNetworkPolicy(t *testing.T) {
	np := buildA2ASessionNetworkPolicy(a2aTestAgent(), []string{"10.96.0.10"})

	if np.Name != "test-agent-a2a-session-netpol" {
		t.Errorf("unexpected name %q", np.Name)
	}
	// Selects exactly the labels the spawner stamps (a2a/gateway/spawn.go),
	// which is also what the bus fence's session peer names.
	wantSel := map[string]string{
		labelPartOf:                   a2aPartOf,
		"app.kubernetes.io/component": a2aSessionComponent,
	}
	if !reflect.DeepEqual(np.Spec.PodSelector.MatchLabels, wantSel) {
		t.Errorf("pod selector = %v, want %v", np.Spec.PodSelector.MatchLabels, wantSel)
	}

	// Both policy types. The empty ingress list is the assertion: nothing
	// dials a session pod, so a listener in a worker is an accident and an
	// accident should be unreachable.
	wantTypes := []networkingv1.PolicyType{networkingv1.PolicyTypeIngress, networkingv1.PolicyTypeEgress}
	if !reflect.DeepEqual(np.Spec.PolicyTypes, wantTypes) {
		t.Errorf("policy types = %v, want %v", np.Spec.PolicyTypes, wantTypes)
	}
	if len(np.Spec.Ingress) != 0 {
		t.Errorf("session policy grants ingress: %+v", np.Spec.Ingress)
	}

	if len(np.Spec.Egress) != 3 {
		t.Fatalf("expected exactly 3 egress rules (DNS, bus, LiteLLM), got %d: %+v", len(np.Spec.Egress), np.Spec.Egress)
	}

	// Rule 1: DNS on 53 only, and to named destinations. A DNS rule with a
	// nil To is port-53 egress to every address, which is a tunnel rather
	// than name resolution.
	dns := np.Spec.Egress[0]
	if len(dns.Ports) != 2 ||
		*dns.Ports[0].Protocol != corev1.ProtocolUDP || dns.Ports[0].Port.IntVal != 53 ||
		*dns.Ports[1].Protocol != corev1.ProtocolTCP || dns.Ports[1].Port.IntVal != 53 {
		t.Errorf("DNS rule ports = %+v, want udp+tcp 53", dns.Ports)
	}
	if len(dns.To) == 0 {
		t.Fatal("DNS rule has no peer, which permits port 53 to every destination")
	}
	// Port 53 to an unbounded destination is an exfiltration channel, not
	// name resolution — the one rule here that names addresses is the one
	// that has to be bounded. Every peer is either a named pod or a host
	// route.
	for _, peer := range dns.To {
		if peer.IPBlock == nil {
			continue
		}
		if _, network, err := net.ParseCIDR(peer.IPBlock.CIDR); err != nil {
			t.Errorf("DNS peer %q is not a CIDR", peer.IPBlock.CIDR)
		} else if ones, bits := network.Mask.Size(); ones != bits {
			t.Errorf("DNS peer %q is a range, not a host: port 53 to a range is a tunnel", peer.IPBlock.CIDR)
		}
		if len(peer.IPBlock.Except) != 0 {
			t.Errorf("DNS peer %q carries an except block, which only ever widens a host route", peer.IPBlock.CIDR)
		}
	}

	// Rule 2: the bus, by pod label rather than IPBlock — a pod IP does not
	// survive a restart and a policy pinned to one stops matching silently.
	bus := np.Spec.Egress[1]
	if len(bus.Ports) != 1 || bus.Ports[0].Port.IntVal != 4222 || *bus.Ports[0].Protocol != corev1.ProtocolTCP {
		t.Errorf("bus rule is not exactly TCP 4222: %+v", bus.Ports)
	}
	if len(bus.To) != 1 || bus.To[0].PodSelector == nil ||
		bus.To[0].PodSelector.MatchLabels[a2aComponentLabel] != "nats" ||
		bus.To[0].PodSelector.MatchLabels[labelPartOf] != a2aPartOf {
		t.Errorf("bus peer does not select the NATS pods by label: %+v", bus.To)
	}

	// Rule 3: LiteLLM, the workers' only model path — they hold no API key
	// and no Workload Identity, so there is no direct 443 to a provider.
	llm := np.Spec.Egress[2]
	if len(llm.Ports) != 3 || llm.Ports[0].Port.IntVal != 80 || llm.Ports[1].Port.IntVal != 4000 || llm.Ports[2].Port.IntVal != 8080 {
		t.Errorf("LiteLLM rule ports = %+v, want tcp 80+4000+8080", llm.Ports)
	}
	if len(llm.To) != 1 || llm.To[0].PodSelector == nil || llm.To[0].PodSelector.MatchLabels["app"] != "litellm" {
		t.Errorf("LiteLLM peer = %+v, want app=litellm", llm.To)
	}

	// The refusal, stated as a refusal: outside the DNS rule nothing reaches
	// an address, only a labelled pod. An IPBlock on the bus or model rule
	// would be the widening this fence exists to prevent, and every peer that
	// names a namespace must name this agent's own.
	for i, rule := range np.Spec.Egress[1:] {
		for _, peer := range rule.To {
			if peer.IPBlock != nil {
				t.Errorf("egress rule %d carries an IPBlock peer: %+v", i+1, peer)
			}
			if peer.NamespaceSelector != nil &&
				peer.NamespaceSelector.MatchLabels["kubernetes.io/metadata.name"] != "test-ns" {
				t.Errorf("egress rule %d crosses namespaces: %+v", i+1, peer)
			}
		}
	}
}

// The session fence must not inherit the agent policy's off switch: that flag
// withholds the agent pod's own gateway policy, and reading it as permission
// to unfence the workers would make delegation the way around the very
// allowlist it governs.
func TestSessionNetworkPolicySurvivesNetworkPolicyDisabled(t *testing.T) {
	agent := a2aTestAgent()
	agent.Spec.NetworkPolicy = &agentv1alpha1.NetworkPolicySpec{
		Enabled:       ptr.To(false),
		DNSClusterIPs: []string{"10.1.2.3"},
	}

	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}

	ips := r.a2aSessionDNSClusterIPs(context.Background(), agent)
	if !reflect.DeepEqual(ips, []string{"10.1.2.3"}) {
		t.Errorf("DNS cluster IPs = %v, want the operator's documented override honoured", ips)
	}

	np := buildA2ASessionNetworkPolicy(agent, ips)
	if len(np.Spec.Egress) != 3 || len(np.Spec.Ingress) != 0 {
		t.Errorf("the fence changed shape when spec.networkPolicy.enabled=false: %+v", np.Spec)
	}
}

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
	session := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-session-netpol", Namespace: "test-ns"}, session); err != nil {
		t.Errorf("session NetworkPolicy not rendered under next: %v", err)
	}

	// Flip to today: both fences go back to dark, and the agent's own netpol
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
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-session-netpol", Namespace: "test-ns"}, session); !errors.IsNotFound(err) {
		t.Errorf("session NetworkPolicy still present under today (err=%v)", err)
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

// assertEveryA2APodBearingBuilderHasARow ties a table of A2A pods to the
// package, so the table cannot narrow silently.
//
// Two tables below say a fourth A2A container "fails here until it has a row".
// Typed out, neither did: a builder nobody listed renders a pod nothing
// reaches, which is exactly how the auth callout arrived with a row in neither.
// This reads the package source instead and requires every buildA2A* function
// returning a pod-bearing kind to be named. Same mechanism as
// TestTheWalkCallsEveryPodBearingBuilder, narrowed to the A2A stack: the other
// pod-bearing builders in this package are the agent's, and that walk covers
// them.
func assertEveryA2APodBearingBuilderHasARow(t *testing.T, rows []string) {
	t.Helper()

	covered := map[string]bool{}
	for _, row := range rows {
		covered[row] = true
	}

	found := 0
	for builder, kind := range buildersReturning(t, podBearingKinds) {
		if !strings.HasPrefix(builder, "buildA2A") {
			continue
		}
		found++
		if !covered[builder] {
			t.Errorf("%s renders a %s and has no row in this table, so its containers are asserted nowhere", builder, kind)
		}
	}
	if found == 0 {
		t.Fatal("no A2A pod-bearing builder found in this package, so this table's coverage check passed vacuously")
	}
	// The other direction. A builder with no row is reported by name above;
	// this catches the reverse, a row left behind after its builder was
	// renamed or removed, which would otherwise sit there asserting nothing.
	if found < len(rows) {
		t.Errorf("the table has %d rows but the package has %d buildA2A* pod-bearing builders; a row names one that is gone", len(rows), found)
	}
}

// TestEveryA2AContainerHasAHardenedSecurityContext is the mode-next half of
// TestEveryContainerHasAHardenedSecurityContext, which walks the agent Pod and
// stops there. These containers are rendered by their own builders and sit
// outside that walk, which is how the first three shipped without the helper --
// the provision container with no SecurityContext at all.
func TestEveryA2AContainerHasAHardenedSecurityContext(t *testing.T) {
	agent := newTestPlatformAgent()
	sts := buildA2ANATSStatefulSet(agent, "deadbeefdeadbeef")
	job := buildA2AProvisionJob(agent)
	dep := buildA2AGatewayDeployment(agent)
	callout := buildA2ACalloutDeployment(agent)

	cases := []struct {
		render  string
		builder string
		spec    corev1.PodSpec
	}{
		{"nats", "buildA2ANATSStatefulSet", sts.Spec.Template.Spec},
		{"provision", "buildA2AProvisionJob", job.Spec.Template.Spec},
		{"gateway", "buildA2AGatewayDeployment", dep.Spec.Template.Spec},
		{"callout", "buildA2ACalloutDeployment", callout.Spec.Template.Spec},
	}
	builders := make([]string, 0, len(cases))
	for _, tc := range cases {
		builders = append(builders, tc.builder)
	}
	assertEveryA2APodBearingBuilderHasARow(t, builders)

	for _, tc := range cases {
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

// TestEveryA2AContainerLandsInAWorkingDirectoryItsUserCanUse is the other half
// of the hardening above, and the half #1211 did not carry. #1259 found it on a
// real cluster: the provision pod runs nats-box as UID 1000, and nats-box ships
// WORKDIR /root with no USER because it expects to be root. Inheriting that
// WORKDIR killed every provisioning run on "stat .: permission denied" after it
// printed its JSON -- a healthy bus with no streams at all, and a client seeing
// "stream TOPICS-STATE: not found" with nothing in the render to blame.
//
// Neither unit tests nor goldens could see it, because the render was right and
// the kubelet was the one refusing. That is the argument for asserting it here
// anyway: the render is where the decision lives, even when the failure lands
// somewhere else.
//
// imageWorkDir is measured, not assumed -- `crane config <pinned tag>` on each
// image, recorded here so a reader can check the premise without pulling
// anything. Every one of these pods overrides the user, so wherever the image's
// WORKDIR is not traversable by the UID the pod imposes, the render owes an
// explicit WorkingDir. A fourth A2A container needs a row and fails here until
// it has one -- enumerated rather than asserted, by the same coverage check the
// hardening test above uses.
func TestEveryA2AContainerLandsInAWorkingDirectoryItsUserCanUse(t *testing.T) {
	agent := newTestPlatformAgent()
	sts := buildA2ANATSStatefulSet(agent, "deadbeefdeadbeef")
	job := buildA2AProvisionJob(agent)
	dep := buildA2AGatewayDeployment(agent)
	callout := buildA2ACalloutDeployment(agent)

	cases := []struct {
		render    string
		builder   string
		container string
		spec      corev1.PodSpec
		// imageWorkDir is what the pinned image ships, and traversable says
		// whether the UID the pod imposes can chdir into it.
		imageWorkDir string
		traversable  bool
		// usable is the set of directories measured usable by this pod's UID
		// on this image. Whether a path is traversable is a fact about the
		// image, which the PodSpec cannot show, so it gets pinned here rather
		// than derived. Keep it to paths someone has actually looked at.
		usable []string
		// wantWritable says the container needs a cwd it can write to, not
		// merely enter. Under ReadOnlyRootFilesystem the only writable paths
		// are the container's own non-ReadOnly mounts, so that is checkable
		// from the spec -- and it is a separate question from `usable`, which
		// a literal pin alone would not answer if the mount went away.
		wantWritable bool
	}{
		// nats:2.10-alpine -- WORKDIR /, mode 0755, so UID 1000 is fine and
		// the render owes nothing.
		{render: "nats", builder: "buildA2ANATSStatefulSet", container: "nats", spec: sts.Spec.Template.Spec,
			imageWorkDir: "/", traversable: true},
		// natsio/nats-box:0.14.5 -- WORKDIR /root, no USER, and /root is
		// drwx------ root:root. This is #1259. The cwd is also the nats CLI's
		// HOME, so it has to be writable, which leaves the emptyDir.
		{render: "provision", builder: "buildA2AProvisionJob", container: "provision", spec: job.Spec.Template.Spec,
			imageWorkDir: "/root", usable: []string{a2aProvisionWritablePath}, wantWritable: true},
		// distroless static nonroot -- WORKDIR /home/nonroot, drwx------
		// owned by 65532, and the pod runs as 1000. Latent rather than broken
		// because the gateway binary never stats ".". It writes nothing, so
		// traversable is enough, and "/" is 0755 on that image.
		{render: "gateway", builder: "buildA2AGatewayDeployment", container: "gateway", spec: dep.Spec.Template.Spec,
			imageWorkDir: "/home/nonroot", usable: []string{"/"}},
		// The same distroless static nonroot base as the gateway, measured
		// with `crane config` on both the built image and the base: WorkingDir
		// /home/nonroot, User nonroot, and this pod imposes UID 1000. Latent
		// in the same way -- the binary never stats "." and the Deployment has
		// been observed 2/2 on a cluster -- which is why it wants a row rather
		// than a shrug. "Latent" describes today's code, and the change that
		// ends it would not announce itself. The callout writes nothing, so
		// traversable is enough.
		{render: "callout", builder: "buildA2ACalloutDeployment", container: "callout", spec: callout.Spec.Template.Spec,
			imageWorkDir: "/home/nonroot", usable: []string{"/"}},
	}
	builders := make([]string, 0, len(cases))
	for _, tc := range cases {
		builders = append(builders, tc.builder)
	}
	assertEveryA2APodBearingBuilderHasARow(t, builders)

	for _, tc := range cases {
		t.Run(tc.render, func(t *testing.T) {
			all := append(append([]corev1.Container{}, tc.spec.InitContainers...), tc.spec.Containers...)
			if len(all) == 0 {
				t.Fatalf("%s: no containers, so this test would pass vacuously", tc.render)
			}
			idx := slices.IndexFunc(all, func(c corev1.Container) bool { return c.Name == tc.container })
			if idx < 0 {
				t.Fatalf("%s: no container named %q; the table is stale", tc.render, tc.container)
			}
			c := all[idx]

			if tc.traversable {
				return
			}
			if c.WorkingDir == "" {
				uid := "the pod's user"
				if tc.spec.SecurityContext != nil && tc.spec.SecurityContext.RunAsUser != nil {
					uid = fmt.Sprintf("UID %d", *tc.spec.SecurityContext.RunAsUser)
				}
				t.Fatalf("container %s inherits the image's WORKDIR (%s) while the pod runs as %s, "+
					"which cannot chdir into it", c.Name, tc.imageWorkDir, uid)
			}
			// Non-empty is not the assertion. #1259 was a WorkingDir the
			// image supplied and the pod's UID could not enter, so the check
			// that matters is which directory, against the ones measured
			// usable on this image.
			dir := path.Clean(c.WorkingDir)
			if !slices.Contains(tc.usable, dir) {
				t.Errorf("container %s: WorkingDir %q is not one of the directories measured "+
					"usable by this pod's UID on this image (%v). The image ships WORKDIR %s, "+
					"which is why this container declares one at all. If %q really is usable, "+
					"measure it and add it to the row.",
					c.Name, c.WorkingDir, tc.usable, tc.imageWorkDir, c.WorkingDir)
			}
			if !tc.wantWritable {
				return
			}
			// A writable cwd under ReadOnlyRootFilesystem means one of the
			// container's own mounts, and a ReadOnly mount is no better than
			// the read-only root it sits on. Checked separately from the pin
			// above so that dropping the mount fails here even though the
			// literal still matches.
			mount := slices.IndexFunc(c.VolumeMounts, func(m corev1.VolumeMount) bool {
				return path.Clean(m.MountPath) == dir
			})
			if mount < 0 {
				t.Errorf("container %s: WorkingDir %q has to be writable -- it is also the "+
					"nats CLI's HOME -- but it names no mount, and the hardened context makes "+
					"everything else read-only", c.Name, c.WorkingDir)
				return
			}
			if m := c.VolumeMounts[mount]; m.ReadOnly {
				t.Errorf("container %s: WorkingDir %q is mount %q, which is ReadOnly",
					c.Name, c.WorkingDir, m.Name)
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
//
// The two failed cases split on the condition's reason, which is what decides
// whether the message carries the consumer remedy. Only the deterministic
// refusal exits 2, only exit 2 matches the podFailurePolicy, and only a Job the
// policy failed carries PodFailurePolicy; a transient failure that spends the
// backoffLimit arrives as BackoffLimitExceeded and must get no stream edit
// prescribed for it.
func TestA2AProvisionJobConditionsDriveStatus(t *testing.T) {
	for _, tc := range []struct {
		name       string
		cond       *batchv1.JobCondition
		wantDone   bool
		wantFailed bool
		wantRemedy bool
	}{
		{name: "pending"},
		{name: "complete", cond: &batchv1.JobCondition{
			Type: batchv1.JobComplete, Status: corev1.ConditionTrue,
		}, wantDone: true},
		{name: "failed-backoff-limit", cond: &batchv1.JobCondition{
			Type: batchv1.JobFailed, Status: corev1.ConditionTrue,
			Reason:  batchv1.JobReasonBackoffLimitExceeded,
			Message: "Job has reached the specified backoff limit",
		}, wantFailed: true},
		{name: "failed-pod-failure-policy", cond: &batchv1.JobCondition{
			Type: batchv1.JobFailed, Status: corev1.ConditionTrue,
			Reason:  batchv1.JobReasonPodFailurePolicy,
			Message: "Container provision for pod test/x failed with exit code 2 matching FailJob rule at index 0",
		}, wantFailed: true, wantRemedy: true},
		// A condition present but False is not the event: the scan must skip it
		// rather than read the type alone.
		{name: "failed-but-false", cond: &batchv1.JobCondition{
			Type: batchv1.JobFailed, Status: corev1.ConditionFalse,
		}},
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
				if !strings.Contains(state.message, tc.cond.Reason) {
					t.Errorf("message drops the condition reason %q: %q", tc.cond.Reason, state.message)
				}
				// The message is the only thing a refusal puts in
				// `kubectl describe`, and the script's closing block
				// refuses installs whose bus is complete. So whatever
				// the cause, it must not promise an empty bus and must
				// not offer a Job delete as the remedy.
				for _, untrue := range []string{"the bus has no streams", "deleting the Job retries"} {
					if strings.Contains(state.message, untrue) {
						t.Errorf("message claims %q, which is false of a closing-block refusal — that bus is fully provisioned and one limit short: %q", untrue, state.message)
					}
				}
			}
			if tc.wantFailed && !tc.wantRemedy {
				// A reason that is not PodFailurePolicy is not the
				// exit-2 refusal, so prescribing the consumer remedy
				// for it is a wrong answer that costs something. The
				// cause may be a NATS outage, and both ways out of
				// the refusal give something up: one lowers the
				// concurrency the install advertises, the other
				// deletes a stream holding task history. `nats stream
				// edit` is in the list because the remedy naming it
				// was wrong for a second reason and could come back
				// as a well-meaning revert. The gateway restart is
				// there for the same reason as the other two: it is
				// the last step of the recreate, and prescribing it
				// against a NATS outage tells an operator to bounce
				// the one component whose durable is fine.
				for _, unwanted := range []string{"delete the TASKS stream", "lower maxSessions", "nats stream edit", "kubectl rollout restart"} {
					if strings.Contains(state.message, unwanted) {
						t.Errorf("a %s failure names %q, which only the exit-2 refusal needs: %q", tc.cond.Reason, unwanted, state.message)
					}
				}
			}
			if tc.wantRemedy {
				// Both ways out, and the second half that makes
				// either of them land. Neither one changes anything
				// the operator can see on its own: reconcileA2A reads
				// the Job's condition, and a Failed Job stays Failed.
				// So the message has to say what re-runs the script -
				// or an operator lowers maxSessions, watches the CR
				// stay Degraded, and concludes the change was wrong.
				//
				// And the recreate's last step, which is the one
				// an operator cannot infer: deleting a stream
				// deletes every consumer on it, and the gateway's
				// event relay and the Hermes bridge hold durables
				// there that their client does not re-create --
				// lib.Client.SubscribeDurable consumes with no
				// jetstream.ConsumeErrHandler, so the deleted
				// consumer ends the subscription silently. The
				// remedy without the restart produces a gateway
				// that spawns session pods and relays no events
				// while this very condition has gone back to
				// Ready, which is a worse place than the refusal.
				//
				// The restart is asserted as the whole command,
				// down to the workload it names. A restart of the
				// wrong Deployment is worse than none: the callout
				// and the agent workload both exist and both
				// restart cleanly, so an operator who is sent to
				// one of those watches the command succeed, sees
				// the relay still dead, and has no reason to
				// suspect the instruction. Only the A2A gateway
				// holds the relay durable.
				for _, want := range []string{
					"maxSessions",
					"delete the TASKS stream",
					"Delete the Job to re-run it now",
					fmt.Sprintf("kubectl rollout restart deployment/%s -n %s", a2aGatewayName(agent), agent.Namespace),
				} {
					if !strings.Contains(state.message, want) {
						t.Errorf("message does not name %q, so the only remedy for a consumer refusal is in a pod log: %q", want, state.message)
					}
				}
				// The half that needs none of the above. Lowering
				// maxSessions moves required_consumers in the
				// render, and the Job's name digests the render,
				// so a new Job appears and runs on its own. The
				// message used to tell the operator that neither
				// way out cleared itself, which sent the one who
				// took the cheap branch looking for something to
				// delete.
				if strings.Contains(state.message, "Neither way out clears this on its own") {
					t.Errorf("message claims neither remedy clears itself, which is false of the maxSessions edit -- it re-renders the Job: %q", state.message)
				}
				// And it must not go back to naming the stream edit
				// it used to name. nats-server refuses a
				// max_consumers change on a stream that exists -
				// "stream configuration update can not change
				// MaxConsumers", in every release this operator's
				// pinned nats:2.10 bus can be - so that remedy sent
				// operators to a command that could only fail. The
				// flag and not the command: the script's
				// max_msgs_per_subject report names a `nats stream
				// edit` that IS legal.
				if strings.Contains(state.message, "--max-consumers=") {
					t.Errorf("message prescribes a --max-consumers= edit, which nats-server refuses on a stream that exists: %q", state.message)
				}
				// The number it does name is the width a fresh render
				// creates, not the raw budget. This agent takes the
				// default maxSessions, so its budget (46) sits below
				// the floor its TASKS renders at (64), and the
				// refusal is reached as readily by an operator
				// upgrade whose stream is already at that floor. A
				// recreate at the budget would hand that operator a
				// NARROWER stream than the one they deleted, which is
				// the downward derivation the render refuses to make.
				width := remedyRecreateWidth(t, state.message)
				if width < a2aTasksMaxConsumersFloor {
					t.Errorf("remedy recreates TASKS at %d, below the shipped floor %d: run against a default install's stream, that TIGHTENS it: %q",
						width, a2aTasksMaxConsumersFloor, state.message)
				}
				if width < a2aTasksConsumerBudget(agent) {
					t.Errorf("remedy recreates TASKS at %d but the budget is %d, so the stream it describes does not clear the script's own gate: %q",
						width, a2aTasksConsumerBudget(agent), state.message)
				}
			}
		})
	}
}

// remedyRecreateWidth reads the number out of the status message's "delete the
// TASKS stream and let provisioning recreate it at N" remedy. It fails rather
// than returning a zero value: a parse that quietly answered 0 would make every
// floor assertion above pass on a message that had stopped naming a number.
func remedyRecreateWidth(t *testing.T, message string) int {
	t.Helper()
	const marker = "recreate it at "
	i := strings.Index(message, marker)
	if i < 0 {
		t.Fatalf("no %q in the status message, so its recreate remedy names no width: %q", marker, message)
	}
	rest := message[i+len(marker):]
	end := strings.IndexFunc(rest, func(r rune) bool { return r < '0' || r > '9' })
	if end == 0 {
		t.Fatalf("%q in the status message is not followed by a number: %q", marker, message)
	}
	if end > 0 {
		rest = rest[:end]
	}
	n, err := strconv.Atoi(rest)
	if err != nil {
		t.Fatalf("parsing the remedy's recreate width from %q: %v", message, err)
	}
	return n
}

// TestCleanupA2AResumesAfterAMidPassError is the safety proof for cleanupA2A's
// early exit. The exit reads four sentinels and returns when all are absent,
// which is only sound while nothing it deletes can outlive them: the
// StatefulSet is deleted last, the gateway Deployment first, and the callout
// keys and config Secrets are the deletable objects the render creates first.
// TestTheEarlyExitSeesEveryObjectTheRenderCreatesFirst holds that soundness
// one object at a time; this one holds it across a pass that dies partway.
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

	// The gateway is the first object cleanupA2A deletes, so it is the object
	// this test's early-exit argument turns on -- and reconcileA2A withholds
	// its creation until BusCredentialsReady is True. Driving reconcileA2A
	// directly never publishes that condition, so without this the gateway is
	// never created and the "it is gone after cleanup" row below asserts the
	// absence of an object the render never made.
	busCredentialsAreReady(agent)
	// The teardown list carries the inject door's four objects, which render
	// only under the operator's flag; set it, so the precondition below
	// covers them rather than failing on objects the render was told not to
	// make.
	t.Setenv(a2aInjectBackendEnvVar, "true")

	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatalf("render: %v", err)
	}

	// The render has to have produced what the cleanup is asked to remove.
	// Checked for the whole teardown list, not the gateway alone: every row of
	// the IsNotFound table below is satisfied by an object that was never
	// created, so this is what makes that table a proof rather than a
	// restatement of what the render skipped.
	for _, entry := range r.a2aNamespacedTeardown(agent) {
		if err := entry.reader.Get(ctx, client.ObjectKeyFromObject(entry.obj), entry.obj); err != nil {
			t.Fatalf("%T %s was not rendered, so cleanup deleting it proves nothing: %v",
				entry.obj, entry.obj.GetName(), err)
		}
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

// TestCleanupA2ACostsFourReadsWhenThereIsNothingToClean measures the thing the
// change was for. Counting is the only honest check here: the early exit is a
// cost optimisation, and a correctness test passes just as well with the reads
// still happening one object at a time.
func TestCleanupA2ACostsFourReadsWhenThereIsNothingToClean(t *testing.T) {
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
	// Four sentinel Gets and nothing else: no per-object walk, and in
	// particular no Job List, which is the uncached one that ran every
	// reconcile of every today install before this.
	//
	// The literal moved 3 -> 4 when the callout keys Secret joined the
	// sentinels. Raising it is a real decision — every today install pays it
	// on every reconcile, forever — so it is spelled out rather than derived.
	// The inequality below is the part that must hold whatever the literal is:
	// the exit is only worth having while it costs less than the walk.
	if gets != 4 {
		t.Errorf("Gets = %d, want 4 (the sentinels); the per-object walk is running on a no-op", gets)
	}
	if walk := len(r.a2aNamespacedTeardown(agent)); gets >= walk {
		t.Errorf("Gets = %d for an exit that saves a %d-object walk; the exit has stopped paying for itself", gets, walk)
	}
	if lists != 0 {
		t.Errorf("Lists = %d, want 0; the provision-Job sweep is still running on a no-op", lists)
	}
}

// TestTheEarlyExitSeesTheResidueOfARenderThatDiedAnywhere is the correctness
// half of the optimisation the test above prices.
//
// cleanupA2A answers "is there anything to tear down?" from four objects. That
// is sound only while every render that leaves residue leaves at least one of
// the four, and the case that breaks it is not a full render -- it is a render
// that died partway. Miss it and an A2A object stays alive on a today install,
// which is the darkness property.
//
// The reachable partial renders are the prefixes of reconcileA2A's own order,
// so that is what this walks: fail the Nth object the render writes, for every
// N, then flip to today and require the namespace to come back clean. Derived
// from the render rather than listed here, so an object inserted anywhere in
// reconcileA2A -- including ahead of the current first sentinel, which is the
// way this breaks -- gets a case for free and reds until the exit can see it.
func TestTheEarlyExitSeesTheResidueOfARenderThatDiedAnywhere(t *testing.T) {
	// The one documented survivor: the per-user creds Secret is created before
	// anything the teardown deletes and is meant to outlive a flip.
	const residue = "test-agent-a2a-nats-creds"

	// buildClient returns a client whose Nth object write fails. failAt 0 never
	// fails, which is how the render's length is measured.
	buildClient := func(agent *agentv1alpha1.PlatformAgent, failAt int, writes *int) client.WithWatch {
		ssa := fakeServerSideApplyInterceptors().Patch
		stop := func() error {
			*writes++
			if *writes == failAt {
				return fmt.Errorf("injected: the render dies on write %d", failAt)
			}
			return nil
		}
		return fake.NewClientBuilder().
			WithScheme(setupScheme()).
			WithObjects(agent.DeepCopy()).
			WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
			WithInterceptorFuncs(interceptor.Funcs{
				Create: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
					if err := stop(); err != nil {
						return err
					}
					return cl.Create(ctx, obj, opts...)
				},
				Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
					if err := stop(); err != nil {
						return err
					}
					return ssa(ctx, cl, obj, patch, opts...)
				},
			}).
			Build()
	}

	scheme := setupScheme()
	next := a2aTestAgent()

	full := 0
	unobstructed := &PlatformAgentReconciler{Client: buildClient(next, 0, &full), Scheme: scheme}
	if _, err := unobstructed.reconcileA2A(context.Background(), next.DeepCopy()); err != nil {
		t.Fatalf("unobstructed render: %v", err)
	}
	if full == 0 {
		t.Fatal("the render wrote nothing; every case below would be vacuous")
	}

	for n := 1; n <= full; n++ {
		t.Run(fmt.Sprintf("render_dies_on_write_%d_of_%d", n, full), func(t *testing.T) {
			writes := 0
			cl := buildClient(next, n, &writes)
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
			ctx := context.Background()

			if _, err := r.reconcileA2A(ctx, next.DeepCopy()); err == nil {
				t.Fatal("want the injected error, got nil: the render did not die where this case says it did")
			}

			today := next.DeepCopy()
			today.Spec.Mode = nil
			if err := r.cleanupA2A(ctx, today); err != nil {
				t.Fatalf("cleanupA2A after a partial render: %v", err)
			}

			var leftovers []string
			sweepA2ALabelled(ctx, t, cl, func(kind, name string) {
				if kind == "Secret" && name == residue {
					return
				}
				leftovers = append(leftovers, kind+"/"+name)
			})
			if len(leftovers) > 0 {
				t.Errorf("a render that died on write %d leaves these on a today install: %v\n"+
					"The early exit returned before the walk because none of its sentinels was present. "+
					"Add the object to the sentinel list in cleanupA2A, or key the exit on something "+
					"that does not have to be re-derived every time the render grows a step.", n, leftovers)
			}
		})
	}
}

func TestBuildNetworkPolicyBusEgressGatedOnMode(t *testing.T) {
	findBusRule := func(np *networkingv1.NetworkPolicy) *networkingv1.NetworkPolicyEgressRule {
		for i := range np.Spec.Egress {
			for _, p := range np.Spec.Egress[i].Ports {
				if p.Port != nil && p.Port.IntVal == 4222 {
					return &np.Spec.Egress[i]
				}
			}
		}
		return nil
	}

	today := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
	if rule := findBusRule(buildNetworkPolicy(today, nil, defaultTestNetpolProfile(), false, "", false)); rule != nil {
		t.Errorf("mode absent rendered a bus egress rule: %+v", rule)
	}

	rule := findBusRule(buildNetworkPolicy(a2aTestAgent(), nil, defaultTestNetpolProfile(), false, "", false))
	if rule == nil {
		t.Fatal("mode next rendered no 4222 egress rule to the NATS pods")
	}
	if len(rule.To) != 1 {
		t.Fatalf("expected exactly one peer on the bus egress rule, got %d", len(rule.To))
	}
	peer := rule.To[0]
	if peer.IPBlock != nil {
		t.Error("bus egress peer is an IPBlock; the rule must select the NATS pods by label")
	}
	if peer.PodSelector == nil || peer.PodSelector.MatchLabels[a2aComponentLabel] != "nats" {
		t.Errorf("bus egress peer does not select %s=nats: %+v", a2aComponentLabel, peer)
	}
	if peer.PodSelector.MatchLabels[labelPartOf] != a2aPartOf {
		t.Errorf("bus egress peer does not pin %s=%s: %+v", labelPartOf, a2aPartOf, peer)
	}
	if len(rule.Ports) != 1 || rule.Ports[0].Protocol == nil || *rule.Ports[0].Protocol != corev1.ProtocolTCP {
		t.Errorf("bus egress rule is not exactly TCP 4222: %+v", rule.Ports)
	}
}

// The agent container gets NATS_URL and A2A_BUS_USER under next, and its bus
// credential as a projected ServiceAccount token — never a password, never on
// the PVC, and never in a profile .env (a second place to rotate and a first
// place to leak). Today's render has none of it.
//
// The negative half is the part A5 added and the part worth keeping: NATS_USER
// and NATS_PASSWORD must be absent under next as well as under today. A change
// that put the token in and left the password behind would pass every positive
// assertion here while the shared credential was still mounted.
func TestBuildPodTemplateSpecBusEnvGatedOnMode(t *testing.T) {
	agentEnv := func(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
		pt := buildPodTemplateSpec(agent, "", "", "", "", nil, renderOptions{})
		for _, c := range pt.Spec.Containers {
			if c.Name == "platform-agent" {
				return c.Env
			}
		}
		t.Fatal("no platform-agent container in the pod template")
		return nil
	}
	find := func(env []corev1.EnvVar, name string) *corev1.EnvVar {
		for i := range env {
			if env[i].Name == name {
				return &env[i]
			}
		}
		return nil
	}

	today := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
	todayEnv := agentEnv(today)
	for _, name := range []string{"NATS_URL", a2aBusUserEnv, "NATS_USER", "NATS_PASSWORD"} {
		if v := find(todayEnv, name); v != nil {
			t.Errorf("mode absent rendered %s onto the agent container", name)
		}
	}
	if podVolume(buildPodTemplateSpec(today, "", "", "", "", nil, renderOptions{}), a2aBusTokenVolume) != nil {
		t.Error("mode absent projected a bus token onto the agent pod")
	}

	nextEnv := agentEnv(a2aTestAgent())
	if v := find(nextEnv, "NATS_URL"); v == nil || v.Value != "nats://test-agent-a2a-nats.test-ns.svc:4222" {
		t.Errorf("NATS_URL = %+v, want the rendered NATS Service address", v)
	}
	if v := find(nextEnv, a2aBusUserEnv); v == nil || v.Value != a2aAgentBusUser {
		t.Errorf("%s = %+v, want %q — the principal whose _INBOX prefix the client has to pin", a2aBusUserEnv, v, a2aAgentBusUser)
	}
	for _, name := range []string{"NATS_USER", "NATS_PASSWORD"} {
		if v := find(nextEnv, name); v != nil {
			t.Errorf("%s = %+v under next; A5 moved this container onto a projected token and the "+
				"static credential must not render alongside it", name, v)
		}
	}

	// The credential itself: a projected token volume on the pod, mounted
	// read-only into the agent container and audience-bound to the bus.
	next := buildPodTemplateSpec(a2aTestAgent(), "", "", "", "", nil, renderOptions{})
	vol := podVolume(next, a2aBusTokenVolume)
	if vol == nil {
		t.Fatalf("no %s volume on the agent pod; the container has a bus address and no credential", a2aBusTokenVolume)
	}
	if vol.Projected == nil || len(vol.Projected.Sources) != 1 || vol.Projected.Sources[0].ServiceAccountToken == nil {
		t.Fatalf("%s is not a single projected ServiceAccount token: %+v", a2aBusTokenVolume, vol)
	}
	if aud := vol.Projected.Sources[0].ServiceAccountToken.Audience; aud != a2aBusTokenAudience {
		t.Errorf("bus token audience = %q, want %q; a default-audience token would be every readable "+
			"token in the cluster", aud, a2aBusTokenAudience)
	}

	var mounts []corev1.VolumeMount
	for _, c := range next.Spec.Containers {
		if c.Name == "platform-agent" {
			mounts = c.VolumeMounts
		}
	}
	var mounted *corev1.VolumeMount
	for i := range mounts {
		if mounts[i].Name == a2aBusTokenVolume {
			mounted = &mounts[i]
		}
	}
	if mounted == nil {
		t.Fatalf("the bus token is projected onto the pod but not mounted into platform-agent")
	}
	if mounted.MountPath != a2aBusTokenPath || !mounted.ReadOnly {
		t.Errorf("bus token mount = %+v, want %s read-only", mounted, a2aBusTokenPath)
	}
}

// podVolume returns the named volume from a pod template, or nil.
func podVolume(pt corev1.PodTemplateSpec, name string) *corev1.Volume {
	for i := range pt.Spec.Volumes {
		if pt.Spec.Volumes[i].Name == name {
			return &pt.Spec.Volumes[i]
		}
	}
	return nil
}

// Adversarial-review finding, reproduced before fixing, and it outlived the
// credential it was found against: the container holds a bus credential
// whatever the address says, so a plugin that could set NATS_URL would have the
// client hand it to a server of the plugin's choosing, in the CONNECT frame, in
// the clear, out through the 443-to-anywhere egress rule. That was the worker
// password; since A5 it is a projected token, which is audience-bound and so
// does not authenticate anywhere else — but it still names this ServiceAccount
// to whoever catches it, and A2A_BUS_USER joined the list on top. And the operator cannot
// simply append its own values over a plugin's: an appended name does not
// shadow a same-named plugin entry, it duplicates it, and server-side apply
// refuses a duplicate env key — which would wedge every reconcile of the CR.
// So while the surface is up the three names are dropped from plugin env
// before the merge, and the operator's appended values are the only entries.
func TestPluginCannotOverrideBusEnv(t *testing.T) {
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "evil", Namespace: "test-ns"},
		Spec: agentv1alpha1.AgentPluginSpec{
			Env: []corev1.EnvVar{
				{Name: "NATS_URL", Value: "nats://attacker.example:443"},
				{Name: a2aBusUserEnv, Value: "gateway"},
				{Name: a2aBusTokenFileEnv, Value: "/opt/data/attacker/token"},
				{Name: "NATS_USER", Value: "gateway"},
				{Name: "NATS_PASSWORD", Value: "hunter2"},
				{Name: "PLUGIN_OWN_KEY", Value: "kept"},
			},
		},
	}
	pt := buildPodTemplateSpec(a2aTestAgent(), "", "", "", "", []*agentv1alpha1.AgentPlugin{plugin}, renderOptions{})

	var agentEnv []corev1.EnvVar
	for _, c := range pt.Spec.Containers {
		if c.Name == "platform-agent" {
			agentEnv = c.Env
		}
	}
	counts := map[string]int{}
	values := map[string]corev1.EnvVar{}
	for _, e := range agentEnv {
		counts[e.Name]++
		values[e.Name] = e
	}
	// Exactly one entry per bus name: a duplicate would not be "last value
	// wins" at the kubelet — server-side apply refuses the whole Deployment,
	// wedging reconcile with no Degraded status to say why.
	for _, name := range []string{"NATS_URL", a2aBusUserEnv} {
		if counts[name] != 1 {
			t.Errorf("%s appears %d times in the agent env; a duplicate key is refused by "+
				"server-side apply and freezes the reconcile", name, counts[name])
		}
	}
	// The three the operator never renders into this container. Dropped rather
	// than overwritten, so the assertion is absence. A surviving plugin
	// NATS_PASSWORD is not a static login as this principal: nats.conf lists no
	// `agent` in auth_users, so the connect reaches the callout, which reads
	// ConnectOptions.Password as a bearer token and TokenReviews it. It is
	// still what the CLI offers when no token is on disk, so what it buys is a
	// refused connect rather than a second way in. NATS_USER is the identity
	// the CLI reads when A2A_BUS_USER is absent, so a plugin that set both
	// would choose the principal. And a surviving A2A_BUS_TOKEN_FILE would be
	// the file the CLI presents as a token instead of the projected one, which
	// it prefers unconditionally and with no fallback.
	for _, name := range []string{"NATS_USER", "NATS_PASSWORD", a2aBusTokenFileEnv} {
		if counts[name] != 0 {
			t.Errorf("a plugin's %s survived into the agent env under next", name)
		}
	}
	if got := values["NATS_URL"].Value; got != "nats://test-agent-a2a-nats.test-ns.svc:4222" {
		t.Errorf("a plugin redirected the bus: NATS_URL = %q", got)
	}
	if got := values[a2aBusUserEnv].Value; got != a2aAgentBusUser {
		t.Errorf("a plugin changed the bus identity: %s = %q", a2aBusUserEnv, got)
	}
	if counts["PLUGIN_OWN_KEY"] != 1 || values["PLUGIN_OWN_KEY"].Value != "kept" {
		t.Error("the bus-name reservation dropped a plugin variable it has no claim on")
	}
	for _, name := range []string{"NATS_URL", a2aBusUserEnv, a2aBusTokenFileEnv, "NATS_USER", "NATS_PASSWORD"} {
		if _, sensitive := agentv1alpha1.SensitiveEnvVars[name]; !sensitive {
			t.Errorf("%s is not in SensitiveEnvVars; the drop above covers plugin env only, and membership "+
				"is what keeps the name out of the sidecar containers, which take spec.deployment.env "+
				"through mergeCredentialProxyEnv rather than through safeSandboxEnvOverrides' allowlist", name)
		}
	}

	// Under today the reservation is off and the plugin's variables pass
	// through untouched — dropping a name only the next stack cares about
	// would be one more way for a normal install to tell the feature exists.
	today := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
	pt = buildPodTemplateSpec(today, "", "", "", "", []*agentv1alpha1.AgentPlugin{plugin}, renderOptions{})
	for _, c := range pt.Spec.Containers {
		if c.Name != "platform-agent" {
			continue
		}
		found := false
		for _, e := range c.Env {
			if e.Name == "NATS_URL" && e.Value == "nats://attacker.example:443" {
				found = true
			}
		}
		if !found {
			t.Error("today dropped a plugin's NATS_URL; the reservation must be gated on the A2A surface")
		}
	}
}

// The reconciler FREEZES the A2A objects on version skew rather than cleaning
// them up, so the agent-side half must freeze with them. Fail-closed here
// would strand a running bus behind an agent that just lost its credentials
// and its egress rule — a dial that hangs to the timeout, which is the
// failure this whole change exists to prevent.
func TestSkewPreservesTheAgentBusSurface(t *testing.T) {
	skewed := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec:       agentv1alpha1.PlatformAgentSpec{Mode: ptr.To("quantum")},
	}

	np := buildNetworkPolicy(skewed, nil, defaultTestNetpolProfile(), false, "", false)
	found := false
	for _, rule := range np.Spec.Egress {
		for _, p := range rule.Ports {
			if p.Port != nil && p.Port.IntVal == 4222 {
				found = true
			}
		}
	}
	if !found {
		t.Error("skew removed the bus egress rule while the bus keeps running")
	}

	pt := buildPodTemplateSpec(skewed, "", "", "", "", nil, renderOptions{})
	names := map[string]bool{}
	for _, c := range pt.Spec.Containers {
		if c.Name != "platform-agent" {
			continue
		}
		for _, e := range c.Env {
			names[e.Name] = true
		}
	}
	for _, want := range []string{"NATS_URL", a2aBusUserEnv} {
		if !names[want] {
			t.Errorf("skew removed %s while the bus keeps running", want)
		}
	}
	// And the credential with them. A projected token is strictly better here
	// than the SecretKeyRef it replaced: the old one needed Optional so a
	// today-lineage install that hit skew would not roll into
	// CreateContainerConfigError against a Secret that had never existed,
	// while the kubelet mints this from the pod's own ServiceAccount, which
	// exists on every lineage.
	if podVolume(pt, a2aBusTokenVolume) == nil {
		t.Error("skew removed the bus token while the bus keeps running; the container would dial with no credential")
	}

	// The managed .env still reports today — the agent-side gate is
	// fail-closed by design, so the SKILL does not appear on a skewed today
	// install even though the wiring is preserved.
	if got := renderManagedEnv(skewed); !strings.Contains(got, "KUBEAGENTS_MODE=today") {
		t.Errorf("skew should still pin the mode as today in the managed env, got %q", got)
	}
}

// TestNoAgentSidePrincipalCanPublishToTheDirectory pins the identity plane
// against its least trusted principals.
//
// a2a.agents.{profile} is last-value, so a single publish REPLACES a profile's
// card and an agent-closed tombstone retires it. The payload spec assigns that to
// the profile's owner explicitly -- "not by workers" -- and nothing in the tree
// publishes a card at all today, so the grant this removes had no caller.
//
// Both halves of the A5 split are asked, and the agent is the one that matters:
// it is the container running model output, and its three topic grants are the
// literal list a consolidating wildcard would replace. The bridge is asked too
// because it is the principal that was holding those grants until A5.
//
// Asserted against those publish lists specifically rather than against the
// whole config: the gateway keeps SUBSCRIBE on the same subjects, which is the
// read discovery needs, and a test that just greps for "a2a.agents" would fail
// on that legitimate line.
// a2aGrantSubjects returns the subjects in one user's publish or subscribe
// allow-list, parsed the way the server reads them.
//
// Parsed rather than grepped, for two reasons this test hit directly. A
// rationale comment left in place of a removed grant names the subject it
// removes, so raw text reports the comment as the grant -- which it did, on
// the first run. And the reverse: commenting a grant out is the house style
// for disabling one here, so a control asserting a grant is present has to
// see the comment marker too, or it passes against a config that lost the
// grant.
func a2aGrantSubjects(t *testing.T, conf, user, section string) []string {
	t.Helper()

	start := strings.Index(conf, "user: "+user)
	if start < 0 {
		t.Fatalf("no %s user in the rendered config", user)
	}
	entry := conf[start:]
	if next := strings.Index(entry[1:], "user: "); next >= 0 {
		entry = entry[:next+1]
	}
	openIdx := strings.Index(entry, section+" { allow = [")
	if openIdx < 0 {
		t.Fatalf("%s has no %s allow-list", user, section)
	}
	closeIdx := strings.Index(entry[openIdx:], "] }")
	if closeIdx < 0 {
		t.Fatalf("%s's %s allow-list is unterminated", user, section)
	}

	var subjects []string
	for _, line := range strings.Split(entry[openIdx:openIdx+closeIdx], "\n") {
		line = strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(line), ","))
		if !strings.HasPrefix(line, `"`) {
			continue
		}
		subjects = append(subjects, strings.Trim(line, `"`))
	}
	if len(subjects) == 0 {
		t.Fatalf("%s's %s allow-list parsed empty", user, section)
	}
	return subjects
}

func TestNoAgentSidePrincipalCanPublishToTheDirectory(t *testing.T) {
	// Third argument is this branch's: the callout's NKey seeds. #1313 wrote
	// this test against the two-argument signature on main.
	agent := a2aTestAgent()
	conf := string(buildA2ANATSConfigSecret(agent, a2aTestCreds(), a2aTestCalloutKeys(t)).Data["nats.conf"])
	doc, err := renderA2AAuthMap(agent)
	if err != nil {
		t.Fatalf("rendering the auth map: %v", err)
	}

	// Asked as the server would ask it, not as a substring scan would. A grant
	// need not spell the subject to authorize it: "a2a.*.*" covers
	// a2a.agents.platform and contains no "a2a.agents" to grep for, and
	// consolidating the agent's three exact topic grants into one wildcard is
	// the plausible way that arrives.
	const card = "a2a.agents.platform"
	publishLists := map[string][]string{
		a2aBridgeUser: a2aGrantSubjects(t, conf, a2aBridgeUser, "publish"),
	}
	for _, id := range doc.Identities {
		if id.User == a2aAgentBusUser {
			publishLists[a2aAgentBusUser] = id.Grants.Publish
		}
	}
	if publishLists[a2aAgentBusUser] == nil {
		t.Fatalf("no %q principal in the rendered map", a2aAgentBusUser)
	}
	for who, grants := range publishLists {
		for _, grant := range grants {
			if subjectMatches(grant, card) {
				t.Errorf("%s can publish %s via grant %q; one publish replaces a profile's "+
					"card, and the payload spec assigns cards to the profile owner, not workers",
					who, card, grant)
			}
		}
	}

	// The control: the gateway's READ of the directory must survive, or this
	// test would pass just as well against a config that broke discovery.
	var gatewayReads bool
	for _, grant := range a2aGrantSubjects(t, conf, "gateway", "subscribe") {
		if subjectMatches(grant, card) {
			gatewayReads = true
		}
	}
	if !gatewayReads {
		t.Error("the gateway lost subscribe on the directory; discovery reads it")
	}
}

// a2aFullCreds is a creds Secret carrying every key in the shape
// randomA2APassword emits, at a known resourceVersion. The rollout-hash tests
// need all five — a2aTestCreds omits sys-password — and they need the values
// to be real 32-hex passwords, because what they assert is that a digest of
// one appears nowhere in the render.
func a2aFullCreds(nibble string, resourceVersion string) *corev1.Secret {
	data := map[string][]byte{}
	for i, key := range a2aCredsKeys {
		data[key] = []byte(strings.Repeat(nibble, 31) + fmt.Sprintf("%x", i))
	}
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:            "test-agent-a2a-nats-creds",
			Namespace:       "test-ns",
			ResourceVersion: resourceVersion,
		},
		Data: data,
	}
}

// a2aPasswordDigestNeedles returns, for one password, every form of it that
// must not reach a rendered name, label or annotation: the value itself, its
// SHA-256, and that digest truncated the two ways this file truncates digests
// (8 characters for the provision Job's name, 16 for the pod-template hash).
func a2aPasswordDigestNeedles(password string) []string {
	sum := sha256.Sum256([]byte(password))
	digest := hex.EncodeToString(sum[:])
	return []string{password, digest, digest[:8], digest[:16]}
}

// The pod-template hash rolls the bus when the config changes, and it used to
// do that by hashing the rendered nats.conf — which carries all five NATS
// passwords, putting a truncated digest of the credentials in an annotation
// anyone who can get the StatefulSet can read (CodeQL alert 27,
// go/weak-sensitive-data-hashing). The hash now covers the placeholder render
// plus the creds Secret's resourceVersion, and all three properties below have
// to hold at once: changing only the passwords must NOT move it, while a
// config change and an in-place rotation must both still move it. The callout
// keypair is a fourth: its public halves are config, not credentials, and they
// are rendered into the auth_callout block, so rotating them has to roll the
// StatefulSet the same way any other config change does.
func TestA2AConfigRolloutHashOmitsCredentialsAndTracksRotation(t *testing.T) {
	agent := a2aTestAgent()
	keys := a2aTestCalloutKeys(t)
	const rv = "4711"
	credsA := a2aFullCreds("a", rv)
	credsB := a2aFullCreds("b", rv)

	// Guard against an inert test: if the two creds rendered the same conf,
	// an equal hash below would prove nothing.
	confA := string(buildA2ANATSConfigSecret(agent, credsA, keys).Data["nats.conf"])
	confB := string(buildA2ANATSConfigSecret(agent, credsB, keys).Data["nats.conf"])
	if confA == confB {
		t.Fatal("the two creds Secrets render the same nats.conf; the omission check below is inert")
	}

	hashA := a2aConfigRolloutHash(agent, credsA, keys)
	if len(hashA) != a2aConfigHashLength {
		t.Errorf("rollout hash is %d characters, want %d", len(hashA), a2aConfigHashLength)
	}
	if hashB := a2aConfigRolloutHash(agent, credsB, keys); hashA != hashB {
		t.Errorf("the rollout hash still tracks the password bytes: %q vs %q", hashA, hashB)
	}

	// An in-place rotation: ensureA2ACredsSecret repairs credentials with an
	// Update on the same Secret, so the UID does not move and the
	// resourceVersion is the only thing that says a credential changed.
	rotated := a2aFullCreds("b", "4712")
	if hashRotated := a2aConfigRolloutHash(agent, rotated, keys); hashA == hashRotated {
		t.Error("a credential rotation does not roll the bus: the hash ignores resourceVersion")
	}

	// A config change: the agent's name is rendered into server_name.
	other := a2aTestAgent()
	other.Name = "other-agent"
	if hashOther := a2aConfigRolloutHash(other, credsA, keys); hashA == hashOther {
		t.Error("a config change does not roll the bus: the hash ignores the render")
	}

	// A callout keypair rotation: the issuer and xkey public keys are rendered
	// into auth_callout, and a server still holding the old ones cannot verify
	// what the new callout signs. If this does not move the hash, the
	// StatefulSet is never rolled and the bus rejects every authorization.
	rotatedKeys := a2aTestCalloutKeys(t)
	if hashKeys := a2aConfigRolloutHash(agent, credsA, rotatedKeys); hashA == hashKeys {
		t.Error("a callout keypair rotation does not roll the bus: the hash ignores the public keys")
	}
}

// The negative half of the same property, taken across the whole render
// rather than one function: no raw password, and no digest of one, may appear
// in any rendered object's name, label or annotation. Names and labels are
// readable by anything that can list the namespace, so a digest there is an
// offline target; the passwords belong in Secret data and nowhere else.
//
// What it does NOT cover, because its needles are digests of known strings: a
// credential folded into the hashed input as part of some third string, which
// produces an annotation that matches no needle here and leaves this test
// green. TestA2AConfigRolloutHashOmitsCredentialsAndTracksRotation is the
// guard for that shape — it varies only the password bytes and requires the
// hash not to move. The two are complementary and neither subsumes the other.
func TestA2ARenderedObjectsCarryNoPasswordDigest(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	creds := a2aFullCreds("c", "")

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, creds).
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
	// The A2A gateway is withheld until BusCredentialsReady is True, and it is
	// one of the objects this test has to look at. Report the callout serving
	// so the walk below has a gateway to walk.
	letTheGatewayThrough(t, ctx, cl, r, req, agent)

	stored := &corev1.Secret{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(creds), stored); err != nil {
		t.Fatalf("creds Secret missing after reconcile: %v", err)
	}
	forbidden := map[string]string{}
	for _, key := range a2aCredsKeys {
		password := string(stored.Data[key])
		if !a2aCredsValueRe.MatchString(password) {
			t.Fatalf("seeded creds key %q was re-rolled into an unexpected shape: %q", key, password)
		}
		for _, needle := range a2aPasswordDigestNeedles(password) {
			forbidden[needle] = key
		}
	}

	// The exact regression: a digest of the rendered nats.conf, which is a
	// digest of the five passwords inside it.
	config := &corev1.Secret{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-nats-config", Namespace: "test-ns"}, config); err != nil {
		t.Fatalf("config Secret missing after reconcile: %v", err)
	}
	confSum := sha256.Sum256(config.Data["nats.conf"])
	confDigest := hex.EncodeToString(confSum[:])
	for _, needle := range []string{confDigest, confDigest[:8], confDigest[:16]} {
		forbidden[needle] = "nats.conf"
	}

	check := func(t *testing.T, kind, where, value string) {
		t.Helper()
		for needle, source := range forbidden {
			if strings.Contains(value, needle) {
				t.Errorf("%s %s carries a digest of %s: %q", kind, where, source, value)
			}
		}
	}

	lists := []client.ObjectList{
		&corev1.SecretList{},
		&corev1.ServiceList{},
		&corev1.ServiceAccountList{},
		&corev1.ConfigMapList{},
		&corev1.ResourceQuotaList{},
		&corev1.PersistentVolumeClaimList{},
		&appsv1.StatefulSetList{},
		&appsv1.DeploymentList{},
		&batchv1.JobList{},
		&networkingv1.NetworkPolicyList{},
		&rbacv1.RoleList{},
		&rbacv1.RoleBindingList{},
	}
	walked := 0
	seen := map[string]bool{}
	for _, list := range lists {
		if err := cl.List(ctx, list); err != nil {
			t.Fatalf("listing %T: %v", list, err)
		}
		items, err := apimeta.ExtractList(list)
		if err != nil {
			t.Fatalf("extracting %T: %v", list, err)
		}
		for _, item := range items {
			obj, ok := item.(client.Object)
			if !ok {
				t.Fatalf("%T is not a client.Object", item)
			}
			walked++
			kind := fmt.Sprintf("%T %s", obj, obj.GetName())
			seen[kind] = true
			check(t, kind, "name", obj.GetName())
			for key, value := range obj.GetLabels() {
				check(t, kind, "label "+key, value)
			}
			for key, value := range obj.GetAnnotations() {
				check(t, kind, "annotation "+key, value)
			}
			// The pod template is a second metadata surface, and the one the
			// rollout hash actually rides.
			var podMeta *metav1.ObjectMeta
			switch typed := obj.(type) {
			case *appsv1.StatefulSet:
				podMeta = &typed.Spec.Template.ObjectMeta
			case *appsv1.Deployment:
				podMeta = &typed.Spec.Template.ObjectMeta
			case *batchv1.Job:
				podMeta = &typed.Spec.Template.ObjectMeta
			}
			if podMeta == nil {
				continue
			}
			for key, value := range podMeta.Labels {
				check(t, kind, "pod-template label "+key, value)
			}
			for key, value := range podMeta.Annotations {
				check(t, kind, "pod-template annotation "+key, value)
			}
		}
	}
	if walked == 0 {
		t.Fatal("walked no rendered objects; the check is inert")
	}
	// Named, not counted. A non-zero walk says the lists found something; it
	// does not say they found the objects whose metadata the passwords could
	// reach. Two must be there or this proves nothing about them: the NATS
	// StatefulSet, which carries the nats.conf digest on its pod template and
	// is the exact regression above, and the A2A gateway Deployment, which the
	// creation gate withholds until BusCredentialsReady is True -- so a test
	// that does not open the gate walks a namespace with no gateway in it and
	// reports green on coverage it never had.
	for _, want := range []string{
		"*v1.StatefulSet test-agent-a2a-nats",
		"*v1.Deployment test-agent-a2a-gateway",
	} {
		if !seen[want] {
			t.Errorf("the walk never saw %s, so nothing here checked its metadata; walked %d objects", want, walked)
		}
	}
}

// The other half of the rotation property, end to end: ensureA2ACredsSecret
// repairs a damaged credential with an Update on the existing Secret, and the
// StatefulSet the same reconcile renders must come out with a different
// pod-template hash — otherwise the bus keeps serving the old password.
func TestA2AConfigHashRollsOnCredentialRepair(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, a2aFullCreds("d", "")).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()

	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()
	stsKey := types.NamespacedName{Name: "test-agent-a2a-nats", Namespace: "test-ns"}

	hashNow := func(t *testing.T) string {
		t.Helper()
		sts := &appsv1.StatefulSet{}
		if err := cl.Get(ctx, stsKey, sts); err != nil {
			t.Fatalf("NATS StatefulSet missing: %v", err)
		}
		return sts.Spec.Template.Annotations["kubeagents.x-k8s.io/a2a-config-hash"]
	}

	// finalizer pass, then the real one
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}
	before := hashNow(t)
	if before == "" {
		t.Fatal("pod template carries no config hash")
	}

	// A reconcile that changes nothing must not roll the bus.
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 3 failed: %v", err)
	}
	if steady := hashNow(t); steady != before {
		t.Errorf("an idle reconcile rolled the bus: %q then %q", before, steady)
	}

	// Damage one credential; ensureA2ACredsSecret re-rolls it in place.
	creds := &corev1.Secret{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-nats-creds", Namespace: "test-ns"}, creds); err != nil {
		t.Fatalf("creds Secret missing: %v", err)
	}
	damaged := string(creds.Data["gateway-password"])
	creds.Data["gateway-password"] = []byte("")
	if err := cl.Update(ctx, creds); err != nil {
		t.Fatalf("failed to damage the creds Secret: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile 4 failed: %v", err)
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-a2a-nats-creds", Namespace: "test-ns"}, creds); err != nil {
		t.Fatalf("creds Secret missing after repair: %v", err)
	}
	if repaired := string(creds.Data["gateway-password"]); repaired == damaged || !a2aCredsValueRe.MatchString(repaired) {
		t.Fatalf("the credential was not rotated in place: %q", repaired)
	}
	if after := hashNow(t); after == before {
		t.Errorf("a credential rotation did not roll the bus: hash stayed %q", before)
	}
}

// TestARefusalDoesNotSuspendTheA2AFences is #1247's assertion extended to the
// two policies this branch's stack depends on.
//
// #1247 established the hazard and the rescue for <name>-gateway-netpol and
// <name>-sandbox-metadata-deny: a refusal withholds the workload, and a policy
// that stops being reconciled is one an operator can delete permanently, after
// which nothing selects the Pod and NetworkPolicy permits all egress. The A2A
// fences were outside that rescue for a positional reason rather than a
// considered one — every refusal path returns before reconcileA2A is reached,
// and reconcileA2A is where the fences were applied.
//
// The session fence is why that mattered enough to move. A session pod runs
// worker code the model steers, and buildA2ASessionNetworkPolicy is the whole
// of its confinement. An install sitting Degraded over one bad control-plane
// CIDR would stop re-asserting it, and the deletion would stick against pods
// that are still running, with the status naming the CIDR and nothing naming
// the fence.
//
// The mode: today subtest is the control that stops this passing for the wrong
// reason: reconcileA2ANetworkFences is gated, so a fence appearing there would
// mean the guardrail path had started rendering the next stack on installs
// that never asked for it.
func TestARefusalDoesNotSuspendTheA2AFences(t *testing.T) {
	for _, tc := range []struct {
		name     string
		mode     *string
		expected bool
	}{
		{"mode next", ptr.To(string(ModeNext)), true},
		{"mode today", nil, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			agent := egressPolicyAgent(func(a *agentv1alpha1.PlatformAgent) {
				a.Spec.Mode = tc.mode
				a.Spec.Security.EgressAllowlist = &agentv1alpha1.EgressAllowlistSpec{
					ControlPlaneCIDRs: []string{"0.0.0.0/0"},
				}
			})
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(agent).
				WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
				WithInterceptorFuncs(ssaApplyInterceptor()).
				Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
			req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
			ctx := context.Background()

			if _, err := r.Reconcile(ctx, req); err != nil {
				t.Fatalf("Reconcile failed: %v", err)
			}

			// Without the refusal the ordinary path would render the fences and
			// this would assert nothing.
			stored := &agentv1alpha1.PlatformAgent{}
			if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
				t.Fatalf("failed to re-read the agent: %v", err)
			}
			var gotReason string
			for _, condition := range stored.Status.Conditions {
				if condition.Type == "Ready" {
					gotReason = condition.Reason
				}
			}
			if gotReason != reasonEgressAllowlistRefused {
				t.Fatalf("the spec was not refused, so this test proves nothing; got reason %q", gotReason)
			}

			for _, fence := range []types.NamespacedName{
				{Name: a2aNATSNetpolName(agent), Namespace: agent.Namespace},
				{Name: a2aSessionNetpolName(agent), Namespace: agent.Namespace},
			} {
				err := cl.Get(ctx, fence, &networkingv1.NetworkPolicy{})
				if !tc.expected {
					if err == nil {
						t.Errorf("%s was rendered outside mode next; the guardrail path must not "+
							"bring up the next stack's fences on an install that never asked for it", fence.Name)
					}
					continue
				}
				if err != nil {
					t.Fatalf("the refusal withheld %s: %v", fence.Name, err)
				}

				// Written once before the spec went bad is not the same as
				// maintained, and only the second is a guardrail.
				if err := cl.Delete(ctx, &networkingv1.NetworkPolicy{
					ObjectMeta: metav1.ObjectMeta{Name: fence.Name, Namespace: fence.Namespace},
				}); err != nil {
					t.Fatalf("failed to delete %s for the restore check: %v", fence.Name, err)
				}
				if _, err := r.Reconcile(ctx, req); err != nil {
					t.Fatalf("second Reconcile failed: %v", err)
				}
				if err := cl.Get(ctx, fence, &networkingv1.NetworkPolicy{}); err != nil {
					t.Fatalf("while the spec was refused %s stopped being reconciled, so deleting it "+
						"stuck; the pods it fences keep running unconfined: %v", fence.Name, err)
				}
			}
		})
	}
}

// TestSeedGrantsAndProvisionScriptNameTheSameStreams pins the pair. seed's
// $JS.API allow-list renders from a2aProvisionedStreams; the provision script
// does NOT -- each create line there carries its own subjects, retention and
// caps, so the script names the streams itself. This is what holds the two
// spellings together, in both directions.
//
// The failure it prevents is quiet in the worst way: a stream added to the
// script but not the grant makes provisioning hang on a refused API request
// until the Job's backoff gives up, and the install comes up with a healthy bus
// and a missing stream. That is the exact shape of #1259 — a provisioning step
// that fails without the render looking wrong.
func TestSeedGrantsAndProvisionScriptNameTheSameStreams(t *testing.T) {
	script := a2aProvisionScript(a2aTestAgent())

	for _, s := range a2aProvisionedStreams {
		// Anchor to the create itself, not to the bare name: the script's prose
		// mentions every bucket by name in a comment block, so a token match
		// stayed green with the `kv add` line deleted. Only a create counts.
		create := "stream add " + s + " "
		if bare, isKV := strings.CutPrefix(s, "KV_"); isKV {
			create = "kv add " + bare + " "
		}
		if !strings.Contains(script, create) {
			t.Errorf("a2aProvisionedStreams names %q but the provision script has no %q; "+
				"the grant permits a stream nothing creates", s, strings.TrimSpace(create))
		}
		for _, verb := range []string{"CREATE", "INFO"} {
			want := "$JS.API.STREAM." + verb + "." + s
			if !slices.Contains(a2aSeedJetStreamGrants(), want) {
				t.Errorf("seed grant is missing %q", want)
			}
		}
	}

	// The other direction: every `stream add` / `kv add` in the script must be
	// covered by the list, or provisioning breaks on a refusal.
	for _, line := range strings.Split(script, "\n") {
		for _, verb := range []string{"stream add ", "kv add "} {
			idx := strings.Index(line, verb)
			if idx < 0 {
				continue
			}
			name := strings.Fields(line[idx+len(verb):])[0]
			if verb == "kv add " {
				name = "KV_" + name
			}
			if !slices.Contains(a2aProvisionedStreams, name) {
				t.Errorf("the provision script creates %q, which a2aProvisionedStreams does not name, "+
					"so seed has no grant for it and the create will be refused", name)
			}
		}
	}
}

// TestSeedHoldsNoWholesaleJetStreamAPI is a shape check, and only that — it reads
// the rendered config rather than asking a server, so it cannot prove a refusal.
// The refusal proof is the live validation in the PR body: seed denied on
// STREAM.PURGE against the running install, and the provision Job still
// completing. This test exists to keep the wildcard from coming back by accident.
//
// It asks two questions, neither by substring. The first version of this test
// scanned seed's block for seven banned strings, and `$JS.API.STREAM.>` — which
// grants PURGE, DELETE, MSG.DELETE, RESTORE and UPDATE on every stream — contains
// none of them; the web user's test in this file records the same lesson.
//
//  1. The publish allow-list is pinned exactly: the three starter topics, the
//     grants a2aSeedJetStreamGrants renders, and seed's own inbox. Any other
//     entry, in any spelling, is a diff.
//  2. Every route architecture A3's write-surface enumeration calls an
//     identity-forgery grant — a concrete subject per verb, on a provisioned
//     stream — is run through subjectMatches against every rendered entry,
//     including the ones a2aSeedJetStreamGrants produced. That is the question
//     the server asks, so a wildcard cannot grant a route without naming it.
func TestSeedHoldsNoWholesaleJetStreamAPI(t *testing.T) {
	conf := string(buildA2ANATSConfigSecret(a2aTestAgent(), a2aTestCreds(), a2aTestCalloutKeys(t)).Data["nats.conf"])

	// Seed's publish allow-list exactly, not the span to the next user: the
	// following block's explanatory comment names grants of its own, and a
	// sloppier cut reads them as seed's. It did, on this test's first run.
	start := strings.Index(conf, "user: seed")
	if start < 0 {
		t.Fatal("no seed user in the rendered config")
	}
	openIdx := strings.Index(conf[start:], "publish { allow = [")
	if openIdx < 0 {
		t.Fatal("seed has no publish allow-list")
	}
	openIdx += start
	closeIdx := strings.Index(conf[openIdx:], "] }")
	if closeIdx < 0 {
		t.Fatal("seed's publish allow-list is unterminated")
	}
	var got []string
	for _, line := range strings.Split(conf[openIdx:openIdx+closeIdx], "\n") {
		line = strings.TrimSuffix(strings.TrimSpace(line), ",")
		if strings.HasPrefix(line, `"`) {
			got = append(got, strings.Trim(line, `"`))
		}
	}

	want := []string{
		"a2a.topics.agent.platform.upgrade-readiness",
		"a2a.topics.shared.blueprint",
		"a2a.topics.shared.annotations",
	}
	want = append(want, a2aSeedJetStreamGrants()...)
	want = append(want, "_INBOX.seed.>")
	if !reflect.DeepEqual(got, want) {
		t.Errorf("seed publish allow-list changed.\n got: %q\nwant: %q", got, want)
	}

	// Every forbidden verb against every provisioned stream, not one sample
	// each. A named grant only widens the stream it names, so a verb sampled
	// on TASKS says nothing about the same verb on DIRECTORY -- adding
	// $JS.API.STREAM.PURGE.TASKS to the grants passed a one-sample-per-verb
	// list, because PURGE was only ever asked about TOPICS-JOURNAL.
	forbiddenVerbs := []string{
		"$JS.API.STREAM.RESTORE.",
		"$JS.API.STREAM.MSG.DELETE.",
		"$JS.API.STREAM.MSG.GET.",
		"$JS.API.STREAM.PURGE.",
		"$JS.API.STREAM.DELETE.",
		"$JS.API.STREAM.UPDATE.",
		"$JS.API.STREAM.SNAPSHOT.",
	}
	var forbidden []string
	for _, s := range a2aProvisionedStreams {
		for _, verb := range forbiddenVerbs {
			forbidden = append(forbidden, verb+s)
		}
		forbidden = append(forbidden,
			"$JS.API.CONSUMER.CREATE."+s+".x",
			"$JS.API.CONSUMER.DURABLE.CREATE."+s+".x",
			"$JS.API.DIRECT.GET."+s+".a2a.topics.shared.blueprint",
		)
	}
	// Plus a stream nobody provisions: the CREATE and INFO grants are the two
	// verbs seed legitimately holds, so they are only safe while they stay
	// bound to names on the list.
	forbidden = append(forbidden,
		"$JS.API.STREAM.CREATE.NOT-PROVISIONED",
		"$JS.API.STREAM.INFO.NOT-PROVISIONED",
	)
	for _, subject := range forbidden {
		for _, grant := range got {
			if subjectMatches(grant, subject) {
				t.Errorf("seed grant %q permits %q; provisioning does not use it", grant, subject)
			}
		}
	}

	// The other direction, and the one a forbidden list cannot cover: a new
	// entry inside a2aSeedJetStreamGrants is invisible to the DeepEqual above,
	// since want is built from that same function. So bound the shape instead.
	// Anything that is not account discovery or a listed stream's CREATE/INFO
	// is a grant that has to be argued for here rather than added quietly.
	allowedShapes := map[string]bool{"$JS.API.INFO": true, "$JS.API.STREAM.NAMES": true}
	for _, s := range a2aProvisionedStreams {
		allowedShapes["$JS.API.STREAM.CREATE."+s] = true
		allowedShapes["$JS.API.STREAM.INFO."+s] = true
	}
	for _, grant := range a2aSeedJetStreamGrants() {
		if !allowedShapes[grant] {
			t.Errorf("seed holds JetStream API grant %q, which is neither account discovery "+
				"nor CREATE/INFO on a provisioned stream", grant)
		}
	}
}

// TestBridgeHoldsNoWholesaleJetStreamAPI is the shape check for the bridge's
// JetStream API grant; the refusal proof is TestBridgeJetStreamGrantOnARealServer,
// which asks a server. This one keeps the wildcard from coming back by
// accident, and asks four questions, none by substring -- the web user's test
// above records why: `$JS.API.STREAM.>` contains none of the words a blocklist
// would think to name.
//
//  1. The publish allow-list is pinned exactly: the task-events subject for the
//     one addressee this principal executes for, the runtime-state bucket, the
//     grants a2aBridgeJetStreamGrants renders, the TASKS ack, flow control, and
//     the bridge's own inbox. Any other entry, in any spelling, is a diff.
//  2. Every destructive or out-of-scope route -- a concrete subject per verb,
//     on every provisioned stream, not one sample per verb -- is run through
//     subjectMatches against every rendered entry. That is the question the
//     server asks, so a wildcard cannot grant a route without naming it.
//  3. The grant function's own output is bounded by shape, because want in (1)
//     is built from that same function and cannot see an entry added inside it.
//  4. The subscribe list is pinned exactly too: a subscribe grant is delivery
//     interest for a push consumer's deliver_subject, so widening it is a
//     review conversation for the same reason widening publish is.
//
// The blackboard streams are on the forbidden side here, and that is the half
// of A5 this test carries. `worker` held INFO and DIRECT.GET on TOPICS-STATE
// and TOPICS-JOURNAL and publish on three topic subjects, because the `a2a`
// CLI shared its credential. The CLI is the `agent` principal now
// (TestAgentHoldsOnlyTheBlackboard), and a change that gave either of them the
// other's list would put `worker` back under a new name.
func TestBridgeHoldsNoWholesaleJetStreamAPI(t *testing.T) {
	conf := string(buildA2ANATSConfigSecret(a2aTestAgent(), a2aTestCreds(), a2aTestCalloutKeys(t)).Data["nats.conf"])
	got := a2aGrantSubjects(t, conf, a2aBridgeUser, "publish")

	if sub, want := a2aGrantSubjects(t, conf, a2aBridgeUser, "subscribe"), []string{
		"a2a.tasks." + a2aBridgeAddressee + ".*.in",
		"$KV.runtime-state.>",
		"_INBOX." + a2aBridgeUser + ".>",
	}; !reflect.DeepEqual(sub, want) {
		t.Errorf("bridge subscribe allow-list changed.\n got: %q\nwant: %q", sub, want)
	}

	want := []string{
		"a2a.tasks." + a2aBridgeAddressee + ".*.events",
		"$KV.runtime-state.>",
	}
	want = append(want, a2aBridgeJetStreamGrants()...)
	want = append(want, "$JS.ACK.TASKS.>", "$JS.FC.>", "_INBOX."+a2aBridgeUser+".>")
	if !reflect.DeepEqual(got, want) {
		t.Errorf("bridge publish allow-list changed.\n got: %q\nwant: %q", got, want)
	}

	// Verbs no bridge path uses, against every stream the provision script
	// creates. A named grant only widens the stream it names, so a verb
	// sampled on TASKS says nothing about the same verb on DIRECTORY.
	provisioned := []string{"TASKS", "DIRECTORY", "TOPICS-STATE", "TOPICS-JOURNAL", "KV_runtime-state", "KV_session-state", "KV_cap"}
	forbiddenVerbs := []string{
		"$JS.API.STREAM.CREATE.",
		"$JS.API.STREAM.UPDATE.",
		"$JS.API.STREAM.DELETE.",
		"$JS.API.STREAM.PURGE.",
		"$JS.API.STREAM.MSG.DELETE.",
		"$JS.API.STREAM.MSG.GET.",
		"$JS.API.STREAM.RESTORE.",
		"$JS.API.STREAM.SNAPSHOT.",
		"$JS.API.CONSUMER.NAMES.",
		"$JS.API.CONSUMER.LIST.",
	}
	var forbidden []string
	for _, s := range provisioned {
		for _, verb := range forbiddenVerbs {
			forbidden = append(forbidden, verb+s)
		}
		forbidden = append(forbidden, "$JS.API.CONSUMER.INFO."+s+".x")
	}
	// Streams the bridge has no business on at all: not a read, not a
	// consumer, not a direct get. The directory is the identity plane; the
	// session registry is the gateway's; cap is the capability envelope's;
	// and the two topic streams are the agent's since A5.
	for _, s := range []string{"DIRECTORY", "KV_session-state", "KV_cap", "TOPICS-STATE", "TOPICS-JOURNAL"} {
		forbidden = append(forbidden,
			"$JS.API.STREAM.INFO."+s,
			"$JS.API.CONSUMER.CREATE."+s+".x",
			"$JS.API.CONSUMER.CREATE."+s+".x.a2a.agents.platform",
			"$JS.API.CONSUMER.DURABLE.CREATE."+s+".x",
			"$JS.API.CONSUMER.MSG.NEXT."+s+".x",
			"$JS.API.CONSUMER.DELETE."+s+".x",
			"$JS.API.DIRECT.GET."+s+".a2a.agents.platform",
			"$JS.API.DIRECT.GET."+s,
		)
	}
	forbidden = append(forbidden,
		// The gateway's relay durable: CREATE and MSG.NEXT on a shared stream
		// are conceded, DELETE is not (see a2aBridgeJetStreamGrants).
		"$JS.API.CONSUMER.DELETE.TASKS.gateway-relay",
		// The registry is put, listed and deleted, never read by key.
		"$JS.API.DIRECT.GET.KV_runtime-state.$KV.runtime-state.k",
		// The blackboard by direct get, which is the read `worker` had.
		"$JS.API.DIRECT.GET.TOPICS-STATE.a2a.topics.shared.blueprint",
		"$JS.API.DIRECT.GET.TOPICS-JOURNAL.a2a.topics.shared.annotations",
		// Account discovery and enumeration.
		"$JS.API.INFO",
		"$JS.API.STREAM.NAMES",
		"$JS.API.STREAM.LIST",
		// A stream nobody provisions, for the verbs the bridge does hold.
		"$JS.API.STREAM.INFO.NOT-PROVISIONED",
		"$JS.API.CONSUMER.CREATE.NOT-PROVISIONED.x",
		"$JS.API.DIRECT.GET.NOT-PROVISIONED.x",
	)
	// Core subjects, not JetStream API: the blackboard writes and every other
	// addressee's task plane. The publish DeepEqual above would catch a new
	// literal entry, but not a widening of one that is already there --
	// a2a.tasks.*.*.events, which is what `worker` held, passes a DeepEqual
	// written against itself.
	forbidden = append(forbidden,
		"a2a.topics.shared.blueprint",
		"a2a.topics.shared.annotations",
		"a2a.topics.agent.platform.upgrade-readiness",
		"a2a.tasks.session-abc123.t1.events",
		"a2a.tasks.session-abc123.t1.in",
		"a2a.tasks."+a2aBridgeAddressee+".t1.in",
	)
	for _, subject := range forbidden {
		for _, grant := range got {
			if subjectMatches(grant, subject) {
				t.Errorf("bridge grant %q permits %q; nothing on the bridge path uses it", grant, subject)
			}
		}
	}

	// The other direction, and the one a forbidden list cannot cover: an
	// entry added inside a2aBridgeJetStreamGrants is invisible to the
	// DeepEqual above. Anything outside these shapes has to be argued for in
	// that function's comment rather than added quietly.
	allowedShapes := map[string]bool{}
	for _, s := range []string{"TASKS", "KV_runtime-state"} {
		allowedShapes["$JS.API.STREAM.INFO."+s] = true
		allowedShapes["$JS.API.CONSUMER.CREATE."+s+".>"] = true
	}
	allowedShapes["$JS.API.DIRECT.GET.TASKS.>"] = true
	allowedShapes["$JS.API.CONSUMER.MSG.NEXT.TASKS.*"] = true
	allowedShapes["$JS.API.CONSUMER.DELETE.KV_runtime-state.*"] = true
	for _, grant := range a2aBridgeJetStreamGrants() {
		if !allowedShapes[grant] {
			t.Errorf("bridge holds JetStream API grant %q, which is outside the shapes a2aBridgeJetStreamGrants argues for", grant)
		}
	}
}

// TestAgentHoldsOnlyTheBlackboard is the other half of the A5 split, and it
// reads the auth map rather than nats.conf: `agent` is callout-authenticated,
// so its grants are rendered as JSON for the callout to serve and never appear
// in a user block.
//
// What it pins is a negative that the bridge's test cannot state. The agent
// container is the widest-reach workload in the namespace -- it runs model
// output against a writable PVC -- and the reason A5 exists is that it used to
// hold a task-plane executor credential in order to run `a2a topics read`. So:
// no publish and no subscribe on a2a.tasks anything, no JetStream consumer verb
// of any kind (a consumer's target stream and deliver subject are body fields,
// which is how a CONSUMER.CREATE grant becomes a read of a stream the subject
// list withholds), and no reach into the buckets.
func TestAgentHoldsOnlyTheBlackboard(t *testing.T) {
	doc, err := renderA2AAuthMap(a2aTestAgent())
	if err != nil {
		t.Fatalf("rendering the auth map: %v", err)
	}
	var agent *a2aAuthMapIdentity
	for i := range doc.Identities {
		if doc.Identities[i].User == a2aAgentBusUser {
			agent = &doc.Identities[i]
		}
	}
	if agent == nil {
		t.Fatalf("no %q principal in the rendered map; the agent container has no identity to present", a2aAgentBusUser)
	}

	wantPublish := []string{
		"a2a.topics.agent.platform.upgrade-readiness",
		"a2a.topics.shared.blueprint",
		"a2a.topics.shared.annotations",
	}
	wantPublish = append(wantPublish, a2aAgentJetStreamGrants()...)
	wantPublish = append(wantPublish, "_INBOX."+a2aAgentBusUser+".>")
	if !reflect.DeepEqual(agent.Grants.Publish, wantPublish) {
		t.Errorf("agent publish allow-list changed.\n got: %q\nwant: %q", agent.Grants.Publish, wantPublish)
	}
	if want := []string{"a2a.topics.>", "_INBOX." + a2aAgentBusUser + ".>"}; !reflect.DeepEqual(agent.Grants.Subscribe, want) {
		t.Errorf("agent subscribe allow-list changed.\n got: %q\nwant: %q", agent.Grants.Subscribe, want)
	}

	// The task plane, both directions and both spellings -- the bridge's own
	// addressee and a session pod's -- plus every consumer verb on every
	// stream, the other principals' buckets, and enumeration.
	var forbidden []string
	for _, addressee := range []string{a2aBridgeAddressee, "session-abc123"} {
		forbidden = append(forbidden,
			"a2a.tasks."+addressee+".t1.in",
			"a2a.tasks."+addressee+".t1.events",
			"$JS.ACK.TASKS.1.2.3",
		)
	}
	for _, s := range []string{"TASKS", "DIRECTORY", "TOPICS-STATE", "TOPICS-JOURNAL", "KV_runtime-state", "KV_session-state", "KV_cap"} {
		forbidden = append(forbidden,
			"$JS.API.CONSUMER.CREATE."+s+".x",
			"$JS.API.CONSUMER.DURABLE.CREATE."+s+".x",
			"$JS.API.CONSUMER.MSG.NEXT."+s+".x",
			"$JS.API.CONSUMER.DELETE."+s+".x",
			"$JS.API.CONSUMER.INFO."+s+".x",
			"$JS.API.STREAM.PURGE."+s,
			"$JS.API.STREAM.UPDATE."+s,
			"$JS.API.STREAM.DELETE."+s,
			"$JS.API.STREAM.MSG.GET."+s,
		)
	}
	forbidden = append(forbidden,
		"$KV.runtime-state.bridge.platform.t1",
		"$KV.session-state.k",
		"$KV.cap.root.t1",
		"$JS.API.STREAM.INFO.TASKS",
		"$JS.API.DIRECT.GET.TASKS.a2a.tasks.platform.t1.events",
		"$JS.API.INFO",
		"$JS.API.STREAM.NAMES",
		"$JS.API.STREAM.LIST",
	)
	for _, subject := range forbidden {
		for _, grant := range append(slices.Clone(agent.Grants.Publish), agent.Grants.Subscribe...) {
			if subjectMatches(grant, subject) {
				t.Errorf("agent grant %q permits %q; the platform agent container holds the blackboard and nothing else", grant, subject)
			}
		}
	}

	// And the bound on the grant function itself, for the same reason the
	// bridge's test bounds its own: a DeepEqual built from the function
	// cannot see an entry added inside it.
	allowedShapes := map[string]bool{}
	for _, s := range []string{a2aTopicsStateStream, a2aTopicsJournalStream} {
		allowedShapes["$JS.API.STREAM.INFO."+s] = true
		allowedShapes["$JS.API.DIRECT.GET."+s+".>"] = true
	}
	for _, grant := range a2aAgentJetStreamGrants() {
		if !allowedShapes[grant] {
			t.Errorf("agent holds JetStream API grant %q, which is outside the shapes a2aAgentJetStreamGrants argues for", grant)
		}
	}
}

// TestBusGrantsNameStreamsTheProvisionScriptCreates pins the pair. The bridge's
// and the agent's grants render from the stream-name constants; the provision
// script spells each name itself, because every create line carries its own
// subjects, retention and caps. Rename a stream on one side only and a
// principal holds grants on a name that does not exist -- an authorization
// failure at runtime with a green suite -- so the script's create lines are
// held to the constants here. Anchored to the create itself, not the bare name,
// for the reason #1306's pairing test records: the script's prose mentions
// every name too.
//
// Both grant functions, in one test, because the pairing is a property of the
// script rather than of either caller: A5 split one list into two, and a test
// that walked only the list it was written for would stop covering the other
// the day someone added a stream to it.
func TestBusGrantsNameStreamsTheProvisionScriptCreates(t *testing.T) {
	script := a2aProvisionScript(a2aTestAgent())
	for _, create := range []string{
		"stream add " + a2aTasksStream + " ",
		"stream add " + a2aTopicsStateStream + " ",
		"stream add " + a2aTopicsJournalStream + " ",
		"kv add " + a2aRuntimeStateBucket + " ",
	} {
		if !strings.Contains(script, create) {
			t.Errorf("the provision script has no %q; a bus grant names a stream nothing creates", strings.TrimSpace(create))
		}
	}
	// Every grant names one of those constants, so the check above covers
	// the whole list rather than the four names this test happens to know.
	known := []string{a2aTasksStream, a2aTopicsStateStream, a2aTopicsJournalStream, a2aKVStreamPrefix + a2aRuntimeStateBucket}
	for who, grants := range map[string][]string{
		a2aBridgeUser:   a2aBridgeJetStreamGrants(),
		a2aAgentBusUser: a2aAgentJetStreamGrants(),
	} {
		for _, grant := range grants {
			if !slices.ContainsFunc(known, func(name string) bool { return strings.Contains(grant, "."+name) }) {
				t.Errorf("%s grant %q names a stream outside the constants the provision script is held to", who, grant)
			}
		}
	}
	// And the direct-get route the grants assume: every stream add says so.
	for _, line := range strings.Split(script, "\n") {
		if strings.Contains(line, "stream add ") && !strings.Contains(line, "--allow-direct") {
			t.Errorf("provision line %q does not set --allow-direct; the bus grants are written for DIRECT.GET, not STREAM.MSG.GET", strings.TrimSpace(line))
		}
	}
}

// a2aGrantStream places one JetStream or KV grant: the stream it names and
// the verb it holds there, or the account-level discovery request it is.
//
// stream and verb are set for a per-stream grant: $JS.API.STREAM.<verb>.<s>,
// $JS.API.STREAM.MSG.<verb>.<s>, $JS.API.CONSUMER.<verb>.<s>...,
// $JS.API.CONSUMER.MSG.NEXT.<s>..., $JS.API.DIRECT.GET.<s>...; $JS.ACK.<s>...
// as verb ACK; and $KV.<b>.>, the bucket's data plane, as verb KV on the
// bucket's backing stream KV_<b>. accountLevel is true for the three discovery
// requests that name no stream. All zero means a shape this helper does not
// know, and the invariant test fails on that rather than letting it through
// unread.
//
// The helper places shapes; it does not judge them. STREAM.DELETE and PURGE
// are placed like INFO, and it is the table row that says which verbs a user
// may hold on each stream. A wildcard where the stream name should be comes
// back as the wildcard itself, so "$JS.API.STREAM.INFO.*" names stream "*",
// which is in no row.
func a2aGrantStream(subject string) (stream, verb string, accountLevel bool) {
	tok := strings.Split(subject, ".")
	at := func(i int) string {
		if i < len(tok) {
			return tok[i]
		}
		return ""
	}
	switch {
	case at(0) == "$KV":
		if b := at(1); b != "" {
			if b == "*" || b == ">" {
				return b, "KV", false
			}
			return a2aKVStreamPrefix + b, "KV", false
		}
		return "", "", false

	case at(0) == "$JS" && at(1) == "ACK":
		return at(2), "ACK", false

	case at(0) == "$JS" && at(1) == "API":
		switch {
		case subject == "$JS.API.INFO",
			subject == "$JS.API.STREAM.NAMES",
			subject == "$JS.API.STREAM.LIST":
			return "", "", true
		case at(2) == "STREAM" && at(3) == "MSG" && (at(4) == "GET" || at(4) == "DELETE"):
			return at(5), "STREAM.MSG." + at(4), false
		case at(2) == "STREAM" && slices.Contains([]string{
			"CREATE", "UPDATE", "DELETE", "INFO", "PURGE", "RESTORE", "SNAPSHOT"}, at(3)):
			// One trailing token exactly: a stream's own verbs take the
			// stream name and nothing after it.
			if len(tok) != 5 {
				return "", "", false
			}
			return at(4), "STREAM." + at(3), false
		case at(2) == "CONSUMER" && at(3) == "MSG" && at(4) == "NEXT":
			return at(5), "CONSUMER.MSG.NEXT", false
		case at(2) == "CONSUMER" && at(3) == "DURABLE" && at(4) == "CREATE":
			return at(5), "CONSUMER.DURABLE.CREATE", false
		case at(2) == "CONSUMER" && slices.Contains([]string{
			"CREATE", "INFO", "DELETE", "NAMES", "LIST"}, at(3)):
			return at(4), "CONSUMER." + at(3), false
		case at(2) == "DIRECT" && at(3) == "GET":
			return at(4), "DIRECT.GET", false
		}
	}
	return "", "", false
}

func TestA2AGrantStream(t *testing.T) {
	cases := []struct {
		subject      string
		stream, verb string
		accountLevel bool
	}{
		{"$JS.API.INFO", "", "", true},
		{"$JS.API.STREAM.NAMES", "", "", true},
		{"$JS.API.STREAM.LIST", "", "", true},
		{"$JS.API.STREAM.CREATE.TASKS", "TASKS", "STREAM.CREATE", false},
		{"$JS.API.STREAM.INFO.KV_cap", "KV_cap", "STREAM.INFO", false},
		{"$JS.API.STREAM.DELETE.TASKS", "TASKS", "STREAM.DELETE", false},
		{"$JS.API.STREAM.MSG.GET.TASKS", "TASKS", "STREAM.MSG.GET", false},
		{"$JS.API.CONSUMER.CREATE.TASKS.>", "TASKS", "CONSUMER.CREATE", false},
		{"$JS.API.CONSUMER.INFO.DIRECTORY.*", "DIRECTORY", "CONSUMER.INFO", false},
		{"$JS.API.CONSUMER.DELETE.KV_runtime-state.*", "KV_runtime-state", "CONSUMER.DELETE", false},
		{"$JS.API.CONSUMER.MSG.NEXT.TOPICS-STATE.*", "TOPICS-STATE", "CONSUMER.MSG.NEXT", false},
		{"$JS.API.CONSUMER.DURABLE.CREATE.TASKS.x", "TASKS", "CONSUMER.DURABLE.CREATE", false},
		{"$JS.API.DIRECT.GET.TOPICS-JOURNAL.>", "TOPICS-JOURNAL", "DIRECT.GET", false},
		{"$JS.ACK.TASKS.>", "TASKS", "ACK", false},
		{"$KV.session-state.>", "KV_session-state", "KV", false},
		// Wildcards in the stream position come back as themselves, so
		// they match no row.
		{"$JS.API.STREAM.INFO.*", "*", "STREAM.INFO", false},
		{"$JS.API.CONSUMER.CREATE.>", ">", "CONSUMER.CREATE", false},
		{"$JS.ACK.>", ">", "ACK", false},
		{"$KV.>", ">", "KV", false},
		// Shapes the helper does not know: neither a stream nor
		// account-level, which the invariant test reads as a failure.
		{"$JS.API.>", "", "", false},
		{"$JS.API.STREAM.>", "", "", false},
		{"$JS.API.STREAM.INFO", "", "", false},
		{"$JS.API.STREAM.INFO.TASKS.x", "", "", false},
		{"$JS.API.CONSUMER.PAUSE.TASKS.x", "", "", false},
		{"$JS.API.SERVER.INFO", "", "", false},
		{"$JS.FC.>", "", "", false},
		{"a2a.tasks.>", "", "", false},
	}
	for _, c := range cases {
		stream, verb, accountLevel := a2aGrantStream(c.subject)
		if stream != c.stream || verb != c.verb || accountLevel != c.accountLevel {
			t.Errorf("a2aGrantStream(%q) = (%q, %q, %v), want (%q, %q, %v)",
				c.subject, stream, verb, accountLevel, c.stream, c.verb, c.accountLevel)
		}
	}
}

// a2aConfUserNames returns every `user:` name in a nats.conf render, in order.
// A trimmed line beginning with the key, so a comment that mentions a user
// does not count as one.
func a2aConfUserNames(conf string) []string {
	var names []string
	for _, line := range strings.Split(conf, "\n") {
		if name, ok := strings.CutPrefix(strings.TrimSpace(line), "user: "); ok {
			names = append(names, strings.TrimSpace(name))
		}
	}
	return names
}

// a2aConfUserBlock returns one user's block: from its `user:` line to the brace
// that closes the block, at the column renderA2AStaticUser and the AUTH
// template both put it. Not the span to the next `user:` line, which
// a2aGrantSubjects uses: that span runs on through the comment above the next
// block, and for the last user in the file through the authorization section.
func a2aConfUserBlock(t *testing.T, conf, user string) string {
	t.Helper()
	start := strings.Index(conf, "user: "+user+"\n")
	if start < 0 {
		t.Fatalf("no %s user in the rendered config", user)
	}
	end := strings.Index(conf[start:], "\n"+a2aUserBlockIndent+"}\n")
	if end < 0 {
		t.Fatalf("%s's user block is unterminated", user)
	}
	return conf[start : start+end]
}

// a2aEnvSecretRef is one Secret a pod's env reads: by key through a
// SecretKeyRef, or whole through envFrom, in which case key is "*".
type a2aEnvSecretRef struct {
	where       string // <container>/<env var>, or <container>/envFrom
	secret, key string
}

// a2aPodSpecEnvSecretRefs returns every Secret read into env across a pod
// spec's containers and init containers.
func a2aPodSpecEnvSecretRefs(spec corev1.PodSpec) []a2aEnvSecretRef {
	var refs []a2aEnvSecretRef
	for _, c := range slices.Concat(spec.InitContainers, spec.Containers) {
		for _, e := range c.Env {
			if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil {
				refs = append(refs, a2aEnvSecretRef{c.Name + "/" + e.Name, e.ValueFrom.SecretKeyRef.Name, e.ValueFrom.SecretKeyRef.Key})
			}
		}
		for _, ef := range c.EnvFrom {
			if ef.SecretRef != nil {
				refs = append(refs, a2aEnvSecretRef{c.Name + "/envFrom", ef.SecretRef.Name, "*"})
			}
		}
	}
	return refs
}

// a2aGrantRow is what one APP principal may reach, as the invariant test's
// table records it.
type a2aGrantRow struct {
	// callout is true for a principal the callout issues rather than
	// nats.conf renders; its lists come from a2aIdentities.
	callout bool
	// perConnection is a callout-issued principal whose entry carries no
	// grants at all: every pod behind it runs as one ServiceAccount, and
	// the callout mints each connection's authorization from the pod the
	// API server attested, so a grant in its rendered lists would be handed
	// to every pod sharing the ServiceAccount. Its row names no streams and
	// no account-level discovery, and both of its lists are held empty;
	// the entry gaining a grant fails, the way an empty row for any other
	// principal fails.
	perConnection bool
	// streams maps each stream the principal may name to the verbs it may
	// hold there, in a2aGrantStream's spelling. Checked in both directions:
	// a grant outside the row fails, and a row entry no grant reaches fails.
	streams map[string][]string
	// accountLevel is the $JS.API discovery the principal may make with no
	// stream in the subject.
	accountLevel []string
	// wholesale records a principal that still holds $JS.API.>; narrowing
	// it is a behaviour change with its own real-server test, not this
	// table's to make.
	wholesale bool
}

// a2aSameVerbsOn builds a streams map granting the same verbs on each stream.
func a2aSameVerbsOn(streams []string, verbs ...string) map[string][]string {
	out := map[string][]string{}
	for _, s := range streams {
		out[s] = verbs
	}
	return out
}

// a2aReservedSubjects returns, for one principal, subjects its row does not
// record, and that no wildcard in either of its lists may therefore cover:
// the account's JetStream discovery and STREAM.DELETE on every provisioned
// stream (unless the row records the wholesale grant, which covers the whole
// API), an ack on every stream the row grants no ACK on, a key in every
// bucket it grants no KV on, and every other principal's inbox. users is
// every principal in the table.
//
// The per-grant rules read a grant's spelling, and a spelling check cannot
// see that "*.API.>" covers "$JS.API.STREAM.DELETE.TASKS" or that "*.>" in a
// subscribe list covers everyone's inbox. This list is what the server would
// be asked, so the caller matches every wildcard grant against it with
// subjectMatches and the spelling stops mattering. It is not everything the
// principal may not do; it is one subject per thing the row withholds, which
// is enough for a wildcard to trip on.
func a2aReservedSubjects(user string, row a2aGrantRow, users []string) []string {
	const (
		// One delivered message's ack subject, in the pre-domain form:
		// consumer, delivered count, stream and consumer sequence,
		// timestamp, pending. Any token values do for matching.
		ackSuffix   = ".c.1.1.1.1.1"
		keySuffix   = ".k"
		inboxSuffix = ".x"
	)
	var out []string
	if !row.wholesale {
		out = append(out, "$JS.API.INFO")
	}
	for _, s := range a2aProvisionedStreams {
		if !row.wholesale && !slices.Contains(row.streams[s], "STREAM.DELETE") {
			out = append(out, "$JS.API.STREAM.DELETE."+s)
		}
		if !slices.Contains(row.streams[s], "ACK") {
			out = append(out, "$JS.ACK."+s+ackSuffix)
		}
		if b, isBucket := strings.CutPrefix(s, a2aKVStreamPrefix); isBucket && !slices.Contains(row.streams[s], "KV") {
			out = append(out, "$KV."+b+keySuffix)
		}
	}
	for _, other := range users {
		if other != user {
			out = append(out, "_INBOX."+other+inboxSuffix)
		}
	}
	return out
}

// a2aGrantReporter is the part of testing.T that checkA2AUserGrants uses,
// so TestCheckA2AUserGrants can hand it a recorder and read what it refused.
type a2aGrantReporter interface {
	Helper()
	Errorf(format string, args ...any)
}

// a2aGrantRecorder collects what checkA2AUserGrants would have failed.
type a2aGrantRecorder struct{ errors []string }

func (r *a2aGrantRecorder) Helper() {}
func (r *a2aGrantRecorder) Errorf(format string, args ...any) {
	r.errors = append(r.errors, fmt.Sprintf(format, args...))
}

// checkA2AUserGrants asks the per-user property of one principal's publish
// and subscribe lists against its row, whichever render the lists came from.
func checkA2AUserGrants(t a2aGrantReporter, user string, row a2aGrantRow, lists map[string][]string) {
	t.Helper()
	if row.perConnection {
		// The row's assertion is the opposite of every other row's: the
		// entry holds nothing, because its authorization is minted per
		// connection. Anything in either list is a grant to every pod
		// behind the ServiceAccount.
		for _, section := range []string{"publish", "subscribe"} {
			if len(lists[section]) != 0 {
				t.Errorf("%s %s = %q; its authorization is minted per connection and its rendered entry holds no grants", user, section, lists[section])
			}
		}
		return
	}
	const (
		bareJetStreamAPI = "$JS.API.>"
		flowControl      = "$JS.FC.>"
		inboxPrefix      = "_INBOX."
		topicsPrefix     = "a2a.topics."
	)
	// The namespaces a grant may start in: the bus's own subjects, the
	// core-NATS heartbeats (agents.hb.>, spec-a2a-payloads' subject table),
	// JetStream, KV, and inboxes. A first token outside them is a grant
	// nothing here can read, and a wildcard there (">", "*.API.>", "*.>")
	// covers all five at once, which no literal spelling check would see.
	namespaces := []string{"a2a", "agents", "$JS", "$KV", "_INBOX"}
	ownInbox := inboxPrefix + user + ".>"
	reached := map[string]map[string]bool{}

	for _, section := range []string{"publish", "subscribe"} {
		grants := lists[section]
		// 1. Both lists exist and are non-empty.
		if len(grants) == 0 {
			t.Errorf("%s has no %s allow-list", user, section)
			continue
		}

		var inboxes []string
		for _, g := range grants {
			// 2. The first token names a namespace, literally; and the
			// wholesale grant appears only where the table records it.
			// Whether a wildcard further in covers a subject the row does
			// not record is asked by subject matching in the caller.
			if first, _, _ := strings.Cut(g, "."); !slices.Contains(namespaces, first) {
				t.Errorf("%s %s holds %q, whose first token %q is none of %v", user, section, g, first, namespaces)
				continue
			}
			if g == bareJetStreamAPI {
				if !row.wholesale {
					t.Errorf("%s %s holds %s, which the table does not record for it", user, section, g)
				}
				continue
			}

			// 4. Inbox entries, collected and checked below.
			if strings.HasPrefix(g, inboxPrefix) {
				inboxes = append(inboxes, g)
				if g != ownInbox {
					t.Errorf("%s %s holds inbox grant %q; the only inbox a user may name is %s", user, section, g, ownInbox)
				}
				continue
			}

			// 5. A topic publish spells its subject. Whether a wildcard
			// elsewhere in the list reaches a topic is asked by subject
			// matching in the caller, over every principal's topics.
			if section == "publish" && strings.HasPrefix(g, topicsPrefix) && strings.ContainsAny(g, "*>") {
				t.Errorf("%s publish holds topic wildcard %q; topic publishes are exact", user, g)
				continue
			}

			// 3. Every JetStream and KV grant names a stream and a verb
			// in the row, or is account-level discovery the row allows.
			isJS := strings.HasPrefix(g, "$JS.")
			isKV := strings.HasPrefix(g, "$KV.")
			if !isJS && !isKV {
				continue
			}
			if g == flowControl {
				// Push flow control's reply subject, neither API nor
				// ACK, and the only $JS. entry outside those.
				continue
			}
			if isJS && !strings.HasPrefix(g, "$JS.API.") && !strings.HasPrefix(g, "$JS.ACK.") {
				t.Errorf("%s %s holds %q, a $JS. subject that is neither API nor ACK nor %s", user, section, g, flowControl)
				continue
			}
			stream, verb, accountLevel := a2aGrantStream(g)
			switch {
			case accountLevel:
				if !slices.Contains(row.accountLevel, g) {
					t.Errorf("%s %s holds account-level %q, which its row does not allow", user, section, g)
				}
			case stream == "":
				t.Errorf("%s %s holds %q, a shape a2aGrantStream cannot place; a new verb needs a case there, and an argument", user, section, g)
			case !slices.Contains(row.streams[stream], verb):
				t.Errorf("%s %s holds %q: verb %s on stream %q, outside its row %v", user, section, g, verb, stream, row.streams)
			default:
				if reached[stream] == nil {
					reached[stream] = map[string]bool{}
				}
				reached[stream][verb] = true
			}
		}

		// 4. One inbox prefix per user, in each list.
		if !slices.Equal(inboxes, []string{ownInbox}) {
			t.Errorf("%s %s inbox grants = %q, want exactly [%s]", user, section, inboxes, ownInbox)
		}
	}

	// The row's other direction: a verb nothing holds is a stale row, and
	// a stale row is a grant waiting to be added unreviewed.
	for _, s := range slices.Sorted(maps.Keys(row.streams)) {
		for _, v := range row.streams[s] {
			if !reached[s][v] {
				t.Errorf("row %s allows %s on %q, which no grant of %s holds; drop it from the row", user, v, s, user)
			}
		}
	}
}

// The deployment spec states one property for every user
// (docs/designs/spec-nats-deployment.md, "Accounts and connection-time
// authorization", the Layout list): deny by default, the JetStream API
// enumerated per stream and per verb, ack grants per stream, topic publishes
// exact, one inbox prefix per user. Four fixes each narrowed one user and each
// left a test for that user; this is the same property asked of every APP
// principal at once, so a grant widened on a user nobody has fixed yet, or a
// new principal added to a2aIdentities, fails here rather than waiting for its
// own incident.
//
// The static users are read from the rendered nats.conf, because the render
// is what the server reads: a user the identities tests never see (callout,
// in the AUTH template) still appears there, and a rendering bug that dropped
// a list would too. The render is also held equal to the identity lists it
// came from. The callout-issued principals never reach nats.conf, so their
// lists are read from a2aIdentities and held to the same rows. A
// callout-issued principal whose entry carries no grants at all,
// because the callout mints each connection's authorization from the pod the
// API server attested, is recorded as perConnection and held to zero grants
// in both lists; an empty row without that flag fails rule 1, so the empty
// entry has to be claimed, not left.
//
// The table is the record of what each principal may reach. Adding a stream,
// a verb or a principal is expected to fail this test once, and the failure
// names the row or helper case to add; that one red is the review the
// widening gets.
func TestEveryNATSUserGrantIsEnumeratedAndStreamScoped(t *testing.T) {
	agent := a2aTestAgent()
	conf := string(buildA2ANATSConfigSecret(agent, a2aTestCreds(), a2aTestCalloutKeys(t)).Data["nats.conf"])

	const (
		bareJetStreamAPI = "$JS.API.>"
		sysUser          = "sys"
		// The writerless probe the provision script creates
		// (TestProbeTopicIsProvisionedAndWriterless); it is never a literal
		// publish grant, so it is added to the topic universe by hand.
		probeTopic = "a2a.topics.shared.probe"
	)

	kvSessionState := a2aKVStreamPrefix + "session-state"
	kvRuntimeState := a2aKVStreamPrefix + a2aRuntimeStateBucket
	rows := map[string]a2aGrantRow{
		"gateway": {
			streams: map[string][]string{
				a2aTasksStream: {"ACK"},
				kvSessionState: {"KV"},
			},
			wholesale: true,
		},
		a2aBridgeUser: {
			streams: map[string][]string{
				a2aTasksStream: {"STREAM.INFO", "CONSUMER.CREATE", "CONSUMER.MSG.NEXT", "DIRECT.GET", "ACK"},
				kvRuntimeState: {"KV", "STREAM.INFO", "CONSUMER.CREATE", "CONSUMER.DELETE"},
			},
		},
		// The other half of what `worker` held. Two streams, two read verbs,
		// no consumer verb anywhere and nothing on the task plane -- and it is
		// a callout row, so the whole list is served from the identity map
		// rather than from a user block in nats.conf.
		a2aAgentBusUser: {
			callout: true,
			streams: map[string][]string{
				a2aTopicsStateStream:   {"STREAM.INFO", "DIRECT.GET"},
				a2aTopicsJournalStream: {"STREAM.INFO", "DIRECT.GET"},
			},
		},
		"seed": {
			// a2aSeedJetStreamGrants enumerates CREATE and INFO over the
			// whole provisioned list, buckets included.
			streams:      a2aSameVerbsOn(a2aProvisionedStreams, "STREAM.CREATE", "STREAM.INFO"),
			accountLevel: []string{"$JS.API.INFO", "$JS.API.STREAM.NAMES"},
		},
		"web": {
			streams: a2aSameVerbsOn(
				[]string{a2aTasksStream, "DIRECTORY", a2aTopicsStateStream, a2aTopicsJournalStream},
				"STREAM.INFO", "CONSUMER.CREATE", "CONSUMER.INFO", "CONSUMER.MSG.NEXT"),
			accountLevel: []string{"$JS.API.INFO"},
		},
		"provision": {
			callout:      true,
			streams:      a2aSameVerbsOn(a2aProvisionedStreams, "STREAM.CREATE", "STREAM.INFO"),
			accountLevel: []string{"$JS.API.INFO", "$JS.API.STREAM.NAMES", "$JS.API.STREAM.LIST"},
		},
		"session": {
			// sessionIdentity lists nothing: the callout derives each
			// connection's grants from the attested pod, and a grant
			// recorded here would reach every session pod at once.
			callout:       true,
			perConnection: true,
		},
	}
	var staticRows, calloutRows []string
	for user, row := range rows {
		for s := range row.streams {
			if !slices.Contains(a2aProvisionedStreams, s) {
				t.Errorf("row %s names stream %q, which a2aProvisionedStreams does not; the table follows the provisioner, not the other way round", user, s)
			}
		}
		if row.perConnection && (!row.callout || len(row.streams) != 0 || len(row.accountLevel) != 0 || row.wholesale) {
			t.Errorf("row %s is per-connection, which is a callout-issued principal with no grants to record; it names %v, %v, wholesale=%v", user, row.streams, row.accountLevel, row.wholesale)
		}
		if row.callout {
			calloutRows = append(calloutRows, user)
		} else {
			staticRows = append(staticRows, user)
		}
	}

	// 6a. The users in the render are exactly the static rows, plus the two
	// the rows do not run: sys ($SYS holds no subject lists) and callout
	// (the AUTH account's one user, asserted on its own below). And the
	// callout issues exactly the callout rows.
	wantUsers := append(slices.Clone(staticRows), a2aCalloutConfUser, sysUser)
	slices.Sort(wantUsers)
	gotUsers := a2aConfUserNames(conf)
	slices.Sort(gotUsers)
	if !slices.Equal(gotUsers, wantUsers) {
		t.Fatalf("nats.conf users = %v, want %v; a new user needs a row in this test's table before it ships, and a row with no user is stale", gotUsers, wantUsers)
	}
	var gotCallout []string
	for _, id := range calloutIdentities(agent) {
		gotCallout = append(gotCallout, id.user)
	}
	slices.Sort(gotCallout)
	slices.Sort(calloutRows)
	if !slices.Equal(gotCallout, calloutRows) {
		t.Fatalf("callout-issued principals = %v, want %v; a new principal needs a row in this test's table before it ships", gotCallout, calloutRows)
	}

	// 6b. sys is the only user in $SYS, and it carries no permissions block:
	// the system account's own privileges are what it holds, and an allow
	// list would only narrow them into something that looks like a grant.
	sysStart := strings.Index(conf, "\n  SYS {")
	sysEnd := strings.Index(conf, "\nsystem_account:")
	if sysStart < 0 || sysEnd < sysStart {
		t.Fatal("cannot find the SYS account block in the rendered config")
	}
	if got := a2aConfUserNames(conf[sysStart:sysEnd]); !slices.Equal(got, []string{sysUser}) {
		t.Errorf("SYS account users = %v, want [%s]; no agent authenticates into the system account", got, sysUser)
	}
	if block := a2aConfUserBlock(t, conf, sysUser); strings.Contains(block, "permissions {") {
		t.Errorf("sys carries a permissions block:\n%s", block)
	}

	// 6c. The sys password is the operator's, not a workload's: no rendered
	// pod reads it from the creds Secret into an env var, by key or by
	// importing the whole Secret. Every pod that reads that Secret at all is
	// here, and so are the ones that should not: the gateway, the callout
	// and the agent pod each take another key from it, and a copied env var
	// with the wrong key would land in one of those.
	credsSecret := a2aCredsSecretName(agent)
	for name, spec := range map[string]corev1.PodSpec{
		"gateway Deployment": buildA2AGatewayDeployment(agent).Spec.Template.Spec,
		"NATS StatefulSet":   buildA2ANATSStatefulSet(agent, "conf-hash").Spec.Template.Spec,
		"provision Job":      buildA2AProvisionJob(agent).Spec.Template.Spec,
		"callout Deployment": buildA2ACalloutDeployment(agent).Spec.Template.Spec,
		"agent pod":          buildPodTemplateSpec(agent, "", "", "", "", nil, renderOptions{}).Spec,
	} {
		for _, ref := range a2aPodSpecEnvSecretRefs(spec) {
			if ref.secret == credsSecret && (ref.key == a2aSysPasswordKey || ref.key == "*") {
				t.Errorf("%s env %s reads %s:%s; the $SYS credential is for a person with a port-forward", name, ref.where, ref.secret, ref.key)
			}
		}
	}

	// callout: the AUTH account's one user, whose grant pair is rendered on
	// one line each and so never reaches a2aGrantSubjects. Asserted exactly:
	// read the authorization requests, answer them, nothing else.
	calloutBlock := a2aConfUserBlock(t, conf, a2aCalloutConfUser)
	for _, want := range []string{
		`subscribe { allow = [ "$SYS.REQ.USER.AUTH" ] }`,
		`publish { allow = [ "$SYS._INBOX.>" ] }`,
	} {
		if !strings.Contains(calloutBlock, want) {
			t.Errorf("callout lacks %s:\n%s", want, calloutBlock)
		}
	}
	if n := strings.Count(calloutBlock, "allow"); n != 2 {
		t.Errorf("callout's block has %d allow lists, want exactly 2:\n%s", n, calloutBlock)
	}

	// The lists under test: the static rows from the render, the callout
	// rows from a2aIdentities. The render is also held equal to the identity
	// it came from, so a rendering bug that dropped or reordered a grant is
	// its own failure rather than a row mismatch.
	identities := map[string]a2aIdentity{}
	for _, id := range a2aIdentities(agent) {
		identities[id.user] = id
	}
	lists := map[string]map[string][]string{}
	for _, user := range slices.Sorted(maps.Keys(rows)) {
		id := identities[user]
		if rows[user].callout {
			lists[user] = map[string][]string{"publish": id.publish, "subscribe": id.subscribe}
			continue
		}
		lists[user] = map[string][]string{
			"publish":   a2aGrantSubjects(t, conf, user, "publish"),
			"subscribe": a2aGrantSubjects(t, conf, user, "subscribe"),
		}
		if !slices.Equal(lists[user]["publish"], id.publish) || !slices.Equal(lists[user]["subscribe"], id.subscribe) {
			t.Errorf("%s's rendered lists differ from its identity.\n render publish:   %q\n identity publish: %q\n render subscribe:   %q\n identity subscribe: %q",
				user, lists[user]["publish"], id.publish, lists[user]["subscribe"], id.subscribe)
		}
	}

	// The wholesale grant: exactly the principals the table records.
	var wantWholesale, gotWholesale []string
	for user, row := range rows {
		if row.wholesale {
			wantWholesale = append(wantWholesale, user)
		}
		if slices.Contains(lists[user]["publish"], bareJetStreamAPI) || slices.Contains(lists[user]["subscribe"], bareJetStreamAPI) {
			gotWholesale = append(gotWholesale, user)
		}
	}
	slices.Sort(wantWholesale)
	slices.Sort(gotWholesale)
	if !slices.Equal(gotWholesale, wantWholesale) {
		t.Errorf("principals holding %s = %v; the table records %v", bareJetStreamAPI, gotWholesale, wantWholesale)
	}

	// 1 to 5, per principal, per list.
	for _, user := range slices.Sorted(maps.Keys(rows)) {
		checkA2AUserGrants(t, user, rows[user], lists[user])
	}

	// 7. The other half of 2 to 5, asked as the server would ask it. The
	// rules above read spellings, and a wildcard need not spell what it
	// covers: a2a.> and a2a.*.shared.blueprint cover a topic without
	// naming a2a.topics., *.API.> covers $JS.API.STREAM.DELETE.TASKS
	// without naming $JS, *.> in a subscribe list covers every inbox. So
	// every wildcard grant in either list of every principal is matched
	// against the subjects its row withholds (a2aReservedSubjects), and
	// every wildcard publish grant against every topic any principal
	// publishes literally plus the writerless probe.
	users := slices.Sorted(maps.Keys(rows))
	topics := []string{probeTopic}
	for _, user := range users {
		for _, g := range lists[user]["publish"] {
			if strings.HasPrefix(g, "a2a.topics.") && !strings.ContainsAny(g, "*>") && !slices.Contains(topics, g) {
				topics = append(topics, g)
			}
		}
	}
	for _, user := range users {
		reserved := a2aReservedSubjects(user, rows[user], users)
		for _, section := range []string{"publish", "subscribe"} {
			for _, g := range lists[user][section] {
				if !strings.ContainsAny(g, "*>") {
					continue
				}
				for _, subject := range reserved {
					if subjectMatches(g, subject) {
						t.Errorf("%s %s %q covers %s, which its row does not record", user, section, g, subject)
					}
				}
				if section != "publish" {
					continue
				}
				for _, topic := range topics {
					if subjectMatches(g, topic) {
						t.Errorf("%s publish %q covers topic %s without naming it; topic publishes are exact", user, g, topic)
					}
				}
			}
		}
	}
}

func TestA2AReservedSubjects(t *testing.T) {
	users := []string{"a", "b", "c"}
	row := a2aGrantRow{streams: map[string][]string{
		"TASKS":            {"ACK", "STREAM.DELETE"},
		"KV_runtime-state": {"KV"},
	}}
	got := a2aReservedSubjects("b", row, users)
	for _, want := range []string{
		"$JS.API.INFO",
		"$JS.API.STREAM.DELETE.DIRECTORY",
		"$JS.API.STREAM.DELETE.KV_runtime-state",
		"$JS.ACK.DIRECTORY.c.1.1.1.1.1",
		"$JS.ACK.KV_runtime-state.c.1.1.1.1.1",
		"$KV.session-state.k",
		"$KV.cap.k",
		"_INBOX.a.x",
		"_INBOX.c.x",
	} {
		if !slices.Contains(got, want) {
			t.Errorf("reserved subjects for b lack %s: %q", want, got)
		}
	}
	for _, notWant := range []string{
		"$JS.API.STREAM.DELETE.TASKS", // the row grants it
		"$JS.ACK.TASKS.c.1.1.1.1.1",   // the row grants ACK there
		"$KV.runtime-state.k",         // the row grants KV there
		"_INBOX.b.x",                  // its own inbox
	} {
		if slices.Contains(got, notWant) {
			t.Errorf("reserved subjects for b hold %s, which its row records: %q", notWant, got)
		}
	}
	if got := a2aReservedSubjects("a", a2aGrantRow{wholesale: true}, users); slices.ContainsFunc(got, func(s string) bool {
		return strings.HasPrefix(s, "$JS.API.")
	}) {
		t.Errorf("a wholesale row reserves JetStream API subjects its grant covers: %q", got)
	}
}

// TestCheckA2AUserGrants runs the per-user checker against a recorder, for
// the two shapes the invariant test's own table cannot show: a row with
// empty lists, which is refused rather than passed, and a per-connection
// row, which passes only while both lists stay empty.
func TestCheckA2AUserGrants(t *testing.T) {
	const user = "u"
	empty := map[string][]string{"publish": nil, "subscribe": nil}
	cases := []struct {
		name    string
		row     a2aGrantRow
		lists   map[string][]string
		wantErr string // a substring of one recorded failure, or "" for none
	}{
		{"empty lists on an ordinary row are refused", a2aGrantRow{}, empty, "has no publish allow-list"},
		{"empty lists on a callout row are refused", a2aGrantRow{callout: true}, empty, "has no publish allow-list"},
		{"a per-connection row passes with nothing rendered", a2aGrantRow{callout: true, perConnection: true}, empty, ""},
		{"a per-connection row fails when its entry gains a publish", a2aGrantRow{callout: true, perConnection: true},
			map[string][]string{"publish": {"a2a.topics.shared.blueprint"}}, "minted per connection"},
		{"a per-connection row fails when its entry gains a subscribe", a2aGrantRow{callout: true, perConnection: true},
			map[string][]string{"subscribe": {"_INBOX." + user + ".>"}}, "minted per connection"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := &a2aGrantRecorder{}
			checkA2AUserGrants(r, user, c.row, c.lists)
			if c.wantErr == "" {
				if len(r.errors) != 0 {
					t.Errorf("checker refused a row it should pass: %q", r.errors)
				}
				return
			}
			if !slices.ContainsFunc(r.errors, func(e string) bool { return strings.Contains(e, c.wantErr) }) {
				t.Errorf("checker did not refuse with %q; recorded %q", c.wantErr, r.errors)
			}
		})
	}
}

// ---- the inject backend (#1704) ------------------------------------------
//
// The gateway's inject door is an HTTP door into handleInbound, for the eval
// harness. Everything below is about the properties that keep it from being a
// hole: it is rendered only when the operator itself was deployed with the
// flag, every request to it carries a bearer token this operator mints, the
// only identity it admits is an eval one, and while it is rendered the
// gateway pod is fenced against every pod on the network.

// a2aInjectAgentState reconciles a next-mode agent twice (finalizer pass,
// then the real one), reports the auth callout serving and reconciles past
// the gateway gate, and hands back the client, so each inject test states
// only what it is asserting. The gate matters here: the door's env rides on
// the gateway Deployment, and the flag-off removal is ordered after its
// apply, so a test that never let the gateway through would assert against
// a Deployment the render withheld.
func a2aInjectAgentState(t *testing.T) (client.Client, *agentv1alpha1.PlatformAgent, *PlatformAgentReconciler, ctrl.Request) {
	t.Helper()
	scheme := setupScheme()
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d failed: %v", i+1, err)
		}
	}
	letTheGatewayThrough(t, ctx, cl, r, req, agent)
	return cl, agent, r, req
}

// a2aGatewayEnv reads the rendered gateway container's environment by name.
func a2aGatewayEnv(t *testing.T, cl client.Client, agent *agentv1alpha1.PlatformAgent) map[string]corev1.EnvVar {
	t.Helper()
	dep := &appsv1.Deployment{}
	key := types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}
	if err := cl.Get(context.Background(), key, dep); err != nil {
		t.Fatalf("gateway Deployment: %v", err)
	}
	env := map[string]corev1.EnvVar{}
	for _, e := range dep.Spec.Template.Spec.Containers[0].Env {
		env[e.Name] = e
	}
	return env
}

// TestA2AInjectBackendIsOffWithoutTheFlag is the property the whole design
// rests on: an ordinary next install renders no inject door at all. Not a
// closed one, not one behind a fence -- none, so there is nothing to
// misconfigure and nothing to find.
func TestA2AInjectBackendIsOffWithoutTheFlag(t *testing.T) {
	agent := a2aTestAgent()

	// The render, first: nothing in the pod spec even mentions it.
	dep := buildA2AGatewayDeployment(agent)
	container := dep.Spec.Template.Spec.Containers[0]
	for _, e := range container.Env {
		if e.Name == a2aInjectListenEnvVar {
			t.Errorf("%s is rendered without the flag", a2aInjectListenEnvVar)
		}
		if e.Name == "A2A_PRINCIPAL_MAP" {
			t.Errorf("A2A_PRINCIPAL_MAP is repointed without the flag: %+v", e)
		}
	}
	if len(container.Ports) != 0 {
		t.Errorf("the gateway publishes %d container ports without the flag", len(container.Ports))
	}
	for _, v := range dep.Spec.Template.Spec.Volumes {
		if strings.Contains(v.Name, "inject") {
			t.Errorf("an inject volume is mounted without the flag: %s", v.Name)
		}
	}

	// And the cluster: no Service to reach, no map to be admitted by.
	cl, agent, _, _ := a2aInjectAgentState(t)
	ctx := context.Background()
	name := types.NamespacedName{Name: a2aInjectName(agent), Namespace: agent.Namespace}
	for _, obj := range []client.Object{&corev1.Service{}, &corev1.ConfigMap{}, &networkingv1.NetworkPolicy{}} {
		if err := cl.Get(ctx, name, obj); !errors.IsNotFound(err) {
			t.Errorf("%T %s exists without the flag (err=%v)", obj, name.Name, err)
		}
	}
}

// TestA2AInjectBackendRendersUnderTheFlag: with the operator deployed with
// the flag, the gateway gets a listener, a map that admits exactly the eval
// author, and a ClusterIP to reach it on.
func TestA2AInjectBackendRendersUnderTheFlag(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "true")
	cl, agent, _, _ := a2aInjectAgentState(t)
	ctx := context.Background()

	env := a2aGatewayEnv(t, cl, agent)
	listen := env[a2aInjectListenEnvVar]
	if listen.Value != fmt.Sprintf(":%d", a2aInjectPort) {
		t.Errorf("%s = %q, want every interface on the inject port", a2aInjectListenEnvVar, listen.Value)
	}
	// The map the gateway reads has to be the one the operator rendered; the
	// default path is the hand-made Discord map, which carries no eval
	// author, and a gateway reading it drops every injected message at
	// verification with nothing in the render looking wrong.
	if got := env[a2aInjectPrincipalMapEnv].Value; got != a2aInjectPrincipalMapPath {
		t.Errorf("%s = %q, want the operator's own map at %q",
			a2aInjectPrincipalMapEnv, got, a2aInjectPrincipalMapPath)
	}
	// Beside the chat map rather than over it. The door may be armed next to
	// a real backend now, and repointing the one variable would take that
	// backend's identities away with it.
	if got := env["A2A_PRINCIPAL_MAP"].Value; got == a2aInjectPrincipalMapPath {
		t.Error("the door repointed A2A_PRINCIPAL_MAP, which is the chat backends' map")
	}
	// The token, from the Secret this operator mints, and NOT optional: a
	// missing Secret must crash-loop the pod rather than leave the gateway
	// to arm a door with no token -- which it would refuse to do anyway.
	tokenRef := env[a2aInjectTokenEnvVar].ValueFrom
	if tokenRef == nil || tokenRef.SecretKeyRef == nil {
		t.Fatalf("%s is not read from a Secret: %+v", a2aInjectTokenEnvVar, env[a2aInjectTokenEnvVar])
	}
	if tokenRef.SecretKeyRef.Name != a2aInjectName(agent) || tokenRef.SecretKeyRef.Key != a2aInjectTokenKey {
		t.Errorf("the token comes from %s/%s, want %s/%s", tokenRef.SecretKeyRef.Name,
			tokenRef.SecretKeyRef.Key, a2aInjectName(agent), a2aInjectTokenKey)
	}
	if tokenRef.SecretKeyRef.Optional != nil && *tokenRef.SecretKeyRef.Optional {
		t.Error("the token reference is optional, so a pod could start with an empty token")
	}
	if env[a2aInjectTokenEnvVar].Value != "" {
		t.Error("the token is rendered as a literal env value rather than a Secret reference")
	}

	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, dep); err != nil {
		t.Fatal(err)
	}
	container := dep.Spec.Template.Spec.Containers[0]
	var mounted bool
	for _, m := range container.VolumeMounts {
		if m.MountPath == a2aInjectPrincipalMapDir {
			mounted = true
			if !m.ReadOnly {
				t.Error("the inject principal map is mounted writable")
			}
		}
	}
	if !mounted {
		t.Errorf("no volume is mounted at %s, so the gateway would read an empty map", a2aInjectPrincipalMapDir)
	}
	// The other map stays mounted and unread: two ConfigMaps cannot share a
	// path, and an install with this backend has no Discord one anyway.
	var stillHasDefault bool
	for _, v := range dep.Spec.Template.Spec.Volumes {
		if v.Name == "principal-map" {
			stillHasDefault = true
		}
	}
	if !stillHasDefault {
		t.Error("the flag removed the default principal-map volume; it is shared with the chat backends")
	}

	cm := &corev1.ConfigMap{}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aInjectName(agent), Namespace: agent.Namespace}, cm); err != nil {
		t.Fatalf("inject principal map: %v", err)
	}
	// One line, "inject:<author> eval:<...>". Both halves are load-bearing:
	// the gateway looks up the prefixed key in this map alone, and refuses
	// any value outside the eval namespace -- which is what keeps a door
	// taking its author from a request body unable to assert a principal a
	// real backend's sender could hold.
	fixture := cm.Data[a2aInjectPrincipalMapKey]
	lines := strings.Fields(strings.TrimSpace(fixture))
	if len(cm.Data) != 1 || len(lines) != 2 {
		t.Fatalf("the map is %q under %d keys, want one prefixed entry", fixture, len(cm.Data))
	}
	if lines[0] != a2aInjectPrincipalPrefix+a2aInjectAuthor {
		t.Errorf("the map's key is %q, want it qualified with %q", lines[0], a2aInjectPrincipalPrefix)
	}
	if !strings.HasPrefix(lines[1], "eval:") {
		t.Errorf("the map admits %q, which is not an eval identity; a synthetic door must be "+
			"structurally incapable of asserting a cloud principal", lines[1])
	}

	secret := &corev1.Secret{}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aInjectName(agent), Namespace: agent.Namespace}, secret); err != nil {
		t.Fatalf("the door's bearer token Secret was not rendered: %v", err)
	}
	if len(secret.Data[a2aInjectTokenKey]) < a2aInjectTokenNumBytes {
		t.Errorf("the token is %d bytes; it is the door's whole access control",
			len(secret.Data[a2aInjectTokenKey]))
	}

	svc := &corev1.Service{}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aInjectName(agent), Namespace: agent.Namespace}, svc); err != nil {
		t.Fatalf("inject Service: %v", err)
	}
	// ClusterIP is the access control: a LoadBalancer or a NodePort would
	// publish an unauthenticated task-submission endpoint off-cluster.
	if svc.Spec.Type != corev1.ServiceTypeClusterIP {
		t.Errorf("inject Service type = %q, want ClusterIP", svc.Spec.Type)
	}
	if svc.Spec.Selector["app"] != a2aGatewayName(agent) {
		t.Errorf("inject Service selects %v, want the gateway pod", svc.Spec.Selector)
	}
	if len(svc.Spec.Ports) != 1 || svc.Spec.Ports[0].Port != a2aInjectPort {
		t.Errorf("inject Service ports = %+v, want just the inject port", svc.Spec.Ports)
	}
	// The listener and the Service must agree, or a port-forward reaches a
	// closed port and reads as a broken gateway.
	if !strings.HasSuffix(listen.Value, fmt.Sprintf(":%d", svc.Spec.Ports[0].Port)) {
		t.Errorf("the gateway listens on %q and the Service publishes %d", listen.Value, svc.Spec.Ports[0].Port)
	}
}

// TestA2AInjectFenceDeniesEveryPod: the fence is the whole of what withholds
// an unauthenticated endpoint from the cluster, so it has to select the
// gateway pod and admit nobody. An ingress rule appearing here later is a
// decision someone has to make deliberately.
func TestA2AInjectFenceDeniesEveryPod(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "true")
	cl, agent, _, _ := a2aInjectAgentState(t)

	np := &networkingv1.NetworkPolicy{}
	key := types.NamespacedName{Name: a2aInjectName(agent), Namespace: agent.Namespace}
	if err := cl.Get(context.Background(), key, np); err != nil {
		t.Fatalf("the gateway fence was not rendered with the inject backend: %v", err)
	}
	if np.Spec.PodSelector.MatchLabels["app"] != a2aGatewayName(agent) {
		t.Errorf("the fence selects %v, want the gateway pod", np.Spec.PodSelector.MatchLabels)
	}
	if len(np.Spec.PolicyTypes) != 1 || np.Spec.PolicyTypes[0] != networkingv1.PolicyTypeIngress {
		t.Errorf("policyTypes = %v, want Ingress alone -- Egress here would cut the gateway off the bus",
			np.Spec.PolicyTypes)
	}
	if len(np.Spec.Ingress) != 0 {
		t.Errorf("the fence admits %d ingress rules; the inject port must be reachable from no pod, "+
			"only through the node path a port-forward uses", len(np.Spec.Ingress))
	}
}

// TestA2AInjectBackendIsRemovedWhenTheFlagGoesOff: unsetting the operator's
// flag has to take the door with it. Left behind, a Service and a map would
// sit on an install that is supposed to look like it never had one -- and the
// next operator roll that re-reads the flag would find them already there.
func TestA2AInjectBackendIsRemovedWhenTheFlagGoesOff(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "true")
	cl, agent, r, req := a2aInjectAgentState(t)
	ctx := context.Background()
	key := types.NamespacedName{Name: a2aInjectName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, key, &corev1.Service{}); err != nil {
		t.Fatalf("the Service was not rendered, so this test proves nothing: %v", err)
	}

	t.Setenv(a2aInjectBackendEnvVar, "")
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after the flag went off: %v", err)
	}
	for _, obj := range a2aInjectRenderedKinds() {
		if err := cl.Get(ctx, key, obj); !errors.IsNotFound(err) {
			t.Errorf("%T %s survived the flag going off (err=%v)", obj, key.Name, err)
		}
	}
	if env := a2aGatewayEnv(t, cl, agent); env[a2aInjectListenEnvVar].Value != "" {
		t.Errorf("the gateway still listens on %q after the flag went off", env[a2aInjectListenEnvVar].Value)
	}
}

// TestA2AInjectFlagOffRemovalReadsTheSecretUncached: the flag-off path reads
// four objects before deleting them, and the fourth is a Secret. The operator
// ships secrets with get only, so a cached Get of one starts an informer
// whose LIST is forbidden and blocks the single reconcile worker for good --
// the fake client cannot show that hang (its cache is the store), so this
// test fails the cached path outright and passes only if the Secret is read
// through the reader the other A2A Secrets use.
func TestA2AInjectFlagOffRemovalReadsTheSecretUncached(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "true")
	scheme := setupScheme()
	agent := a2aTestAgent()
	base := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	// The "cache": the same store, refusing a Secret read once armed. Armed
	// only for the flag-off reconcile, so the setup reconciles that mint the
	// Secret through the reader are not what is under test.
	var strict atomic.Bool
	cached := interceptor.NewClient(base, interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, ok := obj.(*corev1.Secret); ok && strict.Load() {
				return fmt.Errorf("cached Get of Secret %s: on the shipped RBAC this starts an informer whose LIST is forbidden, and the reconcile worker blocks in WaitForCacheSync", key)
			}
			return c.Get(ctx, key, obj, opts...)
		},
	})
	r := &PlatformAgentReconciler{Client: cached, APIReader: base, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d with the flag on: %v", i+1, err)
		}
	}
	key := types.NamespacedName{Name: a2aInjectName(agent), Namespace: agent.Namespace}
	if err := base.Get(ctx, key, &corev1.Secret{}); err != nil {
		t.Fatalf("the token Secret was not minted, so this test proves nothing: %v", err)
	}

	strict.Store(true)
	t.Setenv(a2aInjectBackendEnvVar, "")
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after the flag went off read a Secret through the cache: %v", err)
	}
	if err := base.Get(ctx, key, &corev1.Secret{}); !errors.IsNotFound(err) {
		t.Fatalf("the token Secret survived the flag going off (err=%v)", err)
	}
}

// TestA2AInjectBackendGoesAwayOnAFlipToToday: the darkness property. A today
// install must carry no A2A object, and the inject backend's three are
// exactly the kind that get forgotten -- they are rendered by a branch the
// teardown path never consults.
func TestA2AInjectBackendGoesAwayOnAFlipToToday(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "true")
	cl, agent, r, req := a2aInjectAgentState(t)
	ctx := context.Background()
	key := types.NamespacedName{Name: a2aInjectName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, key, &corev1.Service{}); err != nil {
		t.Fatalf("the Service was not rendered, so this test proves nothing: %v", err)
	}

	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatal(err)
	}
	fresh.Spec.Mode = nil
	if err := cl.Update(ctx, fresh); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after the flip to today: %v", err)
	}
	for _, obj := range a2aInjectRenderedKinds() {
		if err := cl.Get(ctx, key, obj); !errors.IsNotFound(err) {
			t.Errorf("%T %s survived the flip to today (err=%v)", obj, key.Name, err)
		}
	}
}

// a2aInjectRenderedKinds is every kind the door renders, for the tests that
// assert it is entirely gone. A list rather than three literals: the Secret
// was the fourth, and the next one must not be missed the same way.
func a2aInjectRenderedKinds() []client.Object {
	return []client.Object{
		&corev1.Service{},
		&corev1.ConfigMap{},
		&networkingv1.NetworkPolicy{},
		&corev1.Secret{},
	}
}

// TestA2AInjectTokenIsMintedOnceAndKept: the token is a live credential a
// caller holds for the length of an eval run. Re-rolling it on a reconcile --
// which happens every few seconds -- would 401 a run mid-flight, and the
// failure would read as a broken gateway rather than as a rotated secret.
func TestA2AInjectTokenIsMintedOnceAndKept(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "true")
	cl, agent, r, req := a2aInjectAgentState(t)
	ctx := context.Background()
	key := types.NamespacedName{Name: a2aInjectName(agent), Namespace: agent.Namespace}

	first := &corev1.Secret{}
	if err := cl.Get(ctx, key, first); err != nil {
		t.Fatalf("the token Secret was not rendered: %v", err)
	}
	minted := string(first.Data[a2aInjectTokenKey])
	if minted == "" {
		t.Fatal("the token is empty, so the gateway would refuse to arm the door")
	}

	for i := 0; i < 3; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i, err)
		}
	}
	again := &corev1.Secret{}
	if err := cl.Get(ctx, key, again); err != nil {
		t.Fatal(err)
	}
	if string(again.Data[a2aInjectTokenKey]) != minted {
		t.Error("the token changed across reconciles; every caller holding it is now refused")
	}
}

// TestA2AInjectTokenIsRepairedIfEmptied: an empty key renders an empty env,
// and the gateway refuses to arm the door without a token -- so the pod would
// crash-loop with nothing in the object set looking wrong. Emptied is the
// shape a hand-edit or a half-written Secret takes.
func TestA2AInjectTokenIsRepairedIfEmptied(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "true")
	cl, agent, r, req := a2aInjectAgentState(t)
	ctx := context.Background()
	key := types.NamespacedName{Name: a2aInjectName(agent), Namespace: agent.Namespace}

	secret := &corev1.Secret{}
	if err := cl.Get(ctx, key, secret); err != nil {
		t.Fatal(err)
	}
	secret.Data[a2aInjectTokenKey] = []byte("")
	if err := cl.Update(ctx, secret); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after the token was emptied: %v", err)
	}
	repaired := &corev1.Secret{}
	if err := cl.Get(ctx, key, repaired); err != nil {
		t.Fatal(err)
	}
	if len(repaired.Data[a2aInjectTokenKey]) == 0 {
		t.Error("an emptied token was left empty, so the door can never arm")
	}
}

// TestA2AInjectTokenIsNotRenderedWithoutTheFlag: the darkness property
// applied to the credential. A Secret is the one object here whose presence
// on an install that never asked for the door would be more than residue.
func TestA2AInjectTokenIsNotRenderedWithoutTheFlag(t *testing.T) {
	cl, agent, _, _ := a2aInjectAgentState(t)
	key := types.NamespacedName{Name: a2aInjectName(agent), Namespace: agent.Namespace}
	if err := cl.Get(context.Background(), key, &corev1.Secret{}); !errors.IsNotFound(err) {
		t.Errorf("a bearer token Secret exists without the flag (err=%v)", err)
	}
}

// The split A5 made is enforced by the pod spec, and spec.deployment is
// user-authored: sidecars, init containers and their volumes are copied
// verbatim into the render. So the one thing that keeps the projected bus token
// in the platform-agent container is that nothing else mounts it by that name,
// and until
// this test nothing made that true — a2aBusTokenVolume is a public string in
// the rendered Deployment, the volume is in the pod under `next`, and the
// bridge image is built FROM the platform-agent image so it ships the client
// that would read it. A sidecar holding this token AND bridge-password holds
// the union of the two grant sets, which is `worker` rebuilt.
//
// Absence, per container, plus the volume-name shadow: two volumes with one
// name is a Deployment server-side apply refuses, so that arm is a wedge guard
// as well.
//
// Scope, so the name is not read as more than it pins: this is the NAME-based
// reservation. A user volume projecting the a2a-bus audience under some other
// name defeats the reservation and leaves this test passing, which is what the
// ByName is doing in the name. The source check is
// TestUserAuthoredVolumesCannotCarryTheBusCredentialBySource, its sibling in
// platformagent_a2a_bus_source_test.go.
func TestUserAuthoredContainersCannotMountTheBusTokenByName(t *testing.T) {
	grab := func(agent *agentv1alpha1.PlatformAgent) corev1.PodSpec {
		t.Helper()
		return buildPodTemplateSpec(agent, "", "", "", "", nil, renderOptions{}).Spec
	}
	mount := corev1.VolumeMount{Name: a2aBusTokenVolume, MountPath: "/var/run/secrets/a2a-bus", ReadOnly: true}

	agent := a2aTestAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Sidecars: []corev1.Container{{
			Name: "hermes-bridge", Image: "bridge:dev",
			VolumeMounts: []corev1.VolumeMount{{Name: "agent-data", MountPath: "/opt/data"}, mount},
		}},
		InitContainers: []corev1.Container{{
			Name: "peek", Image: "busybox", VolumeMounts: []corev1.VolumeMount{mount},
		}},
		SidecarVolumes: []corev1.Volume{{
			Name: a2aBusTokenVolume, VolumeSource: corev1.VolumeSource{
				Secret: &corev1.SecretVolumeSource{SecretName: "attacker"},
			},
		}},
	}
	spec := grab(agent)

	// Precondition: the surface is up and the real volume is in the pod, so an
	// absent mount below means the strip ran rather than the feature being off.
	var projected int
	for _, v := range spec.Volumes {
		if v.Name != a2aBusTokenVolume {
			continue
		}
		projected++
		if v.Projected == nil {
			t.Errorf("the pod's %s volume is %+v, not the operator's projection; a user-supplied "+
				"volume shadowed it", a2aBusTokenVolume, v)
		}
	}
	if projected != 1 {
		t.Fatalf("the pod carries %d volumes named %s, want exactly 1 (the operator's projection); "+
			"a duplicate name is refused by server-side apply and wedges every reconcile", projected, a2aBusTokenVolume)
	}

	holders := map[string]bool{}
	for _, c := range slices.Concat(spec.Containers, spec.InitContainers) {
		for _, m := range c.VolumeMounts {
			if m.Name == a2aBusTokenVolume {
				holders[c.Name] = true
			}
		}
	}
	if !holders["platform-agent"] {
		t.Error("the platform-agent container lost its bus token mount; the strip is too wide and the CLI " +
			"will fall through to a password that is no longer rendered")
	}
	delete(holders, "platform-agent")
	if len(holders) != 0 {
		t.Errorf("containers other than platform-agent mount %s: %v -- one of them plus bridge-password "+
			"is the retired `worker` credential rebuilt", a2aBusTokenVolume, slices.Sorted(maps.Keys(holders)))
	}

	// The sidecar keeps everything it is entitled to.
	for _, c := range spec.Containers {
		if c.Name != "hermes-bridge" {
			continue
		}
		if len(c.VolumeMounts) != 1 || c.VolumeMounts[0].Name != "agent-data" {
			t.Errorf("the strip took a mount it has no claim on: %+v", c.VolumeMounts)
		}
	}

	// The webhook says why; this render is what holds when the webhook is
	// unreachable (chart default failurePolicy: Ignore).
	if _, reserved := agentv1alpha1.ReservedVolumeNames[a2aBusTokenVolume]; !reserved {
		t.Errorf("%s is not in ReservedVolumeNames, so the webhook admits a sidecar that mounts it and "+
			"the author gets a silent strip instead of a field.Forbidden", a2aBusTokenVolume)
	}

	// Under today there is no such volume and the CR's containers are its
	// author's business.
	today := a2aTestAgent()
	today.Spec.Mode = ptr.To("today")
	today.Spec.Deployment = agent.Spec.Deployment.DeepCopy()
	var sidecarMounts int
	for _, c := range grab(today).Containers {
		if c.Name != "hermes-bridge" {
			continue
		}
		sidecarMounts = len(c.VolumeMounts)
	}
	if sidecarMounts != 2 {
		t.Errorf("a today install's sidecar has %d mounts, want 2; the strip is not gated on the surface, "+
			"which is one more way to tell the next stack exists", sidecarMounts)
	}
}

// The fifth field, and the one the test above does not reach.
//
// spec.deployment.extraVolumeMounts names no container, which is why it did
// not read like a surface the reservation had to cover -- but
// buildBaseContainers appends it verbatim to the platform-agent container AND
// to platform-agent-dashboard (its own comment there says "extraVolumeMounts
// reaches both containers"). So the one CR field that mentions no container at
// all is the one that puts the projected bus token into a SECOND container,
// and the author never wrote a sidecar. The dashboard runs the same image as
// the agent, so it already ships the `a2a` client that would read the file:
// one entry, and the pod has two workloads on the bus wearing the `agent`
// identity, which is the split A5 made undone by four lines of YAML.
//
// Measured on the rendered pod rather than on the presence of a guard. A test
// that asserted "extraVolumeMounts is in ReservedVolumeNames' docstring" would
// pass against the hole this closes.
func TestTheDashboardNeverReceivesTheBusToken(t *testing.T) {
	const ordinary = "my-scratch"
	dep := func() *agentv1alpha1.DeploymentSpec {
		return &agentv1alpha1.DeploymentSpec{
			ExtraVolumeMounts: []corev1.VolumeMount{
				{Name: ordinary, MountPath: "/scratch"},
				{Name: a2aBusTokenVolume, MountPath: "/var/run/secrets/stolen"},
			},
		}
	}

	agent := a2aTestAgent()
	agent.Spec.Deployment = dep()
	spec := buildPodTemplateSpec(agent, "", "", "", "", nil, renderOptions{}).Spec

	// Precondition. Without the projection in the pod an absent mount below
	// says only that the feature is off, and every assertion here is vacuous.
	if podVolume(corev1.PodTemplateSpec{Spec: spec}, a2aBusTokenVolume) == nil {
		t.Fatalf("the pod carries no %s volume, so this test proves nothing about who mounts it",
			a2aBusTokenVolume)
	}
	var dashboard bool
	for _, c := range spec.Containers {
		if c.Name == "platform-agent-dashboard" {
			dashboard = true
		}
	}
	if !dashboard {
		t.Fatal("the render produced no platform-agent-dashboard container; the container this test " +
			"is about is not in the pod, so its absence from the holder set below means nothing")
	}

	holders := map[string][]string{}
	for _, c := range slices.Concat(spec.Containers, spec.InitContainers) {
		for _, m := range c.VolumeMounts {
			if m.Name == a2aBusTokenVolume {
				holders[c.Name] = append(holders[c.Name], m.MountPath)
			}
		}
	}
	// The agent keeps exactly the operator's mount at the operator's path. The
	// CR's own entry is stripped there too: a second mount of the same volume
	// at a path of the author's choosing is not an escalation inside the one
	// container entitled to the token, but it is the CR deciding where a
	// credential appears, and a2aBusTokenPath is the path a2a/lib reads.
	if got := holders["platform-agent"]; len(got) != 1 || got[0] != a2aBusTokenPath {
		t.Errorf("the platform-agent container mounts %s at %v, want exactly [%s]: the operator's mount "+
			"and nothing the CR added", a2aBusTokenVolume, got, a2aBusTokenPath)
	}
	delete(holders, "platform-agent")
	if len(holders) != 0 {
		t.Errorf("containers other than platform-agent mount %s: %v. spec.deployment.extraVolumeMounts "+
			"reaches platform-agent-dashboard as well as the agent, so this entry is a second workload "+
			"wearing the agent's bus identity -- the retired `worker` credential rebuilt from a CR field "+
			"that names no container", a2aBusTokenVolume, holders)
	}

	// Not too wide: an ordinary extraVolumeMount still reaches both.
	for _, c := range spec.Containers {
		if c.Name != "platform-agent" && c.Name != "platform-agent-dashboard" {
			continue
		}
		if !slices.ContainsFunc(c.VolumeMounts, func(m corev1.VolumeMount) bool { return m.Name == ordinary }) {
			t.Errorf("the %s container lost its %s mount; the strip is taking mounts it has no claim on",
				c.Name, ordinary)
		}
	}

	// Gated on the surface, like every other strip: a today install has no such
	// volume and the CR's mount list is its author's business.
	today := a2aTestAgent()
	today.Spec.Mode = ptr.To("today")
	today.Spec.Deployment = dep()
	var kept int
	for _, c := range buildPodTemplateSpec(today, "", "", "", "", nil, renderOptions{}).Spec.Containers {
		for _, m := range c.VolumeMounts {
			if m.Name == a2aBusTokenVolume {
				kept++
			}
		}
	}
	if kept == 0 {
		t.Error("a today install lost the CR's extraVolumeMounts entry too; the strip is not gated on " +
			"the surface, which is one more way to tell the next stack exists")
	}
}

// The fourth field, and the one neither test above reaches.
//
// spec.deployment.sidecarVolumes and spec.deployment.extraVolumes are two
// separate slices that land in the same pod-level volume list, and the render
// strips each with its own call —
// TestUserAuthoredContainersCannotMountTheBusTokenByName puts its shadow in
// sidecarVolumes only. Measured before this test was written: deleting
// `extraVolumes = a2aStripBusTokenVolume(extraVolumes)` from
// buildPodTemplateSpec left the whole operator suite green, so the reservation
// row that claims a render strip for ExtraVolumes
// (TestEveryUserAuthoredMountSurfaceIsReserved) was claiming coverage that did
// not exist. That is the same defect class A5 shipped to close, one field over.
//
// Why extraVolumes is its own surface and not a rewording of sidecarVolumes:
// it needs no container. A CR that declares no sidecar and no init container
// at all can still add a volume named a2a-bus-token, so the author never
// writes anything that looks like it is reaching for the agent's identity.
// The entry is appended AFTER the operator's projection, and both halves of
// a2aStripBusTokenVolume's argument apply to it — a Secret or hostPath under
// that name is a credential of the author's choosing presented as the pod's,
// and two volumes with one name is a Deployment server-side apply refuses,
// which wedges every reconcile of the CR with nothing in status to say which
// field did it.
//
// The webhook refuses the entry and says why, which is the half that gives the
// author a field.Forbidden; this render is the half that holds when the
// webhook is unreachable, which the chart's default failurePolicy: Ignore
// makes the ordinary case rather than the exotic one.
func TestAnExtraVolumesEntryCannotShadowTheBusToken(t *testing.T) {
	const ordinary = "my-scratch"
	dep := func() *agentv1alpha1.DeploymentSpec {
		return &agentv1alpha1.DeploymentSpec{
			// No sidecars, no initContainers, no sidecarVolumes: the only
			// strip that can produce the pod asserted below is the
			// extraVolumes one, so this test goes red for that call alone.
			ExtraVolumes: []corev1.Volume{
				{Name: ordinary, VolumeSource: corev1.VolumeSource{
					EmptyDir: &corev1.EmptyDirVolumeSource{},
				}},
				{Name: a2aBusTokenVolume, VolumeSource: corev1.VolumeSource{
					Secret: &corev1.SecretVolumeSource{SecretName: "attacker"},
				}},
			},
		}
	}

	agent := a2aTestAgent()
	agent.Spec.Deployment = dep()
	pod := buildPodTemplateSpec(agent, "", "", "", "", nil, renderOptions{})

	// Precondition. If the surface were off there would be no projection to
	// shadow, and every assertion below would hold over a pod this test is
	// not about.
	if !a2aAgentSurface(agent) {
		t.Fatal("the a2a agent surface is off for this fixture, so the pod carries no bus token and " +
			"nothing here measures a shadow of it")
	}

	var named []corev1.Volume
	for _, v := range pod.Spec.Volumes {
		if v.Name == a2aBusTokenVolume {
			named = append(named, v)
		}
	}
	if len(named) != 1 {
		t.Fatalf("the pod carries %d volumes named %s, want exactly 1: the CR's extraVolumes entry "+
			"survived the strip. Two volumes with one name is refused by server-side apply, so the "+
			"Deployment never applies and every reconcile of this CR wedges", len(named), a2aBusTokenVolume)
	}
	// And the one that survived is the operator's, not the author's. A strip
	// that removed the projection and kept the Secret would leave the count at
	// 1 and hand the agent container a credential the CR chose.
	if !reflect.DeepEqual(named[0], a2aBusTokenVolumeSource()) {
		t.Errorf("the pod's %s volume is %+v, not the operator's projection; the CR's entry is what the "+
			"platform-agent container would mount at %s, which is the file a2a/lib reads",
			a2aBusTokenVolume, named[0], a2aBusTokenPath)
	}

	// The agent keeps its mount: the strip is on the volume list, and taking
	// the projection away would leave the container mounting a name no volume
	// answers to.
	var mounted bool
	for _, c := range pod.Spec.Containers {
		if c.Name != "platform-agent" {
			continue
		}
		mounted = slices.ContainsFunc(c.VolumeMounts, func(m corev1.VolumeMount) bool {
			return m.Name == a2aBusTokenVolume
		})
	}
	if !mounted {
		t.Error("the platform-agent container no longer mounts the bus token; the strip is too wide and " +
			"the CLI falls through to a password that is no longer rendered")
	}

	// Not too wide: an ordinary extraVolumes entry is still the author's.
	if podVolume(pod, ordinary) == nil {
		t.Errorf("the pod lost its %s volume; the strip is taking entries it has no claim on", ordinary)
	}

	// Gated on the surface, like every other strip: a today install has no
	// projected bus token, and a volume the next stack has never heard of is
	// the CR author's business.
	today := a2aTestAgent()
	today.Spec.Mode = ptr.To("today")
	today.Spec.Deployment = dep()
	if podVolume(buildPodTemplateSpec(today, "", "", "", "", nil, renderOptions{}), a2aBusTokenVolume) == nil {
		t.Error("a today install lost the CR's extraVolumes entry too; the strip is not gated on the " +
			"surface, which is one more way to tell the next stack exists")
	}
}
