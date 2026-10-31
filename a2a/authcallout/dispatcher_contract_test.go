package authcallout

import (
	"context"
	"os"
	"strconv"
	"testing"
	"time"

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

// The map parses with the dispatcher in it, as a plain callout principal.
func TestTheOperatorsDispatcherMapParses(t *testing.T) {
	m, err := ParseIdentityMap([]byte(renderedDispatcherMap(t)))
	if err != nil {
		t.Fatalf("the operator's map with the dispatcher does not parse: %v", err)
	}
	for _, id := range m.Identities {
		if id.User == "dispatcher" {
			if id.ServiceAccount != renderedDispatcherSA || id.Narrowing != "" {
				t.Errorf("dispatcher entry = %+v", id)
			}
			return
		}
	}
	t.Fatal("the operator's map has no dispatcher")
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
	})
	for subject, wantRefused := range map[string]bool{
		lib.DispatchAcceptSubject("tse", "read", pod): false,
		lib.DispatchLeaseSubject("tse", "read", pod):  false,
		"a2a.tasks.>":                           true,
		"a2a.submit.>":                          true,
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

// The console reads a profile submission by subject, and can make no
// consumer on PROFILE_SUBMITS: it shows a profile task's prompt, and holds
// nothing that could pull a profile's queue.
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
	nc, violations := connectStatic(t, h, "console", renderedConsolePassword)
	msg, err := nc.RequestWithContext(ctx, "$JS.API.DIRECT.GET."+lib.ProfileSubmitStream+"."+subject, nil)
	if err != nil || msg.Header.Get("Status") != "404" {
		t.Fatalf("the console's direct get of %s = %v, %v; want the server's 404 for a stream with no such message", subject, msg, err)
	}
	checkPublish(t, nc, violations, map[string]bool{
		"$JS.API.CONSUMER.CREATE." + lib.ProfileSubmitStream + ".x." + subject:                                      true,
		"$JS.API.CONSUMER.MSG.NEXT." + lib.ProfileSubmitStream + "." + lib.ProfileDurableName("tse", "read", "top"): true,
		"$JS.API.CONSUMER.INFO." + lib.ProfileSubmitStream + "." + lib.ProfileDurableName("tse", "read", "top"):     true,
		"$JS.API.STREAM.INFO." + lib.ProfileSubmitStream:                                                            true,
		"$JS.ACK." + lib.ProfileSubmitStream + ".>":                                                                 true,
		subject: true,
	})
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
