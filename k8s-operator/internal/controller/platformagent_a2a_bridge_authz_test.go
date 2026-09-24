/*
Copyright 2025.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// a2aServerLog is a nats-server Logger that keeps every line, so a refusal can
// be asserted from the server's side. A NATS permissions violation reaches the
// client only as a missing reply, and a missing reply on its own proves
// nothing: a slow server produces the same timeout.
type a2aServerLog struct {
	mu    sync.Mutex
	lines []string
}

func (l *a2aServerLog) record(format string, v ...any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.lines = append(l.lines, fmt.Sprintf(format, v...))
}

func (l *a2aServerLog) Noticef(format string, v ...any) { l.record(format, v...) }
func (l *a2aServerLog) Warnf(format string, v ...any)   { l.record(format, v...) }
func (l *a2aServerLog) Fatalf(format string, v ...any)  { l.record(format, v...) }
func (l *a2aServerLog) Errorf(format string, v ...any)  { l.record(format, v...) }
func (l *a2aServerLog) Debugf(format string, v ...any)  { l.record(format, v...) }
func (l *a2aServerLog) Tracef(format string, v ...any)  { l.record(format, v...) }

// publishViolations returns the server's Publish Violation lines for user. The
// line names the user in the client prefix (`user:bridge` on 2.11+, `User
// "bridge"` on 2.10), so both spellings are accepted.
func (l *a2aServerLog) publishViolations(user string) []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	var out []string
	for _, line := range l.lines {
		if !strings.Contains(line, "Publish Violation") {
			continue
		}
		if strings.Contains(line, "user:"+user) || strings.Contains(line, fmt.Sprintf("User %q", user)) {
			out = append(out, line)
		}
	}
	return out
}

// refused reports whether the server logged a violation of kind ("Publish" or
// "Subscription") for user on exactly subject. It waits briefly: the line is
// written on the server's read loop, and the caller has only seen its own
// request time out or its subscription silently receive nothing.
func (l *a2aServerLog) refused(kind, user, subject string) bool {
	kindNeedle := kind + " Violation"
	subjectNeedle := fmt.Sprintf("Subject %q", subject)
	deadline := time.Now().Add(2 * time.Second)
	for {
		l.mu.Lock()
		for _, line := range l.lines {
			if strings.Contains(line, kindNeedle) && strings.Contains(line, subjectNeedle) &&
				(strings.Contains(line, "user:"+user) || strings.Contains(line, fmt.Sprintf("User %q", user))) {
				l.mu.Unlock()
				return true
			}
		}
		l.mu.Unlock()
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// refusedPublish reports whether the server logged a publish violation for
// user on exactly subject.
func (l *a2aServerLog) refusedPublish(user, subject string) bool {
	return l.refused("Publish", user, subject)
}

// refusedSubscribe is refusedPublish for the subscribe side: it reports
// whether the server logged a subscription violation for user on exactly
// subject.
func (l *a2aServerLog) refusedSubscribe(user, subject string) bool {
	return l.refused("Subscription", user, subject)
}

// a2aStartRenderedServer runs an embedded nats-server on conf, the nats.conf
// the operator renders, changing only what a test process cannot honour: the
// listen ports (random), the websocket listener (off) and the store directory
// (a temp dir). Accounts, users and permissions are the render's own.
func a2aStartRenderedServer(t *testing.T, conf string) (*natsserver.Server, *a2aServerLog) {
	t.Helper()
	s, log := a2aStartRenderedServerAt(t, conf, -1, filepath.Join(t.TempDir(), "store"))
	t.Cleanup(func() {
		s.Shutdown()
		s.WaitForShutdown()
	})
	return s, log
}

// a2aStartRenderedServerAt is the same on a fixed port and store directory,
// so a second start is the same bus coming back with the same streams. The
// caller owns shutdown.
func a2aStartRenderedServerAt(t *testing.T, conf string, port int, store string) (*natsserver.Server, *a2aServerLog) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "nats.conf")
	if err := os.WriteFile(path, []byte(conf), 0o600); err != nil {
		t.Fatal(err)
	}
	opts, err := natsserver.ProcessConfigFile(path)
	if err != nil {
		t.Fatalf("the rendered nats.conf does not parse: %v", err)
	}
	opts.Port = port
	opts.HTTPPort = 0
	opts.Websocket.Port = 0
	opts.StoreDir = store
	opts.NoSigs = true
	s, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("nats-server refused the rendered config: %v", err)
	}
	log := &a2aServerLog{}
	s.SetLoggerV2(log, false, false, false)
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("embedded nats-server did not come up")
	}
	// The evidence names the server it came from: the module pin here and
	// the image tag the operator deploys are independent, and the tag floats.
	t.Logf("embedded nats-server %s (the operator deploys %s)", natsserver.VERSION, defaultA2ANATSImage)
	return s, log
}

// a2aConnectAs dials the embedded server as one of the rendered users, with
// the inbox prefix that user's subscribe grant requires. These are the two
// options every bus-side binary sets (lib.WithUserPassword); a client
// without the prefix authenticates, publishes, and then times out on its first
// reply.
func a2aConnectAs(t *testing.T, url, user, password string) (*nats.Conn, jetstream.JetStream) {
	t.Helper()
	nc, err := nats.Connect(url,
		nats.UserInfo(user, password),
		nats.CustomInboxPrefix("_INBOX."+user),
		nats.Name(user),
		// The default handler prints to stderr; the server log is the
		// evidence this test reads, so the client side stays quiet.
		nats.ErrorHandler(func(*nats.Conn, *nats.Subscription, error) {}),
	)
	if err != nil {
		t.Fatalf("connect as %s: %v", user, err)
	}
	t.Cleanup(nc.Close)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	return nc, js
}

// a2aSeedStreamConfigs is what the provision Job creates, with the flags the
// script passes to natscli translated to StreamConfig: four message streams.
// AllowDirect is set because the script says --allow-direct on every `stream
// add` (and a KV bucket always has it); it decides which API subject a
// last-message read uses, and the bridge's grant is written for the direct
// route.
//
// The translation is asserted rather than trusted —
// TestTheSeedFixtureCarriesTheLimitsTheScriptRenders reads the flags back off
// the script — because the fixture is the substrate every authz test in this
// package runs on, and one that has drifted from the render tests the wrong
// deployment without saying so.
func a2aSeedStreamConfigs() []jetstream.StreamConfig {
	return []jetstream.StreamConfig{
		{Name: "TASKS", Subjects: []string{"a2a.tasks.>"},
			Storage: jetstream.FileStorage, Retention: jetstream.LimitsPolicy, Discard: jetstream.DiscardOld,
			MaxAge: 72 * time.Hour, MaxBytes: 21474836480, Replicas: 1,
			MaxConsumers:      a2aTasksMaxConsumersFloor,
			MaxMsgsPerSubject: a2aTasksMaxMsgsPerSubject, AllowDirect: true},
		{Name: "DIRECTORY", Subjects: []string{"a2a.agents.>"},
			Storage: jetstream.FileStorage, Retention: jetstream.LimitsPolicy, Discard: jetstream.DiscardOld,
			MaxMsgsPerSubject: 1, MaxBytes: 1073741824, Replicas: 1, MaxConsumers: 64, AllowDirect: true},
		{Name: "TOPICS-STATE", Subjects: []string{"a2a.topics.agent.platform.upgrade-readiness", "a2a.topics.shared.blueprint", "a2a.topics.shared.probe"},
			Storage: jetstream.FileStorage, Retention: jetstream.LimitsPolicy, Discard: jetstream.DiscardOld,
			MaxMsgsPerSubject: 8, MaxBytes: 1073741824, Replicas: 1, MaxConsumers: 64, AllowDirect: true},
		{Name: "TOPICS-JOURNAL", Subjects: []string{"a2a.topics.shared.annotations"},
			Storage: jetstream.FileStorage, Retention: jetstream.LimitsPolicy, Discard: jetstream.DiscardOld,
			MaxAge: 720 * time.Hour, MaxBytes: 5368709120, Replicas: 1, MaxConsumers: 64, AllowDirect: true},
	}
}

// a2aProvisionLikeTheScript seeds a test bus with what the provision Job
// creates: the streams above and three KV buckets.
func a2aProvisionLikeTheScript(t *testing.T, url, seedPassword string) {
	t.Helper()
	_, js := a2aConnectAs(t, url, "seed", seedPassword)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for _, cfg := range a2aSeedStreamConfigs() {
		if _, err := js.CreateStream(ctx, cfg); err != nil {
			t.Fatalf("provision %s as seed: %v", cfg.Name, err)
		}
	}
	for _, bucket := range []string{"runtime-state", "session-state", "cap"} {
		_, err := js.CreateKeyValue(ctx, jetstream.KeyValueConfig{
			Bucket: bucket, History: 1, Replicas: 1, Storage: jetstream.FileStorage, MaxBytes: 268435456,
		})
		if err != nil {
			t.Fatalf("provision bucket %s as seed: %v", bucket, err)
		}
	}
}

// TestBridgeJetStreamGrantOnARealServer is the refusal proof for the bridge's
// JetStream API grant, measured the way gke-labs/kube-agents#1316 measured the
// hole: the config the operator renders, run by an embedded nats-server, the
// streams provisioned as the provision Job provisions them, and a client
// connected as bridge.
//
// Two tables. The first is every JetStream operation the bus-side binaries
// perform -- the bridge's durable consumer on TASKS with a pull and an ack, a
// task-event publish and the sweep's compare-and-swap publish, the replay's
// horizon read and ordered consumer, and the in-flight registry's put / keys /
// delete on runtime-state -- each of which must succeed, with zero publish
// violations logged for bridge across the lot. A
// grant missing from a2aBridgeJetStreamGrants shows up here as the exact
// subject the server refused, which is #1306's STREAM.NAMES lesson applied
// before the deploy instead of after it.
//
// The second is the destructive and out-of-scope surface: every verb on
// DIRECTORY, PURGE / UPDATE / DELETE / MSG.DELETE / RESTORE / SNAPSHOT on
// TASKS, CONSUMER.DELETE and CONSUMER.INFO on the gateway's relay durable,
// KV_session-state, enumeration, account INFO and STREAM.CREATE. Each must be
// refused, and "refused" is read from the server's log -- a Publish Violation
// naming bridge and the exact subject -- not from the client's timeout. The
// streams are then re-read as seed to show nothing underneath changed.
//
// Two subtests then measure what the refusal table structurally cannot: the
// routes that run over subjects the grant PERMITS, and so leave no violation
// to read. The deliver-subject one is the consumer-create residue #1316 asked
// about; the create-as-update one is what CONSUMER.CREATE reaches on a shared
// stream, because the verb is create-or-update by name and the server has no
// ownership concept for a consumer name.
//
// A final subtest puts the wildcard back into the render and shows the same
// PURGE and DELETE succeeding and DIRECTORY gone: the control that says the
// refusals above are authorization rather than a broken API, and the issue
// reproduced on this server.
func TestBridgeJetStreamGrantOnARealServer(t *testing.T) {
	creds := a2aFullCreds("a", "1")
	conf := string(buildA2ANATSConfigSecret(a2aTestAgent(), creds, a2aTestCalloutKeys(t)).Data["nats.conf"])
	bridgePW := string(creds.Data["bridge-password"])
	seedPW := string(creds.Data["seed-password"])
	gatewayPW := string(creds.Data["gateway-password"])
	webPW := string(creds.Data["web-password"])

	s, log := a2aStartRenderedServer(t, conf)
	a2aProvisionLikeTheScript(t, s.ClientURL(), seedPW)
	_, gw := a2aConnectAs(t, s.ClientURL(), "gateway", gatewayPW)
	// The consumer-info oracle. It was `gateway` while that user held
	// $JS.API.>; the narrowing #1666 asked for scoped it and took
	// CONSUMER.INFO with it, so reading
	// a consumer back moved to `web`, which holds CONSUMER.INFO.TASKS.* by
	// enumeration and is now the only principal that can answer. `gateway`
	// stays for what it still holds and what this test needs it for: owning
	// the relay durable, publishing a submission, and the subscribe grants
	// the deliver-subject cases turn on.
	_, webJS := a2aConnectAs(t, s.ClientURL(), "web", webPW)
	bridge, js := a2aConnectAs(t, s.ClientURL(), "bridge", bridgePW)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	const (
		inSubject     = "a2a.tasks.platform.t1.in"
		eventsSubject = "a2a.tasks.platform.t1.events"
	)
	bridgeDurable := jetstream.ConsumerConfig{
		Durable: "bridge-platform", FilterSubject: "a2a.tasks.platform.*.in", AckPolicy: jetstream.AckExplicitPolicy,
	}

	// The gateway's own durable, so the CONSUMER.DELETE refusal below is
	// measured against a real consumer another principal owns; and a
	// submission for the bridge to consume, from the principal that may
	// publish one.
	if _, err := gw.CreateOrUpdateConsumer(ctx, "TASKS", jetstream.ConsumerConfig{
		Durable: "gateway-relay", FilterSubject: "a2a.tasks.*.*.events", AckPolicy: jetstream.AckExplicitPolicy,
	}); err != nil {
		t.Fatalf("gateway relay durable: %v", err)
	}
	if _, err := gw.Publish(ctx, inSubject, []byte(`{"kind":"message"}`)); err != nil {
		t.Fatalf("gateway submission: %v", err)
	}

	// Table 1: the bridge's own operations, in the order the bridge performs
	// them. Fatal on the first refusal, because everything after it depends on
	// the object it did not get.
	allowed := func(op string, err error) {
		t.Helper()
		if err != nil {
			t.Fatalf("%-64s REFUSED: %v\nserver log for bridge: %q", op, err, log.publishViolations("bridge"))
		}
		t.Logf("%-64s allowed", op)
	}

	stream, err := js.Stream(ctx, "TASKS")
	allowed("STREAM.INFO TASKS (js.Stream)", err)

	cons, err := js.CreateOrUpdateConsumer(ctx, "TASKS", bridgeDurable)
	allowed("CONSUMER.CREATE TASKS durable bridge-platform (SubscribeDurable)", err)
	_, err = js.CreateOrUpdateConsumer(ctx, "TASKS", bridgeDurable)
	allowed("CONSUMER.CREATE TASKS same durable again (the rebuild path)", err)

	batch, err := cons.Fetch(1, jetstream.FetchMaxWait(5*time.Second))
	allowed("CONSUMER.MSG.NEXT TASKS (Fetch)", err)
	var delivered jetstream.Msg
	for m := range batch.Messages() {
		delivered = m
	}
	if delivered == nil {
		t.Fatalf("the pull delivered nothing: %v", batch.Error())
	}
	allowed("$JS.ACK TASKS (msg.Ack)", delivered.Ack())
	// The ack landed, read from web's side because bridge holds no
	// CONSUMER.INFO (and, since #1666, neither does gateway).
	relayView, err := webJS.Consumer(ctx, "TASKS", "bridge-platform")
	if err != nil {
		t.Fatal(err)
	}
	if info, err := relayView.Info(ctx); err != nil || info.AckFloor.Consumer != 1 {
		t.Fatalf("ack floor after the bridge's ack: %+v (%v)", info, err)
	}

	_, err = js.Publish(ctx, eventsSubject, []byte(`{"kind":"status-update"}`), jetstream.WithMsgID("e1"))
	allowed("publish a2a.tasks.platform.t1.events (JetStream, dedup id)", err)
	last, err := stream.GetLastMsgForSubject(ctx, eventsSubject)
	allowed("DIRECT.GET TASKS last-for-subject (replay horizon)", err)
	_, err = js.Publish(ctx, eventsSubject, []byte(`{"kind":"status-update","final":true}`),
		jetstream.WithMsgID("e2"), jetstream.WithExpectLastSequencePerSubject(last.Sequence))
	allowed("publish events with expected last sequence (sweep CAS)", err)

	// The wildcard form of the same route: how a caller that does not know the
	// task id finds the newest submission for an addressee. Its own row because
	// the permission check sees the filter subject's `*` as an ordinary token
	// under the grant's trailing `>` -- were the grant ever narrowed to a
	// concrete-subject form, the DIRECT.GET row above would still pass.
	wild, err := stream.GetLastMsgForSubject(ctx, "a2a.tasks.platform.*.in")
	allowed("DIRECT.GET TASKS last-for-subject, wildcard (newest task for an addressee)", err)
	if wild == nil || wild.Subject != inSubject {
		t.Fatalf("wildcard last-for-subject returned %+v, want the submission on %s", wild, inSubject)
	}

	oc, err := js.OrderedConsumer(ctx, "TASKS", jetstream.OrderedConsumerConfig{
		FilterSubjects: []string{eventsSubject}, DeliverPolicy: jetstream.DeliverAllPolicy,
	})
	allowed("CONSUMER.CREATE TASKS ordered (TasksGet replay)", err)
	it, err := oc.Messages()
	allowed("ordered consumer Messages()", err)
	for i := 1; i <= 2; i++ {
		_, err := it.Next()
		allowed(fmt.Sprintf("CONSUMER.MSG.NEXT TASKS ordered, replay message %d", i), err)
	}
	it.Stop()

	kv, err := js.KeyValue(ctx, "runtime-state")
	allowed("STREAM.INFO KV_runtime-state (js.KeyValue)", err)
	_, err = kv.Put(ctx, "bridge.platform.t1", []byte("platform-bridge"))
	allowed("$KV.runtime-state put (markInFlight)", err)
	keys, err := kv.Keys(ctx)
	allowed("CONSUMER.CREATE + CONSUMER.DELETE KV_runtime-state (kv.Keys, the sweep)", err)
	if !reflect.DeepEqual(keys, []string{"bridge.platform.t1"}) {
		t.Fatalf("sweep keys = %q", keys)
	}
	allowed("$KV.runtime-state delete (clearInFlight)", kv.Delete(ctx, "bridge.platform.t1"))

	if v := log.publishViolations("bridge"); len(v) != 0 {
		t.Errorf("the bridge's own operations tripped %d publish violations; each is a grant a2aBridgeJetStreamGrants is missing:\n%s",
			len(v), strings.Join(v, "\n"))
	}

	// Table 2: what the wildcard allowed and the grant refuses. The bodies are
	// what a real caller would send; the server never reads them, because the
	// permission check runs before the request is parsed.
	type call struct{ subject, body string }
	refused := []call{
		{"$JS.API.STREAM.INFO.DIRECTORY", ""},
		{"$JS.API.STREAM.PURGE.DIRECTORY", ""},
		{"$JS.API.STREAM.UPDATE.DIRECTORY", `{"name":"DIRECTORY","subjects":["a2a.agents.>","a2a.tasks.>"]}`},
		{"$JS.API.STREAM.DELETE.DIRECTORY", ""},
		{"$JS.API.STREAM.MSG.DELETE.DIRECTORY", `{"seq":1}`},
		{"$JS.API.CONSUMER.CREATE.DIRECTORY.peek", `{"stream_name":"DIRECTORY","config":{"name":"peek","deliver_subject":"_INBOX.bridge.peek"}}`},
		{"$JS.API.DIRECT.GET.DIRECTORY.a2a.agents.platform", ""},
		{"$JS.API.STREAM.PURGE.TASKS", ""},
		{"$JS.API.STREAM.UPDATE.TASKS", `{"name":"TASKS","subjects":["a2a.tasks.>","a2a.agents.>"]}`},
		{"$JS.API.STREAM.DELETE.TASKS", ""},
		{"$JS.API.STREAM.MSG.DELETE.TASKS", `{"seq":1}`},
		{"$JS.API.STREAM.RESTORE.TASKS", ""},
		{"$JS.API.STREAM.SNAPSHOT.TASKS", `{"deliver_subject":"_INBOX.bridge.snap"}`},
		{"$JS.API.CONSUMER.DELETE.TASKS.gateway-relay", ""},
		{"$JS.API.CONSUMER.INFO.TASKS.gateway-relay", ""},
		// The write narrowing, from the side that matters. Reading TASKS is not
		// scoped to an addressee (the allowed table's wildcard DIRECT.GET row),
		// so a bridge whose BRIDGE_PROFILE was overridden does get another
		// addressee's submission delivered. This is the row that stops it there:
		// accept publishes `submitted` on the addressee's events subject before
		// it queues anything for a worker, so the refusal lands before the
		// subprocess is spawned rather than after the task has run.
		{"a2a.tasks.chat.t1.events", `{"kind":"status-update"}`},
		{"$JS.API.DIRECT.GET.KV_runtime-state.$KV.runtime-state.bridge.platform.t1", ""},
		{"$JS.API.STREAM.INFO.KV_session-state", ""},
		{"$JS.API.CONSUMER.CREATE.KV_session-state.peek", `{"stream_name":"KV_session-state","config":{"name":"peek","deliver_subject":"_INBOX.bridge.peek"}}`},
		{"$JS.API.DIRECT.GET.KV_session-state.$KV.session-state.k", ""},
		{"$JS.API.STREAM.INFO.KV_cap", ""},
		// The blackboard, which `worker` held and the bridge does not. These four
		// are the A5 split measured from the bridge's side: one credential used to
		// carry both the CLI's topic reads and the bridge's task execution, and the
		// CLI is its own principal now. A regression that merged the two grant
		// lists back together shows up here first — the allowed table above would
		// still pass, because nothing it does was taken away.
		{"$JS.API.STREAM.INFO.TOPICS-STATE", ""},
		{"$JS.API.DIRECT.GET.TOPICS-STATE.a2a.topics.shared.blueprint", ""},
		{"$JS.API.STREAM.INFO.TOPICS-JOURNAL", ""},
		{"$JS.API.DIRECT.GET.TOPICS-JOURNAL.a2a.topics.shared.annotations", ""},
		// And the core-NATS half of the same removal. The JetStream rows above
		// only take away the reads; without these two the bridge could still
		// WRITE the blackboard, which is the half an agent would notice.
		{"a2a.topics.shared.blueprint", `{"kind":"topic-entry"}`},
		{"a2a.topics.agent.platform.upgrade-readiness", `{"kind":"topic-entry"}`},
		{"$JS.API.INFO", ""},
		{"$JS.API.STREAM.NAMES", ""},
		{"$JS.API.STREAM.LIST", ""},
		{"$JS.API.CONSUMER.NAMES.TASKS", ""},
		{"$JS.API.STREAM.CREATE.EVIL", `{"name":"EVIL","subjects":["evil.>"]}`},
	}
	for _, c := range refused {
		// A short client wait is safe: "refused" is decided by the server's
		// log below, and a reply that arrives late fails the run as ALLOWED
		// only if it arrives at all.
		msg, err := bridge.Request(c.subject, []byte(c.body), 250*time.Millisecond)
		if err == nil {
			t.Errorf("%-64s ALLOWED: %s", c.subject, msg.Data)
			continue
		}
		if !log.refusedPublish("bridge", c.subject) {
			t.Errorf("%-64s no reply (%v), but the server logged no publish violation for it", c.subject, err)
			continue
		}
		t.Logf("%-64s refused (server: Publish Violation)", c.subject)
	}

	// Nothing underneath changed: DIRECTORY is as provisioned, TASKS still
	// holds the submission and both events, the relay durable is still there.
	_, seedJS := a2aConnectAs(t, s.ClientURL(), "seed", seedPW)
	dir, err := seedJS.Stream(ctx, "DIRECTORY")
	if err != nil {
		t.Fatalf("DIRECTORY after the refused calls: %v", err)
	}
	if got := dir.CachedInfo(); !reflect.DeepEqual(got.Config.Subjects, []string{"a2a.agents.>"}) || got.State.Msgs != 0 {
		t.Errorf("DIRECTORY changed under the bridge: subjects %q, %d msgs", got.Config.Subjects, got.State.Msgs)
	}
	tasks, err := seedJS.Stream(ctx, "TASKS")
	if err != nil {
		t.Fatal(err)
	}
	if got := tasks.CachedInfo().State.Msgs; got != 3 {
		t.Errorf("TASKS holds %d messages after the refused calls, want 3", got)
	}
	if _, err := webJS.Consumer(ctx, "TASKS", "gateway-relay"); err != nil {
		t.Errorf("the gateway's relay durable after the bridge's refused DELETE: %v", err)
	}

	// The consumer-create case #1316 asked about. CONSUMER.CREATE is scoped
	// to TASKS, but a push consumer's deliver_subject is a body field no
	// subject grant can see: the server delivers TASKS messages onto it, and
	// a stream whose subjects cover it stores them under their ORIGINAL
	// subjects. Delivery starts only once a subscription exists whose subject
	// is EXACTLY the deliver subject, which splits the streams in two, so
	// four measurements -- the fourth is what makes "exactly" a measurement
	// rather than a generalisation from three literal cases:
	//
	//  1. A wildcard-subject stream with no subscriber: the consumer is
	//     created, delivers nothing, and DIRECTORY stays empty. Asserted.
	//  2. A literal-subject stream: TOPICS-STATE's own ingest on the
	//     writerless probe topic is the interest, and the write lands with no
	//     subscription from anyone. This route predates the change, survives
	//     it, and is recorded in a2aBridgeJetStreamGrants; what is asserted
	//     is the property that does hold -- the stored messages keep their
	//     a2a.tasks.* subjects, so a topic read by subject never sees them.
	//  3. A wildcard-subject stream with another principal holding a WILDCARD
	//     subscription that covers the deliver subject -- `a2a.agents.>`,
	//     which is the gateway's whole subscribe grant. Asserted to deliver
	//     nothing, which is what bounds the residue below.
	//  4. A wildcard-subject stream with another principal's subscription on
	//     the deliver subject itself: a card subject under the gateway's
	//     `a2a.agents.>` grant, which nothing in the tree opens today.
	//     Measured and logged.
	t.Run("a push consumer's deliver subject writes only where a subscriber holds it exactly", func(t *testing.T) {
		create := func(name, deliver string) {
			t.Helper()
			body := fmt.Sprintf(`{"stream_name":"TASKS","config":{"name":%q,"deliver_subject":%q,"deliver_policy":"all","ack_policy":"none"}}`, name, deliver)
			if _, err := bridge.Request("$JS.API.CONSUMER.CREATE.TASKS."+name, []byte(body), 2*time.Second); err != nil {
				t.Fatalf("consumer create %s: %v", name, err)
			}
		}
		streamInfo := func(name string, opts ...jetstream.StreamInfoOpt) *jetstream.StreamInfo {
			t.Helper()
			st, err := seedJS.Stream(ctx, name)
			if err != nil {
				t.Fatal(err)
			}
			info, err := st.Info(ctx, opts...)
			if err != nil {
				t.Fatal(err)
			}
			return info
		}

		// 1. DIRECTORY (a2a.agents.>), nobody subscribed to the card subject.
		create("divert", "a2a.agents.platform")
		time.Sleep(time.Second)
		if n := streamInfo("DIRECTORY").State.Msgs; n != 0 {
			t.Errorf("DIRECTORY holds %d messages with no literal subscriber on the deliver subject", n)
		}
		t.Log("wildcard-subject stream, no literal subscriber: consumer created, DIRECTORY msgs=0")

		// 2. TOPICS-STATE, whose probe subject is literal and writerless.
		before := streamInfo("TOPICS-STATE").State.Msgs
		create("probewrite", "a2a.topics.shared.probe")
		time.Sleep(time.Second)
		info := streamInfo("TOPICS-STATE", jetstream.WithSubjectFilter("a2a.tasks.>"))
		var underTaskSubjects uint64
		for _, n := range info.State.Subjects {
			underTaskSubjects += n
		}
		t.Logf("residue: literal-subject stream, no subscriber anywhere: TOPICS-STATE grew %d -> %d, %d of them under a2a.tasks.* subjects",
			before, info.State.Msgs, underTaskSubjects)
		if info.State.Msgs-before != underTaskSubjects {
			t.Errorf("TOPICS-STATE gained %d messages but only %d sit under a2a.tasks.* subjects; the deliver-subject route rewrote a subject, which would be forgery",
				info.State.Msgs-before, underTaskSubjects)
		}
		// Read the probe subject back through STREAM.INFO's per-subject
		// counts rather than a stored-message get, because after the
		// gateway's narrowing no principal this test can dial holds a
		// content read on TOPICS-STATE. It used to be the gateway, on the
		// strength of that user's $JS.API.>; the narrowing #1666 asked for
		// scoped that grant to TASKS and the session registry. A5 then split
		// `worker` in two and gave the topic streams to `agent`, which is
		// callout-authenticated and so has no password this embedded server
		// accepts. Seed is the reader of every other measurement here and
		// holds STREAM.INFO on the streams it provisions; asking it for a
		// stored message would not fail, it would hang, because #1306 scoped
		// its grant and a refused request is not an error nats.go reports.
		//
		// The property is the same one: if the diverted delivery had been
		// rewritten onto the probe topic's subject, that subject would carry
		// it, and a subject-filtered STREAM.INFO reports exactly that count.
		probe := streamInfo("TOPICS-STATE", jetstream.WithSubjectFilter("a2a.topics.shared.probe"))
		if n := probe.State.Subjects["a2a.topics.shared.probe"]; n != 0 {
			t.Errorf("a read of the probe topic by subject sees %d messages after the diverted consumer", n)
		}

		// 3. DIRECTORY with a principal holding a WILDCARD subscription that
		// covers the deliver subject: `a2a.agents.>`, which is exactly the
		// gateway's subscribe grant (web's `a2a.>` covers it too), and the
		// subscription a directory watcher opens. Ordinary NATS interest
		// matching resolves wildcards, so nothing about case 4 below answers
		// for this one and it decides how large the residue is: a wildcard
		// watcher is a configuration the operator supports, whereas nothing
		// in the tree opens a literal card subscription.
		//
		// It does not supply interest, and that is a property of the
		// mechanism rather than of these three streams. A push consumer's
		// deliver subject is registered through Sublist.registerNotification
		// (server/consumer.go, "If push mode, register for notifications on
		// interest"), which walks the match set and takes interest only from
		// a subscription whose subject is byte-equal to the deliver subject
		// -- `if string(sub.subject) == subject`, with the method's own
		// doc comment saying "this interest needs to be exact and ...
		// wildcards will not trigger the notifications". Identical in
		// 2.10.29 and 2.14.5. That is also why case 1 above found nothing:
		// DIRECTORY's own ingest subscription is the wildcard `a2a.agents.>`,
		// while TOPICS-STATE's in case 2 is the literal probe subject.
		//
		// The baseline is read BEFORE the subscription opens, and case 1's
		// `divert` consumer is deliberately still alive: interest appearing
		// later starts an existing push consumer too, so a baseline taken
		// after the subscribe would absorb exactly the delivery this case is
		// looking for. Measured -- with a literal subscription substituted
		// here, that ordering made the case pass while DIRECTORY filled.
		beforeWild := streamInfo("DIRECTORY").State.Msgs
		if beforeWild != 0 {
			t.Fatalf("DIRECTORY holds %d messages before the wildcard subscriber case; the baseline "+
				"has to be empty for this case to be able to see a delivery", beforeWild)
		}
		wildNC, _ := a2aConnectAs(t, s.ClientURL(), "gateway", gatewayPW)
		wildSub, err := wildNC.SubscribeSync("a2a.agents.>")
		if err != nil {
			t.Fatal(err)
		}
		_ = wildNC.Flush()
		create("divertwild", "a2a.agents.platform")
		time.Sleep(time.Second)
		afterWild := streamInfo("DIRECTORY").State.Msgs
		t.Logf("wildcard-subject stream, a principal subscribed to a2a.agents.> (not the deliver subject itself): DIRECTORY %d -> %d",
			beforeWild, afterWild)
		if afterWild != beforeWild {
			t.Errorf("a subscription on a2a.agents.> supplied interest for deliver subject "+
				"a2a.agents.platform: DIRECTORY went %d -> %d. registerNotification's exactness is what "+
				"bounds this residue to a principal subscribed to a card subject itself; if it no longer "+
				"holds, a2aBridgeJetStreamGrants's comment and docs/designs/spec-nats-deployment.md both "+
				"understate the residue", beforeWild, afterWild)
		}
		if err := wildSub.Unsubscribe(); err != nil {
			t.Fatal(err)
		}
		_ = wildNC.Flush()

		// 4. DIRECTORY with the gateway holding a literal card subscription.
		gwNC, _ := a2aConnectAs(t, s.ClientURL(), "gateway", gatewayPW)
		gwSub, err := gwNC.SubscribeSync("a2a.agents.platform")
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = gwSub.Unsubscribe() }()
		_ = gwNC.Flush()
		create("divert2", "a2a.agents.platform")
		time.Sleep(time.Second)
		info = streamInfo("DIRECTORY", jetstream.WithSubjectFilter("a2a.tasks.>"))
		underTaskSubjects = 0
		for _, n := range info.State.Subjects {
			underTaskSubjects += n
		}
		t.Logf("residue: with the gateway holding a literal card subscription, DIRECTORY msgs=%d, %d under a2a.tasks.* subjects", info.State.Msgs, underTaskSubjects)
		if info.State.Msgs != underTaskSubjects {
			t.Errorf("DIRECTORY holds %d messages but only %d sit under a2a.tasks.* subjects; a card subject was written", info.State.Msgs, underTaskSubjects)
		}
	})

	// CONSUMER.CREATE is create-OR-UPDATE by name, which the refusal table
	// above cannot see: it measures the subjects the grant withholds, and
	// this route uses a subject the grant permits. The server has no
	// ownership concept for a consumer name, so within a stream the bridge
	// may create consumers on, every consumer on that stream is the bridge's
	// to reconfigure -- the gateway's relay durable included. Two outcomes,
	// both measured here rather than argued, because withholding
	// CONSUMER.DELETE.TASKS.* is only worth what this subtest says it is
	// worth.
	//
	// This is the residue the `web` block in the render and
	// docs/designs/spec-nats-deployment.md already record for `web`; the
	// bridge holds it on TASKS for the same reason and, unlike web, has
	// another principal's durable on the stream to aim at. It is not a
	// regression -- $JS.API.> permitted all of it -- and there is no narrower
	// grant: nats.go's ordered consumers take server-generated names, so the
	// last token has to be `>`, and NATS wildcards match whole tokens, so a
	// prefix grant is not available either.
	t.Run("CONSUMER.CREATE on TASKS is create-or-update, so it reaches the gateway's durable", func(t *testing.T) {
		const relaySubject = "$JS.API.CONSUMER.CREATE.TASKS.gateway-relay"
		// The relay's config as the gateway created it. An update carries the
		// whole config, not a patch: a body naming only filter_subject is
		// refused by the SERVER (not by the grant) with "ack policy can not
		// be updated", because the zero value of ack_policy is `none`. That
		// is a JSON-assembly detail, not a boundary, so the bodies below are
		// what a real caller sends.
		relayConfig := func(extra string) []byte {
			return fmt.Appendf(nil, `{"stream_name":"TASKS","config":{"durable_name":"gateway-relay","name":"gateway-relay",`+
				`"ack_policy":"explicit","deliver_policy":"all","replay_policy":"instant",%s}}`, extra)
		}
		update := func(what string, body []byte) {
			t.Helper()
			msg, err := bridge.Request(relaySubject, body, 2*time.Second)
			if err != nil {
				t.Fatalf("%s: no reply (%v); server violations for bridge: %q", what, err, log.publishViolations("bridge"))
			}
			if log.refusedPublish("bridge", relaySubject) {
				t.Fatalf("%s: the server refused %s", what, relaySubject)
			}
			var resp struct {
				Error *struct {
					Description string `json:"description"`
				} `json:"error"`
			}
			if err := json.Unmarshal(msg.Data, &resp); err != nil {
				t.Fatalf("%s: %v", what, err)
			}
			if resp.Error != nil {
				t.Fatalf("%s: server rejected the update: %s", what, resp.Error.Description)
			}
			t.Logf("%-58s ALLOWED by the grant and applied by the server", what)
		}

		// 1. Retune another principal's durable: the gateway stops seeing
		// task events, and no permissions violation is logged anywhere,
		// because the call is inside the allow-list.
		update("retune gateway-relay's filter_subject", relayConfig(`"filter_subject":"a2a.tasks.none"`))
		relay, err := webJS.Consumer(ctx, "TASKS", "gateway-relay")
		if err != nil {
			t.Fatalf("gateway-relay after the bridge's update: %v", err)
		}
		if got := relay.CachedInfo().Config.FilterSubject; got != "a2a.tasks.none" {
			t.Errorf("gateway-relay's filter subject is %q; the bridge's create-as-update did not take. "+
				"If the server has gained an ownership check, a2aBridgeJetStreamGrants's comment can stop "+
				"recording this residue", got)
		}

		// 2. Delete it without naming CONSUMER.DELETE. inactive_threshold is
		// a config field, so the same permitted subject sets it and the
		// server reaps the durable -- the outcome withholding
		// CONSUMER.DELETE.TASKS.* is meant to prevent.
		update("set gateway-relay's inactive_threshold to 1s", relayConfig(
			`"filter_subject":"a2a.tasks.*.*.events","inactive_threshold":1000000000`))
		deadline := time.Now().Add(30 * time.Second)
		var gone bool
		for time.Now().Before(deadline) {
			if _, err := webJS.Consumer(ctx, "TASKS", "gateway-relay"); errors.Is(err, jetstream.ErrConsumerNotFound) {
				gone = true
				break
			}
			time.Sleep(200 * time.Millisecond)
		}
		if !gone {
			t.Error("gateway-relay survived a 1s inactive_threshold set through CONSUMER.CREATE; " +
				"if that is now true, withholding CONSUMER.DELETE.TASKS.* closes the route rather than raising its price")
		} else {
			t.Log("gateway-relay was reaped by the threshold the bridge set; the ack floor went with it")
		}
		// The contrast, in one assertion: the table above had this same
		// bridge refused on CONSUMER.DELETE.TASKS.gateway-relay, and the
		// durable is gone anyway.
		if !log.refusedPublish("bridge", "$JS.API.CONSUMER.DELETE.TASKS.gateway-relay") {
			t.Error("the refusal table above no longer measures CONSUMER.DELETE.TASKS.gateway-relay, " +
				"so this row has nothing to contrast the create-as-update route with")
		}
	})

	// The read axis, which the refusal table structurally cannot measure:
	// every row in it is a subject the grant WITHHOLDS, and reading another
	// addressee's task runs over subjects the grant permits. bridgeIdentity's
	// comment asserts the write narrowing and disclaims the read one; this is
	// the measurement behind both halves, and it is the row that fails if a
	// later change makes the disclaimer wrong in the safe direction too.
	t.Run("the addressee scoping is a write control and not a read control", func(t *testing.T) {
		const (
			otherIn     = "a2a.tasks.session-abc123.t9.in"
			otherEvents = "a2a.tasks.session-abc123.t9.events"
			otherBody   = `{"kind":"message","text":"another addressee's prompt"}`
		)
		if _, err := gw.Publish(ctx, otherIn, []byte(otherBody)); err != nil {
			t.Fatalf("gateway submission for the other addressee: %v", err)
		}

		// Write: refused, which is the half the split actually narrowed.
		if _, err := bridge.Request(otherEvents, []byte(`{"kind":"status-update"}`), 250*time.Millisecond); err == nil {
			t.Errorf("%-64s ALLOWED; the addressee scoping in bridgeIdentity's publish list is gone", otherEvents)
		} else if !log.refusedPublish("bridge", otherEvents) {
			t.Errorf("%-64s no reply (%v), but the server logged no publish violation for it", otherEvents, err)
		} else {
			t.Logf("%-64s refused (server: Publish Violation)", otherEvents)
		}

		// Read, route one: DIRECT.GET carries the message subject as its own
		// trailing token, so `$JS.API.DIRECT.GET.TASKS.>` reaches every
		// subject in the stream.
		got, err := stream.GetLastMsgForSubject(ctx, otherIn)
		if err != nil {
			t.Fatalf("DIRECT.GET on another addressee's task: %v", err)
		}
		if string(got.Data) != otherBody {
			t.Fatalf("DIRECT.GET returned %s, want the other addressee's submission", got.Data)
		}
		t.Logf("%-64s ALLOWED: %s", "DIRECT.GET TASKS "+otherIn, got.Data)

		// Read, route two: filter_subject travels in the request BODY, so a
		// consumer scoped to the whole plane is permitted by a grant that
		// names only the stream. No subject grant can scope this one.
		whole, err := js.CreateOrUpdateConsumer(ctx, "TASKS", jetstream.ConsumerConfig{
			Durable: "bridge-wholeplane", FilterSubject: "a2a.tasks.>", AckPolicy: jetstream.AckExplicitPolicy,
		})
		if err != nil {
			t.Fatalf("CONSUMER.CREATE filtered on the whole task plane: %v", err)
		}
		drain, err := whole.Fetch(16, jetstream.FetchMaxWait(5*time.Second))
		if err != nil {
			t.Fatalf("fetch from the whole-plane consumer: %v", err)
		}
		var sawOther bool
		for m := range drain.Messages() {
			if m.Subject() == otherIn && string(m.Data()) == otherBody {
				sawOther = true
			}
			_ = m.Ack()
		}
		if !sawOther {
			t.Error("a consumer filtered on a2a.tasks.> delivered nothing from another addressee: " +
				"if the server has started checking filter_subject against the grant, then the read " +
				"narrowing is real after all and bridgeIdentity's comment overstates the residue")
		} else {
			t.Log("CONSUMER.CREATE + CONSUMER.MSG.NEXT filtered on a2a.tasks.> ALLOWED, and delivered another addressee's submission")
		}
	})

	t.Run("the wildcard this replaces let the bridge delete the directory", func(t *testing.T) {
		before := strings.Replace(conf, a2aNATSConfGrantLines(a2aBridgeJetStreamGrants()), fmt.Sprintf(a2aNATSConfGrantLine, "$JS.API.>"), 1)
		if before == conf {
			t.Fatal("could not put $JS.API.> back into the bridge block; the control is gone")
		}
		s, log := a2aStartRenderedServer(t, before)
		a2aProvisionLikeTheScript(t, s.ClientURL(), seedPW)
		bridge, _ := a2aConnectAs(t, s.ClientURL(), "bridge", bridgePW)
		for _, subject := range []string{"$JS.API.STREAM.PURGE.DIRECTORY", "$JS.API.STREAM.DELETE.DIRECTORY"} {
			msg, err := bridge.Request(subject, nil, 2*time.Second)
			if err != nil {
				t.Fatalf("%s under $JS.API.>: %v (violations %q)", subject, err, log.publishViolations("bridge"))
			}
			var resp struct {
				Success bool `json:"success"`
			}
			if err := json.Unmarshal(msg.Data, &resp); err != nil || !resp.Success {
				t.Fatalf("%s under $JS.API.> answered %s", subject, msg.Data)
			}
			t.Logf("%-64s ALLOWED under $JS.API.>: %s", subject, msg.Data)
		}
		_, seedJS := a2aConnectAs(t, s.ClientURL(), "seed", seedPW)
		if _, err := seedJS.Stream(ctx, "DIRECTORY"); !errors.Is(err, jetstream.ErrStreamNotFound) {
			t.Fatalf("DIRECTORY should be gone after the bridge's DELETE under the wildcard; got %v", err)
		}
		t.Log("DIRECTORY is gone; only a re-run of the provision Job brings it back")
	})
}

// TestBridgeConsumersSurviveABusRestart is the reconnect canary for the grant.
// The question review asked: does the scoped list starve a client's own
// recovery path? An older nats.go re-verified a consumer with CONSUMER.INFO
// after every reconnect, and the bridge holds no CONSUMER.INFO. In the pinned
// nats.go (a2a/go.mod), Consume re-issues its pull on CONNECTED, Messages
// resets its counters, and the ordered consumer re-creates itself through
// CONSUMER.CREATE; none of them asks for consumer info. This test is what
// holds that: the bridge's durable under Consume, an ordered Messages
// iterator (TasksGet's replay) and the sweep's kv.Keys all cross a server
// shutdown and restart on the same port and store, connected as bridge, and
// the only publish violation the restarted server logs is the ordered
// consumer's fire-and-forget CONSUMER.DELETE.TASKS.* -- the one subject the
// grant withholds on purpose. A nats.go bump that brings a CONSUMER.INFO
// re-verify back fails here with the subject named.
func TestBridgeConsumersSurviveABusRestart(t *testing.T) {
	creds := a2aFullCreds("a", "1")
	conf := string(buildA2ANATSConfigSecret(a2aTestAgent(), creds, a2aTestCalloutKeys(t)).Data["nats.conf"])
	bridgePW := string(creds.Data["bridge-password"])
	seedPW := string(creds.Data["seed-password"])
	gatewayPW := string(creds.Data["gateway-password"])

	// A port the restarted server can come back on.
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := l.Addr().(*net.TCPAddr).Port
	_ = l.Close()
	store := t.TempDir()
	url := "nats://127.0.0.1:" + strconv.Itoa(port)

	first, firstLog := a2aStartRenderedServerAt(t, conf, port, store)
	a2aProvisionLikeTheScript(t, url, seedPW)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	// The lib's reconnect posture (MaxReconnects -1), with a short wait so
	// the test is not paced by backoff.
	reconnected := make(chan struct{}, 4)
	bridge, err := nats.Connect(url,
		nats.UserInfo("bridge", bridgePW), nats.CustomInboxPrefix("_INBOX.bridge"), nats.Name("bridge"),
		nats.MaxReconnects(-1), nats.ReconnectWait(200*time.Millisecond),
		nats.ReconnectHandler(func(*nats.Conn) { reconnected <- struct{}{} }),
		nats.ErrorHandler(func(*nats.Conn, *nats.Subscription, error) {}))
	if err != nil {
		t.Fatal(err)
	}
	defer bridge.Close()
	js, err := jetstream.New(bridge)
	if err != nil {
		t.Fatal(err)
	}

	// The bridge's durable under Consume, and an ordered Messages iterator
	// left open across the bounce.
	cons, err := js.CreateOrUpdateConsumer(ctx, "TASKS", jetstream.ConsumerConfig{
		Durable: "bridge-platform", FilterSubject: "a2a.tasks.platform.*.in", AckPolicy: jetstream.AckExplicitPolicy,
	})
	if err != nil {
		t.Fatal(err)
	}
	consumed := make(chan string, 16)
	cc, err := cons.Consume(func(m jetstream.Msg) {
		consumed <- string(m.Data())
		_ = m.Ack()
	})
	if err != nil {
		t.Fatal(err)
	}
	defer cc.Stop()
	oc, err := js.OrderedConsumer(ctx, "TASKS", jetstream.OrderedConsumerConfig{FilterSubjects: []string{"a2a.tasks.platform.t1.events"}})
	if err != nil {
		t.Fatal(err)
	}
	it, err := oc.Messages()
	if err != nil {
		t.Fatal(err)
	}
	defer it.Stop()
	replayed := make(chan string, 16)
	go func() {
		for {
			m, err := it.Next()
			if err != nil {
				return
			}
			replayed <- string(m.Data())
		}
	}()

	submitAsGateway := func(body string) {
		t.Helper()
		nc, err := nats.Connect(url, nats.UserInfo("gateway", gatewayPW), nats.CustomInboxPrefix("_INBOX.gateway"))
		if err != nil {
			t.Fatal(err)
		}
		defer nc.Close()
		gw, err := jetstream.New(nc)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := gw.Publish(ctx, "a2a.tasks.platform.t1.in", []byte(body)); err != nil {
			t.Fatalf("gateway submission %q: %v", body, err)
		}
	}
	expect := func(ch chan string, want string) {
		t.Helper()
		select {
		case got := <-ch:
			if got != want {
				t.Fatalf("received %q, want %q", got, want)
			}
			t.Logf("received %q", want)
		case <-time.After(20 * time.Second):
			t.Fatalf("did not receive %q within 20s", want)
		}
	}

	submitAsGateway("before-restart")
	expect(consumed, "before-restart")
	if _, err := js.Publish(ctx, "a2a.tasks.platform.t1.events", []byte("event-before-restart")); err != nil {
		t.Fatal(err)
	}
	expect(replayed, "event-before-restart")

	first.Shutdown()
	first.WaitForShutdown()
	second, secondLog := a2aStartRenderedServerAt(t, conf, port, store)
	defer func() {
		second.Shutdown()
		second.WaitForShutdown()
	}()
	select {
	case <-reconnected:
		t.Log("bridge reconnected to the restarted server")
	case <-time.After(20 * time.Second):
		t.Fatal("bridge did not reconnect")
	}

	submitAsGateway("after-restart")
	expect(consumed, "after-restart")
	if _, err := js.Publish(ctx, "a2a.tasks.platform.t1.events", []byte("event-after-restart")); err != nil {
		t.Fatal(err)
	}
	expect(replayed, "event-after-restart")
	kv, err := js.KeyValue(ctx, "runtime-state")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := kv.Put(ctx, "bridge.platform.t1", []byte("platform-bridge")); err != nil {
		t.Fatal(err)
	}
	if keys, err := kv.Keys(ctx); err != nil || !reflect.DeepEqual(keys, []string{"bridge.platform.t1"}) {
		t.Fatalf("kv.Keys after the restart: %q, %v", keys, err)
	}

	// The reset's fire-and-forget delete has no reply to wait for; give the
	// server a moment to log it before reading.
	time.Sleep(2 * time.Second)
	if v := firstLog.publishViolations("bridge"); len(v) != 0 {
		t.Errorf("violations before the restart: %q", v)
	}
	for _, line := range secondLog.publishViolations("bridge") {
		switch {
		case strings.Contains(line, "$JS.API.CONSUMER.DELETE.TASKS."):
			t.Logf("expected after the restart, the ordered reset's best-effort delete: %s", line)
		case strings.Contains(line, "$JS.API.CONSUMER.INFO."):
			t.Errorf("the client re-verified a consumer with CONSUMER.INFO after the reconnect, which the grant withholds: %s", line)
		default:
			t.Errorf("unexpected violation after the restart: %s", line)
		}
	}
}
