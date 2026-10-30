package dispatch

import (
	"fmt"
	"sort"
	"sync"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The breaker's defaults, used when the dispatch config leaves a value at 0
// (a config rendered before the operator knew the field).
const (
	defaultBreakerThreshold = 3
	defaultBreakerCooldown  = 5 * time.Minute
)

// ptKey names one (profile, tier), the unit a breaker guards: one tier's
// workers share an identity, an image and a pool.
type ptKey struct{ profile, tier string }

// breaker is one (profile, tier)'s run of lease expiries. After threshold in
// a row it opens: new submissions there are refused until openUntil. Past
// that it is half open: it admits one task at a time; the next expiry opens
// it at once, and a task that finishes closes it.
type breaker struct {
	expiries  int
	openUntil time.Time
	halfOpen  bool
}

// breakers holds every (profile, tier)'s breaker. The state is in memory:
// a restarted dispatcher starts with every breaker closed. A nil *breakers
// (a Dispatcher not built by New) never opens.
type breakers struct {
	mu sync.Mutex
	m  map[ptKey]*breaker
}

func newBreakers() *breakers { return &breakers{m: map[ptKey]*breaker{}} }

// admit refuses a submission while profile's breaker at tier is open. Half
// open, it lets one task through at a time: while busy (a task of that
// profile and tier is in flight) it says wait, and the submission stays
// queued rather than being refused.
func (b *breakers) admit(profile, tier string, now time.Time, busy bool) (*refusal, bool) {
	if b == nil {
		return nil, false
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	br, ok := b.m[ptKey{profile, tier}]
	switch {
	case !ok:
		return nil, false
	case now.Before(br.openUntil):
		return &refusal{ReasonBreakerOpen, fmt.Sprintf("tasks of profile %s at tier %s kept losing their workers; "+
			"refusing new ones until %s", profile, tier, br.openUntil.UTC().Format(time.RFC3339))}, false
	case br.halfOpen && busy:
		return nil, true
	}
	return nil, false
}

// expired counts a task of profile at tier whose lease expired, and opens
// its breaker at the threshold, or at once when half open.
func (b *breakers) expired(cfg lib.DispatchConfig, profile, tier string, now time.Time) {
	if b == nil {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	k := ptKey{profile, tier}
	br := b.m[k]
	if br == nil {
		br = &breaker{}
		b.m[k] = br
	}
	br.expiries++
	if br.halfOpen || br.expiries >= breakerThreshold(cfg) {
		br.openUntil, br.halfOpen, br.expiries = now.Add(breakerCooldown(cfg)), true, 0
	}
}

// finished closes profile's breaker at tier: a task there ran to its end.
func (b *breakers) finished(profile, tier string) {
	if b == nil {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.m, ptKey{profile, tier})
}

// open lists the (profile, tier) pairs refusing submissions at now, as
// "profile/tier", for health.
func (b *breakers) open(now time.Time) []string {
	if b == nil {
		return nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	var out []string
	for k, br := range b.m {
		if now.Before(br.openUntil) {
			out = append(out, k.profile+"/"+k.tier)
		}
	}
	sort.Strings(out)
	return out
}

func breakerThreshold(cfg lib.DispatchConfig) int {
	if cfg.BreakerThreshold > 0 {
		return cfg.BreakerThreshold
	}
	return defaultBreakerThreshold
}

func breakerCooldown(cfg lib.DispatchConfig) time.Duration {
	if cfg.BreakerCooldownSeconds > 0 {
		return seconds(cfg.BreakerCooldownSeconds)
	}
	return defaultBreakerCooldown
}

// inFlight reports whether a task of profile at tier is held here.
func (d *Dispatcher) inFlight(profile, tier string) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	for k := range d.active {
		if k.profile == profile && k.tier == tier {
			return true
		}
	}
	return false
}
