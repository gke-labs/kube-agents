// Package dispatch is the profile dispatcher (#3655; the design is
// docs/designs/spec-profiles-dispatch.md, "Admission and holding"): the only
// fetcher and holder of profile submissions. It binds the operator's held
// durable for each (profile, tier, kind), admits or refuses each submission,
// applies the per-profile and install caps, offers an admitted task to one
// warm worker on that worker's own dispatch subject and confirms it to the
// one whose accept answers the live offer (every other accept is abandoned),
// and holds the submission unacknowledged for the task's whole life while
// the worker renews a liveness lease that starts at the confirm. A task
// whose lease expires or whose deadline passes ends in a terminal failure
// and is never run again.
//
// The wire shapes and the install config are #3655's interface contract
// (lib/dispatch.go, from PR 2); the reason codes and the terminal notice
// are provisional until the contract settles them.
package dispatch

import (
	"fmt"
	"slices"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// DefaultRetryDelay is how long a submission that is over a cap, or has
	// no free worker, waits before it is offered to the dispatcher again.
	DefaultRetryDelay = 5 * time.Second
	// DefaultHealthInterval paces the health line.
	DefaultHealthInterval = time.Minute
	// DefaultStallAfter is how long a queue may wait over its cap with no
	// admission before the health line calls it stalled: the shape a
	// cross-chain admission cycle takes before the parents' deadlines end it.
	DefaultStallAfter = 5 * time.Minute
	// DefaultRefreshInterval paces re-reading the config's durables and
	// binding or unbinding them.
	DefaultRefreshInterval = 10 * time.Second
	// DefaultRegrantAttempts bounds the reconnects one config change may
	// cost: a durable the new connection still can't bind (the callout's
	// watch lagging the config) gets another reconnect, up to this many.
	DefaultRegrantAttempts = 5
	// DefaultRegrantBackoff is the pause before the second such reconnect,
	// doubling after.
	DefaultRegrantBackoff = 2 * time.Second
	// DefaultBindAttemptTimeout bounds one bind of an added durable after a
	// reconnect.
	DefaultBindAttemptTimeout = 5 * time.Second
	// DefaultReconnectTimeout bounds one reconnect, well inside the held
	// durables' 2m AckWait, so no held task is redelivered by it.
	DefaultReconnectTimeout = 30 * time.Second
	// DefaultRegrantCooldown is how long the dispatcher waits, after a
	// config change's reconnects all left a durable unbindable, before it
	// tries again: loud, bounded, never a reconnect loop.
	DefaultRegrantCooldown = 2 * time.Minute
	// DefaultBindRetryBase and DefaultBindRetryMax pace the retries of a
	// durable the config lists that the bus doesn't have yet (the operator
	// creates durables asynchronously): not a failure, just not yet.
	DefaultBindRetryBase = time.Second
	DefaultBindRetryMax  = time.Minute
	// DefaultSweepInterval paces the sweep of lease records nothing holds.
	DefaultSweepInterval = time.Minute
	// DefaultSweepAfter is how long past its expiry a lease record must be
	// before the sweep deletes it, when no hold here has it and its
	// durables are bound. It is far longer than the held durables' AckWait
	// and any restart grace, so a record whose submission can still come
	// back has been re-adopted long before: what is left is a record whose
	// submission is gone (aged off the stream, or its durable deleted). A
	// tombstone is not swept on this clock: it is kept for the submit
	// stream's retention plus a margin.
	DefaultSweepAfter = 30 * time.Minute
	// DefaultSubmitter is the one principal whose submissions are held.
	DefaultSubmitter = "gateway"
	// DefaultStream is the stream the durables are on: profile submissions
	// have their own (contract addendum 7), so only the operator can make a
	// consumer on it.
	DefaultStream = lib.ProfileSubmitStream
	// DefaultUnattendedTier is the one unattended tier until tier CRs say
	// which tiers are.
	DefaultUnattendedTier = "unattended"
	// selfSession and selfAgentType are the `from` of the terminals the
	// dispatcher writes.
	selfSession   = "dispatcher"
	selfAgentType = "a2a-dispatcher"
)

// Config is the dispatcher's process-level settings. The install's caps,
// intervals and profiles are not here: they come from the Source, per
// decision.
type Config struct {
	RetryDelay      time.Duration
	HealthInterval  time.Duration
	StallAfter      time.Duration
	RefreshInterval time.Duration
	// RegrantAttempts, RegrantBackoff, BindAttemptTimeout and
	// ReconnectTimeout pace the reconnect a config adding a durable needs.
	RegrantAttempts    int
	RegrantBackoff     time.Duration
	BindAttemptTimeout time.Duration
	ReconnectTimeout   time.Duration
	RegrantCooldown    time.Duration
	// BindRetryBase and BindRetryMax pace a durable's bind retries.
	BindRetryBase time.Duration
	BindRetryMax  time.Duration
	// SweepInterval and SweepAfter pace and bound the sweep of lease
	// records nothing holds.
	SweepInterval time.Duration
	SweepAfter    time.Duration
	Submitter     string
	Stream        string
	// UnattendedTiers are the tiers on the unattended side of the
	// interactive reserve.
	UnattendedTiers []string
}

// DefaultConfig is every default.
func DefaultConfig() Config {
	return Config{
		RetryDelay:      DefaultRetryDelay,
		HealthInterval:  DefaultHealthInterval,
		StallAfter:      DefaultStallAfter,
		RefreshInterval: DefaultRefreshInterval,

		RegrantAttempts:    DefaultRegrantAttempts,
		RegrantBackoff:     DefaultRegrantBackoff,
		BindAttemptTimeout: DefaultBindAttemptTimeout,
		ReconnectTimeout:   DefaultReconnectTimeout,
		RegrantCooldown:    DefaultRegrantCooldown,
		BindRetryBase:      DefaultBindRetryBase,
		BindRetryMax:       DefaultBindRetryMax,
		SweepInterval:      DefaultSweepInterval,
		SweepAfter:         DefaultSweepAfter,
		Submitter:          DefaultSubmitter,
		Stream:             DefaultStream,
		UnattendedTiers:    []string{DefaultUnattendedTier},
	}
}

// Validate refuses a config the dispatcher can't run on.
func (c Config) Validate() error {
	switch {
	case c.RetryDelay <= 0 || c.HealthInterval <= 0 || c.StallAfter <= 0 || c.RefreshInterval <= 0:
		return fmt.Errorf("dispatch config: retry delay, health interval, stall threshold and refresh interval must be positive")
	case c.RegrantAttempts < 1 || c.RegrantBackoff <= 0 || c.BindAttemptTimeout <= 0 || c.ReconnectTimeout <= 0 || c.RegrantCooldown <= 0:
		return fmt.Errorf("dispatch config: regrant attempts, backoff, cooldown, bind timeout and reconnect timeout must be positive")
	case c.BindRetryBase <= 0 || c.BindRetryMax < c.BindRetryBase:
		return fmt.Errorf("dispatch config: bind retry base must be positive and at most the max")
	case c.SweepInterval <= 0 || c.SweepAfter <= 0:
		return fmt.Errorf("dispatch config: sweep interval and sweep age must be positive")
	case c.Submitter == "" || c.Stream == "":
		return fmt.Errorf("dispatch config: submitter and stream are required")
	}
	return nil
}

func (c Config) unattended(tier string) bool { return slices.Contains(c.UnattendedTiers, tier) }
