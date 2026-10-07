package authcallout

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// Profile narrowing, against a real nats-server enforcing a real minted JWT,
// the same way session_integration_test.go proves session narrowing. The
// property: every pod of one AgentProfile publishes as the profile, and no pod
// reaches another pod's consumers or inbox, another profile's or a session's
// task plane, or a topic its profile did not name.

const (
	auditorSA = "system:serviceaccount:kubeagents-system:agentprofile-auditor"
	clusterSA = "system:serviceaccount:kubeagents-system:agentprofile-cluster-a"

	auditor = "auditor"
	cluster = "cluster-a"

	auditorPod1 = "auditor-task-1-x7k2p"
	auditorPod2 = "auditor-task-2-q9m4z"
	clusterPod  = "cluster-a-task-3-b2c4d"

	tokenAuditorPod1 = "token-for-the-auditor-profile-sa-bound-to-pod-one-padded-out"
	tokenAuditorPod2 = "token-for-the-auditor-profile-sa-bound-to-pod-two-padded-out"
	tokenAuditorNone = "token-for-the-auditor-profile-sa-bound-to-no-pod-padded-out"
	tokenAuditorDots = "token-for-the-auditor-profile-sa-pod-name-has-dots-padded-ok"
	tokenClusterPod  = "token-for-the-cluster-a-profile-sa-bound-to-its-pod-padded-o"

	auditorPublishTopic   = "agent.auditor.findings"
	auditorSubscribeTopic = "shared.blueprint"
)

// profileMap carries the stage-prop gateway from sessionMap (it creates the
// streams), the session entry, and two profile entries: one with topics, one
// with none (the cluster agent's shape, `bus: {}`).
const profileMap = `{
  "version": "profile-itest-1",
  "identities": [
    {
      "serviceAccount": "system:serviceaccount:kubeagents-system:agent-a2a-gateway",
      "user": "gateway",
      "account": "APP",
      "grants": {
        "publish": ["a2a.tasks.>", "$JS.API.>", "_INBOX.gateway.>"],
        "subscribe": ["a2a.tasks.>", "_INBOX.gateway.>"]
      }
    },
    {
      "serviceAccount": "system:serviceaccount:kubeagents-system:agent-a2a-session",
      "user": "session",
      "account": "APP",
      "narrowing": "pod",
      "grants": {"publish": [], "subscribe": []}
    },
    {
      "serviceAccount": "system:serviceaccount:kubeagents-system:agentprofile-auditor",
      "user": "profile-auditor",
      "account": "APP",
      "narrowing": "profile",
      "profile": "auditor",
      "topics": {"publish": ["agent.auditor.findings"], "subscribe": ["shared.blueprint"]},
      "grants": {"publish": [], "subscribe": []}
    },
    {
      "serviceAccount": "system:serviceaccount:kubeagents-system:agentprofile-cluster-a",
      "user": "profile-cluster-a",
      "account": "APP",
      "narrowing": "profile",
      "profile": "cluster-a",
      "grants": {"publish": [], "subscribe": []}
    }
  ]
}`

func profileTokens() map[string]Attested {
	return map[string]Attested{
		gatewayToken:     {ServiceAccount: "system:serviceaccount:kubeagents-system:agent-a2a-gateway"},
		tokenPodA:        {ServiceAccount: sessionSA, PodName: podA, PodUID: "uid-a"},
		tokenAuditorPod1: {ServiceAccount: auditorSA, PodName: auditorPod1, PodUID: "uid-p1"},
		tokenAuditorPod2: {ServiceAccount: auditorSA, PodName: auditorPod2, PodUID: "uid-p2"},
		tokenAuditorNone: {ServiceAccount: auditorSA},
		tokenAuditorDots: {ServiceAccount: auditorSA, PodName: "auditor.task.1", PodUID: "uid-pd"},
		tokenClusterPod:  {ServiceAccount: clusterSA, PodName: clusterPod, PodUID: "uid-c"},
	}
}

// Everything the worker adapter does for a profile task works: it publishes
// as the profile, creates its pod-named consumers filtered on the profile's
// subjects, and reads and writes exactly its profile's topics.
func TestAProfilePodReachesEverythingItsOwnWorkNeeds(t *testing.T) {
	h := startHarness(t, profileMap, profileTokens())
	nc, violations := h.connectAs(t, auditorPod1, tokenAuditorPod1)

	checkPublish(t, nc, violations, map[string]bool{
		lib.TaskEventsSubject(auditor, "task-1"): false,

		consumerSubject("CREATE", lib.SessionConsumerName(auditorPod1, lib.SessionConsumerOrigin)) + "." + lib.TaskInSubject(auditor, "*"):     false,
		consumerSubject("CREATE", lib.SessionConsumerName(auditorPod1, lib.SessionConsumerIn)) + "." + lib.TaskInSubject(auditor, "*"):         false,
		consumerSubject("CREATE", lib.SessionConsumerName(auditorPod1, lib.SessionConsumerEvents)) + "." + lib.TaskEventsSubject(auditor, "*"): false,
		consumerSubject("MSG.NEXT", lib.SessionConsumerName(auditorPod1, lib.SessionConsumerIn)):                                               false,
		consumerSubject("INFO", lib.SessionConsumerName(auditorPod1, lib.SessionConsumerOrigin)):                                               false,
		consumerSubject("DELETE", lib.SessionConsumerName(auditorPod1, lib.SessionConsumerEvents)):                                             false,

		"a2a.topics." + auditorPublishTopic:                                                      false,
		"$JS.API.STREAM.INFO." + lib.StreamTopicsState:                                           false,
		"$JS.API.STREAM.INFO." + lib.StreamTopicsJournal:                                         false,
		"$JS.API.DIRECT.GET." + lib.StreamTopicsState + ".a2a.topics." + auditorSubscribeTopic:   false,
		"$JS.API.DIRECT.GET." + lib.StreamTopicsJournal + ".a2a.topics." + auditorSubscribeTopic: false,

		"_INBOX." + auditorPod1 + ".reply-1": false,
	})

	for _, subject := range []string{"_INBOX." + auditorPod1 + ".>", "a2a.topics." + auditorSubscribeTopic} {
		if subscribeRefused(t, nc, violations, subject) {
			t.Errorf("subscribe %s was refused; a profile pod needs its inbox and its subscribed topics", subject)
		}
	}
}

// The refusals, one row per plane a profile pod must not reach.
func TestAProfilePodIsRefusedEverythingBeyondItsProfile(t *testing.T) {
	h := startHarness(t, profileMap, profileTokens())
	nc, violations := h.connectAs(t, auditorPod1, tokenAuditorPod1)

	checkPublish(t, nc, violations, map[string]bool{
		// Another profile's and a session's task plane.
		lib.TaskEventsSubject(cluster, "task-3"): true,
		lib.TaskEventsSubject(podA, "task-a"):    true,

		// Its own profile's input and supervisor subjects: the requester
		// writes the first, the janitor the second.
		lib.TaskInSubject(auditor, "task-1"):         true,
		lib.TaskSupervisorSubject(auditor, "task-1"): true,

		// Its sibling pod's consumers and inbox: same profile, same
		// ServiceAccount, different pod.
		consumerSubject("MSG.NEXT", lib.SessionConsumerName(auditorPod2, lib.SessionConsumerIn)): true,
		consumerSubject("DELETE", lib.SessionConsumerName(auditorPod2, lib.SessionConsumerIn)):   true,
		"_INBOX." + auditorPod2 + ".reply-1":                                                     true,

		// A consumer named for the profile rather than the pod: the shape a
		// static entry would have had to grant, shared by every pod.
		consumerSubject("CREATE", lib.SessionConsumerName(auditor, lib.SessionConsumerIn)) + "." + lib.TaskInSubject(auditor, "*"): true,

		// The gateway's durable and the stream-level reads that are not
		// subject-scoped.
		consumerSubject("MSG.NEXT", relayDurable): true,
		consumerSubject("DELETE", relayDurable):   true,
		"$JS.API.STREAM.INFO.TASKS":               true,
		"$JS.API.STREAM.MSG.GET.TASKS":            true,
		"$JS.API.DIRECT.GET.TASKS":                true,
		"$JS.API.STREAM.DELETE.TASKS":             true,

		// Topics it was not granted, and reads of them.
		"a2a.topics." + auditorSubscribeTopic:                                            true,
		"a2a.topics.shared.annotations":                                                  true,
		"$JS.API.DIRECT.GET." + lib.StreamTopicsState + ".a2a.topics.shared.annotations": true,
		"$JS.API.DIRECT.GET." + lib.StreamTopicsState + ".>":                             true,

		// The directory: the operator publishes cards, never an agent.
		"a2a.agents." + auditor: true,

		// The capability path, which sessions hold and profile pods do not
		// (yet): see profile_narrowing.go.
		"a2a.cap.verify." + auditorPod1: true,
	})

	for _, subject := range []string{"a2a.tasks.>", lib.TaskInSubject(auditor, "*"), "_INBOX." + auditorPod2 + ".>", "a2a.topics.>", ">"} {
		if !subscribeRefused(t, nc, violations, subject) {
			t.Errorf("subscribe %s was allowed; a profile pod subscribes to its inbox and its topics only", subject)
		}
	}
}

// The consumer filter rides the CREATE subject, so a pod's own consumer name
// cannot be pointed at another addressee's subjects.
func TestAProfilePodCannotFilterItsOwnConsumerOntoAnotherAddressee(t *testing.T) {
	h := startHarness(t, profileMap, profileTokens())
	nc, violations := h.connectAs(t, auditorPod1, tokenAuditorPod1)

	own := lib.SessionConsumerName(auditorPod1, lib.SessionConsumerIn)
	checkPublish(t, nc, violations, map[string]bool{
		consumerSubject("CREATE", own) + "." + lib.TaskInSubject(cluster, "*"): true,
		consumerSubject("CREATE", own) + "." + lib.TaskInSubject(podA, "*"):    true,
		consumerSubject("CREATE", own) + ".a2a.tasks.>":                        true,
		consumerSubject("CREATE", own) + "." + lib.TaskInSubject(auditor, "t"): false,
	})
}

// Two pods of one profile both publish as the profile, and each is refused the
// other's consumers: the collision the narrowing exists to prevent.
func TestTwoPodsOfOneProfileShareTheAddresseeAndNothingElse(t *testing.T) {
	h := startHarness(t, profileMap, profileTokens())
	a, aViolations := h.connectAs(t, auditorPod1, tokenAuditorPod1)
	b, bViolations := h.connectAs(t, auditorPod2, tokenAuditorPod2)

	checkPublish(t, a, aViolations, map[string]bool{
		lib.TaskEventsSubject(auditor, "t1"):                                                     false,
		consumerSubject("INFO", lib.SessionConsumerName(auditorPod1, lib.SessionConsumerOrigin)): false,
		consumerSubject("INFO", lib.SessionConsumerName(auditorPod2, lib.SessionConsumerOrigin)): true,
	})
	checkPublish(t, b, bViolations, map[string]bool{
		lib.TaskEventsSubject(auditor, "t2"):                                                     false,
		consumerSubject("INFO", lib.SessionConsumerName(auditorPod2, lib.SessionConsumerOrigin)): false,
		consumerSubject("INFO", lib.SessionConsumerName(auditorPod1, lib.SessionConsumerOrigin)): true,
	})
}

// A profile with no topics (`bus: {}`) gets the task plane and nothing on the
// blackboard: no topic subject either way, and not the topic-stream info
// grant that any profile with a topic gets. Both sides are still non-empty, which is
// what keeps the mint from reading an empty side as unrestricted.
func TestAProfileWithNoTopicsReachesNoTopic(t *testing.T) {
	g, err := profileGrants(cluster, clusterPod, TopicGrants{})
	if err != nil {
		t.Fatalf("profileGrants: %v", err)
	}
	if len(g.Publish) == 0 || len(g.Subscribe) == 0 {
		t.Fatalf("a no-topics profile derived %d publish and %d subscribe grants; an empty side mints as unrestricted", len(g.Publish), len(g.Subscribe))
	}
	for _, s := range append(append([]string{}, g.Publish...), g.Subscribe...) {
		if strings.Contains(s, "a2a.topics") || strings.Contains(s, "TOPICS") {
			t.Errorf("a no-topics profile was granted %q", s)
		}
	}

	h := startHarness(t, profileMap, profileTokens())
	nc, violations := h.connectAs(t, clusterPod, tokenClusterPod)
	checkPublish(t, nc, violations, map[string]bool{
		lib.TaskEventsSubject(cluster, "task-3"):             false,
		"a2a.topics.shared.blueprint":                        true,
		"a2a.topics.agent.cluster-a.findings":                true,
		"$JS.API.STREAM.INFO." + lib.StreamTopicsState:       true,
		"$JS.API.DIRECT.GET." + lib.StreamTopicsState + ".>": true,
		"$JS.API.STREAM.CREATE." + lib.StreamTopicsState:     true,
		"a2a.agents." + cluster:                              true,
		"anything.at.all":                                    true,
	})
	if !subscribeRefused(t, nc, violations, "a2a.topics.>") {
		t.Error("a no-topics profile pod may subscribe to the blackboard")
	}
}

// The pod attestation is required exactly as it is for a session: the pod name
// becomes consumer names and an inbox.
func TestAProfileTokenWithoutAUsablePodIsRefusedAtConnect(t *testing.T) {
	h := startHarness(t, profileMap, profileTokens())
	for name, token := range map[string]string{"no pod": tokenAuditorNone, "dotted pod": tokenAuditorDots} {
		if nc, err := nats.Connect(h.url, nats.Token(token), nats.CustomInboxPrefix("_INBOX."+auditorPod1)); err == nil {
			nc.Close()
			t.Errorf("%s: connected; a profile entry must refuse a token whose pod it cannot use", name)
		}
	}
}

func TestTheMapRefusesProfileEntriesItCannotServe(t *testing.T) {
	entry := func(extra string) string {
		return `{"version":"v","identities":[{"serviceAccount":"` + auditorSA + `","user":"profile-auditor","account":"APP",` + extra + `}]}`
	}
	cases := map[string]string{
		"grants on a profile entry": `"narrowing":"profile","profile":"auditor","grants":{"publish":["a2a.tasks.>"],"subscribe":["_INBOX.x.>"]}`,
		"no profile":                `"narrowing":"profile","grants":{}`,
		"dotted profile":            `"narrowing":"profile","profile":"a.b","grants":{}`,
		"wildcard profile":          `"narrowing":"profile","profile":"*","grants":{}`,
		"non-topic publish grant":   `"narrowing":"profile","profile":"auditor","topics":{"publish":["tasks.x"]},"grants":{}`,
		"wildcard topic grant":      `"narrowing":"profile","profile":"auditor","topics":{"subscribe":["shared.*"]},"grants":{}`,
		"prefixed topic grant":      `"narrowing":"profile","profile":"auditor","topics":{"subscribe":["a2a.topics.shared.blueprint"]},"grants":{}`,
		"profile on a pod entry":    `"narrowing":"pod","profile":"auditor","grants":{}`,
		"topics on a static entry":  `"topics":{"publish":["shared.blueprint"]},"grants":{"publish":["x"],"subscribe":["y"]}`,
		"profile on a static entry": `"profile":"auditor","grants":{"publish":["x"],"subscribe":["y"]}`,
	}
	for name, extra := range cases {
		if _, err := ParseIdentityMap([]byte(entry(extra))); err == nil {
			t.Errorf("%s: the map parsed; want it refused", name)
		}
	}
	if _, err := ParseIdentityMap([]byte(entry(`"narrowing":"profile","profile":"auditor","topics":{"publish":["agent.auditor.findings"],"subscribe":["shared.blueprint"]},"grants":{}`))); err != nil {
		t.Errorf("a well-formed profile entry was refused: %v", err)
	}
}

// The mint repeats the topic check, so a grant set that reached profileGrants
// without passing the map cannot mint a non-topic subject.
func TestProfileGrantsRefusesANonTopicRatherThanMintingIt(t *testing.T) {
	for _, bad := range []string{">", "a2a.tasks.>", "shared.>", "agent.x", ""} {
		if _, err := profileGrants(auditor, auditorPod1, TopicGrants{Subscribe: []string{bad}}); err == nil {
			t.Errorf("profileGrants accepted topic %q", bad)
		}
		if _, err := profileGrants(auditor, auditorPod1, TopicGrants{Publish: []string{bad}}); err == nil {
			t.Errorf("profileGrants accepted publish topic %q", bad)
		}
	}
}

// The real worker adapter, as a profile pod, runs a whole task under nothing
// but the grants the profile narrowing mints: the profile-shape counterpart of
// TestASessionAdapterRunsAWholeTaskUnderItsOwnGrants, and the check that the
// enumeration in profileGrants is what the adapter actually asks for.
//
// CapabilityOptional is set because a profile pod holds no verifier grant (see
// profile_narrowing.go): which name a profile task's capability is minted to,
// and so which verify subject the pod asks on, is the dispatcher's to decide.
// Until then a profile task runs only in the mixed-version window this flag
// names.
func TestAProfileAdapterRunsAWholeTaskUnderItsOwnGrants(t *testing.T) {
	h := startHarness(t, profileMap, profileTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	provisionTasksStream(t, h)

	const taskID = "task-profile-1"
	submitAs(t, h, auditor, taskID, "audit the thing", nil)

	res, err := workeradapter.Run(ctx, workeradapter.Config{
		NATSURL:            h.url,
		BusTokenFile:       tokenFile(t, tokenAuditorPod1),
		PodName:            auditorPod1,
		TaskID:             taskID,
		Profile:            auditor,
		ProfileExecutor:    true,
		CapabilityOptional: true,
		HarnessCommand: harnessStub(t, `
echo '{"type":"result","subtype":"success","result":"audited"}'
`),
		HarnessEnv:   os.Environ(),
		TaskDeadline: 30 * time.Second,
		KillGrace:    time.Second,
	})
	if err != nil {
		t.Fatalf("the adapter could not complete a task under its profile's grants: %v", err)
	}
	if res.State != lib.StateCompleted {
		t.Fatalf("terminal state = %q, want completed", res.State)
	}

	var sawFinal bool
	for _, e := range readEvents(t, h, auditor, taskID) {
		if e.Kind == lib.KindStatusUpdate && strings.Contains(string(e.Payload), `"final":true`) {
			sawFinal = true
		}
	}
	if !sawFinal {
		t.Error("no final status event on the profile's subject")
	}
	for _, n := range consumerNames(t, h) {
		if !strings.HasPrefix(n, auditorPod1+"-") && n != "test-reader" {
			t.Errorf("consumer %q is not named for the pod; a profile pod created something outside its naming contract", n)
		}
	}
}

// A profile pod that also carries a session name is refused at startup: the
// two shapes publish as different addressees.
func TestAnAdapterRefusesAProfileExecutorWithASession(t *testing.T) {
	h := startHarness(t, profileMap, profileTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	_, err := workeradapter.Run(ctx, workeradapter.Config{
		NATSURL:         h.url,
		BusTokenFile:    tokenFile(t, tokenAuditorPod1),
		PodName:         auditorPod1,
		TaskID:          "task-profile-2",
		Profile:         auditor,
		Session:         auditorPod1,
		ProfileExecutor: true,
		HarnessCommand:  harnessStub(t, `echo '{"type":"result","subtype":"success","result":"x"}'`),
		HarnessEnv:      os.Environ(),
		TaskDeadline:    10 * time.Second,
	})
	if err == nil || !strings.Contains(err.Error(), lib.EnvProfileExecutor) {
		t.Fatalf("want a startup refusal naming %s, got %v", lib.EnvProfileExecutor, err)
	}
}

// A narrowed pod named after a mapped principal would be granted that
// principal's inbox, since the user and the inbox prefix are the pod name. The
// callout refuses it at connect.
func TestANarrowedPodNamedAfterAMappedPrincipalIsRefused(t *testing.T) {
	const tokenPodNamedLikeAnEntry = "token-for-an-auditor-pod-named-like-a-mapped-user-padded-ok"
	tokens := profileTokens()
	tokens[tokenPodNamedLikeAnEntry] = Attested{ServiceAccount: auditorSA, PodName: "profile-cluster-a", PodUID: "uid-x"}
	h := startHarness(t, profileMap, tokens)
	if nc, err := nats.Connect(h.url, nats.Token(tokenPodNamedLikeAnEntry), nats.CustomInboxPrefix("_INBOX.profile-cluster-a")); err == nil {
		nc.Close()
		t.Fatal("a pod named after a mapped principal connected; it would hold that principal's inbox")
	}
}

// A profile that only publishes still opens the topic streams: `a2a topics
// write` resolves through TopicRegistry before it publishes.
func TestAPublishOnlyProfileCanResolveItsTopic(t *testing.T) {
	g, err := profileGrants(auditor, auditorPod1, TopicGrants{Publish: []string{auditorPublishTopic}})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"$JS.API.STREAM.INFO." + lib.StreamTopicsState, "$JS.API.STREAM.INFO." + lib.StreamTopicsJournal, "a2a.topics." + auditorPublishTopic} {
		if !containsString(g.Publish, want) {
			t.Errorf("a publish-only profile lacks %s", want)
		}
	}
}

// An agent-scoped topic has one writer, the agent it names. A profile may read
// another agent's topic but not publish on it, refused at parse and at mint.
func TestAProfileMayNotPublishAnotherAgentsTopic(t *testing.T) {
	if _, err := profileGrants(auditor, auditorPod1, TopicGrants{Publish: []string{"agent.platform.upgrade-readiness"}}); err == nil {
		t.Error("profileGrants minted a publish on the platform agent's topic for the auditor profile")
	}
	if _, err := profileGrants(auditor, auditorPod1, TopicGrants{Subscribe: []string{"agent.platform.upgrade-readiness"}, Publish: []string{"shared.blueprint", auditorPublishTopic}}); err != nil {
		t.Errorf("reading another agent's topic or publishing a shared or own topic was refused: %v", err)
	}
	raw := `{"version":"v","identities":[{"serviceAccount":"` + auditorSA + `","user":"profile-auditor","account":"APP","narrowing":"profile","profile":"auditor","topics":{"publish":["agent.platform.upgrade-readiness"]},"grants":{}}]}`
	if _, err := ParseIdentityMap([]byte(raw)); err == nil {
		t.Error("the map parsed a profile entry publishing another agent's topic")
	}
}

// A session-ServiceAccount pod named after a profile would be minted that
// profile's events and input, since a session's subjects are its pod name. The
// callout refuses it at connect; a real session name still connects.
func TestASessionPodNamedAfterAProfileIsRefused(t *testing.T) {
	const tokenSessionNamedAuditor = "token-for-a-session-sa-pod-named-like-the-auditor-profile-pad"
	tokens := profileTokens()
	tokens[tokenSessionNamedAuditor] = Attested{ServiceAccount: sessionSA, PodName: auditor, PodUID: "uid-s"}
	h := startHarness(t, profileMap, tokens)
	if nc, err := nats.Connect(h.url, nats.Token(tokenSessionNamedAuditor), nats.CustomInboxPrefix("_INBOX."+auditor)); err == nil {
		nc.Close()
		t.Fatal("a session pod named after the auditor profile connected; it would hold the profile's task plane")
	}
	nc, violations := h.connectAs(t, podA, tokenPodA)
	checkPublish(t, nc, violations, map[string]bool{lib.TaskEventsSubject(podA, "t"): false})
}
