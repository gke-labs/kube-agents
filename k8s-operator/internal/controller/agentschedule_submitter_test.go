package controller

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// triggerSubmitFixturePath is where the operator's submission bytes go for
// a2a/lib's contract test (TestTheOperatorsTriggerSubmissionIsALibTriggerSubmit).
const triggerSubmitFixturePath = "../../../a2a/lib/testdata/rendered-trigger-submit.json"

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

func TestAScheduleNameFoldsIntoAJobKeyLabel(t *testing.T) {
	for in, want := range map[string]string{
		"compliance-audit":                    "compliance-audit",
		"team.a.nightly":                      "team-a-nightly",
		strings.Repeat("a", 70):               strings.Repeat("a", 63),
		"x." + strings.Repeat("b", 61) + ".y": "x-" + strings.Repeat("b", 61),
	} {
		if got := foldToLabel(in); got != want {
			t.Errorf("foldToLabel(%q) = %q, want %q", in, got, want)
		}
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
