package mcp

import (
	"context"
	"errors"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	sessionPodStartupGrace     = 2 * time.Minute
	sessionPodMaxIdleAge       = 45 * time.Minute
	sessionConsumerProbeBudget = 1500 * time.Millisecond
	maxSessionConsumerProbes   = 8
)

type sessionWorkerCandidate struct {
	addr            string
	sessionKey      string
	taskID          string
	createdAt       time.Time
	latestTaskAt    time.Time
	hasActiveTask   bool
	previouslyKnown bool
}

// DeriveActiveSessionPods derives currently running /session worker pods purely from
// NATS JetStream bus state (TASKS envelopes, chat.console.*.out route notices, and
// live <addr>-origin / <addr>-in TASKS consumers via $JS.API.CONSUMER.INFO.TASKS.<name>)
// without invoking kubectl or the Kubernetes API.
func (s *BusSession) DeriveActiveSessionPods(ctx context.Context) []SessionPodInfo {
	if s == nil {
		return nil
	}
	now := time.Now().UTC()

	s.mu.RLock()
	candidates := make(map[string]*sessionWorkerCandidate)
	for _, pod := range s.profile.ActiveSessionPods {
		addr := firstNonEmptyStr(strings.TrimSpace(pod.Addressee), strings.TrimSpace(pod.Name))
		if addr == "" {
			continue
		}
		candidates[addr] = &sessionWorkerCandidate{
			addr:            addr,
			sessionKey:      strings.TrimSpace(pod.SessionKey),
			taskID:          strings.TrimSpace(pod.TaskID),
			createdAt:       pod.CreatedAt,
			previouslyKnown: true,
		}
	}
	for _, rec := range s.tasks {
		if !isSessionWorkerTask(rec) {
			continue
		}
		addr := strings.TrimSpace(rec.Addressee)
		if addr == "" {
			continue
		}
		cand, exists := candidates[addr]
		if !exists {
			cand = &sessionWorkerCandidate{addr: addr}
			candidates[addr] = cand
		}
		created := taskEffectiveCreatedAt(rec)
		if !created.IsZero() && (cand.createdAt.IsZero() || created.Before(cand.createdAt)) {
			cand.createdAt = created
		}
		updated := rec.UpdatedAt
		if updated.IsZero() {
			updated = created
		}
		if !updated.IsZero() && updated.After(cand.latestTaskAt) {
			cand.latestTaskAt = updated
			cand.taskID = rec.TaskID
			if strings.TrimSpace(rec.Conversation) != "" {
				cand.sessionKey = strings.TrimSpace(rec.Conversation)
			}
		} else {
			if cand.sessionKey == "" && strings.TrimSpace(rec.Conversation) != "" {
				cand.sessionKey = strings.TrimSpace(rec.Conversation)
			}
			if cand.taskID == "" {
				cand.taskID = rec.TaskID
			}
		}
		if !rec.Terminal {
			cand.hasActiveTask = true
		}
	}

	convOffAt := make(map[string]time.Time)
	for _, n := range s.consoleNotices {
		token := strings.TrimSpace(n.Token)
		if token == "" {
			continue
		}
		conv := consoleConvPrefix + token
		lower := strings.ToLower(n.Text)
		if strings.Contains(lower, "session route off") || strings.Contains(lower, "returning to platform") {
			convOffAt[conv] = n.Timestamp
		} else if strings.Contains(lower, "session route on") || strings.Contains(lower, "session started") {
			delete(convOffAt, conv)
		}
	}
	s.mu.RUnlock()

	var toProbe []*sessionWorkerCandidate
	var aliveCandidates []*sessionWorkerCandidate
	for _, cand := range candidates {
		if offAt, turnedOff := convOffAt[cand.sessionKey]; turnedOff && !cand.hasActiveTask {
			if cand.latestTaskAt.IsZero() || !cand.latestTaskAt.After(offAt) {
				continue
			}
		}
		if cand.hasActiveTask {
			// A newly spawned session pod may take up to ~60-90s to pull its image and
			// register its <addr>-origin / <addr>-in consumer on TASKS. Within that
			// startup grace window (or in unit tests where s.js is nil), trust the
			// in-flight task state directly; older non-terminal tasks are probed via
			// CONSUMER.INFO so orphaned tasks from hours/days ago are not shown as Running.
			if s.js == nil || cand.latestTaskAt.IsZero() || now.Sub(cand.latestTaskAt) <= sessionPodStartupGrace {
				aliveCandidates = append(aliveCandidates, cand)
				continue
			}
		}
		// Skip historical sessions older than the gateway's idle reaper window unless previously active.
		if !cand.previouslyKnown && !cand.latestTaskAt.IsZero() && now.Sub(cand.latestTaskAt) > sessionPodMaxIdleAge {
			continue
		}
		toProbe = append(toProbe, cand)
	}

	if len(toProbe) > 0 && s.js != nil && s.nc != nil && s.nc.IsConnected() {
		probeCtx, cancel := context.WithTimeout(ctx, sessionConsumerProbeBudget)
		st, stErr := s.js.Stream(probeCtx, streamTasks)
		if stErr != nil || st == nil {
			// Transient stream lookup failure: preserve previously known active pods.
			for _, cand := range toProbe {
				if cand.previouslyKnown {
					aliveCandidates = append(aliveCandidates, cand)
				}
			}
		} else {
			aliveFlags := make([]bool, len(toProbe))
			sem := make(chan struct{}, maxSessionConsumerProbes)
			var wg sync.WaitGroup
			for i, cand := range toProbe {
				wg.Add(1)
				go func(idx int, c *sessionWorkerCandidate) {
					defer wg.Done()
					sem <- struct{}{}
					defer func() { <-sem }()
					aliveFlags[idx] = probeSessionPodConsumerLiveness(probeCtx, st, c)
				}(i, cand)
			}
			wg.Wait()
			for i, cand := range toProbe {
				if aliveFlags[i] {
					aliveCandidates = append(aliveCandidates, cand)
				}
			}
		}
		cancel()
	}

	activePods := make([]SessionPodInfo, 0, len(aliveCandidates))
	for _, cand := range aliveCandidates {
		activePods = append(activePods, SessionPodInfo{
			Name:       cand.addr,
			Addressee:  cand.addr,
			SessionKey: cand.sessionKey,
			TaskID:     cand.taskID,
			Phase:      "Running",
			CreatedAt:  cand.createdAt,
		})
	}
	sort.Slice(activePods, func(i, j int) bool {
		return activePods[i].Name < activePods[j].Name
	})

	s.mu.Lock()
	s.profile.ActiveSessionPods = activePods
	s.profile.SessionPodsProbedAt = now
	s.mu.Unlock()

	return append([]SessionPodInfo(nil), activePods...)
}

// probeSessionPodConsumerLiveness checks $JS.API.CONSUMER.INFO.TASKS.<addr>-origin and <addr>-in.
// Note: we intentionally use st.Consumer(ctx, name) ($JS.API.CONSUMER.INFO.TASKS.*) rather than
// st.ConsumerNames(ctx) ($JS.API.CONSUMER.NAMES.TASKS) because the console NATS user's ACLs in
// rendered-nats.conf explicitly grant CONSUMER.INFO.TASKS.* and deny CONSUMER.NAMES.
func probeSessionPodConsumerLiveness(ctx context.Context, st jetstream.Stream, cand *sessionWorkerCandidate) bool {
	if st == nil || cand == nil {
		return false
	}
	allNotFound := true
	for _, kind := range []string{lib.SessionConsumerOrigin, lib.SessionConsumerIn} {
		cName := lib.SessionConsumerName(cand.addr, kind)
		cons, err := st.Consumer(ctx, cName)
		if err == nil && cons != nil {
			return true
		}
		if err != nil && !errors.Is(err, jetstream.ErrConsumerNotFound) {
			allNotFound = false
		}
	}
	if !allNotFound && cand.previouslyKnown {
		return true
	}
	return false
}

// clearActiveSessionPodForToken immediately removes any cached ActiveSessionPods entry
// matching token (or console:<token>) and invalidates SessionPodsProbedAt after /session off.
func (s *BusSession) clearActiveSessionPodForToken(token string) {
	if s == nil {
		return
	}
	trimmed := strings.TrimSpace(token)
	if trimmed == "" {
		return
	}
	withPrefix := consoleConvPrefix + strings.TrimPrefix(trimmed, consoleConvPrefix)
	withoutPrefix := strings.TrimPrefix(trimmed, consoleConvPrefix)

	s.mu.Lock()
	defer s.mu.Unlock()
	filtered := s.profile.ActiveSessionPods[:0]
	for _, pod := range s.profile.ActiveSessionPods {
		sk := strings.TrimSpace(pod.SessionKey)
		if sk == withPrefix || sk == withoutPrefix {
			continue
		}
		filtered = append(filtered, pod)
	}
	s.profile.ActiveSessionPods = filtered
	s.profile.SessionPodsProbedAt = time.Time{}
}
