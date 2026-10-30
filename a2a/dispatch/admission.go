package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Verifier is the one verifier call the dispatcher makes: may the grant at
// ref run profile at tier for target. It is capability.Client's
// CheckProfileRun (#3734); a nil error means permitted, anything else is a
// refusal.
type Verifier interface {
	CheckProfileRun(ctx context.Context, ref capability.Ref, profile string, tier capability.Tier, target capability.Scope) error
}

// The task parameters that make a cluster target, and the target shapes the
// verifier is asked about (contract section 6).
const (
	paramProject      = "project"
	paramLocation     = "location"
	paramCluster      = "cluster"
	clusterTargetForm = "project/%s/location/%s/cluster/%s"
	namespaceTarget   = "namespace/"
)

// errStopping marks an admission that could not finish because the
// dispatcher is shutting down: not a verdict, so the submission is given
// back rather than refused.
var errStopping = errors.New("dispatcher stopping")

// refusal is an admission verdict against a submission.
type refusal struct {
	reason string
	detail string
}

// errHeldBack marks an admission that is neither a verdict nor a stop: the
// submission stays queued, as over a cap, and comes back (an open breaker,
// or a half-open one trying the oldest held task first).
var errHeldBack = errors.New("held back at admission")

// admit runs the admission checks in order, cheapest first: the tier, the
// queue age, the parameters, then the verifier. A nil refusal admits. The
// verifier's permit is reused for verifiedFor across the same submission's
// retries over a cap, so a queue waiting on a cap doesn't call the verifier
// once per retry delay, but only while the retry asks for the same ref and
// target the permit was given for; the cheap checks run every time.
func (d *Dispatcher) admit(ctx context.Context, del lib.Delivery, snap lib.DispatchConfig, prof lib.DispatchProfile, hk holdKey, now time.Time) (*refusal, error) {
	env := del.Envelope()
	tier := hk.tier
	if !allowsTier(prof, tier) {
		return &refusal{ReasonTierNotAllowed, fmt.Sprintf("profile %s does not allow tier %s", prof.Name, tier)}, nil
	}
	queued := del.Received()
	if queued.IsZero() {
		queued = now
	}
	if wait, _ := d.breakers.wait(prof.Name, tier, now, d.inFlight(prof.Name, tier),
		d.olderWaiting(prof.Name, tier, hk.taskID, queued)); wait {
		return nil, errHeldBack
	}
	if rec := del.Received(); !rec.IsZero() {
		if age, limit := now.Sub(rec), queueTimeout(prof); age > limit {
			return &refusal{ReasonQueueTimeout, fmt.Sprintf("queued %s, over the profile's %s", age.Round(time.Second), limit)}, nil
		}
	}
	target, bad := taskTarget(env, prof, snap.Namespace)
	if bad != "" {
		return &refusal{ReasonParametersInvalid, bad}, nil
	}
	ref, present, err := capability.RefFromAuthority(env.Authority)
	switch {
	case err != nil:
		return &refusal{ReasonVerifierRefused, "the authority block is not well-formed"}, nil
	case !present:
		return &refusal{ReasonVerifierRefused, "the submission carries no capability"}, nil
	case d.verifier == nil:
		return &refusal{ReasonVerifierRefused, "no verifier is configured"}, nil
	}
	// The permit is for what the verifier was asked: this ref, this target.
	// The stream keeps one submission per subject and a newer one replaces
	// it, so a retry can carry another capability or another target under
	// the same key: that is a cache miss, checked afresh, never the old
	// permit stretched over an unchecked target.
	asked := permit{ref: ref, target: target}
	d.mu.Lock()
	p, cached := d.verified[hk]
	d.mu.Unlock()
	if cached && now.Sub(p.at) <= verifiedFor && p.ref == asked.ref && p.target == asked.target {
		return nil, nil
	}
	if err := d.verifier.CheckProfileRun(ctx, ref, prof.Name, capability.Tier(tier), target); err != nil {
		if ctx.Err() != nil {
			return nil, errStopping
		}
		return &refusal{ReasonVerifierRefused, capability.Reason(err)}, nil
	}
	asked.at = now
	d.mu.Lock()
	d.verified[hk] = asked
	d.mu.Unlock()
	return nil, nil
}

// permit is a verifier permit admission reuses: when it was given, and the
// capability ref and target it was given for.
type permit struct {
	at     time.Time
	ref    capability.Ref
	target capability.Scope
}

// taskTarget reads the submission's task parameters, checks them against
// the profile's declared ones, and builds the verifier's target: the cluster
// path when the task names a cluster, the install's namespace otherwise. A
// non-empty bad is the parameters-invalid detail.
func taskTarget(env *lib.Envelope, prof lib.DispatchProfile, namespace string) (target capability.Scope, bad string) {
	var msg lib.Message
	if err := json.Unmarshal(env.Payload, &msg); err != nil {
		return "", "the submission's message does not parse"
	}
	tp, _, err := lib.TaskParametersOf(msg.Parts)
	if err != nil {
		return "", err.Error()
	}
	params := tp.Parameters
	var missing []string
	for _, p := range prof.Parameters {
		if _, ok := params[p.Name]; p.Required && !ok {
			missing = append(missing, p.Name)
		}
	}
	if len(missing) > 0 {
		return "", "missing required parameters: " + strings.Join(missing, ", ")
	}
	project, hasProject := params[paramProject]
	location, hasLocation := params[paramLocation]
	cluster, hasCluster := params[paramCluster]
	switch {
	case hasProject && hasLocation && hasCluster:
		return capability.Scope(fmt.Sprintf(clusterTargetForm, project, location, cluster)), ""
	case hasProject || hasLocation || hasCluster:
		return "", "a cluster target needs all of project, location and cluster"
	case namespace == "":
		return "", "the task names no cluster and the dispatcher has no namespace to scope it to"
	}
	return capability.Scope(namespaceTarget + namespace), ""
}
