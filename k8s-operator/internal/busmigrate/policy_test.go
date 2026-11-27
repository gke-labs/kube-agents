package busmigrate

import (
	"bytes"
	"context"
	"errors"
	"slices"
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
	"KV_PROFILE_LEASES", "PROFILE_SUBMITS", "KV_people", "DISPATCH_STOPS",
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

// Appending missing subjects refuses any change other than added policy
// subjects: no removed subject, no changed limit or retention, no RePublish.
func TestSubjectAppendRefusesEveryNonSubjectChange(t *testing.T) {
	f, err := LoadFreshStreams()
	if err != nil {
		t.Fatal(err)
	}
	full := f.Streams["TOPICS-STATE"]
	p := policyOf(full)
	p.MaxConsumers = full.MaxConsumers

	const skewSubject = "a2a.topics.agent.platform.topology-skew"
	before := full
	before.Subjects = slices.DeleteFunc(slices.Clone(full.Subjects), func(s string) bool {
		return s == skewSubject
	})
	if len(before.Subjects) != len(full.Subjects)-1 {
		t.Fatalf("TOPICS-STATE reference does not carry %s: %v", skewSubject, full.Subjects)
	}

	after, added, err := p.AppendMissingSubjects(before)
	if err != nil {
		t.Fatalf("AppendMissingSubjects on pre-upgrade TOPICS-STATE: %v", err)
	}
	if !slices.Equal(added, []string{skewSubject}) {
		t.Errorf("added = %v, want [%s]", added, skewSubject)
	}
	wantSubjects := append(slices.Clone(before.Subjects), skewSubject)
	if !slices.Equal(after.Subjects, wantSubjects) {
		t.Errorf("after.Subjects = %v, want %v", after.Subjects, wantSubjects)
	}
	if _, addedAgain, err := p.AppendMissingSubjects(after); err != nil || len(addedAgain) != 0 {
		t.Errorf("AppendMissingSubjects when complete: added=%v, err=%v", addedAgain, err)
	}

	for name, mutate := range map[string]func(b, a *nats.StreamConfig){
		"removed subject": func(_, a *nats.StreamConfig) {
			a.Subjects = a.Subjects[1:]
		},
		"replaced subject": func(_, a *nats.StreamConfig) {
			a.Subjects[0] = skewSubject
		},
		"added unrendered subject": func(_, a *nats.StreamConfig) {
			a.Subjects = append(a.Subjects, "a2a.topics.other")
		},
		"added wildcard subject": func(_, a *nats.StreamConfig) {
			a.Subjects = append(a.Subjects, "a2a.topics.>")
		},
		"duplicate subject": func(_, a *nats.StreamConfig) {
			a.Subjects = append(a.Subjects, a.Subjects[0])
		},
		"empty subjects": func(_, a *nats.StreamConfig) {
			a.Subjects = nil
		},
		"republish on after": func(_, a *nats.StreamConfig) {
			a.RePublish = &nats.RePublish{Source: ">", Destination: "a2a.tasks.platform.t1.in"}
		},
		"republish on before": func(b, _ *nats.StreamConfig) {
			b.RePublish = &nats.RePublish{Source: ">", Destination: "a2a.tasks.platform.t1.in"}
		},
		"sources on after": func(_, a *nats.StreamConfig) {
			a.Sources = []*nats.StreamSource{{Name: "TASKS"}}
		},
		"mirror on after": func(_, a *nats.StreamConfig) {
			a.Mirror = &nats.StreamSource{Name: "TASKS"}
		},
		"changed retention": func(_, a *nats.StreamConfig) {
			a.Retention = nats.InterestPolicy
		},
		"changed storage": func(_, a *nats.StreamConfig) {
			a.Storage = nats.MemoryStorage
		},
		"changed max_bytes": func(_, a *nats.StreamConfig) {
			a.MaxBytes = before.MaxBytes / 2
		},
		"changed max_age": func(_, a *nats.StreamConfig) {
			a.MaxAge = time.Hour
		},
		"changed discard": func(_, a *nats.StreamConfig) {
			a.Discard = nats.DiscardNew
		},
		"changed max_msgs_per_subject": func(_, a *nats.StreamConfig) {
			a.MaxMsgsPerSubject = 1
		},
		"changed max_msgs": func(_, a *nats.StreamConfig) {
			a.MaxMsgs = 100
		},
		"changed max_msg_size": func(_, a *nats.StreamConfig) {
			a.MaxMsgSize = 1024
		},
		"changed max_consumers": func(_, a *nats.StreamConfig) {
			a.MaxConsumers = before.MaxConsumers + 1
		},
		"changed num_replicas": func(_, a *nats.StreamConfig) {
			a.Replicas = 3
		},
		"changed allow_rollup_hdrs": func(_, a *nats.StreamConfig) {
			a.AllowRollup = !before.AllowRollup
		},
		"changed deny_delete": func(_, a *nats.StreamConfig) {
			a.DenyDelete = !before.DenyDelete
		},
		"changed deny_purge": func(_, a *nats.StreamConfig) {
			a.DenyPurge = !before.DenyPurge
		},
		"changed allow_direct": func(_, a *nats.StreamConfig) {
			a.AllowDirect = !before.AllowDirect
		},
		"changed duplicate_window": func(_, a *nats.StreamConfig) {
			a.Duplicates = 5 * time.Minute
		},
		"changed description": func(_, a *nats.StreamConfig) {
			a.Description = "tampered"
		},
		"changed name": func(_, a *nats.StreamConfig) {
			a.Name = "TASKS"
		},
	} {
		b := before
		b.Subjects = slices.Clone(before.Subjects)
		a := after
		a.Subjects = slices.Clone(after.Subjects)
		mutate(&b, &a)
		if err := p.CheckSubjectAppend(b, a); !errors.Is(err, ErrSubjectAppendRefused) {
			t.Errorf("%s: CheckSubjectAppend = %v, want ErrSubjectAppendRefused", name, err)
		}
	}
}

func TestAppendMissingSubjectsOnARealServer(t *testing.T) {
	f, err := LoadFreshStreams()
	if err != nil {
		t.Fatal(err)
	}
	full := f.Streams["TOPICS-STATE"]
	policy := policyOf(full)
	policy.MaxConsumers = full.MaxConsumers

	const skewSubject = "a2a.topics.agent.platform.topology-skew"
	preUpgrade := full
	preUpgrade.Subjects = slices.DeleteFunc(slices.Clone(full.Subjects), func(s string) bool {
		return s == skewSubject
	})

	srv := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	defer shutdown(srv)
	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := nc.JetStream()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := js.AddStream(&preUpgrade); err != nil {
		t.Fatal(err)
	}
	if _, err := js.Publish("a2a.topics.shared.blueprint", []byte("starter")); err != nil {
		t.Fatal(err)
	}

	ctx := context.Background()
	updates := 0
	sub, err := nc.Subscribe("$JS.API.STREAM.UPDATE.>", func(*nats.Msg) { updates++ })
	if err != nil {
		t.Fatal(err)
	}
	defer sub.Unsubscribe()

	added, err := AppendMissingSubjects(ctx, nc, policy)
	if err != nil {
		t.Fatalf("AppendMissingSubjects: %v", err)
	}
	if !slices.Equal(added, []string{skewSubject}) {
		t.Errorf("added = %v, want [%s]", added, skewSubject)
	}
	if updates != 1 {
		t.Errorf("STREAM.UPDATE count = %d, want 1", updates)
	}
	info, err := js.StreamInfo("TOPICS-STATE")
	if err != nil {
		t.Fatal(err)
	}
	wantSubjects := append(slices.Clone(preUpgrade.Subjects), skewSubject)
	if !slices.Equal(info.Config.Subjects, wantSubjects) {
		t.Errorf("live subjects = %v, want %v", info.Config.Subjects, wantSubjects)
	}
	if info.State.Msgs != 1 {
		t.Errorf("live messages = %d, want 1 preserved", info.State.Msgs)
	}
	if _, err := js.Publish(skewSubject, []byte("skew")); err != nil {
		t.Errorf("publishing %s after append: %v", skewSubject, err)
	}

	// A second pass when no subject is missing sends no STREAM.UPDATE.
	addedAgain, err := AppendMissingSubjects(ctx, nc, policy)
	if err != nil || len(addedAgain) != 0 {
		t.Errorf("second AppendMissingSubjects: added=%v, err=%v", addedAgain, err)
	}
	if updates != 1 {
		t.Errorf("STREAM.UPDATE count after no-op pass = %d, want 1", updates)
	}

	// Forward restore of a pre-upgrade TOPICS-STATE snapshot also appends the
	// missing subject, while rollback restores the snapshot's own subject list.
	if err := js.DeleteStream("TOPICS-STATE"); err != nil {
		t.Fatal(err)
	}
	if _, err := js.AddStream(&preUpgrade); err != nil {
		t.Fatal(err)
	}
	if _, err := js.Publish("a2a.topics.shared.blueprint", []byte("starter")); err != nil {
		t.Fatal(err)
	}
	var snap bytes.Buffer
	meta, err := Snapshot(ctx, nc, "TOPICS-STATE", &snap)
	if err != nil {
		t.Fatal(err)
	}
	if err := js.DeleteStream("TOPICS-STATE"); err != nil {
		t.Fatal(err)
	}
	if err := RestoreVerified(ctx, nc, meta.Config, meta.State, meta.TakenAt, opener(snap.Bytes()), Digest(snap.Bytes()), policy); err != nil {
		t.Fatalf("RestoreVerified on pre-upgrade TOPICS-STATE: %v", err)
	}
	restoredInfo, err := js.StreamInfo("TOPICS-STATE")
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(restoredInfo.Config.Subjects, wantSubjects) {
		t.Errorf("forward-restored subjects = %v, want %v", restoredInfo.Config.Subjects, wantSubjects)
	}
	if err := js.DeleteStream("TOPICS-STATE"); err != nil {
		t.Fatal(err)
	}
	if err := RestoreAsItWas(ctx, nc, meta.Config, meta.State, opener(snap.Bytes()), Digest(snap.Bytes()), policy); err != nil {
		t.Fatalf("RestoreAsItWas on pre-upgrade TOPICS-STATE: %v", err)
	}
	rollbackInfo, err := js.StreamInfo("TOPICS-STATE")
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(rollbackInfo.Config.Subjects, preUpgrade.Subjects) {
		t.Errorf("rollback-restored subjects = %v, want %v", rollbackInfo.Config.Subjects, preUpgrade.Subjects)
	}
}
