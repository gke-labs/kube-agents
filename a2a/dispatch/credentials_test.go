package dispatch

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nkeys"

	"github.com/gke-labs/kube-agents/a2a/authcallout"
	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/taskcred"
)

// Per-task credentials (#3889, contract addendum 8). With a task-signing key
// the dispatcher mints two credentials for the pod it confirms, once its
// lease is recorded, and sends them on the confirm: a task credential for
// the bus, and for a task that names a cluster a token for the credential
// proxy carrying that cluster. Nothing else carries them, nothing else mints
// them, and without a key there are none.

// clusterParams is a task naming a cluster target.
var clusterParams = map[string]string{"project": "proj-1", "location": "us-central1", "cluster": "prod-1"}

// mintLog records every credential the dispatcher mints.
type mintLog struct {
	mu     sync.Mutex
	claims []taskcred.Claims
}

func (m *mintLog) wrap(d *Dispatcher) {
	inner := d.mint
	if inner == nil {
		return
	}
	d.mint = func(c taskcred.Claims) (string, error) {
		m.mu.Lock()
		m.claims = append(m.claims, c)
		m.mu.Unlock()
		return inner(c)
	}
}

// pods is the pod each mint named, in order.
func (m *mintLog) pods() []string {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []string
	for _, c := range m.claims {
		out = append(out, c.Pod)
	}
	return out
}

func (m *mintLog) count() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.claims)
}

// signingKey is a fresh task-signing key and its public half.
func signingKey(t *testing.T) (nkeys.KeyPair, string) {
	t.Helper()
	kp, err := taskcred.NewSigningKey()
	if err != nil {
		t.Fatal(err)
	}
	pub, err := kp.PublicKey()
	if err != nil {
		t.Fatal(err)
	}
	return kp, pub
}

// runKeyed runs a dispatcher holding kp (nil for none), its mints logged.
func (b *bus) runKeyed(ti *testInstall, kp nkeys.KeyPair, maxLifetime time.Duration) (*Dispatcher, *mintLog, func()) {
	b.t.Helper()
	return b.runKeyedWith(ti, kp, maxLifetime, &fakeVerifier{}, nil)
}

// runKeyedWith is runKeyed with verifier v, and prepare (nil for none)
// called on the dispatcher before its mints are logged: a test can replace
// d.mint there and still see what was asked of it.
func (b *bus) runKeyedWith(ti *testInstall, kp nkeys.KeyPair, maxLifetime time.Duration, v Verifier, prepare func(*Dispatcher)) (*Dispatcher, *mintLog, func()) {
	b.t.Helper()
	cfg := testConfig()
	cfg.TaskSigningKey, cfg.TaskCredentialMaxLifetime = kp, maxLifetime
	ml := &mintLog{}
	d, stop := b.runPrepared(b.connect("dispatcher"), cfg, ti, v, func(d *Dispatcher) {
		if prepare != nil {
			prepare(d)
		}
		ml.wrap(d)
	})
	return d, ml, stop
}

// verifyAs checks token as the callout (nats) or the proxy would, at the
// default lifetime.
func verifyAs(t *testing.T, token, pub string, aud taskcred.Audience) taskcred.Claims {
	t.Helper()
	c, err := taskcred.Verify(token, []string{pub}, aud, time.Now(), authcallout.DefaultTaskCredentialMaxLifetime, authcallout.TaskCredentialSkew)
	if err != nil {
		t.Fatalf("the %s credential does not verify: %v", aud, err)
	}
	return c
}

// confirmsOf, offersOf and abandonsOf copy what a fake worker heard.
func confirmsOf(w *fakeWorker) []lib.DispatchMessage {
	w.mu.Lock()
	defer w.mu.Unlock()
	return slices.Clone(w.confirms)
}

func offersOf(w *fakeWorker) []lib.DispatchMessage {
	w.mu.Lock()
	defer w.mu.Unlock()
	return slices.Clone(w.offers)
}

func abandonsOf(w *fakeWorker) []lib.DispatchMessage {
	w.mu.Lock()
	defer w.mu.Unlock()
	return slices.Clone(w.abandons)
}

// noCredentials fails the test if any offer or abandon w heard carries a
// credential.
func noCredentials(t *testing.T, w *fakeWorker) {
	t.Helper()
	for _, m := range append(offersOf(w), abandonsOf(w)...) {
		if m.TaskCredential != "" || m.ProxyToken != "" {
			t.Errorf("%s's %s carries a credential", w.pod, m.Type)
		}
	}
}

// The confirm carries both credentials. Each verifies with the key's public
// half for its own audience only, names the task, its profile and tier, the
// confirmed pod and that pod's UID from the pool, and expires at the offer's
// deadline plus the callout's skew. The proxy token carries the cluster
// admission checked; the bus credential carries none. The offer carried
// neither.
func TestAConfirmCarriesTheTaskCredentials(t *testing.T) {
	b := newBus(t, "tse-1")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-1", answerAccept, time.Second)
	ti := oneProfile("tse", 2, "tse-1")
	ti.pool.SetUID("tse", "read", "tse-1", "uid-tse-1")
	kp, pub := signingKey(t)
	_, ml, _ := b.runKeyed(ti, kp, 0)

	b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "cap-ok", clusterParams)
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v", ts)
	}
	confirms, offers := confirmsOf(w), offersOf(w)
	if len(confirms) != 1 || len(offers) != 1 {
		t.Fatalf("%d confirms, %d offers; want one each", len(confirms), len(offers))
	}
	noCredentials(t, w)
	confirm, offer := confirms[0], offers[0]
	if confirm.TaskCredential == "" || confirm.ProxyToken == "" {
		t.Fatalf("the confirm carries task credential %t, proxy token %t; want both", confirm.TaskCredential != "", confirm.ProxyToken != "")
	}
	wantExpiry := time.Unix(offer.Deadline.Add(authcallout.TaskCredentialSkew).Unix(), 0)
	natsC := verifyAs(t, confirm.TaskCredential, pub, taskcred.AudienceNATS)
	proxyC := verifyAs(t, confirm.ProxyToken, pub, taskcred.AudienceCredentialProxy)
	for name, c := range map[string]taskcred.Claims{"task credential": natsC, "proxy token": proxyC} {
		if c.Task != "task-1" || c.Profile != "tse" || c.Tier != "read" || c.Pod != "tse-1" || c.PodUID != "uid-tse-1" {
			t.Errorf("%s names task %s, profile %s, tier %s, pod %s, uid %s; want task-1, tse, read, tse-1, uid-tse-1",
				name, c.Task, c.Profile, c.Tier, c.Pod, c.PodUID)
		}
		if !c.Expires.Equal(wantExpiry) {
			t.Errorf("%s expires %v, want the offer's deadline %v plus the skew: %v", name, c.Expires, offer.Deadline, wantExpiry)
		}
	}
	if natsC.Target != nil {
		t.Errorf("the task credential carries a target %+v", *natsC.Target)
	}
	want := taskcred.Target{Project: "proj-1", Location: "us-central1", Cluster: "prod-1"}
	if proxyC.Target == nil || *proxyC.Target != want {
		t.Errorf("the proxy token's target %+v, want the admitted cluster %+v", proxyC.Target, want)
	}
	if natsC.ID == "" || natsC.ID == proxyC.ID {
		t.Errorf("jtis %q and %q: want two distinct ones", natsC.ID, proxyC.ID)
	}
	// Each is good for its own audience only.
	if _, err := taskcred.Verify(confirm.TaskCredential, []string{pub}, taskcred.AudienceCredentialProxy, time.Now(), time.Hour, authcallout.TaskCredentialSkew); err == nil {
		t.Error("the task credential verifies for the credential proxy")
	}
	if _, err := taskcred.Verify(confirm.ProxyToken, []string{pub}, taskcred.AudienceNATS, time.Now(), time.Hour, authcallout.TaskCredentialSkew); err == nil {
		t.Error("the proxy token verifies for the bus")
	}
	if got := ml.pods(); !slices.Equal(got, []string{"tse-1", "tse-1"}) {
		t.Errorf("mints for %v, want two, for tse-1", got)
	}
}

// The expiry is the deadline plus the skew only up to the max lifetime: a
// deadline further out gets a credential that expires the max lifetime from
// when it was minted, which the callout, capped the same, accepts.
func TestTheCredentialsExpiryIsCappedAtTheMaxLifetime(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	kp, pub := signingKey(t)
	b.runKeyed(ti, kp, 10*time.Second) // the profile's deadline is 60s

	before := time.Now()
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "cap-ok", clusterParams)
	waitFor(t, 10*time.Second, "the confirm", func() bool { return w.confirmCount() == 1 })
	after := time.Now()
	confirm := confirmsOf(w)[0]
	for aud, token := range map[taskcred.Audience]string{taskcred.AudienceNATS: confirm.TaskCredential, taskcred.AudienceCredentialProxy: confirm.ProxyToken} {
		c, err := taskcred.Verify(token, []string{pub}, aud, time.Now(), 10*time.Second, authcallout.TaskCredentialSkew)
		if err != nil {
			t.Fatalf("%s: a callout capped at 10s refuses it: %v", aud, err)
		}
		lo, hi := before.Add(10*time.Second).Truncate(time.Second), after.Add(10*time.Second)
		if c.Expires.Before(lo) || c.Expires.After(hi) {
			t.Errorf("%s expires %v, want the 10s cap from the mint, in [%v, %v]", aud, c.Expires, lo, hi)
		}
	}
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Errorf("terminal %+v", ts)
	}
}

// A task that names no cluster was checked against the namespace: it gets
// its task credential and no proxy token.
func TestATaskWithNoClusterGetsNoProxyToken(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	kp, pub := signingKey(t)
	_, ml, _ := b.runKeyed(ti, kp, 0)

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v", ts)
	}
	confirm := confirmsOf(w)[0]
	if confirm.ProxyToken != "" {
		t.Error("a task with no cluster got a proxy token")
	}
	if c := verifyAs(t, confirm.TaskCredential, pub, taskcred.AudienceNATS); c.Pod != "tse-0" || c.PodUID != "uid-0" || c.Task != "task-1" {
		t.Errorf("task credential %+v", c)
	}
	if ml.count() != 1 {
		t.Errorf("%d mints, want 1", ml.count())
	}
}

// Without a key the dispatcher runs as before: a confirm carries neither.
func TestWithNoKeyAConfirmCarriesNoCredentials(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	ti := oneProfile("tse", 2, "tse-0") // and no UID: none is needed
	d, _, _ := b.runKeyed(ti, nil, 0)

	b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "cap-ok", clusterParams)
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v", ts)
	}
	if c := confirmsOf(w); len(c) != 1 || c[0].TaskCredential != "" || c[0].ProxyToken != "" {
		t.Errorf("confirms %+v, want one with no credentials", c)
	}
	if d.mint != nil {
		t.Error("a dispatcher with no key can mint")
	}
}

// A pod that never reaches the confirm gets nothing: an accept that was lost
// (the offer timed out) and one that came late (the race lost to another
// pod) are each abandoned, and the only credentials minted are the
// confirmed pod's.
func TestAPodThatIsNotConfirmedGetsNoCredentials(t *testing.T) {
	for name, setup := range map[string]func(*fakeWorker){
		"lost accept": func(w *fakeWorker) { w.dropAccept = true },
		"late accept": func(w *fakeWorker) { w.acceptDelay = 1500 * time.Millisecond },
	} {
		t.Run(name, func(t *testing.T) {
			b := newBus(t, "tse-0", "tse-1")
			b.ensure("tse", 2, 0, "read")
			loser := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
			setup(loser)
			good := b.worker("tse", "read", "tse-1", answerAccept, 3*time.Second)
			ti := oneProfile("tse", 2, "tse-0", "tse-1")
			ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
			ti.pool.SetUID("tse", "read", "tse-1", "uid-1")
			kp, pub := signingKey(t)
			_, ml, _ := b.runKeyed(ti, kp, 0)

			b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "cap-ok", clusterParams)
			waitFor(t, 10*time.Second, "tse-1 confirmed", func() bool { return good.confirmCount() == 1 })
			waitFor(t, 5*time.Second, "tse-0's offer abandoned", func() bool { return loser.abandonCount() >= 1 })
			time.Sleep(time.Second) // past the late accept
			if loser.confirmCount() != 0 {
				t.Errorf("tse-0 was confirmed %d times", loser.confirmCount())
			}
			noCredentials(t, loser)
			if got := ml.pods(); !slices.Equal(got, []string{"tse-1", "tse-1"}) {
				t.Errorf("mints for %v, want only tse-1's two", got)
			}
			c := verifyAs(t, confirmsOf(good)[0].TaskCredential, pub, taskcred.AudienceNATS)
			if c.Pod != "tse-1" || c.PodUID != "uid-1" {
				t.Errorf("tse-1's credential names %s/%s", c.Pod, c.PodUID)
			}
			if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted || ts.from != "tse-1" {
				t.Errorf("terminal %+v, want tse-1's completed", ts)
			}
		})
	}
}

// After a lost confirm the task's lease expires and it fails; the pod that
// was confirmed is gone from the pool. The next task goes to another pod,
// whose credentials are fresh: they name the new pod, its UID and the new
// task, with new jtis, and the old pod is minted nothing more.
func TestTheNextOfferAfterAnExpiredLeaseMintsFreshCredentials(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1")
	b.ensure("tse", 2, 0, "read")
	old := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	old.dropConfirm = true
	fresh := b.worker("tse", "read", "tse-1", answerAccept, time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	ti.pool.SetUID("tse", "read", "tse-1", "uid-1")
	kp, pub := signingKey(t)
	_, ml, _ := b.runKeyed(ti, kp, 0)

	b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "cap-ok", clusterParams)
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateFailed || !hasReason(ts, ReasonLeaseExpired) {
		t.Fatalf("task-1 terminal %+v, want failed lease-expired", ts)
	}
	first := confirmsOf(old)
	if len(first) != 1 {
		t.Fatalf("tse-0 heard %d confirms", len(first))
	}
	oldIDs := map[string]bool{
		verifyAs(t, first[0].TaskCredential, pub, taskcred.AudienceNATS).ID:        true,
		verifyAs(t, first[0].ProxyToken, pub, taskcred.AudienceCredentialProxy).ID: true,
	}
	ti.pool.Set("tse", "read", "tse-1")

	b.submitWith("tse", "read", lib.SubmitKindTop, "task-2", "cap-ok", clusterParams)
	if ts := b.waitTerminal("task-2", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("task-2 terminal %+v", ts)
	}
	second := confirmsOf(fresh)
	if len(second) != 1 {
		t.Fatalf("tse-1 heard %d confirms", len(second))
	}
	for aud, token := range map[taskcred.Audience]string{taskcred.AudienceNATS: second[0].TaskCredential, taskcred.AudienceCredentialProxy: second[0].ProxyToken} {
		c := verifyAs(t, token, pub, aud)
		if c.Pod != "tse-1" || c.PodUID != "uid-1" || c.Task != "task-2" {
			t.Errorf("%s: the new credential names %s/%s for %s, want tse-1/uid-1 for task-2", aud, c.Pod, c.PodUID, c.Task)
		}
		if oldIDs[c.ID] {
			t.Errorf("%s: the new credential reuses an old jti %s", aud, c.ID)
		}
	}
	if len(confirmsOf(old)) != 1 {
		t.Errorf("tse-0 heard %d confirms, want only its first", len(confirmsOf(old)))
	}
	if got := ml.pods(); !slices.Equal(got, []string{"tse-0", "tse-0", "tse-1", "tse-1"}) {
		t.Errorf("mints for %v, want tse-0's two then tse-1's two", got)
	}
}

// A dispatcher stopped mid-offer wrote nothing and minted nothing; the next
// one re-offers the task to another pod, and only that pod is minted for.
func TestAReofferAfterARestartMintsForTheNewPodOnly(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1")
	b.ensure("tse", 2, 0, "read")
	first := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	first.dropAccept = true
	ti := oneProfile("tse", 2, "tse-0")
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	ti.pool.SetUID("tse", "read", "tse-1", "uid-1")
	ti.update(func(c *lib.DispatchConfig) { c.AcceptTimeoutSeconds = 5 })
	kp, pub := signingKey(t)
	_, ml1, stop1 := b.runKeyed(ti, kp, 0)
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "cap-ok", clusterParams)
	waitFor(t, 10*time.Second, "the offer", func() bool { return first.offerCount() == 1 })
	stop1()
	if ml1.count() != 0 {
		t.Fatalf("a dispatcher stopped mid-offer minted %d credentials", ml1.count())
	}

	second := b.worker("tse", "read", "tse-1", answerAccept, 500*time.Millisecond)
	ti.update(func(c *lib.DispatchConfig) { c.AcceptTimeoutSeconds = 1 })
	ti.pool.Set("tse", "read", "tse-1")
	_, ml2, _ := b.runKeyed(ti, kp, 0)
	if ts := b.waitTerminal("task-1", 15*time.Second); ts.state != lib.StateCompleted || ts.from != "tse-1" {
		t.Fatalf("terminal %+v, want tse-1's completed", ts)
	}
	noCredentials(t, first)
	if first.confirmCount() != 0 {
		t.Errorf("tse-0 heard %d confirms", first.confirmCount())
	}
	if got := ml2.pods(); !slices.Equal(got, []string{"tse-1", "tse-1"}) {
		t.Errorf("mints for %v, want only tse-1's two", got)
	}
	if c := verifyAs(t, confirmsOf(second)[0].TaskCredential, pub, taskcred.AudienceNATS); c.Pod != "tse-1" || c.PodUID != "uid-1" {
		t.Errorf("the re-offer's credential names %s/%s", c.Pod, c.PodUID)
	}
}

// Renewals don't re-mint: a task renewed many times was minted for once,
// at its confirm.
func TestRenewalsDoNotMint(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 4*time.Second) // renews every 1s
	ti := oneProfile("tse", 2, "tse-0")
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	kp, _ := signingKey(t)
	_, ml, _ := b.runKeyed(ti, kp, 0)

	b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "cap-ok", clusterParams)
	waitFor(t, 10*time.Second, "the confirm", func() bool { return w.confirmCount() == 1 })
	key := lib.ProfileLeaseKey("tse", "read", "tse-0", "task-1")
	confirmed := b.leases()[key].RenewedAt
	waitFor(t, 10*time.Second, "renewals", func() bool {
		rec, ok := b.leases()[key]
		return ok && rec.RenewedAt.Sub(confirmed) >= 2*time.Second
	})
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v", ts)
	}
	if ml.count() != 2 || w.confirmCount() != 1 {
		t.Errorf("%d mints, %d confirms after renewals; want the confirm's two and one", ml.count(), w.confirmCount())
	}
}

// A pod with no UID in the pool is not offered the task (its credentials
// could name no UID). A pod whose UID changes between the offer and the
// confirm (deleted and re-created under its name) is not confirmed: the
// lease is already recorded, so the task fails when it runs out, and no
// confirm ever goes out without credentials.
func TestAPodsUIDMustBeTheOneItHadAtTheOffer(t *testing.T) {
	t.Run("no UID", func(t *testing.T) {
		b := newBus(t, "tse-0")
		b.ensure("tse", 2, 0, "read")
		w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
		kp, _ := signingKey(t)
		_, ml, _ := b.runKeyed(oneProfile("tse", 2, "tse-0"), kp, 0)
		b.submit("tse", "read", lib.SubmitKindTop, "task-1")
		time.Sleep(2 * time.Second)
		if w.offerCount() != 0 || ml.count() != 0 {
			t.Errorf("a pod with no UID was offered %d times, %d mints", w.offerCount(), ml.count())
		}
	})
	t.Run("UID changed", func(t *testing.T) {
		b := newBus(t, "tse-0")
		b.ensure("tse", 2, 0, "read")
		w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
		w.acceptDelay = time.Second
		ti := oneProfile("tse", 2, "tse-0")
		ti.update(func(c *lib.DispatchConfig) { c.AcceptTimeoutSeconds = 4 })
		ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
		kp, _ := signingKey(t)
		_, ml, _ := b.runKeyed(ti, kp, 0)
		b.submit("tse", "read", lib.SubmitKindTop, "task-1")
		waitFor(t, 10*time.Second, "the offer", func() bool { return w.offerCount() == 1 })
		ti.pool.SetUID("tse", "read", "tse-0", "uid-0-recreated")
		ts := b.waitTerminal("task-1", 15*time.Second)
		if ts.state != lib.StateFailed || !hasReason(ts, ReasonLeaseExpired) {
			t.Errorf("terminal %+v, want failed lease-expired", ts)
		}
		if w.confirmCount() != 0 || ml.count() != 0 {
			t.Errorf("%d confirms, %d mints; want none", w.confirmCount(), ml.count())
		}
	})
}

// The verifier's permit is reused across a waiting submission's retries
// only for what it was given for. The stream keeps one submission per
// subject, so a newer one under the same task id replaces the one waiting
// over a cap: one naming another cluster, or carrying another capability, is
// checked again before anything is minted for it. Refused, it is refused
// (no confirm, no credential for the unchecked target, nor for the checked
// one); permitted, its proxy token carries the cluster just checked.
func TestARetryNamingAnotherClusterIsCheckedAgainBeforeAMint(t *testing.T) {
	const targetA = "project/proj-1/location/us-central1/cluster/prod-1"
	clusterB := map[string]string{"project": "proj-1", "location": "us-central1", "cluster": "other-9"}
	clusterC := map[string]string{"project": "proj-2", "location": "europe-west1", "cluster": "ok-3"}
	const targetB = "project/proj-1/location/us-central1/cluster/other-9"
	const targetC = "project/proj-2/location/europe-west1/cluster/ok-3"
	for name, tc := range map[string]struct {
		capKey   string
		params   map[string]string
		wantCall string
		refused  bool
		cluster  taskcred.Target
	}{
		"another cluster, refused":    {"cap-ok", clusterB, "cap-ok|tse|read|" + targetB, true, taskcred.Target{}},
		"another capability, refused": {"cap-bad", clusterParams, "cap-bad|tse|read|" + targetA, true, taskcred.Target{}},
		"another cluster, permitted":  {"cap-ok", clusterC, "cap-ok|tse|read|" + targetC, false, taskcred.Target{Project: "proj-2", Location: "europe-west1", Cluster: "ok-3"}},
	} {
		t.Run(name, func(t *testing.T) {
			b := newBus(t, "tse-0")
			b.ensure("tse", 2, 0, "read")
			w := b.worker("tse", "read", "tse-0", answerAccept, 3*time.Second)
			ti := oneProfile("tse", 1, "tse-0")
			ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
			kp, pub := signingKey(t)
			v := &fakeVerifier{refuse: map[string]bool{"cap-bad": true}, refuseTarget: map[capability.Scope]bool{targetB: true}}
			_, ml, _ := b.runKeyedWith(ti, kp, 0, v, nil)

			b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "cap-ok", clusterParams)
			waitFor(t, 10*time.Second, "task-1 confirmed", func() bool { return w.confirmCount() == 1 })
			// task-2 is admitted for cluster A and waits over the profile's
			// concurrency of 1 while task-1 runs.
			b.submitWith("tse", "read", lib.SubmitKindTop, "task-2", "cap-ok", clusterParams)
			waitFor(t, 10*time.Second, "task-2 admitted for cluster A", func() bool { return len(v.callList()) == 2 })
			b.submitWith("tse", "read", lib.SubmitKindTop, "task-2", tc.capKey, tc.params) // replaces it

			ts := b.waitTerminal("task-2", 15*time.Second)
			if !slices.Contains(v.callList(), tc.wantCall) {
				t.Errorf("verifier calls %v, want the replacement checked: %s", v.callList(), tc.wantCall)
			}
			if tc.refused {
				if ts.state != lib.StateRejected || !hasReason(ts, ReasonVerifierRefused) {
					t.Errorf("task-2 terminal %+v, want rejected %s", ts, ReasonVerifierRefused)
				}
				time.Sleep(500 * time.Millisecond)
				if n := w.confirmCount(); n != 1 {
					t.Errorf("%d confirms, want task-1's only", n)
				}
				if n := ml.count(); n != 2 {
					t.Errorf("%d mints, want task-1's two only", n)
				}
				b.waitTerminal("task-1", 10*time.Second) // its worker done before the bus goes
				return
			}
			if ts.state != lib.StateCompleted {
				t.Fatalf("task-2 terminal %+v, want completed", ts)
			}
			confirms := confirmsOf(w)
			if len(confirms) != 2 {
				t.Fatalf("%d confirms, want two", len(confirms))
			}
			c := verifyAs(t, confirms[1].ProxyToken, pub, taskcred.AudienceCredentialProxy)
			if c.Task != "task-2" || c.Target == nil || *c.Target != tc.cluster {
				t.Errorf("task-2's proxy token: task %s, target %+v; want the re-checked cluster %+v", c.Task, c.Target, tc.cluster)
			}
		})
	}
}

// A cluster target no credential could carry (a field with whitespace, a
// control byte, or over taskcred's length bound) is refused at admission as
// invalid parameters, before the verifier, an offer, a lease or a mint: the
// dispatcher's own read of the task parameters (lib.TaskParametersOf, a
// stricter rule than taskcred's target fields) refuses it, whatever the
// publisher checked, so the mint never meets it after a lease is written.
func TestATargetNoCredentialCouldCarryIsRefusedAtAdmission(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	kp, _ := signingKey(t)
	v := &fakeVerifier{}
	_, ml, _ := b.runKeyedWith(ti, kp, 0, v, nil)

	bad := map[string]map[string]string{
		"task-space": {"project": "proj 1", "location": "us-central1", "cluster": "prod-1"},
		"task-long":  {"project": "proj-1", "location": "us-central1", "cluster": strings.Repeat("c", 129)},
		"task-ctl":   {"project": "proj-1", "location": "us-central1\x7f", "cluster": "prod-1"},
		"task-empty": {"project": "proj-1", "location": "", "cluster": "prod-1"},
	}
	for id, params := range bad {
		b.submitRawParams("tse", "read", lib.SubmitKindTop, id, "cap-ok", params)
	}
	for id := range bad {
		if ts := b.waitTerminal(id, 10*time.Second); ts.state != lib.StateRejected || !hasReason(ts, ReasonParametersInvalid) {
			t.Errorf("%s: terminal %+v, want rejected %s", id, ts, ReasonParametersInvalid)
		}
	}
	if calls := v.callList(); len(calls) != 0 {
		t.Errorf("the verifier was asked about a malformed target: %v", calls)
	}
	if w.offerCount() != 0 || ml.count() != 0 || len(b.leases()) != 0 {
		t.Errorf("%d offers, %d mints, %d leases for malformed targets; want none", w.offerCount(), ml.count(), len(b.leases()))
	}
}

// submitRawParams submits a task whose taskParameters part is written as
// is, unchecked by the publisher-side lib.TaskParametersPart.
func (b *bus) submitRawParams(profile, tier, kind, taskID, capKey string, params map[string]string) {
	b.t.Helper()
	env := submissionEnv(b.t, profile, taskID, capKey, nil)
	var msg lib.Message
	if err := json.Unmarshal(env.Payload, &msg); err != nil {
		b.t.Fatal(err)
	}
	data, err := json.Marshal(map[string]any{"taskParameters": map[string]any{"parameters": params}})
	if err != nil {
		b.t.Fatal(err)
	}
	msg.Parts = append(msg.Parts, lib.Part{Kind: "data", Data: data})
	env.Payload, err = json.Marshal(msg)
	if err != nil {
		b.t.Fatal(err)
	}
	if err := b.gateway.Publish(b.ctx, lib.TaskSubmitSubject(profile, tier, kind, taskID), env); err != nil {
		b.t.Fatalf("submit %s: %v", taskID, err)
	}
}

// With a key, a confirm never goes out without credentials that verify:
// a mint that fails, and a credential that does not read back with the
// configured key's public half (signed by another key), each leave the
// recorded lease with no confirm, so the task fails lease-expired and the
// pod never starts it.
func TestACredentialThatCannotBeMintedOrReadBackSendsNoConfirm(t *testing.T) {
	other, _ := signingKey(t)
	for name, mint := range map[string]func(taskcred.Claims) (string, error){
		"the mint fails":        func(taskcred.Claims) (string, error) { return "", errors.New("signer unavailable") },
		"signed by another key": func(c taskcred.Claims) (string, error) { return taskcred.Mint(other, c) },
	} {
		t.Run(name, func(t *testing.T) {
			b := newBus(t, "tse-0")
			b.ensure("tse", 2, 0, "read")
			w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
			ti := oneProfile("tse", 2, "tse-0")
			ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
			kp, _ := signingKey(t)
			_, ml, _ := b.runKeyedWith(ti, kp, 0, &fakeVerifier{}, func(d *Dispatcher) { d.mint = mint })

			b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "cap-ok", clusterParams)
			waitFor(t, 10*time.Second, "the offer", func() bool { return w.offerCount() == 1 })
			ts := b.waitTerminal("task-1", 15*time.Second)
			if ts.state != lib.StateFailed || !hasReason(ts, ReasonLeaseExpired) {
				t.Errorf("terminal %+v, want failed lease-expired", ts)
			}
			if w.confirmCount() != 0 {
				t.Errorf("%d confirms, want none", w.confirmCount())
			}
			if ml.count() == 0 {
				t.Error("the mint was never asked: the test did not reach the confirm")
			}
			noCredentials(t, w)
		})
	}
}

// submitChange publishes a submission whose task parameters say the
// requester asked for a change (lib.TaskParameters.ChangeRequested).
func (b *bus) submitChange(profile, tier, kind, taskID string, params map[string]string) {
	b.t.Helper()
	env := submissionEnv(b.t, profile, taskID, "cap-ok", nil)
	var msg lib.Message
	if err := json.Unmarshal(env.Payload, &msg); err != nil {
		b.t.Fatal(err)
	}
	part, err := lib.TaskParametersPart(lib.TaskParameters{Parameters: params, ChangeRequested: true})
	if err != nil {
		b.t.Fatal(err)
	}
	msg.Parts = append(msg.Parts, part)
	payload, err := json.Marshal(msg)
	if err != nil {
		b.t.Fatal(err)
	}
	env.Payload = payload
	if err := b.gateway.Publish(b.ctx, lib.TaskSubmitSubject(profile, tier, kind, taskID), env); err != nil {
		b.t.Fatalf("submit %s: %v", taskID, err)
	}
}

// natsClaimKeys is the set of keys in a token's nats claim block, read from
// the raw payload: what the token carries, not what Verify reads out of it.
func natsClaimKeys(t *testing.T, token string) []string {
	t.Helper()
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		t.Fatalf("not a JWT: %d segments", len(parts))
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		t.Fatal(err)
	}
	var top struct {
		Nats map[string]json.RawMessage `json:"nats"`
	}
	if err := json.Unmarshal(raw, &top); err != nil {
		t.Fatal(err)
	}
	var keys []string
	for k := range top.Nats {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}

// A submission that asked for a change gets the vcs:proposal-create permit
// on its credential-proxy token, and nowhere else: the bus credential
// carries no permits claim.
func TestAChangeRequestPutsThePermitOnTheProxyTokenOnly(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	kp, pub := signingKey(t)
	b.runKeyed(ti, kp, 0)

	b.submitChange("tse", "read", lib.SubmitKindTop, "task-1", clusterParams)
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v", ts)
	}
	confirm := confirmsOf(w)[0]
	proxyC := verifyAs(t, confirm.ProxyToken, pub, taskcred.AudienceCredentialProxy)
	if !slices.Equal(proxyC.Permits, []taskcred.Permit{taskcred.PermitVCSProposalCreate}) {
		t.Errorf("the proxy token's permits = %v, want [%s]", proxyC.Permits, taskcred.PermitVCSProposalCreate)
	}
	if natsC := verifyAs(t, confirm.TaskCredential, pub, taskcred.AudienceNATS); natsC.Permits != nil {
		t.Errorf("the bus credential carries permits %v", natsC.Permits)
	}
	if keys := natsClaimKeys(t, confirm.TaskCredential); slices.Contains(keys, "permits") {
		t.Errorf("the bus credential's claims %v include permits", keys)
	}
}

// A submission that did not ask for a change gets a proxy token with no
// permits key at all, never an empty list: exactly the claim keys a token
// had before the claim existed, so a broker one image behind accepts it.
func TestNoChangeRequestMintsNoPermitsClaim(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	kp, pub := signingKey(t)
	b.runKeyed(ti, kp, 0)

	b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "cap-ok", clusterParams)
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v", ts)
	}
	confirm := confirmsOf(w)[0]
	want := []string{"pod", "pod_uid", "profile", "target", "task", "tier", "type", "version"}
	if got := natsClaimKeys(t, confirm.ProxyToken); !slices.Equal(got, want) {
		t.Errorf("the proxy token's nats claims = %v, want exactly %v", got, want)
	}
	if c := verifyAs(t, confirm.ProxyToken, pub, taskcred.AudienceCredentialProxy); c.Permits != nil {
		t.Errorf("the proxy token carries permits %v", c.Permits)
	}
}

// A change request on a task that names no cluster gets no proxy token, so
// no permit either: the credential proxy refuses such a task at its
// version-control routes (a known limit; the token needs a target).
func TestAChangeRequestWithNoClusterGetsNoProxyToken(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Second)
	ti := oneProfile("tse", 2, "tse-0")
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	kp, pub := signingKey(t)
	_, ml, _ := b.runKeyed(ti, kp, 0)

	b.submitChange("tse", "read", lib.SubmitKindTop, "task-1", map[string]string{})
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v", ts)
	}
	confirm := confirmsOf(w)[0]
	if confirm.ProxyToken != "" {
		t.Error("a task with no cluster got a proxy token")
	}
	if keys := natsClaimKeys(t, confirm.TaskCredential); slices.Contains(keys, "permits") {
		t.Errorf("the bus credential's claims %v include permits", keys)
	}
	verifyAs(t, confirm.TaskCredential, pub, taskcred.AudienceNATS)
	if ml.count() != 1 {
		t.Errorf("%d mints, want 1", ml.count())
	}
}

// A retry whose replacement submission turns ChangeRequested off gets no
// permit, even though the verifier's cached permit for the same capability
// and cluster still applies and admission does not ask the verifier again:
// whether a change is asked for is read from the delivery being admitted,
// never from what was cached with the permit.
func TestARetryThatDropsTheChangeRequestMintsNoPermit(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 3*time.Second)
	ti := oneProfile("tse", 1, "tse-0")
	ti.pool.SetUID("tse", "read", "tse-0", "uid-0")
	kp, pub := signingKey(t)
	v := &fakeVerifier{}
	b.runKeyedWith(ti, kp, 0, v, nil)

	b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "cap-ok", clusterParams)
	waitFor(t, 10*time.Second, "task-1 confirmed", func() bool { return w.confirmCount() == 1 })
	// task-2 asks for a change, is admitted, and waits over the profile's
	// concurrency of 1 while task-1 runs.
	b.submitChange("tse", "read", lib.SubmitKindTop, "task-2", clusterParams)
	waitFor(t, 10*time.Second, "task-2 admitted", func() bool { return len(v.callList()) == 2 })
	// Replaced by the same capability and cluster, no longer asking.
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-2", "cap-ok", clusterParams)

	if ts := b.waitTerminal("task-2", 15*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("task-2 terminal %+v, want completed", ts)
	}
	if calls := v.callList(); len(calls) != 2 {
		t.Fatalf("verifier calls %v; want two, so the replacement was admitted on the cached permit", calls)
	}
	confirms := confirmsOf(w)
	if len(confirms) != 2 {
		t.Fatalf("%d confirms, want two", len(confirms))
	}
	c := verifyAs(t, confirms[1].ProxyToken, pub, taskcred.AudienceCredentialProxy)
	if c.Task != "task-2" {
		t.Fatalf("second confirm is for %s", c.Task)
	}
	if c.Permits != nil {
		t.Errorf("task-2's proxy token carries permits %v after its replacement dropped the change request", c.Permits)
	}
	if keys := natsClaimKeys(t, confirms[1].ProxyToken); slices.Contains(keys, "permits") {
		t.Errorf("task-2's proxy token claims %v include permits", keys)
	}
}
