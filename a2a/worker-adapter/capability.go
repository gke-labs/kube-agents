package workeradapter

import (
	"context"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The executor's half of the capability envelope.
//
// The gateway minted this task's capability at ingress and put a reference to
// it — a key and the revision the write returned — in the envelope's
// `authority.grants`. The executor does not read the entry (no broker may) and
// does not trust the block. It hands the reference to the verifier over its
// own caller-scoped subject and acts on the verdict.
//
// A refusal here is terminal and pre-spend: rejected on the stream, before the
// harness starts, before a model is called. That is deliberate. An executor
// that logged the refusal and ran anyway would be a capability system in
// documentation only, and an executor that failed rather than rejected would
// invite a supervisor retry against a capability that will refuse it again.
//
// Every failure is a refusal, including the ones that are not the caller's
// fault: a malformed block, a verifier that cannot be reached, an answer that
// does not parse. The verifier being down stops work; that is the cost of the
// control and it is named in the deployment's runbook.

// capabilityRefusal returns the reason this task must be refused, or "" to
// proceed. It is called before the prompt is looked at: an executor that
// reported "nothing to execute" to a caller it was never going to serve would
// be answering a question it had no business answering.
func (a *adapter) capabilityRefusal(ctx context.Context, nc *nats.Conn, origin *lib.Envelope) string {
	ref, present, err := capability.RefFromAuthority(origin.Authority)
	switch {
	case err != nil:
		return "reason: capability-refused - the authority block is not well-formed"
	case !present && a.cfg.CapabilityOptional:
		// The rollout window, and the only one: a gateway that predates
		// the mint publishes `grants: null`. Loud, because an install
		// left here has the control switched off.
		a.log.Warn("task carries no capability and A2A_CAPABILITY_REQUIRED is false; executing unauthorized",
			"task", a.cfg.TaskID)
		return ""
	case !present:
		return "reason: capability-refused - the submission carries no capability"
	}

	client, err := capability.NewClient(nc, a.cfg.Addressee())
	if err != nil {
		return "reason: capability-refused - this executor has no verify subject of its own"
	}
	if err := client.Check(ctx, ref, capability.VerbTaskExecute, a.cfg.Scope); err != nil {
		// capability.Reason carries the rule the verifier named, which
		// never quotes anything the caller supplied.
		a.log.Warn("capability refused this task",
			"task", a.cfg.TaskID, "verb", capability.VerbTaskExecute,
			"resource", a.cfg.Scope, "reason", capability.Reason(err))
		return "reason: capability-refused - " + capability.Reason(err)
	}
	a.log.Info("capability permits this task",
		"task", a.cfg.TaskID, "verb", capability.VerbTaskExecute, "resource", a.cfg.Scope)
	return ""
}
