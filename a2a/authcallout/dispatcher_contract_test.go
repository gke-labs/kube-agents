package authcallout

import (
	"context"
	"os"
	"strconv"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The dispatcher's principal as the operator renders it when it has an image
// for the dispatcher (k8s-operator: TestRenderedDispatcherFixtures writes the
// fixture), run through the callout and a real server.
const (
	dispatcherFixturePath = "testdata/rendered-identity-map-dispatcher.json"
	renderedDispatcherSA  = "system:serviceaccount:kubeagents-system:agent-a2a-dispatcher"
	tokenDispatcher       = "token-for-the-rendered-dispatcher-serviceaccount-padded-to-len"
	// renderedConsolePassword is the console's in the rendered nats.conf.
	renderedConsolePassword = "pw-console"
)

func renderedDispatcherMap(t *testing.T) string {
	t.Helper()
	raw, err := os.ReadFile(dispatcherFixturePath)
	if err != nil {
		t.Fatalf("reading %s: %v\nRegenerate with: go test ./internal/controller/ -run 'Fixture' -update (in k8s-operator)", dispatcherFixturePath, err)
	}
	return string(raw)
}

// The map parses with the dispatcher in it, as a plain callout principal in the
// control-plane account.
func TestTheOperatorsDispatcherMapParses(t *testing.T) {
	m, err := ParseIdentityMap([]byte(renderedDispatcherMap(t)))
	if err != nil {
		t.Fatalf("the operator's map with the dispatcher does not parse: %v", err)
	}
	wantAccount := map[string]string{
		"gateway":          AccountControl,
		"provision":        AccountControl,
		"verifier":         AccountControl,
		"operator":         AccountControl,
		"dispatcher":       AccountControl,
		"agent":            AccountApp,
		"session":          AccountApp,
		"profile-tse-read": AccountApp,
	}
	for _, id := range m.Identities {
		if want, ok := wantAccount[id.User]; ok && id.Account != want {
			t.Errorf("principal %q account = %q, want %s", id.User, id.Account, want)
		}
		if id.User == "dispatcher" {
			if id.ServiceAccount != renderedDispatcherSA || id.Narrowing != "" {
				t.Errorf("dispatcher entry = %+v", id)
			}
		}
	}
	if _, ok := m.Lookup(renderedDispatcherSA); !ok {
		t.Fatal("the operator's map has no dispatcher")
	}
}

// Control-plane subjects inside TRUSTED (KV buckets, task inputs, supervisor
// terminals, and dispatcher offers) are not reachable from APP or CONSOLE
// across the account boundary, while the exported worker and console paths
// cross the boundary cleanly.
func TestAccountBoundaryIsolatesControlPlaneSubjects(t *testing.T) {
	hooks := map[string]Attested{
		tokenDispatcher:  {ServiceAccount: renderedDispatcherSA},
		tokenVerifier:    {ServiceAccount: verifierSA},
		tokenProvision:   {ServiceAccount: provisionSA},
		tokenRenderedOperator: {ServiceAccount: renderedOperatorSA},
		agentToken:       {ServiceAccount: agentSA},
		tokenPodA:        {ServiceAccount: sessionSA, PodName: podA, PodUID: "uid-a"},
		tokenAuditorPod1: {ServiceAccount: "system:serviceaccount:kubeagents-system-profiles:agentprofile-tse-read", PodName: "tse-read-7f9c", PodUID: "uid-tse"},
	}
	h := startHarness(t, renderedDispatcherMap(t), hooks)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	seed, _ := connectStatic(t, h, "seed", renderedSeedPassword)
	seedJS, err := jetstream.New(seed)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := seedJS.CreateStream(ctx, jetstream.StreamConfig{
		Name: lib.TasksStream, Subjects: []string{"a2a.tasks.>"}, AllowDirect: true,
	}); err != nil {
		t.Fatalf("creating TASKS as seed: %v", err)
	}
	if _, err := seedJS.CreateStream(ctx, jetstream.StreamConfig{
		Name: "DIRECTORY", Subjects: []string{"a2a.agents.>"}, AllowDirect: true,
	}); err != nil {
		t.Fatalf("creating DIRECTORY as seed: %v", err)
	}
	if _, err := seedJS.CreateStream(ctx, jetstream.StreamConfig{
		Name: lib.StreamTopicsState, Subjects: []string{"a2a.topics.>"}, AllowDirect: true,
	}); err != nil {
		t.Fatalf("creating %s as seed: %v", lib.StreamTopicsState, err)
	}
	if _, err := seedJS.CreateStream(ctx, jetstream.StreamConfig{
		Name: lib.ProfileSubmitStream, Subjects: []string{lib.SubmitSubjectPrefix + ">"}, AllowDirect: true,
	}); err != nil {
		t.Fatalf("creating %s as seed: %v", lib.ProfileSubmitStream, err)
	}
	for _, bucket := range []string{lib.ProfileLeaseBucket, "cap", "session-state", "runtime-state", "feedback"} {
		if _, err := seedJS.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: bucket, History: 1}); err != nil {
			t.Fatalf("creating KV %s as seed: %v", bucket, err)
		}
	}

	disp, _ := h.connectAs(t, "dispatcher", tokenDispatcher)
	verif, _ := h.connectAs(t, "verifier", tokenVerifier)
	prov, _ := h.connectAs(t, "provision", tokenProvision)
	opConn, _ := h.connectAs(t, "operator", tokenRenderedOperator)
	gw, _ := connectStatic(t, h, "gateway", "pw-gateway")
	agentConn, agentViolations := h.connectAs(t, "agent", agentToken)
	worker, workerViolations := h.connectAs(t, "tse-read-7f9c", tokenAuditorPod1)
	sess, sessViolations := h.connectAs(t, podA, tokenPodA)
	bridgeConn, bridgeViolations := connectStatic(t, h, "bridge", "pw-bridge")
	webConn, webViolations := connectStatic(t, h, "web", "pw-web")
	console, consoleViolations := connectStatic(t, h, "console", renderedConsolePassword)
	consoleMCP, mcpViolations := connectStatic(t, h, "console-mcp", "pw-console-mcp")

	// Seed stored messages in TASKS and TOPICS-STATE so DIRECT.GET and
	// MSG.NEXT calls from both APP and CONSOLE return real messages.
	if err := sess.Publish(lib.TaskEventsSubject(podA, "t-probe"), []byte(`{"forged":"from-session"}`)); err != nil {
		t.Fatal(err)
	}
	if err := sess.Flush(); err != nil {
		t.Fatal(err)
	}
	if err := seed.Publish("a2a.topics.shared.blueprint", []byte(`{"forged":"from-topic"}`)); err != nil {
		t.Fatal(err)
	}
	if err := seed.Flush(); err != nil {
		t.Fatal(err)
	}
	submitSubj := lib.TaskSubmitSubject("tse", "read", "top", "t-sub1")

	// Create granted pull consumers in both APP (session on TASKS) and CONSOLE
	// (console-mcp on TASKS and TOPICS-STATE) so both accounts exercise
	// DIRECT.GET, MSG.NEXT, and CONSUMER.CREATE deliver_subject redirection
	// across every target subject.
	consName := lib.SessionConsumerName(podA, lib.SessionConsumerEvents)
	consFilter := lib.TaskEventsSubject(podA, "*")
	createBody := `{"stream_name":"TASKS","config":{"name":"` + consName +
		`","filter_subject":"` + consFilter + `","ack_policy":"none","deliver_policy":"all"}}`
	if _, err := sess.Request(consumerSubject("CREATE", consName)+"."+consFilter, []byte(createBody), 3*time.Second); err != nil {
		t.Fatalf("session creating its own consumer: %v", err)
	}
	mcpConsName := "mcp-probe-tasks"
	mcpCreateBody := `{"stream_name":"TASKS","config":{"name":"` + mcpConsName +
		`","filter_subject":"` + consFilter + `","ack_policy":"none","deliver_policy":"all"}}`
	if _, err := consoleMCP.Request("$JS.API.CONSUMER.CREATE."+lib.TasksStream+"."+mcpConsName+"."+consFilter, []byte(mcpCreateBody), 3*time.Second); err != nil {
		t.Fatalf("console-mcp creating its own TASKS consumer: %v", err)
	}
	mcpTopicsConsName := "mcp-probe-topics"
	mcpTopicsBody := `{"stream_name":"TOPICS-STATE","config":{"name":"` + mcpTopicsConsName +
		`","filter_subject":"a2a.topics.shared.blueprint","ack_policy":"none","deliver_policy":"all"}}`
	if _, err := consoleMCP.Request("$JS.API.CONSUMER.CREATE."+lib.StreamTopicsState+"."+mcpTopicsConsName+".a2a.topics.shared.blueprint", []byte(mcpTopicsBody), 3*time.Second); err != nil {
		t.Fatalf("console-mcp creating its own TOPICS-STATE consumer: %v", err)
	}

	// Every subject any TRUSTED principal (gateway, dispatcher, verifier,
	// operator, provision, seed) subscribes to or trusts by publisher, plus
	// every TRUSTED-only stream/KV subject and every TRUSTED _INBOX.*.>.
	targets := []struct {
		subject string
		subConn *nats.Conn
	}{
		{"a2a.tasks.platform.t-victim.in", gw},
		{"a2a.tasks.platform.t-victim.supervisor", gw},
		{"a2a.tasks.tse.t-victim.events", gw},
		{"a2a.submit.tse.read.top.t-forged", gw},
		{"a2a.agents.forged", gw},
		{"agents.hb.forged", gw},
		{"a2a.topics.shared.annotations", seed},
		{"$KV.cap.root.forged", verif},
		{"$KV.session-state.forged", gw},
		{"$KV.feedback.forged", gw},
		{"$KV.runtime-state.forged", gw},
		{"$KV.PROFILE_LEASES.tse.read.pod.t1", disp},
		{"chat.console.c1.in", gw},
		{"chat.console.c1.confirm", gw},
		{"chat.notify.gchat", gw},
		{"chat.notify.slack", gw},
		{"chat.notify.console", gw},
		{"chat.board.reply.gateway.req1", gw},
		{"a2a.cap.verify.gateway", verif},
		{"a2a.cap.verify.dispatcher", verif},
		{"a2a.cap.verify.platform", verif},
		{"a2a.cap.reply.dispatcher.req1", disp},
		{lib.DispatchAcceptSubject("tse", "read", "tse-read-7f9c"), disp},
		{lib.DispatchLeaseSubject("tse", "read", "tse-read-7f9c"), disp},
		{"_INBOX.gateway.reply-probe", gw},
		{"_INBOX.dispatcher.reply-probe", disp},
		{"_INBOX.verifier.reply-probe", verif},
		{"_INBOX.operator.reply-probe", opConn},
		{"_INBOX.provision.reply-probe", prov},
		{"_INBOX.seed.reply-probe", seed},
	}

	subs := make([]*nats.Subscription, len(targets))
	for i, tgt := range targets {
		sub, err := tgt.subConn.SubscribeSync(tgt.subject)
		if err != nil {
			t.Fatalf("subscribing in TRUSTED to %s: %v", tgt.subject, err)
		}
		subs[i] = sub
	}
	// Also verify that no APP or CONSOLE principal can subscribe to
	// a2a.cap.reply.dispatcher.> (even though TRUSTED exports a2a.cap.reply.>
	// to APP for bridge and session capability replies).
	for _, caller := range []struct {
		name       string
		conn       *nats.Conn
		violations chan error
	}{
		{"agent", agentConn, agentViolations},
		{"worker", worker, workerViolations},
		{"session", sess, sessViolations},
		{"bridge", bridgeConn, bridgeViolations},
		{"web", webConn, webViolations},
		{"console", console, consoleViolations},
		{"console-mcp", consoleMCP, mcpViolations},
	} {
		if !subscribeRefused(t, caller.conn, caller.violations, "a2a.cap.reply.dispatcher.>") {
			t.Fatalf("%s was allowed to subscribe to a2a.cap.reply.dispatcher.>", caller.name)
		}
	}
	for _, c := range []*nats.Conn{gw, disp, verif, opConn, prov, seed} {
		if err := c.Flush(); err != nil {
			t.Fatal(err)
		}
	}

	// Positive sanity checks that the seeded messages in TOPICS-STATE and
	// TASKS are live and readable from both APP and CONSOLE before the
	// negative reflection loop.
	if msg, err := agentConn.RequestWithContext(ctx, "$JS.API.DIRECT.GET."+lib.StreamTopicsState+".a2a.topics.shared.blueprint", nil); err != nil || string(msg.Data) != `{"forged":"from-topic"}` {
		t.Fatalf("positive DIRECT.GET TOPICS-STATE check failed: %v, %v", msg, err)
	}
	if msg, err := bridgeConn.RequestWithContext(ctx, "$JS.API.DIRECT.GET."+lib.TasksStream+"."+lib.TaskEventsSubject(podA, "t-probe"), nil); err != nil || string(msg.Data) != `{"forged":"from-session"}` {
		t.Fatalf("positive DIRECT.GET TASKS check failed: %v, %v", msg, err)
	}
	if msg, err := consoleMCP.RequestWithContext(ctx, "$JS.API.CONSUMER.MSG.NEXT."+lib.StreamTopicsState+"."+mcpTopicsConsName, []byte(`{"batch":1,"no_wait":true}`)); err != nil || string(msg.Data) != `{"forged":"from-topic"}` {
		t.Fatalf("positive console-mcp MSG.NEXT TOPICS-STATE check failed: %v, %v", msg, err)
	}

	for idx, tgt := range targets {
		// 1. Agent in APP attempts DIRECT.GET (normal and no_hdr) to
		// TOPICS-STATE with Reply set to the target subject.
		for _, body := range []string{
			`{"last_by_subj":"a2a.topics.shared.blueprint"}`,
			`{"last_by_subj":"a2a.topics.shared.blueprint","no_hdr":true}`,
		} {
			_ = agentConn.PublishRequest("$JS.API.DIRECT.GET."+lib.StreamTopicsState, tgt.subject, []byte(body))
			_ = agentConn.PublishRequest("$JS.API.DIRECT.GET."+lib.StreamTopicsState+".a2a.topics.shared.blueprint", tgt.subject, nil)
		}
		// 2. Bridge in APP attempts DIRECT.GET (normal and no_hdr) to TASKS
		// with Reply set to the target subject.
		for _, body := range []string{
			`{"last_by_subj":"` + lib.TaskEventsSubject(podA, "t-probe") + `"}`,
			`{"last_by_subj":"` + lib.TaskEventsSubject(podA, "t-probe") + `","no_hdr":true}`,
		} {
			_ = bridgeConn.PublishRequest("$JS.API.DIRECT.GET."+lib.TasksStream, tgt.subject, []byte(body))
			_ = bridgeConn.PublishRequest("$JS.API.DIRECT.GET."+lib.TasksStream+"."+lib.TaskEventsSubject(podA, "t-probe"), tgt.subject, nil)
		}
		// 3. Session in APP attempts MSG.NEXT on its own consumer and push
		// CONSUMER.CREATE deliver_subject targeting the subject.
		// Note: session and profile worker grants name their 3 consumers
		// explicitly, while static principals with CONSUMER.CREATE.<stream>.>
		// (bridge, web, console, console-mcp) hold a wildcard consumer name;
		// NATS evaluates a push consumer's deliver_subject inside the
		// JetStream-hosting account (TRUSTED) regardless of the caller's
		// account, so CONSUMER.CREATE.<stream>.> remains the documented
		// deliver_subject residue on those static readers until pre-created
		// consumers replace CONSUMER.CREATE.
		_ = sess.Publish(lib.TaskEventsSubject(podA, "t-probe"), []byte(`{"forged":"from-session"}`))
		_ = sess.PublishRequest(consumerSubject("MSG.NEXT", consName), tgt.subject, []byte(`{"batch":1,"no_wait":true}`))
		pushName := "sess-push-" + strconv.Itoa(idx)
		pushBody := `{"stream_name":"TASKS","config":{"name":"` + pushName + `","deliver_subject":"` + tgt.subject + `","filter_subject":"` + consFilter + `","ack_policy":"none"}}`
		_ = sess.PublishRequest(consumerSubject("CREATE", pushName)+"."+consFilter, "_INBOX."+podA+".create", []byte(pushBody))

		// 4. console-mcp in CONSOLE attempts DIRECT.GET on PROFILE_SUBMITS and
		// MSG.NEXT on TASKS and TOPICS-STATE with Reply set to the target
		// subject.
		for _, body := range []string{
			`{"last_by_subj":"` + submitSubj + `"}`,
			`{"last_by_subj":"` + submitSubj + `","no_hdr":true}`,
		} {
			_ = consoleMCP.PublishRequest("$JS.API.DIRECT.GET."+lib.ProfileSubmitStream, tgt.subject, []byte(body))
			_ = consoleMCP.PublishRequest("$JS.API.DIRECT.GET."+lib.ProfileSubmitStream+"."+submitSubj, tgt.subject, nil)
		}
		_ = seed.Publish("a2a.topics.shared.blueprint", []byte(`{"forged":"from-topic"}`))
		_ = consoleMCP.PublishRequest("$JS.API.CONSUMER.MSG.NEXT."+lib.TasksStream+"."+mcpConsName, tgt.subject, []byte(`{"batch":1,"no_wait":true}`))
		_ = consoleMCP.PublishRequest("$JS.API.CONSUMER.MSG.NEXT."+lib.StreamTopicsState+"."+mcpTopicsConsName, tgt.subject, []byte(`{"batch":1,"no_wait":true}`))
		// 5. Cross-account core-NATS service imports into TRUSTED:
		// a2a.tasks.*.*.events, a2a.topics.>, $KV.runtime-state.>,
		// $JS.API.INFO, $JS.API.STREAM.INFO.*, $JS.API.CONSUMER.INFO.>,
		// $JS.API.CONSUMER.DELETE.> called with Reply set to the target subject.
		_ = sess.PublishRequest(lib.TaskEventsSubject(podA, "t-probe"), tgt.subject, []byte(`{"forged":"svc-events"}`))
		_ = bridgeConn.PublishRequest("$KV.runtime-state.bridge.platform.t1", tgt.subject, []byte(`{"forged":"svc-kv"}`))
		_ = agentConn.PublishRequest("$JS.API.INFO", tgt.subject, nil)
		_ = agentConn.PublishRequest("$JS.API.STREAM.INFO."+lib.StreamTopicsState, tgt.subject, nil)
		_ = sess.PublishRequest(consumerSubject("INFO", consName), tgt.subject, nil)
		_ = consoleMCP.PublishRequest("$JS.API.INFO", tgt.subject, nil)
		_ = consoleMCP.PublishRequest("$JS.API.STREAM.INFO."+lib.TasksStream, tgt.subject, nil)
		_ = consoleMCP.PublishRequest("$JS.API.CONSUMER.INFO."+lib.TasksStream+"."+mcpConsName, tgt.subject, nil)
	}
	_ = agentConn.Flush()
	_ = bridgeConn.Flush()
	_ = sess.Flush()
	_ = consoleMCP.Flush()

	// Exclude the one legitimate direct-publish path: sess.Publish(TaskEventsSubject)
	// in the loop goes to a2a.tasks.<podA>.t-probe.events (not tse), so none of
	// the 26 targets above should receive a single message.
	for i, tgt := range targets {
		if msg, err := subs[i].NextMsg(100 * time.Millisecond); err == nil {
			t.Errorf("cross-account reply landed on TRUSTED subject %s: subj=%q reply=%q hdr=%v data=%q",
				tgt.subject, msg.Subject, msg.Reply, msg.Header, msg.Data)
		}
		_ = subs[i].Unsubscribe()
	}

	// 1. Dispatcher (TRUSTED) -> worker (APP) offer, and worker (APP) ->
	// dispatcher (TRUSTED) accept/lease cross the account boundary.
	offerSub, err := worker.SubscribeSync(lib.DispatchSubject("tse", "read", "tse-read-7f9c"))
	if err != nil {
		t.Fatalf("worker subscribe offer: %v", err)
	}
	acceptSub, err := disp.SubscribeSync(lib.DispatchAcceptSubject("tse", "read", "tse-read-7f9c"))
	if err != nil {
		t.Fatalf("dispatcher subscribe accept: %v", err)
	}
	if err := worker.Flush(); err != nil {
		t.Fatal(err)
	}
	if err := disp.Flush(); err != nil {
		t.Fatal(err)
	}
	if err := disp.Publish(lib.DispatchSubject("tse", "read", "tse-read-7f9c"), []byte("offer")); err != nil {
		t.Fatal(err)
	}
	if _, err := offerSub.NextMsg(2 * time.Second); err != nil {
		t.Fatalf("dispatcher offer did not cross TRUSTED -> APP: %v", err)
	}
	if err := worker.Publish(lib.DispatchAcceptSubject("tse", "read", "tse-read-7f9c"), []byte("accept")); err != nil {
		t.Fatal(err)
	}
	if _, err := acceptSub.NextMsg(2 * time.Second); err != nil {
		t.Fatalf("worker accept did not cross APP -> TRUSTED: %v", err)
	}

	// 2. console (CONSOLE) can publish chat.console.c1.confirm to gateway
	// (TRUSTED), while console-mcp (CONSOLE) is refused.
	confirmSub, err := gw.SubscribeSync("chat.console.c1.confirm")
	if err != nil {
		t.Fatalf("gateway subscribe confirm: %v", err)
	}
	if err := gw.Flush(); err != nil {
		t.Fatal(err)
	}
	if publishRefused(t, console, consoleViolations, "chat.console.c1.confirm") {
		t.Fatal("console was refused chat.console.c1.confirm")
	}
	if _, err := confirmSub.NextMsg(2 * time.Second); err != nil {
		t.Fatalf("console confirm did not cross CONSOLE -> TRUSTED: %v", err)
	}
	if !publishRefused(t, consoleMCP, mcpViolations, "chat.console.c1.confirm") {
		t.Fatal("console-mcp was allowed to publish chat.console.c1.confirm")
	}
}

// What the dispatcher may do on the bus, and the nearest things it may not,
// against the rendered grants. The refusals are the point: it reads
// submissions only through its durables, never a task's subjects; it writes
// only PROFILE_LEASES among the buckets; it asks the verifier only as itself.
func TestTheRenderedDispatcherReachesItsContractAndNothingNear(t *testing.T) {
	h := startHarness(t, renderedDispatcherMap(t), map[string]Attested{
		tokenDispatcher: {ServiceAccount: renderedDispatcherSA},
	})
	nc, violations := h.connectAs(t, "dispatcher", tokenDispatcher)
	pod := "tse-read-7f9c"
	checkPublish(t, nc, violations, map[string]bool{
		// Allowed: the contract.
		lib.DispatchSubject("tse", "read", pod):                                                                     false,
		lib.ProfileLeaseSubject(lib.ProfileLeaseKey("tse", "read", pod, "t1")):                                      false,
		"$JS.API.CONSUMER.MSG.NEXT." + lib.ProfileSubmitStream + "." + lib.ProfileDurableName("tse", "read", "top"): false,
		"$JS.ACK." + lib.ProfileSubmitStream + "." + lib.ProfileDurableName("tse", "read", "top") + ".1.1.1.1.0":    false,
		"a2a.cap.verify.dispatcher":            false,
		lib.TaskSupervisorSubject("tse", "t1"): false, // terminal failures, on the profile's supervisor subject
		// Refused: the nearest things it must not do.
		lib.TaskEventsSubject("tse", "t1"):                true, // .events is the executor's alone
		lib.TaskInSubject("tse", "t1"):                    true,
		lib.TaskSubmitSubject("tse", "read", "top", "t1"): true,
		lib.DispatchAcceptSubject("tse", "read", pod):     true,
		"$JS.API.CONSUMER.CREATE." + lib.ProfileSubmitStream + "." + lib.ProfileDurableName("tse", "read", "top"): true,
		"$JS.API.CONSUMER.MSG.NEXT.TASKS.session-x-origin":                                                        true,
		// The same durable name on TASKS, and a durable the operator didn't
		// create for it: no grant.
		"$JS.API.CONSUMER.MSG.NEXT.TASKS." + lib.ProfileDurableName("tse", "read", "top"):                             true,
		"$JS.API.CONSUMER.MSG.NEXT." + lib.ProfileSubmitStream + "." + lib.ProfileDurableName("other", "read", "top"): true,
		"$KV.cap.root.x":         true,
		"$KV.session-state.x":    true,
		"a2a.cap.verify.gateway": true,
		"a2a.agents.tse":         true,
		// The supervisor subject of something it doesn't dispatch for: a session's.
		lib.TaskSupervisorSubject("session-abc", "t1"): true,
		// Stop records: read by subject for its own profiles, never written.
		"$JS.API.DIRECT.GET.DISPATCH_STOPS.a2a.stop.tse.t1":   false,
		"$JS.API.DIRECT.GET.DISPATCH_STOPS.a2a.stop.other.t1": true,
		"a2a.stop.tse.t1": true,
	})
	for subject, wantRefused := range map[string]bool{
		lib.DispatchAcceptSubject("tse", "read", pod): false,
		lib.DispatchLeaseSubject("tse", "read", pod):  false,
		"a2a.tasks.>":                           true,
		"a2a.submit.>":                          true,
		"a2a.stop.tse.*":                        false,
		"a2a.stop.other.*":                      true,
		lib.TaskEventsSubject("tse", "t1"):      true,
		lib.DispatchSubject("tse", "read", pod): true,
		"_INBOX.gateway.>":                      true,
	} {
		if got := subscribeRefused(t, nc, violations, subject); got != wantRefused {
			t.Errorf("subscribe %s refused=%v, want %v", subject, got, wantRefused)
		}
	}
}

// With a dispatcher, the rendered operator makes the profiles' durables on
// PROFILE_SUBMITS: a consumer whose filter is a submission subject, and
// nothing on TASKS. A create carries its filter in the subject, which is
// what holds it there.
func TestTheRenderedOperatorMakesOnlySubmitDurables(t *testing.T) {
	h := startHarness(t, renderedDispatcherMap(t), map[string]Attested{
		tokenRenderedOperator: {ServiceAccount: renderedOperatorSA},
	})
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	seed, _ := connectStatic(t, h, "seed", renderedSeedPassword)
	seedJS, err := jetstream.New(seed)
	if err != nil {
		t.Fatal(err)
	}
	for _, cfg := range []jetstream.StreamConfig{
		{Name: lib.TasksStream, Subjects: []string{"a2a.tasks.>"}},
		{Name: lib.ProfileSubmitStream, Subjects: []string{lib.SubmitSubjectPrefix + ">"}},
	} {
		if _, err := seedJS.CreateStream(ctx, cfg); err != nil {
			t.Fatalf("creating %s as seed: %v", cfg.Name, err)
		}
	}
	op, _ := h.connectAs(t, "operator", tokenRenderedOperator)
	js, err := jetstream.New(op)
	if err != nil {
		t.Fatal(err)
	}
	durable := lib.ProfileDurableName("tse", "read", "top")
	if _, err := js.CreateOrUpdateConsumer(ctx, lib.ProfileSubmitStream, jetstream.ConsumerConfig{
		Durable: durable, FilterSubject: lib.ProfileDurableFilter("tse", "read", "top"), AckPolicy: jetstream.AckExplicitPolicy,
	}); err != nil {
		t.Fatalf("the operator could not make a submit durable: %v", err)
	}
	if err := js.DeleteConsumer(ctx, lib.ProfileSubmitStream, durable); err != nil {
		t.Fatalf("the operator could not delete it: %v", err)
	}
	for name, try := range map[string]func(context.Context) error{
		"a durable on TASKS": func(ctx context.Context) error {
			_, err := js.CreateOrUpdateConsumer(ctx, lib.TasksStream, jetstream.ConsumerConfig{
				Durable: durable, FilterSubject: lib.TaskInSubject("tse", "*"), AckPolicy: jetstream.AckExplicitPolicy,
			})
			return err
		},
		"a PROFILE_SUBMITS durable over every submission": func(ctx context.Context) error {
			_, err := js.CreateOrUpdateConsumer(ctx, lib.ProfileSubmitStream, jetstream.ConsumerConfig{
				Durable: durable, FilterSubject: lib.SubmitSubjectPrefix + ">", AckPolicy: jetstream.AckExplicitPolicy,
			})
			return err
		},
	} {
		short, cancelShort := context.WithTimeout(ctx, 3*time.Second)
		if err := try(short); err == nil {
			t.Errorf("the operator made %s", name)
		}
		cancelShort()
	}
}

// The console and console-mcp principals read a profile submission by subject,
// and can make no consumer on PROFILE_SUBMITS: they show a profile task's
// prompt, and hold nothing that could pull a profile's queue.
func TestTheRenderedConsoleReadsSubmissionsOnly(t *testing.T) {
	h := startHarness(t, renderedDispatcherMap(t), nil)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	seed, _ := connectStatic(t, h, "seed", renderedSeedPassword)
	seedJS, err := jetstream.New(seed)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := seedJS.CreateStream(ctx, jetstream.StreamConfig{
		Name: lib.ProfileSubmitStream, Subjects: []string{lib.SubmitSubjectPrefix + ">"}, AllowDirect: true,
	}); err != nil {
		t.Fatalf("creating %s as seed: %v", lib.ProfileSubmitStream, err)
	}
	// Nothing rendered here publishes a submission yet (the gateway's grant
	// comes with its own change), so the read is of an empty stream: the
	// server answering "no message" is the grant, where a refused request
	// would get no answer at all.
	subject := lib.TaskSubmitSubject("tse", "read", "top", "t1")
	for _, tc := range []struct {
		user string
		pass string
	}{
		{"console", renderedConsolePassword},
		{"console-mcp", "pw-console-mcp"},
	} {
		t.Run(tc.user, func(t *testing.T) {
			nc, violations := connectStatic(t, h, tc.user, tc.pass)
			msg, err := nc.RequestWithContext(ctx, "$JS.API.DIRECT.GET."+lib.ProfileSubmitStream+"."+subject, nil)
			if err != nil || msg.Header.Get("Status") != "404" {
				t.Fatalf("%s direct get of %s = %v, %v; want the server's 404 for a stream with no such message", tc.user, subject, msg, err)
			}
			checkPublish(t, nc, violations, map[string]bool{
				"$JS.API.CONSUMER.CREATE." + lib.ProfileSubmitStream + ".x." + subject:                                      true,
				"$JS.API.CONSUMER.MSG.NEXT." + lib.ProfileSubmitStream + "." + lib.ProfileDurableName("tse", "read", "top"): true,
				"$JS.API.CONSUMER.INFO." + lib.ProfileSubmitStream + "." + lib.ProfileDurableName("tse", "read", "top"):     true,
				"$JS.API.STREAM.INFO." + lib.ProfileSubmitStream:                                                            true,
				"$JS.ACK." + lib.ProfileSubmitStream + ".>":                                                                 true,
				subject: true,
			})
			// A console credential reads neither the dispatcher's hand-off,
			// whose confirms carry a task's credentials, nor the stop records.
			for _, s := range []string{lib.DispatchSubject("tse", "read", "pod-1"), "a2a.dispatch.>", "a2a.stop.tse.t1", "a2a.stop.>"} {
				if !subscribeRefused(t, nc, violations, s) {
					t.Errorf("%s may subscribe to %s", tc.user, s)
				}
			}
		})
	}
}

// The dispatcher lists a (profile, tier)'s leases after a restart, through
// the ordered consumer KV key listing runs, flow control and all.
func TestTheRenderedDispatcherListsItsLeases(t *testing.T) {
	h := startHarness(t, renderedDispatcherMap(t), map[string]Attested{
		tokenDispatcher: {ServiceAccount: renderedDispatcherSA},
	})
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	seed, _ := connectStatic(t, h, "seed", renderedSeedPassword)
	seedJS, err := jetstream.New(seed)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := seedJS.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: lib.ProfileLeaseBucket, History: 1}); err != nil {
		t.Fatalf("creating PROFILE_LEASES as seed: %v", err)
	}
	nc, _ := h.connectAs(t, "dispatcher", tokenDispatcher)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	kv, err := js.KeyValue(ctx, lib.ProfileLeaseBucket)
	if err != nil {
		t.Fatalf("the dispatcher can't open its bucket: %v", err)
	}
	// 600 leases, more than one delivery batch. The listing is a
	// headers-only ordered consumer, and at this size the server didn't ask
	// for flow control, so this doesn't prove the $JS.FC grant (removing it
	// still passes); the grant is held for the much larger listings flow
	// control does reach, as the gateway's and the bridge's are.
	big := []byte(`{}`)
	for i := range 600 {
		key := lib.ProfileLeaseKey("tse", "read", "tse-read-7f9c", "t"+strconv.Itoa(i))
		if _, err := kv.Put(ctx, key, big); err != nil {
			t.Fatalf("put %s: %v", key, err)
		}
	}
	lister, err := kv.ListKeysFiltered(ctx, "tse.read.>")
	if err != nil {
		t.Fatalf("the dispatcher can't list its leases: %v", err)
	}
	n := 0
	for range lister.Keys() {
		n++
	}
	if n != 600 {
		t.Errorf("listed %d leases, want 600", n)
	}
	if err := kv.Delete(ctx, lib.ProfileLeaseKey("tse", "read", "tse-read-7f9c", "t0")); err != nil {
		t.Errorf("the dispatcher can't delete a lease: %v", err)
	}
}
