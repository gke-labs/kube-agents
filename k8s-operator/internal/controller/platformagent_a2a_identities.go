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
// callout, and it is off the shared worker credential. `worker` survives below
// as a shrinking residue rather than the session story, and there is still no
// `agent` principal — see the note where it is not, below.

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
// iteration.
func a2aIdentities(agent *agentv1alpha1.PlatformAgent) []a2aIdentity {
	ns := agent.Namespace
	return []a2aIdentity{
		gatewayIdentity(agent, ns),
		provisionIdentity(agent, ns),
		sessionIdentity(agent, ns),
		workerIdentity(),
		seedIdentity(),
		webIdentity(),
		evalIdentity(),
		sysIdentity(),
	}
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

// No `agent` principal here, deliberately, and the omission is the correction
// of a claim this file used to make.
//
// The platform agent's pod holds the widest reach in the namespace, and giving
// it a name of its own on the bus — off the shared `worker` credential and onto
// a token-authenticated identity scoped to reading topics and the directory —
// is the narrowing the callout is worth arming for. It is not this change. The
// only bus client in that pod is the Hermes bridge sidecar, and it authenticates
// from NATS_USER/NATS_PASSWORD as static `worker`
// (a2a/cmd/hermes-bridge/main.go). Nothing renders an a2a-bus token for the
// agent ServiceAccount: a2aBusTokenVolumeSource and a2aBusTokenVolumeMount have
// one caller each, the provisioning Job. That is still true after #1256, which
// gives the agent pod bus credentials as `worker` rather than a token.
//
// So an entry here would be a grant on the agent ServiceAccount that no
// workload can present, in the map that is supposed to be the record of who
// actually authenticates. The narrowing lands with the change that moves the
// agent pod onto a projected token, which is where the grant list belongs and
// where it can be tested against a client that exists. Until then the agent pod
// keeps `worker`'s grants, which is what it has today.

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

// worker: executor for any addressee.
//
// STATIC, and it is now a shrinking residue rather than the whole session
// story. Session pods no longer authenticate as this: they have a
// ServiceAccount and a pod-bound token, and the session principal above gives
// each incarnation its own grants. What still uses this password is the
// hand-applied seed tooling's twin below and the agent-side workloads that have
// not moved yet, so the entry stays until those do — retiring it is sequencing,
// not this change's.
//
// It is kept here unchanged on purpose: every grant in this list is something a
// session pod used to hold, and the diff between it and sessionIdentity above
// is the measure of what A2 actually removed. Publishing task events for ANY
// addressee, `$JS.API.>`, and a subscribe on the whole task plane are the three
// that mattered.
func workerIdentity() a2aIdentity {
	// The JetStream API grant is a2aWorkerJetStreamGrants() rather than the
	// $JS.API.> this user shipped with: INFO, CONSUMER and DIRECT.GET on the
	// four streams it actually touches, by name and by verb. #1393 scoped it
	// against the nats.conf template; A1 moved the subject lists out of that
	// template and into this one, so — exactly like seed above, and like the
	// directory removal below — the scoping is carried here by hand, because
	// git cannot see that the two edits are the same edit. The argument for
	// every verb it holds and every verb it refuses is on
	// a2aWorkerJetStreamGrants itself.
	publish := []string{
		"a2a.tasks.*.*.events",
		"a2a.topics.agent.platform.upgrade-readiness",
		"a2a.topics.shared.blueprint",
		"a2a.topics.shared.annotations",
		// No a2a.agents.> publish. The directory is the identity plane:
		// a2a.agents.{profile} is last-value, so one publish REPLACES a
		// profile's card, and an agent-closed tombstone retires it. The
		// payload spec says cards are "published by the profile's owner
		// (the operator once profiles are CRs), not by workers", and
		// nothing in the tree publishes one -- this grant had no caller
		// and let the least-trusted principal in the deployment forge any
		// profile's card. The gateway keeps SUBSCRIBE on the same
		// subjects, which is the read discovery actually needs. Removed on
		// main by #1313, carried across that merge by hand for the same
		// reason the JetStream scoping is carried here.
		//
		// That closed forgery. Reach was still open until #1393: the bare
		// $JS.API.> below covered STREAM.PURGE.DIRECTORY,
		// STREAM.UPDATE.DIRECTORY and STREAM.DELETE.DIRECTORY, so worker
		// could erase the whole directory in one call.
		"agents.hb.>",
		"$KV.runtime-state.>",
	}
	publish = append(publish, a2aWorkerJetStreamGrants()...)
	publish = append(publish,
		"$JS.ACK.TASKS.>",
		"$JS.FC.>",
		"_INBOX.worker.>",
	)

	return a2aIdentity{
		user:    "worker",
		account: a2aAccountApp,
		comment: "executor for any addressee. STATIC, and no longer held by session pods:\n" +
			"each session now presents a pod-bound ServiceAccount token and the callout\n" +
			"scopes it to its own task (see the session entry in the identity map). What\n" +
			"still authenticates here is the hand-applied seed tooling's twin and the\n" +
			"agent-side workloads that have not moved, so the password stays until they do.",
		auth:     a2aAuthStatic,
		credsKey: a2aWorkerPasswordKey,
		publish:  publish,
		subscribe: []string{
			"a2a.tasks.>",
			"a2a.topics.>",
			"$KV.runtime-state.>",
			"_INBOX.worker.>",
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
			"info checks only, and the a2a CLI runs in the agent pod as worker.",
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
// never publishes on `supervisor`: it is not the task's supervisor. No
// JetStream API at all: the harness subscribes with a core subscription taken
// before it publishes, so it needs no consumer, and a CONSUMER.CREATE on TASKS
// would let it deliver any addressee's task plane into its own inbox
// (a2aJetStreamSurfaceRationale). A core subscription has no replay, so the
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
			"every addressee's task plane).",
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
