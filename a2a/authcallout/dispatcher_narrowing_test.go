package authcallout

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/nats-io/jwt/v2"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"

	lib "github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	dispatcherSA    = "system:serviceaccount:kubeagents-system:agent-a2a-dispatcher"
	dispatcherToken = "token-for-the-dispatcher-serviceaccount-padded-to-a-realistic-length"
	dispatcherPod   = "agent-a2a-dispatcher-7d9f-x2x"
)

// dispatcherFixed is the control connection's grant set: the fixed half of
// what the operator rendered before #5381, which names no profile.
var dispatcherFixed = Grants{
	Publish: []string{
		"$KV.PROFILE_LEASES.>", "$JS.API.STREAM.INFO.KV_PROFILE_LEASES", "$JS.API.DIRECT.GET.KV_PROFILE_LEASES.>",
		"$JS.API.CONSUMER.CREATE.KV_PROFILE_LEASES.>", "$JS.API.CONSUMER.DELETE.KV_PROFILE_LEASES.*",
		"$JS.FC.KV_PROFILE_LEASES.>", "a2a.dispatch.*.*.*", "a2a.cap.verify.dispatcher", "_INBOX.dispatcher.>",
	},
	Subscribe: []string{
		"a2a.dispatch.*.*.*.accept", "a2a.dispatch.*.*.*.lease", "a2a.cap.reply.dispatcher.>", "_INBOX.dispatcher.>",
	},
}

func dispatcherEntry(draining ...string) Identity {
	return Identity{ServiceAccount: dispatcherSA, User: "dispatcher", Account: AccountControl,
		Narrowing: NarrowingDispatcher, Grants: dispatcherFixed, Draining: draining}
}

func profileEntry(profile, tier string) Identity {
	sa := "system:serviceaccount:kubeagents-system:agentprofile-" + profile
	user := "profile-" + profile
	if tier != "" {
		sa, user = sa+"-"+tier, user+"-"+tier
	}
	return Identity{ServiceAccount: sa, User: user, Account: AccountApp, Narrowing: NarrowingProfile,
		Profile: profile, Tier: tier, Grants: Grants{Publish: []string{}, Subscribe: []string{}}}
}

func renderMap(t *testing.T, ids ...Identity) []byte {
	t.Helper()
	base := []Identity{
		{ServiceAccount: "system:serviceaccount:kubeagents-system:agent-a2a-gateway", User: "gateway", Account: AccountControl,
			Grants: Grants{Publish: []string{"a2a.tasks.>", "_INBOX.gateway.>"}, Subscribe: []string{"a2a.tasks.>", "_INBOX.gateway.>"}}},
		{ServiceAccount: sessionSA, User: "session", Account: AccountApp, Narrowing: NarrowingPod,
			Grants: Grants{Publish: []string{}, Subscribe: []string{}}},
	}
	raw, err := json.Marshal(IdentityMap{Version: "dispatcher-itest-1", Identities: append(base, ids...)})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// profilesInBucket and profilesOutside pick test profile names by their
// bucket, so a test can't pass on a lucky hash.
func profileHashingTo(bucket int, skip ...string) string {
	for i := 0; ; i++ {
		name := fmt.Sprintf("prof-%d", i)
		if lib.DispatcherBucket(name) == bucket && !slices.Contains(skip, name) {
			return name
		}
	}
}

func newDispatcherTestService(t *testing.T, rawMap []byte, tokens map[string]Attested, extraPrincipals ...string) *Service {
	t.Helper()
	issuerKP, _ := nkeys.CreateAccount()
	issuerSeed, _ := issuerKP.Seed()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	store := NewStore(log)
	if err := store.Update(rawMap); err != nil {
		t.Fatalf("loading the identity map: %v", err)
	}
	validator, err := NewTokenValidator(tokenReviewer(tokens, nil), testAudience)
	if err != nil {
		t.Fatal(err)
	}
	svc, err := NewService(store, validator, Config{
		IssuerSeed:         string(issuerSeed),
		ReservedPrincipals: append(renderedFixtureAuthUsers(t), extraPrincipals...),
		ReservedAddressees: renderedFixtureAddressees(t),
	}, log)
	if err != nil {
		t.Fatal(err)
	}
	return svc
}

func dispatcherTokens() map[string]Attested {
	return map[string]Attested{
		dispatcherToken: {ServiceAccount: dispatcherSA, PodName: dispatcherPod, PodUID: "uid-d"},
		gatewayToken:    {ServiceAccount: "system:serviceaccount:kubeagents-system:agent-a2a-gateway"},
		tokenPodA:       {ServiceAccount: sessionSA, PodName: podA, PodUID: "uid-a"},
	}
}

func connect(svc *Service, token, password string) (decision, error) {
	req := &jwt.AuthorizationRequestClaims{}
	req.ConnectOptions.Token = token
	req.ConnectOptions.Password = password
	return svc.authorize(context.Background(), req)
}

func allowed(d decision) Grants {
	return Grants{Publish: []string(d.perms.Pub.Allow), Subscribe: []string(d.perms.Sub.Allow)}
}

// The control connection presents the token alone and gets the entry's own
// grants, exactly: nothing per-profile, whatever the map's profiles are.
func TestTheDispatchersControlConnectionGetsTheFixedGrantsOnly(t *testing.T) {
	a := profileHashingTo(3)
	svc := newDispatcherTestService(t, renderMap(t, dispatcherEntry("gone"), profileEntry(a, "read")), dispatcherTokens())
	d, err := connect(svc, dispatcherToken, "")
	if err != nil {
		t.Fatalf("refused: %v", err)
	}
	if d.user != "dispatcher" || d.account != AccountControl {
		t.Fatalf("user/account = %q/%q", d.user, d.account)
	}
	if got := allowed(d); !reflect.DeepEqual(got, dispatcherFixed) {
		t.Fatalf("control grants =\n%v\nwant\n%v", got, dispatcherFixed)
	}
}

// A bucket connection gets its own profiles' grants, at every tier the map
// gives them, its draining profiles' terminal grants, and its own inbox. It
// gets nothing of another bucket's profiles and nothing of the control set.
func TestABucketConnectionGetsItsProfilesGrantsAndNothingElse(t *testing.T) {
	const b = 5
	mine, other := profileHashingTo(b), profileHashingTo(b+1)
	drainMine, drainOther := profileHashingTo(b, mine), profileHashingTo(b+1, other)
	svc := newDispatcherTestService(t, renderMap(t,
		dispatcherEntry(drainMine, drainOther, mine), // a live profile listed as draining too stays live
		profileEntry(mine, "read"), profileEntry(mine, "unattended"), profileEntry(other, "read"),
	), dispatcherTokens())

	d, err := connect(svc, dispatcherToken, lib.DispatcherBucketCredential(b))
	if err != nil {
		t.Fatalf("refused: %v", err)
	}
	if d.user != fmt.Sprintf("dispatcher-b%d", b) || d.account != AccountControl {
		t.Fatalf("user/account = %q/%q", d.user, d.account)
	}
	want := lib.DispatcherGrants([]lib.DispatcherGrantProfile{
		{Name: mine, Tiers: []string{"read", "unattended"}},
		{Name: drainMine, Draining: true},
	})
	inbox := fmt.Sprintf("_INBOX.dispatcher-b%d.>", b)
	wantG := Grants{Publish: append(want.Publish, inbox), Subscribe: append(want.Subscribe, inbox)}
	if got := allowed(d); !reflect.DeepEqual(got, wantG) {
		t.Fatalf("bucket grants =\n%v\nwant\n%v", got, wantG)
	}
	for _, s := range append(allowed(d).Publish, allowed(d).Subscribe...) {
		if strings.Contains(s, other) || strings.Contains(s, drainOther) {
			t.Errorf("bucket %d holds %q, another bucket's", b, s)
		}
		if slices.Contains(dispatcherFixed.Publish, s) || slices.Contains(dispatcherFixed.Subscribe, s) {
			t.Errorf("bucket %d holds the control grant %q", b, s)
		}
	}
}

// An empty bucket is still a valid connection: its inbox on both sides, so
// neither side is empty (which the server would read as unrestricted).
func TestAnEmptyBucketHoldsOnlyItsInbox(t *testing.T) {
	svc := newDispatcherTestService(t, renderMap(t, dispatcherEntry(), profileEntry(profileHashingTo(1), "read")), dispatcherTokens())
	d, err := connect(svc, dispatcherToken, lib.DispatcherBucketCredential(2))
	if err != nil {
		t.Fatalf("refused: %v", err)
	}
	want := Grants{Publish: []string{"_INBOX.dispatcher-b2.>"}, Subscribe: []string{"_INBOX.dispatcher-b2.>"}}
	if got := allowed(d); !reflect.DeepEqual(got, want) {
		t.Fatalf("empty bucket grants = %v, want %v", got, want)
	}
}

// Across all K buckets every admitted profile's grants appear exactly once.
func TestTheBucketsPartitionEveryProfilesGrants(t *testing.T) {
	var ids []Identity
	var names []string
	for i := 0; i < 200; i++ {
		n := fmt.Sprintf("p%d", i)
		names = append(names, n)
		ids = append(ids, profileEntry(n, "read"))
	}
	svc := newDispatcherTestService(t, renderMap(t, append([]Identity{dispatcherEntry()}, ids...)...), dispatcherTokens())
	count := map[string]int{}
	for b := 0; b < lib.DispatcherBuckets; b++ {
		d, err := connect(svc, dispatcherToken, lib.DispatcherBucketCredential(b))
		if err != nil {
			t.Fatalf("bucket %d refused: %v", b, err)
		}
		for _, s := range allowed(d).Subscribe {
			count[s]++
		}
	}
	for _, n := range names {
		if c := count["a2a.stop."+n+".*"]; c != 1 {
			t.Errorf("profile %s's stop grant is in %d buckets", n, c)
		}
	}
}

// A bucket the callout doesn't accept is refused, and never served the
// control connection's grants instead.
func TestABadBucketIsRefusedNotServedTheControlGrants(t *testing.T) {
	svc := newDispatcherTestService(t, renderMap(t, dispatcherEntry(), profileEntry("a", "read")), dispatcherTokens())
	for _, bad := range []string{"7", "07/32", "-1/32", "32/32", "1/32/x", "7/16", " 7/32", "x"} {
		if d, err := connect(svc, dispatcherToken, bad); err == nil {
			t.Errorf("password %q: accepted as user %q", bad, d.user)
		} else if !strings.Contains(err.Error(), "dispatcher bucket") {
			t.Errorf("password %q: %v", bad, err)
		}
	}
}

// The bucket password is not a credential: beside another principal's token
// it is refused, and with no token it is read as the token, which the API
// server does not vouch for.
func TestABucketPasswordIsNotACredential(t *testing.T) {
	svc := newDispatcherTestService(t, renderMap(t, dispatcherEntry(), profileEntry("a", "read")), dispatcherTokens())
	pw := lib.DispatcherBucketCredential(lib.DispatcherBucket("a"))
	if _, err := connect(svc, gatewayToken, pw); err == nil {
		t.Error("the gateway's token with a bucket password was accepted")
	}
	if _, err := connect(svc, tokenPodA, pw); err == nil {
		t.Error("a session pod's token with a bucket password was accepted")
	}
	if _, err := connect(svc, "", pw); err == nil {
		t.Error("a bucket password with no token was accepted")
	}
}

// The dispatcher's bucket users are reserved: a session pod named after one
// would take that bucket's inbox.
func TestASessionPodNamedForABucketUserIsRefused(t *testing.T) {
	tokens := dispatcherTokens()
	tokens["tok-squat"] = Attested{ServiceAccount: sessionSA, PodName: "dispatcher-b4", PodUID: "uid-s"}
	svc := newDispatcherTestService(t, renderMap(t, dispatcherEntry(), profileEntry("a", "read")), tokens)
	if _, err := connect(svc, "tok-squat", ""); err == nil || !strings.Contains(err.Error(), "dispatcher-b4") {
		t.Fatalf("a session pod named dispatcher-b4: %v", err)
	}
}

// The map is refused where the dispatcher entry could be widened or collide.
func TestTheMapRefusesAnUnsoundDispatcherEntry(t *testing.T) {
	inApp := dispatcherEntry()
	inApp.Account = AccountApp
	emptyPub := dispatcherEntry()
	emptyPub.Grants.Publish = nil
	podInTrusted := Identity{ServiceAccount: "system:serviceaccount:x:y", User: "y", Account: AccountControl,
		Narrowing: NarrowingPod, Grants: Grants{Publish: []string{}, Subscribe: []string{}}}
	profileInTrusted := profileEntry("b", "read")
	profileInTrusted.Account = AccountControl
	drainingOnGateway := Identity{ServiceAccount: "system:serviceaccount:x:z", User: "z", Account: AccountControl,
		Grants: Grants{Publish: []string{"a"}, Subscribe: []string{"b"}}, Draining: []string{"p"}}
	second := dispatcherEntry()
	second.ServiceAccount, second.User = "system:serviceaccount:x:d2", "dispatcher2"
	squatUser := Identity{ServiceAccount: "system:serviceaccount:x:sq", User: "dispatcher-b3", Account: AccountControl,
		Grants: Grants{Publish: []string{"a"}, Subscribe: []string{"b"}}}
	for name, ids := range map[string][]Identity{
		"dispatcher in APP":                   {inApp},
		"dispatcher with an empty side":       {emptyPub},
		"pod narrowing in TRUSTED":            {dispatcherEntry(), podInTrusted},
		"profile narrowing in TRUSTED":        {dispatcherEntry(), profileInTrusted},
		"draining on a non-dispatcher entry":  {drainingOnGateway},
		"two dispatchers":                     {dispatcherEntry(), second},
		"a map user named for a bucket user":  {dispatcherEntry(), squatUser},
		"a profile named for a bucket user":   {dispatcherEntry(), profileEntry("dispatcher-b3", "read")},
		"a draining name that is not a token": {dispatcherEntry("a.b")},
		"a draining name twice":               {dispatcherEntry("a", "a")},
	} {
		if _, err := ParseIdentityMap(renderMap(t, ids...)); err == nil {
			t.Errorf("%s: the map was accepted", name)
		}
	}
	if _, err := ParseIdentityMap(renderMap(t, dispatcherEntry("gone"), profileEntry("a", "read"))); err != nil {
		t.Fatalf("a sound dispatcher map was refused: %v", err)
	}
}

// At 2,500 profiles with two tiers each, the largest bucket's minted user
// JWT stays well inside the callout's 1 MiB reply, even when the names are
// skewed. The names here share long prefixes and run in sequence, the way an
// installer stamping out profiles per team and cluster would name them.
func TestTheLargestBucketsJWTAt2500SkewedProfiles(t *testing.T) {
	const n = 2500
	ids := []Identity{dispatcherEntry()}
	perBucket := make([]int, lib.DispatcherBuckets)
	for i := 0; i < n; i++ {
		name := fmt.Sprintf("team-%03d-cluster-%02d", i/25, i%25)
		perBucket[lib.DispatcherBucket(name)]++
		ids = append(ids, profileEntry(name, "read"), profileEntry(name, "unattended"))
	}
	rawMap := renderMap(t, ids...)
	largest := 0
	for b, c := range perBucket {
		if c > perBucket[largest] {
			largest = b
		}
	}
	svc := newDispatcherTestService(t, rawMap, dispatcherTokens())
	d, err := connect(svc, dispatcherToken, lib.DispatcherBucketCredential(largest))
	if err != nil {
		t.Fatalf("bucket %d refused: %v", largest, err)
	}
	user, _ := nkeys.CreateUser()
	userPub, _ := user.PublicKey()
	raw, err := svc.mint(&jwt.AuthorizationRequestClaims{UserNkey: userPub}, d)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("map %d bytes", len(rawMap))
	t.Logf("largest bucket %d: %d profiles (mean %d), user JWT %d bytes", largest, perBucket[largest], n/lib.DispatcherBuckets, len(raw))
	// The JWT rides inside the signed authorization response, which encodes
	// it again, so leave a third of the reply for that.
	if len(raw) > 700*1024 {
		t.Fatalf("the largest bucket's user JWT is %d bytes; past ~700 KiB the callout's reply passes 1 MiB", len(raw))
	}
	if perBucket[largest] > 2*n/lib.DispatcherBuckets {
		t.Errorf("bucket %d holds %d of %d profiles, over twice the mean: the hash is not spreading these names", largest, perBucket[largest], n)
	}
}

// Against a real server: a bucket connection publishes its own profile's
// supervisor and durable subjects, and is refused on another bucket's
// profile and on the control connection's fixed subjects. The control
// connection is refused on every profile's.
func TestABucketConnectionReachesItsProfilesOnARealServer(t *testing.T) {
	const b = 9
	mine, other := profileHashingTo(b), profileHashingTo(b+1)
	h := startHarness(t, string(renderMap(t, dispatcherEntry(), profileEntry(mine, "read"), profileEntry(other, "read"))), dispatcherTokens())

	bucketUser := dispatcherBucketUser("dispatcher", b)
	nc, violations, err := h.connectForTask(t, bucketUser, dispatcherToken, lib.DispatcherBucketCredential(b))
	if err != nil {
		t.Fatalf("bucket %d could not connect: %v", b, err)
	}
	checkPublish(t, nc, violations, map[string]bool{
		lib.TaskSupervisorSubject(mine, "task-1"):                                                    false,
		"$JS.API.CONSUMER.INFO.PROFILE_SUBMITS." + lib.ProfileDurableName(mine, "read", "top"):       false,
		lib.TaskSupervisorSubject(other, "task-1"):                                                   true,
		"$JS.API.CONSUMER.INFO.PROFILE_SUBMITS." + lib.ProfileDurableName(other, "read", "top"):      true,
		"$JS.API.CONSUMER.INFO.PROFILE_SUBMITS." + lib.ProfileDurableName(mine, "unattended", "top"): true,
		lib.DispatchSubject(mine, "read", "pod-1"):                                                   true,
		"$KV.PROFILE_LEASES.x": true,
	})

	ctl, ctlViolations := h.connectAs(t, "dispatcher", dispatcherToken)
	checkPublish(t, ctl, ctlViolations, map[string]bool{
		lib.DispatchSubject(mine, "read", "pod-1"):                                             false,
		lib.TaskSupervisorSubject(mine, "task-1"):                                              true,
		"$JS.API.CONSUMER.INFO.PROFILE_SUBMITS." + lib.ProfileDurableName(mine, "read", "top"): true,
	})
}

// The operator's bucketed render (k8s-operator's
// TestBucketedGrantsRenderTheFixedSetAndTheDrainingNames writes it) parses
// here, and a bucket connection against it gets tse's grants at both tiers
// and the draining profile's terminal grants in their buckets.
func TestTheOperatorsBucketedDispatcherMapParsesAndDerives(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "rendered-identity-map-dispatcher-bucketed.json"))
	if err != nil {
		t.Fatal(err)
	}
	m, err := ParseIdentityMap(raw)
	if err != nil {
		t.Fatalf("the operator's bucketed map was refused: %v", err)
	}
	var disp Identity
	for _, id := range m.Identities {
		if id.Narrowing == NarrowingDispatcher {
			disp = id
		}
	}
	if disp.User == "" || disp.Account != AccountControl || !slices.Equal(disp.Draining, []string{"gone"}) {
		t.Fatalf("dispatcher entry = %+v", disp)
	}
	got := dispatcherBucketGrants(m, disp, lib.DispatcherBucket("tse"))
	for _, want := range []string{
		"$JS.API.CONSUMER.MSG.NEXT.PROFILE_SUBMITS." + lib.ProfileDurableName("tse", "read", "top"),
		"$JS.API.CONSUMER.MSG.NEXT.PROFILE_SUBMITS." + lib.ProfileDurableName("tse", "unattended", "child"),
		lib.TaskSupervisorSubject("tse", "*"),
	} {
		if !slices.Contains(got.Publish, want) {
			t.Errorf("tse's bucket lacks %q", want)
		}
	}
	gone := dispatcherBucketGrants(m, disp, lib.DispatcherBucket("gone"))
	if !slices.Contains(gone.Subscribe, "a2a.stop.gone.*") {
		t.Errorf("gone's bucket lacks its stop grant: %v", gone.Subscribe)
	}
	for _, s := range gone.Publish {
		if strings.Contains(s, "PROFILE_SUBMITS") && strings.Contains(s, "gone") {
			t.Errorf("draining gone still holds a durable grant %q", s)
		}
	}
}

// A draining name the callout reserves (an addressee such as the bridge's,
// or a static principal) would grant the bucket that name's .supervisor
// subject and stop grants. The map can't see the callout's reserved names,
// so the mint refuses the bucket instead.
func TestADrainingNameTheCalloutReservesRefusesTheBucket(t *testing.T) {
	for _, reserved := range append(renderedFixtureAddressees(t)[:1], renderedFixtureAuthUsers(t)[0]) {
		svc := newDispatcherTestService(t, renderMap(t, dispatcherEntry(reserved), profileEntry("a", "read")), dispatcherTokens())
		if _, err := connect(svc, dispatcherToken, lib.DispatcherBucketCredential(lib.DispatcherBucket(reserved))); err == nil || !strings.Contains(err.Error(), reserved) {
			t.Errorf("draining %q: %v", reserved, err)
		}
		// The control connection doesn't derive from the draining names.
		if _, err := connect(svc, dispatcherToken, ""); err != nil {
			t.Errorf("draining %q refused the control connection: %v", reserved, err)
		}
	}
}

// A bucket user that names a static principal is refused: its inbox would
// be that principal's.
func TestABucketUserNamedForAStaticPrincipalIsRefused(t *testing.T) {
	svc := newDispatcherTestService(t, renderMap(t, dispatcherEntry(), profileEntry("a", "read")), dispatcherTokens(), "dispatcher-b3")
	if _, err := connect(svc, dispatcherToken, lib.DispatcherBucketCredential(3)); err == nil || !strings.Contains(err.Error(), "dispatcher-b3") {
		t.Fatalf("bucket 3 with a static principal dispatcher-b3: %v", err)
	}
	if _, err := connect(svc, dispatcherToken, lib.DispatcherBucketCredential(4)); err != nil {
		t.Fatalf("bucket 4 refused: %v", err)
	}
}

// The dispatcher's real bucket options (lib.DispatcherBucketNATSOptions: the
// token from its file, the bucket as the CONNECT password, the bucket user's
// inbox) against a real server serving the operator's bucketed render: the
// bucket connection reaches tse's subjects and is refused on another
// profile's and on the control set.
func TestTheRealBucketOptionsConnectAgainstTheOperatorsBucketedMap(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "rendered-identity-map-dispatcher-bucketed.json"))
	if err != nil {
		t.Fatal(err)
	}
	h := startHarness(t, string(raw), map[string]Attested{
		dispatcherToken: {ServiceAccount: dispatcherSA, PodName: dispatcherPod, PodUID: "uid-d"},
	})
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte(dispatcherToken), 0o600); err != nil {
		t.Fatal(err)
	}
	b := lib.DispatcherBucket("tse")
	opts, err := lib.DispatcherBucketNATSOptions(tokenFile, "dispatcher", b)
	if err != nil {
		t.Fatal(err)
	}
	violations := make(chan error, 16)
	nc, err := nats.Connect(h.url, append([]nats.Option{h.dial, nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, e error) { violations <- e })}, opts...)...)
	if err != nil {
		t.Fatalf("bucket %d with the real options was refused: %v", b, err)
	}
	t.Cleanup(nc.Close)
	other := profileHashingTo((b + 1) % lib.DispatcherBuckets)
	checkPublish(t, nc, violations, map[string]bool{
		lib.TaskSupervisorSubject("tse", "task-1"):                                              false,
		"$JS.API.CONSUMER.INFO.PROFILE_SUBMITS." + lib.ProfileDurableName("tse", "read", "top"): false,
		lib.TaskSupervisorSubject(other, "task-1"):                                              true,
		lib.DispatchSubject("tse", "read", "pod-1"):                                             true,
	})
}
