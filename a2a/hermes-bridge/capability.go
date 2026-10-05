package hermesbridge

import (
	"context"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The bridge's half of the capability envelope, and the reason it exists
// rather than the session executor's being enough.
//
// The operator renders A2A_SPAWN_SESSIONS=true and renders no
// A2A_DEFAULT_ADDRESSEE at all, so a default `mode: next` install leaves the
// gateway on its own default — `platform` — and every turn a user types is
// executed HERE. A session pod is reached only by an explicit `delegate:`.
// So for the shipped default, this file is the whole enforcement point: the
// gateway was minting a capability for every one of those tasks and nothing
// was reading it. A control that runs on the route nobody takes is a control
// in documentation only.
//
// The check is the session executor's, in a2a/worker-adapter/capability.go,
// and the two deliberately share capability.RefFromAuthority so the parse
// cannot drift. One thing differs, and it is the identity: a session pod is
// its own name and asks on `a2a.cap.verify.<pod>`, whereas this executor's
// name is its PROFILE. It dials as the static `bridge` user but asks as
// `platform`, because `platform` is the addressee the gateway wrote into the
// capability's delegate field, and the verifier checks the delegate against
// the caller. That is sound for the same reason the session case is: the
// subject is the identity, the grant `a2a.cap.verify.<profile>` is exactly one
// subject, and the server refuses `bridge` on any other. See bridgeIdentity in
// k8s-operator/internal/controller/platformagent_a2a_identities.go.

// capabilityRefusal returns the reason this submission must be refused, or ""
// to proceed. Called by a worker before the task is started, so a refused
// task never reaches a hermes subprocess and never spends a model call; see
// Bridge.capabilityPermits for why it is not called on the consumer callback.
func (b *Bridge) capabilityRefusal(ctx context.Context, env *lib.Envelope) string {
	ref, present, err := capability.RefFromAuthority(env.Authority)
	switch {
	case err != nil:
		return "reason: capability-refused - the authority block is not well-formed"
	case !present && b.cfg.CapabilityOptional:
		// The rollout window, and the only one: a gateway that predates
		// the mint publishes `grants: null`. Loud, because an install
		// left here has the control switched off.
		b.cfg.Logger.Warn("task carries no capability and A2A_CAPABILITY_REQUIRED is false; executing unauthorized",
			"task", env.TaskID)
		return ""
	case !present:
		return "reason: capability-refused - the submission carries no capability"
	}

	client, err := capability.NewClient(b.nc, b.cfg.Profile)
	if err != nil {
		return "reason: capability-refused - this executor has no verify subject of its own"
	}
	if err := client.Check(ctx, ref, capability.VerbTaskExecute, b.cfg.Scope); err != nil {
		// capability.Reason carries the rule the verifier named, which
		// never quotes anything the caller supplied.
		b.cfg.Logger.Warn("capability refused this task",
			"task", env.TaskID, "verb", capability.VerbTaskExecute,
			"resource", b.cfg.Scope, "reason", capability.Reason(err))
		return "reason: capability-refused - " + capability.Reason(err)
	}
	b.cfg.Logger.Info("capability permits this task",
		"task", env.TaskID, "verb", capability.VerbTaskExecute, "resource", b.cfg.Scope)
	return ""
}
