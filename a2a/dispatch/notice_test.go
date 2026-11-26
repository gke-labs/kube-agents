package dispatch

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/gateway"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The dispatcher's terminal is read by the gateway's relay and its tasks/get
// replay, both under the gateway's agreement policy, which refuses a
// `.supervisor` writer it does not name. The terminal as published must pass
// that policy, or the gateway drops it as a protocol error and the task never
// ends there.
func TestTheDispatchersTerminalPassesTheGatewaysAgreement(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	got := make(chan *nats.Msg, 4)
	sub, err := b.admin.ChanSubscribe("a2a.tasks.*.*."+lib.TaskClassSupervisor, got)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
	b.runDispatcher(testConfig(), oneProfile("tse", 2, "tse-0"), &fakeVerifier{})
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-refused", "", map[string]string{})
	select {
	case msg := <-got:
		env, err := lib.ParseEnvelope(msg.Data)
		if err != nil {
			t.Fatal(err)
		}
		if err := lib.CheckSubjectAgreement(msg.Subject, env, gateway.SupervisorAgreement(nil)); err != nil {
			t.Errorf("the gateway refuses the dispatcher's terminal: %v", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("no terminal on .supervisor")
	}
}

// A task id is unique only within its (profile, tier, kind), so the
// terminal's envelope id, which the TASKS stream deduplicates on, is keyed
// by all four: two profiles refusing the same task id each store their
// terminal, and a repeat publish of one (a redelivery after a lost ack) is
// still dropped by the duplicate window.
func TestTwoProfilesSharingATaskIDEachStoreTheirTerminal(t *testing.T) {
	b := newBus(t, "tse-0", "tse2-0")
	b.ensure("tse", 2, 0, "read")
	b.ensure("tse2", 2, 0, "read")
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.Profiles = append(c.Profiles, testProfile("tse2", 2)) })
	ti.pool.Set("tse2", "read", "tse2-0")
	d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	// No capability key: each is refused at admission with a terminal.
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "", map[string]string{})
	b.submitWith("tse2", "read", lib.SubmitKindTop, "task-1", "", map[string]string{})
	stored := func(profile string) uint64 {
		t.Helper()
		info, err := b.js.Stream(b.ctx, "TASKS")
		if err != nil {
			t.Fatal(err)
		}
		si, err := info.Info(b.ctx, jetstream.WithSubjectFilter(lib.TaskSupervisorSubject(profile, "task-1")))
		if err != nil {
			t.Fatal(err)
		}
		return si.State.Subjects[lib.TaskSupervisorSubject(profile, "task-1")]
	}
	waitFor(t, 15*time.Second, "both profiles' terminals stored", func() bool { return stored("tse") == 1 && stored("tse2") == 1 })
	hk := holdKey{"tse", "read", lib.SubmitKindTop, "task-1"}
	sub := submissionEnv(t, "tse", "task-1", "", map[string]string{})
	if err := d.publishTerminal(b.ctx, nil, hk, sub, lib.StateRejected, ReasonVerifierRefused, ""); err != nil {
		t.Fatal(err)
	}
	if n := stored("tse"); n != 1 {
		t.Errorf("a repeat terminal for the same task was stored again: %d stored", n)
	}
}

// Under the operator's switch (DispatchConfig.ProfileSupervisorForm, #5301)
// the terminal goes on the profile's six-token supervisor subject, and the
// gateway's agreement still takes it; without the switch it stays on the
// five-token one.
func TestTheTerminalTakesTheConfiguredSupervisorForm(t *testing.T) {
	for _, tc := range []struct {
		form, want string
	}{
		{"", lib.TaskSupervisorSubject("tse", "task-form")},
		{lib.ProfileSupervisorFormProfile, lib.ProfileSupervisorSubject("tse", "task-form")},
	} {
		t.Run("form="+tc.form, func(t *testing.T) {
			b := newBus(t, "tse-0")
			b.ensure("tse", 2, 0, "read")
			got := make(chan *nats.Msg, 4)
			sub, err := b.admin.ChanSubscribe("a2a.tasks.>", got)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = sub.Unsubscribe() })
			ti := oneProfile("tse", 2, "tse-0")
			ti.update(func(c *lib.DispatchConfig) { c.ProfileSupervisorForm = tc.form })
			b.runDispatcher(testConfig(), ti, &fakeVerifier{})
			b.submitWith("tse", "read", lib.SubmitKindTop, "task-form", "", map[string]string{})
			deadline := time.After(15 * time.Second)
			for {
				select {
				case msg := <-got:
					if !lib.IsTaskSupervisorSubject(msg.Subject, "tse", "task-form") {
						continue
					}
					if msg.Subject != tc.want {
						t.Fatalf("terminal on %q, want %q", msg.Subject, tc.want)
					}
					env, err := lib.ParseEnvelope(msg.Data)
					if err != nil {
						t.Fatal(err)
					}
					if err := lib.CheckSubjectAgreement(msg.Subject, env, gateway.SupervisorAgreement(nil)); err != nil {
						t.Errorf("the gateway refuses the dispatcher's terminal on %q: %v", msg.Subject, err)
					}
					return
				case <-deadline:
					t.Fatal("no terminal on a supervisor subject")
				}
			}
		})
	}
}

// noSixTokenUser is a dispatcher user refused the six-token supervisor form
// (a grant that hasn't reached its connection, #5301) and allowed the rest,
// the five-token form included.
func noSixTokenUser() []*natsserver.User {
	return []*natsserver.User{{Username: "disp-nosix", Password: dispPass, Permissions: &natsserver.Permissions{
		Publish: &natsserver.SubjectPermission{Allow: []string{">"}, Deny: []string{"a2a.tasks." + lib.TaskProfileSupervisorToken + ".>"}}}}}
}

// storedOn counts the TASKS messages on subject.
func (b *bus) storedOn(subject string) uint64 {
	b.t.Helper()
	info, err := b.js.Stream(b.ctx, "TASKS")
	if err != nil {
		b.t.Fatal(err)
	}
	si, err := info.Info(b.ctx, jetstream.WithSubjectFilter(subject))
	if err != nil {
		b.t.Fatal(err)
	}
	return si.State.Subjects[subject]
}

// A latched dispatcher (the six-token form) whose connection is refused that
// form never writes the terminal on the five-token one, which readers past
// the cutoff don't take as a profile task's (#5301). A refusal at admission
// (endUnstarted, which used to retry without end) retries until
// DroppedTerminalGiveUp, then settles without a terminal: the tombstone
// written, the submission terminated, the give-up counted.
func TestARefusedSixTokenTerminalIsBoundedAndNeverFallsBack(t *testing.T) {
	b := newBusWith(t, noSixTokenUser(), "tse-0")
	b.ensure("tse", 2, 0, "read")
	var g grantSwitch
	g.set("disp-nosix")
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.ProfileSupervisorForm = lib.ProfileSupervisorFormProfile })
	cfg := testConfig()
	cfg.DroppedTerminalGiveUp = time.Second
	d, _ := b.runPrepared(b.connectAs(&g), cfg, ti, &fakeVerifier{}, nil)
	// No capability key: refused at admission.
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-refused", "", map[string]string{})
	durable := lib.ProfileDurableName("tse", "read", lib.SubmitKindTop)
	waitFor(t, 60*time.Second, "the refused task settled without a terminal", func() bool {
		ack, num := b.pending(durable)
		return d.Health().SupervisorGiveUps == 1 && ack == 0 && num == 0
	})
	if n := b.storedOn(lib.TaskSupervisorSubject("tse", "task-refused")); n != 0 {
		t.Errorf("%d terminals on the five-token subject, want none: the refusal fell back", n)
	}
	if ts := b.terminalsOf("task-refused"); len(ts) != 0 {
		t.Errorf("terminals %+v, want none", ts)
	}
	key := lib.ProfileLeaseKey("tse", "read", lib.ProfileLeaseRefusedPod, "task-refused")
	if rec, ok := b.tombstones()[key]; !ok || !rec.Finished() {
		t.Errorf("no tombstone under %s: %v", key, b.records())
	}
}

// The held-task path (publishTerminalRetry) is bounded the same way on a
// latched install: a task whose worker is lost, with its six-token terminal
// refused, is settled after DroppedTerminalGiveUp: its slot freed, its
// tombstone written, the give-up counted, and nothing on either form.
func TestAHeldTasksRefusedTerminalSettlesAfterTheBound(t *testing.T) {
	b := newBusWith(t, noSixTokenUser(), "tse-0")
	b.ensure("tse", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, time.Hour)
	var g grantSwitch
	g.set("disp-nosix")
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.ProfileSupervisorForm = lib.ProfileSupervisorFormProfile })
	cfg := testConfig()
	cfg.DroppedTerminalGiveUp = time.Second
	d, _ := b.runPrepared(b.connectAs(&g), cfg, ti, &fakeVerifier{}, nil)
	b.submit("tse", "read", lib.SubmitKindTop, "task-held")
	waitFor(t, 10*time.Second, "the task running", func() bool { return d.Health().Running["tse"] == 1 })
	w.kill()
	waitFor(t, 60*time.Second, "the held task settled without a terminal", func() bool {
		return d.Health().SupervisorGiveUps == 1 && d.Health().Running["tse"] == 0 && len(b.leases()) == 0
	})
	for _, subject := range []string{lib.TaskSupervisorSubject("tse", "task-held"), lib.ProfileSupervisorSubject("tse", "task-held")} {
		if n := b.storedOn(subject); n != 0 {
			t.Errorf("%d messages on %s, want none", n, subject)
		}
	}
	if len(b.tombstones()) != 1 {
		t.Errorf("tombstones %v, want the settled task's", b.tombstones())
	}
}

// refusedErr is a terminal publish the server refused for its permissions,
// as publishTerminal wraps one.
func refusedErr() error {
	return fmt.Errorf("%w: %w", errSupervisorPublishRefused, context.DeadlineExceeded)
}

// Only a latched install gives up: an unlatched one (the five-token form)
// keeps retrying a refused terminal for as long as it takes, as it did before
// the six-token form, and a duplicate that isn't the task's terminal gives up
// at once whatever the bound. Only a permission refusal counts: a timeout
// with no refusal heard is retried, latched or not.
func TestOnlyALatchedInstallGivesUpOnARefusedTerminal(t *testing.T) {
	now := time.Now()
	for _, tc := range []struct {
		name string
		form string
		err  error
		want []bool // one decision per attempt, an hour apart
	}{
		{"unlatched", "", refusedErr(), []bool{false, false, false}},
		{"latched", lib.ProfileSupervisorFormProfile, refusedErr(), []bool{false, true}},
		{"latched, a timeout with no refusal", lib.ProfileSupervisorFormProfile, context.DeadlineExceeded, []bool{false, false, false}},
		{"planted duplicate", lib.ProfileSupervisorFormProfile, errTerminalUnpublished, []bool{true}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := testDispatchConfig(6, testProfile("tse", 2))
			cfg.ProfileSupervisorForm = tc.form
			clock := now
			d := &Dispatcher{cfg: testConfig(), src: NewStaticSource(cfg), log: quietLog(), now: func() time.Time { return clock }}
			var r terminalRefusal
			for i, want := range tc.want {
				if got := d.terminalGivenUp(d.log, &r, tc.err, lib.StateFailed, ReasonNoWorker); got != want {
					t.Fatalf("attempt %d: gave up = %v, want %v", i, got, want)
				}
				clock = clock.Add(time.Hour)
			}
			wantGiveUps := int64(0)
			if tc.want[len(tc.want)-1] {
				wantGiveUps = 1
			}
			if n := d.supervisorGiveUps.Load(); n != wantGiveUps {
				t.Errorf("supervisorGiveUps = %d, want %d", n, wantGiveUps)
			}
		})
	}
}

// allowDirect turns on direct gets on TASKS, as the provision Job creates it
// (--allow-direct).
func (b *bus) allowDirect() {
	b.t.Helper()
	s, err := b.js.Stream(b.ctx, "TASKS")
	if err != nil {
		b.t.Fatal(err)
	}
	c := s.CachedInfo().Config
	c.AllowDirect = true
	if _, err := b.js.UpdateStream(b.ctx, c); err != nil {
		b.t.Fatal(err)
	}
}

// The terminal's envelope id is predictable, and the TASKS duplicate window
// is keyed on the id alone, whatever the subject. A message stored first
// under that id on the five-token subject, which readers past the cutoff
// don't take as a profile task's terminal, must not swallow the real one: the
// duplicate ack is checked against the six-token subject, and with no
// terminal there the terminal is published again under a fresh id.
func TestAPlantedTerminalIDDoesNotSwallowTheSixTokenTerminal(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	b.allowDirect()
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.ProfileSupervisorForm = lib.ProfileSupervisorFormProfile })
	d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	hk := holdKey{"tse", "read", lib.SubmitKindTop, "task-planted"}
	sub := submissionEnv(t, "tse", "task-planted", "", map[string]string{})
	plant := nats.NewMsg(lib.TaskSupervisorSubject("tse", "task-planted"))
	plant.Header.Set(jetstream.MsgIDHeader, terminalEnvelopeID(hk))
	plant.Data = []byte(`{}`)
	if _, err := b.js.PublishMsg(b.ctx, plant); err != nil {
		t.Fatal(err)
	}
	var r terminalRefusal
	if err := d.publishTerminal(b.ctx, &r, hk, sub, lib.StateRejected, ReasonVerifierRefused, ""); err != nil {
		t.Fatalf("the terminal after a planted id: %v", err)
	}
	six := lib.ProfileSupervisorSubject("tse", "task-planted")
	if n := b.storedOn(six); n != 1 {
		t.Fatalf("%d terminals on %s, want the real one", n, six)
	}
	stored, err := d.storedTerminal(b.ctx, d.busFor("tse"), hk, six, r.freshID)
	if err != nil || !stored {
		t.Errorf("the six-token subject holds no dispatcher terminal: stored %v, err %v", stored, err)
	}
	// The terminal published again in the same retry loop (a retry after a
	// lost ack) is the dispatcher's own duplicate: confirmed on the subject
	// under the loop's fresh id, not stored twice.
	if err := d.publishTerminal(b.ctx, &r, hk, sub, lib.StateRejected, ReasonVerifierRefused, ""); err != nil {
		t.Fatal(err)
	}
	if n := b.storedOn(six); n != 1 {
		t.Errorf("%d terminals on %s after a repeat, want 1", n, six)
	}
}

// An ordinary repeat of a six-token terminal (a retry after a lost ack) is
// confirmed by the direct get and stores nothing more.
func TestARepeatSixTokenTerminalIsConfirmedNotRepublished(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	b.allowDirect()
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.ProfileSupervisorForm = lib.ProfileSupervisorFormProfile })
	d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	hk := holdKey{"tse", "read", lib.SubmitKindTop, "task-repeat"}
	sub := submissionEnv(t, "tse", "task-repeat", "", map[string]string{})
	for range 2 {
		if err := d.publishTerminal(b.ctx, nil, hk, sub, lib.StateRejected, ReasonVerifierRefused, ""); err != nil {
			t.Fatal(err)
		}
	}
	if n := b.storedOn(lib.ProfileSupervisorSubject("tse", "task-repeat")); n != 1 {
		t.Errorf("%d terminals stored, want 1", n)
	}
}

// A duplicate the dispatcher can't check (here TASKS has no direct gets; in
// production, a missing grant) is never taken as landed: the terminal is
// published again under a fresh id, so a planted id still doesn't swallow
// it.
func TestAnUncheckableDuplicateIsRepublishedNotTakenAsLanded(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.ProfileSupervisorForm = lib.ProfileSupervisorFormProfile })
	d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	hk := holdKey{"tse", "read", lib.SubmitKindTop, "task-blind"}
	sub := submissionEnv(t, "tse", "task-blind", "", map[string]string{})
	six := lib.ProfileSupervisorSubject("tse", "task-blind")
	if _, err := d.storedTerminal(b.ctx, d.busFor("tse"), hk, six, ""); err == nil {
		t.Fatal("the direct get worked; this test needs it to fail")
	}
	plant := nats.NewMsg(lib.TaskSupervisorSubject("tse", "task-blind"))
	plant.Header.Set(jetstream.MsgIDHeader, terminalEnvelopeID(hk))
	plant.Data = []byte(`{}`)
	if _, err := b.js.PublishMsg(b.ctx, plant); err != nil {
		t.Fatal(err)
	}
	if err := d.publishTerminal(b.ctx, nil, hk, sub, lib.StateRejected, ReasonVerifierRefused, ""); err != nil {
		t.Fatalf("the terminal after a planted id with no direct get: %v", err)
	}
	if n := b.storedOn(six); n != 1 {
		t.Fatalf("%d terminals on %s, want the real one re-published", n, six)
	}
}

// An outage is never a refusal (#5301): a NATS or TASKS outage, a partition,
// a full stream or a leader loss fails every attempt for as long as it lasts,
// and none of that counts toward the bound. Twenty minutes of such failures,
// on the fake clock, past a 15-minute bound, never give up; and an outage
// between two runs of refusals starts the bound again.
func TestAnOutageNeverCountsTowardTheGiveUp(t *testing.T) {
	outages := []error{
		nats.ErrDisconnected,
		nats.ErrConnectionClosed,
		nats.ErrNoResponders,
		jetstream.ErrNoStreamResponse,
		context.DeadlineExceeded,
		&jetstream.APIError{Code: 503, ErrorCode: jetstream.JSErrCodeJetStreamNotEnabled, Description: "JetStream system temporarily unavailable"},
		&jetstream.APIError{Code: 503, Description: "maximum messages exceeded"},
	}
	cfg := testDispatchConfig(6, testProfile("tse", 2))
	cfg.ProfileSupervisorForm = lib.ProfileSupervisorFormProfile
	clock := time.Now()
	d := &Dispatcher{cfg: testConfig(), src: NewStaticSource(cfg), log: quietLog(), now: func() time.Time { return clock }}
	if d.cfg.DroppedTerminalGiveUp != 15*time.Minute {
		t.Fatalf("the bound is %v, want the default 15m", d.cfg.DroppedTerminalGiveUp)
	}
	var r terminalRefusal
	for i := range 40 { // 20 minutes, an attempt each 30s
		err := outages[i%len(outages)]
		if d.terminalGivenUp(d.log, &r, err, lib.StateFailed, ReasonNoWorker) {
			t.Fatalf("gave up %v into an outage (%v)", time.Duration(i)*30*time.Second, err)
		}
		clock = clock.Add(30 * time.Second)
	}
	// Refusals for 10 minutes, an outage, then refusals again: the bound
	// counts from the second run, not the first.
	attempt := func(err error) bool {
		got := d.terminalGivenUp(d.log, &r, err, lib.StateFailed, ReasonNoWorker)
		clock = clock.Add(30 * time.Second)
		return got
	}
	for range 20 {
		if attempt(refusedErr()) {
			t.Fatal("gave up inside the bound")
		}
	}
	if attempt(nats.ErrNoResponders) {
		t.Fatal("gave up on an outage")
	}
	for range 30 { // the first refusal since the outage, and 14.5 minutes more
		if attempt(refusedErr()) {
			t.Fatal("an outage between refusals did not restart the bound")
		}
	}
	if !attempt(refusedErr()) {
		t.Error("15 minutes of refusals since the outage did not give up")
	}
	if n := d.supervisorGiveUps.Load(); n != 1 {
		t.Errorf("supervisorGiveUps = %d, want 1", n)
	}
}

// The server's refusal of a publish reaches nats.go as an async error naming
// the subject; that, and nothing else, is a refusal of the subject.
func TestOnlyAPublishPermissionViolationNamesARefusedSubject(t *testing.T) {
	six := lib.ProfileSupervisorSubject("tse", "task-1")
	for _, tc := range []struct {
		err  error
		want string
	}{
		{fmt.Errorf("%w: %s", nats.ErrPermissionViolation, `Permissions Violation for Publish to "`+six+`"`), six},
		{fmt.Errorf("%w: %s", nats.ErrPermissionViolation, `Permissions Violation for Publish with Reply of "_INBOX.x"`), ""},
		{fmt.Errorf("%w: %s", nats.ErrPermissionViolation, `Permissions Violation for Subscription to "`+six+`"`), ""},
		{errors.New(`Permissions Violation for Publish to "` + six + `"`), ""},
		{nats.ErrNoResponders, ""},
	} {
		got, ok := refusedPublishSubject(tc.err)
		if got != tc.want || ok != (tc.want != "") {
			t.Errorf("refusedPublishSubject(%v) = %q, %v; want %q", tc.err, got, ok, tc.want)
		}
	}
}

// A TASKS outage longer than the bound, on a latched install, gives up
// nothing: the stream is gone (every publish answered with no responders, no
// permission refused) for several times DroppedTerminalGiveUp, and once it is
// back the refusal's terminal lands on the six-token subject and the
// submission settles, with no give-up counted.
func TestATasksOutageLongerThanTheBoundLandsTheTerminalAfterIt(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	tasks, err := b.js.Stream(b.ctx, "TASKS")
	if err != nil {
		t.Fatal(err)
	}
	tasksCfg := tasks.CachedInfo().Config
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.ProfileSupervisorForm = lib.ProfileSupervisorFormProfile })
	cfg := testConfig()
	cfg.DroppedTerminalGiveUp = time.Second
	six := lib.ProfileSupervisorSubject("tse", "task-outage")
	tried := make(chan *nats.Msg, 64)
	watch, err := b.admin.ChanSubscribe(six, tried)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = watch.Unsubscribe() })
	d, _ := b.runDispatcher(cfg, ti, &fakeVerifier{})
	if err := b.js.DeleteStream(b.ctx, "TASKS"); err != nil {
		t.Fatal(err)
	}
	// No capability key: refused at admission, its terminal published into
	// the outage.
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-outage", "", map[string]string{})
	select {
	case <-tried:
	case <-time.After(15 * time.Second):
		t.Fatal("the refusal's terminal was never tried")
	}
	// The watch answers as a responder (the first attempt times out with no
	// refusal); without it each attempt meets no responders at once.
	if err := watch.Unsubscribe(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(kvOpTimeout + 4*cfg.DroppedTerminalGiveUp)
	if n := d.Health().SupervisorGiveUps; n != 0 {
		t.Fatalf("gave up %d terminals during an outage", n)
	}
	if _, err := b.js.CreateOrUpdateStream(b.ctx, tasksCfg); err != nil {
		t.Fatal(err)
	}
	durable := lib.ProfileDurableName("tse", "read", lib.SubmitKindTop)
	waitFor(t, 30*time.Second, "the terminal landed after the outage and the submission settled", func() bool {
		ack, num := b.pending(durable)
		return b.storedOn(six) == 1 && ack == 0 && num == 0
	})
	if n := d.Health().SupervisorGiveUps; n != 0 {
		t.Errorf("supervisorGiveUps = %d, want 0", n)
	}
}

// On a latched install, a profile that has left the install config keeps its
// own give-up: the dropped-profile log line, not terminalGivenUp's
// terminal-unpublished, even though its six-token terminal is refused too.
func TestADroppedProfileKeepsItsOwnGiveUpOnALatchedInstall(t *testing.T) {
	b := newBusWith(t, noSixTokenUser(), "tse-0")
	var g grantSwitch
	g.set("disp-nosix")
	cfg := testConfig()
	cfg.DroppedTerminalGiveUp = time.Second
	dropped := oneProfile("tse", 2, "tse-0") // tse2 is not in the config
	dropped.update(func(c *lib.DispatchConfig) { c.ProfileSupervisorForm = lib.ProfileSupervisorFormProfile })
	logs := &lockedBuffer{}
	log := slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelError}))
	d, err := New(cfg, b.connectAs(&g), dropped.src, dropped.pool, &fakeVerifier{}, log)
	if err != nil {
		t.Fatal(err)
	}
	h := &holding{profile: "tse2", tier: "read", kind: lib.SubmitKindTop, taskID: "task-1"}
	del := &envDelivery{env: submissionEnv(t, "tse2", "task-1", "", map[string]string{})}
	ctx, cancel := context.WithTimeout(b.ctx, 60*time.Second)
	defer cancel()
	if !d.publishTerminalRetry(ctx, log, del, h, lib.StateFailed, ReasonLeaseExpired, "") {
		t.Fatal("a dropped profile's refused terminal was not given up on")
	}
	if n := d.Health().SupervisorGiveUps; n != 0 {
		t.Errorf("supervisorGiveUps = %d, want 0: the dropped profile was labelled %s", n, ReasonTerminalUnpublished)
	}
	out := logs.String()
	if !strings.Contains(out, "profile left the install config") || strings.Contains(out, ReasonTerminalUnpublished) {
		t.Errorf("the give-up's log is not the dropped profile's own:\n%s", out)
	}
}

// lockedBuffer is a log sink safe for the dispatcher's goroutines.
type lockedBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (l *lockedBuffer) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.Write(p)
}

func (l *lockedBuffer) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.String()
}

// The six-token subject carries no tier or kind, so two tasks sharing a task
// id under one profile (another tier, or the other kind) share it. The
// read-back confirms a duplicate only by this terminal's own envelope id:
// another tier's terminal for the same task id is not this one landing.
func TestAnotherTiersTerminalDoesNotConfirmThisOne(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read", "write")
	b.allowDirect()
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.ProfileSupervisorForm = lib.ProfileSupervisorFormProfile })
	d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	read := holdKey{"tse", "read", lib.SubmitKindTop, "task-shared"}
	write := holdKey{"tse", "write", lib.SubmitKindTop, "task-shared"}
	sub := submissionEnv(t, "tse", "task-shared", "", map[string]string{})
	if err := d.publishTerminal(b.ctx, nil, read, sub, lib.StateRejected, ReasonVerifierRefused, ""); err != nil {
		t.Fatal(err)
	}
	six := lib.ProfileSupervisorSubject("tse", "task-shared")
	if stored, err := d.storedTerminal(b.ctx, d.busFor("tse"), read, six, ""); err != nil || !stored {
		t.Fatalf("the read tier's own terminal is not confirmed: stored %v, err %v", stored, err)
	}
	if stored, err := d.storedTerminal(b.ctx, d.busFor("tse"), write, six, ""); err != nil || stored {
		t.Errorf("the read tier's terminal confirmed the write tier's: stored %v, err %v", stored, err)
	}
}

// A re-publish under a fresh id (after a duplicate the dispatcher can't
// check) that fails transiently is an ordinary retry, not a give-up, and the
// retry re-publishes under the same fresh id: whether the failed send was
// stored (its ack lost) or not, exactly one terminal lands on the six-token
// subject, with no give-up and no Error log.
func TestATransientRepublishErrorRetriesUnderTheSameFreshID(t *testing.T) {
	for _, tc := range []struct {
		name   string
		stored bool // the failed re-publish was stored, its ack lost
	}{{"not stored", false}, {"ack lost", true}} {
		t.Run(tc.name, func(t *testing.T) {
			b := newBus(t, "tse-0")
			b.ensure("tse", 2, 0, "read")
			ti := oneProfile("tse", 2, "tse-0")
			ti.update(func(c *lib.DispatchConfig) { c.ProfileSupervisorForm = lib.ProfileSupervisorFormProfile })
			logs := &lockedBuffer{}
			log := slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelError}))
			d, err := New(testConfig(), b.connect("dispatcher"), ti.src, ti.pool, &fakeVerifier{}, log)
			if err != nil {
				t.Fatal(err)
			}
			h := &holding{profile: "tse", tier: "read", kind: lib.SubmitKindTop, taskID: "task-flaky"}
			hk := h.key()
			var mu sync.Mutex
			var freshIDs []string
			failed := false
			d.publishAck = func(ctx context.Context, c *lib.Client, subject string, env *lib.Envelope) (uint64, bool, error) {
				if env.EnvelopeID == terminalEnvelopeID(hk) {
					return c.PublishAck(ctx, subject, env)
				}
				mu.Lock()
				freshIDs = append(freshIDs, env.EnvelopeID)
				first := !failed
				failed = true
				mu.Unlock()
				if first {
					if tc.stored {
						if _, _, err := c.PublishAck(ctx, subject, env); err != nil {
							return 0, false, err
						}
					}
					return 0, false, nats.ErrTimeout
				}
				return c.PublishAck(ctx, subject, env)
			}
			// TASKS has no direct gets, so the planted id's duplicate can't
			// be checked and takes the re-publish.
			plant := nats.NewMsg(lib.TaskSupervisorSubject("tse", "task-flaky"))
			plant.Header.Set(jetstream.MsgIDHeader, terminalEnvelopeID(hk))
			plant.Data = []byte(`{}`)
			if _, err := b.js.PublishMsg(b.ctx, plant); err != nil {
				t.Fatal(err)
			}
			del := &envDelivery{env: submissionEnv(t, "tse", "task-flaky", "", map[string]string{})}
			ctx, cancel := context.WithTimeout(b.ctx, 30*time.Second)
			defer cancel()
			if !d.publishTerminalRetry(ctx, log, del, h, lib.StateFailed, ReasonLeaseExpired, "") {
				t.Fatal("the terminal never landed")
			}
			if n := b.storedOn(lib.ProfileSupervisorSubject("tse", "task-flaky")); n != 1 {
				t.Errorf("%d terminals on the six-token subject, want 1", n)
			}
			if n := d.Health().SupervisorGiveUps; n != 0 {
				t.Errorf("supervisorGiveUps = %d, want 0", n)
			}
			if out := logs.String(); out != "" {
				t.Errorf("Error logs on a transient re-publish error:\n%s", out)
			}
			mu.Lock()
			defer mu.Unlock()
			if len(freshIDs) != 2 || freshIDs[0] != freshIDs[1] {
				t.Errorf("re-publish ids %v, want the same fresh id twice", freshIDs)
			}
		})
	}
}
