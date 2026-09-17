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
	"bytes"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

// The fixtures in testdata/ were captured from live Cloud Audit Logs with
// `gcloud logging read --format=json`, which returns the same LogEntry shape
// the Log Router publishes to the drift-pubsub topic. CUJ 3 T2 asks for
// captured fixtures rather than hand-written ones, and the reason shows up
// immediately: `"authenticationInfo": {}` in unauthenticated_bot.json is a
// shape nobody would have thought to write by hand, and it is the one that
// breaks the classifier the breakdown specifies.
//
// Only identifiers are substituted -- principal, project, project number,
// cluster, node name and caller IP. Every field name, nesting level and value
// shape is as captured. Where a request or response body ran long it was
// replaced with a stub marked "_trimmed"; the detector decodes neither field.
//
// The two human fixtures came from a 30-day query rather than the 24-hour one
// that produced the rest. Human traffic is bursty: the day the machine
// fixtures were captured had no human changes at all across three projects,
// while 30 days held 694 across two of them -- about the seven a day per
// cluster the spike predicted. A quiet day is normal and is not a broken rule.
//
// One exception, called out because the rest of this comment would otherwise
// vouch for it: human_exec.json is DERIVED, not captured. The live query
// establishes that io.k8s.core.v1.pods.exec.create occurs and what its
// methodName and resourceName look like, and the envelope here is
// human_patch.json's capture with those two fields and the subresource path
// substituted. It exists because nonDeclarativeSubresources is the one rule
// with no captured record behind it, and without it nothing exercises
// parseAuditEntry on a resourceName carrying a subresource -- so if
// resourcename.go ever stopped populating ResourceRef.Subresource for that
// shape, the hand-built subresource test below would still pass while a real
// `kubectl exec` was reported as drift. Replace it with a capture when one is
// to hand.
//
// Captured 2026-09-15 from a live GKE fleet.

// loadFixture reads one captured entry.
func loadFixture(t *testing.T, name string) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", name+".json"))
	if err != nil {
		t.Fatalf("read fixture %s: %v", name, err)
	}
	return data
}

// TestClassifyCapturedFixtures is the table CUJ 3 T2 asks for: every tier, and
// a delete, over records captured from a live subscription rather than
// invented.
func TestClassifyCapturedFixtures(t *testing.T) {
	tests := []struct {
		fixture       string
		wantPrincipal string
		wantTier      Tier
		wantSucceeded bool
		wantActioned  bool
		wantMessage   string
		why           string
	}{
		{
			fixture:       "system_success",
			wantPrincipal: "system:anet-controller-manager",
			wantTier:      TierSystem,
			wantSucceeded: true,
			wantActioned:  false,
			why:           "a control-plane controller patching a ConfigMap is reconciliation, not drift",
		},
		{
			fixture:       "serviceaccount",
			wantPrincipal: "service-000000000000@container-engine-robot.iam.gserviceaccount.com",
			wantTier:      TierAutomation,
			wantSucceeded: true,
			wantActioned:  false,
			why:           "a GCP service account is machine traffic even with no allowlist configured",
		},
		{
			fixture:       "kubelet_bootstrap",
			wantPrincipal: "kubelet-nodepool-bootstrap",
			wantTier:      TierUnattributed,
			wantSucceeded: true,
			wantActioned:  false,
			why:           "the regression this tier exists for: a machine identity with no system: prefix and no service-account domain",
		},
		{
			fixture:       "unauthenticated_bot",
			wantPrincipal: "",
			wantTier:      TierUnattributed,
			wantSucceeded: false,
			wantActioned:  false,
			wantMessage:   "Unauthorized",
			why:           "an unauthenticated crawler the API server rejected with 401; empty principal, and the call changed nothing",
		},
		{
			fixture:       "failed_aborted",
			wantPrincipal: "system:gke-spiffe-controller",
			wantTier:      TierSystem,
			wantSucceeded: false,
			wantActioned:  false,
			wantMessage:   `leases.coordination.k8s.io "gke-spiffe-controller-c4afa9e1-2626-4975-903d-57c6936e78f9" already exists`,
			why:           "status 10 ABORTED: the write lost an optimistic-concurrency race and the cluster did not change",
		},
		{
			fixture:       "delete_call",
			wantPrincipal: "system:node:gke-prod-1-pool-1-abcd1234-node",
			wantTier:      TierSystem,
			wantSucceeded: true,
			wantActioned:  false,
			why:           "a delete parses and classifies like any other verb; T3 is where it short-circuits",
		},
		{
			fixture:       "human_patch",
			wantPrincipal: "engineer@example.com",
			wantTier:      TierHuman,
			wantSucceeded: true,
			wantActioned:  true,
			why:           "the one record in the set that is drift: a person patching a Deployment with kubectl",
		},
		{
			fixture:       "human_denied",
			wantPrincipal: "engineer@example.com",
			wantTier:      TierHuman,
			wantSucceeded: false,
			wantActioned:  false,
			wantMessage:   `pods "crashloop-probe" is forbidden: failed quota: default-quota: must specify limits.cpu for: crashloop-probe; limits.memory for: crashloop-probe; requests.cpu for: crashloop-probe; requests.memory for: crashloop-probe`,
			why:           "status 7 PERMISSION_DENIED: a person tried and was refused, so the cluster did not change",
		},
		{
			fixture:       "human_exec",
			wantPrincipal: "engineer@example.com",
			wantTier:      TierHuman,
			wantSucceeded: true,
			wantActioned:  false,
			why:           "a successful human call that is still not drift: exec changes no declarative state, and it gets here because the methodName carries create as a suffix",
		},
	}

	for _, tc := range tests {
		t.Run(tc.fixture, func(t *testing.T) {
			record, err := parseAuditEntry(loadFixture(t, tc.fixture))
			if err != nil {
				t.Fatalf("parseAuditEntry(%s): %v", tc.fixture, err)
			}
			if record.Principal != tc.wantPrincipal {
				t.Errorf("principal = %q, want %q", record.Principal, tc.wantPrincipal)
			}
			if got := record.Succeeded(); got != tc.wantSucceeded {
				t.Errorf("Succeeded() = %v, want %v (status %d %q)",
					got, tc.wantSucceeded, record.StatusCode, record.StatusMessage)
			}
			// Asserted on its own, not only inside the failure message above.
			// StatusMessage's one consumer is the --log-dropped reason string,
			// so a wrong json tag or a field Google renames would empty the
			// half of that line saying why a change was rejected, while every
			// other assertion here still passed.
			if record.StatusMessage != tc.wantMessage {
				t.Errorf("StatusMessage = %q, want %q", record.StatusMessage, tc.wantMessage)
			}

			classifier := NewClassifier("", "")
			if got := classifier.Classify(record.Principal); got != tc.wantTier {
				t.Errorf("Classify(%q) = %q, want %q -- %s", record.Principal, got, tc.wantTier, tc.why)
			}

			var forwarded []AuditRecord
			filter := newDriftFilter(classifier, func(r AuditRecord) { forwarded = append(forwarded, r) }, false)
			filter.Handle(record)
			if gotActioned := len(forwarded) == 1; gotActioned != tc.wantActioned {
				t.Errorf("forwarded = %v, want %v -- %s", gotActioned, tc.wantActioned, tc.why)
			}
		})
	}
}

// TestClassifyMachineFixturesAreNeverHuman is the regression this whole file
// exists for. Every fixture named here is a machine, and every one of them the
// breakdown's two-rule classifier would have called human: kubelet_bootstrap
// and unauthenticated_bot because they match neither drop rule, the rest
// because a rule ordered differently would let them through. Exactly one
// captured record is allowed to be human, and it is not in this list.
func TestClassifyMachineFixturesAreNeverHuman(t *testing.T) {
	fixtures := []string{
		"system_success", "serviceaccount", "kubelet_bootstrap",
		"unauthenticated_bot", "failed_aborted", "delete_call",
	}
	classifier := NewClassifier("", "")
	for _, name := range fixtures {
		record, err := parseAuditEntry(loadFixture(t, name))
		if err != nil {
			t.Fatalf("parseAuditEntry(%s): %v", name, err)
		}
		if got := classifier.Classify(record.Principal); got == TierHuman {
			t.Errorf("%s: principal %q classified as human, but it is a machine identity",
				name, record.Principal)
		}
	}
}

// TestClassifyPrincipals covers the rule order directly, including the shapes
// the captured set happens not to contain.
func TestClassifyPrincipals(t *testing.T) {
	tests := []struct {
		name       string
		principal  string
		automation string
		domains    string
		want       Tier
	}{
		{"empty principal is unattributed", "", "", "", TierUnattributed},
		{"system prefix", "system:kube-controller-manager", "", "", TierSystem},
		{"system serviceaccount is system, not automation", "system:serviceaccount:kube-system:node-controller", "", "", TierSystem},
		{"gcp service account", "github-deploy-sa@example-project.iam.gserviceaccount.com", "", "", TierAutomation},

		// The Google-managed service accounts do not carry "iam" in their
		// domain, so a suffix test written as ".iam.gserviceaccount.com" sends
		// all four to the human tier on the strength of their "@". The Cloud
		// Build one is the sharpest: a pipeline deploying to the cluster is
		// what the automation tier is for, and misreading it as a person is an
		// inject accusing nobody of a change they did not make.
		{"compute engine default service account", "000000000000-compute@developer.gserviceaccount.com", "", "", TierAutomation},
		{"cloud build service account", "000000000000@cloudbuild.gserviceaccount.com", "", "", TierAutomation},
		{"app engine service account", "example-project@appspot.gserviceaccount.com", "", "", TierAutomation},
		{"cloudservices agent", "000000000000@cloudservices.gserviceaccount.com", "", "", TierAutomation},

		// The suffix is a DNS domain, so it is matched case-insensitively.
		// Unfolded, this principal misses the automation rule, carries an "@",
		// and is reported as a person making an out-of-band change.
		{"service account domain in upper case", "deployer@example-project.iam.GSERVICEACCOUNT.COM", "", "", TierAutomation},
		{"service account domain in mixed case", "deployer@example-project.iam.GServiceAccount.Com", "", "", TierAutomation},

		// The leading dot in the suffix is what keeps the match on a domain
		// boundary. Without it this lookalike would be swallowed as automation
		// and never looked at again. Folding the case must not widen it.
		{"lookalike domain is not a service account", "mallory@evilgserviceaccount.com", "", "", TierHuman},
		{"upper-case lookalike is not a service account", "mallory@EVILGSERVICEACCOUNT.COM", "", "", TierHuman},
		{"allowlisted bare name", "kubelet-nodepool-bootstrap", "kubelet-nodepool-bootstrap", "", TierAutomation},
		{"allowlist is exact, not a prefix", "kubelet-nodepool-bootstrap-2", "kubelet-nodepool-bootstrap", "", TierUnattributed},
		{"human with any domain by default", "ada@example.com", "", "", TierHuman},
		{"human inside configured domain", "ada@example.com", "", "example.com", TierHuman},
		{"human outside configured domain", "mallory@evil.test", "", "example.com", TierUnattributed},
		{"domain match cannot straddle the boundary", "ada@notexample.com", "", "example.com", TierUnattributed},
		{"configured domain tolerates a leading @", "ada@example.com", "", "@example.com", TierHuman},

		// ".example.com" is how a domain-and-its-subtree is written elsewhere,
		// so an operator reaches for it. Stored verbatim it becomes
		// "@.example.com" and matches nothing at all -- every person in the
		// fleet filed as unattributed, with nothing in the log saying why.
		{"configured domain tolerates a leading dot", "ada@example.com", "", ".example.com", TierHuman},
		{"configured domain tolerates both", "ada@example.com", "", "@.example.com", TierHuman},

		// Tolerating the dot is not the same as honouring it: the match stays
		// on the exact domain either way, and an organisation using subdomains
		// lists them. These two pin that down, because widening it silently
		// would widen what the detector reports as somebody's drift.
		{"configured domain does not match a subdomain", "ada@corp.example.com", "", "example.com", TierUnattributed},
		{"leading dot does not add subdomain matching", "ada@corp.example.com", "", ".example.com", TierUnattributed},
		{"subdomains are matched by listing them", "ada@corp.example.com", "", "corp.example.com,eng.example.com", TierHuman},

		// "@" alone is not an account. Testing for the character's presence
		// rather than for a local part and a domain admits all three of these.
		{"bare separator is not human", "@", "", "", TierUnattributed},
		{"missing domain is not human", "ada@", "", "", TierUnattributed},
		{"missing local part is not human", "@example.com", "", "", TierUnattributed},

		{"no domain and no rule is unattributed", "some-bootstrap-identity", "", "", TierUnattributed},
		{"allowlist beats the domain test", "ci-runner@example.com", "ci-runner@example.com", "example.com", TierAutomation},
		{"system beats the allowlist", "system:foo", "system:foo", "", TierSystem},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := NewClassifier(tc.automation, tc.domains).Classify(tc.principal)
			if got != tc.want {
				t.Errorf("Classify(%q) with automation=%q domains=%q = %q, want %q",
					tc.principal, tc.automation, tc.domains, got, tc.want)
			}
		})
	}
}

// TestClassifierListParsing covers the shell-assembled flag values: a trailing
// comma or a stray space must not become a principal that matches "".
func TestClassifierListParsing(t *testing.T) {
	c := NewClassifier(" a@x.iam.gserviceaccount.com , , bare-name,", " example.com , ")
	if got := c.Classify("bare-name"); got != TierAutomation {
		t.Errorf("padded allowlist entry: got %q, want %q", got, TierAutomation)
	}
	if got := c.Classify(""); got != TierUnattributed {
		t.Errorf("empty principal must not match a blank list entry: got %q", got)
	}
	if got := c.Classify("ada@example.com"); got != TierHuman {
		t.Errorf("padded domain entry: got %q, want %q", got, TierHuman)
	}
}

// TestDriftFilterCounts checks that every record is counted in its tier even
// though almost none are forwarded, which is what makes a bad allowlist
// visible.
func TestDriftFilterCounts(t *testing.T) {
	records := []AuditRecord{
		{Principal: "system:a"},
		{Principal: "system:b"},
		{Principal: "sa@p.iam.gserviceaccount.com"},
		{Principal: "kubelet-nodepool-bootstrap"},
		{Principal: "ada@example.com"},
		{Principal: "bob@example.com", StatusCode: 10, StatusMessage: "ABORTED"},
	}

	var forwarded []AuditRecord
	filter := newDriftFilter(NewClassifier("", ""), func(r AuditRecord) { forwarded = append(forwarded, r) }, false)
	for _, r := range records {
		filter.Handle(r)
	}

	counts := filter.Counts()
	want := map[Tier]int{TierSystem: 2, TierAutomation: 1, TierUnattributed: 1, TierHuman: 2}
	for tier, n := range want {
		if counts.ByTier[tier] != n {
			t.Errorf("ByTier[%s] = %d, want %d", tier, counts.ByTier[tier], n)
		}
	}
	if counts.Failed != 1 {
		t.Errorf("Failed = %d, want 1", counts.Failed)
	}
	if counts.Actionable != 1 {
		t.Errorf("Actionable = %d, want 1", counts.Actionable)
	}
	if len(forwarded) != 1 || forwarded[0].Principal != "ada@example.com" {
		t.Errorf("forwarded = %+v, want only ada@example.com", forwarded)
	}
}

// TestDriftFilterCountsFailedHumanInItsTier pins the ordering decision in
// TierCounts.ByTier's comment: a human change that the API server rejected is
// counted as human traffic and not forwarded. A cluster where every human
// change is being denied must not look like a cluster with no human changes.
func TestDriftFilterCountsFailedHumanInItsTier(t *testing.T) {
	filter := newDriftFilter(NewClassifier("", ""), func(AuditRecord) {
		t.Error("a failed call must not be forwarded")
	}, false)
	filter.Handle(AuditRecord{Principal: "ada@example.com", StatusCode: 7, StatusMessage: "PERMISSION_DENIED"})

	counts := filter.Counts()
	if counts.ByTier[TierHuman] != 1 {
		t.Errorf("ByTier[human] = %d, want 1", counts.ByTier[TierHuman])
	}
	if counts.Actionable != 0 {
		t.Errorf("Actionable = %d, want 0", counts.Actionable)
	}
	if counts.Failed != 1 {
		t.Errorf("Failed = %d, want 1", counts.Failed)
	}
}

// TestUnattributedPrincipalsSorted checks the operator-facing half: the names
// come back most-frequent-first, because that is the order you write rules in.
func TestUnattributedPrincipalsSorted(t *testing.T) {
	filter := newDriftFilter(NewClassifier("", ""), func(AuditRecord) {}, false)
	for i := 0; i < 3; i++ {
		filter.Handle(AuditRecord{Principal: "kubelet-nodepool-bootstrap"})
	}
	filter.Handle(AuditRecord{Principal: "other-identity"})

	// Quoted: Sorted renders the principal with %q, matching every other site
	// that prints one, so that a name carrying a newline cannot forge a line.
	got := filter.Unattributed()
	want := []string{`"kubelet-nodepool-bootstrap"=3`, `"other-identity"=1`}
	if len(got) != len(want) {
		t.Fatalf("Unattributed() = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("Unattributed()[%d] = %q, want %q", i, got[i], want[i])
		}
	}
}

// An unauthenticated request carries no principal, and it is the second most
// common entry in this list on a live cluster. Without the substitution the
// shutdown line reads "=120", which names nothing an operator can act on.
func TestUnattributedPrincipalsLabelsTheEmptyPrincipal(t *testing.T) {
	filter := newDriftFilter(NewClassifier("", ""), func(AuditRecord) {}, false)
	for i := 0; i < 2; i++ {
		filter.Handle(AuditRecord{Principal: ""})
	}

	got := filter.Unattributed()
	want := []string{fmt.Sprintf("%q=2", unauthenticatedPrincipalLabel)}
	if len(got) != 1 || got[0] != want[0] {
		t.Fatalf("Unattributed() = %v, want %v", got, want)
	}
	if strings.HasPrefix(got[0], `""=`) {
		t.Errorf("Unattributed()[0] = %q: the empty principal reached the log unlabelled", got[0])
	}
}

// The name set is keyed by a caller-influenced string in a process that runs
// for weeks, so it is capped. Past the cap the counts must stay honest: a
// principal already named keeps its own tally, and only new names collapse
// into the overflow bucket.
func TestUnattributedPrincipalsCapsTheNameSet(t *testing.T) {
	filter := newDriftFilter(NewClassifier("", ""), func(AuditRecord) {}, false)

	// Fill the set, then re-sight the first principal so it outranks the rest.
	for i := 0; i < maxUnattributedPrincipals; i++ {
		filter.Handle(AuditRecord{Principal: fmt.Sprintf("identity-%03d", i)})
	}
	filter.Handle(AuditRecord{Principal: "identity-000"})

	// Every one of these is new, so all of them land in the overflow bucket.
	const overflowSightings = 5
	for i := 0; i < overflowSightings; i++ {
		filter.Handle(AuditRecord{Principal: fmt.Sprintf("late-identity-%d", i)})
	}

	got := filter.Unattributed()
	if len(got) != maxUnattributedPrincipals+1 {
		t.Fatalf("Unattributed() has %d entries, want %d (the cap plus one overflow bucket)",
			len(got), maxUnattributedPrincipals+1)
	}
	// Containment, not position: Sorted orders by count, and the overflow
	// bucket's sightings outnumber this principal's.
	if !slices.Contains(got, `"identity-000"=2`) {
		t.Errorf(`Unattributed() = %v, missing "identity-000"=2: a named principal must keep counting past the cap`, got)
	}

	overflow := fmt.Sprintf("%q=%d", unattributedOverflowLabel, overflowSightings)
	if !slices.Contains(got, overflow) {
		t.Errorf("Unattributed() = %v, missing %s", got, overflow)
	}
	for _, entry := range got {
		if strings.HasPrefix(entry, `"late-identity-`) {
			t.Errorf("Unattributed() contains %q: a new name past the cap must not be stored", entry)
		}
	}

	// The cap bounds the names, not the tally: every sighting is still counted.
	wantTotal := maxUnattributedPrincipals + 1 + overflowSightings
	if got := filter.Counts().ByTier[TierUnattributed]; got != wantTotal {
		t.Errorf("unattributed count = %d, want %d: the cap must not lose sightings", got, wantTotal)
	}
}

// TestTierCountsStringAlwaysNamesEveryTier pins the reason tierOrder exists:
// a tier with no traffic must still print, so that "no human changes" and "the
// human rule stopped matching" are distinguishable in the shutdown log.
func TestTierCountsStringAlwaysNamesEveryTier(t *testing.T) {
	got := TierCounts{ByTier: map[Tier]int{}}.String()
	for _, tier := range tierOrder {
		if !strings.Contains(got, string(tier)+"=0") {
			t.Errorf("String() = %q, missing %s=0", got, tier)
		}
	}
}

// TestDriftFilterDropsNonDeclarativeSubresources pins the measured case behind
// nonDeclarativeSubresources: `kubectl exec` is audited as
// io.k8s.core.v1.pods.exec.create, which matches the sink's mutating-verb
// filter and names a real object, so nothing upstream of this rule stops it.
// A person exec-ing into a pod changes no declarative state and must not be
// reported as drift -- but the drop is counted, not silent.
func TestDriftFilterDropsNonDeclarativeSubresources(t *testing.T) {
	human := "ada@example.com"
	pod := func(subresource string) AuditRecord {
		return AuditRecord{
			Principal: human,
			Resource:  ResourceRef{Version: "v1", Namespace: "prod", Resource: "pods", Name: "api-0", Subresource: subresource},
		}
	}

	var forwarded []AuditRecord
	filter := newDriftFilter(NewClassifier("", ""), func(r AuditRecord) { forwarded = append(forwarded, r) }, false)
	for _, sub := range []string{"exec", "attach", "portforward", "proxy", "ephemeralcontainers"} {
		filter.Handle(pod(sub))
	}
	// Not a pod subresource, but it reaches here by the same route: `kubectl
	// create token` is audited as serviceaccounts.token.create.
	filter.Handle(AuditRecord{
		Principal: human,
		Resource:  ResourceRef{Version: "v1", Namespace: "prod", Resource: "serviceaccounts", Name: "deployer", Subresource: "token"},
	})
	// A real declarative write by the same principal on the same resource, to
	// show the rule keys on the subresource rather than on pods or on exec-like
	// traffic in general.
	filter.Handle(pod("status"))

	counts := filter.Counts()
	if counts.NonDeclarative != 6 {
		t.Errorf("NonDeclarative = %d, want 6", counts.NonDeclarative)
	}
	if counts.Actionable != 1 {
		t.Errorf("Actionable = %d, want 1", counts.Actionable)
	}
	if len(forwarded) != 1 || forwarded[0].Resource.Subresource != "status" {
		t.Fatalf("forwarded = %+v, want only the status write", forwarded)
	}
	// Classified before it is dropped, for the same reason a failed human call
	// is: a cluster whose only human traffic is exec must not read as empty.
	if counts.ByTier[TierHuman] != 7 {
		t.Errorf("ByTier[human] = %d, want 7", counts.ByTier[TierHuman])
	}
	if got := counts.String(); !strings.Contains(got, "non_declarative=6") {
		t.Errorf("String() = %q, want it to report non_declarative=6", got)
	}
}

// TestClassifierHumanDomainsAreCaseInsensitive pins the normalisation in
// NewClassifier. DNS is case-insensitive and GCP lower-cases principalEmail, so
// a miscased --human-domains value must still match. Unnormalised, this sends
// every person in the fleet to unattributed and the detector reports human=0
// while looking healthy, which is the worst shape a bug here can take.
func TestClassifierHumanDomainsAreCaseInsensitive(t *testing.T) {
	for _, configured := range []string{"Corp.Example.com", "@CORP.EXAMPLE.COM", "corp.example.com"} {
		c := NewClassifier("", configured)
		// Both sides are folded, so the principal's own case varies too.
		// GCP normalises principalEmail to lower case, but a username from a
		// customer OIDC provider or a client-certificate CN is forwarded as
		// given -- and folding only the configured domain would file that
		// person's real change as unattributed.
		for _, principal := range []string{"ada@corp.example.com", "Ada@Corp.Example.com", "ADA@CORP.EXAMPLE.COM"} {
			if got := c.Classify(principal); got != TierHuman {
				t.Errorf("--human-domains=%q: Classify(%q) = %s, want %s", configured, principal, got, TierHuman)
			}
		}
		// The domain boundary still holds after lower-casing, in either case.
		for _, principal := range []string{"mallory@evilcorp.example.com.attacker.test", "Mallory@EvilCorp.Example.com.Attacker.test"} {
			if got := c.Classify(principal); got == TierHuman {
				t.Errorf("--human-domains=%q: Classify(%q) was classified human", configured, principal)
			}
		}
	}
}

// TestClassifierAutomationPrincipalsStayCaseSensitive is the other half of the
// rule above, and the reason the two flags are folded differently. A
// Kubernetes username is case-sensitive by specification, so two principals
// differing only in case are two principals; folding the automation set would
// silently merge them. The cost is that a miscased entry never matches, which
// fails open -- the account reaches the human tier, where it is visible.
func TestClassifierAutomationPrincipalsStayCaseSensitive(t *testing.T) {
	c := NewClassifier("CI-Bot@example.com", "")
	if got := c.Classify("CI-Bot@example.com"); got != TierAutomation {
		t.Errorf("Classify(CI-Bot@example.com) = %s, want %s", got, TierAutomation)
	}
	if got := c.Classify("ci-bot@example.com"); got != TierHuman {
		t.Errorf("Classify(ci-bot@example.com) = %s, want %s: the automation set must not be case-folded", got, TierHuman)
	}
}

// TestDriftFilterLogDroppedDoesNotPanic covers the reason-string construction
// at all three drop sites, which is skipped entirely when logDropped is false
// -- the default every other test uses. It asserts behaviour rather than log
// text: the point is that turning the flag on changes no counts and reaches
// every fmt.Sprintf on the dropped path.
func TestDriftFilterLogDroppedDoesNotPanic(t *testing.T) {
	// Restore whatever was there, not os.Stderr, matching the sibling helper in
	// cmd/k8s-event-watcher. Hardcoding stderr would clobber a later test that
	// captures log output into a buffer to assert on the DRIFT line.
	prev := log.Writer()
	log.SetOutput(io.Discard)
	t.Cleanup(func() { log.SetOutput(prev) })

	filter := newDriftFilter(NewClassifier("", ""), func(AuditRecord) {}, true)
	records := []AuditRecord{
		{Principal: "system:kube-scheduler"},
		{Principal: "bob@example.com", StatusCode: 7, StatusMessage: "PERMISSION_DENIED"},
		{Principal: "ada@example.com", Resource: ResourceRef{Version: "v1", Resource: "pods", Name: "api-0", Subresource: "exec"}},
		{Principal: "ada@example.com", Resource: ResourceRef{Version: "v1", Resource: "pods", Name: "api-0"}},
	}
	for _, r := range records {
		filter.Handle(r)
	}

	counts := filter.Counts()
	if counts.Failed != 1 || counts.NonDeclarative != 1 || counts.Actionable != 1 {
		t.Errorf("failed=%d non_declarative=%d actionable=%d, want 1/1/1",
			counts.Failed, counts.NonDeclarative, counts.Actionable)
	}
}

// TestDriftFilterProgressLineIsTimeBounded pins the half of the progress
// interval a record count cannot deliver. countsLogInterval is 10000 records,
// and the measured stream runs from 0.7 to 10 records a second, so on a quiet
// project the count alone leaves the pod log silent for close to four hours --
// and T2 removed T1's line-per-record, so there is nothing else printing on
// the happy path. Driven through an injected clock rather than a sleep.
func TestDriftFilterProgressLineIsTimeBounded(t *testing.T) {
	var buf bytes.Buffer
	prev := log.Writer()
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(prev) })

	now := time.Now()
	filter := newDriftFilter(NewClassifier("", ""), func(AuditRecord) {}, false)
	filter.now = func() time.Time { return now }
	filter.lastCountsLog = now

	record := AuditRecord{Principal: "system:kube-scheduler"}

	// Far short of countsLogInterval with no time elapsed: nothing is owed.
	for i := 0; i < 5; i++ {
		filter.Handle(record)
	}
	if strings.Contains(buf.String(), "progress") {
		t.Fatalf("progress line before either bound was reached: %q", buf.String())
	}

	// Once the time bound passes, the next record reports the running tally.
	now = now.Add(countsLogMaxInterval)
	filter.Handle(record)
	if !strings.Contains(buf.String(), "progress handled=6") {
		t.Errorf("no progress line after %s elapsed; log = %q", countsLogMaxInterval, buf.String())
	}

	// The clock resets when it fires, so it does not then print every record.
	buf.Reset()
	filter.Handle(record)
	if buf.Len() != 0 {
		t.Errorf("progress line repeated on the very next record: %q", buf.String())
	}
}

// TestDriftFilterProgressLineStillFiresOnRecordCount guards the other half:
// the time bound must not have replaced the count. A busy stream reaches
// countsLogInterval long before countsLogMaxInterval elapses, and that is the
// case the constant was written for.
func TestDriftFilterProgressLineStillFiresOnRecordCount(t *testing.T) {
	var buf bytes.Buffer
	prev := log.Writer()
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(prev) })

	now := time.Now()
	filter := newDriftFilter(NewClassifier("", ""), func(AuditRecord) {}, false)
	filter.now = func() time.Time { return now }
	filter.lastCountsLog = now

	// The clock never advances, so only the record count can trip this.
	record := AuditRecord{Principal: "system:kube-scheduler"}
	for i := 0; i < countsLogInterval; i++ {
		filter.Handle(record)
	}
	if !strings.Contains(buf.String(), fmt.Sprintf("progress handled=%d", countsLogInterval)) {
		t.Errorf("no progress line at %d records with the clock frozen; log = %q", countsLogInterval, buf.String())
	}
}
