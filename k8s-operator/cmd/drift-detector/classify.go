// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"fmt"
	"sort"
	"strings"
	"time"
)

const (
	// systemPrincipalPrefix marks a Kubernetes control-plane identity:
	// "system:kube-controller-manager", "system:serviceaccount:ns:name",
	// "system:gke-spiffe-controller". This is the single largest noise source
	// -- measured at 95% or more of the mutating calls that survive lease
	// filtering, on every project sampled.
	//
	// Known gap, deliberate for T2: this swallows the whole
	// "system:serviceaccount:" namespace, so a person acting through a
	// ServiceAccount token -- a CI account's token used by hand, or one
	// mounted in a debug pod -- is classified system and never reported. The
	// audit log cannot distinguish that from the controller that normally
	// holds the token, because the principal is the same string either way;
	// separating them needs the user agent or the delegation chain, neither of
	// which T2 classifies on. It is one of two drops that leave no trace in the
	// shutdown report -- the other being a person behind an impersonated GCP
	// service account, which lands in TierAutomation -- because unlike
	// TierUnattributed neither tier records which principals it saw. Running
	// with --log-dropped does print them, but that is a deliberate shift, not
	// something an operator stumbles on. Narrowing the prefix to known
	// control-plane identities would close it at the cost of reclassifying
	// most of the fleet's traffic, which is a change to make against measured
	// data rather than alongside this one.
	systemPrincipalPrefix = "system:"

	// gcpServiceAccountSuffix marks a Google Cloud service account, which is
	// how CI and GitOps controllers authenticate to a GKE cluster from
	// outside it. Workload Identity presents the GCP identity, so a pipeline
	// running in-cluster lands here rather than under systemPrincipalPrefix.
	//
	// The match is the whole ".gserviceaccount.com" domain and not the
	// ".iam." form, because the Google-managed service accounts do not carry
	// "iam": the Compute Engine default account is
	// "<number>-compute@developer.gserviceaccount.com", and Cloud Build,
	// App Engine and the cloudservices agent use "@cloudbuild.",
	// "@appspot." and "@cloudservices." respectively. Matching only ".iam."
	// sends every one of them to the positive human test, which they pass on
	// the strength of having an "@" -- so a Cloud Build pipeline deploying to
	// a cluster, the precise thing the automation tier exists for, would be
	// reported as a person making an out-of-band change. The leading dot is
	// what stops the suffix matching a lookalike domain such as
	// "evilgserviceaccount.com".
	gcpServiceAccountSuffix = ".gserviceaccount.com"

	// emailLocalDomainSeparator separates the local part of a principal from
	// its domain. Its presence is the positive test for a human: see
	// Classify's comment for why the test is positive rather than negative.
	emailLocalDomainSeparator = "@"

	// principalListSeparator splits the comma-separated flag values that carry
	// the automation allowlist and the human-domain list, matching the
	// event watcher's --reason and --namespace convention.
	principalListSeparator = ","

	// domainPrefix is prepended to a configured bare domain before matching,
	// so that --human-domains=example.com matches "ada@example.com" and not
	// "ada@notexample.com".
	domainPrefix = emailLocalDomainSeparator

	// domainLabelSeparator separates the labels of a DNS name, and is stripped
	// from the front of a configured --human-domains value.
	//
	// ".example.com" is the conventional way to write a domain elsewhere --
	// cookie scopes, TLS name constraints, no_proxy -- so it is what an
	// operator reaches for, and without this it is stored as "@.example.com"
	// and matches nothing, ever, with no error: every person in the fleet is
	// filed as unattributed while the detector reports healthy. Stripping it
	// makes ".example.com" behave exactly as "example.com" does. It does not
	// make either one match subdomains; isHuman says why not.
	domainLabelSeparator = "."

	// unauthenticatedPrincipalLabel stands in for the empty principal in the
	// unattributed list. The empty string renders as "=120" there, which names
	// nothing an operator could act on, and unlike every other entry in that
	// list it is not waiting for a rule to be written: an unauthenticated
	// request has no identity to classify. Naming it says which of the two it
	// is.
	unauthenticatedPrincipalLabel = "(unauthenticated)"

	// tierCountsExtraFields is the number of fields String appends after the
	// per-tier counts: failed_calls, non_declarative and actionable.
	tierCountsExtraFields = 3

	// countsLogInterval is how many records pass through the filter between
	// progress lines. The tally is otherwise reported only on a graceful stop,
	// so a pod that is OOM-killed or SIGKILLed past its grace period takes the
	// whole measurement with it -- and this runs for weeks at a time.
	//
	// It counts delivered records, so it breaks the silence of a fleet with no
	// human changes but not the silence of a subscription delivering nothing at
	// all -- neither half of the interval below can fire on a stream that never
	// reaches Handle. A sink whose filter stopped matching produces no pull
	// error and no batch-skip line either, so that case is covered by
	// idleReportInterval in subscriber.Run and not here.
	countsLogInterval = 10000

	// countsLogMaxInterval bounds the same report in time, because a record
	// count alone does not deliver what countsLogInterval promises. The
	// post-sink stream measured 0.7 to 10 records a second, so 10000 records is
	// seventeen minutes on a busy project and close to four hours on a quiet
	// one -- the guarantee is weakest exactly where a SIGKILL destroys the most
	// unreported work.
	//
	// It also closes a silent window T1 did not have. T1 logged a line per
	// parsed record; T2 forwards only successful human writes, of which the
	// measured 15-minute windows contained none, so between the startup line
	// and the first progress line the pod log would otherwise be empty on a
	// cluster that is working perfectly. What it does not close is the window
	// where nothing is delivered at all: this fires from Handle, so a broken
	// sink never reaches it. subscriber.Run's idleReportInterval is that half,
	// and the two are deliberately the same length.
	//
	// Fifteen minutes is the measurement window the tier ratios were taken
	// over, and 96 lines a day is nothing next to the stream being counted.
	countsLogMaxInterval = 15 * time.Minute

	// maxUnattributedPrincipals caps the distinct-principal set. The measured
	// set is tiny -- two principals accounted for all 1230 unattributed calls
	// over 24 hours -- but the map is keyed by a caller-influenced string in a
	// process expected to run for weeks, so an unusual fleet or a controller
	// minting an identity per pod would grow it without bound. Past the cap new
	// principals are counted under unattributedOverflowLabel, which keeps the
	// total honest while the names stop accumulating.
	maxUnattributedPrincipals = 128

	// unattributedOverflowLabel stands in for every principal seen after the
	// cap. It is deliberately not silent: an operator reading it knows the
	// name list is incomplete, which a truncated list alone would not tell
	// them.
	// No comma in the text: the label is joined into a comma-separated list.
	unattributedOverflowLabel = "(other principals; name list capped)"
)

// Tier is what kind of actor made a change. The detector reports counts per
// tier rather than silently discarding: a bad allowlist is invisible without
// them, and the tier mix is the measurement CUJ 3 asks for.
type Tier string

const (
	// TierSystem is a Kubernetes control-plane identity. Never drift.
	TierSystem Tier = "system"

	// TierAutomation is CI, GitOps, or any other configured machine identity.
	// Its changes are intended, so they are not drift either -- but they are
	// counted, because an automation principal that stops appearing is how a
	// broken pipeline shows up here.
	TierAutomation Tier = "automation"

	// TierHuman is a person. This is the tier the rest of CUJ 3 acts on.
	TierHuman Tier = "human"

	// TierUnattributed is a principal no rule positively identified.
	//
	// This tier does not appear in the CUJ 3 breakdown, and adding it is the
	// main design change measurement forced. The specified classifier drops
	// "^system:", drops an allowlist, and calls whatever is left human. On 24
	// hours of live audit data across three projects that residual was 1230
	// calls, every one of them a machine and not one of them a person:
	//
	//   - "kubelet-nodepool-bootstrap" (1110 calls), a GKE node-bootstrap
	//     identity that is neither prefixed nor a service account;
	//   - an empty principal (120 calls), which is an unauthenticated request
	//     that the API server rejected with 401. Public GKE endpoints are
	//     crawled: Googlebot, Baiduspider, Amazonbot and others probing
	//     invented paths make up all of these.
	//
	// An allowlist cannot fix that, because it has to anticipate every
	// identity GKE invents. Routing the residual here instead means a
	// principal nobody has classified is surfaced for a human to look at
	// rather than being injected as though a person had made the change.
	TierUnattributed Tier = "unattributed"
)

// tierOrder is the order tiers are reported in, loudest-tier-last so the
// interesting counts end the line. Also the set Counts always emits: a tier
// with no traffic reports zero rather than being absent, because "no human
// changes today" and "the human rule stopped matching" have to look different.
var tierOrder = []Tier{TierSystem, TierAutomation, TierUnattributed, TierHuman}

// Classifier assigns a Tier to a principal. It holds no state, so it is safe
// to share.
//
// Its configuration is per detector instance, which is not the same as per
// cluster: the Log Router sink is project-wide, so one subscription carries
// every cluster in the project and one allowlist therefore applies to all of
// them. Narrowing that to a genuine per-cluster allowlist needs either a sink
// per cluster or a cluster-keyed config, and neither is decided yet.
type Classifier struct {
	// automation is the exact-match allowlist of machine principals. Exact
	// match rather than pattern: an operator editing this under an incident
	// should not be able to widen it accidentally, and every automation
	// identity seen in the measurement is a fixed string.
	automation map[string]struct{}

	// humanDomains tightens the human test to an explicit set of domains.
	// Empty means "any principal with a domain", which is the default and is
	// already sufficient to exclude every false positive measured.
	humanDomains []string
}

// NewClassifier builds a Classifier from the two configured lists. Both arrive
// comma-separated from the command line, so that the allowlist is deployment
// configuration and changes without a rebuild -- CUJ 3 T2's acceptance
// criterion. Blank entries and surrounding spaces are tolerated because of how
// these values will arrive once something launches this binary: nothing in
// deploy/ builds or starts the detector yet, and the intended path is the one
// EVENT_WATCHER_* already takes, where the PlatformAgent CR sets an environment
// variable and deploy/shared/start-services.sh templates it into a flag. A
// trailing comma from that templating should not become a principal that
// matches "".
func NewClassifier(automationPrincipals, humanDomains string) *Classifier {
	c := &Classifier{automation: map[string]struct{}{}}
	// Matched exactly, and deliberately not case-folded: a Kubernetes username
	// is case-sensitive by specification, so two principals differing only in
	// case are two principals and folding them would be wrong. The cost is that
	// a miscased entry here never matches -- it fails open, sending an
	// automation account to the human tier, where it is at least visible rather
	// than silently dropped.
	for _, p := range splitCSV(automationPrincipals) {
		c.automation[p] = struct{}{}
	}
	for _, d := range splitCSV(humanDomains) {
		// Lower-cased, unlike the principals above, because DNS is
		// case-insensitive and the principals these are matched against are
		// not: GCP normalises principalEmail to lower case. Without this,
		// --human-domains=Corp.Example.com classifies every person in the
		// fleet as unattributed and the detector reports human=0 while looking
		// perfectly healthy.
		//
		// Stored with the separator attached so the match is a suffix test
		// that cannot straddle the domain boundary.
		//
		// Both an "@" and a leading "." are stripped before it is reattached,
		// so "example.com", "@example.com" and ".example.com" are one domain
		// written three ways rather than three configurations, one of which
		// silently matches nothing. See domainLabelSeparator.
		domain := strings.ToLower(d)
		domain = strings.TrimPrefix(domain, domainPrefix)
		domain = strings.TrimPrefix(domain, domainLabelSeparator)
		c.humanDomains = append(c.humanDomains, domainPrefix+domain)
	}
	return c
}

// splitCSV parses one comma-separated flag value, dropping blanks. Named to
// match the identical helper in cmd/k8s-event-watcher, so that grepping for
// either finds both; they cannot be shared without an extraction into
// k8s-operator/internal/, both being package main.
func splitCSV(value string) []string {
	var out []string
	for _, part := range strings.Split(value, principalListSeparator) {
		if trimmed := strings.TrimSpace(part); trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}

// Classify returns the tier for a principal.
//
// Order matters, and the last two rules are the ones that differ from the
// breakdown. Human is a positive test -- the principal must look like an
// account belonging to a person -- rather than "whatever the drop rules did
// not catch". The negative form makes every identity Google adds in future a
// false human change, which is exactly what the measurement found; the
// positive form makes it an unattributed one, which is loud without being
// wrong. Both of the measured false-positive classes fail the positive test
// for the same simple reason: they carry no domain at all.
func (c *Classifier) Classify(principal string) Tier {
	// An empty principal is an unauthenticated request. The API server
	// rejected it, so the outcome filter would drop it anyway, but it is
	// classified honestly rather than being allowed to fall through the
	// domain test below.
	if principal == "" {
		return TierUnattributed
	}
	if strings.HasPrefix(principal, systemPrincipalPrefix) {
		return TierSystem
	}
	if _, ok := c.automation[principal]; ok {
		return TierAutomation
	}
	// Folded, for the reason isHuman folds --human-domains: this is a suffix
	// test against a DNS domain, and DNS is case-insensitive. Unfolded,
	// "deployer@proj.iam.GSERVICEACCOUNT.COM" misses here, carries an "@", and
	// passes the human test -- a service account reported as a person making an
	// out-of-band change, which is the false positive the automation tier
	// exists to prevent. The two rules above are not folded and should not be:
	// both match a Kubernetes username, which is case-sensitive by
	// specification, rather than a domain.
	if strings.HasSuffix(strings.ToLower(principal), gcpServiceAccountSuffix) {
		return TierAutomation
	}
	if c.isHuman(principal) {
		return TierHuman
	}
	return TierUnattributed
}

// isHuman applies the positive human test: a domain, and when the operator has
// configured a set, one of those domains.
func (c *Classifier) isHuman(principal string) bool {
	if len(c.humanDomains) > 0 {
		// Both sides are lower-cased, not just the configured domain.
		// NewClassifier folds the flag value because DNS is case-insensitive;
		// folding only there assumes the principal arrives lower-cased, which
		// holds for Google identities but not for a username minted by a
		// customer OIDC provider or read off a client-certificate CN, both of
		// which GKE forwards as given. Unfolded, Ada@Corp.Example.com misses
		// the suffix and a real human change is filed as unattributed.
		//
		// The match is the exact domain and not its subtree:
		// --human-domains=example.com does not match "ada@corp.example.com".
		// An organisation whose accounts live under subdomains lists them,
		// which the comma-separated flag is for. Widening this to a subtree
		// would be a one-line change and is deliberately not made: the flag is
		// what narrows the human tier, so every principal it newly admits is a
		// record the detector newly reports as somebody's drift, and there is
		// no measured fleet here whose accounts are spread across subdomains
		// to say the trade is worth making. The misses are not silent -- an
		// unmatched principal lands in TierUnattributed and is logged by name,
		// which is the list an operator reads to find the domain they left out.
		lowered := strings.ToLower(principal)
		for _, d := range c.humanDomains {
			if strings.HasSuffix(lowered, d) {
				return true
			}
		}
		return false
	}
	// A domain means an "@" with something on both sides of it. Testing only
	// for the character's presence admits "@", "ada@" and "@example.com" as
	// people; none of the three is an account, and the last two are the shapes
	// a truncated principal takes. LastIndex rather than Index because a quoted
	// local part may itself contain an "@", and the domain is whatever follows
	// the final one.
	at := strings.LastIndex(principal, emailLocalDomainSeparator)
	return at > 0 && at < len(principal)-1
}

// nonDeclarativeSubresources are subresources whose write verbs act on a
// running workload rather than on declarative state, so a call naming one is
// never drift however it is classified.
//
// They reach here because the audit methodName carries the verb as a suffix:
// `kubectl exec` is logged as io.k8s.core.v1.pods.exec.create, which contains
// "create" and so matches the sink's create|patch|update|delete filter. The
// resourceName names a real object (.../pods/<name>/exec), so the parser's
// "named no object" drop -- the one that already discards subject access
// reviews -- does not catch it either. Measured: pods.exec.create is present in
// the Admin Activity log on a live project. Without this, a person running
// kubectl exec is classified human, succeeds, and is reported as a change they
// did not make.
//
// The set is every subresource that reaches here by that route, not only the
// ones a live capture happened to show:
//
//	exec, attach, portforward, proxy   session subresources of a pod
//	ephemeralcontainers                `kubectl debug`, a PATCH on the pod
//	token                              `kubectl create token`, a TokenRequest
//
// ephemeralcontainers is the one that genuinely mutates the stored object, and
// it is still not drift: the Git-side object is the Deployment that owns the
// pod, which is unchanged, so there is nothing to revert or codify. Leaving it
// out would report `kubectl debug` -- an incident action, and so exactly the
// traffic CUJ 3 sees most of -- as an out-of-band change. token creates no
// persistent object at all.
//
// Excluded on purpose: `status` and `scale` are real declarative writes, and
// `eviction` can destroy a Git-side object where the six above cannot. The
// ownership argument that admits ephemeralcontainers does not carry over to
// it: evicting a Deployment-owned pod changes nothing in Git, but evicting a
// pod applied from a manifest of its own removes the very object Git declares.
// A subresource name cannot tell the two apart -- only the owner references on
// the live object can, which is T3's managedFields join. Until then an
// eviction is reported, so a `kubectl drain` produces a line per pod. That is
// the wrong trade to make blind in the other direction.
var nonDeclarativeSubresources = map[string]bool{
	"exec":                true,
	"attach":              true,
	"portforward":         true,
	"proxy":               true,
	"ephemeralcontainers": true,
	"token":               true,
}

// TierCounts is the per-tier tally, plus the records dropped before a tier
// could act on them.
type TierCounts struct {
	// ByTier counts every classified record, including the ones filtered out.
	// Classification happens before the outcome filter so that a cluster whose
	// human changes are all being rejected is visible as human traffic that
	// never becomes actionable, rather than as no human traffic.
	ByTier map[Tier]int

	// Failed counts records whose call did not succeed. See statusCodeOK: the
	// audit log records attempts, and an attempt that changed nothing is not
	// drift.
	Failed int

	// Actionable counts records passed downstream: successful human changes.
	Actionable int

	// NonDeclarative counts successful human calls dropped for naming a
	// subresource in nonDeclarativeSubresources. Counted rather than dropped
	// silently: these are real actions by real people, and an operator
	// wondering why their kubectl exec raised nothing should find a number
	// saying so instead of an absence.
	NonDeclarative int
}

// String renders the counts for the shutdown log in a fixed tier order.
func (t TierCounts) String() string {
	parts := make([]string, 0, len(tierOrder)+tierCountsExtraFields)
	for _, tier := range tierOrder {
		parts = append(parts, fmt.Sprintf("%s=%d", tier, t.ByTier[tier]))
	}
	parts = append(parts, fmt.Sprintf("failed_calls=%d", t.Failed))
	parts = append(parts, fmt.Sprintf("non_declarative=%d", t.NonDeclarative))
	parts = append(parts, fmt.Sprintf("actionable=%d", t.Actionable))
	return strings.Join(parts, " ")
}

// UnattributedPrincipals is the distinct set of principals that reached
// TierUnattributed, sorted. The counts say a rule is missing; this says which
// principal to write it for, which is the difference between a number an
// operator can act on and one they cannot.
type UnattributedPrincipals struct {
	seen map[string]int
}

// Add records one sighting. The empty principal is stored under a label rather
// than as itself: it is the second most common entry in this list on a live
// cluster, and an unnamed one is neither readable in the log line nor
// actionable.
func (u *UnattributedPrincipals) Add(principal string) {
	if u.seen == nil {
		u.seen = map[string]int{}
	}
	if principal == "" {
		principal = unauthenticatedPrincipalLabel
	}
	// A principal already being counted stays counted past the cap; only a new
	// name is folded into the overflow bucket, so the cap bounds the map
	// without distorting the counts of the principals it already names.
	if _, known := u.seen[principal]; !known && len(u.seen) >= maxUnattributedPrincipals {
		principal = unattributedOverflowLabel
	}
	u.seen[principal]++
}

// Sorted returns "principal=count" pairs, most frequent first.
func (u *UnattributedPrincipals) Sorted() []string {
	out := make([]string, 0, len(u.seen))
	for p := range u.seen {
		out = append(out, p)
	}
	sort.Slice(out, func(i, j int) bool {
		if u.seen[out[i]] != u.seen[out[j]] {
			return u.seen[out[i]] > u.seen[out[j]]
		}
		return out[i] < out[j]
	})
	for i, p := range out {
		// %q, matching every other site that prints a principal (logActionable
		// and logDroppedRecord both use it). These are the two lines that print
		// a principal no rule recognised, which is the population most likely
		// to hold something unusual: a username from a customer OIDC provider
		// is caller-influenced and Kubernetes does not forbid a newline in one.
		// Unquoted, such a name forges what reads as a separate log line.
		out[i] = fmt.Sprintf("%q=%d", p, u.seen[p])
	}
	return out
}

// driftFilter is T2's handler: it classifies each parsed record, counts it,
// and forwards only the ones that represent a real human change. It sits
// behind recordHandler, so the subscriber loop is unchanged.
//
// Not safe for concurrent use, and nothing guards that. Handle mutates
// handled, counts.ByTier and unattributed.seen without a lock, which is sound
// only because there is exactly one caller: subscriber.Run pulls, calls
// processBatch, and processBatch walks the batch in the same goroutine. The
// Classifier it holds is stateless and is safe to share; this is not.
//
// It is called out because the obvious next step makes it wrong. T3's
// managedFields join is a per-record API call against the cluster, which is
// the point at which handling a batch concurrently starts to look worthwhile
// -- and a concurrent Handle races two maps and a counter, producing a torn
// tally at best and a runtime map-write panic at worst. Whoever makes that
// change owns adding the mutex, or moving the counting behind a channel.
type driftFilter struct {
	classifier   *Classifier
	counts       TierCounts
	unattributed UnattributedPrincipals

	// handled counts every record seen, and is what countsLogInterval divides
	// to decide when to print progress. Distinct from the per-tier totals
	// because it advances even for a tier that is not being counted yet.
	handled int

	// next receives the records that survive. T3 joins managedFields here and
	// T4 injects; T2 ships logActionable.
	next recordHandler

	// logDropped reports every record the filter discards. Off by default:
	// the post-sink stream measured 1 to 10 records a second across three
	// projects and is ~98% system tier, so a line per drop is very nearly the
	// whole audit stream copied into the pod log. On for a shift when an
	// operator is working out why a change of theirs never arrived.
	logDropped bool

	// lastCountsLog is when the progress line last went out, for the time half
	// of the interval. Set at construction so the first line is due
	// countsLogMaxInterval after start rather than on the first record.
	lastCountsLog time.Time

	// now is the clock lastCountsLog is measured against. nil means time.Now;
	// a test sets it to drive the interval without sleeping for a quarter of
	// an hour.
	now func() time.Time
}

// clock reads the filter's injectable time source.
func (d *driftFilter) clock() time.Time {
	if d.now == nil {
		return time.Now()
	}
	return d.now()
}

// newDriftFilter wires a classifier to the handler that consumes what survives.
func newDriftFilter(classifier *Classifier, next recordHandler, logDropped bool) *driftFilter {
	return &driftFilter{
		classifier:    classifier,
		counts:        TierCounts{ByTier: map[Tier]int{}},
		next:          next,
		logDropped:    logDropped,
		lastCountsLog: time.Now(),
	}
}

// Handle classifies one record and forwards it if it is actionable drift.
//
// The progress line is emitted after tally has returned rather than on the way
// in, so that the per-tier counts it prints include the record it is counting.
// Printed first, the line reports handled=10000 beside tiers summing to 9999
// and cannot be used to check itself.
func (d *driftFilter) Handle(record AuditRecord) {
	d.handled++
	d.tally(record)
	if d.countsLogDue() {
		// The principal names go out with the counts, not only at shutdown.
		// They are the half of the report an operator can act on, and a
		// SIGKILLed pod never reaches the shutdown line.
		logCountsProgress(d.handled, d.counts, d.unattributed.Sorted())
	}
}

// countsLogDue reports whether the progress line is owed, on either half of
// the interval, and resets the time half when it is. Whichever arrives first
// wins: the record count carries a busy stream, the elapsed time carries a
// quiet one, and neither alone covers the rate range measured.
//
// The clock is read once per record rather than only when the count trips,
// because the point is to bound the wait on a stream too slow to ever trip it.
func (d *driftFilter) countsLogDue() bool {
	now := d.clock()
	if d.handled%countsLogInterval == 0 || now.Sub(d.lastCountsLog) >= countsLogMaxInterval {
		d.lastCountsLog = now
		return true
	}
	return false
}

// tally counts one record against its tier and forwards it if it survives every
// filter. It is separate from Handle only to give the counting a single exit
// point: the filters below return early, and the progress line has to print
// after the last of them.
//
// The two drop paths build their reason string inside the logDropped guard
// rather than passing it to a helper that discards it. Over 99% of the stream
// is dropped, so formatting a reason nobody reads is an allocation per record
// on the hot path.
func (d *driftFilter) tally(record AuditRecord) {
	tier := d.classifier.Classify(record.Principal)
	d.counts.ByTier[tier]++

	if tier == TierUnattributed {
		d.unattributed.Add(record.Principal)
	}

	// Outcome before tier: a failed call changed nothing whatever made it, so
	// reporting it as a drop for the tier reason would be wrong.
	if !record.Succeeded() {
		d.counts.Failed++
		if d.logDropped {
			logDroppedRecord(record, tier, fmt.Sprintf("call failed (status %d: %s)", record.StatusCode, record.StatusMessage))
		}
		return
	}

	if tier != TierHuman {
		if d.logDropped {
			logDroppedRecord(record, tier, fmt.Sprintf("principal is %s", tier))
		}
		return
	}

	// Last, because it is the narrowest test and the only one that has to run
	// on the human path alone. Everything above drops ~98% of the stream, so a
	// map lookup here is paid for by almost nothing.
	if nonDeclarativeSubresources[record.Resource.Subresource] {
		d.counts.NonDeclarative++
		if d.logDropped {
			logDroppedRecord(record, tier, fmt.Sprintf("subresource %q changes no declarative state", record.Resource.Subresource))
		}
		return
	}

	d.counts.Actionable++
	d.next(record)
}

// Counts reports the tally so far.
func (d *driftFilter) Counts() TierCounts {
	return d.counts
}

// Unattributed reports the principals that matched no rule.
func (d *driftFilter) Unattributed() []string {
	return d.unattributed.Sorted()
}
