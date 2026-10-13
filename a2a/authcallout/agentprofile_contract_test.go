package authcallout

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The operator's render with AgentProfiles in it, against this package. "Profile"
// is the AgentProfile resource, not a Hermes profile directory.
//
// The operator writes three fixtures from its real render
// (k8s-operator/internal/controller/agentprofile_test.go, -update): the identity
// map with the operator's own principal and two profiles, and the agent card and
// tombstone it publishes. The operator cannot import lib, so these are the only
// thing holding its hand-built envelope and its map entries to what this side
// parses and enforces.

const (
	profilesFixturePath    = "testdata/rendered-identity-map-profiles.json"
	agentCardFixturePath   = "testdata/rendered-agent-card.json"
	agentClosedFixturePath = "testdata/rendered-agent-closed.json"

	renderedOperatorSA = "system:serviceaccount:kubeagents-operator:kubeagents-controller-manager"
	renderedAuditorSA  = "system:serviceaccount:kubeagents-system:agentprofile-auditor"

	tokenRenderedOperator = "token-for-the-operator-serviceaccount-padded-to-a-realistic-len"
	tokenRenderedAuditor  = "token-for-the-rendered-auditor-profile-sa-bound-to-pod-padding"
	renderedAuditorPod    = "auditor-task-9-k3j8w"

	renderedSeedPassword = "pw-seed"
	cardProfile          = "auditor"
)

func renderedProfilesMap(t *testing.T) string {
	t.Helper()
	raw, err := os.ReadFile(profilesFixturePath)
	if err != nil {
		t.Fatalf("reading the operator's rendered profiles map: %v\nRegenerate with: go test ./internal/controller/ -run 'Fixture' -update (in k8s-operator)", err)
	}
	return string(raw)
}

// The map parses, and carries exactly the principals the operator should
// render once it knows its own ServiceAccount and two profiles exist.
func TestTheOperatorsProfilesMapParses(t *testing.T) {
	m, err := ParseIdentityMap([]byte(renderedProfilesMap(t)))
	if err != nil {
		t.Fatalf("the operator's rendered profiles map does not parse: %v", err)
	}
	want := map[string]string{
		"provision": "", "session": NarrowingPod, "agent": "", "verifier": "",
		"operator": "", "profile-auditor": NarrowingProfile, "profile-cluster-a": NarrowingProfile,
	}
	for _, id := range m.Identities {
		narrowing, ok := want[id.User]
		if !ok {
			t.Errorf("the operator renders a principal this package did not expect: %q", id.User)
			continue
		}
		if id.Narrowing != narrowing {
			t.Errorf("%q narrows on %q, want %q", id.User, id.Narrowing, narrowing)
		}
		delete(want, id.User)
	}
	for user := range want {
		t.Errorf("the operator no longer renders %q", user)
	}
}

// The rendered operator principal publishes and reads the directory, and is
// refused everything else, against a real server running the rendered
// nats.conf and the rendered map.
func TestTheRenderedOperatorReachesTheDirectoryAndNothingElse(t *testing.T) {
	h := startHarness(t, renderedProfilesMap(t), map[string]Attested{
		tokenRenderedOperator: {ServiceAccount: renderedOperatorSA},
		tokenRenderedAuditor:  {ServiceAccount: renderedAuditorSA, PodName: renderedAuditorPod, PodUID: "uid-ra"},
	})
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	// The directory stream as the provisioner creates it: last-value per
	// subject, direct gets allowed.
	seed, _ := connectStatic(t, h, "seed", renderedSeedPassword)
	seedJS, err := jetstream.New(seed)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := seedJS.CreateStream(ctx, jetstream.StreamConfig{
		Name: "DIRECTORY", Subjects: []string{"a2a.agents.>"}, MaxMsgsPerSubject: 1, AllowDirect: true,
	}); err != nil {
		t.Fatalf("creating DIRECTORY as seed: %v", err)
	}

	op, violations := h.connectAs(t, "operator", tokenRenderedOperator)
	card, err := os.ReadFile(agentCardFixturePath)
	if err != nil {
		t.Fatal(err)
	}
	js, err := jetstream.New(op)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := js.Publish(ctx, "a2a.agents."+cardProfile, card); err != nil {
		t.Fatalf("the operator could not publish a card under its rendered grants: %v", err)
	}
	msg, err := op.RequestWithContext(ctx, "$JS.API.DIRECT.GET.DIRECTORY.a2a.agents."+cardProfile, nil)
	if err != nil {
		t.Fatalf("the operator could not read the card back: %v", err)
	}
	if status := msg.Header.Get("Status"); status != "" || string(msg.Data) != string(card) {
		t.Fatalf("the direct get returned status %q and %q; want the card it published", status, msg.Data)
	}

	checkPublish(t, op, violations, map[string]bool{
		"a2a.agents.another-profile":                false,
		lib.TaskInSubject(cardProfile, "t"):         true,
		lib.TaskEventsSubject(cardProfile, "t"):     true,
		lib.TaskSupervisorSubject(cardProfile, "t"): true,
		"a2a.topics.shared.blueprint":               true,
		"$JS.API.STREAM.INFO.DIRECTORY":             true,
		"$JS.API.STREAM.DELETE.DIRECTORY":           true,
		"$JS.API.STREAM.PURGE.DIRECTORY":            true,
		"$JS.API.STREAM.MSG.GET.DIRECTORY":          true,
		"$JS.API.CONSUMER.CREATE.DIRECTORY.x":       true,
		"$JS.API.DIRECT.GET.TASKS.a2a.tasks.x.y.in": true,
		"$JS.API.DIRECT.GET.DIRECTORY.a2a.tasks.x":  true,
		"$KV.cap.root.x":                            true,
		"a2a.cap.verify.operator":                   true,
		"_INBOX.gateway.x":                          true,
	})
	for _, subject := range []string{"a2a.agents.>", "a2a.tasks.>", ">"} {
		if !subscribeRefused(t, op, violations, subject) {
			t.Errorf("the operator may subscribe to %s; it reads the directory by direct get only", subject)
		}
	}

	// And the profile pod from the same render: its own events, never the
	// directory.
	pod, podViolations := h.connectAs(t, renderedAuditorPod, tokenRenderedAuditor)
	checkPublish(t, pod, podViolations, map[string]bool{
		lib.TaskEventsSubject(cardProfile, "t"): false,
		"a2a.agents." + cardProfile:             true,
		"a2a.topics.agent.auditor.findings":     false,
		"a2a.topics.shared.annotations":         true,
	})
}

// The card and tombstone the operator renders are envelopes this library
// accepts for emit, and agree with their directory subject and no other.
func TestTheOperatorsCardFixturesAreDirectoryEnvelopes(t *testing.T) {
	for path, kind := range map[string]lib.Kind{agentCardFixturePath: lib.KindAgentCard, agentClosedFixturePath: lib.KindAgentClosed} {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		env, err := lib.ParseEnvelope(raw)
		if err != nil {
			t.Fatalf("%s does not parse as an envelope: %v", path, err)
		}
		if err := env.ValidateEmit(); err != nil {
			t.Errorf("%s is not an envelope this library would emit: %v", path, err)
		}
		if env.Kind != kind {
			t.Errorf("%s has kind %q, want %q", path, env.Kind, kind)
		}
		if err := lib.CheckSubjectAgreement("a2a.agents."+cardProfile, env, lib.AgreementPolicy{}); err != nil {
			t.Errorf("%s disagrees with its own directory subject: %v", path, err)
		}
		if err := lib.CheckSubjectAgreement("a2a.agents.someone-else", env, lib.AgreementPolicy{}); err == nil || !strings.Contains(err.Error(), "profile") {
			t.Errorf("%s agrees with another profile's directory subject: %v", path, err)
		}
	}
}
