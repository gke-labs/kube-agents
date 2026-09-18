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
	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The bus principals, in one place, as data.
//
// This list is the single source for three renders that MUST agree: the
// nats.conf user blocks for the principals that still authenticate statically,
// the auth callout's identity-to-permissions map for the ones that authenticate
// with a Kubernetes ServiceAccount token, and the NATS_USER a client is handed
// so it can set the inbox prefix its own grants require. Before the callout,
// those three lived in a config string, a Secret and a container env block, and
// nothing but review connected them. A grant list that disagrees with the user
// name in any of the three produces a client that authenticates, publishes, and
// then hangs forever on a reply its subscribe grant does not cover — the
// hardest failure in this deployment to read from the outside, and the one W6
// found twice.
//
// Deny-by-default is unchanged: arming the callout changed who vouches for an
// identity, not what that identity may say. What this change does move is the
// session: it is a principal of its own now, it authenticates through the
// callout, and it is off the shared worker credential. `worker` itself is gone
// — A5 split it into the two workloads that were sharing it, `agent` and
// `bridge`, and TestTheWorkerCredentialIsGone refuses its return.

// a2aAuthMode says how a principal proves who it is.
type a2aAuthMode int

const (
	// a2aAuthCallout: the principal presents a projected ServiceAccount
	// token and the callout resolves it against the cluster. No shared
	// secret exists for it anywhere.
	a2aAuthCallout a2aAuthMode = iota

	// a2aAuthStatic: the principal stays in nats.conf with a rendered
	// password and is listed in auth_users, which exempts it from the
	// callout. Every static principal carries a reason below, and the
	// reason is either "no Kubernetes identity exists to present" or "it
	// cannot have one".
	a2aAuthStatic
)

// a2aIdentity is one bus principal: what it is called on the bus, what it may
// say, and how it proves it is itself.
type a2aIdentity struct {
	// user is the NATS user name. It is also the principal's inbox prefix
	// (_INBOX.<user>.>) and therefore appears in its own subscribe list;
	// a2aIdentities() is what keeps those two in step.
	user string

	// account is the NATS account the principal lands in. The account is
	// the tenant boundary and the blast-radius container.
	account string

	auth a2aAuthMode

	// credsKey names this principal's entry in the creds Secret. Set only
	// for a2aAuthStatic.
	credsKey string

	// serviceAccount is the KSA whose token authenticates this principal,
	// as TokenReview spells it. Set only for a2aAuthCallout.
	serviceAccount string

	// comment is rendered above this principal's block in nats.conf, for the
	// static ones, or beside its entry in the map. The rationale belongs
	// where the operator reading the live config will find it, not only
	// here.
	comment string

	// narrowing, when set, means this principal's grants are NOT rendered
	// into the map: the callout derives them at mint time from a claim the
	// API server attested about the workload connecting. a2aNarrowingPod is
	// the only value. A narrowed principal MUST leave publish and subscribe
	// empty, and the callout refuses the whole map if it does not — see
	// sessionIdentity for why that is fail-closed rather than fussy.
	narrowing string

	publish   []string
	subscribe []string
}

// a2aNarrowingPod marks a principal whose grants derive from the attested pod
// name. It must match the callout's NarrowingPod; the two modules cannot import
// each other, so the shared fixture is what keeps them honest.
const a2aNarrowingPod = "pod"

// Account names. One application account per scope; $SYS for operators and
// monitoring, which no agent ever authenticates into.
const (
	a2aAccountApp = "APP"
	a2aAccountSys = "SYS"
)

// The two principal names A5 split `worker` into. They are spelled once here
// because each is used three times over — the identity's user field, the inbox
// prefix its own grants carry, and (for the agent) the value the operator
// renders into the container so the client can pin that same prefix.
const (
	// a2aAgentBusUser is the platform agent container's principal. The `a2a`
	// CLI reads it back from a2aBusUserEnv; a2a/lib's EnvBusUser is the other
	// half of that contract, duplicated rather than imported because the two
	// modules cannot see each other.
	a2aAgentBusUser = "agent"

	// a2aBridgeUser is the Hermes bridge sidecar's principal. Static, not
	// callout — see bridgeIdentity for why a token cannot separate it from
	// the container above.
	a2aBridgeUser = "bridge"

	// a2aBusUserEnv carries a2aAgentBusUser into the agent container. Not a
	// credential: it selects the inbox prefix the client pins, and the grants
	// come from the callout's answer about the ServiceAccount, not from this.
	// Setting it wrong gets a client that connects and then hangs on every
	// reply, which is why it is reserved against spec.deployment.env and
	// against plugin env both.
	a2aBusUserEnv = "A2A_BUS_USER"

	// a2aBusTokenFileEnv is NOT rendered by the operator: the client falls
	// back to a2aBusTokenPath/a2aBusTokenFile, which is where the projection
	// lands. It is named here only so the reservation can name it. Setting it
	// points the client at a different file to present as its bearer token,
	// and a2a/cmd/a2a/main.go prefers it unconditionally with no fallback --
	// deliberately, because a caller who names a token file and is quietly
	// logged in as something else is the failure that ordering prevents. The
	// blast radius is denial rather than escalation, and the audience is the
	// whole of why: automountServiceAccountToken is false, but this container
	// is not tokenless -- it holds the broker-audience projection at
	// credentialProxyTokenMountPath, and a CR can put more files in reach --
	// and every one of them is minted for somebody else's audience, which the
	// bus refuses at connect. It is still the variable that decides WHICH
	// bearer token this container presents, so it is reserved on the same
	// argument as a2aBusUserEnv above.
	//
	// Mirrors lib.EnvBusTokenFile (a2a/lib/credentials.go), which is the
	// reader. Separate modules, so the spelling is held by the conformance
	// suite rather than by the compiler:
	// test_C1_the_reserved_bus_token_file_env_is_spelled_the_same_in_both_modules
	// compares the two constants, and checks that buildPodTemplateSpec's
	// plugin-env drop still refuses this name by way of this constant rather
	// than a literal of its own.
	a2aBusTokenFileEnv = "A2A_BUS_TOKEN_FILE" // #nosec G101 -- Environment variable name, not hardcoded credentials
)

// a2aBridgeAddressee is the addressee the bridge executes for, and the only one
// its grants name. It is the bridge's BRIDGE_PROFILE default
// (a2a/cmd/hermes-bridge/main.go, defaultProfile) — the two are one value, and a
// deployment that overrides the env without widening this grant gets a bridge
// that reads the other addressee's prompts and then silently drops them.
//
// The read half happens. The bridge consumes through a pull consumer, so the
// filter subject travels in the CONSUMER.CREATE request body and
// `$JS.API.CONSUMER.CREATE.TASKS.>` does not scope it — the inbound leg is
// delivered, on this user's own inbox prefix, whatever addressee it names.
// Same mechanism as the "reading is not narrowed" paragraph in bridgeIdentity.
//
// Hermes never runs it, though. accept (a2a/hermes-bridge/bridge.go) publishes
// `submitted` on the addressee's events subject before it queues the task for a
// worker, and that publish is where the grant bites: the submission is dropped
// there and no subprocess is ever spawned. The refusal arrives as a timeout on
// the JetStream API reply rather than as a permission error, so what the
// submitter sees is a task that never got a terminal event at all.
// TestBridgeJetStreamGrantOnARealServer's refused table carries the events
// publish for an addressee this grant does not name.
const a2aBridgeAddressee = "platform"

// a2aServiceAccountName spells a KSA the way the Kubernetes TokenReview API
// reports it, which is how the callout's map is keyed. Built here rather than
// in the map renderer so the operator and the callout cannot disagree about the
// format of the thing they are matching on.
func a2aServiceAccountName(namespace, name string) string {
	return "system:serviceaccount:" + namespace + ":" + name
}

// a2aIdentities returns every bus principal for this agent.
//
// Ordering is stable and meaningful: it is the order the map and the config are
// rendered in, so a diff of either is a diff of intent rather than of map
// iteration. The eval principal is the one conditional entry: it sits between
// web and sys when the controller runs with A2A_EVAL_PRINCIPAL=true and is
// absent otherwise, so the two renders differ by exactly that block.
func a2aIdentities(agent *agentv1alpha1.PlatformAgent) []a2aIdentity {
	ns := agent.Namespace
	ids := []a2aIdentity{
		gatewayIdentity(agent, ns),
		provisionIdentity(agent, ns),
		sessionIdentity(agent, ns),
		agentIdentity(agent, ns),
		bridgeIdentity(),
		seedIdentity(),
		webIdentity(),
	}
	if a2aEvalPrincipalEnabled() {
		ids = append(ids, evalIdentity())
	}
	return append(ids, sysIdentity())
}

// gateway: task requester, chat-session supervisor, session-registry owner.
// Production scopes supervisor publish to sessions the gateway spawned;
// statically that collapses to the task-supervisor wildcard.
//
// The supervisor's terminal goes on `…supervisor`, the executor's events on
// `…events`, and the gateway holds publish on the first and not the second.
// Before the split it held `a2a.tasks.*.*.events`, which made every executor's
// subject two-writer: a session that terminated its own task wearing the
// gateway's `from` was indistinguishable on replay from the gateway declaring
// it dead. Now the subject says who wrote there and NATS enforces it at
// publish; `from` is checked for agreement by consumers, never trusted.
func gatewayIdentity(agent *agentv1alpha1.PlatformAgent, ns string) a2aIdentity {
	_ = ns
	return a2aIdentity{
		user:    "gateway",
		account: a2aAccountApp,
		comment: "task requester, chat-session supervisor, session-registry owner.\n" +
			"STATIC, and this one is a sequencing fact rather than a property of\n" +
			"the gateway. It has a ServiceAccount and could authenticate with it\n" +
			"tomorrow; what it does not yet have is a client that presents a token\n" +
			"instead of a password, because the gateway program lands separately\n" +
			"from this render. Moving the identity before the program that uses it\n" +
			"would refuse the gateway at connect on every install.",
		auth:     a2aAuthStatic,
		credsKey: a2aGatewayPasswordKey,
		// $JS.ACK / $JS.FC.> are the delivery path's reply subjects: an
		// explicit ack is a publish to $JS.ACK.<stream>.<consumer>...,
		// and push flow control answers on $JS.FC.>. Without them a
		// consumer redelivers forever while TCP health stays green.
		//
		// The ack grant is scoped to the streams this user consumes with
		// explicit ack (the gateway-relay durable on TASKS; everything
		// else it reads is ordered/ack-none). An ack subject names a
		// stream and a CONSUMER, never the caller, so unscoped
		// $JS.ACK.> would let this user +TERM another principal's
		// in-flight delivery on ANY stream.
		publish: []string{
			"a2a.tasks.*.*.in",
			"a2a.tasks.*.*.supervisor",
			"$KV.session-state.>",
			"$JS.API.>",
			"$JS.ACK.TASKS.>",
			"$JS.FC.>",
			"_INBOX.gateway.>",
		},
		subscribe: []string{
			"a2a.tasks.*.*.events",
			"a2a.tasks.*.*.supervisor",
			"a2a.agents.>",
			"agents.hb.>",
			"$KV.session-state.>",
			"_INBOX.gateway.>",
		},
	}
}

// The `agent` principal below is the correction of a claim this file used to
// make. It said there was none, that the platform agent's pod held the widest
// reach in the namespace on the shared `worker` credential, and that giving it
// a token-authenticated name of its own was "not this change". It is this
// change: agentIdentity keys on the agent pod's own ServiceAccount, and the
// grant list is the agent-side reader the render had always claimed
// (platformagent_manifests.go, the A2A bus block) and never had.

// a2aJetStreamSurfaceRationale, kept as prose rather than a symbol because it
// is the reason every callout principal below enumerates its JetStream API
// subjects one at a time instead of taking $JS.API.>:
//
// A grant list is a capability surface for JetStream, not a read/write
// distinction. Subject permissions cannot see a request BODY, and a consumer's
// target stream and its delivery subject are both body fields. So $JS.API.>
// hands back everything the subject lists withhold — a push consumer on TASKS
// delivering into a subject the principal CAN subscribe to reads the whole task
// plane, and STREAM.DELETE destroys it. Both demonstrated live against the
// rendered config, and the same escape the web user's comment below records.

// provision: the operator-rendered Job that creates the streams, buckets and
// starter topics.
//
// Nothing on the task plane — a provisioner that can publish tasks is a
// provisioner that can impersonate the fabric. No ack grant at all: it creates
// no consumers, so provisioning is $JS.API requests and the starter topics are
// publishes, and nothing here ever acks.
func provisionIdentity(agent *agentv1alpha1.PlatformAgent, ns string) a2aIdentity {
	return a2aIdentity{
		user:           "provision",
		account:        a2aAccountApp,
		comment:        "creates the streams, buckets and starter topics; nothing on the task plane",
		auth:           a2aAuthCallout,
		serviceAccount: a2aServiceAccountName(ns, a2aProvisionServiceAccountName(agent)),
		// Enumerated per object, for the reason spelled out in
		// a2aJetStreamSurfaceRationale above: $JS.API.> would let this
		// principal create a
		// consumer that delivers TASKS into its own inbox, and delete any
		// stream on the bus. It provisions - it creates the four streams
		// and three buckets, idempotently, with an info-then-add - so it
		// needs CREATE and INFO on exactly those and nothing else. A KV
		// bucket is a stream named KV_<bucket>, which is why those appear
		// in stream form.
		//
		// Notably absent: STREAM.DELETE, STREAM.PURGE and the whole
		// CONSUMER surface. A provisioner that can delete what it created
		// is a provisioner that can destroy the audit substrate.
		publish: []string{
			"a2a.topics.agent.platform.upgrade-readiness",
			"a2a.topics.shared.blueprint",
			"a2a.topics.shared.annotations",
			"$JS.API.INFO",
			// The stream-name lookup, which is not optional for the way the
			// script is written. It provisions idempotently with
			// `stream info X || stream add X`, and on a fresh bus the CLI
			// answers a miss by trying to LIST the streams so it can offer a
			// choice. Without this grant that list is refused, so the info
			// call does not return not-found - it hangs to its deadline, once
			// per object, on the first run of every install, and logs a
			// timeout rather than the absence it actually found. Read-only,
			// and only over the account this principal already provisions.
			"$JS.API.STREAM.NAMES",
			"$JS.API.STREAM.LIST",
			"$JS.API.STREAM.CREATE.TASKS",
			"$JS.API.STREAM.CREATE.DIRECTORY",
			"$JS.API.STREAM.CREATE.TOPICS-STATE",
			"$JS.API.STREAM.CREATE.TOPICS-JOURNAL",
			"$JS.API.STREAM.CREATE.KV_runtime-state",
			"$JS.API.STREAM.CREATE.KV_session-state",
			"$JS.API.STREAM.CREATE.KV_cap",
			"$JS.API.STREAM.INFO.TASKS",
			"$JS.API.STREAM.INFO.DIRECTORY",
			"$JS.API.STREAM.INFO.TOPICS-STATE",
			"$JS.API.STREAM.INFO.TOPICS-JOURNAL",
			"$JS.API.STREAM.INFO.KV_runtime-state",
			"$JS.API.STREAM.INFO.KV_session-state",
			"$JS.API.STREAM.INFO.KV_cap",
			"_INBOX.provision.>",
		},
		subscribe: []string{
			"a2a.topics.>",
			"_INBOX.provision.>",
		},
	}
}

// session: one spawned session pod, per incarnation.
//
// This is the entry with no grants, and the empty lists are the point.
//
// Every session pod runs as this one ServiceAccount. That is deliberate: a KSA
// per conversation would be a credential-bearing API object created and reaped
// per chat, which is the orphan class the pod sweep just closed, in its most
// dangerous form. So the ServiceAccount cannot tell two sessions apart — and it
// does not have to. The spawner projects a token bound to the pod, TokenReview
// reports that pod's name and UID, and the callout builds the session's entire
// grant set from the attested name: its own events subject, its own three named
// consumers on TASKS, its own inbox, and nothing else.
//
// Why the grants are not written here and then narrowed. If this entry carried
// the real grants, then one code path that forgot to narrow — or one map edit by
// someone who did not know the code narrowed it — would hand every session pod
// the whole list at once. That is the shared `worker` credential reborn under a
// new name, and it would look correct in review. An entry that grants nothing
// on its own cannot be widened by editing the map: no claim, no grants, no
// connection. The callout refuses at parse if this entry ever gains a grant.
//
// What a reader of the live ConfigMap sees here is therefore an entry that
// appears to do nothing, and the comment beside it has to carry the whole
// explanation, because the grants cannot.
func sessionIdentity(agent *agentv1alpha1.PlatformAgent, ns string) a2aIdentity {
	return a2aIdentity{
		user:    "session",
		account: a2aAccountApp,
		comment: "one spawned session pod, per incarnation. Its grants are DERIVED, not listed: " +
			"every session runs as this one ServiceAccount, and the callout scopes each connection " +
			"to the pod the API server attested it was minted into - its own task's events, its own " +
			"three consumers, its own inbox. The empty lists here are load-bearing: an entry that " +
			"granted anything on its own could be widened by editing this map, which is how the " +
			"shared worker credential would come back.",
		auth:           a2aAuthCallout,
		serviceAccount: a2aServiceAccountName(ns, a2aSessionServiceAccountName(agent)),
		narrowing:      a2aNarrowingPod,
	}
}

// agent: the platform agent container, on the blackboard and nowhere else.
//
// This is what the platform agent pod authenticates as since A5. It replaces
// half of `worker`: the operator used to render NATS_USER=worker and the
// worker-password SecretKeyRef straight into the agent container, so the
// widest-reach workload in the namespace held an executor credential for every
// addressee on the bus in order to run `a2a topics read`.
//
// Callout-authenticated, keyed on the agent pod's own ServiceAccount. That is
// the narrowing this file used to defer to "the change that moves the agent pod
// onto a projected token" — a2aBusTokenVolumeSource is mounted into the agent
// container now, and the CLI reads it through lib.WithKSAToken.
//
// The grant is the blackboard and the reply path, and deliberately nothing on
// the task plane: no publish on a2a.tasks.>, no subscribe on it, no consumer
// verb anywhere (see a2aAgentJetStreamGrants). An agent that wants work done by
// another agent asks the gateway, which is a task the gateway authors; it does
// not write another addressee's event stream itself.
//
// Keyed on agentServiceAccountName, which spec.security.serviceAccountName can
// override. Two consequences worth naming. The map entry follows the override
// (the operator renders both from the same call, so they cannot drift). And an
// override that collides with another principal's ServiceAccount would put two
// entries under one key, which validateA2AAuthMapIdentities refuses by returning
// "duplicate serviceAccount" — buildA2AAuthMapConfigMap fails, and with it the
// reconcile. Note where that refusal is NOT: the webhook validates
// spec.security.serviceAccountName against restrictedServiceAccounts only, so
// the CR is admitted and the operator then wedges on
// "failed to render the A2A identity map" rather than the apply being
// rejected. Fail-closed, but the diagnosis is a controller log rather than an
// admission error. TestPrincipalsAreDistinct holds the default
// render; TestAnOverriddenServiceAccountThatCollidesIsRefused holds this path.
func agentIdentity(agent *agentv1alpha1.PlatformAgent, ns string) a2aIdentity {
	publish := []string{
		"a2a.topics.agent.platform.upgrade-readiness",
		"a2a.topics.shared.blueprint",
		"a2a.topics.shared.annotations",
	}
	publish = append(publish, a2aAgentJetStreamGrants()...)
	publish = append(publish, "_INBOX."+a2aAgentBusUser+".>")

	return a2aIdentity{
		user:           a2aAgentBusUser,
		account:        a2aAccountApp,
		auth:           a2aAuthCallout,
		serviceAccount: a2aServiceAccountName(ns, agentServiceAccountName(agent)),
		comment: "the platform agent container. Reads the whole blackboard and writes three\n" +
			"topics on it. Beyond that, exactly two things: STREAM.INFO and DIRECT.GET on\n" +
			"the two TOPICS streams, which carry the blackboard and nothing else, and its\n" +
			"own reply inbox. No task plane either way, and no JetStream consumer verb\n" +
			"anywhere, so it cannot ask the server to deliver a stream. Replaced this\n" +
			"pod's share of the retired shared `worker` credential.",
		publish: publish,
		subscribe: []string{
			"a2a.topics.>",
			"_INBOX." + a2aAgentBusUser + ".>",
		},
	}
}

// bridge: the Hermes bridge sidecar, executor for the `platform` addressee.
//
// The other half of `worker`. The bridge is a passthrough that makes the
// platform agent reachable on the bus: it consumes a2a.tasks.platform.*.in,
// runs Hermes as a subprocess, and publishes the result on
// a2a.tasks.platform.*.events (a2a/hermes-bridge/bridge.go).
//
// STATIC, and the reason is a property of the mechanism rather than a schedule.
// The bridge is a container in the AGENT pod — it execs the Hermes binary
// against the agent's profile state on the agent's PVC, so it cannot move to a
// pod of its own — and the callout resolves an identity from a ServiceAccount
// token, which the API server issues per pod. One pod is one ServiceAccount, so
// a token would give the bridge and the agent container the SAME entry: the
// union of a blackboard reader and a task-plane executor, which is `worker`
// again under a better name. A static password is what keeps them two
// principals.
//
// Not an absolute about tokens -- an absolute about THIS callout. It keys its
// map on the ServiceAccount alone (a2a/authcallout/service.go) and derives
// Narrowing from the pod, so neither field distinguishes two containers that
// share one. A map keyed on (ServiceAccount, audience) would, since a container
// cannot read a volume projected into its neighbour; that is the shape to reach
// for if the bridge ever needs a token before it leaves the pod, and it is
// unbuilt because the bridge is the only workload that would use it.
//
// Narrower than `worker` on the axis a subject grant can narrow, and NOT on the
// other one. Publishing task events is scoped to the one addressee the bridge
// serves rather than `a2a.tasks.*.*`, so it can no longer speak for a session
// pod's task. That narrowing is real and measured.
//
// Reading is not narrowed. The core subscribe is the `platform` inbound leg
// rather than `a2a.tasks.>`, but the JetStream half of the list reaches the
// whole stream by two routes a subject grant cannot scope:
// `$JS.API.DIRECT.GET.TASKS.>` takes the message subject as its trailing token,
// and CONSUMER.CREATE + MSG.NEXT carries filter_subject in the REQUEST BODY, so
// a consumer filtered on `a2a.tasks.>` is permitted by a grant that names only
// the stream. Both are in TestBridgeJetStreamGrantOnARealServer's allowed
// table, measured on the bridge's own addressee: what makes them unscoped is
// the shape of the two routes, not anything that test crosses -- its one
// cross-addressee row is the refused events WRITE. A holder of
// `bridge-password` can therefore drain every session's prompts off TASKS.
// Not a regression -- `worker` held the identical two grants -- and not
// closable by editing this list: what closes it is the bridge not holding
// CONSUMER.CREATE at all, which is a pre-created consumer per task (the stage-3
// dispatcher), the same answer the deliver_subject residue gets in
// a2aBridgeJetStreamGrants' comment.
//
// Dropped rather than carried across: `agents.hb.>`. Nothing in the tree
// publishes a heartbeat — a2a/web/README.md and a2a/docs/hermes-bridge.md both
// say so — and the bridge is explicitly not the thing that would.
func bridgeIdentity() a2aIdentity {
	publish := []string{
		"a2a.tasks." + a2aBridgeAddressee + ".*.events",
		"$KV.runtime-state.>",
	}
	publish = append(publish, a2aBridgeJetStreamGrants()...)
	publish = append(publish,
		"$JS.ACK.TASKS.>",
		"$JS.FC.>",
		"_INBOX."+a2aBridgeUser+".>",
	)

	return a2aIdentity{
		user:     a2aBridgeUser,
		account:  a2aAccountApp,
		auth:     a2aAuthStatic,
		credsKey: a2aBridgePasswordKey,
		comment: "the Hermes bridge sidecar: executor for the `" + a2aBridgeAddressee + "` addressee only.\n" +
			"STATIC because it shares the agent pod, and a ServiceAccount token names a\n" +
			"pod rather than a container — a callout identity here would be the agent\n" +
			"container's grants and this one's added together, which is the credential\n" +
			"the retired `worker` user was. Its WRITE is narrowed to one addressee:\n" +
			"publishing another's events is refused. Its READ is not. The JetStream\n" +
			"grants below name the TASKS stream, and both DIRECT.GET and a consumer's\n" +
			"filter_subject reach every addressee on it, so a holder of this password\n" +
			"can read every session's prompts. `worker` held the identical two grants.",
		publish: publish,
		subscribe: []string{
			"a2a.tasks." + a2aBridgeAddressee + ".*.in",
			"$KV.runtime-state.>",
			"_INBOX." + a2aBridgeUser + ".>",
		},
	}
}

// seed: the hand-applied seed tooling, which writes the starter topic entries.
// There is deliberately no path to cite here — the manifest lives outside this
// repository, which is the whole of what follows.
//
// STATIC, and it is the legacy twin of the provision principal above: the same
// job, done by an object nothing in this repository renders. The darkness audit
// records it as the artifact nobody owns — referenced by no chart, no kustomize
// path, no Makefile target and no operator code, and it survives a flip to
// today until someone deletes it by hand.
//
// It keeps its password because it is APPLIED rather than rendered. It exists on
// the demo install right now, so dropping its user from nats.conf would refuse
// it at connect the next time anyone re-ran it — a live break caused by a change
// that never touched the file, and one no test in this repository could have
// caught, because the file is not in this repository's render path at all. It
// goes away when the seed content becomes a render or ships with the gateway,
// which is an open question elsewhere and not this change's to answer.
func seedIdentity() a2aIdentity {
	// The JetStream API grant is a2aSeedJetStreamGrants() rather than the
	// $JS.API.> this user shipped with: CREATE and INFO on the streams the
	// provisioning names, plus account discovery, and nothing else. #1306
	// scoped it against the nats.conf template; A1 moved the subject lists
	// out of that template and into this list, so the scoping is carried
	// here by hand. The argument for every verb it holds and every verb it
	// refuses is on a2aSeedJetStreamGrants itself, and
	// TestSeedHoldsNoWholesaleJetStreamAPI pins the rendered result.
	publish := []string{
		"a2a.topics.agent.platform.upgrade-readiness",
		"a2a.topics.shared.blueprint",
		"a2a.topics.shared.annotations",
	}
	publish = append(publish, a2aSeedJetStreamGrants()...)
	publish = append(publish, "_INBOX.seed.>")

	return a2aIdentity{
		user:     "seed",
		account:  a2aAccountApp,
		auth:     a2aAuthStatic,
		credsKey: a2aSeedPasswordKey,
		comment: "hand-applied seed tooling. STATIC because it is applied rather than\n" +
			"rendered: it exists on installs today, and removing its user would refuse\n" +
			"it at connect the next time it ran. The rendered provisioner beside it\n" +
			"does the same job through the callout.\n" +
			"Its $JS.API grant is scoped to the streams that provisioning creates, by\n" +
			"name and by verb (#1306): CREATE and INFO on those and nothing else, so no\n" +
			"RESTORE, no MSG.DELETE or PURGE, no CONSUMER.CREATE and no STREAM.DELETE.\n" +
			"No ack grant either - seed creates no consumers, so one would be pure\n" +
			"unused capability to +TERM other principals' deliveries (the same deletion\n" +
			"the web user got).\n" +
			"Seed also reads no topics. \"a2a topics read\" is a stream API call\n" +
			"(GetLastMsgForSubject, so $JS.API.DIRECT.GET.<stream>.<subject> on these\n" +
			"streams, or STREAM.MSG.GET as the fallback) and the scoped grant below\n" +
			"refuses both. Nothing runs it as seed: the seed tooling does writes and\n" +
			"info checks only, and the a2a CLI runs in the agent pod as agent.",
		publish: publish,
		subscribe: []string{
			"a2a.topics.>",
			"_INBOX.seed.>",
		},
	}
}

// web: the read surface, the one user meant to face a browser, and the only
// user whose credential is published to one by design.
//
// STATIC, permanently. A browser holds no Kubernetes ServiceAccount token and
// there is no mechanism by which it could, so this principal can never move to
// the callout. It is not a residue awaiting a card; it is the shape of the
// thing. What the callout does change is that this is now the ONLY credential
// in the deployment a browser is ever handed.
//
// "Read-only" is not expressible as a subject list — subject permissions cannot
// see a request body, and JetStream puts the reach there — so the JS API grants
// are enumerated per stream rather than given as $JS.API.>, and there is no ack
// grant. The residues that enumeration cannot close (durability, ack policy and
// consumer names are body fields) are recorded in the deployment spec, and they
// are the ones the callout was expected to close for this user. It does not:
// they close with a separate account and an export/import, which stays open.
func webIdentity() a2aIdentity {
	return a2aIdentity{
		user:    "web",
		account: a2aAccountApp,
		comment: "the read surface, and the only credential published to a browser by\n" +
			"design. STATIC permanently: a browser holds no ServiceAccount token and\n" +
			"there is no mechanism by which it could. Read-only is not expressible as\n" +
			"a subject list - JetStream puts the reach in the request BODY - so the JS\n" +
			"API grants are enumerated per stream and there is no ack grant.",
		auth:     a2aAuthStatic,
		credsKey: a2aWebPasswordKey,
		publish: []string{
			"$JS.API.INFO",
			"$JS.API.STREAM.INFO.TASKS",
			"$JS.API.STREAM.INFO.DIRECTORY",
			"$JS.API.STREAM.INFO.TOPICS-STATE",
			"$JS.API.STREAM.INFO.TOPICS-JOURNAL",
			"$JS.API.CONSUMER.CREATE.TASKS.>",
			"$JS.API.CONSUMER.CREATE.DIRECTORY.>",
			"$JS.API.CONSUMER.CREATE.TOPICS-STATE.>",
			"$JS.API.CONSUMER.CREATE.TOPICS-JOURNAL.>",
			"$JS.API.CONSUMER.INFO.TASKS.*",
			"$JS.API.CONSUMER.INFO.DIRECTORY.*",
			"$JS.API.CONSUMER.INFO.TOPICS-STATE.*",
			"$JS.API.CONSUMER.INFO.TOPICS-JOURNAL.*",
			"$JS.API.CONSUMER.MSG.NEXT.TASKS.*",
			"$JS.API.CONSUMER.MSG.NEXT.DIRECTORY.*",
			"$JS.API.CONSUMER.MSG.NEXT.TOPICS-STATE.*",
			"$JS.API.CONSUMER.MSG.NEXT.TOPICS-JOURNAL.*",
			"_INBOX.web.>",
		},
		subscribe: []string{
			"a2a.>",
			"_INBOX.web.>",
		},
	}
}

// eval: the bench harness's diagnostic bus transport
// (bench/kube_agents_bench/a2a_transport.py, AGENT_TRANSPORT=a2a), which
// submits one task to the platform executor and awaits its terminal.
//
// STATIC, permanently, for the web user's reason: the holder is a process
// outside the cluster - devops-bench on a workstation or in a Prow pod, over a
// kubectl port-forward - and it has no ServiceAccount in THIS cluster to
// present, so the callout could never resolve it. It exists so the harness
// never touches the gateway's credential: the gateway's grant list is the one
// that may publish on every addressee's `in` subject, and a second process
// holding it would be a second requester indistinguishable from the first on
// replay.
//
// The grants are the subjects a requester of the platform executor needs and
// nothing else: publish on `platform`'s `in` (the submission, and a cancel),
// subscribe on `platform`'s `events` (the executor's status and artifact
// updates, including the terminal) and on its `supervisor`, where a task's
// supervisor writes the synthesized terminal when the executor dies without
// one - the pair lib.TaskReplaySubjects folds. The harness reads both and
// never publishes on `supervisor`: it is not the task's supervisor. Be exact
// about what the wildcards reach, because "a requester's subjects" understates
// it: the events subscription shows every `platform` task's id, the gateway's
// included, and the `in` publish takes a cancel or a message for any of them -
// the bridge cannot tell one requester from another (`from` is display-only).
// A static NATS grant cannot name one task, so whoever holds `eval-password`
// can cancel a task they did not start; the key is held like the gateway's,
// and it exists only where an operator asked for it: the user and the key are
// rendered under A2A_EVAL_PRINCIPAL=true (a2aEvalPrincipalEnabled) and on no
// other install. No JetStream API at all: the harness subscribes with a core
// subscription taken before it publishes, so it needs no consumer, and a
// CONSUMER.CREATE on TASKS would let it deliver any addressee's task plane
// into its own inbox (a2aJetStreamSurfaceRationale). A core subscription has
// no replay, so the
// harness treats a dropped connection as a failed attempt and resubmits, which
// is the trade a diagnostic can make. The inbox pair is what makes a reply to
// this principal deliverable: a publish that carries a reply subject gets the
// stream's PubAck there with no JetStream grant, and it is also the file's
// invariant (every principal may answer its own requests). The harness
// publishes without a reply subject today and learns the server took the frame
// from its flush.
func evalIdentity() a2aIdentity {
	return a2aIdentity{
		user:    "eval",
		account: a2aAccountApp,
		comment: "the bench harness's diagnostic bus transport: one task to the platform\n" +
			"executor, its terminal awaited. STATIC permanently: the holder runs outside\n" +
			"the cluster over a port-forward and has no ServiceAccount here to present.\n" +
			"Publish on platform's in subject and subscribe on its events and supervisor\n" +
			"(the pair a task's terminal may land on), nothing else: no JetStream API (a\n" +
			"core subscription needs no consumer, and CONSUMER.CREATE on TASKS would read\n" +
			"every addressee's task plane). The wildcards reach every platform task, the\n" +
			"gateway's included: a holder can cancel one it did not start, so the key is\n" +
			"held like the gateway's, and this block is rendered only while the operator\n" +
			"runs with A2A_EVAL_PRINCIPAL=true.",
		auth:     a2aAuthStatic,
		credsKey: a2aEvalPasswordKey,
		publish: []string{
			"a2a.tasks.platform.*.in",
			"_INBOX.eval.>",
		},
		subscribe: []string{
			"a2a.tasks.platform.*.events",
			"a2a.tasks.platform.*.supervisor",
			"_INBOX.eval.>",
		},
	}
}

// sys: human operators and monitoring in $SYS. No agent ever authenticates
// here.
//
// STATIC: the holder is a person with a port-forward or a scrape config, not a
// workload with a projected token. The callout service's own connection is a
// separate matter and is not this principal — see a2aCalloutServiceUser.
func sysIdentity() a2aIdentity {
	return a2aIdentity{
		user:    "sys",
		account: a2aAccountSys,
		comment: "human operators and monitoring. No agent ever authenticates here.\n" +
			"STATIC: the holder is a person with a port-forward or a scrape config.",
		auth:     a2aAuthStatic,
		credsKey: a2aSysPasswordKey,
	}
}

// calloutIdentities returns the principals the callout serves, in render order.
func calloutIdentities(agent *agentv1alpha1.PlatformAgent) []a2aIdentity {
	var out []a2aIdentity
	for _, id := range a2aIdentities(agent) {
		if id.auth == a2aAuthCallout {
			out = append(out, id)
		}
	}
	return out
}

// staticIdentities returns the principals that stay in nats.conf, in render
// order. Every one of them is listed in auth_users, which is what exempts it
// from the callout; a static user NOT in that list would be refused at connect
// by a callout that has never heard of it.
func staticIdentities(agent *agentv1alpha1.PlatformAgent) []a2aIdentity {
	var out []a2aIdentity
	for _, id := range a2aIdentities(agent) {
		if id.auth == a2aAuthStatic {
			out = append(out, id)
		}
	}
	return out
}
