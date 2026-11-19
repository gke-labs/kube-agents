package authcallout

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The scheduled-reports bucket (a2a/gateway/scheduled_reports.go) holds each
// schedule run's report and whether and where it was posted. The gateway
// writes a run's record before it posts, and reads it back so a redelivered
// terminal never posts twice; a principal that could write a record could
// mark a run posted and suppress it, and one that could read them would see
// every report. So: the gateway alone, measured on a server running the
// shipped nats.conf and identity map, with every other principal the render
// carries asked by publishing the real subjects, which a wildcard grant would
// let through as surely as a named one.

// scheduledReportsPrincipal is one principal the render carries, connected.
type scheduledReportsPrincipal struct {
	name string
	nc   *nats.Conn
	v    chan error
}

// connectEveryRenderedPrincipal connects as every principal the shipped
// render carries except the gateway: each static user in the rendered
// nats.conf's auth_users, with its rendered password, and each callout entry
// in the rendered profiles map (the fullest map: operator, profiles and
// dispatcher beside the default set), with a token attesting its
// ServiceAccount and, for an entry that narrows on its pod, a pod.
func connectEveryRenderedPrincipal(t *testing.T) (*harness, []scheduledReportsPrincipal) {
	t.Helper()
	m, err := ParseIdentityMap([]byte(renderedProfilesMap(t)))
	if err != nil {
		t.Fatalf("the rendered profiles map does not parse: %v", err)
	}
	type callout struct{ user, token string }
	var callouts []callout
	tokens := map[string]Attested{}
	for _, id := range m.Identities {
		token := "token-for-" + id.User + "-padded-to-a-realistic-length-for-the-review"
		a := Attested{ServiceAccount: id.ServiceAccount}
		user := id.User
		if id.Narrowing == NarrowingPod || id.Narrowing == NarrowingProfile {
			a.PodName, a.PodUID = id.User+"-pod-k3j8w", "uid-"+id.User
			user = a.PodName
		}
		tokens[token] = a
		callouts = append(callouts, callout{user, token})
	}
	h := startHarness(t, renderedProfilesMap(t), tokens)

	var out []scheduledReportsPrincipal
	for _, user := range renderedFixtureAuthUsers(t) {
		if user == "gateway" {
			continue
		}
		nc, v := connectStatic(t, h, user, "pw-"+user)
		out = append(out, scheduledReportsPrincipal{user, nc, v})
	}
	for _, c := range callouts {
		if c.user == "gateway" {
			continue // the gateway's own ServiceAccount entry: the gateway, asked above
		}
		nc, v := h.connectAs(t, c.user, c.token)
		out = append(out, scheduledReportsPrincipal{c.user, nc, v})
	}
	if len(out) < len(callouts)+1 {
		t.Fatalf("connected %d principals; the render carries more", len(out))
	}
	return h, out
}

// TestTheScheduledReportsBucketIsTheGatewaysAlone: provision creates the
// bucket, the gateway creates, updates, puts and reads back a run's record,
// and every other principal is refused the write, the read and the bind.
// The provision principal and seed hold STREAM.INFO on it for the provision
// script's info-then-add guard, asserted as allowed so that losing it fails
// here; it returns stream state, never a record.
func TestTheScheduledReportsBucketIsTheGatewaysAlone(t *testing.T) {
	assertGatewayOnlyBucket(t, lib.ScheduledReportsBucket, "compliance-audit.task-0123456789abcdef")
}

// TestTheScheduleThreadsBucketIsTheGatewaysAlone is the same measurement for
// SCHEDULE_THREADS (a2a/gateway/schedule_threads.go): a principal that could
// write a day's thread would steer every later report of that schedule into
// a thread of its choosing, and one that could read it would learn where.
func TestTheScheduleThreadsBucketIsTheGatewaysAlone(t *testing.T) {
	assertGatewayOnlyBucket(t, lib.ScheduleThreadsBucket, "compliance-audit.2026-11-16")
}

// assertGatewayOnlyBucket holds bucket, at key, to the gateway alone: see
// TestTheScheduledReportsBucketIsTheGatewaysAlone.
func assertGatewayOnlyBucket(t *testing.T, bucket, key string) {
	t.Helper()
	stream := "KV_" + bucket
	h, others := connectEveryRenderedPrincipal(t)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	var provision *nats.Conn
	for _, p := range others {
		if p.name == "provision" {
			provision = p.nc
		}
	}
	if provision == nil {
		t.Fatal("the render carries no provision principal")
	}
	pjs, err := jetstream.New(provision)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := pjs.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: bucket, History: 1}); err != nil {
		t.Fatalf("the provision principal could not create %s: %v", bucket, err)
	}

	// The gateway: bind, create, update in place, put, and read back.
	gw, _ := connectStatic(t, h, "gateway", renderedGatewayPassword)
	gjs, err := jetstream.New(gw)
	if err != nil {
		t.Fatal(err)
	}
	kv, err := gjs.KeyValue(ctx, bucket)
	if err != nil {
		t.Fatalf("the gateway could not bind %s: %v", bucket, err)
	}
	rev, err := kv.Create(ctx, key, []byte(`{"status":"posting"}`))
	if err != nil {
		t.Fatalf("the gateway could not create a record: %v", err)
	}
	if _, err := kv.Update(ctx, key, []byte(`{"status":"posted"}`), rev); err != nil {
		t.Fatalf("the gateway could not update its record: %v", err)
	}
	if _, err := kv.Put(ctx, key, []byte(`{"status":"posted","attempts":1}`)); err != nil {
		t.Fatalf("the gateway could not put its record: %v", err)
	}
	e, err := kv.Get(ctx, key)
	if err != nil || string(e.Value()) != `{"status":"posted","attempts":1}` {
		t.Fatalf("the gateway read back %v (%v)", e, err)
	}
	last := e.Revision()

	write := "$KV." + bucket + "." + key
	read := "$JS.API.DIRECT.GET." + stream + "." + write
	bind := "$JS.API.STREAM.INFO." + stream
	for _, p := range others {
		if p.name == "sys" {
			continue // its own account; asked below
		}
		t.Run(p.name, func(t *testing.T) {
			want := map[string]bool{write: true, read: true, bind: true}
			if p.name == "provision" || p.name == "seed" {
				want[bind] = false
			}
			checkPublish(t, p.nc, p.v, want)
			if !subscribeRefused(t, p.nc, p.v, "$KV."+bucket+".>") {
				t.Errorf("%s may subscribe to the bucket's subjects; it would see every report as it is written", p.name)
			}
		})
	}

	// sys holds no subject list: it is a person's credential in the system
	// account, and the account is its boundary. The TRUSTED account exports
	// neither the bucket's subjects nor its stream's API to it, so its
	// write lands nowhere, its read finds no JetStream to answer, and its
	// subscription sees none of the gateway's writes.
	for _, p := range others {
		if p.name != "sys" {
			continue
		}
		t.Run(p.name, func(t *testing.T) {
			if err := p.nc.Publish(write, []byte(`{"status":"silent"}`)); err != nil {
				t.Fatal(err)
			}
			// Answered, if at all, by the system account's own JetStream
			// API, which says JetStream is not enabled there.
			for _, subject := range []string{read, bind} {
				if msg, err := p.nc.Request(subject, nil, time.Second); err == nil && !strings.Contains(string(msg.Data), `"error"`) {
					t.Errorf("sys was answered on %s: %q", subject, msg.Data)
				}
			}
			// Subscribed after its own publish, so only the gateway's
			// write below could arrive.
			sub, err := p.nc.SubscribeSync("$KV." + bucket + ".>")
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = sub.Unsubscribe() }()
			if err := p.nc.Flush(); err != nil {
				t.Fatal(err)
			}
			if last, err = kv.Put(ctx, key, []byte(`{"status":"posted","attempts":2}`)); err != nil {
				t.Fatal(err)
			}
			if msg, err := sub.NextMsg(500 * time.Millisecond); err == nil {
				t.Errorf("sys saw the gateway's write: %q", msg.Data)
			}
		})
	}

	// Nothing above landed: the record is the gateway's last write.
	e, err = kv.Get(ctx, key)
	if err != nil || e.Revision() != last {
		t.Fatalf("the record moved past the gateway's write: %v (%v)", e, err)
	}
}
