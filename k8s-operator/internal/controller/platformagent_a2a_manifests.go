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

// The A2A stack the operator renders under `mode: next` and nothing else:
// the NATS/JetStream component, its stream/KV/topic provisioning, and the A2A
// gateway Deployment. Dark by construction — no call site outside the
// renderMode gate in Reconcile reaches this file.
//
// The deployment spec (docs/designs/spec-nats-deployment.md) is the law for
// streams, retention, and the account layout; subjects come from the payload
// spec (docs/designs/spec-a2a-payloads.md).
//
// PLAYGROUND POSTURE (stage 1): static per-component NATS users instead of
// the auth callout, single-node R1 JetStream (production: 3-node R3), no
// audit exporter, no breaker, gateway sweep as the only janitor. Each has a
// decided design in the specs; none gates letting people play. Static creds
// are the playground, not the product.

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// a2aPartOf marks every object of the next stack, so
	// `kubectl get -l app.kubernetes.io/part-of=a2a-next` is the whole venue.
	a2aPartOf = "a2a-next"

	// a2aComponentLabel distinguishes the pieces for targeted cleanup — the
	// provision Job's name carries a content hash, so deletion goes by label.
	a2aComponentLabel = "kubeagents.x-k8s.io/a2a-component"

	// a2aProvisionComponent is the a2aComponentLabel value the provision Job
	// carries, and the selector both sweeps of it (deleteA2AProvisionJobs)
	// list by. The builder's a2aLabels call spells the same value; the two
	// are pinned together by TestReconcileA2ADeletesSupersededProvisionJobs.
	a2aProvisionComponent = "provision"

	// The LiteLLM ports the session fence grants, for the reason
	// buildAgentEgressNetworkPolicy's LiteLLM rule states in full: a Pod
	// selector matches after the ClusterIP translation, so the container port
	// is the one that must be named. 8080 is what this repository's chart and
	// integration config render; 80 covers an endpoint listening on the
	// Service port directly and 4000 is LiteLLM's upstream default.
	a2aLiteLLMServicePort   = int32(80)
	a2aLiteLLMUpstreamPort  = int32(4000)
	a2aLiteLLMContainerPort = int32(8080)

	// a2aDNSPort is name resolution, granted on both protocols.
	a2aDNSPort = int32(53)

	// The streams the worker's JetStream API grant names, spelled as the
	// provision script creates them. A KV bucket is a stream called
	// KV_<bucket>, so the bucket name and the prefix are held apart.
	a2aTasksStream         = "TASKS"
	a2aTopicsStateStream   = "TOPICS-STATE"
	a2aTopicsJournalStream = "TOPICS-JOURNAL"
	a2aRuntimeStateBucket  = "runtime-state"
	a2aKVStreamPrefix      = "KV_"

	// a2aNATSConfGrantLine renders one allow-list entry at the depth of
	// accounts.APP.users[].permissions.publish in the nats.conf template
	// below: twelve spaces, the quoted subject, a trailing comma.
	a2aNATSConfGrantLine = "            %q,"

	// a2aProvisionWritablePath is the one writable path the provision
	// container has: the emptyDir mount, the nats CLI's HOME and
	// XDG_CONFIG_HOME, and its working directory. Four references that have to
	// agree, so they read from one name rather than four string literals.
	a2aProvisionWritablePath = "/tmp"

	a2aNATSImageEnvVar      = "A2A_NATS_IMAGE"
	defaultA2ANATSImage     = "nats:2.10-alpine"
	a2aProvisionImageEnvVar = "A2A_PROVISION_IMAGE"
	// nats-box carries the nats CLI the provisioning script drives.
	defaultA2AProvisionImage = "natsio/nats-box:0.14.5"
	a2aGatewayImageEnvVar    = "A2A_GATEWAY_IMAGE"
	// The stage 1 dev registry. A dev toggle's default may name a dev
	// registry; graduation moves this to the release pipeline alongside the
	// other first-party images.
	//
	// None of the four A2A images are in images.json, deliberately: the
	// inventory documents what a SUPPORTED install pulls, and mode next is an
	// unsupported dev toggle. That exemption is graduation debt alongside the
	// registry move — a mirrored or air-gapped install that flips next must
	// override all four via the env vars until then.
	defaultA2AGatewayImage = "northamerica-northeast1-docker.pkg.dev/bnaylor-kagents-dev/a2a-demo/gateway:latest"

	// The session-pod image, on the same terms as the three above. The
	// gateway binary carries this same default of its own (gateway/config.go),
	// which is what a gateway run outside the operator falls back to; the
	// operator renders the env unconditionally so that the override exists
	// wherever the operator is what installed the gateway. Arming spawning
	// without it would mean an install that flips next pulls an image no
	// operator input can redirect.
	a2aWorkerImageEnvVar  = "A2A_WORKER_IMAGE"
	defaultA2AWorkerImage = "northamerica-northeast1-docker.pkg.dev/bnaylor-kagents-dev/a2a-demo/worker-next:latest"

	// a2aConfigHashPlaceholder is the stand-in a2aConfigRolloutHash puts where
	// each password goes when it re-renders nats.conf for hashing. It carries
	// the key name so moving a credential from one user to another is still a
	// changed render, and it is the reason the digest in the pod template is
	// not a digest of the credentials.
	a2aConfigHashPlaceholder = "{{a2a-credential:%s}}"

	// a2aConfigHashRotationSeparator joins that render to the creds Secret's
	// resourceVersion, which is what makes an in-place credential rotation
	// roll the bus. A NUL byte cannot appear in the render, so no config text
	// can forge the boundary and pass itself off as a resourceVersion.
	a2aConfigHashRotationSeparator = "\x00resourceVersion="

	// a2aConfigHashLength is how much of the hex digest rides the pod-template
	// annotation. The annotation is a change detector, not an identifier.
	a2aConfigHashLength = 16

	// a2aNATSClientPort is the bus's client port: what the server listens on,
	// what the Service and the container port publish, what the ingress fence
	// allows, what the session-pod fence grants, and what every client URL
	// below dials. One name because those have to agree — a port changed in
	// all but one of them is a bus nothing can reach, and each is in a
	// different shape (a config line, an int32, an intstr, a URL) so a grep
	// does not reliably find them all. A fence and a listener that disagree
	// about a port fail as a timeout rather than as a refusal, which is the
	// slowest way to find this out.
	//
	// Untyped on purpose: strconv.Itoa below wants an int and the container
	// port wants an int32.
	a2aNATSClientPort = 4222

	// The other two listeners, named for the same reason: each is written in
	// the config, the container port and the Service, and the ingress fence
	// argues about all three by number.
	a2aNATSMonitorPort   = 8222
	a2aNATSWebSocketPort = 9222

	// The creds Secret's keys. Each is written in at least three places — this
	// list, the nats.conf template, and whatever workload consumes it through
	// a secretKeyRef — and a key that disagrees between them renders
	// `password: ""` or mounts nothing, so they are named rather than spelled
	// out at each site.
	a2aGatewayPasswordKey = "gateway-password" // #nosec G101 -- Secret key name, not a credential
	a2aWorkerPasswordKey  = "worker-password"  // #nosec G101 -- Secret key name, not a credential
	a2aSeedPasswordKey    = "seed-password"    // #nosec G101 -- Secret key name, not a credential
	a2aWebPasswordKey     = "web-password"     // #nosec G101 -- Secret key name, not a credential
	a2aSysPasswordKey     = "sys-password"     // #nosec G101 -- Secret key name, not a credential
	a2aCalloutPasswordKey = "callout-password" // #nosec G101 -- Secret key name, not a credential

	// a2aCredsSecretSuffix is appended to the NATS object name.
	a2aCredsSecretSuffix = "-creds"

	// a2aProvisionJobNameInfix sits between the agent's name and the digest in
	// the provision Job's name; a2aProvisionJobNameHashLength is how much of
	// the hex digest follows it. Eight characters is a change detector, the
	// same role the annotation above plays, and it is what
	// a2aPasswordDigestNeedles in the tests assumes when it checks that no
	// credential digest reaches a rendered name.
	a2aProvisionJobNameInfix      = "-a2a-provision-"
	a2aProvisionJobNameHashLength = 8

	// a2aPostureComment travels on every rendered config and script so the
	// posture cannot be mistaken for the product when read on the cluster.
	a2aPostureComment = `# PLAYGROUND POSTURE (stage 1): single-node R1 JetStream (production: 3-node
# R3), no audit exporter, no breaker, gateway sweep as the only janitor. Each
# has a decided design in the specs (spec-nats-deployment.md); none gates
# letting people play.
#
# Authentication is NOT on that list any more. The auth callout is armed: a
# client presents a projected Kubernetes ServiceAccount token, the callout
# validates it against the cluster with a TokenReview, and answers with the
# permission set the operator mapped that identity to. The users that remain
# static below are the ones with nothing to present - a browser, a session pod
# that carries no ServiceAccount, an operator at a port-forward, the callout
# itself - and each says so where it is defined.`
)

func a2aNATSImage() string {
	if override := os.Getenv(a2aNATSImageEnvVar); override != "" {
		return override
	}
	return defaultA2ANATSImage
}

func a2aProvisionImage() string {
	if override := os.Getenv(a2aProvisionImageEnvVar); override != "" {
		return override
	}
	return defaultA2AProvisionImage
}

func a2aGatewayImage() string {
	if override := os.Getenv(a2aGatewayImageEnvVar); override != "" {
		return override
	}
	return defaultA2AGatewayImage
}

func a2aWorkerImage() string {
	if override := os.Getenv(a2aWorkerImageEnvVar); override != "" {
		return override
	}
	return defaultA2AWorkerImage
}

func a2aNATSName(agent *agentv1alpha1.PlatformAgent) string    { return agent.Name + "-a2a-nats" }
func a2aGatewayName(agent *agentv1alpha1.PlatformAgent) string { return agent.Name + "-a2a-gateway" }
func a2aCalloutName(agent *agentv1alpha1.PlatformAgent) string { return agent.Name + "-a2a-callout" }

// a2aCredsSecretName is the Secret holding the static users' passwords.
func a2aCredsSecretName(agent *agentv1alpha1.PlatformAgent) string {
	return a2aNATSName(agent) + a2aCredsSecretSuffix
}

// a2aNATSAddress is the bus's in-cluster host:port. a2aNATSClientURL is the
// same thing as a client URL; both exist because the nats CLI takes the first
// and the Go client takes the second.
func a2aNATSAddress(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("%s.%s.svc:%d", a2aNATSName(agent), agent.Namespace, a2aNATSClientPort)
}

func a2aNATSClientURL(agent *agentv1alpha1.PlatformAgent) string {
	return "nats://" + a2aNATSAddress(agent)
}

// The provision Job's pods run as their own ServiceAccount so the auth callout
// has an identity to resolve them by. It holds no RBAC — the token exists to
// authenticate to NATS, not to talk to the API server.
func a2aProvisionServiceAccountName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-a2a-provision"
}

// a2aLabels returns the common labels with part-of overridden to a2a-next and
// the component named. withCommonLabels leaves pre-set keys alone, so these
// survive applyManaged.
func a2aLabels(agent *agentv1alpha1.PlatformAgent, component string) map[string]string {
	labels := commonLabels(agent)
	labels[labelPartOf] = a2aPartOf
	labels[a2aComponentLabel] = component
	return labels
}

// randomA2APassword returns a 32-hex-char credential. Playground: the value
// only ever lives in the two Secrets this file renders and is never a
// substitute for the auth callout.
func randomA2APassword() (string, error) {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return "", fmt.Errorf("generating NATS credential: %w", err)
	}
	return hex.EncodeToString(buf), nil
}

// a2aCredsKeys is every key the creds Secret must carry; an absent or empty
// key would render `password: ""` into nats.conf — a user anyone can log in
// as — so ensureA2ACredsSecret repairs the shape rather than trusting it.
// The gateway, agent and provision principals are absent on purpose: under the
// auth callout they hold no shared secret at all, which is the point. The
// gateway and seed keys survive so an install that predates the callout keeps a
// valid Secret shape through the upgrade, and so the hand-applied seed tooling
// still has a credential.
var a2aCredsKeys = []string{
	a2aGatewayPasswordKey, a2aWorkerPasswordKey, a2aSeedPasswordKey,
	a2aWebPasswordKey, a2aSysPasswordKey, a2aCalloutPasswordKey,
}

// a2aProvisionedStreams is every JetStream stream the provision Job creates, and
// the exact set seed's $JS.API grant is scoped to. KV buckets are streams named
// KV_<bucket>, so they belong in the same list.
//
// The seed grant in nats.conf renders from this slice. The provision script
// does not: each stream's create line carries its own subjects, retention and
// caps, so the script names the streams itself, in a2aProvisionScript. The two
// are a pair — a grant that does not name a stream makes that create time out
// on a refused API request, and a script that creates a stream the grant does
// not name is the same bug from the other side — and what holds them together
// is TestSeedGrantsAndProvisionScriptNameTheSameStreams, which reads the
// script's `stream add` / `kv add` lines and checks both directions against
// this list. Add a stream to one side and that test says so.
//
// Since the callout armed, the rendered Job authenticates as `provision` rather
// than as seed, and provisionIdentity enumerates the same objects for itself
// rather than from this slice. So the refusal the pair describes is provision's
// to hit now; seed keeps the scoped grant because the hand-applied seed tooling
// still connects with it. Nothing yet binds that third spelling to this list.
var a2aProvisionedStreams = []string{
	"TASKS", "DIRECTORY", "TOPICS-STATE", "TOPICS-JOURNAL",
	"KV_runtime-state", "KV_session-state", "KV_cap",
}

// a2aSeedJetStreamGrants is seed's publish allow-list for the JetStream API,
// replacing the `$JS.API.>` wildcard this user shipped with.
//
// seed was the identity the provision Job ran under, and it is still the one the
// hand-applied seed tooling connects with — the rendered Job has moved to the
// callout-authenticated `provision` principal — so this is defence in depth
// rather than a boundary. It is worth having anyway, because the seed password
// lives in the creds Secret for the life of the CR and deliberately survives a
// flip back to today, so the blast radius of a leak is not bounded by anything
// else.
//
// What the wildcard granted that provisioning never uses, and this list now
// refuses: STREAM.RESTORE (arbitrary messages with arbitrary stored subjects),
// STREAM.MSG.DELETE and PURGE (selective editing of the audit substrate),
// CONSUMER.CREATE (deliver-subject redirection, the server-originated write onto
// a subject nobody granted), and STREAM.DELETE.
//
// UPDATE is absent deliberately, and it is the interesting one. The script
// guards every create with an info check (`stream info X || stream add X`), so
// it never updates an existing stream — which means seed cannot set RePublish on
// one either. RePublish is a stream-config field settable at CREATE and UPDATE,
// and CREATE on an existing stream either returns that stream unchanged (when
// the config it carries is identical) or fails with JSStreamNameExistErr (when
// it differs). A RePublish edit is a differing config, so it takes the second
// branch. The one write route that survives a name-scoped allow-list in general
// is therefore closed here by the script's own idempotence. If a
// future script ever needs UPDATE, that reopens RePublish and the grant should
// say so out loud rather than quietly gaining a verb.
func a2aSeedJetStreamGrants() []string {
	// Account-level JetStream discovery. `stream add` asks for it
	// (IsStreamMaxBytesRequired -> JetStreamAccountInfo) and so does the
	// legacy CreateKeyValue path, which is what `kv add` runs.
	//
	// STREAM.NAMES is the one that is easy to miss and expensive to omit.
	// natscli's selectStream falls through to mgr.StreamNames(nil) when
	// LoadStream fails, which is exactly the first-run case the CREATE grants
	// exist for: every `stream info X || stream add X` guard on a fresh store
	// asks for it. A refused request is not an error the client sees -- nats.go
	// only records it and fires the async callback -- so the CLI waits out its
	// 5s timeout instead. Four streams, four timeouts, and four Publish
	// Violations in the same log the install is verified from. It is a
	// read-only listing of names the seed already knows, so granting it costs
	// nothing the CREATE and INFO grants above do not already concede.
	grants := []string{"$JS.API.INFO", "$JS.API.STREAM.NAMES"}
	for _, s := range a2aProvisionedStreams {
		grants = append(grants,
			`$JS.API.STREAM.CREATE.`+s,
			`$JS.API.STREAM.INFO.`+s,
		)
	}
	return grants
}

// a2aWorkerJetStreamGrants is the worker's publish allow-list for the
// JetStream API, replacing the `$JS.API.>` wildcard this user shipped with
// (gke-labs/kube-agents#1316).
//
// worker is the least-trusted principal in the deployment: it is the identity
// a session pod runs as, executing model output against untrusted input. The
// wildcard covered STREAM.PURGE, STREAM.UPDATE, STREAM.DELETE and
// STREAM.MSG.DELETE on every stream, DIRECTORY included. One PURGE empties the
// directory for every profile and nothing repopulates it; DELETE leaves only a
// re-run of the provision Job to bring the stream back.
//
// The list is what the worker-side binaries emit, read out of nats.go and then
// measured against a real server running this render
// (TestWorkerJetStreamGrantOnARealServer). Per stream:
//
//   - TASKS: STREAM.INFO (js.Stream in lib.TasksGet and the bridge's sweep),
//     CONSUMER.CREATE (the bridge's durable through CreateOrUpdateConsumer, and
//     the replay's ordered consumer; nats.go puts the filter subject in the API
//     subject, so the grant ends in `>`), CONSUMER.MSG.NEXT (every pull), and
//     DIRECT.GET (GetLastMsgForSubject: the replay horizon and the sweep's CAS
//     baseline). Acks are $JS.ACK.TASKS.>, granted beside this list.
//   - KV_runtime-state, the bridge's in-flight registry: STREAM.INFO
//     (js.KeyValue binds a bucket by reading its stream), and CONSUMER.CREATE
//     with CONSUMER.DELETE (kv.Keys is a push ordered consumer that nats.go
//     creates and then deletes on Unsubscribe, and the sweep runs it at every
//     bridge start). Put and Delete are publishes on $KV.runtime-state.>,
//     granted beside this list. No DIRECT.GET: nothing on the path calls
//     kv.Get -- the bridge puts, deletes and lists, and the worker adapter
//     touches no bucket -- and when a caller appears the grant is
//     DIRECT.GET.KV_runtime-state.>, with the server test as the place its
//     absence shows.
//   - TOPICS-STATE and TOPICS-JOURNAL: STREAM.INFO and DIRECT.GET, reads only.
//     `a2a topics read` and `list` are js.Stream, Stream.Info and
//     GetLastMsgForSubject on these two streams (lib.ReadTopicLatest,
//     lib.TopicRegistry), and the CLI dials with whatever NATS_USER its pod
//     carries; the worker credential is what a gateway-spawned session pod
//     gets. The writes are the three exact topic subjects above this list.
//
// Reads go through DIRECT.GET and not STREAM.MSG.GET because every stream the
// provision script creates has allow_direct set: the script says
// --allow-direct on each `stream add` (natscli's default too, stated so the
// grant does not rest on one), a KV bucket always has it, and the live store
// shows it on all seven. nats.go picks the route from the stream's own config,
// so the fallback is never emitted and is not granted.
//
// What the wildcard granted that nothing on the worker path uses, and this
// list now refuses: every verb on DIRECTORY (no STREAM.INFO, no consumer, no
// DIRECT.GET -- the gateway keeps subscribe on the cards, which is the read
// discovery needs), every verb on KV_session-state and KV_cap, PURGE / UPDATE /
// DELETE / MSG.DELETE / RESTORE / SNAPSHOT on every stream, STREAM.CREATE,
// enumeration (STREAM.NAMES, STREAM.LIST, CONSUMER.NAMES, CONSUMER.LIST),
// account INFO (jetstream.New never asks for it), and CONSUMER.INFO (nothing
// on the path binds to an existing consumer by name, and on nats.go v1.53.1
// no consumer re-verifies itself with it after a reconnect either --
// TestWorkerConsumersSurviveABusRestart holds that across a server restart).
//
// CONSUMER.DELETE on TASKS is withheld, and it is the one subject nats.go
// does emit here without a grant. The only emitter is the ordered consumer's
// reset path, which fires DeleteConsumer in a goroutine and ignores the
// result; an ephemeral it could not delete is reaped by its own five-minute
// inactive threshold.
//
// Withholding it raises the price of reaching another principal's durable and
// does not close the route, which is the correction to what this comment said
// first. CONSUMER.CREATE is create-OR-UPDATE by name -- the request's `action`
// field is empty for both, and the server has no ownership concept for a
// consumer name -- so within a stream the worker may create consumers on,
// every consumer on that stream is the worker's to reconfigure. Measured
// against this render, on the gateway's relay durable: one permitted
// $JS.API.CONSUMER.CREATE.TASKS.gateway-relay carrying the durable's own
// config with filter_subject changed retunes it, and the gateway stops seeing
// task events with no permissions violation logged anywhere; the same subject
// carrying inactive_threshold has the server reap the durable, ack floor and
// all, while CONSUMER.DELETE is refused in the same run. That is the residue
// the web block below already records for web, on the one stream where
// another principal has a durable to aim at. It is not new -- $JS.API.>
// permitted all of it -- and no narrower grant exists: nats.go's ordered
// consumers take server-generated names, so the last token has to be `>`, and
// NATS wildcards match whole tokens, so a per-prefix grant matches a consumer
// literally named that. What closes it is the auth callout giving each
// principal its own user. DELETE stays out as the one destructive verb here
// that nothing on the worker path needs.
//
// One route this list narrows but cannot close, because it lives in a request
// body: a push consumer's deliver_subject. CONSUMER.CREATE on TASKS (or on the
// KV bucket) lets the worker ask the server to deliver that stream's messages
// onto any subject, and a stream whose subjects cover the deliver subject
// stores them -- under their ORIGINAL subjects, so this is not forgery (a
// topic or card read by subject never sees them) but it is a persisted write
// into a stream the worker has no publish grant for, and with discard=old an
// eviction lever against it. The server delivers only once a subscription
// exists whose subject is EXACTLY the deliver subject: a push consumer
// registers through Sublist.registerNotification, which takes interest only
// from a match whose `sub.subject` is byte-equal to the deliver subject, and
// says so in its own doc comment ("this interest needs to be exact and ...
// wildcards will not trigger the notifications"). Identical in 2.10.29 and
// 2.14.5, and it is what bounds the residue: a principal watching the whole
// plane is not enough, and neither is a stream's own wildcard ingest.
// Measured on both versions, that splits the streams in two. TOPICS-STATE and
// TOPICS-JOURNAL have literal subjects, so their own ingest subscription IS
// the exact match and the write lands with no help (three TASKS messages
// arrived in TOPICS-STATE under a2a.tasks.* subjects). DIRECTORY, TASKS and
// the buckets have wildcard subjects, so a client has to hold a subscription
// on the deliver subject itself -- for the directory, another principal
// subscribed to one card subject. The gateway's `a2a.agents.>` and web's
// `a2a.>` subscribe grants permit that but do not supply it: a subscription
// on either wildcard leaves DIRECTORY empty, measured. Nothing in the tree
// opens a literal card subscription today. The wildcard this replaces had the
// same route with every stream as a source; what closes it is the worker not
// holding CONSUMER.CREATE at all, which is a pre-created consumer per task
// (the stage-3 dispatcher), not a grant. The server test measures all four
// cases.
func a2aWorkerJetStreamGrants() []string {
	kvRuntimeState := a2aKVStreamPrefix + a2aRuntimeStateBucket
	return []string{
		"$JS.API.STREAM.INFO." + a2aTasksStream,
		"$JS.API.CONSUMER.CREATE." + a2aTasksStream + ".>",
		"$JS.API.CONSUMER.MSG.NEXT." + a2aTasksStream + ".*",
		"$JS.API.DIRECT.GET." + a2aTasksStream + ".>",
		"$JS.API.STREAM.INFO." + kvRuntimeState,
		"$JS.API.CONSUMER.CREATE." + kvRuntimeState + ".>",
		"$JS.API.CONSUMER.DELETE." + kvRuntimeState + ".*",
		"$JS.API.STREAM.INFO." + a2aTopicsStateStream,
		"$JS.API.DIRECT.GET." + a2aTopicsStateStream + ".>",
		"$JS.API.STREAM.INFO." + a2aTopicsJournalStream,
		"$JS.API.DIRECT.GET." + a2aTopicsJournalStream + ".>",
	}
}

// a2aNATSConfGrantLines renders grants as nats.conf allow-list entries at the
// depth of a user's publish or subscribe list, one per line, for splicing
// into the template below.
func a2aNATSConfGrantLines(grants []string) string {
	lines := make([]string, 0, len(grants))
	for _, g := range grants {
		lines = append(lines, fmt.Sprintf(a2aNATSConfGrantLine, g))
	}
	return strings.Join(lines, "\n")
}

// a2aCredsValueRe is the exact shape randomA2APassword emits. It is a
// security check, not tidiness: buildA2ANATSConfigSecret interpolates these
// values into nats.conf inside double quotes, so a value carrying a quote and
// a newline is a config injection — a new user, a widened grant — that the
// operator would then faithfully re-render on every reconcile, converting a
// one-time Secret write into durable bus authority. A key that does not match
// is treated exactly like a missing key and re-rolled; hand-seeding the creds
// Secret is not a supported flow (see ensureA2ACredsSecret).
var a2aCredsValueRe = regexp.MustCompile(`^[0-9a-f]{32}$`)

// a2aReader returns the reader for A2A bookkeeping objects. Straight from the
// API server on purpose: the cached client's first Get against a kind starts
// a cluster-wide informer for it, and this path runs on every reconcile of
// every agent — including today-mode installs that will never render the A2A
// stack. Caching every Secret and Job in the cluster to serve that is the
// same trade APIReader already refuses for collector discovery.
func (r *PlatformAgentReconciler) a2aReader() client.Reader {
	if r.APIReader != nil {
		return r.APIReader
	}
	return r.Client
}

// ensureA2ACredsSecret creates the per-user credential Secret once and then
// leaves it alone: regenerating on reconcile would invalidate every connected
// client every few seconds. It survives a flip back to `today` on purpose —
// it is inert data, and re-enabling `next` must not re-roll credentials the
// gateway image may have cached in a still-running pod. The one thing it
// changes on an existing Secret is a missing or empty key, which it fills.
func (r *PlatformAgentReconciler) ensureA2ACredsSecret(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (*corev1.Secret, error) {
	name := types.NamespacedName{Name: a2aCredsSecretName(agent), Namespace: agent.Namespace}
	existing := &corev1.Secret{}
	err := r.a2aReader().Get(ctx, name, existing)
	if err == nil {
		repaired := false
		if existing.Data == nil {
			existing.Data = map[string][]byte{}
		}
		for _, key := range a2aCredsKeys {
			if a2aCredsValueRe.Match(existing.Data[key]) {
				continue
			}
			pw, err := randomA2APassword()
			if err != nil {
				return nil, err
			}
			existing.Data[key] = []byte(pw)
			repaired = true
		}
		if repaired {
			if err := r.Update(ctx, existing); err != nil {
				return nil, err
			}
		}
		return existing, nil
	}
	if !errors.IsNotFound(err) {
		return nil, err
	}

	data := map[string][]byte{}
	for _, key := range a2aCredsKeys {
		pw, err := randomA2APassword()
		if err != nil {
			return nil, err
		}
		data[key] = []byte(pw)
	}
	secret := &corev1.Secret{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{Name: name.Name, Namespace: name.Namespace, Labels: a2aLabels(agent, "nats-creds")},
		Data:       data,
	}
	if err := ctrl.SetControllerReference(agent, secret, r.Scheme); err != nil {
		return nil, err
	}
	if err := r.Create(ctx, secret); err != nil {
		return nil, err
	}
	return secret, nil
}

// renderA2ANATSConf renders nats.conf, taking every password from pw.
//
// The property being preserved, verbatim from the deployment spec: the bus
// decides who may say what before a message is read. Deny-by-default — a
// permissions block with allow lists denies everything else — with per-user
// _INBOX prefixes so the reply path cannot leak what the subject grants
// withheld. Seed's JetStream API grant is scoped to the streams it provisions
// and the worker's to the streams it uses, both by name and by verb
// (a2aSeedJetStreamGrants, a2aWorkerJetStreamGrants), and provision has moved
// to the callout and holds the enumerated subjects too (its identity entry
// spells them). Gateway alone still holds a bare $JS.API.>, which is playground
// posture; narrowing it is the same change again with its own table of what it
// emits, and it wants its own live proof because it owns the relay durable and
// the session registry. It is also the one identity that cannot narrow on this
// branch's terms: it has no client presenting a token yet.
//
// This comment is only true of the identity table below it: read that, not this.
//
// pw is a parameter rather than a closure over the creds Secret because two
// callers walk this template: buildA2ANATSConfigSecret with the real lookup,
// and a2aConfigRolloutHash with one that returns placeholders. One template
// and two lookups is what lets the rollout digest cover every non-secret byte
// without covering a credential — a password interpolated here by any route
// other than pw is back in the digest.
//
// TestA2AConfigRolloutHashOmitsCredentialsAndTracksRotation is the guard for
// that: it hashes two creds Secrets that differ only in their password bytes
// and requires the digests to be equal, so any route by which a credential
// re-enters the hashed input reds it.
// TestA2ARenderedObjectsCarryNoPasswordDigest is the wider but shallower one —
// it catches a digest of a password, or of the real conf, reaching a rendered
// name, label or annotation, and it cannot see a credential folded into the
// hashed bytes as a third string.
//
// keys carries the callout's two PUBLIC keys, which is why they travel with
// pw's placeholders rather than being one of them: an issuer public key is not
// a credential, and the rollout digest has to cover it. If it did not, rotating
// the callout keypair would update the config Secret without rolling the
// StatefulSet, leaving the server trusting an issuer nothing signs with any
// more — every answer the callout gives refused, on a bus that looks healthy.
func renderA2ANATSConf(agent *agentv1alpha1.PlatformAgent, pw func(key string) string, keys *a2aCalloutKeys) string {
	return a2aPostureComment + `

server_name: ` + a2aNATSName(agent) + `
port: ` + strconv.Itoa(a2aNATSClientPort) + `
http: ` + strconv.Itoa(a2aNATSMonitorPort) + `

# A ServiceAccount token travels inside the client's CONNECT frame, and the
# default max_control_line of 4096 bounds that whole frame - measured, the
# usable room for the token itself is around 3920 bytes once the rest of the
# CONNECT JSON is accounted for. A plain projected token fits with room to
# spare; one bound to several audiences, from a client with a long name, does
# not. The failure is not graceful: the server closes the connection with
# "Maximum Control Line Exceeded" before authentication happens at all, so it
# reads as the bus refusing a workload rather than as a size limit.
max_control_line: 65536

# Websocket listener for the web user (the read-only web rail reads the bus
# over this).
#
# Plain ws IS the playground posture, stated rather than implied, and stated
# accurately: the CONNECT frame carries the web password in cleartext across
# the pod network. The Service is ClusterIP, so nothing OUTSIDE the cluster
# reaches this listener — and an ingress NetworkPolicy fences the pod network
# too: 4222 from the enumerated bus clients only, and NO pod-network peer for
# 8222 or 9222. The port-forward the demo uses and the kubelet's readiness
# probe both enter from the node, which the policy does not govern, so the ws
# surface is reachable through kubectl and through nothing else. Production
# still terminates TLS in front of the bus, which is not a toggle that exists
# yet.
#
# The origin allow-list is the one thing here that is not posture. WebSockets
# are exempt from CORS, and the demo transport is a kubectl port-forward to
# 9222 on a workstation — for as long as that runs, every page the operator's
# browser visits can open a socket to localhost:9222, with a credential that
# lives in browser JS by construction.
#
# allowed_origins, NOT same_origin: same_origin compares the browser's Origin
# against this listener's own host:port, and the UI is always a page on a
# different port than the bus (vite on 5173, or an nginx port), so it can never
# match. Measured in a real browser: same_origin gives every UI deployment a
# 403 at the handshake. A CLI or Node client sends no Origin header at all,
# which both settings permit — which is exactly why this needed a browser to
# find.
#
# This is defense in depth and not a boundary: Origin is browser-asserted, so
# anything that is not a browser simply omits it. The boundary is the web
# user's grant list below.
websocket {
  port: ` + strconv.Itoa(a2aNATSWebSocketPort) + `
  no_tls: true
  allowed_origins: ["http://localhost:5173", "http://127.0.0.1:5173"]
}

jetstream {
  store_dir: /data
  # Under the 40Gi PV; the stream max_bytes caps (20+5+1+1 GiB) plus KV live
  # inside this.
  max_file_store: 34359738368
}
accounts {
  # AUTH: the auth callout service and nothing else.
  #
  # A dedicated account, and that is a boundary rather than tidiness. The
  # server publishes each authorization request into THIS account and takes
  # the first answer that comes back on the reply inbox — and it does not
  # check that the answer's outer envelope was signed by the configured
  # issuer (measured; the inner user JWT's signature IS checked). So anything
  # able to publish into this account's $SYS._INBOX.> and win the race can
  # answer an authorization request. It could not forge a grant without the
  # issuer seed, but it could refuse one. Nothing else belongs in here.
  AUTH {
    users [
      {
        # The callout cannot authenticate through itself, so it is exempt via
        # auth_users below and carries a password. This permission pair is
        # the entire surface it needs: read the requests, answer them.
        user: callout
        password: "` + pw(a2aCalloutPasswordKey) + `"
        permissions {
          subscribe { allow = [ "$SYS.REQ.USER.AUTH" ] }
          publish { allow = [ "$SYS._INBOX.>" ] }
        }
      }
    ]
  }
  APP {
    jetstream: enabled
    users [
` + renderA2AStaticUsers(agent, pw, a2aAccountApp) + `    ]
  }
  # $SYS: human operators and monitoring only; no agent authenticates here.
  SYS {
    users [
` + renderA2AStaticUsers(agent, pw, a2aAccountSys) + `    ]
  }
}
system_account: SYS

# The auth callout: who a connection is, decided against the cluster that
# issued its identity rather than against a password this file rendered.
#
# What changes for a client: it presents a projected ServiceAccount token
# instead of a password, the callout validates that token with a TokenReview
# against the local API server, and the grants it gets back are the ones the
# operator rendered for that ServiceAccount. What does NOT change is where
# enforcement happens — the permission set still arrives before the connection
# is usable, and the server still refuses on it without consulting any
# application code.
authorization {
  # Two seconds, and this is a ceiling rather than a preference.
  #
  # The server starts a first-ping timer on a connection that has not yet
  # authenticated, at roughly two seconds. If the callout has not answered by
  # then the client receives a PING where the Go client library requires a
  # PONG, and it aborts the connect reporting "expected 'PONG', got 'PING'" —
  # which names nothing about authorization and sends whoever is debugging it
  # to the network layer. Measured: at timeout 2 the failure is a clean
  # Authorization Violation at a predictable deadline; at 3 or above it is
  # that message instead. A merely SLOW callout hits it too, so the real
  # budget for a TokenReview round trip is under two seconds whatever this
  # number says.
  timeout: 2

  auth_callout {
    # Public halves only. The seeds live in the callout's own Secret, mounted
    # by the callout Deployment and nothing else. The issuer signs the user
    # JWTs that carry the permissions this server enforces, so its holder can
    # mint a user with any grants at all — see the callout-keys Secret for the
    # custody note.
    issuer: ` + keys.IssuerPublic + `
    account: AUTH

    # The request carries the client's raw ServiceAccount token, so it is
    # encrypted in flight to the callout. Note the server does not require the
    # RESPONSE to be encrypted even with this set, so response confidentiality
    # is the callout's own discipline rather than something enforced here.
    xkey: ` + keys.XKeyPublic + `

    # Exempt from the callout: authenticated from this file, by username.
    #
    # This is a bypass and not a fallback — a listed user with a wrong
    # password is refused statically and never reaches the callout at all.
    # Every name here is a principal that cannot present a ServiceAccount
    # token: the callout itself (it cannot authenticate through itself), the
    # session workers (a session pod carries no Kubernetes identity yet), the
    # browser-facing read user (a browser never can), and the operator's own
    # $SYS login.
    auth_users: [ ` + renderA2AAuthUsers(agent) + ` ]
  }
}
`
}

// buildA2ANATSConfigSecret renders nats.conf with the real credentials from
// the creds Secret. This Secret's Data is the one place the passwords are
// meant to appear; a2aConfigRolloutHash covers the rest of the render.
func buildA2ANATSConfigSecret(agent *agentv1alpha1.PlatformAgent, creds *corev1.Secret, keys *a2aCalloutKeys) *corev1.Secret {
	conf := renderA2ANATSConf(agent, func(key string) string { return string(creds.Data[key]) }, keys)

	return &corev1.Secret{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aNATSName(agent) + "-config",
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "nats-config"),
		},
		Data: map[string][]byte{"nats.conf": []byte(conf)},
	}
}

// a2aConfigRolloutHash is the digest that rides the StatefulSet pod template
// so a changed bus config reaches a running server: the config Secret updates
// in place, but the nats container only reads it at boot.
//
// It is deliberately NOT a digest of the rendered nats.conf. That file carries
// all five NATS passwords, so hashing it put a truncated digest of the
// credentials in an annotation anyone who can get the StatefulSet can read —
// harmless against 32 random hex characters, an offline target the day a
// password is hand-set, and CodeQL alert 27 (go/weak-sensitive-data-hashing).
//
// Instead it covers the conf rendered with a placeholder in each password's
// place, plus the creds Secret's resourceVersion. Both halves are load-bearing:
// the placeholder render tracks every non-secret byte, so a config change still
// rolls the bus, and the resourceVersion tracks a credential rotation, which
// ensureA2ACredsSecret performs as an Update on the existing Secret — the UID
// would not move, which is why this is the resourceVersion.
//
// Two things that costs, both accepted rather than overlooked:
//
// The hash is no longer content-addressed. resourceVersion moves on ANY
// accepted write to the creds Secret, so labelling it by hand, a policy
// controller stamping the namespace, or a restore that renumbers the namespace
// rolls the single-replica bus once with nothing the server reads having
// changed — clients reconnect, and JetStream state lives on the PV. The
// alternative that ignores metadata churn is a digest of the password bytes,
// which is the alert this function exists to close, so the spurious roll is
// the price of not hashing the credential.
//
// And the rotation it notices rolls the bus, not the bus's clients. This hash
// rides the NATS pod template alone; the gateway Deployment and the provision
// Job take their passwords through valueFrom.secretKeyRef, which a running pod
// does not re-read, so a repaired credential leaves the gateway holding the old
// one until something else restarts it. That gap predates this function — the
// conf digest rolled only the StatefulSet too — and closing it means deciding
// what a rotation should restart, which is not this function's call.
func a2aConfigRolloutHash(agent *agentv1alpha1.PlatformAgent, creds *corev1.Secret, keys *a2aCalloutKeys) string {
	redacted := renderA2ANATSConf(agent, func(key string) string {
		return fmt.Sprintf(a2aConfigHashPlaceholder, key)
	}, keys)
	sum := sha256.Sum256([]byte(redacted + a2aConfigHashRotationSeparator + creds.ResourceVersion))
	return hex.EncodeToString(sum[:])[:a2aConfigHashLength]
}

// buildA2ANATSStatefulSet renders the bus. confHash comes from
// a2aConfigRolloutHash and rides the pod template — the agent Deployment's
// config-hash mechanism — so a changed render rolls the server instead of
// silently diverging from it.
// a2aNATSDataClaim is the StatefulSet's volumeClaimTemplate name. The claim the
// controller stamps out is "<this>-<sts>-0", which handleDeletion reaps by name --
// so a rename here that is not matched there turns the reap into a silent no-op
// and leaks the PV on every CR deletion. One spelling, both sites.
const a2aNATSDataClaim = "data"

func buildA2ANATSStatefulSet(agent *agentv1alpha1.PlatformAgent, confHash string) *appsv1.StatefulSet {
	name := a2aNATSName(agent)
	labels := a2aLabels(agent, "nats")
	selector := map[string]string{"app": name}
	podLabels := map[string]string{"app": name}
	for k, v := range labels {
		podLabels[k] = v
	}

	return &appsv1.StatefulSet{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "StatefulSet"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: labels},
		Spec: appsv1.StatefulSetSpec{
			ServiceName: name,
			// Single node, R1, per the dev posture in the deployment spec;
			// production guidance is a 3-node cluster with stream replicas R3.
			Replicas: ptr.To(int32(1)),
			Selector: &metav1.LabelSelector{MatchLabels: selector},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      podLabels,
					Annotations: map[string]string{"kubeagents.x-k8s.io/a2a-config-hash": confHash},
				},
				Spec: corev1.PodSpec{
					AutomountServiceAccountToken: ptr.To(false),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot:   ptr.To(true),
						RunAsUser:      ptr.To(int64(1000)),
						FSGroup:        ptr.To(int64(1000)),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: []corev1.Container{{
						Name:  "nats",
						Image: a2aNATSImage(),
						Args:  []string{"-c", "/etc/nats/nats.conf"},
						Ports: []corev1.ContainerPort{
							{Name: "client", ContainerPort: a2aNATSClientPort},
							{Name: "monitor", ContainerPort: a2aNATSMonitorPort},
							{Name: "websocket", ContainerPort: a2aNATSWebSocketPort},
						},
						VolumeMounts: []corev1.VolumeMount{
							{Name: "config", MountPath: "/etc/nats", ReadOnly: true},
							{Name: a2aNATSDataClaim, MountPath: "/data"},
						},
						SecurityContext: hardenedSecurityContext(),
						ReadinessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{
								HTTPGet: &corev1.HTTPGetAction{Path: "/healthz", Port: intstr.FromString("monitor")},
							},
						},
					}},
					Volumes: []corev1.Volume{{
						Name: "config",
						VolumeSource: corev1.VolumeSource{
							Secret: &corev1.SecretVolumeSource{SecretName: a2aNATSName(agent) + "-config"},
						},
					}},
				},
			},
			VolumeClaimTemplates: []corev1.PersistentVolumeClaim{{
				// The labels ride to the PVC the StatefulSet controller
				// stamps out, which is what lets handleDeletion verify the
				// claim is this render's before deleting it — a template PVC
				// carries no owner reference, so the instance label is the
				// only ownership signal it has.
				ObjectMeta: metav1.ObjectMeta{Name: a2aNATSDataClaim, Labels: a2aLabels(agent, "nats")},
				Spec: corev1.PersistentVolumeClaimSpec{
					AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
					Resources: corev1.VolumeResourceRequirements{
						Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse("40Gi")},
					},
				},
			}},
		},
	}
}

func buildA2ANATSService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	name := a2aNATSName(agent)
	return &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aLabels(agent, "nats")},
		Spec: corev1.ServiceSpec{
			Selector: map[string]string{"app": name},
			Ports: []corev1.ServicePort{
				{Name: "client", Port: a2aNATSClientPort},
				{Name: "monitor", Port: a2aNATSMonitorPort},
				// The web user's transport. ClusterIP on purpose: the demo
				// reaches it with kubectl port-forward, and plain ws must not
				// be reachable any other way.
				{Name: "websocket", Port: a2aNATSWebSocketPort},
			},
		},
	}
}

// a2aSessionComponent is the label value the gateway's spawner stamps on every
// session pod it creates, paired with part-of: a2aPartOf under the STANDARD
// app.kubernetes.io/component key (the spawner is a client of the cluster, not
// the operator, so it uses the standard key; operator-rendered pieces carry
// a2aComponentLabel). Three things select on this pair and must agree: the
// bus fence's session peer, the session fence's own podSelector, and the
// gateway's session cap and sweeper, which count and list pods by it.
const a2aSessionComponent = "a2a-session"

func a2aNATSNetpolName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-a2a-nats-netpol"
}

func a2aSessionNetpolName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-a2a-session-netpol"
}

// buildA2ASessionNetworkPolicy fences the pods the gateway spawns. Nothing
// selected them before this policy, so a session pod's egress was open while
// the agent pod it works for was fenced by buildAgentEgressNetworkPolicy —
// the delegation path was the way around the agent's own allowlist.
//
// Deny-by-default with three destinations, which is the whole of a worker's
// job description:
//
//	DNS       — name resolution for the two peers below, same peer set the
//	            agent's egress policy uses so the two cannot drift on what DNS
//	            means.
//	NATS 4222 — the bus, by pod label rather than CIDR: a pod IP does not
//	            survive a restart and a policy pinned to one stops matching
//	            silently.
//	LiteLLM   — the model path. Ports 80/4000/8080 for the reason
//	            buildAgentEgressNetworkPolicy's LiteLLM rule states: a Pod
//	            selector matches after the ClusterIP translation, so the port
//	            that must be granted is the container's.
//
// There is no API-server rule, no 443 and no metadata rule beyond DNS, because
// a session pod carries no ServiceAccount and no Workload Identity (spawn.go
// sets AutomountServiceAccountToken: false and names none). A worker that
// needs the internet is a design change, not a policy widening.
//
// PolicyTypes carries Ingress with no rules on purpose: nothing dials a
// session pod, so a listener in a worker is an accident and an accident should
// be unreachable. kubectl exec and logs ride the kubelet API rather than the
// pod network, so debugging is unaffected.
func buildA2ASessionNetworkPolicy(agent *agentv1alpha1.PlatformAgent, dnsClusterIPs []string) *networkingv1.NetworkPolicy {
	// clusterDNSPeers is the one definition of "DNS" this package has — the
	// gateway policy and the shell sandbox's policy already share it, and
	// sharing it here is what keeps a correction from landing on two of the
	// three. Its own comment argues each peer; the part that matters for a
	// session pod is that port 53 to the Cloud DNS resolver address reaches
	// no credential, because the token API is on :80 pre-NAT and :988
	// post-NAT and the only rule below naming 80 is LiteLLM's, whose peer is
	// a Pod selector that no link-local address matches.
	dnsPeers := clusterDNSPeers(dnsClusterIPs)

	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aSessionNetpolName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "session-netpol"),
		},
		Spec: networkingv1.NetworkPolicySpec{
			// No instance label, unlike the rest of what the operator
			// renders, because the spawner stamps none — the selector can
			// only name what the pods carry. Two PlatformAgents in one
			// namespace would each fence the other's session pods with an
			// identical rule set, so the effect is a duplicate fence rather
			// than a gap; the bus grants still separate them at auth.
			PodSelector: metav1.LabelSelector{
				MatchLabels: map[string]string{
					labelPartOf:                   a2aPartOf,
					"app.kubernetes.io/component": a2aSessionComponent,
				},
			},
			PolicyTypes: []networkingv1.PolicyType{
				networkingv1.PolicyTypeIngress,
				networkingv1.PolicyTypeEgress,
			},
			Egress: []networkingv1.NetworkPolicyEgressRule{
				{
					Ports: []networkingv1.NetworkPolicyPort{udpPort(a2aDNSPort), tcpPort(a2aDNSPort)},
					To:    dnsPeers,
				},
				{
					Ports: []networkingv1.NetworkPolicyPort{tcpPort(a2aNATSClientPort)},
					To: []networkingv1.NetworkPolicyPeer{
						namespacedPodPeer(agent.Namespace, map[string]string{
							labelPartOf:       a2aPartOf,
							a2aComponentLabel: "nats",
						}),
					},
				},
				{
					Ports: []networkingv1.NetworkPolicyPort{
						tcpPort(a2aLiteLLMServicePort),
						tcpPort(a2aLiteLLMUpstreamPort),
						tcpPort(a2aLiteLLMContainerPort),
					},
					To: []networkingv1.NetworkPolicyPeer{
						namespacedPodPeer(agent.Namespace, map[string]string{"app": "litellm"}),
					},
				},
			},
		},
	}
}

// buildA2ANATSNetworkPolicy governs ingress to the NATS pod. Without it every
// pod in the cluster reaches 4222/8222/9222 while the deny-by-default bus
// grants do the real refusing; with it the network layer agrees with the
// grants: 4222 from exactly the enumerated bus clients, nothing else.
//
// 8222 (monitor) and 9222 (ws) get no pod-network peer at all, decided rather
// than forgotten. Both surfaces are node-path consumers: the kubelet's
// readiness probe on 8222 and the demo's kubectl port-forward on 9222 enter
// from the node, which NetworkPolicy does not govern (Dataplane V2 exempts
// host-local traffic), so denying every pod costs neither. An in-cluster ws
// client would be the web rail deployed into the cluster — a peer to add to
// this list when it exists, not a reason to leave the port open to every pod
// now.
func buildA2ANATSNetworkPolicy(agent *agentv1alpha1.PlatformAgent) *networkingv1.NetworkPolicy {
	tcp := corev1.ProtocolTCP

	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aNATSNetpolName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "nats-netpol"),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{
				MatchLabels: map[string]string{"app": a2aNATSName(agent)},
			},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
			Ingress: []networkingv1.NetworkPolicyIngressRule{{
				Ports: []networkingv1.NetworkPolicyPort{
					{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(a2aNATSClientPort))},
				},
				From: []networkingv1.NetworkPolicyPeer{
					// The auth callout, FIRST, and the ordering is the
					// point rather than tidiness.
					//
					// The callout is itself a bus client: it subscribes
					// to $SYS.REQ.USER.AUTH from its own AUTH account -
					// the subject name is not the system account - and
					// answers every connection attempt. So it sits
					// ON the connection path, and a fence that does not
					// name it refuses the one peer every new connection
					// depends on. Nothing looks broken when that
					// happens — established connections are already
					// authorized and keep working, so the bus stays up,
					// serves traffic, and silently accepts no new
					// client until something tries to connect and hangs.
					// That is why this peer and the callout itself have
					// to land in one change: arming the callout against
					// a fence that predates it takes the fabric dark to
					// new work.
					//
					// The same rule is owed to the peers that arm later.
					// The audit exporter, the janitor and the metrics
					// scrape each add one when they exist. And the NATS
					// pods join on their route port the moment this
					// leaves the single-node dev shape: a 3-node cluster's
					// servers dial each other, and a fence without the
					// route peer means the cluster never forms.
					{PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
						"app": a2aCalloutName(agent),
					}}},
					// The agent pod — a bridge sidecar declared on
					// spec.deployment.sidecars rides this selector too.
					{PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
						"app": agent.Name + "-gateway",
					}}},
					// The A2A gateway.
					{PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
						"app": a2aGatewayName(agent),
					}}},
					// Session pods, by the spawner's labels (see
					// a2aSessionComponent above).
					{PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
						labelPartOf:                   a2aPartOf,
						"app.kubernetes.io/component": a2aSessionComponent,
					}}},
					// The provision Job's pods.
					{PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
						labelPartOf:       a2aPartOf,
						a2aComponentLabel: "provision",
					}}},
					// Seed tooling: hand-applied, not a render, but a
					// legitimate bus client whose re-run must refuse at auth
					// if anything, not hang at the dial.
					{PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
						labelPartOf:       a2aPartOf,
						a2aComponentLabel: "seed",
					}}},
				},
			}},
		},
	}
}

// a2aProvisionScript is the provisioning payload: the four streams, three KV
// buckets, and three starter topics from the deployment spec, created
// idempotently with the nats CLI. Topics are provisioned-only (payload spec):
// which topics exist is exactly the subject lists rendered here.
func a2aProvisionScript(agent *agentv1alpha1.PlatformAgent) string {
	server := a2aNATSAddress(agent)
	return a2aPostureComment + `
set -euo pipefail
# This Job authenticates to the bus with its own projected ServiceAccount
# token, which the auth callout resolves against the cluster. It holds no
# password: there is no "provision" entry in nats.conf at all.
#
# The token goes in the PASSWORD field rather than a --token flag. NOT for
# secrecy: --password puts it on argv exactly as --token would, so it is in
# /proc/<pid>/cmdline for the life of each call either way, and the pod is the
# boundary that matters. The reason is that the username half then carries the
# ServiceAccount this Job claims to be - a claim the callout does not trust and
# does not need to, since it validates the token and derives the identity from
# the TokenReview. It travels because it costs nothing and makes a connection
# legible in a server-side log. The callout accepts the token in either field.
BUS_TOKEN="$(cat ` + a2aBusTokenPath + `/` + a2aBusTokenFile + `)"

# --inbox-prefix: every stream/kv call here is a $JS.API request whose reply
# lands on an inbox, and this principal may only subscribe under
# _INBOX.provision.> — the CLI's default _INBOX.<nuid> would be refused and
# every call would time out.
NATS="nats --server ` + server + ` --user ${BUS_USER} --password ${BUS_TOKEN} --inbox-prefix=_INBOX.provision"

# max_consumers caps each stream at 64. Consumer durability is a request-body
# field, so no permission list can hold web to ephemeral ones (see the web user
# in nats.conf); the cap is what stops an unreapable durable per page-load from
# growing the file store without bound. The failure it converts to is loud — a
# refused create — rather than silent disk growth. Note the trade: a client that
# burns the cap can also deny a legitimate consumer, which is the right way
# round for a playground and the wrong one for production, where the callout
# mints per-identity users and this becomes a per-user limit instead.

# Retention rule (deployment spec): acknowledgement must not delete — all
# message streams are limits-based with an age window; replay is a read.
# Every stream carries a hard max_bytes with discard old so a flood degrades
# replay oldest-first instead of filling the PV and stalling JetStream.
#
# --allow-direct is stated on every stream even though it is the CLI's
# default: the worker's JetStream API grant is written for the direct-get
# route (nats.go picks DIRECT.GET or STREAM.MSG.GET from the stream's own
# config), so the bit the grant rests on is set here, not inherited.

# TASKS: a2a.tasks.>, 72h dev window, 20GiB cap.
$NATS stream info TASKS >/dev/null 2>&1 || $NATS stream add TASKS --allow-direct \
  --subjects='a2a.tasks.>' --storage=file --retention=limits \
  --max-age=72h --max-bytes=21474836480 --discard=old --replicas=1 --max-consumers=64 --defaults

# DIRECTORY: last-value — the tombstone replaces the card. 1GiB cap.
$NATS stream info DIRECTORY >/dev/null 2>&1 || $NATS stream add DIRECTORY --allow-direct \
  --subjects='a2a.agents.>' --storage=file --retention=limits \
  --max-msgs-per-subject=1 --max-bytes=1073741824 --discard=old --replicas=1 --max-consumers=64 --defaults

# TOPICS-STATE: current answer plus short history, no age limit. 1GiB cap.
# State-class topics (provisioned registry): upgrade-readiness, blueprint, probe.
#
# The probe subject is the one here with NO writer, deliberately, and it is the
# single exception to the rule that a topic's subject list and its writer's
# grant travel together. It exists so that an authorization probe has a real
# provisioned subject to be refused ON: a refusal against an unprovisioned
# subject proves only that the subject does not exist, while a refusal here
# proves the grant. The web rail ships that probe as a button, so it is pressed
# in front of an audience rather than living in a test file.
#
# It was aimed at the blueprint topic first. That works right up until the
# grant is wrong, at which point the probe writes junk into a state-class
# topic the fleet actually reads - and "it cannot happen while the grants
# hold" is the assumption the web user already broke once. A writerless
# subject makes the failure mode land nowhere.
$NATS stream info TOPICS-STATE >/dev/null 2>&1 || $NATS stream add TOPICS-STATE --allow-direct \
  --subjects='a2a.topics.agent.platform.upgrade-readiness,a2a.topics.shared.blueprint,a2a.topics.shared.probe' \
  --storage=file --retention=limits \
  --max-msgs-per-subject=8 --max-bytes=1073741824 --discard=old --replicas=1 --max-consumers=64 --defaults

# TOPICS-JOURNAL: append-only, ages out at 30d. 5GiB cap.
# Journal-class topics: annotations.
$NATS stream info TOPICS-JOURNAL >/dev/null 2>&1 || $NATS stream add TOPICS-JOURNAL --allow-direct \
  --subjects='a2a.topics.shared.annotations' --storage=file --retention=limits \
  --max-age=720h --max-bytes=5368709120 --discard=old --replicas=1 --max-consumers=64 --defaults

# Heartbeats (agents.hb.>) are core NATS, outside JetStream — no stream.

# KV buckets: runtime-state (who is alive), session-state (the gateway's
# registry; its user is the only writer), cap (reserved for capability
# entries per docs/architecture/09-capability-envelope.md — arms with the
# authority work). Capped at 256MiB each: the streams' max_bytes discipline
# applies to KV too, or unbounded bucket growth eats the file store's
# headroom and stalls every JetStream write.
$NATS kv info runtime-state >/dev/null 2>&1 || $NATS kv add runtime-state --history=1 --replicas=1 --storage=file --max-bucket-size=268435456
$NATS kv info session-state >/dev/null 2>&1 || $NATS kv add session-state --history=1 --replicas=1 --storage=file --max-bucket-size=268435456
$NATS kv info cap           >/dev/null 2>&1 || $NATS kv add cap --history=1 --replicas=1 --storage=file --max-bucket-size=268435456

echo "a2a provisioning complete"
`
}

// buildA2AProvisionJob runs the provisioning script against the rendered NATS.
// The name carries a digest of the rendered spec (a2aProvisionJobName) so a
// changed render is a new Job — Jobs are immutable — and completed runs clean
// themselves up via TTL. The TTL has a known cost, chosen not overlooked: once
// it removes the completed Job, the next reconcile's create-if-absent re-runs
// the (idempotent) script under the same name, so a standing next install
// re-proves its provisioning roughly daily. That churn is one short-lived pod
// a day; the alternative — a completed Job kept forever as the done-marker —
// trades it for permanent clutter and a stale-looking object in every kubectl
// listing.
//
// The digest covers everything this function renders into the spec: the
// script, the image, the uid and security contexts, env, volumes, mounts,
// backoffLimit and the TTL. A superseded Job is not deleted here, and how it
// leaves depends on how far it got. A completed one leaves by TTL; one whose
// pod ran and failed runs out its backoffLimit and then leaves by TTL; one
// whose pod never ran — an unpullable image, an unschedulable pod, an
// admission refusal — has no terminal condition for the TTL to start from
// and stays until the mode flips or the agent is deleted, holding one slot
// in the namespace pod quota the whole time. That last case is the image
// override scenario this digest exists for, so deleting superseded
// generations by label is owed, not merely nice. What holds today: the
// status scan in reconcileA2A reads the current name only, so a stale
// failure does not park the phase, and cleanupA2A deletes by label, so a
// mode flip removes every generation at once.
//
// What the digest does not cover is what is on the bus. Creation is
// create-only convergence: the script's `info || add` lines make re-runs
// clean but do NOT edit a stream that already exists, so a retention or
// subject change in a later payload reaches fresh installs only. Migrating an
// existing install is a manual `nats stream edit` — stage 1 accepts that and
// says it here rather than implying the digest-rename re-provisions.
func buildA2AProvisionJob(agent *agentv1alpha1.PlatformAgent) *batchv1.Job {
	script := a2aProvisionScript(agent)

	job := &batchv1.Job{
		TypeMeta:   metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"},
		ObjectMeta: metav1.ObjectMeta{Namespace: agent.Namespace, Labels: a2aLabels(agent, "provision")},
		Spec: batchv1.JobSpec{
			BackoffLimit:            ptr.To(int32(20)),
			TTLSecondsAfterFinished: ptr.To(int32(86400)),
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: a2aLabels(agent, "provision")},
				Spec: corev1.PodSpec{
					RestartPolicy: corev1.RestartPolicyOnFailure,
					// Its own ServiceAccount, holding no RBAC at all: the
					// token exists to authenticate to the bus, not to talk to
					// the API server. Automount stays off and the bus token is
					// an explicit projected volume, so the only credential in
					// this pod is the audience-bound one it actually needs.
					ServiceAccountName:           a2aProvisionServiceAccountName(agent),
					AutomountServiceAccountToken: ptr.To(false),
					SecurityContext: &corev1.PodSecurityContext{
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
						RunAsNonRoot:   ptr.To(true),
						RunAsUser:      ptr.To(int64(1000)),
					},
					// The nats CLI wants a writable HOME for its context
					// directory even when every call passes --server, so the
					// hardened read-only root needs somewhere to point it.
					Volumes: []corev1.Volume{a2aBusTokenVolumeSource(), {
						Name:         "tmp",
						VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
					}},
					Containers: []corev1.Container{{
						Name:            "provision",
						Image:           a2aProvisionImage(),
						Command:         []string{"sh", "-c", script},
						SecurityContext: hardenedSecurityContext(),
						// nats-box ships WORKDIR /root and declares no USER,
						// so it expects to run as root (measured with
						// `crane config` on 0.14.5). The pod above runs it as
						// 1000, which cannot so much as stat a 0700 root-owned
						// directory: the Job died on "stat .: permission
						// denied" after printing its provisioning JSON, and a
						// fresh next install came up with a healthy bus and no
						// streams at all (#1259).
						//
						// An image's WORKDIR is chosen for the user that image
						// expects, so a render overriding the user owns the
						// working directory too. See hardenedSecurityContext().
						// This container wants a writable one rather than
						// merely a traversable one, because it is also the nats
						// CLI's HOME.
						WorkingDir: a2aProvisionWritablePath,
						Env: []corev1.EnvVar{{
							Name: "HOME", Value: a2aProvisionWritablePath,
						}, {
							Name: "XDG_CONFIG_HOME", Value: a2aProvisionWritablePath,
						}, {
							// The ServiceAccount this Job claims to be.
							// Unverified by construction: the callout
							// derives the real identity from the TokenReview
							// and never reads this. It is here so the
							// connection is legible in a server-side log,
							// not as any part of the decision.
							Name:  "BUS_USER",
							Value: a2aServiceAccountName(agent.Namespace, a2aProvisionServiceAccountName(agent)),
						}},
						VolumeMounts: []corev1.VolumeMount{
							{Name: "tmp", MountPath: a2aProvisionWritablePath},
							a2aBusTokenVolumeMount(),
						},
					}},
				},
			},
		},
	}
	job.Name = a2aProvisionJobName(agent, job.Spec)
	return job
}

// a2aProvisionJobName derives the provision Job's name from a digest of its
// rendered spec. The name is the only lever the operator has on this object:
// a Job's pod template is immutable and reconcileA2A creates the Job only when
// nothing exists under that name, so a rendered change reaches an existing
// install only by producing a new name. Until #1347 the digest covered the
// script alone, and a change to anything else in the pod spec — the image,
// the uid, a securityContext field, env, a mount, WorkingDir — rendered a Job
// with the name already on the cluster and silently never took effect; the
// #1259 WorkingDir fix sat undelivered on a live install until someone deleted
// the Job by hand. The digest is over the whole JobSpec rather than the
// template alone because backoffLimit and the TTL are exactly as unreachable
// under create-only convergence.
//
// json.Marshal is the serializer because it is deterministic for these
// types: struct fields in declaration order, map keys sorted (the template's
// labels are the only map), and nothing in the render is time- or
// randomness-derived — the one Secret reference is by name and key, not by
// value. Determinism is the property that matters most here: a digest that
// moved between two renders of the same agent would create a Job on every
// reconcile, which TestA2AProvisionJobNameIsDeterministic pins. No error
// return, for the reason scopedSAPoolJSON gives: every field is an API type
// the server itself round-trips through JSON, a builder has nowhere to put an
// error, and a Marshal failure would show up as every render digesting the
// same bytes, which TestA2AProvisionJobNameTracksThePodSpec catches.
func a2aProvisionJobName(agent *agentv1alpha1.PlatformAgent, spec batchv1.JobSpec) string {
	rendered, _ := json.Marshal(spec)
	sum := sha256.Sum256(rendered)
	return agent.Name + a2aProvisionJobNameInfix + hex.EncodeToString(sum[:])[:a2aProvisionJobNameHashLength]
}

// defaultA2AMaxSessions is spec.harness.tuning.maxSessions when unset; the
// CRD field's comment carries the sizing rationale. Keep it in step with the
// gateway's own default (a2a/gateway/config.go, arriving with the gateway
// PR) - the operator renders the value explicitly onto A2A_MAX_SESSIONS, so
// the gateway's own constant only governs runs outside the operator (the
// playground path).
const defaultA2AMaxSessions = 10

// a2aQuotaHeadroom is what the namespace pod quota adds above the gateway's
// cap. The quota is namespace-wide because that is the only shape a hostile
// pod-creator cannot dodge (ResourceQuota scopes select on fields the
// creator writes), so it must leave room for everything else that
// legitimately runs here: the rendered stack and its neighbors (operator,
// agent pod, gateway, NATS, LiteLLM, dashboard), Job pods (provision, seed),
// rollout surge doubling a Deployment for a moment, and the gateway's
// count-then-create overshoot. Fifteen covers roughly ten standing pods plus
// surge; if the base install grows past that, raise this before anything
// user-visible starts failing admission.
const a2aQuotaHeadroom = 15

func resolveA2AMaxSessions(agent *agentv1alpha1.PlatformAgent) int {
	if limits := agentTuning(agent); limits != nil && limits.MaxSessions != nil {
		return *limits.MaxSessions
	}
	return defaultA2AMaxSessions
}

func a2aSessionQuotaName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-a2a-session-quota"
}

// buildA2ASessionQuota is the enforcement half of the session-pod bound; the
// gateway's A2A_MAX_SESSIONS cap is the usability half. The distinction is
// the point: the gateway counts and refuses so users get an honest chat
// reply, but the thing being bounded is the gateway itself - a compromised
// or buggy gateway ignores its own cap and cannot ignore this quota, whose
// admission check the API server runs and whose object the gateway's Role
// cannot touch. Sized above the cap so the gateway hits its own limit first
// and nobody legitimate ever sees the admission failure.
//
// `pods` (not count/pods) is deliberate: it counts non-terminal pods only,
// matching the gateway's LiveSessions denominator, so a finished worker
// awaiting sweep does not hold a slot. It is also the only key - a
// compute-resource key (requests.*) would force resource requests onto
// every pod in the namespace, which is not this bound's mandate.
func buildA2ASessionQuota(agent *agentv1alpha1.PlatformAgent) *corev1.ResourceQuota {
	limit := int64(resolveA2AMaxSessions(agent) + a2aQuotaHeadroom)
	return &corev1.ResourceQuota{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ResourceQuota"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aSessionQuotaName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "session-quota"),
		},
		Spec: corev1.ResourceQuotaSpec{
			Hard: corev1.ResourceList{
				corev1.ResourcePods: *resource.NewQuantity(limit, resource.DecimalSI),
			},
		},
	}
}

func buildA2AGatewayServiceAccount(agent *agentv1alpha1.PlatformAgent) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aGatewayName(agent), Namespace: agent.Namespace, Labels: a2aLabels(agent, "gateway")},
	}
}

// buildA2AGatewayRole carries exactly what the gateway's boot and its session
// spawning need, and nothing else. Both rules arrive with their consumer: the
// owner read shipped with the Deployment that reads it, and the pod verbs ship
// here, with the worker image and the A2A_SPAWN_SESSIONS that make the gateway
// use them. A pod-lifecycle grant with nothing spawning pods would be a
// standing grant nobody can point at a caller for.
//
// Namespaced, and pods only. The gateway creates and reaps one pod per
// delegated task in its own namespace; it reads no Secret, no ConfigMap and no
// other namespace.
//
// What it does NOT bound, stated because the next reader will otherwise take
// this rule for a ceiling: `create` on pods is a privilege-escalation
// primitive wherever admission does not constrain the PodSpec, and nothing in
// this repository constrains it here. A gateway that is compromised or
// prompt-steered into building its own PodSpec can name any ServiceAccount in
// the namespace — including the platform agent's, whose Workload Identity
// binding then resolves for that pod — mount any Secret in it, and stamp
// labels no NetworkPolicy selects. spawn.go declining to do any of that is
// what the gateway CHOOSES, not what this grant PERMITS, and the two are not
// the same claim. Narrowing it takes a ValidatingAdmissionPolicy on pod create
// by this subject (no serviceAccountName, no secret volumes,
// automountServiceAccountToken false); that policy does not exist yet and is
// named in this change's PR body as the follow-up it owes.
func buildA2AGatewayRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.Role {
	return &rbacv1.Role{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "Role"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aGatewayName(agent), Namespace: agent.Namespace, Labels: a2aLabels(agent, "gateway")},
		Rules: []rbacv1.PolicyRule{
			// Session pods: create one per delegated task, watch it to
			// completion, delete it on cancel or sweep. No `patch` and no
			// `update` — the gateway never edits a running session pod, and
			// pods/exec is absent, so this is not a route into a worker.
			{
				APIGroups: []string{""},
				Resources: []string{"pods"},
				Verbs:     []string{"create", "get", "list", "watch", "delete"},
			},
			// One read, on one named object: the gateway resolves its own
			// Deployment's UID at boot to build the ownerReference its
			// spawned pods carry (an ownerReference is name+UID, and the UID
			// exists only server-side). resourceNames pins the grant to
			// exactly that Deployment — this is not a deployments read.
			{
				APIGroups:     []string{"apps"},
				Resources:     []string{"deployments"},
				ResourceNames: []string{a2aGatewayName(agent)},
				Verbs:         []string{"get"},
			},
		},
	}
}

func buildA2AGatewayRoleBinding(agent *agentv1alpha1.PlatformAgent) *rbacv1.RoleBinding {
	name := a2aGatewayName(agent)
	return &rbacv1.RoleBinding{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "RoleBinding"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aLabels(agent, "gateway")},
		RoleRef:    rbacv1.RoleRef{APIGroup: "rbac.authorization.k8s.io", Kind: "Role", Name: name},
		Subjects:   []rbacv1.Subject{{Kind: "ServiceAccount", Name: name, Namespace: agent.Namespace}},
	}
}

// buildA2AGatewayDeployment renders the A2A gateway (the chatops gateway of
// docs/designs/spec-chatops-gateway.md: Discord adapter and session manager;
// the program itself arrives in its own PR). It is expected to crash-loop until
// the gateway image is reachable and the discord-bot Secret is created — both
// are optional references so the render never blocks the rest of the stack.
func buildA2AGatewayDeployment(agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	name := a2aGatewayName(agent)
	labels := a2aLabels(agent, "gateway")
	selector := map[string]string{"app": name}
	podLabels := map[string]string{"app": name}
	for k, v := range labels {
		podLabels[k] = v
	}

	return &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: labels},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr.To(int32(1)),
			Selector: &metav1.LabelSelector{MatchLabels: selector},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: podLabels},
				Spec: corev1.PodSpec{
					// The gateway runs as its own ServiceAccount with the
					// narrow Role above - the token this automounts is
					// exactly that grant, nothing ambient.
					ServiceAccountName:           a2aGatewayName(agent),
					AutomountServiceAccountToken: ptr.To(true),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot:   ptr.To(true),
						RunAsUser:      ptr.To(int64(1000)),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: []corev1.Container{{
						Name:  "gateway",
						Image: a2aGatewayImage(),
						// Same rule as the provision container, caught by the
						// same pass: the image is distroless nonroot, which
						// ships WORKDIR /home/nonroot owned 0700 by 65532, and
						// the pod above runs it as 1000. Latent rather than
						// broken because the gateway binary never stats ".",
						// which is luck rather than a guard. "/" is 0755 on
						// that image and the gateway needs no writable cwd --
						// it wants a directory it can traverse, not one it can
						// write.
						WorkingDir: "/",
						Env: []corev1.EnvVar{
							{Name: "NATS_URL", Value: a2aNATSClientURL(agent)},
							{Name: "NATS_USER", Value: "gateway"},
							{Name: "NATS_PASSWORD", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
								LocalObjectReference: corev1.LocalObjectReference{Name: a2aCredsSecretName(agent)},
								Key:                  a2aGatewayPasswordKey,
							}}},
							// Created by hand at install time (the bot token is
							// operator input, never repo content); the
							// reference is optional so the pod schedules
							// before it.
							{Name: "DISCORD_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
								LocalObjectReference: corev1.LocalObjectReference{Name: "discord-bot"},
								Key:                  "token",
								Optional:             ptr.To(true),
							}}},
							// Rendered explicitly even when the CR is silent:
							// the number a `kubectl describe` reader sees is
							// the same one the session quota was sized above,
							// so the two halves cannot drift apart silently.
							{Name: "A2A_MAX_SESSIONS", Value: strconv.Itoa(resolveA2AMaxSessions(agent))},
							// Arms the spawner. The gateway shipped its
							// session-spawn path dark behind this flag; the
							// worker image it spawns and the Role that lets
							// it are in this same change, so the flag flips
							// where all three become true together.
							{Name: "A2A_SPAWN_SESSIONS", Value: "true"},
							// The image those sessions run. Rendered even
							// when it matches the gateway's own default, so
							// the operator-side override reaches it.
							{Name: "A2A_WORKER_IMAGE", Value: a2aWorkerImage()},
							// The Secret the spawner projects the bus
							// password from. The gateway's baked default
							// spells it for a CR named platform-agent, so on
							// any install that renames the CR every session
							// pod would wedge in CreateContainerConfigError
							// on a Secret that does not exist — and wedge
							// silently, because a pod that never runs never
							// reaches a terminal phase for the sweeper to
							// find, holding its session slot until the
							// deadline. Same travel-together rule as the
							// namespace and the owner.
							{Name: "A2A_NATS_CREDS_SECRET", Value: a2aNATSName(agent) + "-creds"},
							// The namespace from the downward API, not a baked
							// default: the boot-time owner resolution below
							// reads the gateway's own Deployment in THIS
							// namespace.
							{Name: "POD_NAMESPACE", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{
								FieldPath: "metadata.namespace",
							}}},
							// The attribution salt is SESSION_KV_SALT, the
							// same Secret key the platform agent hashes
							// session metadata with — one human, one
							// pseudonym, on the bus and in session metadata,
							// or the cross-surface audit join silently yields
							// nothing. Same resolver as the agent render,
							// same optional posture: a pod without it
							// degrades to the gateway's derived fallback, the
							// recorded deviation.
							{Name: "SESSION_KV_SALT", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: sessionKVSaltSecretRef(agent)}},
							// The gateway's own Deployment: spawned session
							// pods carry an ownerReference to it, so
							// Kubernetes GC reaps sessions when cleanupA2A —
							// or anything else — deletes the gateway. The
							// Role above grants the one get this needs.
							{Name: "A2A_OWNER_DEPLOYMENT", Value: name},
						},
						VolumeMounts: []corev1.VolumeMount{{
							Name: "principal-map", MountPath: "/etc/a2a/principal-map", ReadOnly: true,
						}},
						SecurityContext: hardenedSecurityContext(),
					}},
					Volumes: []corev1.Volume{{
						Name: "principal-map",
						VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
							LocalObjectReference: corev1.LocalObjectReference{Name: "principal-map"},
							Optional:             ptr.To(true),
						}},
					}},
				},
			},
		},
	}
}

// a2aProvisionState reports where the provision Job stands, because nothing
// watches Jobs (a Job watch would mean a cluster-wide informer every install
// pays for; see a2aReader). Pending drives a requeue so completion — or the
// TTL removing a finished Job — is noticed without an unrelated event; failed
// drives a Degraded status so a dead bus is visible in `kubectl describe`
// rather than sitting behind a Ready phase.
type a2aProvisionState struct {
	done    bool
	failed  bool
	message string

	// AuthMapVersion is the identity-map version this reconcile rendered.
	// BusCredentialsReady is the callout confirming it is serving this
	// value, so it has to travel out of the render to the status write.
	AuthMapVersion string
}

// a2aSessionDNSClusterIPs is the resolved cluster DNS VIP list for the session
// fence's DNS rule. Ungated through the shared helper: spec.networkPolicy
// .enabled withholds the agent's own gateway policy and nothing else, so a
// profile that returned early on that flag would pin this rule to the
// fallback VIP and discard a documented override on a policy that is still
// enforcing. Nor does the flag switch the fence off — it is a knob about the
// agent pod's policy, and reading it as permission to unfence the workers
// would make delegation the way around the agent's own allowlist, which is
// the hole this fence closes.
func (r *PlatformAgentReconciler) a2aSessionDNSClusterIPs(ctx context.Context, agent *agentv1alpha1.PlatformAgent) []string {
	return r.ungatedDNSClusterIPs(ctx, agent)
}

// reconcileA2ANetworkFences applies the two NetworkPolicies that fence the
// next stack: the bus's ingress policy and the session pods' egress one.
//
// Separate from the rest of reconcileA2A because a NetworkPolicy is not
// rendering, it is a guardrail, and #1247 settled what that distinction costs:
// a policy that stops being reconciled is one an operator can delete
// permanently, and nothing selecting a Pod does not leave it restricted, it
// leaves NetworkPolicy permitting all egress. Every refusal path in Reconcile
// returns before reconcileA2A is reached, so the fences needed the same rescue
// reconcileAgentNetworkGuardrails already gives <name>-gateway-netpol and
// <name>-sandbox-metadata-deny — which is the caller that reaches this on a
// refusal.
//
// The session fence is the one that makes this worth the split. A session pod
// runs worker code the model steers, and buildA2ASessionNetworkPolicy is the
// whole of what confines it: deny-all ingress, and an egress allowlist of DNS,
// the bus, and LiteLLM. Delete it while the CR sits Degraded over an unrelated
// bad CIDR and the confinement is gone from pods that are still running, with
// the status naming the CIDR and saying nothing about the fence.
func (r *PlatformAgentReconciler) reconcileA2ANetworkFences(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	for _, np := range []*networkingv1.NetworkPolicy{
		buildA2ANATSNetworkPolicy(agent),
		buildA2ASessionNetworkPolicy(agent, r.a2aSessionDNSClusterIPs(ctx, agent)),
	} {
		if err := ctrl.SetControllerReference(agent, np, r.Scheme); err != nil {
			return err
		}
		if err := r.applyManaged(ctx, agent, np); err != nil {
			return fmt.Errorf("failed to apply A2A NetworkPolicy %s: %w", np.Name, err)
		}
	}
	return nil
}

// reconcileA2A renders the next stack. Callers gate on renderMode; this
// function assumes the answer was ModeNext.
func (r *PlatformAgentReconciler) reconcileA2A(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (a2aProvisionState, error) {
	state := a2aProvisionState{}

	creds, err := r.ensureA2ACredsSecret(ctx, agent)
	if err != nil {
		return state, fmt.Errorf("failed to ensure A2A NATS creds: %w", err)
	}

	// Before the config, because the config carries the public halves. A
	// nats.conf rendered without them would name an issuer nothing holds and
	// refuse every callout-authenticated connection.
	calloutKeys, err := r.ensureA2ACalloutKeysSecret(ctx, agent)
	if err != nil {
		return state, fmt.Errorf("failed to ensure A2A callout keys: %w", err)
	}

	// The identity map before the server that will be authorizing against
	// it: the callout refuses connections until it is serving a map, so
	// rendering the map first shortens the window in which a restarting bus
	// has a callout with nothing to say.
	authMap, authMapVersion, err := buildA2AAuthMapConfigMap(agent)
	if err != nil {
		return state, fmt.Errorf("failed to render the A2A identity map: %w", err)
	}
	if err := ctrl.SetControllerReference(agent, authMap, r.Scheme); err != nil {
		return state, err
	}
	if err := r.applyManaged(ctx, agent, authMap); err != nil {
		return state, fmt.Errorf("failed to apply the A2A identity map: %w", err)
	}
	state.AuthMapVersion = authMapVersion

	config := buildA2ANATSConfigSecret(agent, creds, calloutKeys)
	if err := ctrl.SetControllerReference(agent, config, r.Scheme); err != nil {
		return state, err
	}
	if err := r.applyManaged(ctx, agent, config); err != nil {
		return state, fmt.Errorf("failed to apply A2A NATS config: %w", err)
	}

	sts := buildA2ANATSStatefulSet(agent, a2aConfigRolloutHash(agent, creds, calloutKeys))
	if err := ctrl.SetControllerReference(agent, sts, r.Scheme); err != nil {
		return state, err
	}
	if err := r.applyManaged(ctx, agent, sts); err != nil {
		return state, fmt.Errorf("failed to apply A2A NATS StatefulSet: %w", err)
	}

	svc := buildA2ANATSService(agent)
	if err := ctrl.SetControllerReference(agent, svc, r.Scheme); err != nil {
		return state, err
	}
	if err := r.applyManaged(ctx, agent, svc); err != nil {
		return state, fmt.Errorf("failed to apply A2A NATS Service: %w", err)
	}

	// The auth callout, before the fence and before anything that dials the
	// bus. nats.conf now names it as the authority for every non-exempt
	// connection, so a bus standing up without it accepts only the static
	// users and refuses everything else — and refuses it as an Authorization
	// Violation, which reads exactly like a credential problem.
	if err := r.reconcileA2ACallout(ctx, agent); err != nil {
		return state, err
	}

	// Both fences ride reconcileA2ANetworkFences so they appear and disappear
	// with the stack they fence — including the skew freeze, where a frozen,
	// running bus keeps its ingress policy and the workers on it keep their
	// egress one.
	if err := r.reconcileA2ANetworkFences(ctx, agent); err != nil {
		return state, err
	}

	// The session-pod quota, the enforcement half of the bound whose
	// usability half is the gateway's own cap (see buildA2ASessionQuota for
	// why both exist and why the quota sits above the cap).
	quota := buildA2ASessionQuota(agent)
	if err := ctrl.SetControllerReference(agent, quota, r.Scheme); err != nil {
		return state, err
	}
	if err := r.applyManaged(ctx, agent, quota); err != nil {
		return state, fmt.Errorf("failed to apply A2A session ResourceQuota: %w", err)
	}

	// Jobs are immutable, so the provision Job is create-if-absent under its
	// spec-digested name; a changed render — script or pod spec — is a new
	// name and a fresh run. The superseded generation is swept below rather
	// than left to its TTL, for the reason the sweep's own comment gives.
	job := buildA2AProvisionJob(agent)
	if err := ctrl.SetControllerReference(agent, job, r.Scheme); err != nil {
		return state, err
	}
	withCommonLabels(job, agent)
	existing := &batchv1.Job{}
	if err := r.a2aReader().Get(ctx, client.ObjectKeyFromObject(job), existing); err != nil {
		if !errors.IsNotFound(err) {
			return state, err
		}
		if err := r.Create(ctx, job); err != nil {
			return state, fmt.Errorf("failed to create A2A provision Job: %w", err)
		}
	} else {
		for _, cond := range existing.Status.Conditions {
			if cond.Status != corev1.ConditionTrue {
				continue
			}
			switch cond.Type {
			case batchv1.JobComplete:
				state.done = true
			case batchv1.JobFailed:
				state.failed = true
				state.message = fmt.Sprintf(
					"A2A provision Job %s failed (%s: %s); the bus has no streams until it succeeds. Inspect its pod logs; deleting the Job retries.",
					existing.Name, cond.Reason, cond.Message)
			}
		}
	}

	// Superseded generations go now, not by TTL. A Job whose name the render
	// has moved past leaves on its own only if it reached a terminal
	// condition: a completed one by TTL, one whose pod ran and failed by
	// backoffLimit and then TTL. One whose pod never ran — an unpullable
	// image, an unschedulable pod, an admission refusal — has no condition
	// for the TTL to start from and would sit in Pending until a mode flip
	// or agent deletion, holding one slot of the namespace pod quota the
	// whole time (#1389). An unpullable image is the likely way to get there
	// once the name tracks the pod spec (#1347), and a bad script is the way
	// today. The sweep runs after the current Job is ensured above, never
	// before it: a reconcile leaves N+1 provision Jobs for a moment, never
	// zero. The status scan above read the current name only, so a Failed
	// on a generation deleted here never reached the phase.
	if err := r.deleteA2AProvisionJobs(ctx, agent, job.Name); err != nil {
		return state, fmt.Errorf("failed to delete superseded A2A provision Jobs: %w", err)
	}

	// Identity before workload: the gateway pod must not start before the
	// ServiceAccount its pod spec names exists.
	for _, obj := range []client.Object{
		buildA2AGatewayServiceAccount(agent),
		buildA2AGatewayRole(agent),
		buildA2AGatewayRoleBinding(agent),
	} {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return state, err
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return state, fmt.Errorf("failed to apply A2A gateway %T: %w", obj, err)
		}
	}

	dep := buildA2AGatewayDeployment(agent)
	if err := ctrl.SetControllerReference(agent, dep, r.Scheme); err != nil {
		return state, err
	}
	if err := r.applyManaged(ctx, agent, dep); err != nil {
		return state, fmt.Errorf("failed to apply A2A gateway Deployment: %w", err)
	}

	return state, nil
}

// a2aTeardownEntry is one namespaced object cleanupA2A removes, with the reader
// that can see it: the Owns() kinds come from the cache, the rest go through
// a2aReader so no cluster-wide informer starts for a kind nothing watches.
type a2aTeardownEntry struct {
	obj    client.Object
	reader client.Reader
}

// a2aNamespacedTeardown is the ordered list of namespaced objects cleanupA2A
// deletes by name. The order is load-bearing: see the sentinel argument in
// cleanupA2A below.
//
// A function rather than a literal inside cleanupA2A so its length is readable
// from a test. That is what lets the cost test assert the early exit is cheaper
// than the walk it skips, instead of restating how long the walk is and going
// stale the next time the render grows a step.
func (r *PlatformAgentReconciler) a2aNamespacedTeardown(agent *agentv1alpha1.PlatformAgent) []a2aTeardownEntry {
	return []a2aTeardownEntry{
		{&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: a2aGatewayName(agent), Namespace: agent.Namespace}}, r.Client},
		// The auth callout, before the bus it authorizes for. Its Deployment
		// goes first so it stops answering while there is still a server to
		// answer for; the keys Secret goes with it rather than surviving like
		// the per-user creds, because a flip back to today and forward again
		// re-renders nats.conf anyway, and a stale issuer is the one thing
		// that would make every callout answer be refused.
		{&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutName(agent), Namespace: agent.Namespace}}, r.Client},
		{&corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutName(agent), Namespace: agent.Namespace}}, r.Client},
		{&rbacv1.RoleBinding{ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutName(agent), Namespace: agent.Namespace}}, r.a2aReader()},
		{&rbacv1.Role{ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutName(agent), Namespace: agent.Namespace}}, r.a2aReader()},
		{&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutName(agent), Namespace: agent.Namespace}}, r.Client},
		{&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: a2aProvisionServiceAccountName(agent), Namespace: agent.Namespace}}, r.Client},
		{&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutKeysName(agent), Namespace: agent.Namespace}}, r.a2aReader()},
		{&corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: a2aAuthMapName(agent), Namespace: agent.Namespace}}, r.a2aReader()},
		{&rbacv1.RoleBinding{ObjectMeta: metav1.ObjectMeta{Name: a2aGatewayName(agent), Namespace: agent.Namespace}}, r.a2aReader()},
		{&rbacv1.Role{ObjectMeta: metav1.ObjectMeta{Name: a2aGatewayName(agent), Namespace: agent.Namespace}}, r.a2aReader()},
		// ServiceAccount is an Owns() kind, so this read is cached and free.
		{&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: a2aGatewayName(agent), Namespace: agent.Namespace}}, r.Client},
		{&corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: a2aNATSName(agent), Namespace: agent.Namespace}}, r.Client},
		// NetworkPolicy is an Owns() kind (the agent's own policy), so the
		// cached reads are free.
		{&networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: a2aNATSNetpolName(agent), Namespace: agent.Namespace}}, r.Client},
		// The session fence goes after the gateway Deployment above, which is
		// what stops new pods being spawned. It does not close the window:
		// Delete returns as soon as the API server accepts it, and the pods
		// already running are reaped asynchronously by GC through their
		// ownerReference, then by their termination grace. So a flip to today
		// with sessions in flight leaves those workers unfenced for seconds,
		// not for their lifetimes — ordering shortens that window rather than
		// removing it, and removing it would take a foreground delete and a
		// wait this reconcile has no reason to block on.
		{&networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: a2aSessionNetpolName(agent), Namespace: agent.Namespace}}, r.Client},
		{&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: a2aNATSName(agent) + "-config", Namespace: agent.Namespace}}, r.a2aReader()},
		// ResourceQuota is not a watched kind, so the read goes through
		// a2aReader like the Secrets. Deleting it here is safe even with
		// session pods still draining (see the function comment): a quota
		// only gates admission, never running pods.
		{&corev1.ResourceQuota{ObjectMeta: metav1.ObjectMeta{Name: a2aSessionQuotaName(agent), Namespace: agent.Namespace}}, r.a2aReader()},
		// LAST, deliberately: the StatefulSet is this function's sentinel. The
		// early exit above treats its absence as "an earlier pass reached the
		// end", which is only true while nothing is deleted after it.
		{&appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: a2aNATSName(agent), Namespace: agent.Namespace}}, r.Client},
	}
}

// cleanupA2A returns the dark stack to dark when the mode is not next. The
// creds Secret stays (inert data; re-enabling must not re-roll credentials)
// and so does the StatefulSet's PVC (JetStream's file store is the audit
// substrate — flipping a mode is not license to destroy evidence).
//
// Session pods — spawned by the gateway once the worker PR arms spawning —
// are the gateway's, not the operator's: every spawned pod carries an
// ownerReference to the gateway Deployment (A2A_OWNER_DEPLOYMENT above), so
// deleting the gateway here hands any stragglers to Kubernetes GC, with no
// operator exception to the IsControlledBy refusal below.
func (r *PlatformAgentReconciler) cleanupA2A(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	// The early exit. This path runs on every reconcile of every install that
	// is not `next` — forever, on installs that have never rendered an A2A
	// object — so proving "nothing to do" one object at a time is a standing
	// cost for a no-op. Four reads answer it instead of nineteen:
	//
	//   - the StatefulSet, which is deleted LAST below, so its absence means an
	//     earlier pass ran to completion rather than dying partway,
	//   - the gateway Deployment, which the render creates last and this
	//     function deletes first, so it catches a pass that failed immediately,
	//   - the callout keys Secret, which is the FIRST deletable object
	//     reconcileA2A creates — the per-user creds Secret is created before it
	//     and deliberately survives — so a render that died anywhere leaves this
	//     one behind. It covers the identity-map ConfigMap created right after
	//     it for the same reason,
	//   - the config Secret, which held that role before the callout existed.
	//     Kept for the install whose partial render predates the keys Secret:
	//     an operator upgraded across this change and then flipped to `today`
	//     would otherwise step over a config Secret no later object accompanies.
	//
	// Without the third and fourth the exit would step over those objects and
	// leave an A2A object on a `today` install, which is the darkness property.
	// The first two are Owns kinds and free; the two Secret reads are uncached
	// and happen only when the free two both miss.
	//
	// Adding an object to reconcileA2A ahead of the keys Secret means adding it
	// here. TestTheEarlyExitSeesTheResidueOfARenderThatDiedAnywhere walks every
	// prefix of the render and is what makes forgetting it red rather than
	// silent: without the keys Secret below, its writes 3 and 4 fail.
	sentinels := []a2aTeardownEntry{
		{&appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: a2aNATSName(agent), Namespace: agent.Namespace}}, r.Client},
		{&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: a2aGatewayName(agent), Namespace: agent.Namespace}}, r.Client},
		{&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutKeysName(agent), Namespace: agent.Namespace}}, r.a2aReader()},
		{&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: a2aNATSName(agent) + "-config", Namespace: agent.Namespace}}, r.a2aReader()},
	}
	anyPresent := false
	for _, s := range sentinels {
		err := s.reader.Get(ctx, client.ObjectKeyFromObject(s.obj), s.obj)
		if err == nil {
			anyPresent = true
			break
		}
		if client.IgnoreNotFound(err) != nil {
			return err
		}
	}
	if !anyPresent {
		return nil
	}

	// Deployment/StatefulSet/Service/ServiceAccount reads come from the cache —
	// those kinds are already watched (Owns, see SetupWithManager) so the reads
	// are free. Secret, Role/RoleBinding, ResourceQuota and Job reads go through
	// a2aReader: a cached read would start a cluster-wide informer for a kind
	// this controller otherwise never watches, on every install.
	//
	// Those uncached reads are the standing cost of this path, which runs on
	// every reconcile of every today install — see the note on the sweep below.
	for _, entry := range r.a2aNamespacedTeardown(agent) {
		obj := entry.obj
		if err := entry.reader.Get(ctx, client.ObjectKeyFromObject(obj), obj); err != nil {
			if client.IgnoreNotFound(err) != nil {
				return err
			}
			continue
		}
		if !metav1.IsControlledBy(obj, agent) {
			return fmt.Errorf("refusing to delete unowned A2A %T %s/%s", obj, obj.GetNamespace(), obj.GetName())
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, obj)); err != nil {
			return err
		}
	}

	// The callout's ClusterRoleBinding. Cluster-scoped, so it carries no
	// owner reference — the garbage collector treats a cluster-scoped object
	// owned by a namespaced one as an orphan and deletes it at once — which
	// means nothing reclaims it but this. Left behind, it is an A2A-named
	// ClusterRoleBinding on an install that is supposed to look like it has
	// never heard of A2A, and it is the darkness property's most visible
	// residue: cluster-scoped objects are exactly what a security reviewer
	// lists first. The ownership refusal above cannot apply, so it is matched
	// on its labels instead.
	if err := r.deleteA2ACalloutClusterRoleBinding(ctx, agent); err != nil {
		return err
	}

	// Provision Jobs carry a content hash in the name, one per generation
	// that has been rendered here; a mode flip removes every generation.
	return r.deleteA2AProvisionJobs(ctx, agent, "")
}

// deleteA2AProvisionJobs deletes this agent's provision Jobs, found by label
// because their names carry a content hash, except the one named keep. Two
// callers: reconcileA2A passes the current render's name so superseded
// generations go, and cleanupA2A passes "" so a mode flip clears them all.
// One function so the two sweeps cannot drift apart in what they select.
//
// The List is uncached (a2aReader) for the reason that function gives, and
// on the reconcile path it is a standing cost paid once per reconcile of a
// next install — the same shape as the Job Get that precedes it. Sweeping
// only on the pass that created a new generation would be cheaper and would
// miss two cases: an install whose superseded Jobs predate this sweep, which
// never sees a create again under the current name, and a pass whose create
// succeeded and whose sweep then failed.
//
// Background propagation, for the same reason cleanupA2A always used it: the
// pod is what holds the quota slot, and Background hands it to the garbage
// collector the moment the Job is gone rather than pinning the Job under a
// foregroundDeletion finalizer until the pod has left — which would put the
// same Job back in this List on the next pass. A Job already carrying a
// deletionTimestamp is skipped for that reason too. The IsControlledBy guard
// is cleanupA2A's: a Job somebody labelled to look like ours, but which this
// agent does not own, is not ours to delete.
func (r *PlatformAgentReconciler) deleteA2AProvisionJobs(ctx context.Context, agent *agentv1alpha1.PlatformAgent, keep string) error {
	var jobs batchv1.JobList
	if err := r.a2aReader().List(ctx, &jobs, client.InNamespace(agent.Namespace), client.MatchingLabels{
		a2aComponentLabel: a2aProvisionComponent,
		labelInstance:     instanceLabel(agent.Namespace, agent.Name),
	}); err != nil {
		return err
	}
	for i := range jobs.Items {
		job := &jobs.Items[i]
		if job.Name == keep || job.DeletionTimestamp != nil || !metav1.IsControlledBy(job, agent) {
			continue
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, job, client.PropagationPolicy(metav1.DeletePropagationBackground))); err != nil {
			return err
		}
	}
	return nil
}

// deleteA2ACalloutClusterRoleBinding reaps the callout's cluster-scoped grant.
//
// Called from two places, because there are two ways the next stack goes away:
// a flip to today (cleanupA2A) and deletion of the CR itself (handleDeletion).
// Nothing else reclaims it — a cluster-scoped object cannot carry an owner
// reference to a namespaced CR — so missing either path leaves a standing
// TokenReview grant behind forever.
func (r *PlatformAgentReconciler) deleteA2ACalloutClusterRoleBinding(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	crb := &rbacv1.ClusterRoleBinding{ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutClusterRoleBindingName(agent)}}
	if err := r.a2aReader().Get(ctx, client.ObjectKeyFromObject(crb), crb); err != nil {
		return client.IgnoreNotFound(err)
	}
	// Ownership by label, since the refusal the named objects get cannot
	// apply: there is no owner reference to check.
	if crb.Labels[labelInstance] != instanceLabel(agent.Namespace, agent.Name) {
		return fmt.Errorf("refusing to delete unowned A2A ClusterRoleBinding %s", crb.Name)
	}
	return client.IgnoreNotFound(r.Delete(ctx, crb))
}
