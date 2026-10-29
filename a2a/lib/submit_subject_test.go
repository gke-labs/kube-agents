package lib

// The submission subject (#3660, decided 2026-10-22): a new task for a
// profile goes on a2a.tasks.{profile}.{tier}.{kind}.{taskId}.submit, while
// its follow-ups and cancels stay on the five-token `…in`. The property the
// split exists for is the routing one: a durable on a profile's submissions
// never sees a follow-up, and nothing filtering the five-token subjects ever
// sees a submission.

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

func TestSubmitSubjectRoundTrips(t *testing.T) {
	subject := TaskSubmitSubject("cluster-agent", "read", SubmitKindChild, "task-1")
	if subject != "a2a.tasks.cluster-agent.read.child.task-1.submit" {
		t.Fatalf("TaskSubmitSubject = %q", subject)
	}
	profile, tier, kind, taskID, ok := ParseSubmitSubject(subject)
	if !ok || profile != "cluster-agent" || tier != "read" || kind != SubmitKindChild || taskID != "task-1" {
		t.Errorf("ParseSubmitSubject(%q) = %q %q %q %q %v", subject, profile, tier, kind, taskID, ok)
	}
	if _, _, _, ok := ParseTaskSubject(subject); ok {
		t.Errorf("ParseTaskSubject accepted the submission subject %q", subject)
	}
	for _, s := range []string{
		TaskInSubject("cluster-agent", "task-1"),
		"a2a.tasks.cluster-agent.read.other.task-1.submit", // not top or child
		"a2a.tasks.cluster-agent.read.top.task-1.in",
		"a2a.tasks.cluster-agent.read.top.submit",
		"a2a.tasks.cluster-agent..top.task-1.submit",
		"a2a.tasks.cluster-agent.read.top.task-1.extra.submit",
	} {
		if _, _, _, _, ok := ParseSubmitSubject(s); ok {
			t.Errorf("ParseSubmitSubject accepted %q", s)
		}
	}
}

func submission(t *testing.T, from Party, taskID, profile string) *Envelope {
	t.Helper()
	env, err := NewMessageEnvelope(from, taskID, "ctx-"+taskID, "corr-"+taskID,
		validMessagePayload(), WithTo(Party{Session: profile}))
	if err != nil {
		t.Fatal(err)
	}
	return env
}

// The gateway submits as itself, including a child it mints for the same
// profile; everything else that disagrees with the tokens is refused.
func TestSubmitSubjectAgreement(t *testing.T) {
	const profile, taskID = "cluster-agent", "task-1"
	subject := TaskSubmitSubject(profile, "read", SubmitKindTop, taskID)
	gateway := Party{Session: "gateway", AgentType: "a2a-gateway"}
	if err := CheckSubjectAgreement(subject, submission(t, gateway, taskID, profile), AgreementPolicy{}); err != nil {
		t.Errorf("the gateway's submission was refused: %v", err)
	}
	child := TaskSubmitSubject(profile, "read", SubmitKindChild, "task-2")
	if err := CheckSubjectAgreement(child, submission(t, gateway, "task-2", profile), AgreementPolicy{}); err != nil {
		t.Errorf("a same-profile child the gateway minted was refused: %v", err)
	}

	cancel, err := NewCancelEnvelope(gateway, taskID, "ctx-"+taskID, "corr-"+taskID, WithTo(Party{Session: profile}))
	if err != nil {
		t.Fatal(err)
	}
	refused := map[string]*Envelope{
		"a cancel":                              cancel,
		"another task's id":                     submission(t, gateway, "task-other", profile),
		"a to naming another profile":           submission(t, gateway, taskID, "chat"),
		"the profile submitting to itself":      submission(t, Party{Session: "cluster-agent-x", Profile: profile}, taskID, profile),
		"a writer whose session is the profile": submission(t, Party{Session: profile}, taskID, profile),
	}
	for name, env := range refused {
		var agreeErr *AgreementError
		if err := CheckSubjectAgreement(subject, env, AgreementPolicy{}); !errors.As(err, &agreeErr) || agreeErr.Advisory {
			t.Errorf("%s on a submission subject: %v, want a hard disagreement", name, err)
		}
	}

	// A consumer that names the submitter refuses every other writer, not
	// only the profile.
	named := AgreementPolicy{Submitter: "gateway"}
	if err := CheckSubjectAgreement(subject, submission(t, gateway, taskID, profile), named); err != nil {
		t.Errorf("the named submitter's submission was refused: %v", err)
	}
	var agreeErr *AgreementError
	if err := CheckSubjectAgreement(subject, submission(t, Party{Session: "chat-otter"}, taskID, profile), named); !errors.As(err, &agreeErr) {
		t.Errorf("a submission from another writer under a named submitter: %v, want a disagreement", err)
	}
}

// The delivery path the dispatcher will use: a durable on one (profile, tier,
// kind)'s submissions, Session the profile, the gateway named as submitter.
// The gateway's submission is delivered; another writer's is not.
func TestASubmissionIsDeliveredOnlyFromTheSubmitter(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	ctx := testCtx(t)
	pub, err := Connect(ctx, clientURL(s), WithName("publisher"))
	if err != nil {
		t.Fatal(err)
	}
	defer pub.Close()
	disp, err := Connect(ctx, clientURL(s), WithName("dispatcher"), WithAgreementPolicy(AgreementPolicy{Submitter: "gateway"}))
	if err != nil {
		t.Fatal(err)
	}
	defer disp.Close()
	const profile = "cluster-agent"
	got := make(chan string, 4)
	sub, err := disp.SubscribeDurable(ctx, SubscribeConfig{
		Stream: "TASKS", Durable: "dispatch-" + profile + "-read-top",
		Subject: "a2a.tasks." + profile + ".read.top.*.submit", Session: profile,
	}, func(env *Envelope) { got <- env.TaskID })
	if err != nil {
		t.Fatalf("SubscribeDurable: %v", err)
	}
	defer sub.Stop()
	if err := pub.Publish(ctx, TaskSubmitSubject(profile, "read", SubmitKindTop, "task-intruder"),
		submission(t, Party{Session: "chat-otter"}, "task-intruder", profile)); err != nil {
		t.Fatalf("publish the other writer's submission: %v", err)
	}
	if err := pub.Publish(ctx, TaskSubmitSubject(profile, "read", SubmitKindTop, "task-1"),
		submission(t, Party{Session: "gateway", AgentType: "a2a-gateway"}, "task-1", profile)); err != nil {
		t.Fatalf("publish the gateway's submission: %v", err)
	}
	select {
	case id := <-got:
		if id != "task-1" {
			t.Fatalf("delivered %q first, want only the gateway's task-1", id)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the gateway's submission was not delivered")
	}
	select {
	case id := <-got:
		t.Errorf("also delivered %q, want only the gateway's submission", id)
	case <-time.After(time.Second):
	}
}

// Publishing checks the submission subject's tokens and its agreement at the
// source, as it does every task subject's.
func TestPublishChecksASubmissionSubject(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	ctx := testCtx(t)
	c, err := Connect(ctx, clientURL(s), WithName("gateway"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	gateway := Party{Session: "gateway", AgentType: "a2a-gateway"}
	if err := c.Publish(ctx, TaskSubmitSubject("cluster-agent", "read", SubmitKindTop, "task-1"),
		submission(t, gateway, "task-1", "cluster-agent")); err != nil {
		t.Fatalf("publish of a well-formed submission: %v", err)
	}
	for subject, want := range map[string]string{
		"a2a.tasks.cluster-agent.Read.top.task-2.submit":   "malformed submission subject",
		"a2a.tasks.cluster-agent.r_d.top.task-2.submit":    "malformed submission subject",
		"a2a.tasks.cluster-agent.read.other.task-2.submit": "want a2a.tasks.<profile>.<tier>.<top|child>.<taskId>.submit",
	} {
		var perr *ProtocolError
		err := c.Publish(ctx, subject, submission(t, gateway, "task-2", "cluster-agent"))
		if !errors.As(err, &perr) || !strings.Contains(err.Error(), want) {
			t.Errorf("publish on %q = %v, want a ProtocolError saying %q", subject, err, want)
		}
	}
	var perr *ProtocolError
	if err := c.Publish(ctx, TaskSubmitSubject("cluster-agent", "read", SubmitKindTop, "task-3"),
		submission(t, Party{Session: "cluster-agent-x", Profile: "cluster-agent"}, "task-3", "cluster-agent")); !errors.As(err, &perr) {
		t.Errorf("publish of a profile's submission to itself = %v, want a ProtocolError", err)
	}
}

// The routing property: a durable on one (profile, tier, kind)'s submissions
// takes only those, and a consumer on the profile's five-token `…in` subjects
// takes the follow-up and never the submission.
func TestSubmissionsAndFollowUpsReachDifferentConsumers(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	ctx := testCtx(t)
	c, err := Connect(ctx, clientURL(s), WithName("gateway"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	gateway := Party{Session: "gateway", AgentType: "a2a-gateway"}
	const profile = "cluster-agent"
	publish := map[string]*Envelope{
		TaskSubmitSubject(profile, "read", SubmitKindTop, "task-1"):   submission(t, gateway, "task-1", profile),
		TaskSubmitSubject(profile, "read", SubmitKindChild, "task-2"): submission(t, gateway, "task-2", profile),
		TaskSubmitSubject(profile, "write", SubmitKindTop, "task-3"):  submission(t, gateway, "task-3", profile),
		TaskInSubject(profile, "task-1"):                              submission(t, gateway, "task-1", profile),
	}
	for subject, env := range publish {
		if err := c.Publish(ctx, subject, env); err != nil {
			t.Fatalf("publish %s: %v", subject, err)
		}
	}
	_, js := c.conn()
	got := func(filter string) []string {
		t.Helper()
		cons, err := js.OrderedConsumer(ctx, "TASKS", jetstream.OrderedConsumerConfig{FilterSubjects: []string{filter}})
		if err != nil {
			t.Fatalf("consumer on %s: %v", filter, err)
		}
		var subjects []string
		for {
			msg, err := cons.Next(jetstream.FetchMaxWait(400 * time.Millisecond))
			if err != nil {
				return subjects
			}
			subjects = append(subjects, msg.Subject())
		}
	}
	if s := got("a2a.tasks." + profile + ".read.top.*.submit"); len(s) != 1 || s[0] != TaskSubmitSubject(profile, "read", SubmitKindTop, "task-1") {
		t.Errorf("the (read, top) durable's filter took %v, want only task-1's submission", s)
	}
	if s := got("a2a.tasks." + profile + ".*.in"); len(s) != 1 || s[0] != TaskInSubject(profile, "task-1") {
		t.Errorf("the profile's `…in` filter took %v, want only the follow-up", s)
	}
}
