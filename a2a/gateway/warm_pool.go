package gateway

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
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
// takes turns. A turn claiming a pod and the pool deleting one mark the pod
// under warmMu first, and neither touches a pod the other has marked; the
// lock is held only for the marking, so a claim never waits on the pool's
// API calls. A delete also re-reads the claim index and checks this gateway
// still leads just before it goes (deleteWarmIfUnclaimed), so neither a claim
// made in between nor an overlapping leader's turn loses its pod.
//
// The claim index is not the only thing keeping a pod to one conversation: a
// pod is claimed only if nothing was ever published to its task subjects
// (warmPodUsed), so a claim entry lost from the bucket can't hand a pod in
// use to a second conversation. Such a pod is left alone, not deleted.
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
// the session route (on the platform route nothing new runs, whatever
// A2A_WARM_SESSIONS says; the operator renders it 0 there too), pods to
// create, a warm count asked for, reuse on (a claimed pod serves its
// conversation as a reused pod does), and a cap with a slot to spare.
func (g *Gateway) warmPoolOn() bool {
	return g.cfg.DefaultAddressee == RouteSession && g.spawner != nil && g.cfg.SessionReuse &&
		g.cfg.WarmSessions > 0 && g.cfg.MaxSessions >= warmMinCap
}

// warmPoolSize is how many warm pods the pool keeps: the configured count,
// less what would take the cap's last slot with live sessions running.
// Never negative.
func warmPoolSize(warm, maxSessions, live int) int {
	return max(0, min(warm, maxSessions-1-live))
}

// warmClaimable reports whether a warm pod can be handed a conversation now:
// Ready, made under this gateway's config, unclaimed, and young enough.
func warmClaimable(p warmPod, claimed bool, now time.Time) bool {
	return p.Ready && !p.Gone && p.Current && !claimed && now.Sub(p.Created) < warmClaimableAge
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
// cold spawn, never a refused turn. The pods and the claim index are read
// with no lock held; a candidate is then marked as being claimed
// (beginWarmClaim), checked for any task ever published to it, and claimed
// with a create in the index, which fails for a pod already claimed.
func (g *Gateway) claimWarm(ctx context.Context, rec *SessionRecord) string {
	if !g.warmPoolOn() || !warmClaimEligible(g.cfg, rec) {
		return ""
	}
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
		if _, claimed := claims[p.Name]; claimed || !g.beginWarmClaim(p.Name) {
			continue
		}
		pod, done, used := g.tryClaimWarm(ctx, rec, p.Name)
		g.endWarmClaim(p.Name)
		if used {
			// Something was published to it, and the index names no
			// conversation: most likely a claim entry lost from the bucket,
			// with the pod serving the conversation that claimed it. It is
			// never handed out again, and it is not deleted either, which
			// would take a live conversation's pod: its own lifetime, and
			// the reap or sweep of that conversation, end it.
			g.log.Warn("warm pool: an unclaimed warm pod has had a task; not claiming it", "pod", p.Name)
		}
		if done {
			return pod
		}
	}
	return ""
}

// tryClaimWarm claims pod for rec, while the caller holds its claiming mark.
// done is false to try the next pod; used is a pod something was published
// to, which is never claimed.
func (g *Gateway) tryClaimWarm(ctx context.Context, rec *SessionRecord, pod string) (claimed string, done, used bool) {
	used, err := g.warmPodUsed(ctx, pod)
	if err != nil {
		g.log.Warn("warm pool: task subject read failed; spawning cold", "conversation", rec.Key, "pod", pod, "err", err)
		return "", true, false
	}
	if used {
		return "", false, true
	}
	err = g.reg.ClaimWarmPod(ctx, pod, rec.Key)
	if errors.Is(err, jetstream.ErrKeyExists) {
		return "", false, false
	}
	if err != nil {
		g.log.Warn("warm pool: claim failed; spawning cold", "conversation", rec.Key, "pod", pod, "err", err)
		return "", true, false
	}
	return pod, true, false
}

// warmPodUsed reports whether anything was ever published to pod's task
// subjects, read with one direct get of the newest message on them.
func (g *Gateway) warmPodUsed(ctx context.Context, pod string) (bool, error) {
	stream, err := g.client.JetStream().Stream(ctx, lib.TasksStream)
	if err != nil {
		return false, fmt.Errorf("stream %s: %w", lib.TasksStream, err)
	}
	_, err = stream.GetLastMsgForSubject(ctx, lib.TaskSubjectsOf(pod))
	if errors.Is(err, jetstream.ErrMsgNotFound) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}

// beginWarmClaim marks pod as being claimed, unless the pool is deleting it.
func (g *Gateway) beginWarmClaim(pod string) bool {
	g.warmMu.Lock()
	defer g.warmMu.Unlock()
	if g.warmDeleting[pod] || g.warmClaiming[pod] {
		return false
	}
	if g.warmClaiming == nil {
		g.warmClaiming = map[string]bool{}
	}
	g.warmClaiming[pod] = true
	return true
}

func (g *Gateway) endWarmClaim(pod string) {
	g.warmMu.Lock()
	delete(g.warmClaiming, pod)
	g.warmMu.Unlock()
}

// beginWarmDelete marks pod as being deleted, unless a turn is claiming it.
func (g *Gateway) beginWarmDelete(pod string) bool {
	g.warmMu.Lock()
	defer g.warmMu.Unlock()
	if g.warmClaiming[pod] || g.warmDeleting[pod] {
		return false
	}
	if g.warmDeleting == nil {
		g.warmDeleting = map[string]bool{}
	}
	g.warmDeleting[pod] = true
	return true
}

func (g *Gateway) endWarmDelete(pod string) {
	g.warmMu.Lock()
	delete(g.warmDeleting, pod)
	g.warmMu.Unlock()
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
// replace unclaimed warm pods past warmClaimableAge or made under an earlier
// config, delete any beyond the configured count, and create pods up to
// warmPoolSize. A pod is deleted at the age past which nothing claims it,
// before it is replaced, so the pool never holds more warm pods than the
// operator budgeted for: for the seconds a replacement takes to start, a new
// conversation spawns cold. No lock is held across its API calls.
func (g *Gateway) warmPoolOnce(ctx context.Context) {
	ctx, cancel := context.WithTimeout(ctx, warmPassTimeout)
	defer cancel()
	// The claims before the pods: a claim is made only on a listed pod, so a
	// claim this read sees whose pod the later list lacks is a pod gone.
	claims, err := g.reg.WarmClaims(ctx)
	if err != nil {
		g.log.Error("warm pool: claim index read failed", "err", err)
		return
	}
	warm, err := g.spawner.WarmPods(ctx)
	if err != nil {
		g.log.Error("warm pool: pod list failed", "err", err)
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
		switch {
		case !p.Current:
			g.deleteWarmIfUnclaimed(ctx, p.Name, "made under an earlier config")
		case now.Sub(p.Created) >= warmClaimableAge:
			g.deleteWarmIfUnclaimed(ctx, p.Name, "aged past its claim window")
		default:
			fresh = append(fresh, p)
		}
	}
	slices.SortFunc(fresh, func(a, b warmPod) int { return a.Created.Compare(b.Created) })
	for len(fresh) > g.cfg.WarmSessions {
		g.deleteWarmIfUnclaimed(ctx, fresh[0].Name, "beyond the configured warm count")
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

// deleteWarmIfUnclaimed deletes an unclaimed warm pod: not while a turn is
// claiming it, not once the index names a conversation for it, not if
// anything was ever published to it, and not unless this gateway still
// leads, each checked immediately before the delete.
func (g *Gateway) deleteWarmIfUnclaimed(ctx context.Context, pod, why string) {
	if !g.beginWarmDelete(pod) {
		return
	}
	defer g.endWarmDelete(pod)
	claims, err := g.reg.WarmClaims(ctx)
	if err != nil {
		g.log.Warn("warm pool: claim index read failed; not deleting", "pod", pod, "err", err)
		return
	}
	if _, claimed := claims[pod]; claimed {
		return
	}
	// A pod with a task on its subjects is a conversation's, its claim
	// entry lost or not: never deleted here (claimWarm says why).
	if used, err := g.warmPodUsed(ctx, pod); err != nil || used {
		return
	}
	if !g.reg.StillLeading(ctx) {
		g.log.Info("warm pool: no longer leading; not deleting", "pod", pod)
		return
	}
	if err := g.spawner.Delete(ctx, pod); err != nil {
		g.log.Warn("warm pool: warm pod delete failed", "pod", pod, "why", why, "err", err)
		return
	}
	g.log.Info("warm pool: deleted an unclaimed warm pod", "pod", pod, "why", why)
}
