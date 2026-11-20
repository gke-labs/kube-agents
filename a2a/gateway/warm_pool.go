package gateway

import (
	"context"
	"errors"
	"slices"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// The warm pool: session pods started before any conversation needs one, so
// a new conversation's first turn is handed a running pod instead of waiting
// on scheduling and the image pull (the bulk of a cold first turn when the
// image is not on the node yet, which is every conversation's first turn after
// a release changes the tag).
//
// A session pod's whole bus identity is its name. The callout grants a pod
// the subjects named for its attested pod name and nothing else
// (authcallout/session.go), the capability's delegate is the addressee the
// gateway mints each task for (mintTaskCapability), and every consumer and
// inbox derives from the name (workeradapter.Config.consumerStem). So a warm
// pod is started under a freshly minted name, like any incarnation, and
// becomes a conversation's session when the gateway addresses that
// conversation's first task to the name instead of minting one
// (claimOrMint). Nothing is re-bound and nothing is widened: a claimed pod
// holds exactly what a pod spawned fresh under that name would, and an
// unclaimed one can run nothing, since every task needs a capability naming
// the pod and the gateway mints one only for an addressee it has set.
//
// What the gateway cannot do is patch the claimed pod (its Role has no pod
// patch or update, and the admission policy it is held to covers CREATE
// only), so the pod keeps the warm mark and never gets the conversation
// annotations a spawned pod carries. The claim index in the session-state
// bucket stands in for them (Registry.ClaimWarmPod), and sessionPods and
// claimedOrphans read it, so the cap count, the cap eviction and both sweeps
// see a claimed warm pod as the session pod it is, and an unclaimed one as
// no session at all.
//
// Only the leader runs the pool (it starts in Run), and only the leader
// takes turns, so warmMu, which serializes a claim against the pool's own
// deletes, is one process's lock.
const (
	// warmRecycleAge is how long a warm pod waits unclaimed. Its adapter
	// exits at this age if nothing has claimed it
	// (lib.EnvSessionWarmIdleSeconds), counted from its own start, which is
	// after the pod's creation; so under a gateway that doesn't know warm
	// pods (a rollback) each one is gone within this age plus its start, and
	// that gateway's sweep deletes it.
	warmRecycleAge = 30 * time.Minute
	// warmClaimMargin is how long before warmRecycleAge, by the pod's
	// creation time, the gateway stops handing a warm pod a conversation and
	// the pool replaces it: room for the first task to reach the adapter
	// well before the adapter's own idle exit, whatever the clocks of the
	// API server and the node disagree by.
	warmClaimMargin = 5 * time.Minute
	// warmClaimableAge is the age past which a warm pod is no longer
	// claimed and is replaced.
	warmClaimableAge = warmRecycleAge - warmClaimMargin
	// warmPoolInterval paces the pool's pass; warmPassTimeout bounds one.
	warmPoolInterval = 30 * time.Second
	warmPassTimeout  = time.Minute
	// warmMinCap is the smallest session cap the pool runs under: it never
	// takes the last slot, so below two there is none to spare.
	warmMinCap = 2
)

// warmPoolOn reports whether this gateway keeps a pool and claims from it:
// pods to create, a warm count asked for, reuse on (a claimed pod serves its
// conversation as a reused pod does), and a cap with a slot to spare.
func (g *Gateway) warmPoolOn() bool {
	return g.spawner != nil && g.cfg.SessionReuse && g.cfg.WarmSessions > 0 && g.cfg.MaxSessions >= warmMinCap
}

// warmPoolSize is how many warm pods the pool keeps: the configured count,
// less what would take the cap's last slot with live sessions running.
// Never negative.
func warmPoolSize(warm, maxSessions, live int) int {
	return max(0, min(warm, maxSessions-1-live))
}

// warmClaimable reports whether a warm pod can be handed a conversation now.
func warmClaimable(p warmPod, claimed bool, now time.Time) bool {
	return p.Ready && !p.Gone && !claimed && now.Sub(p.Created) < warmClaimableAge
}

// warmClaimEligible reports whether rec's next incarnation may be a warm pod:
// a session-routed conversation that will reuse its pod, on the pool's
// profile, with no earlier turn. No earlier turn is what makes the pod's
// rehydration primer empty, and a warm pod has none: a primer is read once at
// start from the pod's own annotation, which a warm pod can't be given. A
// turn on a conversation with history (a rehydration, a wake) spawns cold, as
// does a one-shot Delegate, which never calls this.
func warmClaimEligible(cfg *Config, rec *SessionRecord) bool {
	return cfg.reusePod(rec) && rec.Profile == sessionProfile && len(rec.Tasks) == 0
}

// claimOrMint names rec's next incarnation: a claimed warm pod when one is
// Ready (claimWarm), a freshly minted name otherwise, which ensureSessionPod
// then spawns as it always did.
func (g *Gateway) claimOrMint(ctx context.Context, rec *SessionRecord) {
	if pod := g.claimWarm(ctx, rec); pod != "" {
		rec.BusSession, rec.Addressee, rec.warmClaim = pod, pod, pod
		g.log.Info("claimed a warm session pod", "conversation", rec.Key, "pod", pod)
		return
	}
	rec.BusSession = mintSessionName(rec.Profile)
	rec.Addressee = rec.BusSession
}

// claimWarm claims a Ready warm pod for rec, oldest first, and returns its
// name, or "" when rec may not have one or none is Ready. Any failure is a
// cold spawn, never a refused turn.
func (g *Gateway) claimWarm(ctx context.Context, rec *SessionRecord) string {
	if !g.warmPoolOn() || !warmClaimEligible(g.cfg, rec) {
		return ""
	}
	g.warmMu.Lock()
	defer g.warmMu.Unlock()
	pods, err := g.spawner.WarmPods(ctx)
	if err != nil {
		g.log.Warn("warm pool: pod list failed; spawning cold", "conversation", rec.Key, "err", err)
		return ""
	}
	now := time.Now()
	pods = slices.DeleteFunc(pods, func(p warmPod) bool { return !warmClaimable(p, false, now) })
	if len(pods) == 0 {
		return ""
	}
	claims, err := g.reg.WarmClaims(ctx)
	if err != nil {
		g.log.Warn("warm pool: claim index read failed; spawning cold", "conversation", rec.Key, "err", err)
		return ""
	}
	slices.SortFunc(pods, func(a, b warmPod) int { return a.Created.Compare(b.Created) })
	for _, p := range pods {
		if _, claimed := claims[p.Name]; claimed {
			continue
		}
		err := g.reg.ClaimWarmPod(ctx, p.Name, rec.Key)
		if errors.Is(err, jetstream.ErrKeyExists) {
			continue
		}
		if err != nil {
			g.log.Warn("warm pool: claim failed; spawning cold", "conversation", rec.Key, "pod", p.Name, "err", err)
			return ""
		}
		return p.Name
	}
	return ""
}

// sessionPods is the spawner's live session pods as the cap and the sweep
// must see them: a claimed warm pod with the conversation the claim index
// names, and an unclaimed one left out. An unclaimed warm pod is no
// conversation's, so it holds no cap slot (the pool sizes itself inside the
// cap, and the operator's quota headroom and TASKS budget count it apart),
// and it is no stranded orphan for the untracked sweep or a candidate for
// the cap's eviction. The claim index is read only when a warm pod is
// listed.
func (g *Gateway) sessionPods(ctx context.Context) ([]sessionPod, error) {
	pods, err := g.spawner.SessionPods(ctx)
	if err != nil || !slices.ContainsFunc(pods, func(p sessionPod) bool { return p.Warm }) {
		return pods, err
	}
	claims, err := g.reg.WarmClaims(ctx)
	if err != nil {
		return nil, err
	}
	out := pods[:0]
	for _, p := range pods {
		if p.Warm {
			key, claimed := claims[p.PodName]
			if !claimed {
				continue
			}
			p.SessionKey = key
		}
		out = append(out, p)
	}
	return out, nil
}

// claimedOrphans fills in the conversation of each terminal warm pod the
// claim index names, as the annotations of a spawned pod would: its session
// key, and its own name as the addressee. A warm pod carries no task
// annotation, and needs none: the task it may owe a terminal for is its
// conversation's active one (sweepTasks).
func (g *Gateway) claimedOrphans(ctx context.Context, orphans []orphanPod) ([]orphanPod, error) {
	if !slices.ContainsFunc(orphans, func(o orphanPod) bool { return o.Warm }) {
		return orphans, nil
	}
	claims, err := g.reg.WarmClaims(ctx)
	if err != nil {
		return nil, err
	}
	for i := range orphans {
		if key, claimed := claims[orphans[i].PodName]; orphans[i].Warm && claimed {
			orphans[i].SessionKey = key
			orphans[i].Addressee = orphans[i].PodName
		}
	}
	return orphans, nil
}

// warmPoolLoop is the leader's pool keeper.
func (g *Gateway) warmPoolLoop(ctx context.Context) {
	g.warmPoolOnce(ctx)
	ticker := time.NewTicker(warmPoolInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			g.warmPoolOnce(ctx)
		}
	}
}

// warmPoolOnce is one pass of the pool: drop claims whose pod is gone,
// replace unclaimed warm pods at warmClaimableAge, delete any beyond the
// configured count, and create pods up to warmPoolSize. It deletes only
// unclaimed pods, under warmMu, so never one a turn is claiming. A pod is
// deleted at the age past which nothing claims it, before it is replaced,
// so the pool never holds more warm pods than the operator budgeted for:
// for the seconds a replacement takes to start, a new conversation spawns
// cold.
func (g *Gateway) warmPoolOnce(ctx context.Context) {
	ctx, cancel := context.WithTimeout(ctx, warmPassTimeout)
	defer cancel()
	g.warmMu.Lock()
	defer g.warmMu.Unlock()
	warm, err := g.spawner.WarmPods(ctx)
	if err != nil {
		g.log.Error("warm pool: pod list failed", "err", err)
		return
	}
	claims, err := g.reg.WarmClaims(ctx)
	if err != nil {
		g.log.Error("warm pool: claim index read failed", "err", err)
		return
	}
	present := make(map[string]bool, len(warm))
	for _, p := range warm {
		present[p.Name] = true
	}
	for pod := range claims {
		if !present[pod] {
			if err := g.reg.DropWarmClaim(ctx, pod); err != nil {
				g.log.Warn("warm pool: stale claim not dropped", "pod", pod, "err", err)
			}
		}
	}
	pods, err := g.sessionPods(ctx)
	if err != nil {
		g.log.Error("warm pool: live count failed", "err", err)
		return
	}
	live := len(pods) + len(g.capHoldSnapshot("", time.Now()))
	now := time.Now()
	var fresh []warmPod
	for _, p := range warm {
		if _, claimed := claims[p.Name]; claimed || p.Gone {
			continue
		}
		if now.Sub(p.Created) >= warmClaimableAge {
			g.deleteWarm(ctx, p.Name, "aged past its claim window")
			continue
		}
		fresh = append(fresh, p)
	}
	slices.SortFunc(fresh, func(a, b warmPod) int { return a.Created.Compare(b.Created) })
	for len(fresh) > g.cfg.WarmSessions {
		g.deleteWarm(ctx, fresh[0].Name, "beyond the configured warm count")
		fresh = fresh[1:]
	}
	for n := len(fresh); n < warmPoolSize(g.cfg.WarmSessions, g.cfg.MaxSessions, live); n++ {
		name := mintSessionName(sessionProfile)
		if err := g.spawner.SpawnWarm(ctx, name); err != nil {
			// Next pass tries again; a create refused here (the quota, the
			// admission policy) would be refused for a cold spawn too, and
			// that turn says so.
			g.log.Error("warm pool: warm pod create failed", "pod", name, "err", err)
			return
		}
		g.log.Info("warm pool: started a warm session pod", "pod", name)
	}
}

func (g *Gateway) deleteWarm(ctx context.Context, pod, why string) {
	if err := g.spawner.Delete(ctx, pod); err != nil {
		g.log.Warn("warm pool: warm pod delete failed", "pod", pod, "why", why, "err", err)
		return
	}
	g.log.Info("warm pool: deleted an unclaimed warm pod", "pod", pod, "why", why)
}
