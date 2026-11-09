package controller

import (
	"context"
	"os"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// requester is the one call the refusal tables make.
type requester interface {
	Request(subject string, data []byte, timeout time.Duration) (*nats.Msg, error)
}

// The TASKS durables the provision Job pre-creates (#4173): the gateway's
// event relay and the bridge's intake. The Job creates them create-only, their
// readers bind and create nothing, and every principal that may create
// consumers on TASKS is refused create, update and delete on those two names.

// a2aProvisionStandInPW is the password of the static stand-in for the
// provision Job. The Job authenticates through the auth callout, which an
// embedded server can't run, so the tests render provisionIdentity's
// permissions as a static TRUSTED user: the same grants, checked by the same
// server, reached by a password instead of a token.
const a2aProvisionStandInPW = "pw-provision-stand-in"

// a2aWithProvisionStandIn returns conf with the provision stand-in added to
// the TRUSTED account's users.
func a2aWithProvisionStandIn(t *testing.T, conf string) string {
	t.Helper()
	trusted := strings.Index(conf, "\n  TRUSTED {\n")
	if trusted < 0 {
		t.Fatal("no TRUSTED account in the rendered nats.conf")
	}
	const usersOpen = "\n    users [\n"
	users := strings.Index(conf[trusted:], usersOpen)
	if users < 0 {
		t.Fatal("no users list in the TRUSTED account")
	}
	at := trusted + users + len(usersOpen)
	id := provisionIdentity(a2aTestAgent(), a2aTestAgent().Namespace)
	user := renderA2AStaticUser(id, func(string) string { return a2aProvisionStandInPW })
	conf = conf[:at] + user + conf[at:]
	// A static user the callout skips, or the callout is asked about it.
	const bypass = "auth_users: [ "
	if strings.Count(conf, bypass) != 1 {
		t.Fatal("no single auth_users list in the rendered nats.conf")
	}
	return strings.Replace(conf, bypass, bypass+"provision, ", 1)
}

// a2aFixedDurableConfigs is what the provision script's two `consumer add`
// lines ask for, translated to ConsumerConfig. TestTheProvisionScript-
// CreatesTheFixedDurablesAsTheTestsDo reads the flags back off the script.
func a2aFixedDurableConfigs() []jetstream.ConsumerConfig {
	return []jetstream.ConsumerConfig{
		{
			Durable:        a2aGatewayRelayDurable,
			FilterSubjects: []string{a2aGatewayEventsFilter, a2aGatewaySupervisorFilter},
			AckPolicy:      jetstream.AckExplicitPolicy,
			DeliverPolicy:  jetstream.DeliverAllPolicy,
			ReplayPolicy:   jetstream.ReplayInstantPolicy,
			MaxDeliver:     -1,
			MaxAckPending:  a2aGatewayRelayMaxAckPending,
		},
		{
			Durable:       a2aBridgeDurable(),
			FilterSubject: a2aBridgeIntakeFilter(),
			AckPolicy:     jetstream.AckExplicitPolicy,
			DeliverPolicy: jetstream.DeliverAllPolicy,
			ReplayPolicy:  jetstream.ReplayInstantPolicy,
			MaxDeliver:    -1,
		},
	}
}

// a2aPreCreateLikeTheProvisionJob creates the two durables as the provision
// stand-in, the way the script does: info, and create only on a miss. nats.go
// sends the same API subjects natscli 0.1.5 does for these shapes (CREATE
// with the filter token for one filter, without it for two), so a grant this
// misses is the grant the Job would miss.
func a2aPreCreateLikeTheProvisionJob(t *testing.T, url string, log *a2aServerLog) {
	t.Helper()
	_, js := a2aConnectAs(t, url, "provision", a2aProvisionStandInPW)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for _, cfg := range a2aFixedDurableConfigs() {
		if _, err := js.Consumer(ctx, a2aTasksStream, cfg.Durable); err == nil {
			continue
		}
		if _, err := js.CreateConsumer(ctx, a2aTasksStream, cfg); err != nil {
			t.Fatalf("pre-create %s as provision: %v\nserver log for provision: %q", cfg.Durable, err, log.publishViolations("provision"))
		}
	}
}

// TestTheFixedDurablesMatchTheirReaders holds the operator's names for the
// pre-created durables against the code that binds them. A rename on either
// side would leave the reader waiting for a durable the Job never makes.
func TestTheFixedDurablesMatchTheirReaders(t *testing.T) {
	read := func(path string) string {
		t.Helper()
		b, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		return string(b)
	}
	gateway := read("../../../a2a/gateway/gateway.go")
	relay := read("../../../a2a/gateway/relay.go")
	bridge := read("../../../a2a/hermes-bridge/bridge.go")
	lib := read("../../../a2a/lib/client.go")

	for _, want := range []struct{ src, file, needle string }{
		{gateway, "gateway.go", `const relayDurable = "` + a2aGatewayRelayDurable + `"`},
		{gateway, "gateway.go", `Subjects:      []string{"a2a.tasks.*.*." + lib.TaskClassEvents, "a2a.tasks.*.*." + lib.TaskClassSupervisor},`},
		{gateway, "gateway.go", `MaxAckPending: relayMaxAckPending,`},
		{relay, "relay.go", "const relayMaxAckPending = 8192"},
		{lib, "lib/client.go", `TaskClassEvents     = "events"`},
		{lib, "lib/client.go", `TaskClassSupervisor = "supervisor"`},
		{bridge, "hermes-bridge/bridge.go", `Subject: fmt.Sprintf("a2a.tasks.%s.*.in", b.cfg.Profile),`},
		{bridge, "hermes-bridge/bridge.go", `Durable: "bridge-" + b.cfg.Profile,`},
	} {
		if !strings.Contains(want.src, want.needle) {
			t.Errorf("%s no longer says %q; the pre-created durable and its reader may have parted", want.file, want.needle)
		}
	}
	if a2aGatewayRelayMaxAckPending != 8192 {
		t.Errorf("a2aGatewayRelayMaxAckPending is %d, the relay asks for 8192", a2aGatewayRelayMaxAckPending)
	}
	if a2aGatewayEventsFilter != "a2a.tasks.*.*.events" || a2aGatewaySupervisorFilter != "a2a.tasks.*.*.supervisor" {
		t.Errorf("the relay filters are %q and %q", a2aGatewayEventsFilter, a2aGatewaySupervisorFilter)
	}
	if a2aBridgeIntakeFilter() != "a2a.tasks.platform.*.in" || a2aBridgeDurable() != "bridge-platform" {
		t.Errorf("the bridge intake is %q on %q", a2aBridgeDurable(), a2aBridgeIntakeFilter())
	}
}

// TestProvisionHoldsConsumerGrantsOnlyOnTheFixedDurables: the provision Job
// may read and create the two durables and no other consumer, and may delete
// none. Its grant-row entry allows CONSUMER.INFO and CONSUMER.CREATE on TASKS
// as verbs; this holds the names.
func TestProvisionHoldsConsumerGrantsOnlyOnTheFixedDurables(t *testing.T) {
	var got []string
	for _, g := range provisionIdentity(a2aTestAgent(), a2aTestAgent().Namespace).publish {
		if strings.HasPrefix(g, "$JS.API.CONSUMER.") {
			got = append(got, g)
		}
	}
	want := []string{
		"$JS.API.CONSUMER.INFO.TASKS.gateway-relay",
		"$JS.API.CONSUMER.CREATE.TASKS.gateway-relay",
		"$JS.API.CONSUMER.CREATE.TASKS.gateway-relay.>",
		"$JS.API.CONSUMER.INFO.TASKS.bridge-platform",
		"$JS.API.CONSUMER.CREATE.TASKS.bridge-platform",
		"$JS.API.CONSUMER.CREATE.TASKS.bridge-platform.>",
	}
	if !slices.Equal(got, want) {
		t.Errorf("provision's consumer grants:\n got  %q\n want %q", got, want)
	}
}

// TestTheProvisionScriptCreatesTheFixedDurablesAsTheTestsDo reads the two
// `consumer add` lines back off the rendered script, so the configs the
// server tests pre-create are the ones the Job creates.
func TestTheProvisionScriptCreatesTheFixedDurablesAsTheTestsDo(t *testing.T) {
	script := a2aProvisionScript(a2aTestAgent())
	for _, cfg := range a2aFixedDurableConfigs() {
		info := "$NATS consumer info TASKS " + cfg.Durable + " >/dev/null 2>&1 || $NATS consumer add TASKS " + cfg.Durable + " --pull"
		i := strings.Index(script, info)
		if i < 0 {
			t.Errorf("the script does not create %s create-only", cfg.Durable)
			continue
		}
		line := script[i:]
		if end := strings.Index(line, "--defaults"); end >= 0 {
			line = line[:end]
		}
		filters := cfg.FilterSubjects
		if cfg.FilterSubject != "" {
			filters = []string{cfg.FilterSubject}
		}
		want := []string{"--ack=explicit", "--deliver=all", "--replay=instant", "--max-deliver=-1"}
		for _, f := range filters {
			want = append(want, "--filter='"+f+"'")
		}
		if cfg.MaxAckPending != 0 {
			want = append(want, "--max-pending="+strconv.Itoa(cfg.MaxAckPending))
		} else if strings.Contains(line, "--max-pending") {
			t.Errorf("%s: the script sets --max-pending and the test config does not", cfg.Durable)
		}
		for _, flag := range want {
			if !strings.Contains(line, flag) {
				t.Errorf("%s: the script's consumer add lacks %s:\n%s", cfg.Durable, flag, line)
			}
		}
		if got := strings.Count(line, "--filter="); got != len(filters) {
			t.Errorf("%s: the script passes %d filters, the test config %d", cfg.Durable, got, len(filters))
		}
	}
}

// TestTheFixedTasksDurablesOnARealServer is the proof for #4173 on the
// rendered config: the provision Job's grants create the two durables; the
// gateway and bridge bind them and read; every principal that may create
// consumers on TASKS is refused create, update and delete on both names, the
// refusal read from the server's log; the durables are unchanged after; and
// the readers that still create their own consumers (a replay's ordered
// consumer, a per-task durable) still can.
func TestTheFixedTasksDurablesOnARealServer(t *testing.T) {
	creds := a2aFullCreds("d", "1")
	conf := a2aWithProvisionStandIn(t, string(buildA2ANATSConfigSecret(a2aTestAgent(), creds, a2aTestCalloutKeys(t)).Data["nats.conf"]))
	pw := func(key string) string { return string(creds.Data[key]) }

	s, log := a2aStartRenderedServer(t, conf)
	a2aProvisionLikeTheScript(t, s.ClientURL(), pw("seed-password"))
	a2aPreCreateLikeTheProvisionJob(t, s.ClientURL(), log)
	if v := log.publishViolations("provision"); len(v) != 0 {
		t.Fatalf("pre-creating the durables tripped %d publish violations for provision:\n%s", len(v), strings.Join(v, "\n"))
	}
	// A re-run is info and nothing else.
	a2aPreCreateLikeTheProvisionJob(t, s.ClientURL(), log)

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	_, web := a2aConnectAs(t, s.ClientURL(), "web", pw("web-password"))
	snapshot := func() map[string]jetstream.ConsumerConfig {
		t.Helper()
		out := map[string]jetstream.ConsumerConfig{}
		for _, name := range a2aTasksFixedDurables() {
			c, err := web.Consumer(ctx, a2aTasksStream, name)
			if err != nil {
				t.Fatalf("%s as web: %v", name, err)
			}
			out[name] = c.CachedInfo().Config
		}
		return out
	}
	made := snapshot()

	// The readers bind and read. Binding is CONSUMER.INFO on their own
	// durable, which is all each was granted of INFO on TASKS.
	gateway, gatewayJS := a2aConnectAs(t, s.ClientURL(), "gateway", pw("gateway-password"))
	bridge, bridgeJS := a2aConnectAs(t, s.ClientURL(), "bridge", pw("bridge-password"))
	if _, err := gatewayJS.Publish(ctx, "a2a.tasks.platform.t1.in", []byte(`{"kind":"message"}`)); err != nil {
		t.Fatalf("task submission as gateway: %v", err)
	}
	if _, err := bridgeJS.Publish(ctx, "a2a.tasks.platform.t1.events", []byte(`{"kind":"status-update"}`)); err != nil {
		t.Fatalf("executor event as bridge: %v", err)
	}
	for _, r := range []struct {
		user    string
		js      jetstream.JetStream
		durable string
	}{
		{"gateway", gatewayJS, a2aGatewayRelayDurable},
		{"bridge", bridgeJS, a2aBridgeDurable()},
	} {
		cons, err := r.js.Consumer(ctx, a2aTasksStream, r.durable)
		if err != nil {
			t.Fatalf("%s binding %s: %v\nserver log: %q", r.user, r.durable, err, log.publishViolations(r.user))
		}
		batch, err := cons.Fetch(1, jetstream.FetchMaxWait(5*time.Second))
		if err != nil {
			t.Fatalf("%s pulling %s: %v", r.user, r.durable, err)
		}
		var got jetstream.Msg
		for m := range batch.Messages() {
			got = m
		}
		if got == nil {
			t.Fatalf("%s's pull on %s delivered nothing: %v", r.user, r.durable, batch.Error())
		}
		if err := got.Ack(); err != nil {
			t.Fatalf("%s acking on %s: %v", r.user, r.durable, err)
		}
		t.Logf("%-8s binds %s, pulls and acks", r.user, r.durable)
	}

	// The readers that still create their own consumers on TASKS: the
	// gateway's tasks/get replay (an ordered consumer) and a per-task durable
	// under another name.
	oc, err := gatewayJS.OrderedConsumer(ctx, a2aTasksStream, jetstream.OrderedConsumerConfig{
		FilterSubjects: []string{"a2a.tasks.platform.t1.events"},
	})
	if err != nil {
		t.Fatalf("the gateway's replay consumer: %v", err)
	}
	it, err := oc.Messages()
	if err != nil {
		t.Fatalf("the gateway's replay iterator: %v", err)
	}
	if m, err := it.Next(); err != nil || string(m.Data()) != `{"kind":"status-update"}` {
		t.Fatalf("the gateway's replay read: %v", err)
	}
	it.Stop()
	if _, err := bridgeJS.CreateOrUpdateConsumer(ctx, a2aTasksStream, jetstream.ConsumerConfig{
		Durable: "task-t1", FilterSubject: "a2a.tasks.platform.t1.in", AckPolicy: jetstream.AckExplicitPolicy,
	}); err != nil {
		t.Fatalf("a per-task durable as bridge: %v", err)
	}
	for _, user := range []string{"gateway", "bridge"} {
		if v := log.publishViolations(user); len(v) != 0 {
			t.Errorf("%s's own operations tripped publish violations:\n%s", user, strings.Join(v, "\n"))
		}
	}

	// Refused: create (both subject forms), the older durable create, and
	// delete, on both names, for every principal that may create consumers
	// on TASKS. The body is what a reconfiguring caller would send: a push
	// consumer delivering into the caller's own inbox.
	webNC, _ := a2aConnectAs(t, s.ClientURL(), "web", pw("web-password"))
	consoleNC, _ := a2aConnectAs(t, s.ClientURL(), "console", pw("console-password"))
	mcpNC, _ := a2aConnectAs(t, s.ClientURL(), a2aConsoleMCPUser, pw("console-mcp-password"))
	callers := map[string]requester{
		"gateway":         gateway,
		"bridge":          bridge,
		"web":             webNC,
		"console":         consoleNC,
		a2aConsoleMCPUser: mcpNC,
	}
	users := make([]string, 0, len(callers))
	for u := range callers {
		users = append(users, u)
	}
	slices.Sort(users)
	for _, user := range users {
		nc := callers[user]
		for _, cfg := range a2aFixedDurableConfigs() {
			filterToken := cfg.FilterSubject
			if filterToken == "" {
				filterToken = cfg.FilterSubjects[0]
			}
			body := []byte(`{"stream_name":"TASKS","config":{"durable_name":"` + cfg.Durable + `","name":"` + cfg.Durable +
				`","deliver_subject":"_INBOX.` + user + `.divert","ack_policy":"none","deliver_policy":"all"}}`)
			for _, subject := range []string{
				"$JS.API.CONSUMER.CREATE." + a2aTasksStream + "." + cfg.Durable,
				"$JS.API.CONSUMER.CREATE." + a2aTasksStream + "." + cfg.Durable + "." + filterToken,
				"$JS.API.CONSUMER.DURABLE.CREATE." + a2aTasksStream + "." + cfg.Durable,
				"$JS.API.CONSUMER.DELETE." + a2aTasksStream + "." + cfg.Durable,
			} {
				if msg, err := nc.Request(subject, body, 250*time.Millisecond); err == nil {
					t.Errorf("%-14s %-72s ALLOWED: %s", user, subject, msg.Data)
					continue
				}
				if !log.refusedPublish(user, subject) {
					t.Errorf("%-14s %-72s no reply, but the server logged no publish violation for it", user, subject)
					continue
				}
				t.Logf("%-14s %-72s refused (server: Publish Violation)", user, subject)
			}
		}
	}
	// Each reader holds INFO on its own durable only.
	for _, c := range []struct {
		user    string
		nc      requester
		subject string
	}{
		{"gateway", gateway, "$JS.API.CONSUMER.INFO." + a2aTasksStream + "." + a2aBridgeDurable()},
		{"bridge", bridge, "$JS.API.CONSUMER.INFO." + a2aTasksStream + "." + a2aGatewayRelayDurable},
	} {
		if _, err := c.nc.Request(c.subject, nil, 250*time.Millisecond); err == nil || !log.refusedPublish(c.user, c.subject) {
			t.Errorf("%s %s was not refused (%v)", c.user, c.subject, err)
		}
	}
	// And the provision Job creates them but may not delete them.
	provisionNC, _ := a2aConnectAs(t, s.ClientURL(), "provision", a2aProvisionStandInPW)
	for _, name := range a2aTasksFixedDurables() {
		subject := "$JS.API.CONSUMER.DELETE." + a2aTasksStream + "." + name
		if _, err := provisionNC.Request(subject, nil, 250*time.Millisecond); err == nil || !log.refusedPublish("provision", subject) {
			t.Errorf("provision %s was not refused (%v)", subject, err)
		}
	}

	// Nothing underneath changed.
	after := snapshot()
	for _, name := range a2aTasksFixedDurables() {
		b, a := made[name], after[name]
		if a.DeliverSubject != "" || !slices.Equal(a.FilterSubjects, b.FilterSubjects) || a.FilterSubject != b.FilterSubject ||
			a.AckPolicy != b.AckPolicy || a.MaxAckPending != b.MaxAckPending {
			t.Errorf("%s changed under the refused calls:\nbefore %+v\nafter  %+v", name, b, a)
		}
	}
}
