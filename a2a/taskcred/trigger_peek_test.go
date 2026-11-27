package taskcred

import (
	"testing"
	"time"
)

// UnverifiedTrigger reads a credential's trigger without its signature, to
// decide a refusal only: a trigger and its script, none, or an error for
// what isn't a credential.
func TestUnverifiedTriggerReadsTheClaimsTrigger(t *testing.T) {
	kp, err := NewSigningKey()
	if err != nil {
		t.Fatal(err)
	}
	base := Claims{Task: "t1", Profile: "helpers", Tier: "unattended", Pod: "helpers-job-1",
		PodUID: "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0", Audience: AudienceCredentialProxy, Expires: time.Now().Add(time.Minute),
		// A target, so the no-trigger case is a credential Mint will write.
		Target: &Target{Project: "proj-a", Location: "us-central1", Cluster: "cluster-a"}}
	scripted := base
	scripted.Trigger = &Trigger{Name: "schedule/stall-watch", Origin: "builtin", Script: "stall-watch"}
	for name, tc := range map[string]struct {
		claims Claims
		want   string
		none   bool
	}{
		"a script":   {scripted, "stall-watch", false},
		"no trigger": {base, "", true},
	} {
		tok, err := Mint(kp, tc.claims)
		if err != nil {
			t.Fatal(err)
		}
		tr, err := UnverifiedTrigger(tok)
		if err != nil || (tr == nil) != tc.none || (tr != nil && tr.Script != tc.want) {
			t.Errorf("%s: %+v, %v", name, tr, err)
		}
	}
	for _, bad := range []string{"", "a.b", "a.!!!.c", "a.bm90IGpzb24.c"} {
		if _, err := UnverifiedTrigger(bad); err == nil {
			t.Errorf("%q: no error", bad)
		}
	}
}

// UnverifiedTarget reads a credential's target without its signature, for
// routing only: the cluster, none for a report credential, or an error for
// what isn't a credential.
func TestUnverifiedTargetReadsTheClaimsTarget(t *testing.T) {
	kp, err := NewSigningKey()
	if err != nil {
		t.Fatal(err)
	}
	targeted := Claims{Task: "t1", Profile: "helpers", Tier: "read", Pod: "helpers-job-1",
		PodUID: "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0", Audience: AudienceCredentialProxy, Expires: time.Now().Add(time.Minute),
		Target: &Target{Project: "proj-a", Location: "us-central1", Cluster: "cluster-a"}}
	// A report credential: the report tier and a trigger, and no target.
	report := targeted
	report.Tier, report.Target = ReportTier, nil
	report.Trigger = &Trigger{Name: "schedule/nightly", Origin: TriggerOriginBuiltin, Stream: "nightly"}
	for name, tc := range map[string]struct {
		claims Claims
		want   *Target
	}{
		"a target":            {targeted, targeted.Target},
		"a report credential": {report, nil},
	} {
		tok, err := Mint(kp, tc.claims)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		got, err := UnverifiedTarget(tok)
		switch {
		case err != nil:
			t.Errorf("%s: %v", name, err)
		case (got == nil) != (tc.want == nil):
			t.Errorf("%s: target %+v, want %+v", name, got, tc.want)
		case got != nil && *got != *tc.want:
			t.Errorf("%s: target %+v, want %+v", name, *got, *tc.want)
		}
	}
	for _, bad := range []string{"", "a.b", "a.!!!.c", "a.bm90IGpzb24.c"} {
		if _, err := UnverifiedTarget(bad); err == nil {
			t.Errorf("%q: no error", bad)
		}
	}
}
