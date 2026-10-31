package busmigrate

import (
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// provisionedStreams mirrors the operator's a2aProvisionedStreams; the
// operator's own test holds its table to the reference too.
var provisionedStreams = []string{
	"TASKS", "DIRECTORY", "TOPICS-STATE", "TOPICS-JOURNAL",
	"KV_runtime-state", "KV_session-state", "KV_cap", "KV_feedback",
	"KV_PROFILE_LEASES", "PROFILE_SUBMITS",
}

func policyOf(cfg nats.StreamConfig) StreamPolicy {
	return StreamPolicy{
		Name: cfg.Name, Subjects: cfg.Subjects, Retention: cfg.Retention, Storage: cfg.Storage,
		MaxBytes: cfg.MaxBytes, MaxAge: cfg.MaxAge, Discard: cfg.Discard, MaxMsgsPerSubject: cfg.MaxMsgsPerSubject,
		AllowRollup: cfg.AllowRollup, DenyDelete: cfg.DenyDelete, DenyPurge: cfg.DenyPurge, AllowDirect: cfg.AllowDirect,
		Replicas: cfg.Replicas,
	}
}

func TestTheFreshInstallReferenceCoversEveryProvisionedStream(t *testing.T) {
	f, err := LoadFreshStreams()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(f.Provenance, "natscli v0.1.5") || !strings.Contains(f.Provenance, "v2.10.29") {
		t.Errorf("provenance %q does not name the CLI and server versions", f.Provenance)
	}
	for _, n := range provisionedStreams {
		cfg, ok := f.Streams[n]
		if !ok || cfg.Name != n {
			t.Errorf("no fresh-install reference for %s", n)
		}
	}
}

// Every policy field, changed alone, is a difference Check reports; a
// stream matching its policy reports none.
func TestAPolicyCatchesEveryFieldItDecides(t *testing.T) {
	f, err := LoadFreshStreams()
	if err != nil {
		t.Fatal(err)
	}
	base := f.Streams["KV_cap"]
	p := policyOf(base)
	if d := p.Check(base); len(d) != 0 {
		t.Fatalf("the stream its policy came from differs: %v", d)
	}
	for name, edit := range map[string]func(*nats.StreamConfig){
		"name":                 func(c *nats.StreamConfig) { c.Name = "KV_other" },
		"subjects":             func(c *nats.StreamConfig) { c.Subjects = append(c.Subjects, ">") },
		"subjects ":            func(c *nats.StreamConfig) { c.Subjects[0] = ">" }, // widened, same count
		"retention":            func(c *nats.StreamConfig) { c.Retention = nats.WorkQueuePolicy },
		"storage":              func(c *nats.StreamConfig) { c.Storage = nats.MemoryStorage },
		"max_bytes":            func(c *nats.StreamConfig) { c.MaxBytes *= 2 },
		"max_age":              func(c *nats.StreamConfig) { c.MaxAge = time.Hour },
		"discard":              func(c *nats.StreamConfig) { c.Discard = nats.DiscardOld },
		"max_msgs_per_subject": func(c *nats.StreamConfig) { c.MaxMsgsPerSubject = 100 },
		"allow_rollup_hdrs":    func(c *nats.StreamConfig) { c.AllowRollup = !c.AllowRollup },
		"deny_delete":          func(c *nats.StreamConfig) { c.DenyDelete = !c.DenyDelete },
		"deny_purge":           func(c *nats.StreamConfig) { c.DenyPurge = !c.DenyPurge },
		"allow_direct":         func(c *nats.StreamConfig) { c.AllowDirect = !c.AllowDirect },
		"num_replicas":         func(c *nats.StreamConfig) { c.Replicas = 3 },
		"sources":              func(c *nats.StreamConfig) { c.Sources = []*nats.StreamSource{{Name: "TASKS"}} },
		"mirror":               func(c *nats.StreamConfig) { c.Mirror = &nats.StreamSource{Name: "TASKS"} },
		"republish":            func(c *nats.StreamConfig) { c.RePublish = &nats.RePublish{Source: ">", Destination: "x.>"} },
	} {
		cfg := base
		cfg.Subjects = append([]string(nil), base.Subjects...)
		edit(&cfg)
		d := p.Check(cfg)
		if len(d) != 1 || !strings.HasPrefix(d[0], strings.TrimSpace(name)+":") {
			t.Errorf("changing %s: Check = %v, want one difference naming it", name, d)
		}
	}
	// Subjects are a set: the same ones in another order match.
	multi := policyOf(f.Streams["TOPICS-STATE"])
	cfg := f.Streams["TOPICS-STATE"]
	cfg.Subjects = append([]string(nil), cfg.Subjects...)
	cfg.Subjects[0], cfg.Subjects[len(cfg.Subjects)-1] = cfg.Subjects[len(cfg.Subjects)-1], cfg.Subjects[0]
	if d := multi.Check(cfg); len(d) != 0 {
		t.Errorf("reordered subjects differ: %v", d)
	}
}

// A field outside the policy that differs from a fresh install is a named
// warning; a policy field never is (Check owns those); a stream the
// reference does not know is a warning too.
func TestDriftOutsideThePolicyIsNamed(t *testing.T) {
	f, err := LoadFreshStreams()
	if err != nil {
		t.Fatal(err)
	}
	cfg := f.Streams["TASKS"]
	if d := f.OutsidePolicyDrift(cfg); len(d) != 0 {
		t.Fatalf("the reference drifts from itself: %v", d)
	}
	cfg.MaxConsumers = 999
	cfg.Description = "edited"
	d := f.OutsidePolicyDrift(cfg)
	if len(d) != 2 || !strings.Contains(strings.Join(d, "\n"), "max_consumers is 999") || !strings.Contains(strings.Join(d, "\n"), "description is edited") {
		t.Errorf("drift %v, want max_consumers and description named", d)
	}
	policyOnly := f.Streams["TASKS"]
	policyOnly.MaxBytes, policyOnly.Replicas, policyOnly.Subjects = 1, 3, []string{">"}
	if d := f.OutsidePolicyDrift(policyOnly); len(d) != 0 {
		t.Errorf("policy fields reported as drift: %v", d)
	}
	if d := f.OutsidePolicyDrift(nats.StreamConfig{Name: "UNKNOWN"}); len(d) != 1 || !strings.Contains(d[0], "no fresh-install reference") {
		t.Errorf("unknown stream drift %v", d)
	}
}

// Subject membership is string-exact: a wildcard that covers a policy
// subject is refused, never read as a match, and so is an empty list; a
// subset passes, with each missing subject named as a warning.
func TestSubjectsMustBeExactlyAmongThePolicys(t *testing.T) {
	p := StreamPolicy{Name: "TOPICS-STATE", Subjects: []string{"a2a.topics.x", "a2a.topics.y"}}
	cfg := func(subjects ...string) nats.StreamConfig {
		return nats.StreamConfig{Name: "TOPICS-STATE", Subjects: subjects}
	}
	subjectDiffs := func(c nats.StreamConfig) []string {
		var d []string
		for _, s := range p.Check(c) {
			if strings.HasPrefix(s, "subjects:") {
				d = append(d, s)
			}
		}
		return d
	}
	for name, c := range map[string]nats.StreamConfig{
		"a wildcard over the rendered subject": cfg("a2a.topics.>"),
		"a wider wildcard":                     cfg("a2a.>"),
		"a token wildcard":                     cfg("a2a.topics.*"),
		"the full wildcard":                    cfg(">"),
		"a rendered subject plus a wildcard":   cfg("a2a.topics.x", "a2a.topics.>"),
		"a rewritten token":                    cfg("a2a.topics.z"),
		"none":                                 cfg(),
	} {
		if len(subjectDiffs(c)) == 0 {
			t.Errorf("%s (%v) passed the subject check", name, c.Subjects)
		}
	}
	for name, c := range map[string]nats.StreamConfig{
		"all of them":         cfg("a2a.topics.x", "a2a.topics.y"),
		"all, another order":  cfg("a2a.topics.y", "a2a.topics.x"),
		"a subset (narrower)": cfg("a2a.topics.x"),
	} {
		if d := subjectDiffs(c); len(d) != 0 {
			t.Errorf("%s refused: %v", name, d)
		}
	}
	if m := p.MissingSubjects(cfg("a2a.topics.x")); len(m) != 1 || !strings.Contains(m[0], "a2a.topics.y is missing") {
		t.Errorf("MissingSubjects of a subset = %v", m)
	}
	if m := p.MissingSubjects(cfg("a2a.topics.y", "a2a.topics.x")); len(m) != 0 {
		t.Errorf("MissingSubjects of the full set = %v", m)
	}
}
