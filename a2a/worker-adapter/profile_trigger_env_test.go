package workeradapter

import (
	"context"
	"encoding/json"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

type warnSink struct{ warned []string }

func (w *warnSink) Warn(msg string, _ ...any) { w.warned = append(w.warned, msg) }

// envValue is the value the harness would see for name: os/exec keeps the
// last of duplicate keys.
func envValue(env []string, name string) (string, bool) {
	var value string
	found := false
	for _, kv := range env {
		if k, v, ok := strings.Cut(kv, "="); ok && k == name {
			value, found = v, true
		}
	}
	return value, found
}

func withAuthority(t *testing.T, env *lib.Envelope, authority string) *lib.Envelope {
	t.Helper()
	if !json.Valid([]byte(authority)) {
		t.Fatalf("test authority isn't JSON: %s", authority)
	}
	env.Authority = json.RawMessage(authority)
	return env
}

// A task a schedule's trigger started gets the trigger's stream and origin.
func TestATriggeredTaskGetsItsTriggerStreamAndOrigin(t *testing.T) {
	env := withAuthority(t, profileSubmission(t, "task-tr-1", "audit", nil),
		`{"trigger":{"name":"schedule/compliance-audit-canary","origin":"builtin","stream":"compliance-audit-canary"}}`)
	got := taskTriggerEnv(env, &warnSink{})
	if v, _ := envValue(got, profileTriggerStreamEnv); v != "compliance-audit-canary" {
		t.Errorf("%s = %q, want the schedule's stream; env %v", profileTriggerStreamEnv, v, got)
	}
	if v, _ := envValue(got, profileTriggerOriginEnv); v != "builtin" {
		t.Errorf("%s = %q, want builtin; env %v", profileTriggerOriginEnv, v, got)
	}
}

// A task no trigger started gets neither, and neither does one whose block
// can't be read (which is said).
func TestAnUntriggeredOrUnreadableTaskGetsNoTrigger(t *testing.T) {
	for name, authority := range map[string]string{
		"none":       `null`,
		"no trigger": `{"requester":{"principal":"p"}}`,
		"malformed":  `{"trigger":{"name":"schedule/x","origin":"builtin","stream":"x"},"trigger":{"name":"schedule/y","origin":"builtin","stream":"y"}}`,
	} {
		t.Run(name, func(t *testing.T) {
			sink := &warnSink{}
			got := taskTriggerEnv(withAuthority(t, profileSubmission(t, "task-tr-2", "audit", nil), authority), sink)
			if len(got) != 0 {
				t.Errorf("env %v, want none", got)
			}
			if (name == "malformed") != (len(sink.warned) > 0) {
				t.Errorf("warnings %v", sink.warned)
			}
		})
	}
}

// A task parameter can't set or override the trigger's names: its own name
// takes the PROFILE_TASK_ prefix, and the harness env takes the trigger's
// after the parameters.
func TestATaskParameterCantSetTheTriggerEnv(t *testing.T) {
	env := withAuthority(t,
		profileSubmission(t, "task-tr-3", "audit", map[string]string{"trigger_stream": "compliance-audit", "trigger_origin": "user"}),
		`{"trigger":{"name":"schedule/compliance-audit-canary","origin":"builtin","stream":"compliance-audit-canary"}}`)
	sink := &warnSink{}
	harness := append(taskParameterEnv(env, sink), taskTriggerEnv(env, sink)...)
	if v, _ := envValue(harness, profileTriggerStreamEnv); v != "compliance-audit-canary" {
		t.Errorf("%s = %q, want the trigger's; env %v", profileTriggerStreamEnv, v, harness)
	}
	if v, _ := envValue(harness, profileTriggerOriginEnv); v != "builtin" {
		t.Errorf("%s = %q, want the trigger's; env %v", profileTriggerOriginEnv, v, harness)
	}
	if !slices.Contains(harness, "PROFILE_TASK_TRIGGER_STREAM=compliance-audit") {
		t.Errorf("the parameter should still pass as PROFILE_TASK_TRIGGER_STREAM; env %v", harness)
	}
	// With no trigger, the parameter alone still sets nothing of the trigger's.
	untriggered := profileSubmission(t, "task-tr-4", "audit", map[string]string{"trigger_stream": "compliance-audit"})
	alone := append(taskParameterEnv(untriggered, sink), taskTriggerEnv(untriggered, sink)...)
	if _, found := envValue(alone, profileTriggerStreamEnv); found {
		t.Errorf("a parameter set %s with no trigger; env %v", profileTriggerStreamEnv, alone)
	}
}

// End to end: the harness of a dispatched task a schedule's trigger started
// reads the trigger's stream.
func TestADispatchedTriggeredTaskHandsTheHarnessItsStream(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"pw-tr"}'
read first || exit 1
printf '{"type":"result","subtype":"success","result":"stream=%s origin=%s"}\n' "$PROFILE_TRIGGER_STREAM" "$PROFILE_TRIGGER_ORIGIN"
`)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	startProfileWorker(t, ctx, profileWorkerConfig(t, url, harness), false)

	const task = "task-pw-tr"
	sub := withAuthority(t, profileSubmission(t, task, "audit", nil),
		`{"trigger":{"name":"schedule/compliance-audit-canary","origin":"builtin","stream":"compliance-audit-canary"}}`)
	d.offer(task, sub, 5)
	if a := d.nextAccept(5 * time.Second); a.TaskID != task || !a.Accepted {
		t.Fatalf("answer = %+v, want an accept", a)
	}
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
	if _, final := drainLeases(d, task, 20*time.Second); !final {
		t.Fatal("no final renewal after the task ended")
	}
	got := foldTask(t, testClient(t, url), pwProfile, task)
	if want := "stream=compliance-audit-canary origin=builtin"; artifactText(got, lib.ArtifactResult) != want {
		t.Errorf("result %q (state %s), want %q", artifactText(got, lib.ArtifactResult), got.State, want)
	}
}

// The pod's own environment can't hand an untriggered task a trigger.
func TestThePodsEnvCantHandATaskATrigger(t *testing.T) {
	base := []string{"PATH=/bin", profileTriggerStreamEnv + "=compliance-audit", profileTriggerOriginEnv + "=builtin"}
	got := withoutTriggerEnv(base)
	if !slices.Equal(got, []string{"PATH=/bin"}) {
		t.Errorf("withoutTriggerEnv(%v) = %v, want PATH alone", base, got)
	}
}

// A script run never sees the trigger env: scriptEnv is an allowlist, and
// this holds it, so a later change that seeded it from the harness env fails
// here, even with the worker's own environment carrying the names.
func TestAScriptRunNeverSeesTheTriggerEnv(t *testing.T) {
	sub := withAuthority(t, profileSubmission(t, "task-tr-5", "audit", map[string]string{"cluster": "prod-east"}),
		`{"trigger":{"name":"schedule/compliance-audit-canary","origin":"builtin","stream":"compliance-audit-canary"}}`)
	pod := map[string]string{"PATH": "/bin", profileTriggerStreamEnv: "compliance-audit-canary", profileTriggerOriginEnv: "builtin"}
	env, err := scriptEnv(sub, func(k string) string { return pod[k] })
	if err != nil {
		t.Fatal(err)
	}
	for _, kv := range env {
		if strings.HasPrefix(kv, "PROFILE_TRIGGER_") {
			t.Errorf("a script's env carries %q; env %v", kv, env)
		}
	}
}
