package capability

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
)

// The attacker, written before the verifier. 09 §9 opens by warning that a
// denial test can pass against a server where nothing is configured at all, and
// the same trap has a library shape: a chain walk that only ever sees
// well-formed capabilities has not been tested. Every test below is a thing an
// attacker does, and each names the single rule that has to catch it.
//
// Three of 09 §9's checks are not here and cannot be: "no broker writes in
// another's namespace", "no broker reads the store", and "the permissions are
// actually configured" are properties of the rendered nats.conf and the running
// server, not of this package. They live in the operator's conformance suite,
// and they are the ones that must be written in subject space.

// fakeStore models the `cap` bucket at its provisioned history of 1: one
// retrievable revision per key, and a bucket-wide revision counter, because a
// KV revision is the underlying stream's sequence rather than a per-key
// version. Both of those matter to the tests below — history 1 is why an
// overwrite makes the old pin dangle, and a bucket-wide counter is why an
// entry's first write carries whatever number the bucket had reached and there
// is nothing for a verifier holding one entry to compare against.
type fakeStore struct {
	mu   sync.Mutex
	seq  uint64
	vals map[string]storedEntry
	gets int
}

type storedEntry struct {
	rev uint64
	val []byte
}

func newStore() *fakeStore { return &fakeStore{vals: map[string]storedEntry{}, seq: 400} }

// write is any principal writing a key. It deliberately does NOT check who is
// allowed to write where: that is the server's job in production, and a test
// harness that enforced it could not express the attacks where a compromised
// broker writes something the server would have refused. Where the server is
// the control, the conformance suite is the test.
func (f *fakeStore) write(t *testing.T, key string, e Entry) Ref {
	t.Helper()
	b, err := json.Marshal(e)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seq++
	f.vals[key] = storedEntry{rev: f.seq, val: b}
	return Ref{Key: key, Revision: f.seq}
}

func (f *fakeStore) del(key string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.vals, key)
}

func (f *fakeStore) GetRevision(_ context.Context, key string, revision uint64) ([]byte, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.gets++
	got, ok := f.vals[key]
	if !ok || got.rev != revision {
		return nil, ErrNotFound
	}
	return got.val, nil
}

const (
	gwTask  = "task-0000000000000000000000000000aa"
	podA    = "chat-aaaa11"
	podB    = "chat-bbbb22"
	podEvil = "chat-evil99"
)

func rootKey(t *testing.T, id string) string {
	t.Helper()
	k, err := RootKey(id)
	if err != nil {
		t.Fatalf("RootKey: %v", err)
	}
	return k
}

func hopKey(t *testing.T, p string, n int) string {
	t.Helper()
	k, err := HopKey(p, n)
	if err != nil {
		t.Fatalf("HopKey: %v", err)
	}
	return k
}

func resolver(s Store) *Resolver { return &Resolver{Store: s} }

// mustRefuse asserts a refusal and asserts what a refusal is allowed to say.
// A verifier's refusal is answered to the party that provoked it, so it names
// the rule that fired and never a value the caller supplied: echoing a key or a
// principal back turns the verifier into a confirmation oracle for ids it just
// refused to resolve.
func mustRefuse(t *testing.T, err error, wantRule string, tainted ...string) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected a refusal, got none")
	}
	// A refusal and a broken store are both denials, but they are not the
	// same thing: Answer converges one into a verdict and escalates the
	// other to the operator. A test that only matched the text would not
	// notice a rule turning into an infrastructure error.
	if !errors.Is(err, ErrRefused) {
		t.Fatalf("expected a refusal, got a non-refusal error: %v", err)
	}
	if !strings.Contains(err.Error(), wantRule) {
		t.Fatalf("refusal does not name the rule that fired:\n  got:  %s\n  want: %s", err, wantRule)
	}
	for _, v := range tainted {
		if v != "" && strings.Contains(err.Error(), v) {
			t.Fatalf("refusal echoes a caller-supplied value %q: %s", v, err)
		}
	}
}

// --- the well-formed chain the attacks are variations on ------------------

// twoHop builds gateway → podA → podB and returns the store and both refs.
func twoHop(t *testing.T) (*fakeStore, Ref, Ref) {
	t.Helper()
	s := newStore()
	root := s.write(t, rootKey(t, gwTask), Entry{
		Tier: TierPlatform, Scope: "project/P", Delegate: podA,
	})
	hop := s.write(t, hopKey(t, podA, 1), Entry{
		Tier: TierClusterAdmin, Scope: "project/P/cluster/C", Delegate: podB, Parent: &root,
	})
	return s, root, hop
}

func TestTheWellFormedChainResolves(t *testing.T) {
	s, root, hop := twoHop(t)
	// Control. Without it every refusal below could be the walk refusing
	// everything, which is the failure mode a denial suite cannot see.
	if _, err := resolver(s).Resolve(context.Background(), podA, root); err != nil {
		t.Fatalf("root should resolve for the delegate it names: %v", err)
	}
	leaf, err := resolver(s).Resolve(context.Background(), podB, hop)
	if err != nil {
		t.Fatalf("hop should resolve for the delegate it names: %v", err)
	}
	if leaf.Tier != TierClusterAdmin || leaf.Scope != "project/P/cluster/C" {
		t.Fatalf("resolved the wrong leaf: %+v", leaf)
	}
}

// --- 1. descent without delegation ----------------------------------------

func TestABrokerCannotDescendFromARootThatNamesSomebodyElse(t *testing.T) {
	s := newStore()
	// The root is minted for podA. Request ids are not secrets, so podEvil
	// can name it; the question is whether naming it is enough.
	root := s.write(t, rootKey(t, gwTask), Entry{
		Tier: TierPlatform, Scope: "project/P", Delegate: podA,
	})
	forged := s.write(t, hopKey(t, podEvil, 1), Entry{
		Tier: TierPlatform, Scope: "project/P", Delegate: podEvil, Parent: &root,
	})
	// Every other check passes: the root is under `root.*`, the child
	// narrows (by equality), and the key's prefix correctly proves podEvil
	// wrote it. Only "the parent names its delegate" catches this.
	_, err := resolver(s).Resolve(context.Background(), podEvil, forged)
	mustRefuse(t, err, "was not written by the principal its parent named as delegate", podEvil, gwTask)
}

// --- 2. resolving an id you were never handed -----------------------------

func TestABrokerCannotResolveARootItWasNeverHanded(t *testing.T) {
	s := newStore()
	root := s.write(t, rootKey(t, gwTask), Entry{
		Tier: TierPlatform, Scope: "project/P", Delegate: podA,
	})
	// No forged write at all: podEvil simply puts the id on its outbound
	// message. This is the cheaper escalation and it is on the read path.
	// Every chain check passes VACUOUSLY — a single-entry chain widens
	// nothing, and a root has no parent whose delegate could be violated.
	// Only "the verifier authenticates its caller" catches it.
	_, err := resolver(s).Resolve(context.Background(), podEvil, root)
	mustRefuse(t, err, "does not name the caller as its delegate", podEvil, gwTask)
}

// --- 3. widening ----------------------------------------------------------

func TestAHopCannotWidenTheTier(t *testing.T) {
	s := newStore()
	root := s.write(t, rootKey(t, gwTask), Entry{
		Tier: TierClusterAdmin, Scope: "project/P", Delegate: podA,
	})
	wide := s.write(t, hopKey(t, podA, 1), Entry{
		Tier: TierPlatform, Scope: "project/P", Delegate: podB, Parent: &root,
	})
	_, err := resolver(s).Resolve(context.Background(), podB, wide)
	mustRefuse(t, err, "widens the tier")
}

func TestAHopCannotWidenTheScope(t *testing.T) {
	s := newStore()
	root := s.write(t, rootKey(t, gwTask), Entry{
		Tier: TierPlatform, Scope: "project/P/cluster/C", Delegate: podA,
	})
	wide := s.write(t, hopKey(t, podA, 1), Entry{
		Tier: TierPlatform, Scope: "project/P", Delegate: podB, Parent: &root,
	})
	_, err := resolver(s).Resolve(context.Background(), podB, wide)
	mustRefuse(t, err, "widens the scope")
}

func TestASiblingScopeIsNotContainedByIt(t *testing.T) {
	// project/P/cluster/CC is not inside project/P/cluster/C, and a
	// containment written as a string prefix rather than a segment prefix
	// would say it is. That bug widens silently and only against clusters
	// whose names extend another's.
	if Scope("project/P/cluster/C").Contains("project/P/cluster/CC") {
		t.Fatal("containment is a string prefix, not a segment prefix")
	}
}

// --- 4. a concurrent capability belonging to another request --------------

func TestAPrincipalCannotPresentAConcurrentRequestsCapability(t *testing.T) {
	// This is the one 09 §4 says stops separating anything when the
	// identity is an agent id: one agent is the named delegate of every
	// request routed through it, at the same time, so it holds a
	// legitimate credential AND is legitimately named by the wider entry.
	// It only fails if the delegate field holds a per-request principal.
	s := newStore()
	narrow := s.write(t, rootKey(t, "task-0000000000000000000000000000bb"), Entry{
		Tier: TierDeveloperTeam, Scope: "project/P/cluster/C/ns/N", Delegate: podA,
	})
	wide := s.write(t, rootKey(t, "task-0000000000000000000000000000cc"), Entry{
		Tier: TierPlatform, Scope: "project/P", Delegate: podB,
	})
	if _, err := resolver(s).Resolve(context.Background(), podA, narrow); err != nil {
		t.Fatalf("its own capability should resolve: %v", err)
	}
	// podA, serving the narrow request, presents the concurrent wide one.
	_, err := resolver(s).Resolve(context.Background(), podA, wide)
	mustRefuse(t, err, "does not name the caller as its delegate")
}

// --- 5. a cyclic chain, refused and refused quickly -----------------------

func TestPinningMakesACycleUnconstructible(t *testing.T) {
	// 09 §5 treats the cycle as the reason the walk is bounded: one broker,
	// two entries in its own namespace naming each other as parent, each
	// naming itself as delegate, identical payloads. Every other rule holds
	// and the walk never reaches a terminal.
	//
	// It cannot be built. Closing the loop means rewriting the entry the
	// second one already pinned, and the rewrite moves that entry to a new
	// revision, so the pin dangles before the cycle exists. §4's revision
	// pinning — introduced in the same document — already forecloses the
	// hazard §5 raises. The bound and the visited set stay anyway, for the
	// reason the next test gives; what is NOT true is that they are what
	// stands between us and a hung verifier.
	s := newStore()
	e := Entry{Tier: TierDeveloperTeam, Scope: "project/P", Delegate: podEvil}
	one := s.write(t, hopKey(t, podEvil, 1), e)
	two := e
	two.Parent = &one
	twoRef := s.write(t, hopKey(t, podEvil, 2), two)

	closing := e
	closing.Parent = &twoRef
	s.write(t, hopKey(t, podEvil, 1), closing) // the rewrite that would close the loop

	_, err := resolver(s).Resolve(context.Background(), podEvil, twoRef)
	mustRefuse(t, err, "pinned revision")
	if s.gets > 2 {
		t.Fatalf("the walk did %d reads to notice a dangling pin", s.gets)
	}
}

// unpinnedStore serves whatever key is asked for at whatever revision, which is
// what a Store written against KeyValue.Get instead of GetRevision behaves
// like. It exists to keep the visited set load-bearing under the one bug that
// would otherwise reopen 09 §5's cycle.
type unpinnedStore struct{ inner *fakeStore }

func (u unpinnedStore) GetRevision(_ context.Context, key string, _ uint64) ([]byte, error) {
	u.inner.mu.Lock()
	defer u.inner.mu.Unlock()
	u.inner.gets++
	got, ok := u.inner.vals[key]
	if !ok {
		return nil, ErrNotFound
	}
	return got.val, nil
}

func TestTheVisitedSetStopsACycleIfAPinEverStopsHolding(t *testing.T) {
	s := newStore()
	e := Entry{Tier: TierDeveloperTeam, Scope: "project/P", Delegate: podEvil}
	one := s.write(t, hopKey(t, podEvil, 1), e)
	two := e
	two.Parent = &one
	twoRef := s.write(t, hopKey(t, podEvil, 2), two)
	closing := e
	closing.Parent = &twoRef
	s.write(t, hopKey(t, podEvil, 1), closing)

	_, err := (&Resolver{Store: unpinnedStore{inner: s}}).Resolve(context.Background(), podEvil, twoRef)
	mustRefuse(t, err, "revisits a key")
	// Refused quickly is half the requirement: the verifier is a single
	// service on the request path, so a walk that terminates only by
	// timeout is still a fleet-wide outage from one buggy hop.
	if s.gets > MaxDepth+1 {
		t.Fatalf("the walk did %d reads before refusing; the bound is %d", s.gets, MaxDepth+1)
	}
}

// --- 6. an over-deep chain ------------------------------------------------

func TestAChainDeeperThanTheBoundIsRefused(t *testing.T) {
	s := newStore()
	prev := s.write(t, rootKey(t, gwTask), Entry{
		Tier: TierDeveloperTeam, Scope: "project/P", Delegate: podA,
	})
	// Each link is written by the principal the previous named, narrows by
	// equality, and names the next. Entirely well formed, just too long.
	for i := 1; i <= MaxDepth+2; i++ {
		p := prev
		prev = s.write(t, hopKey(t, podA, i), Entry{
			Tier: TierDeveloperTeam, Scope: "project/P", Delegate: podA, Parent: &p,
		})
	}
	_, err := resolver(s).Resolve(context.Background(), podA, prev)
	mustRefuse(t, err, "chain is deeper than the bound")
}

// --- 7 & 8. a rewritten entry, both ways ----------------------------------

func TestAnEntryOverwrittenAfterItWasResolvedNoLongerResolves(t *testing.T) {
	s, root, hop := twoHop(t)
	if _, err := resolver(s).Resolve(context.Background(), podB, hop); err != nil {
		t.Fatalf("precondition: %v", err)
	}
	// The gateway's own key, rewritten wider. In production this is the
	// gateway compromised, or a bug; the permission that lets it create the
	// root is the same one that lets it update it, and no subject
	// permission can express "create but do not update".
	s.write(t, root.Key, Entry{Tier: TierPlatform, Scope: "project/", Delegate: podA})
	_, err := resolver(s).Resolve(context.Background(), podB, hop)
	mustRefuse(t, err, "pinned revision", root.Key)
}

func TestAnEntryDeletedAndRecreatedNoLongerResolves(t *testing.T) {
	s, root, hop := twoHop(t)
	if _, err := resolver(s).Resolve(context.Background(), podB, hop); err != nil {
		t.Fatalf("precondition: %v", err)
	}
	// Worse for the attacker, not better: the recreated entry lands at a
	// fresh sequence, so every pin to the old one dangles.
	s.del(root.Key)
	recreated := s.write(t, root.Key, Entry{Tier: TierPlatform, Scope: "project/P", Delegate: podA})
	if recreated.Revision == root.Revision {
		t.Fatal("the fake store recycles revisions; the real bucket does not")
	}
	_, err := resolver(s).Resolve(context.Background(), podB, hop)
	mustRefuse(t, err, "pinned revision")
}

func TestAReferenceThatIsNotPinnedIsRefused(t *testing.T) {
	// Revision zero is what an unpinned reference looks like after a round
	// trip through JSON, and it is the one value the KV API reads as
	// "latest". A verifier that passed it through would fetch the latest
	// value and undo every pin in the chain at once.
	s, root, _ := twoHop(t)
	_, err := resolver(s).Resolve(context.Background(), podA, Ref{Key: root.Key})
	mustRefuse(t, err, "not pinned to a revision")

	unpinned := Entry{Tier: TierPlatform, Scope: "project/P", Delegate: podB,
		Parent: &Ref{Key: root.Key}}
	ref := s.write(t, hopKey(t, podA, 9), unpinned)
	_, err = resolver(s).Resolve(context.Background(), podB, ref)
	mustRefuse(t, err, "not pinned to a revision")
}

// --- 9 & 10. the root namespace -------------------------------------------

func TestAHopEntryWithNoParentIsNotARoot(t *testing.T) {
	// An orphan: a broker writes a terminal entry in its own namespace and
	// presents it. Only "the chain terminates under `root.*`" catches it,
	// and without that check a broker mints its own authority from nothing.
	s := newStore()
	orphan := s.write(t, hopKey(t, podEvil, 1), Entry{
		Tier: TierPlatform, Scope: "project/P", Delegate: podEvil,
	})
	_, err := resolver(s).Resolve(context.Background(), podEvil, orphan)
	mustRefuse(t, err, "chain does not terminate at a root the gateway minted")
}

func TestARootWithAParentIsRefused(t *testing.T) {
	// The mirror: an entry in the gateway's namespace that claims to
	// descend from something. Only the gateway can write here, so this is
	// the gateway misbehaving — but a walk that followed the pointer would
	// let a root be laundered under a wider parent.
	s := newStore()
	base := s.write(t, hopKey(t, podEvil, 1), Entry{
		Tier: TierPlatform, Scope: "project/P", Delegate: podA,
	})
	odd := s.write(t, rootKey(t, gwTask), Entry{
		Tier: TierPlatform, Scope: "project/P", Delegate: podA, Parent: &base,
	})
	_, err := resolver(s).Resolve(context.Background(), podA, odd)
	mustRefuse(t, err, "root carries a parent")
}

func TestAKeyInNeitherNamespaceIsRefused(t *testing.T) {
	s := newStore()
	ref := s.write(t, "cap.somewhere.else", Entry{
		Tier: TierPlatform, Scope: "project/P", Delegate: podA,
	})
	_, err := resolver(s).Resolve(context.Background(), podA, ref)
	mustRefuse(t, err, "neither the root nor the hop namespace")
}

// --- 14. revocation is immediate ------------------------------------------

func TestDeletingAnAncestorRevokesEveryDescendantImmediately(t *testing.T) {
	s, root, hop := twoHop(t)
	if _, err := resolver(s).Resolve(context.Background(), podB, hop); err != nil {
		t.Fatalf("precondition: %v", err)
	}
	s.del(root.Key)
	// The leaf is untouched and still resolves on its own terms; what makes
	// revocation work is that the walk does not stop at the leaf. An
	// implementation that cached the leaf's verdict would serve a stale
	// answer here, which is why 09 §5 says leave it uncached.
	_, err := resolver(s).Resolve(context.Background(), podB, hop)
	mustRefuse(t, err, "pinned revision")
}

// --- the verb question ----------------------------------------------------

func TestAVerbAboveTheCapabilitysTierIsRefused(t *testing.T) {
	e := Entry{Tier: TierDeveloperTeam, Scope: "project/P/cluster/C/ns/N", Delegate: podA}
	if err := Permits(e, VerbTaskExecute, "project/P/cluster/C/ns/N"); err != nil {
		t.Fatalf("control: its own tier's verb should pass: %v", err)
	}
	mustRefuse(t, Permits(e, VerbClusterMutate, "project/P/cluster/C"),
		"below the tier the verb requires")
}

func TestAResourceOutsideTheScopeIsRefused(t *testing.T) {
	e := Entry{Tier: TierDeveloperTeam, Scope: "project/P/cluster/C/ns/N", Delegate: podA}
	mustRefuse(t, Permits(e, VerbTaskExecute, "project/P/cluster/C/ns/OTHER"),
		"outside the capability's scope")
}

// The placeholder NamespaceScope("") returns is a mismatch marker, not a
// deny-all, and the comment on it used to claim the second. Both facts are
// pinned here because the false one reads like a fail-closed guarantee for
// the unconfigured case, and the unconfigured case is fail-open between two
// components that defaulted together.
func TestTheNamespacePlaceholderIsAMismatchMarkerNotADenyAll(t *testing.T) {
	const placeholder = Scope("namespace/-")
	if got := NamespaceScope(""); got != placeholder {
		t.Fatalf("precondition: NamespaceScope(%q) = %q, want %q", "", got, placeholder)
	}

	// Both directions of the mismatch, which is what an unrendered half of
	// the pair actually runs into.
	mustRefuse(t, Permits(Entry{Tier: TierDeveloperTeam, Scope: placeholder, Delegate: podA},
		VerbTaskExecute, NamespaceScope("kubeagents-system")),
		"outside the capability's scope")
	mustRefuse(t, Permits(Entry{Tier: TierDeveloperTeam, Scope: NamespaceScope("kubeagents-system"), Delegate: podA},
		VerbTaskExecute, placeholder),
		"outside the capability's scope")

	// And the tautology. Contains returns true on equality before it looks
	// at segments, so a mint and a check that both defaulted agree — this
	// is not a refusal and the doc comment must not say it is.
	if err := Permits(Entry{Tier: TierDeveloperTeam, Scope: placeholder, Delegate: podA},
		VerbTaskExecute, placeholder); err != nil {
		t.Fatalf("the placeholder does not permit itself, so the comment correction is wrong: %v", err)
	}
}

func TestAnUnknownVerbIsRefusedRatherThanAllowed(t *testing.T) {
	e := Entry{Tier: TierPlatform, Scope: "project/P", Delegate: podA}
	mustRefuse(t, Permits(e, Verb("cluster.delete-everything"), "project/P"),
		"verb is not in the table")
}

func TestAnUnknownTierIsRefusedRatherThanRanked(t *testing.T) {
	// The subtle one: an unknown tier compares equal to itself, so a
	// narrowing check that ranked by string would pass `tier: root` under
	// `tier: root` and a Permits that defaulted to zero would rank it below
	// everything — refused by accident today, allowed by accident the first
	// time somebody reorders the constants.
	e := Entry{Tier: Tier("root"), Scope: "project/P", Delegate: podA}
	if err := e.Validate(); err == nil {
		t.Fatal("an unknown tier validated")
	}
	mustRefuse(t, Narrows(Entry{Tier: Tier("root"), Scope: "project/P"}, e), "not in the lattice")
	mustRefuse(t, Permits(e, VerbTaskExecute, "project/P"), "not in the lattice")
}

// --- the store's contract -------------------------------------------------

func TestTheWalkFetchesEveryLinkAtThePinnedRevisionAndNeverTheLatest(t *testing.T) {
	// The one property a mock cannot be relied on to catch by accident: a
	// verifier written against KeyValue.Get instead of GetRevision passes
	// every test above except this one, because in a fake with a single
	// revision per key the two agree. Raise history above one — which is a
	// bucket setting an operator can change without touching this code —
	// and they diverge, with the pin resolving to the intended content and
	// the latest value being whatever was written last.
	s := newStore()
	root := s.write(t, rootKey(t, gwTask), Entry{
		Tier: TierDeveloperTeam, Scope: "project/P", Delegate: podA,
	})
	asked := map[uint64]bool{}
	probe := recordingStore{inner: s, seen: asked}
	if _, err := (&Resolver{Store: probe}).Resolve(context.Background(), podA, root); err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if asked[0] {
		t.Fatal("the walk asked for revision 0, which the KV API reads as latest")
	}
	if !asked[root.Revision] {
		t.Fatalf("the walk did not ask for the pinned revision %d", root.Revision)
	}
}

type recordingStore struct {
	inner Store
	seen  map[uint64]bool
}

func (r recordingStore) GetRevision(ctx context.Context, key string, rev uint64) ([]byte, error) {
	r.seen[rev] = true
	return r.inner.GetRevision(ctx, key, rev)
}

func TestAMissingEntryAndARefusedEntryAreNotDistinguishable(t *testing.T) {
	// Request ids are not secrets, but the verifier must not become the
	// thing that confirms which ones exist. The Resolver's own errors name
	// the rule — they are for the operator and the tests — so the
	// convergence is at the wire boundary, in Service.Answer, and this test
	// asserts it there.
	s := newStore()
	root := s.write(t, rootKey(t, gwTask), Entry{
		Tier: TierPlatform, Scope: "project/P", Delegate: podA,
	})
	svc := &Service{Resolver: resolver(s)}
	ctx := context.Background()
	subject, err := VerifySubject(podEvil)
	if err != nil {
		t.Fatal(err)
	}
	ask := func(ref Ref) Response {
		b, err := json.Marshal(Request{Ref: ref, Verb: VerbTaskExecute, Resource: "project/P"})
		if err != nil {
			t.Fatal(err)
		}
		return svc.Answer(ctx, subject, b)
	}
	missing := ask(Ref{Key: rootKey(t, "task-000000000000000000000000000fff"), Revision: 999})
	present := ask(root)
	if missing.Allowed || present.Allowed {
		t.Fatalf("both should refuse: %+v / %+v", missing, present)
	}
	if missing.Reason != present.Reason {
		t.Fatalf("the verifier says whether the key existed:\n  missing %q\n  present %q", missing.Reason, present.Reason)
	}
	if missing.Reason != WalkRefused {
		t.Fatalf("reason = %q, want the one converged walk refusal", missing.Reason)
	}
}
