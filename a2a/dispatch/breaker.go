package dispatch

import (
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
// a row it opens: new submissions there stay queued until openUntil. Past
// that it is half open: it admits one task at a time; the next expiry opens
// it at once, and a task that finishes closes it.
type breaker struct {
	expiries  int
	openUntil time.Time
	halfOpen  bool
	// epoch numbers this opening, so a probe of an earlier one (a breaker
	// closed and opened again) is not this one's.
	epoch int
}

// breakers holds every (profile, tier)'s breaker. The state is in memory:
// a restarted dispatcher starts with every breaker closed. A nil *breakers
// (a Dispatcher not built by New) never opens.
type breakers struct {
	mu sync.Mutex
	m  map[ptKey]*breaker
	// epochs counts openings across all breakers.
	epochs int
}

func newBreakers() *breakers { return &breakers{m: map[ptKey]*breaker{}} }

// wait reports whether a submission of profile at tier must stay queued
// rather than be admitted, and whether that is because its breaker is open:
// while open; and while half open, either with a probe of that profile and
// tier in flight (busy: a task reserved while half open, never one that was
// already running when the breaker opened, such as a parent waiting on this
// child) or with an older submission of theirs waiting (older), so the
// oldest held task is the one tried, one at a time. A held
// submission is never refused for the breaker: it waits, as over a cap, and
// ends with queue-timeout if the breaker never closes. A refusal would let
// one input that keeps killing workers fail every other user's task on that
// tier for the cooldown.
func (b *breakers) wait(profile, tier string, now time.Time, busy, older func() bool) (wait, open bool) {
	if b == nil {
		return false, false
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	br, ok := b.m[ptKey{profile, tier}]
	switch {
	case !ok:
		return false, false
	case now.Before(br.openUntil):
		return true, true
	}
	if !br.halfOpen {
		return false, false
	}
	// Called only here, half open: while closed or open nothing reads them,
	// and each takes the dispatcher's lock and walks its holdings.
	b.mu.Unlock()
	defer b.mu.Lock()
	return busy() || older(), false
}

// probeEpoch is the epoch of profile's breaker at tier if it is half open at
// now (past its cooldown, trying one task at a time), or 0.
func (b *breakers) probeEpoch(profile, tier string, now time.Time) int {
	if b == nil {
		return 0
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if br, ok := b.m[ptKey{profile, tier}]; ok && br.halfOpen && !now.Before(br.openUntil) {
		return br.epoch
	}
	return 0
}

// epoch is the epoch of profile's breaker at tier, or 0 if it is closed.
func (b *breakers) epoch(profile, tier string) int {
	if b == nil {
		return 0
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if br, ok := b.m[ptKey{profile, tier}]; ok {
		return br.epoch
	}
	return 0
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
		b.epochs++
		br.openUntil, br.halfOpen, br.expiries, br.epoch = now.Add(breakerCooldown(cfg)), true, 0, b.epochs
	}
}

// finished records that a task of profile at tier ran to its end, reserved
// as the probe of opening probe (0 for none). Below the threshold it resets
// the run of expiries. Once the breaker has opened, only that opening's
// probe closes it: a task admitted before it opened, ending during the
// cooldown or the half-open phase, says nothing about whether the tier has
// recovered, and closing on it would cut the cooldown short.
func (b *breakers) finished(profile, tier string, probe int) {
	if b == nil {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	k := ptKey{profile, tier}
	if br, ok := b.m[k]; ok && br.halfOpen && probe != br.epoch {
		return
	}
	delete(b.m, k)
}

// open lists the (profile, tier) pairs whose breaker is open at now, and
// those half open, as "profile/tier", for health.
func (b *breakers) open(now time.Time) (open, halfOpen []string) {
	if b == nil {
		return nil, nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	for k, br := range b.m {
		switch {
		case now.Before(br.openUntil):
			open = append(open, k.profile+"/"+k.tier)
		case br.halfOpen:
			halfOpen = append(halfOpen, k.profile+"/"+k.tier)
		}
	}
	sort.Strings(open)
	sort.Strings(halfOpen)
	return open, halfOpen
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

// probing reports whether a probe of profile at tier's current breaker is
// held here.
func (d *Dispatcher) probing(profile, tier string) bool {
	epoch := d.breakers.epoch(profile, tier)
	if epoch == 0 {
		return false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	for k, h := range d.active {
		if k.profile == profile && k.tier == tier && h.probe == epoch {
			return true
		}
	}
	return false
}

// olderWaiting reports whether another submission of hk's profile and tier
// (either kind), queued before queued, is held here by the breaker. One
// waiting over a cap doesn't count: the breaker isn't what holds it, and a
// child must not wait behind a top task the caps hold.
func (d *Dispatcher) olderWaiting(hk holdKey, queued time.Time) bool {
	if d.breakers.epoch(hk.profile, hk.tier) == 0 {
		return false // no breaker: nothing is held, so nothing is older
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	for k, w := range d.waiting {
		if k != hk && k.profile == hk.profile && k.tier == hk.tier && w.why == waitBreaker && w.queued.Before(queued) {
			return true
		}
	}
	return false
}

// probesChild reports whether a submission of kind with correlation is a
// child of the current half-open probe of profile at tier: a probe of this
// opening held here whose correlationId it carries. A correlationId is the
// user interaction a task serves, which its children inherit, so this is
// the probe's family; a child of any other task is not exempt.
func (d *Dispatcher) probesChild(profile, tier, kind, correlation string) bool {
	if kind != lib.SubmitKindChild || correlation == "" {
		return false
	}
	epoch := d.breakers.probeEpoch(profile, tier, d.now())
	if epoch == 0 {
		return false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	for k, h := range d.active {
		if k.profile == profile && k.tier == tier && h.probe == epoch && h.correlation == correlation {
			return true
		}
	}
	return false
}
