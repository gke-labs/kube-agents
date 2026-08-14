# 09 — The Capability Envelope

**Status:** Design. Not built. This document specifies how a request's authority travels between
agents once they are separate workloads.

[05-system-architecture.md](05-system-architecture.md) describes agents as separate principals and
[03-security-model.md](03-security-model.md) states that model output is never an authorization
signal. Both assume a request carries the authority of the human who made it, across every hop.
Nothing today carries it: the personas share one process, so "who asked" is ambient rather than
transmitted. This is the mechanism that makes it transmitted.

## The recommendation, first

**No token format.  Nothing signed.  No cryptographic key anywhere in the design.**

The capability lives in NATS KV.  The message on the bus carries only a lookup id.

**"Key" below means a KV lookup key** -- a string like `cap.root.req-8f2a` -- and never a
cryptographic key.  There are none in this design.

## How it works

**Gateway.**  Mints the capability, writes it to KV under `cap.root.<request-id>`, and puts the id
-- not the capability -- into the A2A message.

**Broker.**  Reads the id off the message, looks the capability up in KV.  The agent behind it
never sees either.

**Attenuation.**  A hop that narrows writes a *new* entry -- narrower capability, pointer to its
parent -- under its own namespace, and passes the new id downstream.

**Verification.**  Walk the chain to the root.  Confirm the root sits under `cap.root.*`.  Confirm
each link is narrower than its parent.  Refuse otherwise.

```
   gateway   writes  cap.root.req-8f2a        = {tier: operator, scope: project-P}
                     └─ message carries "req-8f2a"

   hop A     reads   cap.root.req-8f2a
             writes  cap.hop.fleet-recon.1    = {..., scope: cluster-C}   parent: cap.root.req-8f2a
                     └─ message carries "cap.hop.fleet-recon.1"

   hop B     reads   that, walks to root, checks each link narrows
             writes  cap.hop.platform-a.7     = {tier: reader, scope: cluster-C}
                     └─ and so on
```

## Why this needs no crypto

NATS KV keys live on subjects, and subject write permissions are enforced **at connect**, before a
message is parsed.  So:

- Only the gateway may publish under `cap.root.*`
- Each broker may publish only under `cap.hop.<its-own-agent-id>.*`

That buys the two properties a signature would have bought:

**Who wrote this link.**  The subject prefix proves it.  Forging a root capability means
publishing on a subject NATS refuses you at connection time.

**Did each link narrow.**  The verifier reads parent and child and compares.  A compromised broker
that writes something wider than it received is caught by the next hop walking the chain.

The integrity comes from connection-time permissions rather than from cryptography.  Same
guarantee, no key to custody, rotate, distribute or recover.

**Revocation is deleting an entry**, which is the other reason to prefer this.  A signed token is
valid until it expires no matter what you learn in the meantime.

**The cost** is a lookup on the request path and a dependency on the bus.  If the bus is down
there are no messages to authorize, so that dependency is smaller than it first appears.

## What this does not solve

Two things, stated so nobody assumes otherwise.

**Attenuation is code.**  A hop that forwards without narrowing is a hole, and no token format or
KV scheme fixes that.  Real tension with "structural, not behavioural."

The bound that makes it survivable: a hop can only forward what it *received*, and every
capability descends from one the gateway minted from the requester's own authority.

> **A broken hop cannot exceed the origin.**  Worst case is "narrowed less than intended", never
> "escalated past the human who asked."

That is the sentence to have ready when someone probes the design.  It holds under imperfect
implementation, which is the only kind there is.

**Chain depth.**  Verification walks to the root, so a long chain is a lot of KV reads.  Not a
problem at three or four hops.  Worth watching if agent-to-agent chains get deeper.

---

## Background: this pattern has a name

Everything above is an application of **macaroons**, and it is worth being able to say so.

> Birgisson, Politz, Erlingsson, Taly, Vrable, Lentczner.  *Macaroons: Cookies with Contextual
> Caveats for Decentralized Authorization in the Cloud.*  NDSS 2014.

A Google paper, which is convenient for the audience.  The core idea is an authority token that
any holder can narrow by appending a caveat, and that nobody can widen.  That is precisely the
C_new ⊆ C_old chain, and we should present it as applying a known pattern rather than as a scheme
we invented.  Naming it first turns "did you two design your own crypto?" into "yes, that one."

**We are not using the macaroon construction itself**, and there is a specific reason worth
recording.

Macaroons chain with symmetric HMAC: `sig = HMAC(root_key, id)`, then `sig = HMAC(sig, caveat)`
for each caveat.  Appending needs no key, which is the elegant part.  But **verification requires
the root key** -- so every component that verifies also holds the key that mints.

In hub-and-spoke that means shipping a fleet-wide minting key to every broker in every spoke.  One
compromised broker becomes a fleet-wide authority.  Bad trade, and easy to walk into if someone
reads the citation and reaches for a library.

**If we ever need self-contained tokens, use biscuit, not macaroons.**  Same append-only
attenuation, built on Ed25519 rather than HMAC: verification needs only the root *public* key, so
verifiers verify and cannot mint.  <https://www.biscuitsec.org/>

The only scenario that would force this is a hop that must authorize without reaching the bus.
Nothing in the current topology needs that.

## The general rule this came from

The same reasoning decided three separate questions:

| Question | The crypto answer | What we do instead |
| :--- | :--- | :--- |
| How do agents authenticate to the bus? | NATS decentralized JWT -- operator key signs accounts, accounts sign users | Auth callout against ServiceAccount tokens the cluster already issues.  Every conformant cluster is an OIDC issuer with audience-bound, rotated tokens.  **We hold no signing key.** |
| What stops a capability being forged? | Sign it, distribute verification keys | A KV entry on a subject the forger cannot publish to.  Enforced at connect. |
| What stops a token being used against the wrong cluster? | Encode a scope, check it | The token is issued *by* the target cluster.  Another cluster rejects it because a different issuer signed it.  **Nothing has to check anything.** |

> **Prefer a boundary that already exists and is enforced by someone else over a check we have to
> write, distribute and operate.**

Every cryptographic check we build is a key to custody, rotate, revoke and recover, plus a
verification path that can have a bug.  A structural property has none of those.  A token from
cluster C does not work against cluster D whether or not our code is correct today.

It is also why the RBAC-over-IAM measurement felt like a win rather than a setback.  We went
looking for a way to *express* per-cluster scope and found the scope was already structural one
layer down.

## References

- Birgisson et al., *Macaroons*, NDSS 2014.
- Biscuit: <https://www.biscuitsec.org/>
- The object-capability model generally, for "authority is something you hold and pass on,
  narrowed."
- SPIFFE/SPIRE, if ServiceAccount-token authentication ever needs to span non-Kubernetes
  workloads.
