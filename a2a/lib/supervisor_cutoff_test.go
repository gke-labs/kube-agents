package lib

import (
	"bytes"
	"encoding/json"
	"errors"
	"log/slog"
	"reflect"
	"strings"
	"testing"
	"time"
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
// on each side of it.
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
			publishRaw(t, url, TaskSupervisorSubject(addressee, taskID), cutoffEnvelope(t, "gateway", taskID, StateFailed, true))
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

func cutoffReader(t *testing.T, url string, cutoff time.Time) *Client {
	t.Helper()
	opts := []ClientOption{WithName("cutoff-reader"), WithAgreementPolicy(cutoffReaderPolicy)}
	if !cutoff.IsZero() {
		opts = append(opts, WithProfileSupervisorCutoff(cutoff))
	}
	c, err := Connect(testCtx(t), url, opts...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Close)
	return c
}

// A five-token supervisor write is not a profile task's terminal on a latched
// reader once its stored time is past the cutoff, whatever the addressee's
// name looks like; the same write on a session task still is (#5301, gate 2
// on the flip). A profile and a session fill the same addressee token, so the
// read is told which the task is, never the name's shape: each row is one
// token with a profile task and a session task under it. Every row is also
// read through the direct-get helper the gateway's busy count and
// no-first-event notice use, so TaskNotFound and "no heads" stay one answer.
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
		// The five-token write lands first: on the profile task the
		// executor's own terminal behind it is the end; on the session task
		// the supervisor's is, and the executor's is the post-final drop.
		{name: "five-token after the cutoff ahead of the executor's terminal", writes: cutoffWrites{working: true, eventsTerminal: true, fiveAfter: true}, reads: []read{
			{profile: true, latched: true, final: true, subject: events},
			{profile: false, latched: true, final: true, subject: five},
		}},
	}
	// A session-shaped token used as a profile's name, and a profile-shaped
	// token used as a session's.
	for _, addressee := range []string{"cto-otter-1a2b", "fleet-audit"} {
		for _, r := range rows {
			t.Run(addressee+"/"+r.name, func(t *testing.T) {
				s := startServer(t)
				url := clientURL(s)
				provisionTasksStream(t, url)
				tasks := map[[2]string]cutoffWrites{}
				for _, profile := range []bool{true, false} {
					tasks[[2]string{addressee, cutoffTaskID(profile)}] = r.writes
				}
				cutoff := publishAroundCutoff(t, url, tasks)
				latched, unlatched := cutoffReader(t, url, cutoff), cutoffReader(t, url, time.Time{})
				ctx := testCtx(t)
				for _, rd := range r.reads {
					c := unlatched
					if rd.latched {
						c = latched
					}
					taskID := cutoffTaskID(rd.profile)
					what := map[bool]string{true: "profile", false: "session"}[rd.profile] + map[bool]string{true: " on a latched reader", false: " on an unlatched reader"}[rd.latched]
					task, subject, _, err := c.TasksGetTerminal(ctx, addressee, taskID, ProfileTaskIf(rd.profile))
					heads, herr := c.TaskReplayHeads(ctx, nil, addressee, taskID, ProfileTaskIf(rd.profile))
					if herr != nil {
						t.Fatalf("%s: TaskReplayHeads: %v", what, herr)
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
						want = rd.subject(addressee, taskID)
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

func cutoffTaskID(profile bool) string {
	if profile {
		return "task-profile"
	}
	return "task-session"
}

// An unlatched install reads every task exactly as before the cutoff existed:
// a reader with no cutoff gives the same fold, terminal subject and stored
// time whether or not it is told the task is a profile's, and so does a
// reader handed the zero cutoff, for every shape of write. This is the
// no-regression guarantee that makes the change inert until the operator
// renders the cutoff.
func TestAnUnlatchedReaderReadsBothFormsAsBefore(t *testing.T) {
	s := startServer(t)
	url := clientURL(s)
	provisionTasksStream(t, url)
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
		tasks[[2]string{"fleet-audit", name}] = w
	}
	publishAroundCutoff(t, url, tasks)
	before := cutoffReader(t, url, time.Time{})
	zero, err := Connect(testCtx(t), url, WithName("zero-cutoff"), WithAgreementPolicy(cutoffReaderPolicy), WithProfileSupervisorCutoff(time.Time{}))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(zero.Close)
	ctx := testCtx(t)
	type answer struct {
		Task     []byte
		Subject  string
		StoredAt time.Time
		Err      string
		Heads    []ReplayHead
	}
	readAs := func(c *Client, taskID string, opts ...ReadOption) answer {
		task, subject, at, err := c.TasksGetTerminal(ctx, "fleet-audit", taskID, opts...)
		var a answer
		a.Subject, a.StoredAt = subject, at
		if err != nil {
			a.Err = err.Error()
		}
		if a.Task, err = json.Marshal(task); err != nil {
			t.Fatal(err)
		}
		if a.Heads, err = c.TaskReplayHeads(ctx, nil, "fleet-audit", taskID, opts...); err != nil {
			t.Fatal(err)
		}
		return a
	}
	for name := range shapes {
		want := readAs(before, name)
		for label, got := range map[string]answer{
			"no cutoff, read as a profile task":   readAs(before, name, ProfileTask()),
			"zero cutoff, read as a profile task": readAs(zero, name, ProfileTask()),
			"zero cutoff, read as a session task": readAs(zero, name, ProfileTaskIf(false)),
		} {
			if !bytes.Equal(got.Task, want.Task) || got.Subject != want.Subject || !got.StoredAt.Equal(want.StoredAt) ||
				got.Err != want.Err || !reflect.DeepEqual(got.Heads, want.Heads) {
				t.Errorf("%s: %s read differs from the plain read:\n got %+v\nwant %+v", name, label, got, want)
			}
		}
	}
	if before.ProfileSupervisorDrops() != 0 || zero.ProfileSupervisorDrops() != 0 {
		t.Errorf("an unlatched reader counted drops: %d, %d", before.ProfileSupervisorDrops(), zero.ProfileSupervisorDrops())
	}
}

// The predicate refuses one case only, and a zero stored time is accepted.
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
		{"session, five-token, after", cutoff, five, after, false, true},
		{"profile, six-token, after", cutoff, six, after, true, true},
		{"profile, events, after", cutoff, TaskEventsSubject("fleet-audit", "t1"), after, true, true},
		{"profile, five-token addressed to p, after", cutoff, TaskSupervisorSubject("p", "t1"), after, true, false},
		{"unlatched", time.Time{}, five, after, true, true},
		{"no stored time", cutoff, five, time.Time{}, true, true},
	} {
		if got := AcceptsSupervisorEvent(c.cutoff, c.subject, c.storedAt, c.profile); got != c.want {
			t.Errorf("%s: AcceptsSupervisorEvent = %v, want %v", c.name, got, c.want)
		}
	}
}

// The env reads absent and empty as unlatched, an RFC3339 time as the
// cutoff, and anything else as unlatched with an Error line naming the
// variable: fail-safe to the reads every install made before the cutoff.
func TestProfileSupervisorCutoffFromEnv(t *testing.T) {
	want := time.Date(2026, 11, 23, 12, 10, 0, 0, time.UTC)
	for _, c := range []struct {
		value   string
		want    time.Time
		errLine bool
	}{
		{"", time.Time{}, false},
		{"   ", time.Time{}, false},
		{"2026-11-23T12:10:00Z", want, false},
		{" 2026-11-23T13:10:00+01:00 ", want, false},
		{"2026-11-23 12:10:00", time.Time{}, true},
		{"yes", time.Time{}, true},
	} {
		var buf bytes.Buffer
		log := slog.New(slog.NewTextHandler(&buf, nil))
		got := ProfileSupervisorCutoffFromEnv(func(k string) string {
			if k == EnvProfileSupervisorCutoff {
				return c.value
			}
			return ""
		}, log)
		if !got.Equal(c.want) {
			t.Errorf("%q: cutoff = %v, want %v", c.value, got, c.want)
		}
		if isErr := strings.Contains(buf.String(), "level=ERROR") && strings.Contains(buf.String(), EnvProfileSupervisorCutoff); isErr != c.errLine {
			t.Errorf("%q: error line = %v, want %v (log %q)", c.value, isErr, c.errLine, buf.String())
		}
	}
}
