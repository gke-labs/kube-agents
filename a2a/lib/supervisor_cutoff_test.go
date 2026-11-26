package lib

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
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
// the replay and on the direct get, with and without allow_direct. A direct
// get takes no time from its headers at all (storedTimeOf): its own Time
// field, which the client parses from the first stamp, is the forged one.
// The read takes the server's time from a consumer instead, which is after
// the cutoff.
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
// and anything else as latched at the unix epoch, with an Error line naming
// the variable and the source saying so: a value that's there at all is the
// operator saying the install latched.
func TestProfileSupervisorCutoffFromEnv(t *testing.T) {
	want := time.Date(2026, 11, 23, 12, 10, 0, 0, time.UTC)
	start := MalformedCutoff
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
		}, log)
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

	if g := ProfileSupervisorGateFromEnv(ctx, getenv, log); !g.Cutoff.IsZero() || g.Names != nil || g.Source != CutoffSourceUnset {
		t.Errorf("unset: %+v, want no cutoff and no names", g)
	}

	env[EnvProfileSupervisorCutoff] = "2026-11-23T12:10:00Z"
	g := ProfileSupervisorGateFromEnv(ctx, getenv, log)
	if g.Names != nil || !strings.Contains(buf.String(), "no profile names file") {
		t.Errorf("a cutoff with no names file: names %+v, log %q", g.Names, buf.String())
	}
	if c := (&Client{opts: clientOptions{profileNames: g.Names}}); !c.IsProfileAddressee("anything") {
		t.Error("with no names every addressee must read as a profile")
	}

	env[EnvProfileNamesFile] = path
	buf.Reset()
	g = ProfileSupervisorGateFromEnv(ctx, getenv, log)
	if g.Names.Loaded() || !g.Names.Has("anything") || !strings.Contains(buf.String(), "level=ERROR") {
		t.Errorf("an unreadable file: loaded %v, log %q; want fail closed at Error", g.Names.Loaded(), buf.String())
	}

	if err := os.WriteFile(path, []byte(`{"profiles":["fleet-audit"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	g = ProfileSupervisorGateFromEnv(ctx, getenv, log)
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

// publishHeaderBlock publishes data on subject over the raw client protocol
// with hdr as its header block, byte for byte: nats.go's header encoding
// would refuse or clean what a raw HPUB can carry. It waits for the server's
// PONG, so the publish has been processed, then for the stream to hold it.
func publishHeaderBlock(t *testing.T, url, subject, hdr string, data []byte) {
	t.Helper()
	conn, err := net.DialTimeout("tcp", strings.TrimPrefix(url, "nats://"), 5*time.Second)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	rd := bufio.NewReader(conn)
	if line, err := rd.ReadString('\n'); err != nil || !strings.HasPrefix(line, "INFO ") {
		t.Fatalf("server greeting %q: %v", line, err)
	}
	var b strings.Builder
	b.WriteString(`CONNECT {"verbose":false,"pedantic":false,"headers":true,"protocol":1}` + "\r\n")
	fmt.Fprintf(&b, "HPUB %s %d %d\r\n%s%s\r\nPING\r\n", subject, len(hdr), len(hdr)+len(data), hdr, data)
	if _, err := conn.Write([]byte(b.String())); err != nil {
		t.Fatalf("write: %v", err)
	}
	for {
		line, err := rd.ReadString('\n')
		if err != nil {
			t.Fatalf("waiting for PONG: %v", err)
		}
		if strings.HasPrefix(line, "-ERR") {
			t.Fatalf("server refused the publish: %s", line)
		}
		if strings.HasPrefix(line, "PONG") {
			break
		}
	}
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	waitFor(t, 5*time.Second, "the raw publish stored", func() bool {
		st, err := js.Stream(testCtx(t), TasksStream)
		if err != nil {
			return false
		}
		info, err := st.Info(testCtx(t), jetstream.WithSubjectFilter(subject))
		return err == nil && info.State.Subjects[subject] > 0
	})
}

// blankLineStampBlock is a header block that ends itself early: the headers a
// direct get's reply needs, a Nats-Time-Stamp of the publisher's own set to
// at, then a blank line, then one more header so the block still ends as the
// server expects. The server appends its own headers after the last line;
// the client's parse stops at the blank line and never sees them.
func blankLineStampBlock(subject string, at time.Time) string {
	return blankLineBlock(subject, 1, at)
}

// blankLineBlock is blankLineStampBlock with the publisher's Nats-Sequence
// set to seq as well.
func blankLineBlock(subject string, seq uint64, at time.Time) string {
	return "NATS/1.0\r\n" +
		"Nats-Stream: " + TasksStream + "\r\n" +
		"Nats-Subject: " + subject + "\r\n" +
		"Nats-Sequence: " + fmt.Sprint(seq) + "\r\n" +
		"Nats-Time-Stamp: " + at.UTC().Format(time.RFC3339Nano) + "\r\n" +
		"\r\n" +
		"X-Pad: 1\r\n\r\n"
}

// The forgery the header count couldn't catch (H1 on #6885): a five-token
// terminal on a profile's task, published after the cutoff with a blank line
// inside its own header block and a Nats-Time-Stamp before the cutoff ahead
// of it. A direct get on a stream with allow_direct shows the client exactly
// one stamp, the forged one, and the read must still refuse it: the direct
// get gives no time, and the consumer read that replaces it gives the
// server's. The replay refuses it too, and the task with nothing else reads
// as no task on every path.
func TestABlankLineInTheHeaderBlockDoesNotPassTheCutoff(t *testing.T) {
	s := startServer(t)
	url := clientURL(s)
	provisionCutoffTasksStream(t, url, true)
	cutoff := time.Now().Add(-time.Minute)
	subject := TaskSupervisorSubject(cutoffProfileName, "task-blank")
	forged := cutoff.Add(-time.Hour)
	publishHeaderBlock(t, url, subject, blankLineStampBlock(subject, forged), cutoffEnvelope(t, "gateway", "task-blank", StateFailed, true))
	c := cutoffReader(t, url, cutoff, NewProfileNames(cutoffProfileName))
	ctx := testCtx(t)

	// The forgery reaches the client as one stamp, the publisher's.
	_, js := c.conn()
	st, err := js.Stream(ctx, TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := st.GetLastMsgForSubject(ctx, subject)
	if err != nil {
		t.Fatal(err)
	}
	if stamps := raw.Header.Values(jetstream.TimeStampHeaer); len(stamps) != 1 || !raw.Time.Equal(forged) {
		t.Fatalf("direct get: stamps %v, Time %v; want the forged time alone (the server no longer hides its own?)", stamps, raw.Time)
	}

	heads, err := c.TaskReplayHeads(ctx, nil, cutoffProfileName, "task-blank")
	if err != nil || len(heads) != 0 {
		t.Errorf("TaskReplayHeads: %+v, %v; want none", heads, err)
	}
	if empty, err := c.TaskReplayEmpty(ctx, nil, cutoffProfileName, "task-blank"); err != nil || !empty {
		t.Errorf("TaskReplayEmpty: %v, %v; want empty", empty, err)
	}
	if _, err := c.TasksGet(ctx, cutoffProfileName, "task-blank"); !isTaskNotFoundErr(err) {
		t.Errorf("replay: %v, want TaskNotFound", err)
	}
	if c.ProfileSupervisorDrops() < 3 {
		t.Errorf("drops = %d, want one per read", c.ProfileSupervisorDrops())
	}
}

// The replay's horizon is the server's, not the direct get's Nats-Sequence,
// which sits in the same header block a blank line ends early (the sibling
// of H1 on #6885). A publisher-forged sequence on a task's newest event
// neither hides the events stored before it nor admits one stored after the
// call: a forged low one used to stop the replay at the first event, and a
// forged high one to fold whatever was stored while it ran.
func TestAForgedSequenceDoesNotMoveTheReplayHorizon(t *testing.T) {
	cases := []struct {
		name string
		seq  uint64
	}{
		{"low", 1},
		{"high", 1 << 40},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := startServer(t)
			url := clientURL(s)
			provisionCutoffTasksStream(t, url, true)
			taskID := "task-seq-" + tc.name
			subject := TaskEventsSubject(cutoffSessionName, taskID)
			publishRaw(t, url, subject, cutoffEnvelope(t, cutoffSessionName, taskID, StateSubmitted, false))
			publishRaw(t, url, subject, cutoffEnvelope(t, cutoffSessionName, taskID, StateWorking, false))
			publishHeaderBlock(t, url, subject, blankLineBlock(subject, tc.seq, time.Now()),
				cutoffEnvelope(t, cutoffSessionName, taskID, StateInputRequired, false))
			c := cutoffReader(t, url, time.Time{}, nil)
			ctx := testCtx(t)

			// The forgery reaches the client: the direct get's sequence is
			// the publisher's.
			_, js := c.conn()
			st, err := js.Stream(ctx, TasksStream)
			if err != nil {
				t.Fatal(err)
			}
			raw, err := st.GetLastMsgForSubject(ctx, subject)
			if err != nil {
				t.Fatal(err)
			}
			if raw.Sequence != tc.seq {
				t.Fatalf("direct get: Sequence %d; want the forged %d (the server no longer hides its own?)", raw.Sequence, tc.seq)
			}

			// A terminal stored after the horizon is read and before the
			// consumer is made is past the call.
			replayHorizonTaken = func() {
				publishRaw(t, url, subject, cutoffEnvelope(t, cutoffSessionName, taskID, StateFailed, true))
			}
			t.Cleanup(func() { replayHorizonTaken = nil })
			task, err := c.TasksGet(ctx, cutoffSessionName, taskID)
			replayHorizonTaken = nil
			if err != nil {
				t.Fatal(err)
			}
			want := []TaskState{StateSubmitted, StateWorking, StateInputRequired}
			if !reflect.DeepEqual(task.StatusHistory, want) || task.Final {
				t.Errorf("replay: history %v, final %v; want %v and not final", task.StatusHistory, task.Final, want)
			}

			// The next call's horizon is past the terminal, and folds it.
			if task, err := c.TasksGet(ctx, cutoffSessionName, taskID); err != nil || !task.Final {
				t.Errorf("second replay: %+v, %v; want the terminal", task, err)
			}
		})
	}
}

// A five-token terminal the dispatcher wrote before the cutoff is still a
// profile task's terminal on the direct-get reads: the direct get gives no
// time, so that one subject is read again on a consumer, and the server's
// time from its metadata is before the cutoff. Only that subject opens a
// consumer: a session's five-token subject and the events subject don't.
func TestAPreCutoffFiveTokenTerminalIsReadThroughTheConsumer(t *testing.T) {
	s := startServer(t)
	url := clientURL(s)
	provisionCutoffTasksStream(t, url, true)
	publishRaw(t, url, TaskSupervisorSubject(cutoffProfileName, "task-old"), cutoffEnvelope(t, DispatcherSession, "task-old", StateFailed, true))
	publishRaw(t, url, TaskEventsSubject(cutoffSessionName, "task-sess"), cutoffEnvelope(t, cutoffSessionName, "task-sess", StateWorking, false))
	publishRaw(t, url, TaskSupervisorSubject(cutoffSessionName, "task-sess"), cutoffEnvelope(t, "gateway", "task-sess", StateFailed, true))
	time.Sleep(20 * time.Millisecond)
	cutoff := time.Now()
	c := cutoffReader(t, url, cutoff, NewProfileNames(cutoffProfileName))
	ctx := testCtx(t)
	_, js := c.conn()
	consumers := func() int {
		st, err := js.Stream(ctx, TasksStream)
		if err != nil {
			t.Fatal(err)
		}
		info, err := st.Info(ctx)
		if err != nil {
			t.Fatal(err)
		}
		return info.State.Consumers
	}

	if _, err := c.TaskReplayHeads(ctx, nil, cutoffSessionName, "task-sess"); err != nil {
		t.Fatal(err)
	}
	if n := consumers(); n != 0 {
		t.Errorf("a session task's read opened %d consumers, want none", n)
	}

	heads, err := c.TaskReplayHeads(ctx, nil, cutoffProfileName, "task-old")
	if err != nil {
		t.Fatal(err)
	}
	if len(heads) != 1 || heads[0].StoredAt.IsZero() || !heads[0].StoredAt.Before(cutoff) {
		t.Fatalf("heads %+v; want the pre-cutoff terminal with the server's time", heads)
	}
	if env, err := ParseEnvelope(heads[0].Data); err != nil || !IsFinalStatus(env) {
		t.Errorf("head %q: %v; want the terminal", heads[0].Data, err)
	}
	if empty, err := c.TaskReplayEmpty(ctx, nil, cutoffProfileName, "task-old"); err != nil || empty {
		t.Errorf("TaskReplayEmpty: %v, %v; want the terminal found", empty, err)
	}
	if n := consumers(); n != 2 {
		t.Errorf("two profile reads opened %d consumers, want one each", n)
	}
	if c.ProfileSupervisorDrops() != 0 {
		t.Errorf("a pre-cutoff terminal counted %d drops", c.ProfileSupervisorDrops())
	}
}

// A malformed cutoff refuses every five-token terminal on a profile task,
// however old: the cutoff is the unix epoch, the same on every restart, so a
// terminal stored before the reader started is refused as one stored after.
// A session's is still taken.
func TestAMalformedCutoffRefusesEveryFiveTokenTerminalOnAProfile(t *testing.T) {
	s := startServer(t)
	url := clientURL(s)
	provisionCutoffTasksStream(t, url, true)
	publishRaw(t, url, TaskSupervisorSubject(cutoffProfileName, "task-1"), cutoffEnvelope(t, DispatcherSession, "task-1", StateFailed, true))
	publishRaw(t, url, TaskSupervisorSubject(cutoffSessionName, "task-1"), cutoffEnvelope(t, "gateway", "task-1", StateFailed, true))
	time.Sleep(20 * time.Millisecond)
	getenv := func(string) string { return "not a time" }
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	setting := ProfileSupervisorCutoffFromEnv(getenv, log)
	if again := ProfileSupervisorCutoffFromEnv(getenv, log); !again.Cutoff.Equal(setting.Cutoff) || !setting.Cutoff.Equal(MalformedCutoff) {
		t.Fatalf("malformed cutoff %v then %v; want the epoch both times", setting.Cutoff, again.Cutoff)
	}
	c := cutoffReader(t, url, setting.Cutoff, NewProfileNames(cutoffProfileName))
	ctx := testCtx(t)
	if _, err := c.TasksGet(ctx, cutoffProfileName, "task-1"); !isTaskNotFoundErr(err) {
		t.Errorf("replay of the profile's: %v, want its pre-start terminal refused", err)
	}
	if heads, err := c.TaskReplayHeads(ctx, nil, cutoffProfileName, "task-1"); err != nil || len(heads) != 0 {
		t.Errorf("direct gets of the profile's: %+v, %v; want none", heads, err)
	}
	if task, err := c.TasksGet(ctx, cutoffSessionName, "task-1"); err != nil || !task.Final {
		t.Errorf("the session's: %+v, %v; want its terminal", task, err)
	}
}

// LoadedAt moves on every read of the names file, changed or not, and stands
// still while reads fail: a reader's health shows names gone stale.
func TestProfileNamesLoadedAtStopsWhenReadsFail(t *testing.T) {
	path := filepath.Join(t.TempDir(), "profiles.json")
	p := &ProfileNames{}
	if !p.LoadedAt().IsZero() {
		t.Fatal("a set never loaded has a load time")
	}
	if err := os.WriteFile(path, []byte(`{"profiles":["fleet-audit"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := p.load(path); err != nil {
		t.Fatal(err)
	}
	first := p.LoadedAt()
	if first.IsZero() {
		t.Fatal("a loaded set has no load time")
	}
	time.Sleep(5 * time.Millisecond)
	if changed, err := p.load(path); err != nil || changed {
		t.Fatalf("an unchanged re-read: changed %v, %v", changed, err)
	}
	second := p.LoadedAt()
	if !second.After(first) {
		t.Errorf("an unchanged re-read left the load time at %v", second)
	}
	if err := os.WriteFile(path, []byte(`garbled`), 0o600); err != nil {
		t.Fatal(err)
	}
	time.Sleep(5 * time.Millisecond)
	if _, err := p.load(path); err == nil {
		t.Fatal("a garbled file read")
	}
	if !p.LoadedAt().Equal(second) || !p.Has("fleet-audit") {
		t.Errorf("after a failed read: load time %v (was %v), names kept %v", p.LoadedAt(), second, p.Has("fleet-audit"))
	}
}

// A stored header block the client can't parse at all makes the direct get
// fail, so the read errors rather than answering: every caller takes a
// failed read as the task still running, the safe side.
func TestAnUnparseableHeaderBlockFailsTheRead(t *testing.T) {
	s := startServer(t)
	url := clientURL(s)
	provisionCutoffTasksStream(t, url, true)
	cutoff := time.Now().Add(-time.Minute)
	subject := TaskSupervisorSubject(cutoffProfileName, "task-garbled")
	publishHeaderBlock(t, url, subject, "garbage\r\n\r\n", cutoffEnvelope(t, "gateway", "task-garbled", StateFailed, true))
	c := cutoffReader(t, url, cutoff, NewProfileNames(cutoffProfileName))
	ctx := testCtx(t)
	if heads, err := c.TaskReplayHeads(ctx, nil, cutoffProfileName, "task-garbled"); err == nil {
		t.Errorf("TaskReplayHeads: %+v with no error; want the read to fail", heads)
	}
	if empty, err := c.TaskReplayEmpty(ctx, nil, cutoffProfileName, "task-garbled"); err == nil {
		t.Errorf("TaskReplayEmpty: %v with no error; want the read to fail", empty)
	}
}

// storedTimeReadHook sets lastBySubjectHold to give a slot back after hold
// and lastBySubjectCreating to record the most slots in use at a create and
// how many creates there were, and restores both at cleanup. It waits for
// every slot to come back first, so no earlier test's hold leaks in.
func storedTimeReadHook(t *testing.T, hold time.Duration) (maxInUse, creates func() int) {
	t.Helper()
	waitFor(t, 10*time.Second, "every stored-time read slot free", func() bool { return len(storedTimeReadSlots) == 0 })
	var mu sync.Mutex
	var most, n int
	prevHold, prevCreating := lastBySubjectHold, lastBySubjectCreating
	lastBySubjectHold = func(release func()) { time.AfterFunc(hold, release) }
	lastBySubjectCreating = func(inUse int) {
		mu.Lock()
		defer mu.Unlock()
		n++
		most = max(most, inUse)
	}
	t.Cleanup(func() {
		lastBySubjectHold, lastBySubjectCreating = prevHold, prevCreating
		waitFor(t, 10*time.Second, "every stored-time read slot back", func() bool { return len(storedTimeReadSlots) == 0 })
	})
	return func() int { mu.Lock(); defer mu.Unlock(); return most },
		func() int { mu.Lock(); defer mu.Unlock(); return n }
}

// The stored-time reads a process makes hold at most StoredTimeReadSlots
// consumers at once, however many callers read at the same moment: twenty
// readers of twenty pre-cutoff profile terminals all get their answer, and
// no create runs with more than the bound in use. The hold is shortened so
// the test does not wait out five seconds per slot.
func TestStoredTimeReadsNeverExceedTheirSlots(t *testing.T) {
	s := startServer(t)
	url := clientURL(s)
	provisionCutoffTasksStream(t, url, true)
	const readers = 20
	for i := range readers {
		id := fmt.Sprintf("task-%d", i)
		publishRaw(t, url, TaskSupervisorSubject(cutoffProfileName, id), cutoffEnvelope(t, DispatcherSession, id, StateFailed, true))
	}
	time.Sleep(20 * time.Millisecond)
	c := cutoffReader(t, url, time.Now(), NewProfileNames(cutoffProfileName))
	maxInUse, creates := storedTimeReadHook(t, 100*time.Millisecond)

	var wg sync.WaitGroup
	errs := make(chan error, readers)
	for i := range readers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			heads, err := c.TaskReplayHeads(ctx, nil, cutoffProfileName, fmt.Sprintf("task-%d", i))
			if err == nil && len(heads) != 1 {
				err = fmt.Errorf("task-%d: %d heads, want the pre-cutoff terminal", i, len(heads))
			}
			errs <- err
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Error(err)
		}
	}
	if n := creates(); n != readers {
		t.Errorf("%d consumers made, want one per reader (%d)", n, readers)
	}
	if got := maxInUse(); got > StoredTimeReadSlots || got < 1 {
		t.Errorf("most slots in use at a create = %d, want 1..%d", got, StoredTimeReadSlots)
	}
}

// A caller out of time opens no consumer: one whose deadline has passed is
// refused before the create, and so is one that waits for a slot while all
// are held and runs out of time waiting. Both are failed reads, which every
// caller takes as the task still running.
func TestAStoredTimeReadPastItsDeadlineMakesNoConsumer(t *testing.T) {
	s := startServer(t)
	url := clientURL(s)
	provisionCutoffTasksStream(t, url, true)
	publishRaw(t, url, TaskSupervisorSubject(cutoffProfileName, "task-old"), cutoffEnvelope(t, DispatcherSession, "task-old", StateFailed, true))
	time.Sleep(20 * time.Millisecond)
	c := cutoffReader(t, url, time.Now(), NewProfileNames(cutoffProfileName))
	_, creates := storedTimeReadHook(t, 100*time.Millisecond)
	_, js := c.conn()
	consumers := func() int {
		st, err := js.Stream(testCtx(t), TasksStream)
		if err != nil {
			t.Fatal(err)
		}
		info, err := st.Info(testCtx(t))
		if err != nil {
			t.Fatal(err)
		}
		return info.State.Consumers
	}
	subject := TaskSupervisorSubject(cutoffProfileName, "task-old")

	past, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancel()
	if _, _, _, err := c.lastBySubject(past, subject); !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("read past its deadline: %v, want DeadlineExceeded", err)
	}

	for range StoredTimeReadSlots {
		storedTimeReadSlots <- struct{}{}
	}
	short, cancelShort := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancelShort()
	_, _, _, err := c.lastBySubject(short, subject)
	for range StoredTimeReadSlots {
		releaseStoredTimeRead()
	}
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("read with every slot held: %v, want DeadlineExceeded", err)
	}

	if n := creates(); n != 0 {
		t.Errorf("%d consumers made by reads out of time, want none", n)
	}
	if n := consumers(); n != 0 {
		t.Errorf("TASKS holds %d consumers after reads out of time, want none", n)
	}
	if len(storedTimeReadSlots) != 0 {
		t.Errorf("%d slots still held after reads that made no consumer", len(storedTimeReadSlots))
	}
}
