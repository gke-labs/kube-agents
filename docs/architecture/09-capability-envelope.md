# 09 — The Capability Envelope

**Status:** Design. Not built.

> **Specifies the end state, not current behaviour.** Nothing here is implemented. See
> [README.md](README.md) for the delta against what ships today.

> **North star, not a build item.** This presumes agents are separate workloads, which is not the
> current topology, so there is nothing here to build yet and no phase in
> [07](07-implementation-roadmap.md) that builds it. The Verification suite below joins the phase
> loop when one does. Core invariant #3 used to ban agent-to-agent calls outright and would have
> ruled this out; #727 restated it as the property it protects, and section 1 states where the
> mechanism stands against the revised version.

**Overview:** [README.md](README.md) · **Depends on:** [03](03-security-model.md),
[05](05-system-architecture.md), [08](08-agent-runtime-and-identity.md) · **Tier:** Foundational
(north star)

---

## TL;DR

Once agents are separate workloads a request crosses three or four hops between the human and the
action, and every hop is somewhere "who asked" can be dropped. This specifies an **attenuating
capability**: minted at ingress from the human's verified identity, narrowed at each hop, never
widened.

**No token format. Nothing signed. No cryptographic key anywhere in the design.** The capability
lives in NATS KV and the message carries only a lookup id. Integrity comes from connection-time
subject permissions, which NATS enforces before a message is parsed. Three rules make it hold: a
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
half was measured on a live cluster on 12 August and does not work:

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

- **Durable.** The bus is durable with replay, and the capability chain is a second durable record
  -- immutable once written, and readable after the fact by anyone auditing.
- **Attributable.** This is the property the document exists to carry. The root is minted from the
  requester's verified identity and every hop descends from it, so "which human" is a chain walk
  rather than a correlation exercise across logs. Conditional on a request-scoped caller identity,
  below: where one identity serves several requests at once, a resolution can attach to the wrong
  chain and the walk then names the wrong human confidently, which is worse than naming none.
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
the same shape as the NATS auth callout this design already accepts, and it produces a decision
about an entry rather than work product from a peer. If that reading is wrong the design has a
problem, so it is stated here to be argued with rather than left implicit.

**What is settled and what is not.** The mechanism is agreed as the design. The topology it
presumes does not exist, so the open question is not whether to do this but when there is anything
to do it to -- see the north-star note at the top.

## The recommendation, first

**No token format. Nothing signed. No cryptographic key anywhere in the design.**

The capability lives in NATS KV. The message on the bus carries only a lookup id.

**"Key" below means a KV lookup key** -- a string like `cap.root.req-8f2a` -- and never a
cryptographic key. There are none in this design.

## How it works

**Gateway.** Mints the capability, writes it to KV under `cap.root.<request-id>`, and puts the id
-- not the capability -- into the message. The entry names the one agent permitted to descend from
it.

**Broker.** Reads the id off the message and asks the verifier to resolve it. The agent behind it
sees neither the capability nor the store.

**Attenuation.** A hop that narrows writes a _new_ entry -- narrower capability, a pointer to its
parent, and the next hop as its own delegate -- under its own namespace, and passes the new id
downstream.

**Verification.** Five checks, all required. Authenticate the caller and confirm the entry it is
asking about names that caller as its delegate. Walk the chain to the root, refusing a chain that
revisits an entry or exceeds a fixed depth bound. Confirm the root sits under `cap.root.*`. Confirm
each link is narrower than its parent. Confirm **each link was written by the agent its parent
named as delegate**. Refuse otherwise.

The caller identity in the first check **must be request-scoped**. An identity shared across
concurrent requests cannot separate them, and the guarantee this design exists to make is void
without it. "The identity the verifier authenticates" below states what that requires of the
runtime.

The first of those is about the caller and the other four are about the chain, which is why an
earlier draft had only the four. See "The subject prefix does not prove entitlement" below.

```
   gateway   writes  cap.root.req-8f2a      = {tier: operator, scope: project-P,
                                               delegate: fleet-recon}
                     └─ message carries "req-8f2a"

   hop A     asks the verifier to resolve it, and is authenticated as fleet-recon --
             the delegate the entry names, so it resolves and fleet-recon may descend
             writes  cap.hop.fleet-recon.1  = {..., scope: cluster-C,
                                               delegate: platform-a}
                                              parent: cap.root.req-8f2a
                     └─ message carries "cap.hop.fleet-recon.1"

   hop B     resolves that as platform-a: chain narrows, and platform-a is the delegate
             the entry names.  fleet-recon asking for the same id would be refused
             writes  cap.hop.platform-a.7   = {tier: reader, scope: cluster-C, ...}
                     └─ and so on
```

A hop delegating to several agents writes one child per recipient, each naming a single delegate.
A list would also work; one child per recipient keeps the audit trail exact about who was handed
what.

## Why this needs no crypto

NATS KV keys live on subjects, and subject write permissions are enforced **at connect**, before a
message is parsed. So:

- Only the gateway may publish under `cap.root.*`
- Each broker may publish only under `cap.hop.<its-own-agent-id>.*`
- **No broker may read `cap.*` at all.** Only the verification service holds read.

That buys the two properties a signature would have bought:

**Who wrote this link.** The subject prefix proves it. Forging a root capability means
publishing on a subject NATS refuses you at connection time.

**Did each link narrow.** The verifier reads parent and child and compares. A compromised broker
that writes something wider than it received is caught when the next hop resolves the chain.

The integrity comes from connection-time permissions rather than from cryptography. Same
guarantee, no key to custody, rotate, distribute or recover.

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
a child entirely. The four chain checks all pass, and vacuously: a single-entry chain widens
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
stop separating anything. A broker serving a reader-tier request presents the id of a concurrent
operator-tier one, is the named delegate of that entry too, and passes all five checks holding a
capability minted for someone else. No forged write and no second compromise -- and a concurrency
bug in an honest broker reaches the same place as a malicious one, which is the part that should
worry you.

**So the identity the verifier authenticates must be scoped to the request, not to the agent.** A
capability is per-request, and a check that compares it against a per-agent identity is comparing
against the wrong thing. This is a requirement 09 places on the runtime rather than something the
KV scheme can fix from inside: whatever issues the broker's credential must issue a distinct one
per request, so that "the caller" and "the request" are the same subject. The scoped ServiceAccount
pool in the F10 work is the obvious place for it to come from.

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
The verifier sits on the request path, which is the same shape -- and the same cost -- as the NATS auth callout this design
already accepts.

**Revocation is deleting an entry**, which is the other reason to prefer this. A signed token is
valid until it expires no matter what you learn in the meantime.

**The cost** is a lookup on the request path and a dependency on the bus. If the bus is down
there are no messages to authorize, so that dependency is smaller than it first appears.

## What this does not solve

Three things, stated so nobody assumes otherwise.

**Attenuation is code.** A hop that forwards without narrowing is a hole, and no token format or
KV scheme fixes that. Real tension with "structural, not behavioural."

The bound that makes it survivable: a hop can only descend from a parent that named it, and every
chain terminates at a root the gateway minted from the requester's own authority.

> **A broken hop cannot exceed what it was delegated for the request it is serving.** Worst case is
> "narrowed less than intended", never "escalated past the human who asked."

That is the sentence to have ready when someone probes the design, and it is worth knowing exactly
what carries it: the two delegate rules above, a request-scoped caller identity, and nothing else.
The write half stops a hop descending from an origin it was never handed. The read half stops it
presenting that origin directly. The identity is what makes "it" mean this request rather than this
agent -- drop that and the last five words of the claim go with it. It holds under imperfect
implementation, which is the only kind there is, but it does not hold under a missing rule.

**Chain depth is a refusal, not just a cost.** Resolution walks to the root, so a long chain is a
lot of KV reads. Those are now the verifier's reads rather than every broker's, which makes them
cacheable per id -- a chain is immutable once written, so the only invalidation is revocation. Not
a problem at three or four hops.

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
authorizes. That is a real concentration and it is the price of not distributing read. It belongs
in the same tier of scrutiny as the auth callout, with no model attached to it.

---

## Background: this pattern has a name

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

**If we ever need self-contained tokens, use biscuit, not macaroons.** Same append-only
attenuation, built on Ed25519 rather than HMAC: verification needs only the root _public_ key, so
verifiers verify and cannot mint. <https://www.biscuitsec.org/>

The only scenario that would force this is a hop that must authorize without reaching the bus.
Nothing in the current topology needs that.

## The general rule this came from

The same reasoning decided three separate questions:

| Question                                                 | The crypto answer                                                          | What we do instead                                                                                                                                                                 |
| :------------------------------------------------------- | :------------------------------------------------------------------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| How do agents authenticate to the bus?                   | NATS decentralized JWT -- operator key signs accounts, accounts sign users | Auth callout against ServiceAccount tokens the cluster already issues. Every conformant cluster is an OIDC issuer with audience-bound, rotated tokens. **We hold no signing key.** |
| What stops a capability being forged?                    | Sign it, distribute verification keys                                      | A KV entry on a subject the forger cannot publish to. Enforced at connect.                                                                                                         |
| What stops a token being used against the wrong cluster? | Encode a scope, check it                                                   | The token is issued _by_ the target cluster. Another cluster rejects it because a different issuer signed it. **Nothing has to check anything.**                                   |

> **Prefer a boundary that already exists and is enforced by someone else over a check we have to
> write, distribute and operate.**

Every cryptographic check we build is a key to custody, rotate, revoke and recover, plus a
verification path that can have a bug. A structural property has none of those. A token from
cluster C does not work against cluster D whether or not our code is correct today.

It is also why the RBAC-over-IAM measurement felt like a win rather than a setback. We went
looking for a way to _express_ per-cluster scope and found the scope was already structural one
layer down.

## Goals & non-goals

### Goals

- Make **effective authority = agent ceiling ∩ requester** hold across process boundaries, not only
  inside one process.
- Carry it with **no cryptographic key anywhere** -- nothing to custody, rotate, distribute or
  recover.
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
- **Not per-user granularity.** Capabilities carry tiers, so this is as fine-grained as the tier map
  and no finer.

## Verification

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
- **An orphan root is refused:** a chain whose terminal entry does not sit under `cap.root.*` fails,
  including one that terminates at a well-formed `cap.hop.*` entry.
- **Only the gateway mints roots:** any other connection publishing to `cap.root.*` is rejected by
  NATS **at connect**, not by application code.
- **No broker writes in another broker's namespace:** broker A publishing to `cap.hop.<B>.*` is
  rejected at connect.
- **No broker reads the store:** a broker connection attempting any read on `cap.*` is rejected at
  connect. Assert this for a broker that legitimately participates in a chain, since the whole point
  is that participation does not imply read.
- **Revocation is immediate:** delete an entry mid-flight; the next resolution of any id descending
  from it fails.
- **The agent never sees the capability:** from inside an agent container, the capability id and the
  KV store are both unreachable -- no bus credential, no verifier route.

## References

- Birgisson et al., _Macaroons_, NDSS 2014.
- Biscuit: <https://www.biscuitsec.org/>
- The object-capability model generally, for "authority is something you hold and pass on,
  narrowed."
- SPIFFE/SPIRE, if ServiceAccount-token authentication ever needs to span non-Kubernetes
  workloads.
