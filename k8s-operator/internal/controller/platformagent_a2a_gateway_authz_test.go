/*
Copyright 2026.

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
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// The gateway's own names on the bus, as its code spells them: the event
// relay's durable (a2a/gateway/gateway.go, relayDurable) and the two filter
// subjects it binds together since the supervisor split.
const (
	a2aGatewayRelayDurable     = "gateway-relay"
	a2aGatewayEventsFilter     = "a2a.tasks.*.*.events"
	a2aGatewaySupervisorFilter = "a2a.tasks.*.*.supervisor"
)

// TestGatewayJetStreamGrantOnARealServer is the refusal proof for the
// gateway's JetStream API grant, measured the way gke-labs/kube-agents#1666
// measured the hole: the config the operator renders, run by an embedded
// nats-server, the streams provisioned as the provision Job provisions them,
// and a client connected as gateway.
//
// Three tables and a control.
//
// The first is every JetStream operation the gateway performs -- the relay
// durable on TASKS with a pull and an ack, task submission and the
// supervisor's terminal, tasks/get's horizon read and ordered replay, and the
// session registry's bind, create, get, list and delete on KV_session-state --
// each of which must succeed, with zero publish violations logged for gateway
// across the lot. A grant missing from a2aGatewayJetStreamGrants shows up here
// as the exact subject the server refused, which is #1306's STREAM.NAMES
// lesson applied before the deploy instead of after it.
//
// The second is the destructive and out-of-scope surface, led by the call
// #1666 reported: STREAM.DELETE.TASKS. Each must be refused, and "refused" is
// read from the server's log -- a Publish Violation naming gateway and the
// exact subject -- not from the client's timeout, because a slow server
// produces the same timeout. The streams are then re-read as web to show
// nothing underneath changed.
//
// The third is small and is the honest part: the two routes the grant narrows
// and does not close, run as calls that SUCCEED because they ride subjects the
// grant permits. They are recorded rather than claimed shut.
//
// The control puts $JS.API.> back into the render and shows the same
// STREAM.DELETE.TASKS succeeding and the stream gone: the evidence that the
// refusals above are authorization rather than a broken API, and #1666
// reproduced on this server.
//
// The observer is `web`, not `seed` and not `gateway`. Reading a consumer back
// is CONSUMER.INFO, which this change takes off the gateway and which #1306
// took off seed along with everything but STREAM.CREATE and STREAM.INFO; web
// holds STREAM.INFO and CONSUMER.INFO on TASKS by enumeration and is the one
// principal that can answer. Asking as a principal without the grant does not
// fail, it hangs: a refused request is not an error nats.go reports.
func TestGatewayJetStreamGrantOnARealServer(t *testing.T) {
	creds := a2aFullCreds("a", "1")
	conf := string(buildA2ANATSConfigSecret(a2aTestAgent(), creds, a2aTestCalloutKeys(t)).Data["nats.conf"])
	gatewayPW := string(creds.Data["gateway-password"])
	bridgePW := string(creds.Data["bridge-password"])
	seedPW := string(creds.Data["seed-password"])
	webPW := string(creds.Data["web-password"])

	s, log := a2aStartRenderedServer(t, conf)
	a2aProvisionLikeTheScript(t, s.ClientURL(), seedPW)
	gateway, js := a2aConnectAs(t, s.ClientURL(), "gateway", gatewayPW)
	_, web := a2aConnectAs(t, s.ClientURL(), "web", webPW)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	const (
		inSubject         = "a2a.tasks.platform.t1.in"
		eventsSubject     = "a2a.tasks.platform.t1.events"
		supervisorSubject = "a2a.tasks.platform.t1.supervisor"
		sessionKey        = "sessions.discord_live_thread-2f9a1c3d"
		taskIndexKey      = "tasks.t1"
	)

	// Table 1: the gateway's own operations, in the order it performs them.
	// Fatal on the first refusal, because everything after it depends on the
	// object it did not get.
	allowed := func(op string, err error) {
		t.Helper()
		if err != nil {
			t.Fatalf("%-72s REFUSED: %v\nserver log for gateway: %q", op, err, log.publishViolations("gateway"))
		}
		t.Logf("%-72s allowed", op)
	}

	// The relay: two filter subjects, so nats.go puts the filter in the
	// request BODY and the API subject ends at the durable name.
	relay, err := js.CreateOrUpdateConsumer(ctx, a2aTasksStream, jetstream.ConsumerConfig{
		Durable:        a2aGatewayRelayDurable,
		FilterSubjects: []string{a2aGatewayEventsFilter, a2aGatewaySupervisorFilter},
		AckPolicy:      jetstream.AckExplicitPolicy,
	})
	allowed("CONSUMER.CREATE TASKS durable gateway-relay, two filters (gateway.Run)", err)

	// The same durable with ONE filter subject: the shape every install
	// already carries on disk, and the shape a rebind goes through. nats.go
	// appends the filter to the API subject here, so this is the row that
	// needs CONSUMER.CREATE.TASKS.> rather than a grant naming the durable.
	_, err = js.CreateOrUpdateConsumer(ctx, a2aTasksStream, jetstream.ConsumerConfig{
		Durable:       a2aGatewayRelayDurable,
		FilterSubject: a2aGatewayEventsFilter,
		AckPolicy:     jetstream.AckExplicitPolicy,
	})
	allowed("CONSUMER.CREATE TASKS gateway-relay, single filter in the API subject (rebind)", err)
	relay, err = js.CreateOrUpdateConsumer(ctx, a2aTasksStream, jetstream.ConsumerConfig{
		Durable:        a2aGatewayRelayDurable,
		FilterSubjects: []string{a2aGatewayEventsFilter, a2aGatewaySupervisorFilter},
		AckPolicy:      jetstream.AckExplicitPolicy,
	})
	allowed("CONSUMER.CREATE TASKS gateway-relay, back to the pair (the update lib does)", err)

	// Submission, then an executor's event for the relay to deliver. The
	// executor is the bridge, which is the principal that may write events
	// on the platform addressee.
	_, err = js.Publish(ctx, inSubject, []byte(`{"kind":"message"}`))
	allowed("publish a2a.tasks.platform.t1.in (task submission)", err)
	bridge, bridgeJS := a2aConnectAs(t, s.ClientURL(), "bridge", bridgePW)
	_ = bridge
	if _, err := bridgeJS.Publish(ctx, eventsSubject, []byte(`{"kind":"status-update"}`)); err != nil {
		t.Fatalf("executor event as bridge: %v", err)
	}

	batch, err := relay.Fetch(1, jetstream.FetchMaxWait(5*time.Second))
	allowed("CONSUMER.MSG.NEXT TASKS gateway-relay (relay Consume)", err)
	var delivered jetstream.Msg
	for m := range batch.Messages() {
		delivered = m
	}
	if delivered == nil {
		t.Fatalf("the relay pull delivered nothing: %v", batch.Error())
	}
	allowed("$JS.ACK TASKS gateway-relay (msg.Ack)", delivered.Ack())
	// The ack landed, read from web's side because the gateway holds no
	// CONSUMER.INFO.
	relayView, err := web.Consumer(ctx, a2aTasksStream, a2aGatewayRelayDurable)
	if err != nil {
		t.Fatal(err)
	}
	if info, err := relayView.Info(ctx); err != nil || info.AckFloor.Consumer != 1 {
		t.Fatalf("ack floor after the gateway's ack: %+v (%v)", info, err)
	}

	// The supervisor's terminal, the gateway's own write on the task plane.
	_, err = js.Publish(ctx, supervisorSubject, []byte(`{"kind":"status-update","final":true}`))
	allowed("publish a2a.tasks.platform.t1.supervisor (supervisor terminal)", err)

	// lib.TasksGet: stream info, the replay horizon on both subjects, then
	// an ordered consumer over the pair.
	stream, err := js.Stream(ctx, a2aTasksStream)
	allowed("STREAM.INFO TASKS (lib.TasksGet js.Stream)", err)
	_, err = stream.GetLastMsgForSubject(ctx, eventsSubject)
	allowed("DIRECT.GET TASKS last-for-subject, events (replay horizon)", err)
	_, err = stream.GetLastMsgForSubject(ctx, supervisorSubject)
	allowed("DIRECT.GET TASKS last-for-subject, supervisor (replay horizon)", err)
	oc, err := js.OrderedConsumer(ctx, a2aTasksStream, jetstream.OrderedConsumerConfig{
		FilterSubjects: []string{eventsSubject, supervisorSubject},
		DeliverPolicy:  jetstream.DeliverAllPolicy,
	})
	allowed("CONSUMER.CREATE TASKS ordered, server-generated name (TasksGet replay)", err)
	it, err := oc.Messages()
	allowed("ordered consumer Messages()", err)
	for i := 1; i <= 2; i++ {
		_, err := it.Next()
		allowed(fmt.Sprintf("CONSUMER.MSG.NEXT TASKS ordered, replay message %d", i), err)
	}
	it.Stop()

	// The direct-get route a2a/gateway/live_test.go takes to find a task by
	// subject filter, which is the same API subject with a wildcard in the
	// subject token. Raw, because it is a raw request there too.
	lastIn, err := gateway.Request("$JS.API.DIRECT.GET."+a2aTasksStream+".a2a.tasks.platform.*.in", nil, 5*time.Second)
	allowed("DIRECT.GET TASKS with a wildcard subject token (live_test findLatestLiveTask)", err)
	if err == nil && !strings.Contains(string(lastIn.Data), `"kind":"message"`) {
		t.Errorf("the wildcard direct get answered %q, not the submission; live_test.go reads this reply's body", lastIn.Data)
	}

	// gateway.Registry, over the session-state bucket.
	kv, err := js.KeyValue(ctx, a2aSessionStateBucket)
	allowed("STREAM.INFO KV_session-state (Registry js.KeyValue)", err)
	_, err = kv.Create(ctx, sessionKey, []byte(`{"key":"discord:live/thread","contextId":"ctx-1"}`))
	allowed("$KV.session-state create, first contact (Registry.Create)", err)
	_, err = kv.Put(ctx, sessionKey, []byte(`{"key":"discord:live/thread","contextId":"ctx-1","podName":"p1"}`))
	allowed("$KV.session-state put (Registry.Put)", err)
	_, err = kv.Get(ctx, sessionKey)
	allowed("DIRECT.GET KV_session-state (Registry.Get)", err)
	_, err = kv.Put(ctx, taskIndexKey, []byte("discord:live/thread"))
	allowed("$KV.session-state put, task index (Registry.IndexTask)", err)
	_, err = kv.Get(ctx, taskIndexKey)
	allowed("DIRECT.GET KV_session-state, task index (Registry.SessionForTask)", err)

	lister, err := kv.ListKeysFiltered(ctx, "sessions.>")
	allowed("CONSUMER.CREATE KV_session-state, filtered watcher (Registry.Sessions, the reap scan)", err)
	var listed []string
	for key := range lister.Keys() {
		listed = append(listed, key)
	}
	if len(listed) != 1 || listed[0] != sessionKey {
		t.Fatalf("the reap scan listed %q, want [%s]", listed, sessionKey)
	}
	t.Logf("%-72s allowed", "CONSUMER.DELETE KV_session-state, watcher Stop (Registry.Sessions)")
	allowed("$KV.session-state delete (Registry.DropTask)", kv.Delete(ctx, taskIndexKey))

	if v := log.publishViolations("gateway"); len(v) != 0 {
		t.Errorf("the gateway's own operations tripped %d publish violations; each is a grant a2aGatewayJetStreamGrants is missing:\n%s",
			len(v), strings.Join(v, "\n"))
	}

	// Table 2: what the wildcard allowed and the grant refuses. The bodies
	// are what a real caller would send; the server never reads them,
	// because the permission check runs before the request is parsed.
	type call struct{ subject, body string }
	refused := []call{
		// #1666's own call, first.
		{"$JS.API.STREAM.DELETE." + a2aTasksStream, ""},
		{"$JS.API.STREAM.PURGE." + a2aTasksStream, ""},
		{"$JS.API.STREAM.UPDATE." + a2aTasksStream, `{"name":"TASKS","subjects":["a2a.tasks.>","a2a.agents.>"]}`},
		{"$JS.API.STREAM.MSG.DELETE." + a2aTasksStream, `{"seq":1}`},
		{"$JS.API.STREAM.RESTORE." + a2aTasksStream, ""},
		{"$JS.API.STREAM.SNAPSHOT." + a2aTasksStream, `{"deliver_subject":"_INBOX.gateway.snap"}`},
		// The read fallback: every provisioned stream is allow_direct, so
		// nats.go never emits this and the grant does not carry it.
		{"$JS.API.STREAM.MSG.GET." + a2aTasksStream, `{"last_by_subj":"a2a.tasks.platform.*.in"}`},
		// Another principal's consumer, by name. Create-as-update reaches
		// it (table 3); deleting it does not.
		{"$JS.API.CONSUMER.DELETE." + a2aTasksStream + ".bridge-platform", ""},
		{"$JS.API.CONSUMER.DELETE." + a2aTasksStream + "." + a2aGatewayRelayDurable, ""},
		{"$JS.API.CONSUMER.INFO." + a2aTasksStream + "." + a2aGatewayRelayDurable, ""},
		// The directory: the identity plane. The gateway reads it by
		// subscribing to the cards, and holds nothing on it here.
		{"$JS.API.STREAM.INFO.DIRECTORY", ""},
		{"$JS.API.STREAM.PURGE.DIRECTORY", ""},
		{"$JS.API.STREAM.DELETE.DIRECTORY", ""},
		{"$JS.API.CONSUMER.CREATE.DIRECTORY.peek", `{"stream_name":"DIRECTORY","config":{"name":"peek","deliver_subject":"_INBOX.gateway.peek"}}`},
		{"$JS.API.DIRECT.GET.DIRECTORY.a2a.agents.platform", ""},
		// The blackboard, which nothing in a2a/gateway touches.
		{"$JS.API.STREAM.INFO." + a2aTopicsStateStream, ""},
		{"$JS.API.DIRECT.GET." + a2aTopicsStateStream + ".a2a.topics.shared.blueprint", ""},
		{"$JS.API.STREAM.INFO." + a2aTopicsJournalStream, ""},
		{"$JS.API.STREAM.DELETE." + a2aTopicsJournalStream, ""},
		// The other two buckets: the bridge's registry and the capability
		// envelope's.
		{"$JS.API.STREAM.INFO." + a2aKVStreamPrefix + a2aRuntimeStateBucket, ""},
		{"$JS.API.DIRECT.GET." + a2aKVStreamPrefix + a2aRuntimeStateBucket + ".$KV." + a2aRuntimeStateBucket + ".k", ""},
		{"$JS.API.STREAM.INFO." + a2aKVStreamPrefix + "cap", ""},
		{"$JS.API.CONSUMER.CREATE." + a2aKVStreamPrefix + "cap.peek", `{"stream_name":"KV_cap","config":{"name":"peek","deliver_subject":"_INBOX.gateway.peek"}}`},
		// Its own bucket's destructive verbs: the registry is read and
		// written through the data plane, never edited as a stream.
		{"$JS.API.STREAM.PURGE." + a2aKVStreamPrefix + a2aSessionStateBucket, ""},
		{"$JS.API.STREAM.DELETE." + a2aKVStreamPrefix + a2aSessionStateBucket, ""},
		// Account discovery and enumeration.
		{"$JS.API.INFO", ""},
		{"$JS.API.STREAM.NAMES", ""},
		{"$JS.API.STREAM.LIST", ""},
		{"$JS.API.CONSUMER.NAMES." + a2aTasksStream, ""},
		{"$JS.API.STREAM.CREATE.EVIL", `{"name":"EVIL","subjects":["evil.>"]}`},
	}
	for _, c := range refused {
		// A short client wait is safe: "refused" is decided by the server's
		// log below, and a reply that arrives late fails the run as ALLOWED
		// only if it arrives at all.
		msg, err := gateway.Request(c.subject, []byte(c.body), 250*time.Millisecond)
		if err == nil {
			t.Errorf("%-72s ALLOWED: %s", c.subject, msg.Data)
			continue
		}
		if !log.refusedPublish("gateway", c.subject) {
			t.Errorf("%-72s no reply (%v), but the server logged no publish violation for it", c.subject, err)
			continue
		}
		t.Logf("%-72s refused (server: Publish Violation)", c.subject)
	}

	// Nothing underneath changed. Read as web, which holds STREAM.INFO on
	// the message streams by enumeration.
	tasks, err := web.Stream(ctx, a2aTasksStream)
	if err != nil {
		t.Fatalf("TASKS after the refused calls: %v", err)
	}
	if got := tasks.CachedInfo().State.Msgs; got != 3 {
		t.Errorf("TASKS holds %d messages after the refused calls, want 3 (submission, event, terminal)", got)
	}
	dir, err := web.Stream(ctx, "DIRECTORY")
	if err != nil {
		t.Fatalf("DIRECTORY after the refused calls: %v", err)
	}
	if got := dir.CachedInfo(); got.State.Msgs != 0 {
		t.Errorf("DIRECTORY changed under the gateway: %d msgs", got.State.Msgs)
	}
	if _, err := web.Consumer(ctx, a2aTasksStream, a2aGatewayRelayDurable); err != nil {
		t.Errorf("the relay durable after the gateway's refused CONSUMER.DELETE on itself: %v", err)
	}

	// Table 3: the two routes this grant narrows and does not close. Both
	// ride subjects the grant PERMITS, so the refusal table above cannot
	// see them and no violation is logged for either. Measured here so the
	// claim in a2aGatewayJetStreamGrants is a measurement.
	t.Run("the routes the grant narrows and does not close", func(t *testing.T) {
		// 1. CONSUMER.CREATE is create-or-update by name, and the server
		// has no ownership concept for a consumer name. The bridge's
		// durable is another principal's; the gateway can retune it
		// without ever naming CONSUMER.DELETE.
		if _, err := bridgeJS.CreateOrUpdateConsumer(ctx, a2aTasksStream, jetstream.ConsumerConfig{
			Durable: "bridge-platform", FilterSubject: "a2a.tasks.platform.*.in", AckPolicy: jetstream.AckExplicitPolicy,
		}); err != nil {
			t.Fatalf("the bridge's own durable: %v", err)
		}
		const bridgeCreate = "$JS.API.CONSUMER.CREATE." + a2aTasksStream + ".bridge-platform"
		body := []byte(`{"stream_name":"TASKS","config":{"durable_name":"bridge-platform","name":"bridge-platform",` +
			`"ack_policy":"explicit","deliver_policy":"all","replay_policy":"instant","filter_subject":"a2a.tasks.none"}}`)
		msg, err := gateway.Request(bridgeCreate, body, 5*time.Second)
		if err != nil {
			t.Fatalf("%s: no reply (%v); violations: %q", bridgeCreate, err, log.publishViolations("gateway"))
		}
		if log.refusedPublish("gateway", bridgeCreate) {
			t.Fatalf("%s was refused; the grant no longer permits the relay's own create either", bridgeCreate)
		}
		var resp struct {
			Error *struct {
				Description string `json:"description"`
			} `json:"error"`
		}
		if err := json.Unmarshal(msg.Data, &resp); err != nil || resp.Error != nil {
			t.Fatalf("create-as-update on another principal's durable: %v %+v", err, resp.Error)
		}
		view, err := web.Consumer(ctx, a2aTasksStream, "bridge-platform")
		if err != nil {
			t.Fatal(err)
		}
		if got := view.CachedInfo().Config.FilterSubject; got != "a2a.tasks.none" {
			t.Errorf("bridge-platform's filter subject is %q; the gateway's create-as-update did not take. "+
				"If the server has gained an ownership check, a2aGatewayJetStreamGrants can stop recording this residue", got)
		} else {
			t.Log("residue 1: the gateway retuned another principal's durable through CONSUMER.CREATE, with no violation logged")
		}

		// 2. A push consumer's deliver_subject is a body field no subject
		// grant can see. Aimed at the directory -- a stream the gateway
		// holds nothing on -- with the gateway itself subscribed to the
		// card subject, which is inside its own a2a.agents.> grant.
		before := func() uint64 {
			t.Helper()
			st, err := web.Stream(ctx, "DIRECTORY")
			if err != nil {
				t.Fatal(err)
			}
			return st.CachedInfo().State.Msgs
		}()
		sub, err := gateway.SubscribeSync("a2a.agents.platform")
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = sub.Unsubscribe() }()
		_ = gateway.Flush()
		divert := []byte(`{"stream_name":"TASKS","config":{"name":"divert","deliver_subject":"a2a.agents.platform","deliver_policy":"all","ack_policy":"none"}}`)
		if _, err := gateway.Request("$JS.API.CONSUMER.CREATE."+a2aTasksStream+".divert", divert, 5*time.Second); err != nil {
			t.Fatalf("the deliver-subject consumer: %v", err)
		}
		time.Sleep(time.Second)
		st, err := web.Stream(ctx, "DIRECTORY")
		if err != nil {
			t.Fatal(err)
		}
		after, err := st.Info(ctx, jetstream.WithSubjectFilter("a2a.tasks.>"))
		if err != nil {
			t.Fatal(err)
		}
		var underTaskSubjects uint64
		for _, n := range after.State.Subjects {
			underTaskSubjects += n
		}
		t.Logf("residue 2: deliver_subject aimed at the directory with the gateway subscribed to the card subject: DIRECTORY %d -> %d, %d under a2a.tasks.* subjects",
			before, after.State.Msgs, underTaskSubjects)
		if after.State.Msgs != underTaskSubjects {
			t.Errorf("DIRECTORY holds %d messages but only %d sit under a2a.tasks.* subjects; a card subject was written, which would be forgery rather than the eviction lever this residue is",
				after.State.Msgs, underTaskSubjects)
		}
	})

	// The control: the same render with the wildcard put back, and #1666
	// reproduced. Last, because it deletes TASKS.
	t.Run("the wildcard this replaces let the gateway delete the task stream", func(t *testing.T) {
		before := strings.Replace(conf, a2aNATSConfGrantLines(a2aGatewayJetStreamGrants()), fmt.Sprintf(a2aNATSConfGrantLine, "$JS.API.>"), 1)
		if before == conf {
			t.Fatal("could not put $JS.API.> back into the gateway block; the control is gone")
		}
		s, log := a2aStartRenderedServer(t, before)
		a2aProvisionLikeTheScript(t, s.ClientURL(), seedPW)
		gateway, _ := a2aConnectAs(t, s.ClientURL(), "gateway", gatewayPW)
		for _, subject := range []string{"$JS.API.STREAM.PURGE." + a2aTasksStream, "$JS.API.STREAM.DELETE." + a2aTasksStream} {
			msg, err := gateway.Request(subject, nil, 2*time.Second)
			if err != nil {
				t.Fatalf("%s under $JS.API.>: %v (violations %q)", subject, err, log.publishViolations("gateway"))
			}
			var resp struct {
				Success bool `json:"success"`
			}
			if err := json.Unmarshal(msg.Data, &resp); err != nil || !resp.Success {
				t.Fatalf("%s under $JS.API.> answered %s", subject, msg.Data)
			}
			t.Logf("%-72s ALLOWED under $JS.API.>: %s", subject, msg.Data)
		}
		_, webJS := a2aConnectAs(t, s.ClientURL(), "web", webPW)
		if _, err := webJS.Stream(ctx, a2aTasksStream); !errors.Is(err, jetstream.ErrStreamNotFound) {
			t.Fatalf("TASKS should be gone after the gateway's DELETE under the wildcard; got %v", err)
		}
		t.Log("TASKS is gone, with every task's history and its consumers; only a re-run of the provision Job brings the stream back, empty")
	})
}

// TestGatewayConsumersSurviveABusRestart is the reconnect canary, and it is
// what holds the CONSUMER.INFO decision up.
//
// a2aGatewayJetStreamGrants withholds CONSUMER.INFO on the argument that
// nothing on the gateway path binds a consumer by name and that on nats.go
// v1.53.1 neither Consume nor an ordered iterator re-verifies its consumer
// with Info() after a reconnect. That is a reading of a pinned library, and
// the failure it would cause -- a relay that goes quiet after the bus bounces,
// with nothing in the gateway's own log to say why -- is exactly the class
// this deployment has been bitten by. So it is executed rather than read: the
// relay durable under Consume, an ordered Messages() iterator and a KV watcher
// all cross a server shutdown and restart on the same port and store,
// connected as gateway, and any CONSUMER.INFO violation fails the test with
// the subject named.
func TestGatewayConsumersSurviveABusRestart(t *testing.T) {
	creds := a2aFullCreds("a", "1")
	conf := string(buildA2ANATSConfigSecret(a2aTestAgent(), creds, a2aTestCalloutKeys(t)).Data["nats.conf"])
	gatewayPW := string(creds.Data["gateway-password"])
	bridgePW := string(creds.Data["bridge-password"])
	seedPW := string(creds.Data["seed-password"])

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
	gateway, err := nats.Connect(url,
		nats.UserInfo("gateway", gatewayPW), nats.CustomInboxPrefix("_INBOX.gateway"), nats.Name("gateway"),
		nats.MaxReconnects(-1), nats.ReconnectWait(200*time.Millisecond),
		nats.ReconnectHandler(func(*nats.Conn) { reconnected <- struct{}{} }),
		nats.ErrorHandler(func(*nats.Conn, *nats.Subscription, error) {}))
	if err != nil {
		t.Fatal(err)
	}
	defer gateway.Close()
	js, err := jetstream.New(gateway)
	if err != nil {
		t.Fatal(err)
	}

	relay, err := js.CreateOrUpdateConsumer(ctx, a2aTasksStream, jetstream.ConsumerConfig{
		Durable:        a2aGatewayRelayDurable,
		FilterSubjects: []string{a2aGatewayEventsFilter, a2aGatewaySupervisorFilter},
		AckPolicy:      jetstream.AckExplicitPolicy,
	})
	if err != nil {
		t.Fatal(err)
	}
	relayed := make(chan string, 16)
	cc, err := relay.Consume(func(m jetstream.Msg) {
		relayed <- string(m.Data())
		_ = m.Ack()
	})
	if err != nil {
		t.Fatal(err)
	}
	defer cc.Stop()

	oc, err := js.OrderedConsumer(ctx, a2aTasksStream, jetstream.OrderedConsumerConfig{
		FilterSubjects: []string{"a2a.tasks.platform.t1.events"},
	})
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

	eventAsBridge := func(body string) {
		t.Helper()
		nc, err := nats.Connect(url, nats.UserInfo("bridge", bridgePW), nats.CustomInboxPrefix("_INBOX.bridge"))
		if err != nil {
			t.Fatal(err)
		}
		defer nc.Close()
		bjs, err := jetstream.New(nc)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := bjs.Publish(ctx, "a2a.tasks.platform.t1.events", []byte(body)); err != nil {
			t.Fatalf("executor event %q: %v", body, err)
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

	eventAsBridge("before-restart")
	expect(relayed, "before-restart")
	expect(replayed, "before-restart")

	first.Shutdown()
	first.WaitForShutdown()
	second, secondLog := a2aStartRenderedServerAt(t, conf, port, store)
	defer func() {
		second.Shutdown()
		second.WaitForShutdown()
	}()
	select {
	case <-reconnected:
	case <-time.After(30 * time.Second):
		t.Fatal("the gateway client did not reconnect to the restarted server")
	}

	eventAsBridge("after-restart")
	expect(relayed, "after-restart")
	expect(replayed, "after-restart")

	// The registry across the bounce too: bind, write, read, list.
	kv, err := js.KeyValue(ctx, a2aSessionStateBucket)
	if err != nil {
		t.Fatalf("the session registry after the restart: %v", err)
	}
	if _, err := kv.Put(ctx, "sessions.after-restart", []byte(`{"key":"after"}`)); err != nil {
		t.Fatalf("registry put after the restart: %v", err)
	}
	if _, err := kv.Get(ctx, "sessions.after-restart"); err != nil {
		t.Fatalf("registry get after the restart: %v", err)
	}
	lister, err := kv.ListKeysFiltered(ctx, "sessions.>")
	if err != nil {
		t.Fatalf("the reap scan after the restart: %v", err)
	}
	var keys []string
	for k := range lister.Keys() {
		keys = append(keys, k)
	}
	if len(keys) != 1 {
		t.Errorf("the reap scan listed %q after the restart", keys)
	}

	// The only violation either server may log for the gateway is the
	// ordered consumer's fire-and-forget reset delete, which nats.go
	// ignores. A CONSUMER.INFO here means the grant is wrong.
	for name, log := range map[string]*a2aServerLog{"before the restart": firstLog, "after the restart": secondLog} {
		for _, line := range log.publishViolations("gateway") {
			switch {
			case strings.Contains(line, "$JS.API.CONSUMER.INFO."):
				t.Errorf("%s: the gateway emitted CONSUMER.INFO, which a2aGatewayJetStreamGrants withholds: %s", name, line)
			case strings.Contains(line, "$JS.API.CONSUMER.DELETE."+a2aTasksStream):
				t.Logf("%s: the ordered consumer's best-effort reset delete, refused and ignored (expected): %s", name, line)
			default:
				t.Errorf("%s: unexpected publish violation for gateway: %s", name, line)
			}
		}
	}
}
