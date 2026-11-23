package lib

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// cutoffReaderPolicy is the agreement policy the gateway and the MCP server
// read TASKS under: the gateway is the supervisor, the dispatcher another.
var cutoffReaderPolicy = AgreementPolicy{Supervisor: "gateway", OtherSupervisors: []string{DispatcherSession}}

// cutoffEnvelope is one status-update for taskID from writer, marshalled.
func cutoffEnvelope(t *testing.T, writer, taskID string, state TaskState, final bool) []byte {
	t.Helper()
	payload, err := json.Marshal(StatusUpdate{TaskID: taskID, ContextID: "ctx-" + taskID,
		Status: TaskStatus{State: state}, Final: final})
	if err != nil {
		t.Fatal(err)
	}
	env, err := NewStatusUpdateEnvelope(Party{Session: writer}, taskID, "ctx-"+taskID, "corr-"+taskID, payload)
	if err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(env)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

// The writes on one task, by when they land against the cutoff.
type cutoffWrites struct {
	working        bool // an executor's working status, before the cutoff
	fiveBefore     bool // a five-token terminal stored before the cutoff
	fiveAfter      bool // a five-token terminal stored after the cutoff
	sixTokenAfter  bool // a six-token terminal stored after the cutoff
	eventsTerminal bool // the executor's own terminal, after the cutoff
}

// publishAroundCutoff publishes every task's before-writes, takes the cutoff,
// then publishes the after-writes, with the server's clock (the stored time)
// on each side of it. A five-token write after the cutoff carries a
// Nats-Time-Stamp header of its own, an hour before the cutoff: what a
// publisher sets to pass for an older message on a read that takes the first
// such header (storedTimeOf).
func publishAroundCutoff(t *testing.T, url string, tasks map[[2]string]cutoffWrites) time.Time {
	t.Helper()
	for key, w := range tasks {
		addressee, taskID := key[0], key[1]
		if w.working {
			publishRaw(t, url, TaskEventsSubject(addressee, taskID), cutoffEnvelope(t, addressee, taskID, StateWorking, false))
		}
		if w.fiveBefore {
			publishRaw(t, url, TaskSupervisorSubject(addressee, taskID), cutoffEnvelope(t, DispatcherSession, taskID, StateFailed, true))
		}
	}
	time.Sleep(20 * time.Millisecond)
	cutoff := time.Now()
	time.Sleep(20 * time.Millisecond)
	for key, w := range tasks {
		addressee, taskID := key[0], key[1]
		if w.fiveAfter {
			// The forgery PR 5 has to rule out: the gateway's own word on a
			// profile task, on the form its grant still reaches.
			publishForgedTime(t, url, TaskSupervisorSubject(addressee, taskID), cutoffEnvelope(t, "gateway", taskID, StateFailed, true), cutoff.Add(-time.Hour))
		}
		if w.sixTokenAfter {
			publishRaw(t, url, ProfileSupervisorSubject(addressee, taskID), cutoffEnvelope(t, DispatcherSession, taskID, StateFailed, true))
		}
		if w.eventsTerminal {
			publishRaw(t, url, TaskEventsSubject(addressee, taskID), cutoffEnvelope(t, addressee, taskID, StateCompleted, true))
		}
	}
	return cutoff
}

// publishForgedTime publishes data on subject with a Nats-Time-Stamp header
// of the publisher's own, set to at.
func publishForgedTime(t *testing.T, url, subject string, data []byte, at time.Time) {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	msg := nats.NewMsg(subject)
	msg.Data = data
	msg.Header.Set(jetstream.TimeStampHeaer, at.UTC().Format(time.RFC3339Nano))
	if _, err := js.PublishMsg(testCtx(t), msg); err != nil {
		t.Fatalf("publish with a forged time: %v", err)
	}
}

// provisionCutoffTasksStream is TASKS as the provision script creates it,
// allow_direct included, so a direct get is the DIRECT.GET API the gateway's
// grant names; direct false is the stream without it, where a direct get is
// STREAM.MSG.GET.
func provisionCutoffTasksStream(t *testing.T, url string, direct bool) {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	if _, err := js.CreateOrUpdateStream(testCtx(t), jetstream.StreamConfig{
		Name: TasksStream, Subjects: []string{"a2a.tasks.>"}, Retention: jetstream.LimitsPolicy,
		MaxAge: 72 * time.Hour, AllowDirect: direct,
	}); err != nil {
		t.Fatalf("create TASKS stream: %v", err)
	}
}

// The gate's two operator inputs as the tests use them: a profile named like
// a session token, and a session's addressee named like a profile.
const (
	cutoffProfileName = "cto-otter-1a2b"
	cutoffSessionName = "fleet-audit"
)

// cutoffReader is a reader with the cutoff (none when zero) and the profile
// names; names nil gives it none.
func cutoffReader(t *testing.T, url string, cutoff time.Time, names *ProfileNames) *Client {
	t.Helper()
	opts := []ClientOption{WithName("cutoff-reader"), WithAgreementPolicy(cutoffReaderPolicy)}
	if !cutoff.IsZero() {
		opts = append(opts, WithProfileSupervisorCutoff(cutoff))
	}
	if names != nil {
		opts = append(opts, WithProfileNames(names))
	}
	c, err := Connect(testCtx(t), url, opts...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Close)
	return c
}

// A five-token supervisor write is not a profile task's terminal on a latched
// reader once its stored time is past the cutoff; the same write under an
// addressee that is no profile still is (#5301, gate 2 on the flip). Which
// addressees are profiles is the operator's profile names and nothing else:
// the profile's name is session-shaped and the session's profile-shaped, so
// the name's shape decides nothing. Every row is read by the replay and by
// the direct gets the gateway's busy count and no-first-event notice use, on
// a stream with allow_direct and on one without, so TaskNotFound and "no
// heads" stay one answer on both. Each five-token write after the cutoff
// carries a forged Nats-Time-Stamp an hour before it, and is refused on
// every path.
func TestAFiveTokenWriteIsNotAProfileTasksTerminalOnALatchedInstall(t *testing.T) {
	type read struct {
		profile  bool
		latched  bool
		final    bool
		notFound bool
		subject  func(addressee, taskID string) string // the terminal's subject; nil when not final
	}
	type row struct {
		name   string
		writes cutoffWrites
		reads  []read
	}
	five, six, events := TaskSupervisorSubject, ProfileSupervisorSubject, TaskEventsSubject
	rows := []row{
		{name: "five-token after the cutoff", writes: cutoffWrites{working: true, fiveAfter: true}, reads: []read{
			{profile: true, latched: true, final: false},
			{profile: false, latched: true, final: true, subject: five},
			{profile: true, latched: false, final: true, subject: five},
			{profile: false, latched: false, final: true, subject: five},
		}},
		{name: "five-token after the cutoff, nothing else", writes: cutoffWrites{fiveAfter: true}, reads: []read{
			{profile: true, latched: true, notFound: true},
			{profile: false, latched: true, final: true, subject: five},
			{profile: true, latched: false, final: true, subject: five},
		}},
		{name: "five-token before the cutoff", writes: cutoffWrites{working: true, fiveBefore: true}, reads: []read{
			{profile: true, latched: true, final: true, subject: five},
			{profile: false, latched: true, final: true, subject: five},
		}},
		{name: "six-token after the cutoff", writes: cutoffWrites{working: true, sixTokenAfter: true}, reads: []read{
			{profile: true, latched: true, final: true, subject: six},
			{profile: false, latched: true, final: true, subject: six},
			{profile: true, latched: false, final: true, subject: six},
		}},
		// The five-token write lands first: on the profile's task the
		// executor's own terminal behind it is the end; on the session's
		// the supervisor's is, and the executor's is the post-final drop.
		{name: "five-token after the cutoff ahead of the executor's terminal", writes: cutoffWrites{working: true, eventsTerminal: true, fiveAfter: true}, reads: []read{
			{profile: true, latched: true, final: true, subject: events},
			{profile: false, latched: true, final: true, subject: five},
		}},
	}
	for _, direct := range []bool{true, false} {
		for _, r := range rows {
			t.Run(map[bool]string{true: "allow_direct", false: "no allow_direct"}[direct]+"/"+r.name, func(t *testing.T) {
				s := startServer(t)
				url := clientURL(s)
				provisionCutoffTasksStream(t, url, direct)
				tasks := map[[2]string]cutoffWrites{}
				for _, profile := range []bool{true, false} {
					tasks[[2]string{cutoffAddressee(profile), "task-1"}] = r.writes
				}
				cutoff := publishAroundCutoff(t, url, tasks)
				names := NewProfileNames(cutoffProfileName)
				latched, unlatched := cutoffReader(t, url, cutoff, names), cutoffReader(t, url, time.Time{}, names)
				ctx := testCtx(t)
				for _, rd := range r.reads {
					c := unlatched
					if rd.latched {
						c = latched
					}
					addressee := cutoffAddressee(rd.profile)
					what := addressee + map[bool]string{true: " on a latched reader", false: " on an unlatched reader"}[rd.latched]
					task, subject, _, err := c.TasksGetTerminal(ctx, addressee, "task-1")
					heads, herr := c.TaskReplayHeads(ctx, nil, addressee, "task-1")
					if herr != nil {
						t.Fatalf("%s: TaskReplayHeads: %v", what, herr)
					}
					empty, eerr := c.TaskReplayEmpty(ctx, nil, addressee, "task-1")
					if eerr != nil {
						t.Fatalf("%s: TaskReplayEmpty: %v", what, eerr)
					}
					if empty != (len(heads) == 0) {
						t.Errorf("%s: TaskReplayEmpty = %v with %d heads", what, empty, len(heads))
					}
					headFinal := false
					for _, h := range heads {
						env, perr := ParseEnvelope(h.Data)
						headFinal = headFinal || (perr == nil && IsFinalStatus(env))
					}
					if rd.notFound {
						var aerr *A2AError
						if !errors.As(err, &aerr) || aerr.Code != CodeTaskNotFound {
							t.Errorf("%s: read = %+v, %v; want TaskNotFound", what, task, err)
						}
						if len(heads) != 0 {
							t.Errorf("%s: direct gets found %d heads where the replay found no task", what, len(heads))
						}
						continue
					}
					if err != nil {
						t.Fatalf("%s: %v", what, err)
					}
					if task.Final != rd.final {
						t.Errorf("%s: final = %v (%s), want %v", what, task.Final, task.State, rd.final)
					}
					if len(heads) == 0 {
						t.Errorf("%s: direct gets found no heads on a task the replay folded", what)
					}
					if headFinal != rd.final {
						t.Errorf("%s: direct gets read final = %v, want %v", what, headFinal, rd.final)
					}
					want := ""
					if rd.subject != nil {
						want = rd.subject(addressee, "task-1")
					}
					if subject != want {
						t.Errorf("%s: terminal subject = %q, want %q", what, subject, want)
					}
				}
				if r.writes.fiveAfter && latched.ProfileSupervisorDrops() == 0 {
					t.Error("the latched reader refused a five-token write and counted nothing")
				}
				if unlatched.ProfileSupervisorDrops() != 0 {
					t.Errorf("the unlatched reader counted %d drops", unlatched.ProfileSupervisorDrops())
				}
			})
		}
	}
}

func cutoffAddressee(profile bool) string {
	if profile {
		return cutoffProfileName
	}
	return cutoffSessionName
}

// A five-token terminal published on a profile's task after the cutoff with
// a Nats-Time-Stamp header of its own, set before the cutoff, is refused on
// the replay and on the direct get, with and without allow_direct. On a
// direct get the server appends its own Nats-Time-Stamp after the
// publisher's, so the read sees two and takes neither: the time is unknown,
// and an unknown time is refused. The direct get's own Time field, which the
// client parses from the first header, is the forged one, which is why the
// read doesn't use it.
func TestAForgedStoredTimeDoesNotPassTheCutoff(t *testing.T) {
	for _, direct := range []bool{true, false} {
		t.Run(map[bool]string{true: "allow_direct", false: "no allow_direct"}[direct], func(t *testing.T) {
			s := startServer(t)
			url := clientURL(s)
			provisionCutoffTasksStream(t, url, direct)
			cutoff := time.Now().Add(-time.Minute)
			subject := TaskSupervisorSubject(cutoffProfileName, "task-forged")
			publishForgedTime(t, url, subject, cutoffEnvelope(t, "gateway", "task-forged", StateFailed, true), cutoff.Add(-time.Hour))
			c := cutoffReader(t, url, cutoff, NewProfileNames(cutoffProfileName))
			ctx := testCtx(t)
			if _, err := c.TasksGet(ctx, cutoffProfileName, "task-forged"); !isTaskNotFoundErr(err) {
				t.Errorf("replay: %v, want TaskNotFound", err)
			}
			heads, err := c.TaskReplayHeads(ctx, nil, cutoffProfileName, "task-forged")
			if err != nil || len(heads) != 0 {
				t.Errorf("direct get: heads %+v, %v; want none", heads, err)
			}
			if empty, err := c.TaskReplayEmpty(ctx, nil, cutoffProfileName, "task-forged"); err != nil || !empty {
				t.Errorf("direct get: empty = %v, %v; want empty", empty, err)
			}
			if direct {
				// The forgery reaches the client's own parse: that is the
				// time the read must not take.
				_, js := c.conn()
				st, err := js.Stream(ctx, TasksStream)
				if err != nil {
					t.Fatal(err)
				}
				raw, err := st.GetLastMsgForSubject(ctx, subject)
				if err != nil {
					t.Fatal(err)
				}
				if !raw.Time.Before(cutoff) || len(raw.Header.Values(jetstream.TimeStampHeaer)) != 2 {
					t.Errorf("direct get: Time %v, stamps %v; want the forged time first of two", raw.Time, raw.Header.Values(jetstream.TimeStampHeaer))
				}
			}
			if c.ProfileSupervisorDrops() < 3 {
				t.Errorf("drops = %d, want one per read", c.ProfileSupervisorDrops())
			}
		})
	}
}

func isTaskNotFoundErr(err error) bool {
	var aerr *A2AError
	return errors.As(err, &aerr) && aerr.Code == CodeTaskNotFound
}

// Profile-ness is the operator's names and nothing else. With a cutoff and no
// names, or names never loaded, every addressee reads as a profile, so a
// five-token message after the cutoff is refused under any name: the gate
// fails closed.
func TestWithNoProfileNamesEveryAddresseeIsAProfile(t *testing.T) {
	s := startServer(t)
	url := clientURL(s)
	provisionCutoffTasksStream(t, url, true)
	cutoff := time.Now().Add(-time.Minute)
	publishRaw(t, url, TaskSupervisorSubject(cutoffSessionName, "task-1"), cutoffEnvelope(t, "gateway", "task-1", StateFailed, true))
	ctx := testCtx(t)
	for name, names := range map[string]*ProfileNames{"no names": nil, "names never loaded": {}} {
		c := cutoffReader(t, url, cutoff, names)
		if _, err := c.TasksGet(ctx, cutoffSessionName, "task-1"); !isTaskNotFoundErr(err) {
			t.Errorf("%s: replay = %v, want the five-token write refused", name, err)
		}
	}
	c := cutoffReader(t, url, cutoff, NewProfileNames(cutoffProfileName))
	if task, err := c.TasksGet(ctx, cutoffSessionName, "task-1"); err != nil || !task.Final {
		t.Errorf("with names that leave it out: %+v, %v; want the session's terminal", task, err)
	}
}

// An unlatched install reads every task exactly as before the cutoff existed:
// a reader with no cutoff gives the same fold, terminal subject and stored
// time whatever profile names it holds, and so does a reader handed the zero
// cutoff, for every shape of write. This is the no-regression guarantee that
// makes the change inert until the operator renders the cutoff.
func TestAnUnlatchedReaderReadsBothFormsAsBefore(t *testing.T) {
	s := startServer(t)
	url := clientURL(s)
	provisionCutoffTasksStream(t, url, true)
	shapes := map[string]cutoffWrites{
		"five-before":     {working: true, fiveBefore: true},
		"five-after":      {working: true, fiveAfter: true},
		"five-only":       {fiveAfter: true},
		"six-after":       {working: true, sixTokenAfter: true},
		"events-terminal": {working: true, eventsTerminal: true, fiveAfter: true},
		"running":         {working: true},
	}
	tasks := map[[2]string]cutoffWrites{}
	for name, w := range shapes {
		tasks[[2]string{cutoffProfileName, name}] = w
	}
	publishAroundCutoff(t, url, tasks)
	plain := cutoffReader(t, url, time.Time{}, nil)
	ctx := testCtx(t)
	type answer struct {
		Task     []byte
		Subject  string
		StoredAt time.Time
		Err      string
		Heads    []ReplayHead
	}
	readAs := func(c *Client, taskID string) answer {
		task, subject, at, err := c.TasksGetTerminal(ctx, cutoffProfileName, taskID)
		var a answer
		a.Subject, a.StoredAt = subject, at
		if err != nil {
			a.Err = err.Error()
		}
		if a.Task, err = json.Marshal(task); err != nil {
			t.Fatal(err)
		}
		if a.Heads, err = c.TaskReplayHeads(ctx, nil, cutoffProfileName, taskID); err != nil {
			t.Fatal(err)
		}
		return a
	}
	readers := map[string]*Client{
		"no cutoff, the name a profile's":   cutoffReader(t, url, time.Time{}, NewProfileNames(cutoffProfileName)),
		"no cutoff, names never loaded":     cutoffReader(t, url, time.Time{}, &ProfileNames{}),
		"zero cutoff, the name a profile's": cutoffReader(t, url, time.Time{}, NewProfileNames(cutoffProfileName)),
	}
	for name := range shapes {
		want := readAs(plain, name)
		for label, c := range readers {
			got := readAs(c, name)
			if !bytes.Equal(got.Task, want.Task) || got.Subject != want.Subject || !got.StoredAt.Equal(want.StoredAt) ||
				got.Err != want.Err || !reflect.DeepEqual(got.Heads, want.Heads) {
				t.Errorf("%s: %s read differs from the plain read:\n got %+v\nwant %+v", name, label, got, want)
			}
		}
	}
	for label, c := range readers {
		if c.ProfileSupervisorDrops() != 0 {
			t.Errorf("%s: an unlatched reader counted %d drops", label, c.ProfileSupervisorDrops())
		}
	}
}

// The predicate refuses one case only: a profile's addressee, the five-token
// form, at or after the cutoff, or at a time the server didn't give.
func TestAcceptsSupervisorEvent(t *testing.T) {
	cutoff := time.Date(2026, 11, 23, 12, 0, 0, 0, time.UTC)
	before, after := cutoff.Add(-time.Second), cutoff.Add(time.Second)
	five, six := TaskSupervisorSubject("fleet-audit", "t1"), ProfileSupervisorSubject("fleet-audit", "t1")
	for _, c := range []struct {
		name     string
		cutoff   time.Time
		subject  string
		storedAt time.Time
		profile  bool
		want     bool
	}{
		{"profile, five-token, after", cutoff, five, after, true, false},
		{"profile, five-token, at the cutoff", cutoff, five, cutoff, true, false},
		{"profile, five-token, before", cutoff, five, before, true, true},
		{"profile, five-token, no stored time", cutoff, five, time.Time{}, true, false},
		{"session, five-token, after", cutoff, five, after, false, true},
		{"session, five-token, no stored time", cutoff, five, time.Time{}, false, true},
		{"profile, six-token, after", cutoff, six, after, true, true},
		{"profile, events, after", cutoff, TaskEventsSubject("fleet-audit", "t1"), after, true, true},
		{"profile, five-token addressed to p, after", cutoff, TaskSupervisorSubject("p", "t1"), after, true, false},
		{"unlatched", time.Time{}, five, after, true, true},
		{"unlatched, no stored time", time.Time{}, five, time.Time{}, true, true},
	} {
		if got := AcceptsSupervisorEvent(c.cutoff, c.subject, c.storedAt, c.profile); got != c.want {
			t.Errorf("%s: AcceptsSupervisorEvent = %v, want %v", c.name, got, c.want)
		}
	}
}

// The client's gate reads the addressee off the subject and asks the
// operator's names alone.
func TestAcceptSupervisorEventAsksTheProfileNames(t *testing.T) {
	cutoff := time.Now().Add(-time.Minute)
	after := cutoff.Add(30 * time.Second)
	c := &Client{opts: clientOptions{profileSupervisorCutoff: cutoff, profileNames: NewProfileNames(cutoffProfileName)}}
	if c.AcceptSupervisorEvent(TaskSupervisorSubject(cutoffProfileName, "t1"), after) {
		t.Error("a five-token message after the cutoff on a profile's addressee was accepted")
	}
	if !c.AcceptSupervisorEvent(TaskSupervisorSubject(cutoffSessionName, "t1"), after) {
		t.Error("a five-token message on an addressee that is no profile was refused")
	}
	if !c.AcceptSupervisorEvent(ProfileSupervisorSubject(cutoffProfileName, "t1"), after) {
		t.Error("the six-token form was refused")
	}
	if c.ProfileSupervisorDrops() != 1 {
		t.Errorf("drops = %d, want 1", c.ProfileSupervisorDrops())
	}
}

// The env reads absent and empty as unset, an RFC3339 time as the cutoff,
// and anything else as latched at the process's start, with an Error line
// naming the variable and the source saying so: a value that's there at all
// is the operator saying the install latched.
func TestProfileSupervisorCutoffFromEnv(t *testing.T) {
	want := time.Date(2026, 11, 23, 12, 10, 0, 0, time.UTC)
	start := time.Date(2026, 11, 23, 15, 0, 0, 0, time.UTC)
	for _, c := range []struct {
		value   string
		want    time.Time
		source  string
		errLine bool
	}{
		{"", time.Time{}, CutoffSourceUnset, false},
		{"   ", time.Time{}, CutoffSourceUnset, false},
		{"2026-11-23T12:10:00Z", want, CutoffSourceEnv, false},
		{" 2026-11-23T13:10:00+01:00 ", want, CutoffSourceEnv, false},
		{"2026-11-23 12:10:00", start, CutoffSourceMalformed, true},
		{"yes", start, CutoffSourceMalformed, true},
	} {
		var buf bytes.Buffer
		log := slog.New(slog.NewTextHandler(&buf, nil))
		got := ProfileSupervisorCutoffFromEnv(func(k string) string {
			if k == EnvProfileSupervisorCutoff {
				return c.value
			}
			return ""
		}, start, log)
		if !got.Cutoff.Equal(c.want) || got.Source != c.source {
			t.Errorf("%q: cutoff = %v (%s), want %v (%s)", c.value, got.Cutoff, got.Source, c.want, c.source)
		}
		if isErr := strings.Contains(buf.String(), "level=ERROR") && strings.Contains(buf.String(), EnvProfileSupervisorCutoff); isErr != c.errLine {
			t.Errorf("%q: error line = %v, want %v (log %q)", c.value, isErr, c.errLine, buf.String())
		}
	}
}

// The profile names file: the operator's JSON list of DNS-1123 labels,
// refused whole when it is anything else.
func TestParseProfileNames(t *testing.T) {
	for _, c := range []struct {
		raw  string
		want []string
		ok   bool
	}{
		{`{"profiles":["a","fleet-audit"]}`, []string{"a", "fleet-audit"}, true},
		{`{"profiles":[]}`, []string{}, true},
		{`{}`, nil, false},
		{`{"profiles":["a.b"]}`, nil, false},
		{`{"profiles":["*"]}`, nil, false},
		{`{"profiles":["a"],"extra":1}`, nil, false},
		{`not json`, nil, false},
		{``, nil, false},
	} {
		got, err := ParseProfileNames([]byte(c.raw))
		if (err == nil) != c.ok || (c.ok && !reflect.DeepEqual(got, c.want)) {
			t.Errorf("%q: %v, %v; want %v ok=%v", c.raw, got, err, c.want, c.ok)
		}
	}
}

// The gate from the env: no cutoff starts no names; a cutoff with a names
// file loads it at once and picks a change up; a garbled file keeps the names
// last read; and a cutoff with an unreadable file, or none named, reads every
// addressee as a profile, logged at Error.
func TestProfileSupervisorGateFromEnv(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "profiles.json")
	env := map[string]string{}
	getenv := func(k string) string { return env[k] }
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	var buf bytes.Buffer
	log := slog.New(slog.NewTextHandler(&buf, nil))
	start := time.Now()

	if g := ProfileSupervisorGateFromEnv(ctx, getenv, start, log); !g.Cutoff.IsZero() || g.Names != nil || g.Source != CutoffSourceUnset {
		t.Errorf("unset: %+v, want no cutoff and no names", g)
	}

	env[EnvProfileSupervisorCutoff] = "2026-11-23T12:10:00Z"
	g := ProfileSupervisorGateFromEnv(ctx, getenv, start, log)
	if g.Names != nil || !strings.Contains(buf.String(), "no profile names file") {
		t.Errorf("a cutoff with no names file: names %+v, log %q", g.Names, buf.String())
	}
	if c := (&Client{opts: clientOptions{profileNames: g.Names}}); !c.IsProfileAddressee("anything") {
		t.Error("with no names every addressee must read as a profile")
	}

	env[EnvProfileNamesFile] = path
	buf.Reset()
	g = ProfileSupervisorGateFromEnv(ctx, getenv, start, log)
	if g.Names.Loaded() || !g.Names.Has("anything") || !strings.Contains(buf.String(), "level=ERROR") {
		t.Errorf("an unreadable file: loaded %v, log %q; want fail closed at Error", g.Names.Loaded(), buf.String())
	}

	if err := os.WriteFile(path, []byte(`{"profiles":["fleet-audit"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	g = ProfileSupervisorGateFromEnv(ctx, getenv, start, log)
	if !g.Names.Loaded() || !g.Names.Has("fleet-audit") || g.Names.Has("cto-otter-1a2b") {
		t.Errorf("loaded file: loaded %v, fleet-audit %v, cto-otter-1a2b %v", g.Names.Loaded(), g.Names.Has("fleet-audit"), g.Names.Has("cto-otter-1a2b"))
	}

	if err := os.WriteFile(path, []byte(`{"profiles":["fleet-audit","cto-otter-1a2b"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if changed, err := g.Names.load(path); err != nil || !changed || !g.Names.Has("cto-otter-1a2b") {
		t.Errorf("a changed file: changed %v, %v; want the new name read", changed, err)
	}
	if err := os.WriteFile(path, []byte(`garbled`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := g.Names.load(path); err == nil || !g.Names.Has("cto-otter-1a2b") || g.Names.Has("other") {
		t.Errorf("a garbled file: %v; want an error and the names last read kept", err)
	}
}
