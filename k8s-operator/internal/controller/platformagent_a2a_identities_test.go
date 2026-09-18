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
	"slices"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func identityTestAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "agent", Namespace: "kubeagents-system"},
	}
}

// The inbox trap, pinned. Push delivery and every JetStream API request come
// back on an inbox subject, each principal is granted only its own prefix, and
// the client sets that prefix from its user name. A principal whose subscribe
// list does not contain its own prefix authenticates, publishes, and then hangs
// on the first reply — found live twice in W6 (the provision Job could never
// succeed; no consumer could ever ack). This asserts the property rather than
// the spelling of any one grant.
func TestEveryPrincipalMaySubscribeToItsOwnInbox(t *testing.T) {
	for _, id := range a2aIdentities(identityTestAgent()) {
		if id.account == a2aAccountSys {
			// $SYS holds no application inbox grants; its user is a
			// human with the system account's own privileges.
			continue
		}
		if id.narrowing != "" {
			// A narrowed principal's inbox grant is derived at mint
			// time from the claim the API server attested, not listed
			// here — and the callout refuses the whole map if it IS
			// listed, which would take every other principal down with
			// it. So the property this test exists for is asserted
			// where the derivation happens, against a real server:
			// a2a/authcallout, TestASessionReachesEverythingItsOwnWorkNeeds.
			// What is checkable here is the half that keeps the map
			// servable at all.
			if len(id.publish) > 0 || len(id.subscribe) > 0 {
				t.Errorf("%s narrows on %q but carries grants (%d publish, %d subscribe); the callout refuses a map like this outright",
					id.user, id.narrowing, len(id.publish), len(id.subscribe))
			}
			continue
		}
		want := "_INBOX." + id.user + ".>"
		if !slices.Contains(id.subscribe, want) {
			t.Errorf("%s: subscribe list lacks %q; every reply it waits on would time out", id.user, want)
		}
		if !slices.Contains(id.publish, want) {
			t.Errorf("%s: publish list lacks %q; it could not answer its own requests", id.user, want)
		}
	}
}

// No principal may hold another's inbox prefix. Without this the whole
// connect-time property leaks through the reply path: an agent that can
// subscribe to another's inbox reads what that principal's subject grants
// withheld.
func TestNoPrincipalHoldsAnotherPrincipalsInbox(t *testing.T) {
	ids := a2aIdentities(identityTestAgent())
	for _, id := range ids {
		for _, other := range ids {
			if other.user == id.user {
				continue
			}
			foreign := "_INBOX." + other.user + ".>"
			if slices.Contains(id.subscribe, foreign) {
				t.Errorf("%s may subscribe to %s's inbox (%q)", id.user, other.user, foreign)
			}
		}
	}
}

func TestPrincipalsAreDistinct(t *testing.T) {
	seenUser := map[string]bool{}
	seenSA := map[string]bool{}
	for _, id := range a2aIdentities(identityTestAgent()) {
		if seenUser[id.user] {
			t.Errorf("two principals share the NATS user %q", id.user)
		}
		seenUser[id.user] = true

		if id.auth != a2aAuthCallout {
			continue
		}
		// Two ServiceAccounts mapping to one user would re-create the
		// shared credential the callout exists to end, and the callout
		// refuses such a map outright — better to fail here than to
		// render a map the callout will reject at startup.
		if seenSA[id.serviceAccount] {
			t.Errorf("two principals share the ServiceAccount %q", id.serviceAccount)
		}
		seenSA[id.serviceAccount] = true
	}
}

// Each auth mode owes different fields, and a principal carrying the wrong set
// renders into the wrong place: a callout principal with no ServiceAccount
// cannot be resolved, and a static one with no creds key renders a password of
// "" — a user anyone can log in as, which is W6 finding #9 in a new costume.
func TestEachPrincipalCarriesWhatItsAuthModeNeeds(t *testing.T) {
	for _, id := range a2aIdentities(identityTestAgent()) {
		switch id.auth {
		case a2aAuthCallout:
			if id.serviceAccount == "" {
				t.Errorf("%s authenticates by callout but names no ServiceAccount", id.user)
			}
			if !strings.HasPrefix(id.serviceAccount, "system:serviceaccount:") {
				t.Errorf("%s: ServiceAccount %q is not in the form TokenReview reports", id.user, id.serviceAccount)
			}
			if id.credsKey != "" {
				t.Errorf("%s authenticates by callout but also carries the creds key %q; it must have no shared secret at all", id.user, id.credsKey)
			}
		case a2aAuthStatic:
			if id.credsKey == "" {
				t.Errorf("%s authenticates statically but names no creds key; it would render an empty password", id.user)
			}
			if id.serviceAccount != "" {
				t.Errorf("%s authenticates statically but names a ServiceAccount", id.user)
			}
		}
		if id.account == "" {
			t.Errorf("%s names no account", id.user)
		}
	}
}

// Every static principal must be exempted from the callout, and the exemption
// list is built from exactly this set. A static user missing from auth_users is
// refused at connect by a callout that has never heard of it.
func TestStaticAndCalloutPrincipalsPartitionTheSet(t *testing.T) {
	agent := identityTestAgent()
	all := a2aIdentities(agent)
	static := staticIdentities(agent)
	callout := calloutIdentities(agent)

	if len(static)+len(callout) != len(all) {
		t.Fatalf("static (%d) + callout (%d) != all (%d); a principal is in neither render or both",
			len(static), len(callout), len(all))
	}
	for _, id := range static {
		if id.auth != a2aAuthStatic {
			t.Errorf("%s is in the static set with auth mode %v", id.user, id.auth)
		}
	}
	for _, id := range callout {
		if id.auth != a2aAuthCallout {
			t.Errorf("%s is in the callout set with auth mode %v", id.user, id.auth)
		}
	}
}

// The residue, asserted so it cannot grow quietly. Each of these has a reason
// recorded at its definition, and the reasons are not the same kind of thing:
// web can never present a ServiceAccount token because a browser has none;
// bridge has a ServiceAccount and still cannot use it, because it is a sidecar
// in the agent pod and a token names a pod rather than a container — a callout
// entry would hand it the agent container's grants as well, which is the union
// A5 broke up; sys is a human; gateway could move today but its client program lands
// separately from this render, so moving the identity first would refuse it at
// connect on every install; seed is applied rather than rendered, so dropping
// its user would break an object already running on installs today; and eval
// is the bench harness outside the cluster, over a port-forward, with no
// ServiceAccount here to present, and rendered only when an operator opts in.
// A name beyond these means someone added a principal without asking whether
// it could have an identity.
func TestTheStaticResidueIsExactlyTheOnesWithReasons(t *testing.T) {
	statics := func() []string {
		var got []string
		for _, id := range staticIdentities(identityTestAgent()) {
			got = append(got, id.user)
		}
		return got
	}
	t.Setenv(a2aEvalPrincipalEnvVar, "")
	want := []string{"gateway", a2aBridgeUser, "seed", "web", "sys"}
	if got := statics(); !slices.Equal(got, want) {
		t.Errorf("static principals = %v, want %v.\nA new static principal needs a recorded reason it cannot present a ServiceAccount token, and a card that closes it if it can.", got, want)
	}
	t.Setenv(a2aEvalPrincipalEnvVar, "true")
	want = []string{"gateway", a2aBridgeUser, "seed", "web", "eval", "sys"}
	if got := statics(); !slices.Equal(got, want) {
		t.Errorf("static principals with the eval flag = %v, want %v", got, want)
	}
}

// The eval principal is opt-in. Its static grants reach every platform task,
// the gateway's included (a holder of eval-password can cancel or steer one it
// did not start), so an install that runs no eval must carry neither the user
// nor the key. Asserted at every layer the flag governs: the identity list,
// the creds keys the Secret is repaired to, the rendered nats.conf user block
// and the auth_users exemption; and against the near-miss, which must fall on
// the side of no credential.
func TestTheEvalPrincipalRendersOnlyUnderItsFlag(t *testing.T) {
	agent := identityTestAgent()
	render := func() (users []string, keys []string, conf string) {
		t.Helper()
		for _, id := range a2aIdentities(agent) {
			users = append(users, id.user)
		}
		conf = string(buildA2ANATSConfigSecret(agent, a2aTestCreds(), a2aTestCalloutKeys(t)).Data["nats.conf"])
		return users, a2aCredsKeys(), conf
	}
	authUsers := func(conf string) string {
		t.Helper()
		i := strings.Index(conf, "auth_users:")
		if i < 0 {
			t.Fatal("rendered nats.conf has no auth_users line")
		}
		return conf[i : i+strings.Index(conf[i:], "\n")]
	}

	for _, off := range []string{"", "TRUE", "1", "yes"} {
		t.Setenv(a2aEvalPrincipalEnvVar, off)
		users, keys, conf := render()
		if slices.Contains(users, "eval") {
			t.Errorf("%s=%q renders the eval identity; the principal is opt-in", a2aEvalPrincipalEnvVar, off)
		}
		if slices.Contains(keys, a2aEvalPasswordKey) {
			t.Errorf("%s=%q mints %s into the creds Secret", a2aEvalPrincipalEnvVar, off, a2aEvalPasswordKey)
		}
		if strings.Contains(conf, "user: eval") {
			t.Errorf("%s=%q renders a `user: eval` block into nats.conf", a2aEvalPrincipalEnvVar, off)
		}
		if strings.Contains(authUsers(conf), "eval") {
			t.Errorf("%s=%q lists eval in auth_users with no user block behind it", a2aEvalPrincipalEnvVar, off)
		}
	}

	t.Setenv(a2aEvalPrincipalEnvVar, "true")
	users, keys, conf := render()
	if !slices.Contains(users, "eval") {
		t.Error("the flag set renders no eval identity")
	}
	if !slices.Contains(keys, a2aEvalPasswordKey) {
		t.Errorf("the flag set mints no %s key", a2aEvalPasswordKey)
	}
	if !strings.Contains(conf, "user: eval") {
		t.Error("the flag set renders no `user: eval` block")
	}
	if !strings.Contains(authUsers(conf), "eval") {
		t.Error("the flag set renders the eval user but leaves it out of auth_users, so the callout would refuse it at connect")
	}
}

// The other half of the static-residue check, and the one that catches the
// opposite mistake: a principal declared on the callout that no workload can
// present a token for.
//
// An entry in the identity map is a grant on a ServiceAccount, live from the
// moment the map is served. If nothing renders an a2a-bus token for that
// account, the grant is not documentation of a future client — it is a standing
// authorization waiting for one, in the file that is supposed to record who
// actually authenticates. This file had exactly that before A5: an `agent`
// principal keyed on the platform agent's ServiceAccount whose only bus client
// was the Hermes bridge sidecar, authenticating as static `worker` — so the
// entry authorized nobody and was withdrawn. It is back, and the answer is real
// now: buildPodTemplateSpec mounts a2aBusTokenVolumeSource into the
// platform-agent container, and the `a2a` CLI reads it (a2a/cmd/a2a/main.go,
// connect).
//
// So the set is pinned by name rather than by shape. Adding a principal here
// means saying, at review, which rendered workload presents its token.
//
// Two of the three answer inside this module: a2aBusTokenVolumeSource and
// a2aBusTokenVolumeMount have two callers each -- buildPodTemplateSpec for
// `agent` and buildA2AProvisionJob for `provision` -- so a renderer that
// stopped projecting either token would fail a render assertion here.
//
// `session` is the one that does not. The workload that mounts its token is
// the session pod, and the gateway's spawner builds that pod
// (a2a/gateway/spawn.go), not the operator, so no assertion in this package can
// reach it. What holds it is tests/conformance's C1, which reads the spawner
// and this package's RBAC together for exactly that reason. If the spawner ever
// stops projecting the token, this test keeps passing and C1 is what fails.
func TestEveryCalloutPrincipalHasAClientThatCanPresentAToken(t *testing.T) {
	var got []string
	for _, id := range a2aIdentities(identityTestAgent()) {
		if id.auth == a2aAuthCallout {
			got = append(got, id.user)
		}
	}
	want := []string{"provision", "session", a2aAgentBusUser}
	if !slices.Equal(got, want) {
		t.Errorf("callout principals = %v, want %v.\nA new callout principal needs a rendered workload that mounts an a2a-bus token for its ServiceAccount (a2aBusTokenVolumeSource / a2aBusTokenVolumeMount). Without one the entry authorizes nobody and misreports who authenticates.", got, want)
	}
}

// a2aTestCalloutKeys generates a real keypair set for render tests. Real rather
// than a fixture string: the server validates both key types and refuses to
// start on either being wrong, so a test rendering a placeholder would assert
// against a config the server would reject.
func a2aTestCalloutKeys(t *testing.T) *a2aCalloutKeys {
	t.Helper()
	keys, _, err := generateA2ACalloutKeys()
	if err != nil {
		t.Fatalf("generateA2ACalloutKeys: %v", err)
	}
	return keys
}

// TestOnlyTheseIdentitiesHoldTheBareJetStreamAPIGrant pins the sentence in
// renderA2ANATSConf's doc comment that names them.
//
// That sentence used to read "$JS.API.> on every app user is playground
// posture". It was a summary, nothing checked it, and it was wrong in both
// directions by the time anyone read it again: web has carried the enumerated
// per-stream subjects since before the callout existed, so "every" was never
// true, and provision moved to the callout, so the set changed underneath it.
// A comment that names a set is a comment that needs a test naming the same
// set, which is what this is. Seed came off the list the same way, in
// gke-labs#1306: its $JS.API grant is now a2aSeedJetStreamGrants(), CREATE and
// INFO on the streams provisioning names. Worker came off it in #1393, the same
// way, and A5 then split that list in two: a2aBridgeJetStreamGrants() on TASKS
// and the runtime-state bucket, a2aAgentJetStreamGrants() on the two topic
// streams and nothing else. Gateway is the last one, and it is the one that cannot narrow on
// this branch's terms -- it has no client presenting a token yet.
//
// Failing here means one of two things and they want opposite responses. An
// identity dropping off the list is the callout doing its job — narrow the
// grant, then narrow this list and the comment with it. An identity appearing
// on it is a new bare $JS.API.>, which is a principal that can create, delete
// or purge any stream and any consumer on the account, including another
// principal's. That is not a list to grow without an argument in the identity's
// own comment for why it cannot be enumerated instead.
func TestOnlyTheseIdentitiesHoldTheBareJetStreamAPIGrant(t *testing.T) {
	const bare = "$JS.API.>"
	expected := []string{"gateway"}

	var got []string
	for _, id := range a2aIdentities(identityTestAgent()) {
		if slices.Contains(id.publish, bare) || slices.Contains(id.subscribe, bare) {
			got = append(got, id.user)
		}
	}
	slices.Sort(got)

	if !slices.Equal(got, expected) {
		t.Errorf("the set of identities holding a bare %s is %v, and renderA2ANATSConf's doc "+
			"comment says %v; correct whichever is wrong, and read this test's own comment "+
			"first because the two directions want opposite fixes", bare, got, expected)
	}
}

// The narrowed principal, pinned as data. Three things have to agree for a
// session to connect at all — the ServiceAccount the spawner names, the
// ServiceAccount the map is keyed on, and the narrowing the callout switches on
// — and they are rendered from three different places.
func TestTheSessionPrincipalIsNarrowedAndOtherwiseEmpty(t *testing.T) {
	agent := identityTestAgent()
	var session *a2aIdentity
	for _, id := range a2aIdentities(agent) {
		if id.user == "session" {
			cp := id
			session = &cp
		}
	}
	if session == nil {
		t.Fatal("no session principal is rendered; spawned pods would have no identity to present")
	}
	if session.auth != a2aAuthCallout {
		t.Error("the session principal does not authenticate by callout; a static session is the shared credential again")
	}
	if session.credsKey != "" {
		t.Errorf("the session principal carries creds key %q; no session may have a shared secret", session.credsKey)
	}
	if session.narrowing != a2aNarrowingPod {
		t.Errorf("session narrowing = %q, want %q", session.narrowing, a2aNarrowingPod)
	}
	if len(session.publish) != 0 || len(session.subscribe) != 0 {
		t.Errorf("the session principal carries grants: %v / %v", session.publish, session.subscribe)
	}
	// Keyed on the ServiceAccount the spawner will actually name.
	want := a2aServiceAccountName(agent.Namespace, a2aSessionServiceAccountName(agent))
	if session.serviceAccount != want {
		t.Errorf("session serviceAccount = %q, want %q", session.serviceAccount, want)
	}
}

// No session pod may be handed a shared static password again. This is the
// regression that gke-labs#1270 is about, asserted at the render.
//
// Written against any credsKey rather than against the one name it used to be:
// A5 retired `worker-password`, and a test that named it would have gone
// vacuous at that rename while still reporting a pass.
func TestNoSessionPrincipalSharesAStaticCredential(t *testing.T) {
	for _, id := range a2aIdentities(identityTestAgent()) {
		if id.user != "session" {
			continue
		}
		if id.credsKey != "" || id.auth != a2aAuthCallout {
			t.Fatalf("the session principal reads static credential %q (auth %v); it must be callout-issued", id.credsKey, id.auth)
		}
	}
}

// The task plane's writer sets, as the render grants them. Subject-derived
// identity is decision-grade on a subject exactly where the principals whose
// grants reach it are the ones the subject names, so this is asserted over
// every rendered principal's publish list with the same matcher the server
// applies, not over the one entry that changed.
//
//   - `…supervisor` has one writer, the gateway, which is the supervisor for
//     the chat sessions it spawns. The dispatcher's janitor inherits the token
//     at stage 3 and will appear here when it does.
//   - The gateway holds no publish on `…events`. The executor's subject has one
//     writer class, and a supervisor terminal there is exactly what a hostile
//     executor would forge.
//   - The gateway reads both, because its relay folds the pair.
//
// What is NOT asserted here, and why: that `…events` has no writer beyond the
// executor. That is a statement about the whole rendered principal set rather
// than about the supervisor split, and it is asserted in tests/conformance,
// where it was a known violation until this change retired `worker`. The
// bridge that inherits the static half publishes `…events` for its own
// addressee only, so it does not reach another addressee's.
func TestTheSupervisorSubjectHasExactlyOneWriterAndItIsNotAnEventsWriter(t *testing.T) {
	const (
		supervisorProbe = "a2a.tasks.chat-otter-1a2b.task-0001.supervisor"
		eventsProbe     = "a2a.tasks.chat-otter-1a2b.task-0001.events"
	)
	var supervisorWriters []string
	for _, id := range a2aIdentities(identityTestAgent()) {
		for _, grant := range id.publish {
			if subjectMatches(grant, supervisorProbe) {
				supervisorWriters = append(supervisorWriters, id.user)
				break
			}
		}
		if id.user != "gateway" {
			continue
		}
		for _, grant := range id.publish {
			if subjectMatches(grant, eventsProbe) {
				t.Errorf("the gateway's publish grant %q reaches an executor's events subject; the supervisor split moved its terminal off that subject", grant)
			}
		}
		for _, probe := range []string{supervisorProbe, eventsProbe} {
			if !slices.ContainsFunc(id.subscribe, func(grant string) bool { return subjectMatches(grant, probe) }) {
				t.Errorf("the gateway cannot subscribe to %s; its relay folds both task-event subjects", probe)
			}
		}
	}
	if !slices.Equal(supervisorWriters, []string{"gateway"}) {
		t.Errorf("principals whose publish grants reach %s: %v, want exactly [gateway]", supervisorProbe, supervisorWriters)
	}
}

// The `…events` writer-class check ships advisory and must be tightenable
// without a new gateway image, one retention window after an install takes
// the supervisor split. That makes the rendered env var the mechanism, so it
// is rendered explicitly at its default and honours the controller override.
func TestTheEventsWriterCheckIsTightenedByConfigNotByCode(t *testing.T) {
	agent := a2aTestAgent()
	strictEnv := func() *corev1.EnvVar {
		t.Helper()
		dep := buildA2AGatewayDeployment(agent)
		for i := range dep.Spec.Template.Spec.Containers[0].Env {
			if e := &dep.Spec.Template.Spec.Containers[0].Env[i]; e.Name == "A2A_STRICT_EVENTS_WRITER" {
				return e
			}
		}
		return nil
	}
	e := strictEnv()
	if e == nil {
		t.Fatal("the gateway Deployment does not render A2A_STRICT_EVENTS_WRITER; the advisory check has no flip")
	}
	if e.Value != "false" {
		t.Errorf("A2A_STRICT_EVENTS_WRITER defaults to %q, want \"false\" - a strict default refuses pre-split supervisor terminals", e.Value)
	}
	t.Setenv("A2A_STRICT_EVENTS_WRITER", "true")
	if got := strictEnv().Value; got != "true" {
		t.Errorf("the controller override did not reach the gateway: %q", got)
	}
	// Anything that is not exactly "true" relaxes rather than tightens.
	t.Setenv("A2A_STRICT_EVENTS_WRITER", "TRUE")
	if got := strictEnv().Value; got != "false" {
		t.Errorf("a near-miss value tightened the check: %q", got)
	}
}

// The credential A5 retired, refused by name at the render.
//
// Three routes could bring it back and two of them would be quiet. A
// `worker-password` key in a2aCredsKeys puts the password back in the Secret
// with nothing reading it; an identity named `worker` puts the user back in
// nats.conf or in the map; and either one alone becomes a working shared
// credential the moment the other appears. So all three are checked, and
// against the strings rather than against a deleted symbol — a deleted symbol
// is exactly what a re-add restores.
func TestTheWorkerCredentialIsGone(t *testing.T) {
	agent := identityTestAgent()
	for _, id := range a2aIdentities(agent) {
		if id.user == "worker" {
			t.Errorf("the `worker` principal is back in the identity list; A5 split it into %q and %q", a2aAgentBusUser, a2aBridgeUser)
		}
		if id.credsKey == "worker-password" {
			t.Errorf("%s reads worker-password; that key is retired", id.user)
		}
	}
	if slices.Contains(a2aCredsKeys(), "worker-password") {
		t.Error("worker-password is back in a2aCredsKeys; the operator would mint a password nothing authenticates with")
	}

	conf := string(buildA2ANATSConfigSecret(agent, a2aTestCreds(), a2aTestCalloutKeys(t)).Data["nats.conf"])
	if strings.Contains(conf, "user: worker") {
		t.Error("the rendered nats.conf still declares a `worker` user")
	}
	if strings.Contains(renderA2AAuthUsers(agent), "worker") {
		t.Error("`worker` is still in the auth_users exemption; the callout would be bypassed for a name with no user block")
	}
}

// Two identities on one ServiceAccount is one identity, and the map decides
// which -- the callout indexes its entries by ServiceAccount, so the loser is
// silently unreachable and the winner's grants are what both workloads get.
// The collision is not even symmetric: the session entry is narrowed and the
// agent entry is not, so whichever wins, one workload runs on grants derived
// for the other.
//
// The default agent's accounts are distinct by construction, and a future
// identity copied in with someone else's serviceAccount needs no test of its
// own here: validateA2AAuthMapIdentities refuses a duplicate at render, and
// TestRenderedAuthMapCarriesEveryCalloutPrincipalAndNoStaticOne renders the
// default agent and fails on the error. This test is for the case neither of
// those reaches from a default CR -- the one a user can cause today.
//
// spec.security.serviceAccountName overrides the agent pod's account, and the
// webhook only checks it against restrictedServiceAccounts, so pointing it at
// the session pod's account is admitted. What refuses it is validateA2AAuthMap,
// one layer in, which is fail-closed but diagnosed in a controller log rather
// than at the apply. A5 is what makes this reachable: `agent` is the first
// callout identity keyed on a user-settable field.
func TestAnOverriddenServiceAccountThatCollidesIsRefused(t *testing.T) {
	agent := identityTestAgent()
	collide := a2aSessionServiceAccountName(agent)
	agent.Spec.Security = &agentv1alpha1.SecuritySpec{ServiceAccountName: collide}

	// Precondition: the override actually landed on the agent identity, so a
	// refusal below is the collision and not some unrelated validation.
	var agentSA string
	for _, id := range a2aIdentities(agent) {
		if id.user == a2aAgentBusUser {
			agentSA = id.serviceAccount
		}
	}
	if want := a2aServiceAccountName(agent.Namespace, collide); agentSA != want {
		t.Fatalf("the `agent` identity is keyed on %q, want %q; spec.security.serviceAccountName no longer "+
			"reaches the map and this test measures nothing", agentSA, want)
	}

	if _, _, err := buildA2AAuthMapConfigMap(agent); err == nil {
		t.Error("an override colliding with the session pod's ServiceAccount rendered a map: the callout " +
			"serves one entry per account, so one workload would run on the other's grants")
	} else if !strings.Contains(err.Error(), "duplicate serviceAccount") {
		t.Errorf("the render failed for some other reason: %v", err)
	} else {
		t.Logf("refused: %v", err)
	}
}
