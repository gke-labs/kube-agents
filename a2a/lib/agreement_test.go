package lib

// Envelope-subject agreement, and the attack it exists for.
//
// Built attacker-first. Before the supervisor subject split, `…events` had two
// writers - the executor and its supervisor - and the payload spec promised
// that the supervisor's `from` was how replay told "the worker said failed"
// from "the supervisor declared it dead". `from` is publisher-asserted, so a
// hostile executor could publish a terminal on its own subject wearing the
// supervisor's `from`, and nothing anywhere could tell. The first test below
// is that forgery. The rest are the consumer-side checks that make the
// subject's identity decision-grade: closed-world kind, taskId, and writer
// class, per subject class, with the one advisory check called out.

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"
)

const (
	agreementSupervisor = "gateway"
	agreementRequester  = "chatops"
)

// forgedSupervisorTerminal is the attack: a final `failed` status-update built
// by the executor for its own task, with the supervisor's identity in `from`.
func forgedSupervisorTerminal(t *testing.T, taskID string) *Envelope {
	t.Helper()
	payload, err := json.Marshal(StatusUpdate{
		TaskID: taskID, ContextID: "ctx-" + taskID,
		Status: TaskStatus{State: StateFailed, Message: &Message{Role: "agent", MessageID: "msg-forged",
			Parts: []Part{{Kind: "text", Text: "executor died; declared failed by its supervisor"}}}},
		Final: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	env, err := NewStatusUpdateEnvelope(Party{Session: agreementSupervisor, AgentType: "a2a-gateway"},
		taskID, "ctx-"+taskID, "corr-"+taskID, payload)
	if err != nil {
		t.Fatal(err)
	}
	return env
}

// supervisorTerminal is the real thing, as the gateway publishes it on the
// supervisor subject.
func supervisorTerminal(t *testing.T, taskID string, state TaskState, final bool) *Envelope {
	t.Helper()
	payload, err := json.Marshal(StatusUpdate{
		TaskID: taskID, ContextID: "ctx-" + taskID,
		Status: TaskStatus{State: state},
		Final:  final,
	})
	if err != nil {
		t.Fatal(err)
	}
	env, err := NewStatusUpdateEnvelope(Party{Session: agreementSupervisor, AgentType: "a2a-gateway"},
		taskID, "ctx-"+taskID, "corr-"+taskID, payload)
	if err != nil {
		t.Fatal(err)
	}
	return env
}

// The attack, and what the split does to it.
//
// The executor publishes a forged supervisor terminal on its own `…events`.
// The server permits it - it is the executor's own subject - and the stored
// `from` says "gateway". What the consumer must now do with it: attribute the
// bytes to the SUBJECT's principal (the executor), never to `from`; count the
// disagreement; and, once the writer check is strict, refuse the envelope
// outright. And the forgery cannot move to the subject a real supervisor
// terminal lives on: `…supervisor` is a separate writer set, refused at the
// server (the callout's session grants, proven in a2a/authcallout) and, for
// any bytes that do land there wearing the wrong `from`, refused here.
func TestAttacker_ForgedSupervisorTerminalOnTheExecutorsOwnSubject(t *testing.T) {
	const taskID = "task-forge"
	addressee := replayAddressee(taskID)
	events := TaskEventsSubject(addressee, taskID)
	forged := forgedSupervisorTerminal(t, taskID)

	t.Run("the_forgery_is_a_writer_class_disagreement_on_events", func(t *testing.T) {
		err := CheckSubjectAgreement(events, forged, AgreementPolicy{})
		if err == nil {
			t.Fatal("a terminal wearing the supervisor's from on the executor's subject passed as agreeing")
		}
		if !IsAdvisoryDisagreement(err) {
			t.Fatalf("the events writer check shipped hard; the 72h window (ratification amendment 2) makes it advisory first: %v", err)
		}
		if err := CheckSubjectAgreement(events, forged, AgreementPolicy{StrictEventsWriter: true}); err == nil || IsAdvisoryDisagreement(err) {
			t.Fatalf("with the strict flag the forgery must be a hard disagreement, got %v", err)
		}
	})

	t.Run("identity_is_the_subjects_not_froms", func(t *testing.T) {
		// What a consumer derives from the delivery: the subject names the
		// executor. from says gateway. The subject wins, by construction -
		// there is no API that returns from as the publisher.
		gotAddressee, _, class, ok := ParseTaskSubject(events)
		if !ok || class != TaskClassEvents || gotAddressee != addressee {
			t.Fatalf("ParseTaskSubject(%s) = %q %q %v", events, gotAddressee, class, ok)
		}
		if forged.From.Session == gotAddressee {
			t.Fatal("test bug: the forgery must name a principal other than the addressee")
		}
	})

	t.Run("live_replay_and_relay_count_it_and_strict_refuses_it", func(t *testing.T) {
		s := startServer(t)
		provisionTasksStream(t, clientURL(s))
		c := replayFixture(t, clientURL(s), taskID, []TaskState{StateSubmitted, StateWorking})
		ctx := testCtx(t)

		// The executor's own client lets the forgery out: the events writer
		// check is advisory at the source too, counted rather than refused,
		// which is the window's price and the reason the flag exists.
		before := c.ProtocolViolations()
		if err := c.Publish(ctx, events, forged); err != nil {
			t.Fatalf("advisory-window publish refused: %v", err)
		}
		if c.ProtocolViolations() != before+1 {
			t.Errorf("publishing the forgery counted %d violations, want 1", c.ProtocolViolations()-before)
		}

		// A default-policy reader folds it - the pre-split world's behavior,
		// and the reason a strict check cannot ship until the stream holds
		// no legitimate supervisor terminal on `…events` - but counts it.
		reader, err := Connect(ctx, clientURL(s), WithName("reader"))
		if err != nil {
			t.Fatal(err)
		}
		defer reader.Close()
		task, err := reader.TasksGet(ctx, addressee, taskID)
		if err != nil {
			t.Fatalf("TasksGet: %v", err)
		}
		if task.State != StateFailed || !task.Final {
			t.Fatalf("advisory reader folded %s final=%v, want the forged failed terminal folded and counted", task.State, task.Final)
		}
		if reader.ProtocolViolations() == 0 {
			t.Error("the advisory reader must count the disagreement it folded")
		}

		// A strict reader refuses it: the fold is what the executor really
		// said, and the forgery is a counted protocol error.
		strict, err := Connect(ctx, clientURL(s), WithName("strict-reader"),
			WithAgreementPolicy(AgreementPolicy{StrictEventsWriter: true}))
		if err != nil {
			t.Fatal(err)
		}
		defer strict.Close()
		task, err = strict.TasksGet(ctx, addressee, taskID)
		if err != nil {
			t.Fatalf("strict TasksGet: %v", err)
		}
		if task.Final || task.State != StateWorking {
			t.Fatalf("strict reader folded %s final=%v; the forged terminal must not fold", task.State, task.Final)
		}
		if strict.ProtocolViolations() == 0 {
			t.Error("the strict reader must count the refused forgery")
		}

		// A strict live consumer never hands it to the application.
		got := &collector{}
		_, err = strict.SubscribeDurable(ctx, SubscribeConfig{
			Stream: TasksStream, Subject: events, Durable: "forge-strict", Session: agreementRequester,
		}, got.handle)
		if err != nil {
			t.Fatal(err)
		}
		waitFor(t, 5*time.Second, "the two honest events", func() bool { return got.count() == 2 })
		time.Sleep(200 * time.Millisecond)
		for _, env := range got.all() {
			if env.From.Session == agreementSupervisor {
				t.Fatal("the strict durable delivered the forged supervisor terminal to the handler")
			}
		}
	})

	t.Run("on_the_supervisor_subject_the_wrong_from_is_refused_hard", func(t *testing.T) {
		// Bytes that reach `…supervisor` wearing an executor's from - the
		// relocation case, since the executor's grants never reach the
		// subject directly - are a hard disagreement whichever way the
		// consumer is configured.
		sup := TaskSupervisorSubject(addressee, taskID)
		asExecutor := supervisorTerminal(t, taskID, StateFailed, true)
		asExecutor.From = Party{Session: addressee}
		for name, p := range map[string]AgreementPolicy{
			"named_supervisor": {Supervisor: agreementSupervisor},
			"negative_form":    {},
		} {
			if err := CheckSubjectAgreement(sup, asExecutor, p); err == nil || IsAdvisoryDisagreement(err) {
				t.Errorf("%s: an executor's from on the supervisor subject passed (%v)", name, err)
			}
		}
		if err := CheckSubjectAgreement(sup, supervisorTerminal(t, taskID, StateFailed, true),
			AgreementPolicy{Supervisor: agreementSupervisor}); err != nil {
			t.Errorf("the real supervisor terminal disagreed with its own subject: %v", err)
		}
	})
}

// Condition 2, per class, closed-world on kind. Every row is an envelope
// relocated onto a subject it does not belong to, or one whose writer the
// subject does not imply; the two "agrees" rows pin the legitimate shapes so
// the check cannot be satisfied by refusing everything.
func TestRelocatedEnvelopeIsAProtocolError(t *testing.T) {
	const taskID = "task-reloc"
	addressee := replayAddressee(taskID)
	origin, err := NewMessageEnvelope(Party{Session: agreementRequester}, taskID, "ctx-"+taskID, "corr-reloc",
		validMessagePayload(), WithTo(Party{Session: addressee}))
	if err != nil {
		t.Fatal(err)
	}
	exec, err := (&Client{}).NewTaskExecution(origin, Party{Session: addressee}, addressee)
	if err != nil {
		t.Fatal(err)
	}
	working, err := exec.StatusEnvelope(StateWorking, false)
	if err != nil {
		t.Fatal(err)
	}
	artifact, err := exec.ArtifactEnvelope(Artifact{ArtifactID: "r", Name: ArtifactResult, Parts: []Part{{Kind: "text", Text: "x"}}})
	if err != nil {
		t.Fatal(err)
	}
	otherTask, err := (&Client{}).NewTaskExecution(mustMessage(t, "task-other", addressee), Party{Session: addressee}, addressee)
	if err != nil {
		t.Fatal(err)
	}
	otherWorking, err := otherTask.StatusEnvelope(StateWorking, false)
	if err != nil {
		t.Fatal(err)
	}
	bridgeWorking := cloneEnvelope(working)
	bridgeWorking.From = Party{Session: "platform-bridge", AgentType: "hermes-bridge", Profile: addressee}
	cancel, err := NewCancelEnvelope(Party{Session: agreementRequester}, taskID, "ctx-"+taskID, "corr-reloc", WithTo(Party{Session: addressee}))
	if err != nil {
		t.Fatal(err)
	}
	selfSteer := cloneEnvelope(origin)
	selfSteer.From = Party{Session: addressee}
	misaddressed := cloneEnvelope(origin)
	misaddressed.To = &Party{Session: "someone-else"}
	// Events carry no `to`; one that does, naming another session, is what
	// the check this replaced refused on every task subject.
	misaddressedEvent := cloneEnvelope(working)
	misaddressedEvent.To = &Party{Session: "someone-else"}
	misaddressedTerminal := cloneEnvelope(supervisorTerminal(t, taskID, StateFailed, true))
	misaddressedTerminal.To = &Party{Session: "someone-else"}
	addressedEvent := cloneEnvelope(working)
	addressedEvent.To = &Party{Session: addressee}
	card, err := NewAgentCardEnvelope(Party{Session: "operator", Profile: "platform"}, "corr-card", json.RawMessage(`{"name":"platform"}`))
	if err != nil {
		t.Fatal(err)
	}
	relocatedCard := cloneEnvelope(card)
	relocatedCard.From.Profile = "other-profile"
	topic, err := NewTopicUpdateEnvelope(Party{Session: addressee}, "", "", "corr-t", json.RawMessage(`{"artifactId":"a","name":"blueprint","parts":[{"kind":"text","text":"x"}]}`))
	if err != nil {
		t.Fatal(err)
	}

	events := TaskEventsSubject(addressee, taskID)
	sup := TaskSupervisorSubject(addressee, taskID)
	in := TaskInSubject(addressee, taskID)
	strict := AgreementPolicy{Supervisor: agreementSupervisor, StrictEventsWriter: true}

	cases := []struct {
		name     string
		subject  string
		env      *Envelope
		policy   AgreementPolicy
		agrees   bool
		advisory bool
	}{
		{"events: executor status agrees", events, working, strict, true, false},
		{"events: executor artifact agrees", events, artifact, strict, true, false},
		{"events: profile-addressed executor agrees by from.profile", events, bridgeWorking, strict, true, false},
		{"events: message kind is not an event", events, origin, strict, false, false},
		{"events: cancel kind is not an event", events, cancel, strict, false, false},
		{"events: topic-update kind is not an event", events, topic, strict, false, false},
		{"events: another task's event", events, otherWorking, strict, false, false},
		{"events: writer is not the addressee (strict)", events, forgedSupervisorTerminal(t, taskID), strict, false, false},
		{"events: writer is not the addressee (advisory)", events, forgedSupervisorTerminal(t, taskID), AgreementPolicy{}, false, true},
		{"supervisor: the supervisor's terminal agrees", sup, supervisorTerminal(t, taskID, StateCanceled, true), strict, true, false},
		{"supervisor: non-final status is not a supervisor event", sup, supervisorTerminal(t, taskID, StateWorking, false), strict, false, false},
		{"supervisor: artifact kind is not a supervisor event", sup, artifact, strict, false, false},
		{"supervisor: the executor's own terminal relocated", sup, mustFinal(t, exec), strict, false, false},
		{"supervisor: another task's terminal", sup, supervisorTerminal(t, "task-other", StateFailed, true), strict, false, false},
		{"supervisor: unnamed supervisor, negative form catches the executor", sup, mustFinal(t, exec), AgreementPolicy{}, false, false},
		{"supervisor: unnamed supervisor, a non-addressee passes", sup, supervisorTerminal(t, taskID, StateFailed, true), AgreementPolicy{}, true, false},
		{"in: requester message agrees", in, origin, strict, true, false},
		{"in: requester cancel agrees", in, cancel, strict, true, false},
		{"in: status-update kind is not an in kind", in, working, strict, false, false},
		{"in: to disagrees with the addressee", in, misaddressed, strict, false, false},
		// The `to` rule is not scoped to `…in`: it held on every task subject
		// before this change and still does.
		{"events: to disagrees with the addressee", events, misaddressedEvent, strict, false, false},
		{"events: to naming the addressee is fine", events, addressedEvent, strict, true, false},
		{"supervisor: to disagrees with the addressee", sup, misaddressedTerminal, strict, false, false},
		{"in: the executor writing its own in subject", in, selfSteer, strict, false, false},
		{"directory: a bound card agrees", AgentSubject("platform"), card, strict, true, false},
		{"directory: a card relocated onto another profile", AgentSubject("other-profile"), card, strict, false, false},
		{"directory: a card naming another profile", AgentSubject("platform"), relocatedCard, strict, false, false},
		{"directory: task traffic relocated onto the directory", AgentSubject("platform"), working, strict, false, false},
		{"topics: not identity-bearing, inapplicable", "a2a.topics.shared.blueprint", topic, strict, true, false},
		{"heartbeats: no envelope class, inapplicable", "agents.hb.claude-code.app." + addressee, working, strict, true, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := CheckSubjectAgreement(tc.subject, tc.env, tc.policy)
			switch {
			case tc.agrees && err != nil:
				t.Fatalf("agreeing envelope refused: %v", err)
			case !tc.agrees && err == nil:
				t.Fatal("disagreeing envelope accepted")
			case !tc.agrees && IsAdvisoryDisagreement(err) != tc.advisory:
				t.Fatalf("advisory = %v, want %v: %v", IsAdvisoryDisagreement(err), tc.advisory, err)
			}
			if err != nil {
				var aerr *AgreementError
				if !errors.As(err, &aerr) || aerr.Subject != tc.subject {
					t.Fatalf("disagreement does not name its subject: %v", err)
				}
			}
		})
	}
}

// The source-side half: a publisher's own library refuses a hard disagreement
// before it reaches the wire, so a well-behaved component cannot relocate its
// own envelopes by mistake, and the advisory check is counted there too.
func TestPublishRefusesAnEnvelopeThatDisagreesWithItsSubject(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	ctx := testCtx(t)
	c, err := Connect(ctx, clientURL(s), WithName("publisher"), WithAgreementPolicy(AgreementPolicy{Supervisor: agreementSupervisor}))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	const taskID = "task-pub"
	addressee := replayAddressee(taskID)
	origin := mustMessage(t, taskID, addressee)
	exec, err := c.NewTaskExecution(origin, Party{Session: addressee}, addressee)
	if err != nil {
		t.Fatal(err)
	}
	working, err := exec.StatusEnvelope(StateWorking, false)
	if err != nil {
		t.Fatal(err)
	}
	selfSteer := cloneEnvelope(origin)
	selfSteer.From = Party{Session: addressee}

	refused := map[string]*Envelope{
		TaskSupervisorSubject(addressee, taskID): supervisorTerminal(t, taskID, StateWorking, false),
		TaskEventsSubject(addressee, taskID):     origin,
		TaskInSubject(addressee, taskID):         selfSteer,
	}
	for subject, env := range refused {
		var perr *ProtocolError
		if err := c.Publish(ctx, subject, env); !errors.As(err, &perr) {
			t.Errorf("publish of a disagreeing envelope on %s returned %v, want a ProtocolError", subject, err)
		}
	}
	if err := c.Publish(ctx, TaskSupervisorSubject(addressee, taskID), supervisorTerminal(t, taskID, StateFailed, true)); err != nil {
		t.Errorf("the supervisor's own terminal was refused at the source: %v", err)
	}
	if err := c.Publish(ctx, TaskEventsSubject(addressee, taskID), working); err != nil {
		t.Errorf("the executor's own event was refused at the source: %v", err)
	}
}

// tasks/get after the split: one fold over both subjects in stream order, so
// assertions 9, 10 and 11 hold across the pair - first event submitted,
// exactly one final with everything after it dropped and counted, replay
// equal to what a live two-subject subscriber saw.
func TestTasksGet_FoldsTheSupervisorTerminalInStreamOrder(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	const taskID = "task-split"
	addressee := replayAddressee(taskID)
	ctx := testCtx(t)

	// A live observer configured as the supervisor's own relay is: both
	// subjects, one durable.
	obs, err := Connect(ctx, clientURL(s), WithName("observer"), WithAgreementPolicy(AgreementPolicy{Supervisor: agreementSupervisor}))
	if err != nil {
		t.Fatal(err)
	}
	defer obs.Close()
	live := &collector{}
	if _, err := obs.SubscribeDurable(ctx, SubscribeConfig{
		Stream: TasksStream, Subjects: TaskReplaySubjects(addressee, taskID),
		Durable: "split-live", Session: agreementRequester,
	}, live.handle); err != nil {
		t.Fatal(err)
	}

	c := replayFixture(t, clientURL(s), taskID, []TaskState{StateSubmitted, StateWorking})
	sup, err := Connect(ctx, clientURL(s), WithName("supervisor"), WithAgreementPolicy(AgreementPolicy{Supervisor: agreementSupervisor}))
	if err != nil {
		t.Fatal(err)
	}
	defer sup.Close()
	if err := sup.Publish(ctx, TaskSupervisorSubject(addressee, taskID), supervisorTerminal(t, taskID, StateFailed, true)); err != nil {
		t.Fatalf("supervisor terminal: %v", err)
	}
	// The zombie executor flushes after the supervisor declared it dead.
	exec, err := c.NewTaskExecution(mustMessage(t, taskID, addressee), Party{Session: addressee}, addressee)
	if err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, StateWorking, false); err != nil {
		t.Fatal(err)
	}

	waitFor(t, 5*time.Second, "four events across both subjects", func() bool { return live.count() == 4 })
	liveTask, err := FoldTask(taskID, live.all())
	if err != nil {
		t.Fatalf("fold of the live view: %v", err)
	}
	reader, err := Connect(ctx, clientURL(s), WithName("reader"), WithAgreementPolicy(AgreementPolicy{Supervisor: agreementSupervisor}))
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	replayed, err := reader.TasksGet(ctx, addressee, taskID)
	if err != nil {
		t.Fatalf("TasksGet: %v", err)
	}
	for name, task := range map[string]*Task{"live": liveTask, "replay": replayed} {
		if task.StatusHistory[0] != StateSubmitted {
			t.Errorf("%s: first event %s, want submitted (assertion 9)", name, task.StatusHistory[0])
		}
		if task.State != StateFailed || !task.Final {
			t.Errorf("%s: folded %s final=%v, want the supervisor's failed terminal", name, task.State, task.Final)
		}
		if task.PostFinalDropped != 1 {
			t.Errorf("%s: PostFinalDropped = %d, want the zombie's flush dropped (assertion 10)", name, task.PostFinalDropped)
		}
	}
	if replayed.State != liveTask.State || replayed.PostFinalDropped != liveTask.PostFinalDropped ||
		len(replayed.StatusHistory) != len(liveTask.StatusHistory) {
		t.Errorf("replay and live disagree (assertion 11):\nreplay %+v\n  live %+v", replayed, liveTask)
	}
}

// A task whose only event is the supervisor's - the spawn-failure shape, where
// the executor never existed - exists to tasks/get. The existence probe has
// to look at both subjects, or the gateway's heal path would call such a task
// unknown and never release the conversation.
func TestTasksGet_FindsATaskThatOnlyItsSupervisorWroteTo(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	const taskID = "task-suponly"
	addressee := replayAddressee(taskID)
	ctx := testCtx(t)
	sup, err := Connect(ctx, clientURL(s), WithName("supervisor"), WithAgreementPolicy(AgreementPolicy{Supervisor: agreementSupervisor}))
	if err != nil {
		t.Fatal(err)
	}
	defer sup.Close()
	if err := sup.Publish(ctx, TaskSupervisorSubject(addressee, taskID), supervisorTerminal(t, taskID, StateFailed, true)); err != nil {
		t.Fatal(err)
	}
	task, err := sup.TasksGet(ctx, addressee, taskID)
	if err != nil {
		t.Fatalf("TasksGet on a supervisor-only task: %v", err)
	}
	if task.State != StateFailed || !task.Final {
		t.Errorf("folded %s final=%v, want failed final", task.State, task.Final)
	}
}

// Ratification amendment 2: for one retention window the stream holds
// legitimate supervisor terminals on `…events`, written before the split. They
// must still replay to a terminal fold under the shipped (advisory) policy,
// and this is the test that says what the strict flag costs if it is flipped
// early.
func TestTasksGet_ATaskThatPredatesTheSplitStillReplays(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	const taskID = "task-presplit"
	addressee := replayAddressee(taskID)
	ctx := testCtx(t)
	replayFixture(t, clientURL(s), taskID, []TaskState{StateSubmitted, StateWorking})
	// The pre-split gateway's write: its terminal on `…events`, from gateway.
	// publishRaw, because the pre-split gateway had no agreement check to
	// count it.
	raw, err := json.Marshal(supervisorTerminal(t, taskID, StateCanceled, true))
	if err != nil {
		t.Fatal(err)
	}
	publishRaw(t, clientURL(s), TaskEventsSubject(addressee, taskID), raw)

	shipped, err := Connect(ctx, clientURL(s), WithName("shipped"), WithAgreementPolicy(AgreementPolicy{Supervisor: agreementSupervisor}))
	if err != nil {
		t.Fatal(err)
	}
	defer shipped.Close()
	task, err := shipped.TasksGet(ctx, addressee, taskID)
	if err != nil {
		t.Fatalf("TasksGet: %v", err)
	}
	if task.State != StateCanceled || !task.Final {
		t.Fatalf("the shipped policy folded %s final=%v; a pre-split supervisor terminal must still replay", task.State, task.Final)
	}
	if shipped.ProtocolViolations() != 1 {
		t.Errorf("violations = %d, want the historical terminal counted once", shipped.ProtocolViolations())
	}

	early, err := Connect(ctx, clientURL(s), WithName("early"), WithAgreementPolicy(AgreementPolicy{Supervisor: agreementSupervisor, StrictEventsWriter: true}))
	if err != nil {
		t.Fatal(err)
	}
	defer early.Close()
	task, err = early.TasksGet(ctx, addressee, taskID)
	if err != nil {
		t.Fatalf("strict TasksGet: %v", err)
	}
	if task.Final {
		t.Fatal("test premise broken: a strict reader is expected to refuse the historical terminal; if it folds now, the window argument needs rewriting")
	}
}

// The gateway's relay durable exists on every install with a single-subject
// filter. Rebinding it with both subjects has to be an update the server
// accepts, or the first gateway that ships the split fails at Run on a live
// install.
func TestSubscribeDurable_RebindsASingleFilterDurableToThePair(t *testing.T) {
	s := startServer(t)
	provisionTasksStream(t, clientURL(s))
	ctx := testCtx(t)
	c, err := Connect(ctx, clientURL(s), WithName("relay"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	old, err := c.SubscribeDurable(ctx, SubscribeConfig{
		Stream: TasksStream, Subject: "a2a.tasks.*.*.events", Durable: "relay-rebind", Session: agreementSupervisor,
	}, func(*Envelope) {})
	if err != nil {
		t.Fatal(err)
	}
	old.Stop()
	got := &collector{}
	if _, err := c.SubscribeDurable(ctx, SubscribeConfig{
		Stream:   TasksStream,
		Subjects: []string{"a2a.tasks.*.*.events", "a2a.tasks.*.*.supervisor"},
		Durable:  "relay-rebind", Session: agreementSupervisor,
		Agreement: &AgreementPolicy{Supervisor: agreementSupervisor},
	}, got.handle); err != nil {
		t.Fatalf("rebinding the single-filter durable to the pair: %v", err)
	}
	const taskID = "task-rebind"
	addressee := replayAddressee(taskID)
	replayFixture(t, clientURL(s), taskID, []TaskState{StateSubmitted})
	sup, err := Connect(ctx, clientURL(s), WithName("supervisor"), WithAgreementPolicy(AgreementPolicy{Supervisor: agreementSupervisor}))
	if err != nil {
		t.Fatal(err)
	}
	defer sup.Close()
	if err := sup.Publish(ctx, TaskSupervisorSubject(addressee, taskID), supervisorTerminal(t, taskID, StateFailed, true)); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 5*time.Second, "both subjects delivered through the rebound durable", func() bool { return got.count() == 2 })
}

func mustMessage(t *testing.T, taskID, addressee string) *Envelope {
	t.Helper()
	env, err := NewMessageEnvelope(Party{Session: agreementRequester}, taskID, "ctx-"+taskID, "corr-"+taskID,
		validMessagePayload(), WithTo(Party{Session: addressee}))
	if err != nil {
		t.Fatal(err)
	}
	return env
}

func mustFinal(t *testing.T, exec *TaskExecution) *Envelope {
	t.Helper()
	env, err := exec.StatusEnvelope(StateCompleted, true)
	if err != nil {
		t.Fatal(err)
	}
	return env
}

func cloneEnvelope(e *Envelope) *Envelope {
	c := *e
	if e.To != nil {
		to := *e.To
		c.To = &to
	}
	return &c
}

// TestSubscribeDurable_RetriesABindThatRacesStreamRecovery models the live
// failure that this retry exists for: a server accepting connections before
// its JetStream state is current, so the first bind is answered against state
// that is not there yet. Here the stream is simply absent when the bind
// starts and appears while it retries.
func TestSubscribeDurable_RetriesABindThatRacesStreamRecovery(t *testing.T) {
	s := startServer(t)
	ctx := testCtx(t)
	c, err := Connect(ctx, clientURL(s), WithName("relay"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()

	got := &collector{}
	type result struct {
		sub Subscription
		err error
	}
	done := make(chan result, 1)
	go func() {
		sub, err := c.SubscribeDurable(ctx, SubscribeConfig{
			Stream:   TasksStream,
			Subjects: []string{"a2a.tasks.*.*.events", "a2a.tasks.*.*.supervisor"},
			Durable:  "relay-recovery-race", Session: agreementSupervisor,
			Agreement: &AgreementPolicy{Supervisor: agreementSupervisor},
		}, got.handle)
		done <- result{sub, err}
	}()

	// The bind is already failing against a stream that does not exist.
	select {
	case r := <-done:
		t.Fatalf("SubscribeDurable returned before the stream existed: sub=%v err=%v", r.sub, r.err)
	case <-time.After(500 * time.Millisecond):
	}
	provisionTasksStream(t, clientURL(s))

	select {
	case r := <-done:
		if r.err != nil {
			t.Fatalf("the bind did not recover once the stream appeared: %v", r.err)
		}
		defer r.sub.Stop()
	case <-time.After(20 * time.Second):
		t.Fatal("SubscribeDurable never returned after the stream appeared")
	}
	replayFixture(t, clientURL(s), "task-recovery-race", []TaskState{StateSubmitted})
	waitFor(t, 5*time.Second, "the recovered durable delivers", func() bool { return got.count() == 1 })
}

// TestSubscribeDurable_GivesUpOnABindTheServerWillNeverAccept: the retry is
// bounded, and the caller's context bounds it too. A consumer config no
// server will accept must fail the process, not hang it.
func TestSubscribeDurable_GivesUpOnABindTheServerWillNeverAccept(t *testing.T) {
	s := startServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	c, err := Connect(context.Background(), clientURL(s), WithName("relay"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	start := time.Now()
	// No stream, ever.
	if _, err := c.SubscribeDurable(ctx, SubscribeConfig{
		Stream: TasksStream, Subject: "a2a.tasks.*.*.events",
		Durable: "relay-never", Session: agreementSupervisor,
	}, func(*Envelope) {}); err == nil {
		t.Fatal("SubscribeDurable succeeded against a stream that does not exist")
	}
	if elapsed := time.Since(start); elapsed > subscribeBindWindow {
		t.Errorf("the bind outlived its own window: %v", elapsed)
	}
}
