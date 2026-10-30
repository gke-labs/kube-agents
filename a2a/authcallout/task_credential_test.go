package authcallout

import (
	"context"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/jwt/v2"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/nats-io/nkeys"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/taskcred"
)

// Per-task credentials (#3889): a profile worker's task connection presents
// its ServiceAccount token as the connection token and a task credential the
// dispatcher signed as the password, and gets that one task's subjects. The
// unit tests read the refusal reasons, which the client never sees; the
// integration tests prove the scope against a real server.

const (
	taskOne = "task-1"
	taskTwo = "task-2"

	// auditorPod1UID is profileTokens' attested UID for auditorPod1.
	auditorPod1UID = "uid-p1"
)

// stemOne and stemTwo are auditorPod1's task connections' consumer stems,
// which also name their inboxes.
var (
	stemOne = taskcred.ConsumerStem(auditorPod1, taskOne)
	stemTwo = taskcred.ConsumerStem(auditorPod1, taskTwo)
)

func taskSigningKey(t *testing.T) (nkeys.KeyPair, string) {
	t.Helper()
	kp, err := taskcred.NewSigningKey()
	if err != nil {
		t.Fatalf("NewSigningKey: %v", err)
	}
	pub, _ := kp.PublicKey()
	return kp, pub
}

// taskToken mints a nats credential for auditorPod1's task-1, as the
// dispatcher would, with edit applied first.
func taskToken(t *testing.T, kp nkeys.KeyPair, edit func(*taskcred.Claims)) string {
	t.Helper()
	c := taskcred.Claims{
		Task:     taskOne,
		Profile:  auditor,
		Tier:     "read",
		Pod:      auditorPod1,
		PodUID:   auditorPod1UID,
		Audience: taskcred.AudienceNATS,
		Expires:  time.Now().Add(10 * time.Minute),
	}
	if edit != nil {
		edit(&c)
	}
	tok, err := taskcred.Mint(kp, c)
	if err != nil {
		t.Fatalf("Mint: %v", err)
	}
	return tok
}

func newTaskTestService(t *testing.T, keys []string, grantTTL time.Duration) *Service {
	t.Helper()
	issuerKP, err := nkeys.CreateAccount()
	if err != nil {
		t.Fatalf("CreateAccount: %v", err)
	}
	issuerSeed, _ := issuerKP.Seed()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	store := NewStore(log)
	if err := store.Update([]byte(profileMap)); err != nil {
		t.Fatalf("loading the identity map: %v", err)
	}
	validator, err := NewTokenValidator(tokenReviewer(profileTokens(), nil), testAudience)
	if err != nil {
		t.Fatalf("NewTokenValidator: %v", err)
	}
	svc, err := NewService(store, validator, Config{
		IssuerSeed:         string(issuerSeed),
		ReservedPrincipals: renderedFixtureAuthUsers(t),
		ReservedAddressees: renderedFixtureAddressees(t),
		TaskSigningKeys:    keys,
		GrantTTL:           grantTTL,
	}, log)
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func authorizeWithTask(svc *Service, saToken, task string) (decision, error) {
	req := &jwt.AuthorizationRequestClaims{}
	req.ConnectOptions.Token = saToken
	req.ConnectOptions.Password = task
	return svc.authorize(context.Background(), req)
}

// Each mismatch between the credential and the connection presenting it is a
// refusal, and the reason names the claim.
func TestATaskCredentialMustBeThisConnections(t *testing.T) {
	kp, pub := taskSigningKey(t)
	other, _ := taskSigningKey(t)
	svc := newTaskTestService(t, []string{pub}, 0)

	cases := []struct {
		name  string
		token string
		want  string
	}{
		{"another pod", taskToken(t, kp, func(c *taskcred.Claims) { c.Pod = auditorPod2 }), "pod"},
		{"another pod UID", taskToken(t, kp, func(c *taskcred.Claims) { c.PodUID = "uid-p2" }), "pod UID"},
		{"another profile", taskToken(t, kp, func(c *taskcred.Claims) { c.Profile = cluster }), "profile"},
		{"another tier", taskToken(t, kp, func(c *taskcred.Claims) { c.Tier = "unattended" }), "tier"},
		{"past the max lifetime", taskToken(t, kp, func(c *taskcred.Claims) { c.Expires = time.Now().Add(defaultTaskCredentialMaxLifetime + time.Hour) }), "ceiling"},
		{"an untrusted key", taskToken(t, other, nil), "not a trusted task-signing key"},
		{"the proxy audience", taskToken(t, kp, func(c *taskcred.Claims) { c.Audience = taskcred.AudienceCredentialProxy }), "credential-proxy"},
		{"not a credential", "not-a-jwt", "not a JWT"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d, err := authorizeWithTask(svc, tokenAuditorPod1, tc.token)
			if err == nil {
				t.Fatalf("authorized with grants %+v", d.perms)
			}
			if !strings.Contains(err.Error(), tc.want) || !strings.Contains(err.Error(), "task credential") {
				t.Errorf("refusal %q does not name %q", err, tc.want)
			}
		})
	}
}

// Expiry is read against the callout's clock, moved forward here because Mint
// stamps iat with the wall clock and a credential issued after its own expiry
// is refused for that instead. Past the skew Verify refuses it; inside the
// skew but past now the callout does, since the JWT it minted would already
// be expired.
func TestAnExpiredTaskCredentialIsRefused(t *testing.T) {
	kp, pub := taskSigningKey(t)
	for name, tc := range map[string]struct{ clock, exp time.Duration }{
		"past the skew":                {5 * time.Minute, time.Minute},
		"inside the skew but past now": {70 * time.Second, time.Minute},
	} {
		t.Run(name, func(t *testing.T) {
			svc := newTaskTestService(t, []string{pub}, 0)
			now := time.Now()
			svc.now = func() time.Time { return now.Add(tc.clock) }
			tok := taskToken(t, kp, func(c *taskcred.Claims) { c.Expires = now.Add(tc.exp) })
			if _, err := authorizeWithTask(svc, tokenAuditorPod1, tok); err == nil || !strings.Contains(err.Error(), "expired") {
				t.Errorf("refusal %v does not name the expiry", err)
			}
			// The same credential a minute earlier on the callout's clock
			// is honoured, so the refusal above is the expiry's.
			svc.now = func() time.Time { return now }
			if _, err := authorizeWithTask(svc, tokenAuditorPod1, tok); err != nil {
				t.Errorf("the credential before its expiry was refused: %v", err)
			}
		})
	}
}

// A task credential on an entry that isn't a profile worker's is refused, not
// ignored; so is one at a callout that trusts no key, and one for an entry
// with no tier.
func TestATaskCredentialIsRefusedWhereItCannotBeHonoured(t *testing.T) {
	kp, pub := taskSigningKey(t)
	svc := newTaskTestService(t, []string{pub}, 0)
	if _, err := authorizeWithTask(svc, tokenPodA, taskToken(t, kp, nil)); err == nil || !strings.Contains(err.Error(), "not a profile worker") {
		t.Errorf("a session pod's task credential: %v", err)
	}
	if _, err := authorizeWithTask(svc, gatewayToken, taskToken(t, kp, nil)); err == nil {
		t.Error("a static entry's task credential was accepted")
	}
	// cluster-a's entry names no tier.
	tok := taskToken(t, kp, func(c *taskcred.Claims) { c.Profile, c.Pod, c.PodUID = cluster, clusterPod, "uid-c" })
	if _, err := authorizeWithTask(svc, tokenClusterPod, tok); err == nil || !strings.Contains(err.Error(), "tier") {
		t.Errorf("a credential for an entry with no tier: %v", err)
	}

	keyless := newTaskTestService(t, nil, 0)
	if _, err := authorizeWithTask(keyless, tokenAuditorPod1, taskToken(t, kp, nil)); err == nil || !strings.Contains(err.Error(), "trusts no task-signing key") {
		t.Errorf("a callout with no task-signing key: %v", err)
	}
}

func TestNewServiceRefusesTaskSigningKeysItCannotUse(t *testing.T) {
	_, a := taskSigningKey(t)
	_, b := taskSigningKey(t)
	_, c := taskSigningKey(t)
	acct, _ := nkeys.CreateAccount()
	acctPub, _ := acct.PublicKey()
	issuerSeed, _ := acct.Seed()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	validator, err := NewTokenValidator(tokenReviewer(nil, nil), testAudience)
	if err != nil {
		t.Fatal(err)
	}
	for name, keys := range map[string][]string{
		"three keys":     {a, b, c},
		"an account key": {acctPub},
		"garbage":        {a, "C-not-a-key"},
	} {
		if _, err := NewService(NewStore(log), validator, Config{
			IssuerSeed:         string(issuerSeed),
			ReservedPrincipals: renderedFixtureAuthUsers(t),
			ReservedAddressees: renderedFixtureAddressees(t),
			TaskSigningKeys:    keys,
		}, log); err == nil {
			t.Errorf("%s: NewService accepted it", name)
		}
	}
}

// A valid credential scopes the grant to its task: the events publish and all
// three consumer filters are exact on it, and nothing of the pod connection's
// (hand-off, lease, topics) comes with it.
func TestAValidTaskCredentialGrantsThatTaskOnly(t *testing.T) {
	kp, pub := taskSigningKey(t)
	_, oldPub := taskSigningKey(t)
	svc := newTaskTestService(t, []string{oldPub, pub}, 0)
	exp := time.Now().Add(10 * time.Minute)
	d, err := authorizeWithTask(svc, tokenAuditorPod1, taskToken(t, kp, func(c *taskcred.Claims) { c.Expires = exp }))
	if err != nil {
		t.Fatalf("refused: %v", err)
	}
	if d.task != taskOne || d.notAfter.Unix() != exp.Unix() || d.user != auditorPod1 {
		t.Errorf("decision task/notAfter/user = %q/%v/%q", d.task, d.notAfter, d.user)
	}
	pubs, subs := []string(d.perms.Pub.Allow), []string(d.perms.Sub.Allow)

	for _, want := range []string{
		lib.TaskEventsSubject(auditor, taskOne),
		consumerSubject("CREATE", lib.SessionConsumerName(stemOne, lib.SessionConsumerOrigin)) + "." + lib.TaskInSubject(auditor, taskOne),
		consumerSubject("CREATE", lib.SessionConsumerName(stemOne, lib.SessionConsumerIn)) + "." + lib.TaskInSubject(auditor, taskOne),
		consumerSubject("CREATE", lib.SessionConsumerName(stemOne, lib.SessionConsumerEvents)) + "." + lib.TaskEventsSubject(auditor, taskOne),
		consumerSubject("MSG.NEXT", lib.SessionConsumerName(stemOne, lib.SessionConsumerIn)),
		"_INBOX." + stemOne + ".reply",
	} {
		if !grantsCover(pubs, want) {
			t.Errorf("publish %s is not granted; the task connection needs it", want)
		}
	}
	for _, refused := range []string{
		lib.TaskEventsSubject(auditor, taskTwo),
		lib.TaskInSubject(auditor, taskOne),
		consumerSubject("CREATE", lib.SessionConsumerName(stemOne, lib.SessionConsumerIn)) + "." + lib.TaskInSubject(auditor, taskTwo),
		lib.DispatchAcceptSubject(auditor, "read", auditorPod1),
		lib.DispatchLeaseSubject(auditor, "read", auditorPod1),
		"a2a.topics." + auditorPublishTopic,
		"$JS.API.STREAM.INFO." + lib.StreamTopicsState,
	} {
		if grantsCover(pubs, refused) {
			t.Errorf("publish %s is granted on a task-1 connection", refused)
		}
	}
	for _, p := range append(append([]string{}, pubs...), subs...) {
		if strings.Contains(p, ".*.") || strings.HasSuffix(p, ".*") || (strings.Contains(p, ">") && !strings.HasPrefix(p, "_INBOX."+stemOne+".")) {
			t.Errorf("a wildcard grant on a task connection: %s", p)
		}
	}
	if grantsCover(subs, lib.DispatchSubject(auditor, "read", auditorPod1)) || grantsCover(subs, "a2a.topics."+auditorSubscribeTopic) {
		t.Errorf("a task connection subscribes beyond its inbox: %v", subs)
	}
}

// TRANSITIONAL, flip with #3889's worker PR. Today the pod connection, with
// no credential, keeps the profile-wide task reach it had before #3889:
// .events on every task of its profile and its consumers filtered on <P>.*.
// When the worker moves onto its task connection, that reach goes (the TODO on
// profileWorkerGrants), and this test becomes "the pod connection holds no
// a2a.tasks. grant and no consumer on TASKS", which it asserted before the
// removal was staged. It must flip before any tier stronger than Read goes
// live (#3659).
func TestATokenlessWorkerKeepsProfileWideTaskReachUntilTheWorkerPR(t *testing.T) {
	_, pub := taskSigningKey(t)
	svc := newTaskTestService(t, []string{pub}, 0)
	for name, tc := range map[string]struct{ token, profile, pod string }{
		"tiered":   {tokenAuditorPod1, auditor, auditorPod1},
		"tierless": {tokenClusterPod, cluster, clusterPod},
	} {
		d, err := svc.authorize(context.Background(), &jwt.AuthorizationRequestClaims{ConnectOptions: jwt.ConnectOptions{Token: tc.token}})
		if err != nil {
			t.Fatalf("%s: refused: %v", name, err)
		}
		if d.task != "" || !d.notAfter.IsZero() {
			t.Errorf("%s: a pod connection carries task %q, notAfter %v", name, d.task, d.notAfter)
		}
		pubs := []string(d.perms.Pub.Allow)
		for _, task := range []string{taskOne, taskTwo} {
			for _, want := range []string{
				lib.TaskEventsSubject(tc.profile, task),
				consumerSubject("CREATE", lib.SessionConsumerName(tc.pod, lib.SessionConsumerIn)) + "." + lib.TaskInSubject(tc.profile, "*"),
			} {
				if !grantsCover(pubs, want) {
					t.Errorf("%s: the pod connection lost %s before the worker PR moved it onto a task connection", name, want)
				}
			}
		}
	}
}

// TRANSITIONAL, flip with #3889's worker PR. Today the task connection is
// strictly narrower than the pod connection it sits beside: every task-plane
// grant it holds, the pod connection covers too, and the pod connection
// reaches a sibling task the task connection is refused. Both halves flip when
// the worker PR removes the pod connection's task reach: the pod connection
// then holds no task-plane grant at all, and neither connection reaches the
// sibling. Flip with TestATokenlessWorkerKeepsProfileWideTaskReachUntilTheWorkerPR.
func TestATaskConnectionIsNarrowerThanThePodConnection(t *testing.T) {
	kp, pub := taskSigningKey(t)
	svc := newTaskTestService(t, []string{pub}, 0)
	pod, err := svc.authorize(context.Background(), &jwt.AuthorizationRequestClaims{ConnectOptions: jwt.ConnectOptions{Token: tokenAuditorPod1}})
	if err != nil {
		t.Fatal(err)
	}
	task, err := authorizeWithTask(svc, tokenAuditorPod1, taskToken(t, kp, nil))
	if err != nil {
		t.Fatal(err)
	}
	// The task plane only: the consumer names and inbox are the task's own
	// (taskcred.ConsumerStem), so they differ from the pod's by design.
	for _, g := range task.perms.Pub.Allow {
		if strings.HasPrefix(g, "a2a.tasks.") && !grantsCover(pod.perms.Pub.Allow, g) {
			t.Errorf("the task connection holds %s, which the pod connection does not", g)
		}
	}
	sibling := lib.TaskEventsSubject(auditor, taskTwo)
	if !grantsCover(pod.perms.Pub.Allow, sibling) || grantsCover(task.perms.Pub.Allow, sibling) {
		t.Errorf("want %s on the pod connection (transitional) and not on the task-1 connection", sibling)
	}
}

// The minted JWT expires no later than the credential, and no later than
// GrantTTL.
func TestTheMintedJWTExpiresWithItsTaskCredential(t *testing.T) {
	kp, pub := taskSigningKey(t)
	userKP, _ := nkeys.CreateUser()
	userPub, _ := userKP.PublicKey()
	req := &jwt.AuthorizationRequestClaims{}
	req.UserNkey = userPub

	for name, tc := range map[string]struct {
		ttl time.Duration
		exp time.Duration
	}{
		"the credential is shorter": {time.Hour, 5 * time.Minute},
		"GrantTTL is shorter":       {10 * time.Minute, 10*time.Minute + 20*time.Second},
	} {
		t.Run(name, func(t *testing.T) {
			svc := newTaskTestService(t, []string{pub}, tc.ttl)
			exp := time.Now().Add(tc.exp)
			d, err := authorizeWithTask(svc, tokenAuditorPod1, taskToken(t, kp, func(c *taskcred.Claims) { c.Expires = exp }))
			if err != nil {
				t.Fatalf("refused: %v", err)
			}
			raw, err := svc.mint(req, d)
			if err != nil {
				t.Fatal(err)
			}
			uc, err := jwt.DecodeUserClaims(raw)
			if err != nil {
				t.Fatal(err)
			}
			if uc.Expires > exp.Unix() {
				t.Errorf("minted exp %d is after the credential's %d", uc.Expires, exp.Unix())
			}
			if ceiling := time.Now().Add(tc.ttl).Unix(); uc.Expires > ceiling {
				t.Errorf("minted exp %d is past GrantTTL (%d)", uc.Expires, ceiling)
			}
		})
	}
}

// --- against a real server ---

// connectForTask dials as a task connection: the ServiceAccount token as the
// connection token, the task credential as the password.
func (h *harness) connectForTask(t *testing.T, user, saToken, task string, extra ...nats.Option) (*nats.Conn, chan error, error) {
	t.Helper()
	violations := make(chan error, 16)
	nc, err := nats.Connect(h.url, append([]nats.Option{h.dial,
		nats.Token(saToken),
		nats.UserInfo("", task),
		nats.CustomInboxPrefix("_INBOX." + user),
		nats.Name(user + "-task"),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, e error) { violations <- e }),
	}, extra...)...)
	if err != nil {
		return nil, nil, err
	}
	t.Cleanup(nc.Close)
	return nc, violations, nil
}

func TestATaskConnectionReachesItsTaskAndNoOther(t *testing.T) {
	kp, pub := taskSigningKey(t)
	h := startHarness(t, profileMap, profileTokens(), withTaskSigningKeys(pub))
	nc, violations, err := h.connectForTask(t, stemOne, tokenAuditorPod1, taskToken(t, kp, nil))
	if err != nil {
		t.Fatalf("a valid task credential was refused: %v", err)
	}

	in := lib.SessionConsumerName(stemOne, lib.SessionConsumerIn)
	checkPublish(t, nc, violations, map[string]bool{
		lib.TaskEventsSubject(auditor, taskOne): false,
		consumerSubject("CREATE", lib.SessionConsumerName(stemOne, lib.SessionConsumerOrigin)) + "." + lib.TaskInSubject(auditor, taskOne):     false,
		consumerSubject("CREATE", in) + "." + lib.TaskInSubject(auditor, taskOne):                                                              false,
		consumerSubject("CREATE", lib.SessionConsumerName(stemOne, lib.SessionConsumerEvents)) + "." + lib.TaskEventsSubject(auditor, taskOne): false,
		consumerSubject("MSG.NEXT", in): false,
		"_INBOX." + stemOne + ".r":      false,

		// Another task of the same profile, by every route.
		lib.TaskEventsSubject(auditor, taskTwo):                                                                                                true,
		consumerSubject("CREATE", in) + "." + lib.TaskInSubject(auditor, taskTwo):                                                              true,
		consumerSubject("CREATE", in) + "." + lib.TaskInSubject(auditor, "*"):                                                                  true,
		consumerSubject("CREATE", lib.SessionConsumerName(stemOne, lib.SessionConsumerEvents)) + "." + lib.TaskEventsSubject(auditor, taskTwo): true,
		// Its own task's input, which the requester writes.
		lib.TaskInSubject(auditor, taskOne): true,
		// The pod connection's grants stay on the pod connection.
		lib.DispatchAcceptSubject(auditor, "read", auditorPod1): true,
		lib.DispatchLeaseSubject(auditor, "read", auditorPod1):  true,
		"a2a.topics." + auditorPublishTopic:                     true,
	})
	for _, subject := range []string{
		lib.TaskEventsSubject(auditor, taskTwo),
		lib.TaskEventsSubject(auditor, taskOne),
		lib.DispatchSubject(auditor, "read", auditorPod1),
		"a2a.topics." + auditorSubscribeTopic,
	} {
		if !subscribeRefused(t, nc, violations, subject) {
			t.Errorf("subscribe %s was allowed on a task connection", subject)
		}
	}
}

func TestABadTaskCredentialIsRefusedAtConnect(t *testing.T) {
	kp, pub := taskSigningKey(t)
	untrusted, _ := taskSigningKey(t)
	h := startHarness(t, profileMap, profileTokens(), withTaskSigningKeys(pub))
	for name, tok := range map[string]string{
		"another pod":     taskToken(t, kp, func(c *taskcred.Claims) { c.Pod = auditorPod2 }),
		"another pod UID": taskToken(t, kp, func(c *taskcred.Claims) { c.PodUID = "uid-p2" }),
		"another profile": taskToken(t, kp, func(c *taskcred.Claims) { c.Profile = cluster }),
		"another tier":    taskToken(t, kp, func(c *taskcred.Claims) { c.Tier = "unattended" }),
		"untrusted key":   taskToken(t, untrusted, nil),
	} {
		if _, _, err := h.connectForTask(t, auditorPod1, tokenAuditorPod1, tok); err == nil {
			t.Errorf("%s: connected", name)
		}
	}
	// The same pod's token with no credential still connects, as the pod
	// connection.
	h.connectAs(t, auditorPod1, tokenAuditorPod1)

	// Expired: the callout's clock five minutes on, a credential for one.
	now := time.Now()
	late := startHarness(t, profileMap, profileTokens(), withTaskSigningKeys(pub), withNow(func() time.Time { return now.Add(5 * time.Minute) }))
	if _, _, err := late.connectForTask(t, auditorPod1, tokenAuditorPod1, taskToken(t, kp, func(c *taskcred.Claims) { c.Expires = now.Add(time.Minute) })); err == nil {
		t.Error("expired: connected")
	}
}

// The server closes a task connection when its credential expires: the minted
// JWT carries the credential's expiry.
func TestATaskConnectionClosesWhenItsCredentialExpires(t *testing.T) {
	kp, pub := taskSigningKey(t)
	h := startHarness(t, profileMap, profileTokens(), withTaskSigningKeys(pub))
	closed := make(chan struct{})
	_, _, err := h.connectForTask(t, stemOne, tokenAuditorPod1,
		taskToken(t, kp, func(c *taskcred.Claims) { c.Expires = time.Now().Add(3 * time.Second) }),
		nats.NoReconnect(),
		nats.ClosedHandler(func(*nats.Conn) { close(closed) }))
	if err != nil {
		t.Fatalf("refused: %v", err)
	}
	select {
	case <-closed:
	case <-time.After(10 * time.Second):
		t.Fatal("the task connection was still open 10s after connecting, 7s past its 3s credential")
	}
}

// TRANSITIONAL, flip with #3889's worker PR: on a real server the pod
// connection still reaches its profile's task plane, every task of it, while
// the task connection beside it is refused the sibling task. After the worker
// PR the pod connection's rows below become refusals.
func TestOnARealServerThePodConnectionIsWiderThanTheTaskConnection(t *testing.T) {
	kp, pub := taskSigningKey(t)
	h := startHarness(t, profileMap, profileTokens(), withTaskSigningKeys(pub))
	pod, podViolations := h.connectAs(t, auditorPod1, tokenAuditorPod1)
	checkPublish(t, pod, podViolations, map[string]bool{
		lib.TaskEventsSubject(auditor, taskOne): false,
		lib.TaskEventsSubject(auditor, taskTwo): false,
		consumerSubject("CREATE", lib.SessionConsumerName(auditorPod1, lib.SessionConsumerIn)) + "." + lib.TaskInSubject(auditor, "*"): false,
		lib.DispatchAcceptSubject(auditor, "read", auditorPod1):                                                                        false,
	})
	task, taskViolations, err := h.connectForTask(t, stemOne, tokenAuditorPod1, taskToken(t, kp, nil))
	if err != nil {
		t.Fatal(err)
	}
	checkPublish(t, task, taskViolations, map[string]bool{
		lib.TaskEventsSubject(auditor, taskOne): false,
		lib.TaskEventsSubject(auditor, taskTwo): true,
		consumerSubject("CREATE", lib.SessionConsumerName(stemOne, lib.SessionConsumerIn)) + "." + lib.TaskInSubject(auditor, "*"): true,
	})
}

// A consumer's INFO, MSG.NEXT and DELETE carry its name and no filter, so the
// task connection's consumers are named for the task (taskcred.ConsumerStem).
// On one warm pod, task B's connection is refused every operation on the
// consumers task A's connection and the pod connection made.
func TestATaskConnectionCannotReachAnotherTasksConsumers(t *testing.T) {
	kp, pub := taskSigningKey(t)
	h := startHarness(t, profileMap, profileTokens(), withTaskSigningKeys(pub))
	provisionTasksStream(t, h)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	create := func(nc *nats.Conn, name, filter string) {
		t.Helper()
		js, err := jetstream.New(nc)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := js.CreateOrUpdateConsumer(ctx, lib.TasksStream, jetstream.ConsumerConfig{
			Name: name, FilterSubject: filter, AckPolicy: jetstream.AckNonePolicy, InactiveThreshold: time.Minute, MemoryStorage: true,
		}); err != nil {
			t.Fatalf("creating %s: %v", name, err)
		}
	}
	a, _, err := h.connectForTask(t, stemOne, tokenAuditorPod1, taskToken(t, kp, nil))
	if err != nil {
		t.Fatal(err)
	}
	aIn := lib.SessionConsumerName(stemOne, lib.SessionConsumerIn)
	create(a, aIn, lib.TaskInSubject(auditor, taskOne))
	pod, _ := h.connectAs(t, auditorPod1, tokenAuditorPod1)
	podIn := lib.SessionConsumerName(auditorPod1, lib.SessionConsumerIn)
	create(pod, podIn, lib.TaskInSubject(auditor, "*"))

	b, bViolations, err := h.connectForTask(t, stemTwo, tokenAuditorPod1, taskToken(t, kp, func(c *taskcred.Claims) { c.Task = taskTwo }))
	if err != nil {
		t.Fatal(err)
	}
	cases := map[string]bool{
		consumerSubject("INFO", lib.SessionConsumerName(stemTwo, lib.SessionConsumerIn)): false,
	}
	for _, name := range []string{aIn, lib.SessionConsumerName(stemOne, lib.SessionConsumerOrigin), lib.SessionConsumerName(stemOne, lib.SessionConsumerEvents), podIn} {
		for _, op := range []string{"INFO", "MSG.NEXT", "DELETE"} {
			cases[consumerSubject(op, name)] = true
		}
	}
	checkPublish(t, b, bViolations, cases)
}

// The credential's lifetime is the callout's TaskCredentialMaxLifetime, not
// GrantTTL: a task runs to its profile's deadline. The connection JWT is still
// capped by GrantTTL.
func TestATaskCredentialMayOutliveGrantTTLButTheConnectionMayNot(t *testing.T) {
	kp, pub := taskSigningKey(t)
	userKP, _ := nkeys.CreateUser()
	userPub, _ := userKP.PublicKey()
	svc := newTaskTestService(t, []string{pub}, time.Hour)

	exp := time.Now().Add(2 * time.Hour)
	d, err := authorizeWithTask(svc, tokenAuditorPod1, taskToken(t, kp, func(c *taskcred.Claims) { c.Expires = exp }))
	if err != nil {
		t.Fatalf("a credential 2h out was refused under a 1h GrantTTL: %v", err)
	}
	raw, err := svc.mint(&jwt.AuthorizationRequestClaims{UserNkey: userPub}, d)
	if err != nil {
		t.Fatal(err)
	}
	uc, err := jwt.DecodeUserClaims(raw)
	if err != nil {
		t.Fatal(err)
	}
	if ceiling := time.Now().Add(time.Hour).Unix(); uc.Expires > ceiling {
		t.Errorf("minted exp %d is past GrantTTL (%d); the credential's lifetime must not lengthen the connection", uc.Expires, ceiling)
	}

	tooLong := taskToken(t, kp, func(c *taskcred.Claims) {
		c.Expires = time.Now().Add(defaultTaskCredentialMaxLifetime + taskCredentialSkew + time.Minute)
	})
	if _, err := authorizeWithTask(svc, tokenAuditorPod1, tooLong); err == nil || !strings.Contains(err.Error(), "ceiling") {
		t.Errorf("a credential past the max lifetime plus skew: %v", err)
	}

	short := newTaskTestServiceWithLifetime(t, pub, 30*time.Minute)
	if _, err := authorizeWithTask(short, tokenAuditorPod1, taskToken(t, kp, func(c *taskcred.Claims) { c.Expires = exp })); err == nil {
		t.Error("a 2h credential was accepted under a 30m TaskCredentialMaxLifetime")
	}
}

func newTaskTestServiceWithLifetime(t *testing.T, pub string, lifetime time.Duration) *Service {
	t.Helper()
	svc := newTaskTestService(t, []string{pub}, 0)
	svc.taskMaxLifetime = lifetime
	return svc
}

// When GrantTTL ends a task connection, the worker reconnects with the same
// credential, still inside its expiry, and gets the same task's grants.
func TestATaskConnectionReconnectsWithTheSameCredentialAfterGrantTTL(t *testing.T) {
	kp, pub := taskSigningKey(t)
	h := startHarness(t, profileMap, profileTokens(), withTaskSigningKeys(pub), withGrantTTL(2*time.Second))
	cred := taskToken(t, kp, func(c *taskcred.Claims) { c.Expires = time.Now().Add(10 * time.Minute) })
	closed := make(chan struct{})
	if _, _, err := h.connectForTask(t, stemOne, tokenAuditorPod1, cred,
		nats.NoReconnect(), nats.ClosedHandler(func(*nats.Conn) { close(closed) })); err != nil {
		t.Fatalf("first connect: %v", err)
	}
	select {
	case <-closed:
	case <-time.After(10 * time.Second):
		t.Fatal("the first connection outlived GrantTTL")
	}
	nc, violations, err := h.connectForTask(t, stemOne, tokenAuditorPod1, cred)
	if err != nil {
		t.Fatalf("reconnecting with the same credential was refused: %v", err)
	}
	checkPublish(t, nc, violations, map[string]bool{
		lib.TaskEventsSubject(auditor, taskOne): false,
		lib.TaskEventsSubject(auditor, taskTwo): true,
	})
}

func TestParseTaskCredentialMaxLifetime(t *testing.T) {
	for raw, want := range map[string]time.Duration{
		"": 0, "6h0m0s": 6 * time.Hour, "1m0s": time.Minute, "90m": 90 * time.Minute,
		// Trimmed: whitespace alone is unset, and padding doesn't make a
		// value unusable.
		"   ": 0, "\n": 0, "6h0m0s\n": 6 * time.Hour, " 6h ": 6 * time.Hour,
		// The ceiling itself is usable.
		"168h": 168 * time.Hour,
	} {
		if got, err := ParseTaskCredentialMaxLifetime(raw); err != nil || got != want {
			t.Errorf("%q = %v, %v; want %v", raw, got, err, want)
		}
	}
	// Past the 7-day ceiling, and near time.Duration's maximum.
	for _, raw := range []string{"6", "six hours", "0s", "-1h", "168h1s", "2562047h"} {
		if got, err := ParseTaskCredentialMaxLifetime(raw); err == nil || !strings.Contains(err.Error(), "A2A_TASK_CREDENTIAL_MAX_LIFETIME") {
			t.Errorf("%q = %v, %v; want an error naming the variable", raw, got, err)
		}
	}
}

// An unusable lifetime setting fails closed for task credentials and only
// for them: on a real server a valid credential is refused, naming the
// setting, while the same pod's tokenless connection still connects. The
// default is never applied in its place.
func TestAnUnusableLifetimeSettingRefusesTaskCredentialsOnly(t *testing.T) {
	kp, pub := taskSigningKey(t)
	for _, raw := range []string{"six hours", "0s", "168h1s"} {
		t.Run(raw, func(t *testing.T) {
			d, perr := ParseTaskCredentialMaxLifetime(raw)
			h := startHarness(t, profileMap, profileTokens(), withTaskSigningKeys(pub), withTaskLifetime(d, perr))
			if _, _, err := h.connectForTask(t, stemOne, tokenAuditorPod1, taskToken(t, kp, nil)); err == nil {
				t.Error("a valid task credential connected under an unusable lifetime setting")
			}
			h.connectAs(t, auditorPod1, tokenAuditorPod1)

			svc := newTaskTestService(t, []string{pub}, 0)
			svc.taskLifetimeErr, svc.taskMaxLifetime = nil, 0
			// Rebuilt through NewService so the setting takes NewService's path.
			svc2, err := NewService(svc.store, svc.validator, Config{
				IssuerSeed:                       mustIssuerSeed(t),
				ReservedPrincipals:               renderedFixtureAuthUsers(t),
				ReservedAddressees:               renderedFixtureAddressees(t),
				TaskSigningKeys:                  []string{pub},
				TaskCredentialMaxLifetime:        d,
				TaskCredentialMaxLifetimeInvalid: perr,
			}, slog.New(slog.NewTextHandler(io.Discard, nil)))
			if err != nil {
				t.Fatalf("NewService stopped on an unusable lifetime setting: %v", err)
			}
			if _, err := authorizeWithTask(svc2, tokenAuditorPod1, taskToken(t, kp, nil)); err == nil || !strings.Contains(err.Error(), "A2A_TASK_CREDENTIAL_MAX_LIFETIME") {
				t.Errorf("refusal %v does not name the setting", err)
			}
		})
	}
}

func mustIssuerSeed(t *testing.T) string {
	t.Helper()
	kp, err := nkeys.CreateAccount()
	if err != nil {
		t.Fatal(err)
	}
	seed, _ := kp.Seed()
	return string(seed)
}

// The longest stem a task connection can have: a 63-byte pod name and a
// 63-byte task id, so a 127-byte stem and consumer names up to 134 bytes. The
// server accepts all three consumers with their exact filters, and the
// connection can INFO and pull from them under its grants.
func TestTheLongestStemWorksOnARealServer(t *testing.T) {
	kp, pub := taskSigningKey(t)
	longPod := "auditor-read-" + strings.Repeat("p", 63-len("auditor-read-"))
	longTask := "task-" + strings.Repeat("t", 63-len("task-"))
	const longPodToken = "token-for-the-auditor-profile-sa-bound-to-the-longest-pod-name-padded"
	tokens := profileTokens()
	tokens[longPodToken] = Attested{ServiceAccount: auditorSA, PodName: longPod, PodUID: "uid-long"}
	if len(longPod) != 63 || len(longTask) != 63 || !lib.ValidSubjectToken(longPod) || !lib.ValidSubjectToken(longTask) {
		t.Fatalf("the inputs are not 63-byte DNS-1123 labels: %d %d", len(longPod), len(longTask))
	}
	stem := taskcred.ConsumerStem(longPod, longTask)
	if len(stem) != 127 {
		t.Fatalf("stem is %d bytes, want 127", len(stem))
	}

	h := startHarness(t, profileMap, tokens, withTaskSigningKeys(pub))
	provisionTasksStream(t, h)
	// One submission on the task's .in, so the pull below has something
	// to deliver on the two .in consumers.
	submitAs(t, h, auditor, longTask, "the longest stem", nil)
	nc, violations, err := h.connectForTask(t, stem, longPodToken, taskToken(t, kp, func(c *taskcred.Claims) {
		c.Task, c.Pod, c.PodUID = longTask, longPod, "uid-long"
	}))
	if err != nil {
		t.Fatalf("the longest-stem task connection was refused: %v", err)
	}
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	for _, role := range lib.SessionConsumerRoles {
		name := lib.SessionConsumerName(stem, role)
		filter := lib.TaskInSubject(auditor, longTask)
		if role == lib.SessionConsumerEvents {
			filter = lib.TaskEventsSubject(auditor, longTask)
		}
		c, err := js.CreateOrUpdateConsumer(ctx, lib.TasksStream, jetstream.ConsumerConfig{
			Name: name, FilterSubject: filter, AckPolicy: jetstream.AckNonePolicy, InactiveThreshold: time.Minute, MemoryStorage: true,
		})
		if err != nil {
			t.Fatalf("CREATE %s (%d bytes) was refused: %v", name, len(name), err)
		}
		info, err := c.Info(ctx)
		if err != nil {
			t.Fatalf("INFO %s: %v", name, err)
		}
		if info.Config.FilterSubject != filter {
			t.Errorf("%s filters %q, want %q", name, info.Config.FilterSubject, filter)
		}
		// MSG.NEXT: the .in consumers deliver the submission; the .events
		// one has nothing to deliver and times out, which is not a refusal
		// (a refusal arrives on violations, checked below).
		batch, err := c.Fetch(1, jetstream.FetchMaxWait(2*time.Second))
		if err != nil {
			t.Fatalf("MSG.NEXT %s: %v", name, err)
		}
		got := 0
		for range batch.Messages() {
			got++
		}
		if want := map[bool]int{true: 0, false: 1}[role == lib.SessionConsumerEvents]; got != want {
			t.Errorf("MSG.NEXT %s delivered %d messages, want %d (err %v)", name, got, want, batch.Error())
		}
	}
	select {
	case v := <-violations:
		t.Errorf("the longest-stem connection hit a permissions error: %v", v)
	default:
	}
}
