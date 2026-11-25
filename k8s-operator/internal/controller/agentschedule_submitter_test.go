package controller

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// triggerSubmitFixturePath is where the operator's submission bytes go for
// a2a/lib's contract test (TestTheOperatorsTriggerSubmissionIsALibTriggerSubmit).
const triggerSubmitFixturePath = "../../../a2a/lib/testdata/rendered-trigger-submit.json"

// triggerSubmitScriptFixturePath is the same for a built-in schedule's
// script run.
const triggerSubmitScriptFixturePath = "../../../a2a/lib/testdata/rendered-trigger-submit-script.json"

// scriptSubmission is submitterSubmission's schedule turned into a script
// run: a script in place of its prompt, and no skills or maxTurns, which
// shape a model run (the CRD refuses them beside a script).
func scriptSubmission() ScheduleSubmission {
	sub := submitterSubmission()
	sub.Schedule.Spec.Prompt = ""
	sub.Schedule.Spec.Script = "compliance-audit"
	sub.Schedule.Spec.Skills = nil
	sub.Schedule.Spec.MaxTurns = nil
	return sub
}

func submitterSchedule() *agentv1alpha1.AgentSchedule {
	return &agentv1alpha1.AgentSchedule{
		ObjectMeta: metav1.ObjectMeta{Namespace: "kubeagents-system", Name: "compliance-audit"},
		Spec: agentv1alpha1.AgentScheduleSpec{
			Schedule:              "20 6 * * *",
			ProfileRef:            agentv1alpha1.AgentScheduleProfileRef{Name: "fleet-audit"},
			Prompt:                "Run the compliance audit.",
			Skills:                []string{"fleet-audit"},
			Parameters:            map[string]string{"schedule_mode": "scheduled"},
			ActiveDeadlineSeconds: ptr.To[int64](3600),
			MaxTurns:              ptr.To[int32](250),
			Delivery:              agentv1alpha1.AgentScheduleDelivery{SilentWhenClean: true},
		},
	}
}

func submitterSubmission() ScheduleSubmission {
	return ScheduleSubmission{
		IdempotencyKey: "schedule/kubeagents-system/compliance-audit/1763100000",
		TriggerName:    "schedule/compliance-audit",
		Origin:         agentv1alpha1.AgentScheduleOriginBuiltin,
		SlotTime:       time.Unix(1763100000, 0),
		Schedule:       submitterSchedule(),
	}
}

// TestTheTriggerSubmitFixtureIsWhatTheOperatorSends writes the bytes the
// operator sends for a built-in schedule's run, for a2a/lib to decode
// strictly into lib.TriggerSubmit and validate.
func TestTheTriggerSubmitFixtureIsWhatTheOperatorSends(t *testing.T) {
	body, err := json.MarshalIndent(triggerSubmitFor(submitterSubmission()), "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	checkFixture(t, triggerSubmitFixturePath, string(body)+"\n")
}

// TestTheScriptTriggerSubmitFixtureIsWhatTheOperatorSends is the fixture
// for a built-in schedule's script run, which a2a/lib decodes and validates
// as it does the prompt run's.
func TestTheScriptTriggerSubmitFixtureIsWhatTheOperatorSends(t *testing.T) {
	body, err := json.MarshalIndent(triggerSubmitFor(scriptSubmission()), "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	checkFixture(t, triggerSubmitScriptFixturePath, string(body)+"\n")
}

// A built-in schedule's script rides on its trigger, beside its stream, with
// a task text the gateway requires and no model reads. A user schedule's
// submission never carries one, even if one reached it.
func TestAScriptRunsSubmissionCarriesItsScriptOnTheTrigger(t *testing.T) {
	w := triggerSubmitFor(scriptSubmission())
	if w.Trigger != (triggerClaimWire{Name: "schedule/compliance-audit", Origin: "builtin", Stream: "compliance-audit", Script: "compliance-audit"}) {
		t.Errorf("trigger = %+v, want the script beside the stream", w.Trigger)
	}
	if w.Prompt != "Scheduled script run: compliance-audit (no model turn)." {
		t.Errorf("task text = %q", w.Prompt)
	}
	// A schedule stored without the CRD's rule may still carry skills and
	// maxTurns beside its script: they stay off the wire.
	stray := scriptSubmission()
	stray.Schedule.Spec.Skills = []string{"fleet-audit"}
	stray.Schedule.Spec.MaxTurns = ptr.To[int32](250)
	body, err := json.Marshal(triggerSubmitFor(stray))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(body), `"skills"`) || strings.Contains(string(body), `"maxTurns"`) {
		t.Errorf("a script run's submission carries skills or maxTurns: %s", body)
	}
	user := scriptSubmission()
	user.Origin = agentv1alpha1.AgentScheduleOriginUser
	if got := triggerSubmitFor(user); got.Trigger.Script != "" || got.Prompt != "" {
		t.Errorf("a user schedule's submission carries script %q and task text %q", got.Trigger.Script, got.Prompt)
	}
}

// With no script, the submission is what it was before spec.script existed,
// byte for byte: the script field is omitted, not sent empty.
func TestAPromptRunsSubmissionHasNoScriptField(t *testing.T) {
	body, err := json.Marshal(triggerSubmitFor(submitterSubmission()))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(body), `"script"`) {
		t.Errorf("a prompt run's submission names a script field: %s", body)
	}
}

func TestAScheduleRunsSubmissionCarriesItsTriggerAndDefaults(t *testing.T) {
	w := triggerSubmitFor(submitterSubmission())
	if w.Trigger != (triggerClaimWire{Name: "schedule/compliance-audit", Origin: "builtin", Stream: "compliance-audit"}) {
		t.Errorf("trigger = %+v", w.Trigger)
	}
	if w.Delivery != (scheduleDeliveryWire{Mode: "chat", SilentWhenClean: true, JobKey: "compliance-audit"}) {
		t.Errorf("delivery = %+v, want chat and the name as the job key", w.Delivery)
	}
	if w.ActiveDeadlineSeconds != 3600 || w.MaxTurns != 250 || w.Profile != "fleet-audit" {
		t.Errorf("bounds or profile = %+v", w)
	}
	user := submitterSubmission()
	user.Origin = agentv1alpha1.AgentScheduleOriginUser
	if got := triggerSubmitFor(user).Trigger.Stream; got != "" {
		t.Errorf("a user schedule's submission binds stream %q", got)
	}
}

// submitterRig is the operator's submitter on an embedded server, its
// connection seeded so no bus TLS is needed, and a stub gateway answering
// on the trigger subjects with answer.
func submitterRig(t *testing.T, answer func(subject string, data []byte) any) (*natsScheduleSubmitter, *[]string) {
	t.Helper()
	s, err := natsserver.NewServer(&natsserver.Options{Port: -1})
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats server not ready")
	}
	t.Cleanup(s.Shutdown)
	operator, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(operator.Close)
	gateway, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(gateway.Close)
	var replies []string
	for _, subject := range []string{triggerSubmitSubject, triggerStatusSubject} {
		if _, err := gateway.Subscribe(subject, func(m *nats.Msg) {
			replies = append(replies, m.Reply)
			data, _ := json.Marshal(answer(m.Subject, m.Data))
			_ = m.Respond(data)
		}); err != nil {
			t.Fatal(err)
		}
	}
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Namespace: "kubeagents-system", Name: "platform-agent"}}
	reader := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).Build()
	bus := newNATSCardPublisher(reader)
	bus.conns["kubeagents-system/platform-agent"] = operator
	sub := newNATSScheduleSubmitter(bus, reader)
	sub.now = func() time.Time { return time.Unix(1763100500, 0) }
	return sub, &replies
}

func TestTheSubmitterMapsTheGatewaysAnswers(t *testing.T) {
	for _, tc := range []struct {
		name   string
		answer triggerSubmitReplyWire
		want   error
		task   string
	}{
		{"a task", triggerSubmitReplyWire{TaskID: "task-1"}, nil, "task-1"},
		{"rate limited", triggerSubmitReplyWire{Refused: &triggerRefusalWire{Reason: "rate_limited"}}, ErrScheduleRateLimited, ""},
		{"unavailable", triggerSubmitReplyWire{Refused: &triggerRefusalWire{Reason: "unavailable"}}, ErrScheduleGatewayUnavailable, ""},
		{"invalid", triggerSubmitReplyWire{Refused: &triggerRefusalWire{Reason: "invalid"}}, errTriggerRefused, ""},
		{"no task id", triggerSubmitReplyWire{}, ErrScheduleGatewayUnavailable, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sub, replies := submitterRig(t, func(string, []byte) any { return tc.answer })
			task, err := sub.Submit(context.Background(), submitterSubmission())
			if !errors.Is(err, tc.want) && (tc.want != nil || err != nil) {
				t.Fatalf("err = %v, want %v", err, tc.want)
			}
			if task != tc.task {
				t.Errorf("task = %q, want %q", task, tc.task)
			}
			if len(*replies) != 1 || !strings.HasPrefix((*replies)[0], "a2a.trigger.reply.schedule.") {
				t.Errorf("reply subjects = %v, want one in the source's reply namespace", *replies)
			}
		})
	}
}

func TestTheTrackerReadsARunsEndFromTheGateway(t *testing.T) {
	sched := submitterSchedule()
	for _, tc := range []struct {
		name   string
		answer triggerStatusReplyWire
		want   ScheduleTaskState
	}{
		{"running", triggerStatusReplyWire{Found: true, State: "working"}, ScheduleTaskState{Found: true}},
		{"completed", triggerStatusReplyWire{Found: true, State: "completed", Terminal: true},
			ScheduleTaskState{Found: true, Terminal: true, Outcome: agentv1alpha1.AgentScheduleOutcomeSucceeded, CompletedAt: time.Unix(1763100500, 0)}},
		{"canceled", triggerStatusReplyWire{Found: true, State: "canceled", Terminal: true},
			ScheduleTaskState{Found: true, Terminal: true, Outcome: agentv1alpha1.AgentScheduleOutcomeCancelled, CompletedAt: time.Unix(1763100500, 0)}},
		{"failed", triggerStatusReplyWire{Found: true, State: "failed", Terminal: true},
			ScheduleTaskState{Found: true, Terminal: true, Outcome: agentv1alpha1.AgentScheduleOutcomeFailed, CompletedAt: time.Unix(1763100500, 0)}},
		{"gone", triggerStatusReplyWire{Found: false}, ScheduleTaskState{Found: false}},
		{"delivered", triggerStatusReplyWire{Found: true, State: "completed", Terminal: true, Delivery: "posted"},
			ScheduleTaskState{Found: true, Terminal: true, Outcome: agentv1alpha1.AgentScheduleOutcomeSucceeded, CompletedAt: time.Unix(1763100500, 0), Delivery: "posted"}},
		{"failed with a reason", triggerStatusReplyWire{Found: true, State: "failed", Terminal: true, Reason: "deadline-exceeded"},
			ScheduleTaskState{Found: true, Terminal: true, Outcome: agentv1alpha1.AgentScheduleOutcomeFailed, CompletedAt: time.Unix(1763100500, 0), Reason: "deadline-exceeded"}},
		{"a reason before the end is not read", triggerStatusReplyWire{Found: true, State: "working", Reason: "deadline-exceeded"}, ScheduleTaskState{Found: true}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var asked triggerStatusWire
			sub, _ := submitterRig(t, func(_ string, data []byte) any {
				_ = json.Unmarshal(data, &asked)
				return tc.answer
			})
			got, err := sub.State(context.Background(), sched, "task-1")
			if err != nil {
				t.Fatal(err)
			}
			if !got.CompletedAt.Equal(tc.want.CompletedAt) {
				t.Errorf("completed at = %v, want %v", got.CompletedAt, tc.want.CompletedAt)
			}
			got.CompletedAt, tc.want.CompletedAt = time.Time{}, time.Time{}
			if got != tc.want {
				t.Errorf("state = %+v, want %+v", got, tc.want)
			}
			if asked != (triggerStatusWire{Schedule: scheduleRefWire{Namespace: "kubeagents-system", Name: "compliance-audit"}, TaskID: "task-1"}) {
				t.Errorf("asked %+v", asked)
			}
		})
	}
}

func TestTheSubmitterNeedsExactlyOnePlatformAgent(t *testing.T) {
	reader := fake.NewClientBuilder().WithScheme(setupScheme()).Build()
	sub := newNATSScheduleSubmitter(newNATSCardPublisher(reader), reader)
	if _, err := sub.Submit(context.Background(), submitterSubmission()); !errors.Is(err, ErrScheduleGatewayUnavailable) {
		t.Fatalf("err = %v, want gateway unavailable with no PlatformAgent", err)
	}
}

// The schedule source's grants (#5617, six approved): with schedules on, the
// operator publishes the schedule source's submit and status subjects and
// reads its reply namespace; the gateway reads every source's two subjects
// and answers in the reply namespace. With schedules off the operator holds
// none of them.
func TestTheTriggerGrantsFollowTheScheduleSwitch(t *testing.T) {
	withOperatorBusPrincipal(t)
	id, ok := operatorIdentity()
	if !ok {
		t.Fatal("no operator identity")
	}
	for _, g := range append(append([]string(nil), id.publish...), id.subscribe...) {
		if strings.HasPrefix(g, "a2a.trigger.") {
			t.Errorf("with schedules off the operator holds %q", g)
		}
	}
	t.Setenv(AgentSchedulesEnvVar, "true")
	id, _ = operatorIdentity()
	for _, want := range []string{triggerSubmitSubject, triggerStatusSubject, triggerStopSubject} {
		if !slices.Contains(id.publish, want) {
			t.Errorf("operator publish lacks %q", want)
		}
	}
	if !slices.Contains(id.subscribe, triggerReplyPrefix+">") {
		t.Errorf("operator subscribe lacks %q", triggerReplyPrefix+">")
	}
	gw := gatewayIdentity(a2aTestAgent(), "kubeagents-system")
	for _, want := range []string{"a2a.trigger.*.submit", "a2a.trigger.*.status", "a2a.trigger.*.stop"} {
		if !slices.Contains(gw.subscribe, want) {
			t.Errorf("gateway subscribe lacks %q", want)
		}
	}
	if !slices.Contains(gw.publish, "a2a.trigger.reply.>") {
		t.Error("gateway publish lacks a2a.trigger.reply.>")
	}
}

// Under the rendered grants of both sides, on a real server: the operator's
// submitter round-trips a submission and a status query with a gateway, and
// may not publish another source's subjects or read another source's
// replies.
func TestTheSubmitterWorksUnderTheRenderedGrants(t *testing.T) {
	withOperatorBusPrincipal(t)
	t.Setenv(AgentSchedulesEnvVar, "true")
	op, _ := operatorIdentity()
	gw := gatewayIdentity(a2aTestAgent(), "kubeagents-system")
	s, err := natsserver.NewServer(&natsserver.Options{Port: -1, Users: []*natsserver.User{
		{Username: "operator", Password: "operator", Permissions: &natsserver.Permissions{
			Publish:   &natsserver.SubjectPermission{Allow: op.publish},
			Subscribe: &natsserver.SubjectPermission{Allow: op.subscribe},
		}},
		{Username: "gateway", Password: "gateway", Permissions: &natsserver.Permissions{
			Publish:   &natsserver.SubjectPermission{Allow: gw.publish, Deny: gw.denyPublish},
			Subscribe: &natsserver.SubjectPermission{Allow: gw.subscribe, Deny: gw.denySubscribe},
		}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats server not ready")
	}
	t.Cleanup(s.Shutdown)

	var mu sync.Mutex
	var violations []string
	operator, err := nats.Connect(s.ClientURL(), nats.UserInfo("operator", "operator"), nats.CustomInboxPrefix("_INBOX."+a2aOperatorBusUser),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) {
			mu.Lock()
			violations = append(violations, err.Error())
			mu.Unlock()
		}))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(operator.Close)
	gateway, err := nats.Connect(s.ClientURL(), nats.UserInfo("gateway", "gateway"), nats.CustomInboxPrefix("_INBOX.gateway"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(gateway.Close)
	answer := func(m *nats.Msg) {
		var data []byte
		switch m.Subject {
		case triggerSubmitSubject:
			data, _ = json.Marshal(triggerSubmitReplyWire{TaskID: "task-1"})
		case triggerStopSubject:
			data, _ = json.Marshal(triggerStopReplyWire{Result: triggerStopStopped})
		default:
			data, _ = json.Marshal(triggerStatusReplyWire{Found: true, State: "working"})
		}
		_ = m.Respond(data)
	}
	for _, subject := range []string{"a2a.trigger.*.submit", "a2a.trigger.*.status", "a2a.trigger.*.stop"} {
		if _, err := gateway.Subscribe(subject, answer); err != nil {
			t.Fatal(err)
		}
	}
	if err := gateway.Flush(); err != nil {
		t.Fatal(err)
	}

	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Namespace: "kubeagents-system", Name: "platform-agent"}}
	reader := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).Build()
	bus := newNATSCardPublisher(reader)
	bus.conns["kubeagents-system/platform-agent"] = operator
	sub := newNATSScheduleSubmitter(bus, reader)
	if task, err := sub.Submit(context.Background(), submitterSubmission()); err != nil || task != "task-1" {
		t.Fatalf("submit under the rendered grants: task %q, %v", task, err)
	}
	if st, err := sub.State(context.Background(), submitterSchedule(), "task-1"); err != nil || !st.Found {
		t.Fatalf("status under the rendered grants: %+v, %v", st, err)
	}
	if res, err := sub.Stop(context.Background(), submitterSchedule(), "task-1"); err != nil || res != triggerStopStopped {
		t.Fatalf("stop under the rendered grants: %q, %v", res, err)
	}

	_ = operator.Publish("a2a.trigger.drift-detector.submit", []byte("{}"))
	_ = operator.Publish("a2a.trigger.drift-detector.stop", []byte("{}"))
	if _, err := operator.SubscribeSync("a2a.trigger.reply.drift-detector.>"); err != nil {
		t.Fatal(err)
	}
	_ = operator.FlushTimeout(2 * time.Second)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := len(violations)
		mu.Unlock()
		if n >= 3 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	joined := strings.Join(violations, "\n")
	for _, s := range []string{"a2a.trigger.drift-detector.submit", "a2a.trigger.drift-detector.stop", "a2a.trigger.reply.drift-detector.>"} {
		if !strings.Contains(joined, s) {
			t.Errorf("the operator was not refused on %q; got:\n%s", s, joined)
		}
	}
}

// A namespace whose PlatformAgent is gone has its bus connection closed when
// its last schedule goes, rather than reconnecting forever.
func TestTheSubmittersConnectionClosesWithItsPlatformAgent(t *testing.T) {
	sub, _ := submitterRig(t, func(string, []byte) any { return triggerSubmitReplyWire{TaskID: "task-1"} })
	nc := sub.bus.conns["kubeagents-system/platform-agent"]
	r := &AgentScheduleReconciler{Client: fake.NewClientBuilder().WithScheme(setupScheme()).Build(), Submitter: sub}
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "kubeagents-system", Name: "gone"}}); err != nil {
		t.Fatal(err)
	}
	if !nc.IsClosed() || len(sub.bus.conns) != 0 {
		t.Fatalf("closed %v, conns %v; want the agent's connection closed and dropped", nc.IsClosed(), sub.bus.conns)
	}
}
