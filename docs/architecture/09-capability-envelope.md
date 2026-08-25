# Design 09: The Capability Envelope

**Status:** ✅ Agreed

> **Specifies the end state, not current behaviour.** Nothing here is implemented. See
> [README.md](README.md) for the delta against what ships today.

> **North star, not a build item.** This presumes agents are separate workloads, which is not the
> current topology, so there is nothing here to build yet and no phase in
> [07](07-implementation-roadmap.md) that builds it. The Verification suite below joins the phase
> loop when one does. Core invariant #3 used to ban agent-to-agent calls outright and would have
> ruled this out; #727 restated it as the property it protects, and section 1 states where the
> mechanism stands against the revised version.

**Overview:** [README.md](README.md) · **Depends on:** [02](02-agent-personas.md), [03](03-security-model.md),
[05](05-system-architecture.md), [08](08-agent-runtime-and-identity.md) · **Tier:** Foundational
(north star)

---

## TL;DR

Once agents are separate workloads a request crosses three or four hops between the human and the
action, and every hop is somewhere "who asked" can be dropped. This specifies an **attenuating
capability**: minted at ingress from the human's verified identity, narrowed at each hop, never
widened.

**No token format. Nothing signed. No key on the capability path.** The capability
lives in NATS KV and the message carries only a lookup id. Integrity comes from subject
permissions the server evaluates on every operation, fixed when the connection authenticates. Three rules make it hold: a
parent **names the one agent permitted to descend from it**, the verifier resolves an entry **only
for the agent it names**, and only that verifier may read the store. Revocation is deleting an
entry.

This is the **deferred hardening** already named in [03](03-security-model.md) §4a and
[08](08-agent-runtime-and-identity.md) §5, with a different mechanism.

## 1. What this decides, and what it supersedes

**Those two sections stay canonical for the requirement.** [03](03-security-model.md) §4a is
explicit that v1 does **not** check the requester's own permissions, files per-request authority as
"Deferred hardening — user-scoped authorization", and points at
[08](08-agent-runtime-and-identity.md) §5. This document does not restate the requirement or the
trade; read them there.

What changes here is the **mechanism**. 03 §4a sketches it as `SubjectAccessReview` for Kubernetes
plus `testIamPermissions` / Policy Troubleshooter for GCP, with per-run downscoped tokens. The GCP
half was measured on a live cluster on 12 August 2026 and does not work:

- OAuth scopes do not constrain Kubernetes object operations -- a token minted `container.read-only`
  created a namespace.
- **No IAM Condition of any kind scopes a Kubernetes object operation.** Four spellings, four
  service accounts, one cluster, all refused, including
  `resource.service == "container.googleapis.com"`, which asserts nothing beyond "this is a GKE
  call". A conditioned binding is stored correctly, reported by Policy Troubleshooter as found and
  relevant, and grants nothing.

Generalising both: **GCP-layer credential attenuation does not reach Kubernetes object
authorization.** "Down-scope the agent's effective authority with per-run tokens" therefore has no
GCP mechanism behind it, and a Credential Access Boundary would not have rescued it either.

**This is the replacement for that half.** Attenuation moves into the envelope rather than the
credential, and the per-cluster credential becomes a Kubernetes ServiceAccount token minted by the
broker -- cluster-scoped by construction, because the issuer is the cluster. Where this and 08 §5
disagree on mechanism, this is the later measurement. Where they disagree on requirement, 03 §4a
wins.

**On invariant #3.** An earlier draft of this document argued that the invariant banned a transport
where what it protected was a property, and proposed restating it. That has happened -- #727
rewrote #3 as "agents coordinate through durable, attributable state -- never synchronous RPC",
with "no agent gains authority by being called" attached to it. So the argument this section used
to make is settled and does not need making again. What replaces it is the question the revised
invariant actually asks.

**The four-property test.** [02](02-agent-personas.md) section 2.3 says a new coordination
substrate must be durable, attributable, non-escalating and non-authoritative, and that meeting all
four is necessary rather than sufficient. Against that:

- **Durable.** The bus is durable with replay, and the capability chain is a second durable record,
  readable after the fact by anyone auditing. The store does not give append-only on its own -- see
  "every reference pins a revision" below -- so the audit value rests on that pinning, not on the
  bucket.
- **Attributable.** This is the property the document exists to carry, and as specified it does not
  fully carry it -- so read this bullet as the target rather than as a claim of conformance. The
  root is minted from the requester's verified identity and every hop descends from it, so the
  chain ties the work to one origin without a correlation exercise across logs. Two gaps, both in
  §5. The chain terminates at a request id and no entry holds a requester, so turning that origin
  into "which human" is still a lookup elsewhere, and `02` §2.3 asks for the human by name. And it
  is conditional on a request-scoped caller identity: where one identity serves several requests at
  once a resolution can attach to the wrong chain, and the walk then names the wrong origin
  confidently, which is worse than naming none.
- **Non-escalating.** Also this document, and the stronger claim: a message confers no authority
  because authority does not travel in the message at all. It travels in an entry the receiver
  cannot read, cannot widen, and cannot resolve unless it was named. Same condition -- "named"
  has to mean this request.
- **Non-authoritative.** Untouched by this design and not weakened by it. A capability bounds what a
  peer message may _ask for_; it says nothing about trusting the message content, which stays
  untrusted input under [03](03-security-model.md).

**The verifier is a synchronous call, and the invariant says never synchronous RPC.** Worth meeting
head-on rather than hoping nobody asks. What #3 forbids is agents coordinating by calling each
other -- one agent blocking on another's model output, with the call as the only record. The
verifier is neither an agent nor coordination. It is an authorization callout on the request path,
the same shape as the NATS auth callout 09 proposes alongside it, and it produces a decision about
an entry rather than work product from a peer. If that reading is wrong the design has a problem,
so it is stated here to be argued with rather than left implicit.

**Three components here do not exist in the design set, and 09 introduces all three.** The bus, the
per-agent broker in front of each agent, and the verification service are named throughout as
though they were furniture, and they are not: [05](05-system-architecture.md)'s component inventory
runs C1-C15 with no message bus and no verifier, and its "broker" (C6) is the GitHub token minter,
which is a different thing wearing the same word. Read every "the broker" below as something this
document proposes. Adding them to 05 with C-numbers, so 09 can cite them instead of describing
them, is the first thing to do if this moves toward being built.

**What is settled and what is not.** The mechanism is agreed as the design. The topology it
presumes does not exist, so the open question is not whether to do this but when there is anything
to do it to -- see the north-star note at the top.

## 2. The recommendation, first

**No token format. Nothing signed. No key on the capability path.**

The capability lives in NATS KV. The message on the bus carries only a lookup id.

**"Key" below means a KV lookup key** -- a string like `cap.root.req-8f2a` -- and never a
cryptographic key. No capability is signed, and nothing has to hold a key to mint or verify one.

**One cryptographic key does exist, and it is the most powerful thing in the design.** A NATS auth
callout answers each authorization request with a user JWT it signs with the issuer account's seed,
and that JWT carries the publish and subscribe permissions the server then enforces. So the seed
does not merely authenticate connections. It decides what every connection may do, including the
two permissions the whole integrity argument rests on: whoever holds it can issue itself a user
with publish on `$KV.cap.root.>` and read on `$KV.cap.>`, and then mint a root capability at any
tier and scope and read every capability in flight.

What the design avoids is narrower than "no keys", and it is still worth having: **no key on the
capability path**. Nothing mints, signs or verifies a capability, so no component needs a key in
order to participate in one, and there is no verification key to distribute to every hop. That is
the property §7's comparison turns on and it survives intact.

What it does not buy is immunity to key compromise, and an earlier version of this paragraph
claimed it did. The seed is the root of authority for the bus, so it wants gateway-grade custody, a
rotation story and a compromise runbook -- not the handling a credential described as
"authenticating connections" would get. §5 lists it with the other concentrations.

## 3. How it works

**Gateway.** Mints the capability, writes it to KV under `cap.root.<request-id>`, and puts the id
and the revision the write returned -- not the capability -- into the message. The entry names the
one agent permitted to descend from it.

**Broker.** Reads the id off the message and asks the verifier to resolve it. The agent behind it
sees neither the capability nor the store.

**Attenuation.** A hop that narrows writes a _new_ entry -- narrower capability, a pointer to its
parent pinned at the revision it resolved, and the next hop as its own delegate -- under its own
namespace, and passes the new id and revision downstream.

**Verification.** Six checks, all required. Authenticate the caller and confirm the entry it is
asking about names that caller as its delegate. Walk the chain to the root, refusing a chain that
revisits an entry or exceeds a fixed depth bound. Confirm the root sits under `cap.root.*`. Confirm
each link is narrower than its parent. Confirm **each link was written by the agent its parent
named as delegate**. Fetch every link **at the revision its referrer pinned**, and refuse if that
revision is no longer the one the store holds. Refuse otherwise.

The caller identity in the first check **must be request-scoped**. An identity shared across
concurrent requests cannot separate them, and the guarantee this design exists to make is void
without it. "The identity the verifier authenticates" below states what that requires of the
runtime.

The first is about the caller and the rest are about the chain, which is why an earlier draft had
only the chain ones. See "The subject prefix does not prove entitlement" below.

```
   gateway   writes  cap.root.req-8f2a      = {tier: platform, scope: project-P,
                                               delegate: fleet-recon}
                     └─ the write returns revision 412
                     └─ message carries "req-8f2a @412"

   hop A     asks the verifier to resolve it, and is authenticated as fleet-recon --
             the delegate the entry names, so it resolves and fleet-recon may descend
             writes  cap.hop.fleet-recon.1  = {tier: cluster-admin, scope: cluster-C,
                                               delegate: platform-a}
                                              parent: cap.root.req-8f2a @412
                     └─ message carries "cap.hop.fleet-recon.1 @418"

   hop B     resolves that as platform-a: chain narrows, and platform-a is the delegate
             the entry names.  fleet-recon asking for the same id would be refused
             writes  cap.hop.platform-a.7   = {tier: developer-team,
                                               scope: cluster-C/ns-web, ...}
                     └─ and so on
```

A hop delegating to several agents writes one child per recipient, each naming a single delegate.
A list would also work; one child per recipient keeps the audit trail exact about who was handed
what.

## 4. Why this needs no crypto

NATS KV keys live on subjects, and a client's subject permissions are **fixed when it
authenticates and evaluated by the server on every operation**. So, writing a bucket named `cap`:

- Only the gateway may publish under `$KV.cap.root.*`
- Each broker may publish only under `$KV.cap.hop.<its-own-agent-id>.*`
- **No broker may read the bucket at all.** Only the verification service holds read, and that
  means both `$KV.cap.>` and the JetStream API subjects that serve the bucket.

**Spell these in subject space, not key space.** A KV key does not live at its bare name: bucket
`cap` is stream `KV_cap` on `$KV.cap.>`, and a get is a JetStream API call under `$JS.API.*` rather
than a subscribe on the key. A permission written as `cap.root.*` matches no subject any KV
operation touches, so it grants and denies nothing -- and a read denial written that way leaves a
broker holding broad `$JS.API.>` for any other stream with a working path to the store. The
denial tests inherit the same requirement: written against the bare names they pass on a server
where no `$KV` permission has been configured at all, which is the shape of test §9 opens by
warning about.

That buys the two properties a signature would have bought:

**Who wrote this link.** The subject prefix proves it. Forging a root capability means
publishing on a subject the server refuses you, with the permissions
it attached when you authenticated and cannot be talked out of afterwards.

**Did each link narrow.** The verifier reads parent and child and compares. A compromised broker
that writes something wider than it received is caught when the next hop resolves the chain.

The integrity comes from server-enforced permissions rather than from cryptography. Same guarantee,
and no key on the capability path to distribute -- with the one exception §2 names: the permissions
above are carried in the user JWT the auth callout signs, so the seed that signs it can grant
itself any of them. The refusal is the server's rather than our code's, which is what this section
claims; it is not independent of the seed.

**Every reference pins a revision.** A KV put on an existing key is an update rather than an error,
and a KV delete is itself a publish to the key's own subject -- so the one permission that lets a
broker create a link also lets it overwrite that link, or delete it and create it again, after the
link has been resolved and acted on. Either way the audit record is rewritable by the party it
exists to attest, and a subject permission cannot express "create but do not update", so the
permission model cannot close this on its own.

What closes it is that nothing refers to an entry by key alone. A write returns the revision it
landed at; whoever refers to that entry afterwards carries the revision with the key, and the
verifier fetches at that revision rather than fetching the latest. The message carries `id @rev`,
and a child's parent pointer carries its parent's `@rev`, so the chain is pinned end to end.

Both rewrite paths then fail the same way rather than needing separate detection. An overwrite moves
the entry to a new revision and the pinned one no longer resolves. A delete-and-recreate is worse
for the attacker, not better: the recreated entry lands at a fresh sequence, so every pin to the old
one dangles. This is deliberately not "check the entry is at its first revision" -- a revision is
the underlying stream's sequence and is bucket-wide, not per key, so an entry's first write carries
whatever number the bucket had reached and there is nothing for a verifier holding one entry to
compare against.

One consequence for anyone reading the store afterwards. A pin protects the _verifier_, which asks
for a specific revision and gets what the referrer intended or nothing. It does not protect a reader
who asks for a key and takes the latest value, and that is how an audit tool would naturally be
written. At the bucket's default of one revision per key the two agree, because an overwrite
discards what it replaced and the pin then dangles. Raise history above one and they diverge: the
pin still resolves to the intended content while the latest value at that key is whatever was
written last. **So audit reads follow the pins, by revision, rather than reading keys** -- which is
also the only reading that reconstructs the chain as it was resolved.

The cost is that a broker can still break its own descendants by deleting a link it wrote. That is
denial of service against a chain it is already inside, not a way to widen anything, and it is the
same delete authority §5 flags as unresolved for revocation.

**Say "the server refuses it", not "the connection is refused".** The two are different observables
and only one of them happens. A client with no publish right on `$KV.cap.root.*` connects fine and
authenticates fine; it gets `-ERR 'Permissions Violation for Publish to ...'` when it publishes, and
the connection stays open. The property §4 needs is intact -- the refusal comes from the server and
not from application code we have to write -- but a denial test worded as "the connect is rejected"
fails against a correctly configured server, and the natural repair is to weaken it to "the connect
succeeds", which asserts nothing at all.

### The subject prefix does not prove entitlement

Worth stating separately, because two drafts of this design got it wrong in two different places
and neither failure is obvious.

Write permission proves who wrote a child. It proves nothing about whether that writer ever
_received_ the parent the child names. Without a further rule, a compromised broker writes
`cap.hop.<its-own-id>.N` naming `parent: cap.root.<somebody-else's-request>` -- a root minted for a
more privileged human -- and every check passes. The root is under `cap.root.*`, each link narrows,
and the prefix correctly proves who wrote the child. The hop has escalated by descending from
someone else's origin. Request ids are not secrets, so naming one is no barrier.

**Three rules close it, and all three are needed.**

**The parent names its delegate.** Every entry carries the single agent id permitted to write
children of it, and verification refuses a child whose writer is not that agent. Authority to
descend is granted by the parent's author and proved by the subject prefix, rather than inferred
from the child.

**The verifier authenticates its caller.** A resolution is refused unless the entry names the
caller as its delegate. The rule above closes descending from a root you were never handed. On its
own it does nothing about simply _presenting_ that root, which reaches the same authority with less
work. A broker puts `cap.root.<somebody-else's-request>` on its outbound message and skips writing
a child entirely. Every chain check passes, and vacuously: a single-entry chain widens
nothing, and a root has no parent whose delegate could be violated. Same precondition as above --
ids are not secrets -- and the same escalation, on the read path.

One field does both jobs, one level apart. The writer of an entry must be the delegate its
_parent_ names; the resolver of an entry must be the delegate _it_ names. Both are the party that
was handed the id, which is the point.

### The identity the verifier authenticates

Both delegate rules are only as sharp as the identity they compare against, and an agent id is not
sharp enough. [02](02-agent-personas.md) fixes cardinality at one Cluster Admin Agent per cluster
and one Developer Team Agent per namespace, so a single agent id is the named delegate of every
request routed through it, for every human, at the same time. Check the rules against that and they
stop separating anything. A broker serving a developer-team-tier request presents the id of a
concurrent platform-tier one, is the named delegate of that entry too, and passes every check holding a
capability minted for someone else. No forged write and no second compromise -- and a concurrency
bug in an honest broker reaches the same place as a malicious one, which is the part that should
worry you.

**So the identity the verifier authenticates must be scoped to the request, not to the agent.** A
capability is per-request, and a check that compares it against a per-agent identity is comparing
against the wrong thing. This is a requirement 09 places on the runtime rather than something the
KV scheme can fix from inside: whatever issues the broker's credential must issue a distinct one
per request, so that "the caller" and "the request" are the same subject.

**Nothing in the design set provides that today, and the nearest thing to it is deferred on
purpose.** [08](08-agent-runtime-and-identity.md) §5 lists a scope broker issuing per-run ephemeral
tokens among the options held out of v1, and its non-goals rule out "per-request credential
enforcement" by name; [02](02-agent-personas.md) §8 and [06](06-api-and-data-contracts.md) §2 fix
agent identity as one pre-created, tier-scoped ServiceAccount per agent. So this is a new
requirement 09 raises and not a dependency on work already in flight -- which makes it a cost of
the design rather than something arriving on its own.

**Until that exists the guarantee is weaker, and it is worth saying which weaker.** With a shared
agent identity the bound is the widest capability concurrently delegated to that agent, not the
authority of the human whose request is being served. That is still a bound and it is still worth
having. It is not the sentence below, and 09 should not be built as though it were.

**Only the verifier reads.** If every broker could walk the chain itself, every broker would need
read across `cap.*`, which is what makes other agents' roots discoverable in the first place.
Moving the walk into one service removes the need to distribute read at all. It does not by itself
remove discovery: every broker must be able to call the verifier, so without the caller check above
an id is still resolvable by anyone who can name one, and discovery has moved from a KV read to an
RPC rather than gone away. Withholding read and checking the caller are what close it between them.
The verifier sits on the request path, which is the same shape -- and the same
cost -- as the NATS auth callout 09 proposes alongside it.

**Revocation is deleting an entry**, which is the other reason to prefer this. A signed token is
valid until it expires no matter what you learn in the meantime.

**The cost** is a lookup on the request path and a dependency on the bus. If the bus is down
there are no messages to authorize, so that dependency is smaller than it first appears.

## 5. What this does not solve

Seven things, stated so nobody assumes otherwise. The last three are open questions rather than
accepted limits, and they go to the downscoping design discussion together.

**Attenuation is code.** A hop that forwards without narrowing is a hole, and no token format or
KV scheme fixes that. Real tension with "structural, not behavioural."

The bound that makes it survivable: a hop can only descend from a parent that named it, and every
chain terminates at a root the gateway minted from the requester's own authority.

> **A broken hop cannot exceed what it was delegated for the request it is serving.** Worst case is
> "narrowed less than intended", never "escalated past the human who asked."

That is the sentence to have ready when someone probes the design, and it is worth knowing exactly
what carries it: the two delegate rules above, a per-request principal both of them can name, and
nothing else. The write half stops a hop descending from an origin it was never handed. The read
half stops it presenting that origin directly. The principal is what makes "it" mean this request
rather than this agent, and neither half has one today -- so read the last five words of the claim
as the target. What follows is why. It holds under imperfect implementation, which is the only kind
there is, but it does not hold under a missing rule.

**Neither side is carried, and the claim is ahead of the mechanism until they are.** The write side
is proved by the subject prefix, which is per agent, so an agent that is concurrently the delegate
of two roots can write a child descending from the wider one while serving the narrower one's
request.

The read side looks better and is not. The check compares the caller against the entry's `delegate`
field, and that field holds an agent id written by the parent -- so making the caller's credential
per-request changes one side of a comparison whose other side is still per-agent. Compare at agent
granularity and the concurrent-capability escalation survives untouched, because the wider entry
really does name that agent and the caller really is it. Demand exact equality and no legitimate
resolution matches at all. A per-request caller identity on its own buys nothing.

**What both sides actually need is a principal whose name the parent can predict.** The obstacle
this paragraph used to give -- that the parent writes the entry before the downstream hop's
credential exists -- is not the real one, and it applies equally to `delegate`, which the same
parent writes at the same moment. The credential does not have to exist for the parent to name it;
the _name_ has to be derivable. It is, if the principal is the pair of an agent and the request id,
because the parent allocates the request id it is dispatching. Whether that is the right shape, and
what issues such a principal, is the design question rather than this document's to settle.

Treat the claim above as the target rather than as the current state, and as missing two of its
three supports rather than one. This is going to the downscoping design discussion together with
the three below, since all four turn on what "the request it is serving" means to a mechanism that
cannot observe a request.

**Chain depth is a refusal, not just a cost.** Resolution walks to the root, so a long chain is a
lot of KV reads. Those are now the verifier's reads rather than every broker's, so caching them is
tempting. Be careful with it: a per-id cache invalidated only by revoking that id serves a stale
answer after an _ancestor_ is deleted, which is exactly the case §9's revocation test exercises, so
an implementation doing the obvious thing fails that test. A cache has to be invalidated by any
delete or overwrite anywhere in the chain, which means watching the bucket rather than reasoning
about the id in hand. Not a problem at three or four hops, so the honest advice is to leave it
uncached until it measures.

The reason the walk is bounded is the other one. A broker holds publish across
`cap.hop.<its-own-id>.*`, so it can write two entries in its own namespace naming each other as
parent, each naming itself as delegate, with identical payloads. Every rule holds -- both writes
are inside its permitted subject, each entry's parent names it as delegate, and `C_new ⊆ C_old` is
satisfied by equality -- and the walk never reaches a terminal. One broker, using only the
permissions this design grants it, hangs the verifier. Since the verifier is a single service on
the request path and nothing authorizes while it is down, that is a fleet-wide outage from one
compromised or simply buggy hop. Hence the depth bound and the visited set in the checks above,
and the denial tests for both.

**The verifier is trusted and on the request path.** It is the only component holding read across
`cap.*`, so compromising it exposes every in-flight capability, and if it is down nothing
authorizes. That is a real concentration and it is the price of not distributing read.

**The auth callout's signing seed is a larger one.** It signs the user JWTs that carry the
permissions §4 relies on, so its holder can grant itself publish under `$KV.cap.root.>` and read
across the bucket -- mint a root at any tier, and read every capability in flight. Compromising the
verifier exposes what is in the store; compromising the seed lets you write to it as the gateway.
Neither is a reason not to do this, and both belong in the same tier of scrutiny, but the seed is
the one to write the custody and rotation story for first.

**Nothing expires.** No entry carries an issue time, a use count, or any notion of the request
being over, and a TTL is rejected elsewhere in this document as a thing revocation saves us from. So
a root minted at 10:00 for a request that finished at 10:01 is still a valid parent at 03:00, and a
hop can write a fresh child of its own stale root and drive work attributed to a human who went
home. "For the request it is serving" is not observable to the mechanism as specified.

**Revocation names no actor.** Deleting an entry is a stated goal and nothing says who deletes. The
verifier holds read only, so it cannot. The gateway holds `$KV.cap.root.*`, so it can revoke roots
and nothing else. The only party that can delete a hop entry is the broker that wrote it, which is
the party being revoked from.

**The envelope has no requester field.** It carries a tier and a scope. [03](03-security-model.md)
§4a stays canonical for the requirement and that requirement is authorization against the
requester's own identity, which a hop cannot perform from a resolved capability because it does not
know who the human is. The goal below is written as "agent ceiling ∩ requester" while the mechanism
delivers agent ceiling ∩ tier, and the chain walk terminates at a request id rather than at a
person -- so "who asked" is still a lookup somewhere else. Which way to reconcile that is a
downscoping question, not one this document should settle alone.

---

## 6. Background: this pattern has a name

Everything above is an application of **macaroons**, and it is worth being able to say so.

> Birgisson, Politz, Erlingsson, Taly, Vrable, Lentczner. _Macaroons: Cookies with Contextual
> Caveats for Decentralized Authorization in the Cloud._ NDSS 2014.

A Google paper, which is convenient for the audience. The core idea is an authority token that
any holder can narrow by appending a caveat, and that nobody can widen. That is precisely the
C_new ⊆ C_old chain, and we should present it as applying a known pattern rather than as a scheme
we invented. Naming it first turns "did you two design your own crypto?" into "yes, that one."

**We are not using the macaroon construction itself**, and there is a specific reason worth
recording.

Macaroons chain with symmetric HMAC: `sig = HMAC(root_key, id)`, then `sig = HMAC(sig, caveat)`
for each caveat. Appending needs no key, which is the elegant part. But **verification requires
the root key** -- so every component that verifies also holds the key that mints.

In hub-and-spoke that means shipping a fleet-wide minting key to every broker in every spoke. One
compromised broker becomes a fleet-wide authority. Bad trade, and easy to walk into if someone
reads the citation and reaches for a library.

**The difference is copies, not kind, and the argument is weaker than it first reads.** The auth
callout's seed can mint too (§2), so this design also has a key whose holder gets fleet-wide
authority -- it has one copy of it, in one service, instead of one in every broker in every spoke.
That is a real and large difference in blast radius and it is the honest version of the claim. It
is not "we avoided the key".

**If we ever need self-contained tokens, use biscuit, not macaroons.** Same append-only
attenuation, built on Ed25519 rather than HMAC: verification needs only the root _public_ key, so
verifiers verify and cannot mint. <https://www.biscuitsec.org/>

The only scenario that would force this is a hop that must authorize without reaching the bus.
Nothing in the current topology needs that.

## 7. The general rule this came from

The same reasoning decided three separate questions:

| Question                                                 | The crypto answer                                                          | What we do instead                                                                                                                                                                                                                                         |
| :------------------------------------------------------- | :------------------------------------------------------------------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| How do agents authenticate to the bus?                   | NATS decentralized JWT -- operator key signs accounts, accounts sign users | Auth callout against ServiceAccount tokens the cluster already issues. Every conformant cluster is an OIDC issuer with audience-bound, rotated tokens. **One account seed for the callout, none for capabilities -- and see §5 on what that seed can do.** |
| What stops a capability being forged?                    | Sign it, distribute verification keys                                      | A KV entry on a subject the forger cannot publish to. The server refuses the publish.                                                                                                                                                                      |
| What stops a token being used against the wrong cluster? | Encode a scope, check it                                                   | The token is issued _by_ the target cluster. Another cluster rejects it because a different issuer signed it. **Nothing has to check anything.**                                                                                                           |

> **Prefer a boundary that already exists and is enforced by someone else over a check we have to
> write, distribute and operate.**

Every cryptographic check we build is a key to custody, rotate, revoke and recover, plus a
verification path that can have a bug. A structural property has none of those. A token from
cluster C does not work against cluster D whether or not our code is correct today.

It is also why the RBAC-over-IAM measurement felt like a win rather than a setback. We went
looking for a way to _express_ per-cluster scope and found the scope was already structural one
layer down.

## 8. Goals & non-goals

### Goals

- Make **effective authority = agent ceiling ∩ requester** hold across process boundaries, not only
  inside one process.
- Carry it with **no key on the capability path** -- nothing mints, signs or verifies a capability,
  so no component needs a key to participate in one and there is no verification key to hand every
  hop. Not a claim of key-compromise immunity: the auth callout's account seed signs the JWTs that
  carry the bus permissions, so its holder can mint a root (§2, §5).
- **Revocation that takes effect now**, by deleting an entry, rather than waiting out a TTL.
- Give audit "who asked" directly, without a correlation step across hops.

### Non-goals

- **Not a replacement for the read-only ceiling or the PR gate** ([03](03-security-model.md) §1).
  This narrows authority within those bounds; it never widens anything and never creates a write
  path.
- **Not offline-capable.** A hop that must authorize without reaching the bus is out of scope --
  see the biscuit note above if that ever changes.
- **Not credential attenuation.** The envelope narrows what a request may ask for. The credential
  handed to the API server is a separate mechanism (a per-cluster ServiceAccount token, §1).
- **Not a defence against a compromised gateway.** The gateway is the root of trust for the whole
  chain; if it lies about who asked, everything below inherits the lie.
- **Not per-user granularity.** Capabilities carry the same `tier` the `Agent` CR does
  ([02](02-agent-personas.md) §6.1), so this is as fine-grained as that field and no finer.

## 9. Verification

Every check below asserts a **denial**. A test that only confirms an authorised request succeeds
cannot distinguish a working control from an absent one, and several of the failure modes here are
silent.

- **Descent without delegation is refused:** a broker writes a child naming a parent that names a
  _different_ agent as delegate. Resolution fails. This is one of the two checks that carry the
  "cannot exceed what it was delegated" claim; without it the design is broken, so it is the first
  test to write.
- **Resolving an id you were never handed is refused:** a broker asks the verifier to resolve an
  entry naming a different agent as delegate, and is refused. The one to write second, and the
  easier of the two to leave out: it needs no forged write, so a chain-only test suite passes with
  the hole open. Assert it for a bare `cap.root.*` id in particular, where the other four checks
  pass vacuously -- nothing widens in a one-entry chain, and a root has no parent to violate.
- **Widening is refused:** a child granting a tier or scope its parent does not hold fails
  resolution, whether it widens by one field or replaces the payload wholesale.
- **A concurrent capability belonging to another request is refused:** one agent is handed two
  capabilities at once, at different tiers, for two different humans. Resolving the wider one while
  serving the narrower one's message fails. This is the test that decides whether the caller
  identity is really request-scoped, and it passes vacuously against a shared agent identity -- so
  assert the tiers actually differ and that the refusal is the identity check rather than a
  coincidence of the chain.
- **A cyclic chain is refused, and quickly:** two entries in one broker's own namespace naming each
  other as parent, each naming that broker as delegate, with identical payloads. Resolution fails on
  the visited set rather than running. Assert the refusal is bounded in time: the failure mode being
  tested is a verifier that never returns, so a test that only checks the verdict would hang with
  the bug present.
- **An over-deep chain is refused:** a well-formed chain longer than the bound fails, terminal
  `cap.root.*` and all.
- **A rewritten entry is refused, by either route:** write a link, resolve it once, then (a)
  overwrite it in place and (b) in a second run delete it and create the same key again with
  different content. Both resolutions fail on the pinned revision. Test both: create-only writes
  stop (a) and do nothing about (b), since the create succeeds once the key is gone, so a suite that
  only exercises the overwrite passes with the delete path open.
- **An orphan root is refused:** a chain whose terminal entry does not sit under `cap.root.*` fails,
  including one that terminates at a well-formed `cap.hop.*` entry.
- **Only the gateway mints roots:** any other connection publishing to `$KV.cap.root.*` gets a
  permissions violation **from the server**, not a rejection from application code. The connection
  is expected to succeed and stay open; the publish is what fails.
- **No broker writes in another broker's namespace:** broker A publishing to `$KV.cap.hop.<B>.*` is
  refused the same way.
- **No broker reads the store:** a broker attempting any read of the bucket is refused -- both a
  direct get on `$KV.cap.>` and the JetStream API path to the same stream,
  since denying only the first leaves the second open. Assert this for a broker that legitimately
  participates in a chain, since the whole point is that participation does not imply read.
- **The permissions are actually configured:** a connection with no capability permissions at all is
  refused the operations above. Written against bare key names rather than `$KV` subjects, each test
  above passes on a server where nothing was configured, so this one is what distinguishes the
  control from its absence.
- **Revocation is immediate:** delete an entry mid-flight; the next resolution of any id descending
  from it fails.
- **The agent never sees the capability:** from inside an agent container, the capability id and the
  KV store are both unreachable -- no bus credential, no verifier route.

## 10. References

- Birgisson et al., _Macaroons_, NDSS 2014.
- Biscuit: <https://www.biscuitsec.org/>
- The object-capability model generally, for "authority is something you hold and pass on,
  narrowed."
- SPIFFE/SPIRE, if ServiceAccount-token authentication ever needs to span non-Kubernetes
  workloads.
