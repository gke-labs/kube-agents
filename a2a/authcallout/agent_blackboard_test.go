package authcallout

import (
	"context"
	"log/slog"
	"os"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The `agent` principal's blackboard grants, measured against a real server
// rather than read out of the render.
//
// This is the measurement A5 deleted. Before the split, one static `worker`
// credential carried both the bridge's task execution and the CLI's topic
// reads, and the operator's own TestWorkerJetStreamGrantOnARealServer walked
// the second half on an embedded server: STREAM.INFO on each topic stream,
// STREAM.INFO with the registry's subject filter, a publish, and a
// last-message read. A5 moved those grants
// from `worker` to `agent` — a callout principal — and the operator test, now
// TestBridgeJetStreamGrantOnARealServer, replaced that loop with refusal rows
// proving the *bridge* no longer has them. Nothing was left proving the agent
// does. That is the wrong half to keep: a refusal table stays green when a
// grant list is emptied, and the whole point of the split was that the
// blackboard keeps working under a narrower credential.
//
// It cannot go back where it came from. The operator module has no replace
// directive for the a2a module, so the callout service cannot run inside
// k8s-operator's tests, and `agent` is not in the rendered nats.conf's
// auth_users — there is no static `agent` password to connect with. The
// principal only exists once a callout answers for it, which is exactly what
// this package already stands up: the operator's real rendered nats.conf, the
// real Service, and testdata/rendered-identity-map.json, which is the
// operator's real render of a2aAgentJetStreamGrants() held byte-equal to the
// live render by TestRenderedAuthMap next door. So a grant deleted from
// a2aAgentJetStreamGrants fails that golden test first, and after a
// `-update` it fails here, naming the operation the server refused.
//
// Everything below goes through lib.Client — TopicRegistry, PublishTopic,
// ReadTopicLatest — and not through hand-rolled jetstream calls, because the
// grant has to match the shape the library asks in, not the shape a test
// author would. That distinction has already cost this project once: ordered
// consumers were granted in spirit and unnameable in fact. AllowDirect on the
// streams is the same trap in miniature — nats.go picks DIRECT.GET or
// STREAM.MSG.GET from the stream's own config, and only one of the two is in
// the grant.

const (
	// The provision Job's identity, which this test needs only to create the
	// two topic streams the way its script creates them. The fixture maps
	// this ServiceAccount to the `provision` user.
	renderedProvisionSA    = "system:serviceaccount:kubeagents-system:" + provisionSAName
	renderedProvisionToken = "token-for-the-provision-serviceaccount-padded-to-a-realistic"
)

// The four topic subjects the provision script's `stream add` lines name. The
// first three are the agent's; the fourth, shared.probe, is provisioned with
// no writer on purpose (see the script's comment) and is the refusal row below
// that proves a publish grant rather than the existence of a subject.
const (
	topicUpgradeReadiness = "a2a.topics.agent.platform.upgrade-readiness"
	topicBlueprint        = "a2a.topics.shared.blueprint"
	topicProbe            = "a2a.topics.shared.probe"
	topicAnnotations      = "a2a.topics.shared.annotations"
)

func TestTheAgentPrincipalWorksTheBlackboardOnARealServer(t *testing.T) {
	raw, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatalf("reading the operator's rendered identity map: %v", err)
	}
	h := startHarness(t, string(raw), map[string]Attested{
		renderedProvisionToken: {ServiceAccount: renderedProvisionSA},
		agentToken:             {ServiceAccount: agentSA},
	})
	// The server's own log, so a refusal is read where an operator would read
	// it. The rendered config sets no log level and nats-server reports
	// permission violations at error level, so nothing but the logger changes.
	vl := &violationLog{lines: make(chan string, 256)}
	h.server.SetLoggerV2(vl, false, false, false)

	// A refused JetStream request is not an error, it is a reply that never
	// comes, so this deadline is how long a missing grant takes to diagnose.
	// Every operation below is milliseconds against a local server.
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	provisionTopicStreams(t, h)

	// The client's own log, captured. A refused JetStream publish does come
	// back from PublishTopic, as a timeout on the API reply rather than as a
	// permission error, so the arms below check the returned error too. What
	// this adds is the second channel: lib owns nats.ErrorHandler, and a
	// permission violation the server attributes to this connection lands
	// there even when the operation it broke reported success — a refused
	// subscription, or an inbox the grants do not cover.
	clientLog := &safeBuffer{}
	client, err := lib.Connect(ctx, h.url,
		lib.WithName("a2a-cli-topics"),
		lib.WithLogger(slog.New(slog.NewTextHandler(clientLog, &slog.HandlerOptions{Level: slog.LevelDebug}))),
		// Exactly what `a2a` does in the agent container: the projected token
		// from a file, and the inbox prefix the agent's grant covers. Without
		// the prefix every JetStream call below would time out instead of
		// being refused, which is the failure mode that reads like an outage.
		lib.WithKSAToken(tokenFile(t, agentToken), "agent"),
	)
	if err != nil {
		t.Fatalf("the agent principal could not connect with its projected token: %v", err)
	}
	t.Cleanup(client.Close)

	allowed := func(op string, err error) {
		t.Helper()
		if err != nil {
			t.Fatalf("%-72s REFUSED: %v\nserver violations: %q\nEach line is a subject a2aAgentJetStreamGrants (or the agent's publish list) is missing.",
				op, err, vl.violationsSoFar())
		}
		t.Logf("%-72s allowed", op)
	}

	// 1. The registry read: STREAM.INFO on both topic streams, then a second
	// STREAM.INFO carrying the a2a.topics.> subject filter. Asserting on the
	// subjects rather than on err is not decoration — TopicRegistry joins its
	// per-stream errors and keeps going, so a principal refused STREAM.INFO on
	// TOPICS-JOURNAL gets a nil error and half a registry. That is the shape
	// a missing grant takes here, and `err == nil` would not see it.
	registry, err := client.TopicRegistry(ctx)
	allowed("TopicRegistry: STREAM.INFO + subject filter on both topic streams", err)
	var got []string
	for _, e := range registry {
		got = append(got, e.Subject)
	}
	sort.Strings(got)
	want := []string{topicUpgradeReadiness, topicBlueprint, topicProbe, topicAnnotations}
	sort.Strings(want)
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("the registry the agent can see is\n  %v\nwant\n  %v\n(a short registry means STREAM.INFO was refused on a stream and TopicRegistry swallowed it)\nserver violations: %q",
			got, want, vl.violationsSoFar())
	}
	for _, e := range registry {
		switch e.Subject {
		case topicAnnotations:
			if e.Stream != lib.StreamTopicsJournal || e.Class != "journal" {
				t.Errorf("%s resolved to %s/%s, want the journal stream", e.Subject, e.Stream, e.Class)
			}
		default:
			if e.Stream != lib.StreamTopicsState || e.Class != "state" {
				t.Errorf("%s resolved to %s/%s, want the state stream", e.Subject, e.Stream, e.Class)
			}
		}
	}

	// 2 and 3. Write then read back every topic the agent owns, by the names
	// the CLI resolves — one row per subject, because the publish grants are
	// three concrete subjects and not a wildcard, so a subject dropped from
	// the list is invisible in any other row.
	for _, name := range []string{
		"agent.platform.upgrade-readiness",
		"shared.blueprint",
		"shared.annotations",
	} {
		entry, err := lib.ResolveTopic(registry, name)
		if err != nil {
			t.Fatalf("resolving %q against the registry the agent read: %v", name, err)
		}
		artifact, err := lib.NewTopicArtifact(entry.Topic, "written by the agent principal", map[string]string{"by": "agent"})
		if err != nil {
			t.Fatalf("building the topic artifact for %s: %v", entry.Subject, err)
		}
		allowed("PublishTopic "+entry.Subject,
			client.PublishTopic(ctx, entry.Subject,
				lib.Party{Session: "agent", Profile: "platform"},
				"", "", "corr-"+entry.Topic, artifact))

		// The read is the half the JetStream grant carries: a direct
		// last-message-for-subject on the holding stream. It also closes the
		// publish above: PublishTopic goes through JetStream and does report a
		// refusal, but as a timeout on the API reply rather than as a
		// permission error, so the allowed() check alone says the reply
		// arrived and not that the entry is on the stream. Reading it back
		// says that.
		env, err := client.ReadTopicLatest(ctx, entry.Stream, entry.Subject)
		allowed("ReadTopicLatest "+entry.Subject+" ($JS.API.DIRECT.GET."+entry.Stream+")", err)
		if env == nil || env.Kind != lib.KindTopicUpdate {
			t.Fatalf("read back %+v from %s, want the topic-update just written", env, entry.Subject)
		}
		if !strings.Contains(string(env.Payload), entry.Topic) {
			t.Errorf("the entry read back from %s does not carry the topic name: %s", entry.Subject, env.Payload)
		}
	}

	// Nothing above tripped the server, and nothing above tripped the client's
	// async handler. Both halves: the server log catches a refusal on an
	// operation whose error the library swallowed, and the client log catches
	// one the server logged under a different client.
	if v := vl.violationsSoFar(); len(v) != 0 {
		t.Errorf("the agent's own blackboard operations tripped %d permission violations; each is a grant the render is missing:\n%s",
			len(v), strings.Join(v, "\n"))
	}
	if l := clientLog.String(); strings.Contains(l, "Violation") {
		t.Errorf("the client logged a permission violation during operations that all reported success:\n%s", l)
	}

	// 4. The other direction, so that a future "fix" for a refusal above
	// cannot be $JS.API.> or a2a.topics.>. Every row is refused today and the
	// refusal is read from the server's log, not from the client's timeout.
	//
	// shared.probe is the interesting one: it is a real provisioned subject on
	// a stream the agent may read, with no writer by design, so its refusal
	// proves the publish grant rather than proving the subject does not exist.
	nc, violations := h.connectAs(t, "agent", agentToken)
	for _, c := range []struct{ op, subject string }{
		{"write the writerless probe subject", topicProbe},
		{"reshape the topic registry", "$JS.API.STREAM.CREATE.TOPICS-STATE"},
		{"drain the blackboard through a consumer", "$JS.API.CONSUMER.CREATE.TOPICS-STATE.peek"},
		{"delete a blackboard entry", "$JS.API.STREAM.MSG.DELETE.TOPICS-STATE"},
		{"look at the task plane", "$JS.API.STREAM.INFO.TASKS"},
		{"read a task directly", "$JS.API.DIRECT.GET.TASKS.a2a.tasks.platform.t1.in"},
		{"enumerate the account's streams", "$JS.API.STREAM.NAMES"},
	} {
		if !publishRefused(t, nc, violations, c.subject) {
			t.Errorf("%-40s ALLOWED on %s; the agent credential reaches past the blackboard", c.op, c.subject)
			continue
		}
		if !vl.sawViolationFor(c.subject) {
			t.Errorf("%-40s the client saw a violation on %s but the server logged none naming it", c.op, c.subject)
		}
	}
}

// provisionTopicStreams creates the two topic streams as the provision Job's
// own principal, with the flags a2aProvisionScript passes to natscli. Not as a
// privileged test fixture: the create has to go through the callout too, so
// the streams this test reads are ones the deployment's own provisioner could
// have made, and AllowDirect — which decides whether a last-message read is a
// DIRECT.GET or a STREAM.MSG.GET, and so which of the two the grant must
// name — is set here because the script sets it, not because the test needs it.
func provisionTopicStreams(t *testing.T, h *harness) {
	t.Helper()
	nc, _ := h.connectAs(t, "provision", renderedProvisionToken)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	for _, cfg := range []jetstream.StreamConfig{{
		Name:              lib.StreamTopicsState,
		Subjects:          []string{topicUpgradeReadiness, topicBlueprint, topicProbe},
		Storage:           jetstream.FileStorage,
		Retention:         jetstream.LimitsPolicy,
		MaxMsgsPerSubject: 8,
		AllowDirect:       true,
	}, {
		Name:        lib.StreamTopicsJournal,
		Subjects:    []string{topicAnnotations},
		Storage:     jetstream.FileStorage,
		Retention:   jetstream.LimitsPolicy,
		MaxAge:      720 * time.Hour,
		AllowDirect: true,
	}} {
		if _, err := js.CreateStream(ctx, cfg); err != nil {
			t.Fatalf("provisioning %s as the provision principal: %v", cfg.Name, err)
		}
	}
}

// violationsSoFar drains whatever the server has logged up to now and returns
// the permission-violation lines. Unlike sawViolationFor it never waits: it is
// used to assert an absence, and a wait there would only slow a passing run.
func (vl *violationLog) violationsSoFar() []string {
	var out []string
	for {
		select {
		case line := <-vl.lines:
			if strings.Contains(line, "Violation") {
				out = append(out, line)
			}
		default:
			return out
		}
	}
}
