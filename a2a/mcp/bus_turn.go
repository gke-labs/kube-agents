package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// SendTurn publishes a message or control command to chat.console.<token>.in
// and streams back all thinking, tool activities, progress, delegation children,
// wake turns, and results.
func (s *BusSession) SendTurn(ctx context.Context, store *ConfigStore, conv ConversationEntry, rawText string, mode string, waitTimeout time.Duration) (*TurnOutcome, error) {
	return s.SendTurnWithOptions(ctx, store, conv, rawText, mode, waitTimeout, false)
}

// SendTurnWithOptions publishes a message or control command to chat.console.<token>.in.
// When streamSteps is true, it waits for the first step batch (or completion if fast) and
// returns immediately so Antigravity can render incremental collapsed step updates as the task runs.
func (s *BusSession) SendTurnWithOptions(ctx context.Context, store *ConfigStore, conv ConversationEntry, rawText string, mode string, waitTimeout time.Duration, streamSteps bool) (*TurnOutcome, error) {
	if waitTimeout <= 0 {
		waitTimeout = defaultTurnWaitTimeout
	}
	text := strings.TrimSpace(rawText)
	if text == "" {
		return nil, errors.New("message text cannot be empty")
	}

	mode = strings.ToLower(strings.TrimSpace(mode))
	if mode == "" {
		mode = modeAuto
	}

	convName := consoleConvPrefix + conv.Token
	s.watchConv(convName, 1)
	defer s.watchConv(convName, -1)

	outcome := &TurnOutcome{
		ConversationAlias: conv.Alias,
		ConversationToken: conv.Token,
		SentText:          text,
	}

	if mode == modeSession && !conv.SessionMode && text != cmdSessionOn && text != cmdSessionOff {
		notices, err := s.sendControlCommand(ctx, conv.Token, cmdSessionOn)
		outcome.ConsoleNotices = append(outcome.ConsoleNotices, notices...)
		if err == nil && store != nil {
			_, _ = store.SetConversationSessionMode(conv.Alias, true)
		}
	} else if mode == modePlatform && conv.SessionMode && text != cmdSessionOff && text != cmdSessionOn {
		notices, err := s.sendControlCommand(ctx, conv.Token, cmdSessionOff)
		outcome.ConsoleNotices = append(outcome.ConsoleNotices, notices...)
		if err == nil {
			s.clearActiveSessionPodForToken(conv.Token)
			if store != nil {
				_, _ = store.SetConversationSessionMode(conv.Alias, false)
			}
		}
	}

	isOneShotDelegate := false
	if mode == modeDelegate && !conv.SessionMode && !strings.HasPrefix(strings.ToLower(text), cmdDelegatePrefix) {
		text = cmdDelegatePrefix + " " + text
		outcome.SentText = text
		isOneShotDelegate = true
	} else if !conv.SessionMode && strings.HasPrefix(strings.ToLower(text), cmdDelegatePrefix) {
		isOneShotDelegate = true
	}

	if isPureControlCommand(text) {
		if strings.EqualFold(text, cmdStatus) && s.findActiveTaskForConv(convName) == nil {
			outcome.ConsoleNotices = append(outcome.ConsoleNotices, ConsoleOutFrame{
				Token:     conv.Token,
				Text:      "🤷 nothing is running",
				Timestamp: time.Now().UTC(),
			})
			return outcome, nil
		}
		if strings.EqualFold(text, cmdHelp) {
			outcome.ConsoleNotices = append(outcome.ConsoleNotices, ConsoleOutFrame{
				Token:     conv.Token,
				Text:      "ℹ️ Commands: `/session` (start persistent Claude Code worker), `/session off` (return to platform agent), `delegate: <ask>` (delegate to platform), `status` (replay active task status), `stop` (cancel active task)",
				Timestamp: time.Now().UTC(),
			})
			return outcome, nil
		}
		notices, err := s.sendControlCommand(ctx, conv.Token, text)
		outcome.ConsoleNotices = append(outcome.ConsoleNotices, notices...)
		if err != nil {
			return outcome, err
		}
		if strings.EqualFold(text, cmdSessionOff) {
			s.clearActiveSessionPodForToken(conv.Token)
		}
		if store != nil {
			// Only the explicit route commands change the stored mode; the gateway's notice
			// wording is never parsed. Any other transition (e.g. a /session typed from Slack)
			// is picked up structurally from the addressee of the next turn's task.
			switch {
			case strings.EqualFold(text, cmdSessionOn):
				_, _ = store.SetConversationSessionMode(conv.Alias, true)
			case strings.EqualFold(text, cmdSessionOff):
				_, _ = store.SetConversationSessionMode(conv.Alias, false)
			}
		}
		return outcome, nil
	}

	baselineTasks := s.snapshotTaskIDsForConv(convName)
	activeBefore := s.findActiveTaskForConv(convName)
	noticeCursor := s.consoleNoticeCursor()
	preNotices := append([]ConsoleOutFrame(nil), outcome.ConsoleNotices...)

	if err := s.publishConsoleFrame(conv.Token, text); err != nil {
		return nil, err
	}

	if cb := progressCallbackFromContext(ctx); cb != nil {
		cb(1, fmt.Sprintf("📨 Sent message to kube-agents (`%s` / `console:%s`) — waiting for task acceptance on `TASKS`...", conv.Alias, conv.Token))
	}

	turnCtx, cancel := context.WithTimeout(ctx, waitTimeout)
	defer cancel()

	primaryID, err := s.awaitPrimaryTask(turnCtx, convName, baselineTasks, activeBefore, conv.Token, noticeCursor)
	outcome.ConsoleNotices = append(preNotices, s.consoleNoticesSince(conv.Token, noticeCursor)...)
	if err != nil || primaryID == "" {
		if len(outcome.ConsoleNotices) > 0 {
			return outcome, nil
		}
		return outcome, err
	}

	// A brand-new task starts its step cursor at 0. A steer/follow-up absorbed by an already
	// running task (primaryID existed in the baseline) must keep its cursor, otherwise every step
	// already shown in Antigravity would be re-streamed as a duplicate row.
	if _, existed := baselineTasks[primaryID]; !existed {
		s.resetStepProgress(primaryID)
	}
	timedOut := s.WaitForTaskStepOrSettlement(turnCtx, primaryID, streamSteps)
	if errors.Is(ctx.Err(), context.Canceled) && !s.IsTaskTreeSettled(primaryID) {
		stopCtx, stopCancel := context.WithTimeout(context.Background(), controlNoticeWait)
		_, _ = s.sendControlCommand(stopCtx, conv.Token, cmdStop)
		stopCancel()
	}
	built := s.BuildTaskOutcome(primaryID, conv.Alias, conv.Token, text, timedOut)
	built.ConsoleNotices = append(preNotices, s.consoleNoticesSince(conv.Token, noticeCursor)...)
	if store != nil && built.PrimaryTask != nil && isSessionWorkerTask(built.PrimaryTask) {
		store.SetConversationSessionWorkerID(conv.Alias, built.PrimaryTask.Addressee)
		if !isOneShotDelegate {
			_, _ = store.SetConversationSessionMode(conv.Alias, true)
		}
	}

	if cb := progressCallbackFromContext(ctx); cb != nil && built.PrimaryTask != nil {
		cb(999, FormatLiveTaskProgress(built.PrimaryTask, built.ChildTasks, built.WakeTask, time.Since(built.PrimaryTask.CreatedAt)))
	}

	return built, nil
}

// BuildTaskOutcome constructs a TurnOutcome for a primary task ID, including any delegated child
// tasks and session wake synthesis task.
func (s *BusSession) BuildTaskOutcome(primaryID string, convAlias string, convToken string, sentText string, timedOut bool) *TurnOutcome {
	primary := s.GetTask(primaryID)
	prevSteps, reportedSteps := s.getStepProgress(primaryID)
	prevActs, reportedActs := s.getActProgress(primaryID)
	outcome := &TurnOutcome{
		ConversationAlias:     convAlias,
		ConversationToken:     convToken,
		SentText:              sentText,
		PrimaryTask:           primary,
		TimedOut:              timedOut,
		PrevStepCount:         prevSteps,
		ReportedStepCount:     reportedSteps,
		PrevActivityCount:     prevActs,
		ReportedActivityCount: reportedActs,
	}
	if primary == nil {
		return outcome
	}
	if outcome.ConversationToken == "" && strings.HasPrefix(primary.Conversation, consoleConvPrefix) {
		outcome.ConversationToken = strings.TrimPrefix(primary.Conversation, consoleConvPrefix)
	}
	children := s.findChildTasks(primaryID)
	outcome.ChildTasks = children
	childIDs := make([]string, 0, len(children))
	for _, child := range children {
		childIDs = append(childIDs, child.TaskID)
		if wake := s.findWakeTask(child.TaskID); wake != nil {
			outcome.WakeTask = wake
		}
	}
	outcome.CardTasks = s.findCardTasks(primaryID, childIDs...)
	return outcome
}

func totalOutcomeActivityCount(outcome *TurnOutcome) int {
	if outcome == nil {
		return 0
	}
	n := 0
	if outcome.PrimaryTask != nil {
		n += len(outcome.PrimaryTask.Activities)
	}
	for _, c := range outcome.ChildTasks {
		n += len(c.Activities)
	}
	for _, ct := range outcome.CardTasks {
		n += len(ct.Activities)
	}
	if outcome.WakeTask != nil {
		n += len(outcome.WakeTask.Activities)
	}
	return n
}

// IsTaskTreeSettled returns true if the primary task and any expected delegated child / wake tasks
// have reached a terminal or input-required state.
func (s *BusSession) IsTaskTreeSettled(primaryID string) bool {
	primary := s.GetTask(primaryID)
	if primary == nil {
		return false
	}
	if !primary.Terminal && primary.State != lib.StateInputRequired {
		return false
	}
	children := s.findChildTasks(primaryID)
	if len(primary.Delegates) > 0 && len(children) == 0 {
		if time.Since(primary.UpdatedAt) < delegationChildWait {
			return false
		}
		return true
	}
	if len(children) == 0 {
		return true
	}
	var wake *TaskRecord
	for _, child := range children {
		if !child.Terminal && child.State != lib.StateInputRequired {
			return false
		}
		if w := s.findWakeTask(child.TaskID); w != nil {
			wake = w
		}
	}
	if wake == nil && (len(primary.Delegates) > 0 || isSessionWorkerTask(primary)) {
		lastChild := children[len(children)-1]
		if time.Since(lastChild.UpdatedAt) < delegationWakeWait {
			return false
		}
		return true
	}
	if wake != nil && !wake.Terminal && wake.State != lib.StateInputRequired {
		return false
	}
	return true
}

// WaitForTaskStepOrSettlement waits for a task tree. When streamSteps is false, it waits until
// full settlement or ctx expiration. When streamSteps is true, it waits until either the task tree
// settles OR the next human-readable step arrives (advancing by 1 step at a time so each step
// appends a single item to Antigravity's collapsible step list rather than creating duplicate sub-lists),
// returning true if the task is still in flight (timedOut).
func (s *BusSession) WaitForTaskStepOrSettlement(ctx context.Context, primaryID string, streamSteps bool) bool {
	s.watchTask(primaryID, 1)
	defer s.watchTask(primaryID, -1)

	baseline := s.getReportedStepCount(primaryID)
	_, baseActs := s.getActProgress(primaryID)
	if !streamSteps {
		timedOut := s.waitForTaskAndDelegations(ctx, primaryID)
		if !timedOut {
			s.markForegroundStepTask(primaryID, true)
		}
		built := s.BuildTaskOutcome(primaryID, "", "", "", timedOut)
		steps := CollectOutcomeSteps(built)
		s.recordStepProgress(primaryID, baseline, len(steps))
		s.recordActProgress(primaryID, baseActs, totalOutcomeActivityCount(built))
		return timedOut
	}

	s.markForegroundStepTask(primaryID, false)
	window := defaultStepStreamWindow
	if baseline == 0 {
		window = initialStepStreamWindow
	}
	stepCtx, cancel := context.WithTimeout(ctx, window)
	defer cancel()

	ticker := time.NewTicker(pollTickInterval)
	defer ticker.Stop()

	startedAt := time.Now()
	firstSeenAboveBaseline := time.Time{}
	lastActCount := baseActs
	lastActChangeAt := time.Time{}

	for {
		settled := s.IsTaskTreeSettled(primaryID)
		built := s.BuildTaskOutcome(primaryID, "", "", "", !settled)
		steps := CollectOutcomeSteps(built)
		curCount := len(steps)
		curActs := totalOutcomeActivityCount(built)

		if settled {
			s.markForegroundStepTask(primaryID, true)
			s.recordStepProgress(primaryID, baseline, curCount)
			s.recordActProgress(primaryID, baseActs, curActs)
			return false
		}

		now := time.Now()
		if curCount > baseline {
			if firstSeenAboveBaseline.IsZero() {
				firstSeenAboveBaseline = now
			}
			if now.Sub(startedAt) >= minStepBatchWait && now.Sub(firstSeenAboveBaseline) >= stepDebounceWait {
				s.markForegroundStepTask(primaryID, false)
				s.recordStepProgress(primaryID, baseline, baseline+1)
				s.recordActProgress(primaryID, baseActs, curActs)
				if baseline < len(steps) {
					s.setLastWaitStepTitle(primaryID, strings.TrimSuffix(steps[baseline], "."))
				}
				return true
			}
		} else if curActs > baseActs {
			candTitle := meaningfulDeltaActivityStepTitle(built, baseActs, curActs)
			if candTitle != "" && !strings.EqualFold(candTitle, s.getLastWaitStepTitle(primaryID)) {
				if curActs != lastActCount {
					lastActCount = curActs
					lastActChangeAt = now
				}
				if now.Sub(startedAt) >= activityStepBatchWait && !lastActChangeAt.IsZero() && now.Sub(lastActChangeAt) >= activityStepDebounceWait {
					s.markForegroundStepTask(primaryID, false)
					s.recordStepProgress(primaryID, baseline, baseline)
					s.recordActProgress(primaryID, baseActs, curActs)
					s.setLastWaitStepTitle(primaryID, candTitle)
					return true
				}
			}
		}

		select {
		case <-stepCtx.Done():
			if errors.Is(ctx.Err(), context.Canceled) && !s.IsTaskTreeSettled(primaryID) {
				if rec := s.GetTask(primaryID); rec != nil && strings.HasPrefix(rec.Conversation, consoleConvPrefix) {
					tok := strings.TrimPrefix(rec.Conversation, consoleConvPrefix)
					if tok != "" {
						stopCtx, stopCancel := context.WithTimeout(context.Background(), controlNoticeWait)
						_, _ = s.sendControlCommand(stopCtx, tok, cmdStop)
						stopCancel()
					}
				}
			}
			finalSettled := s.IsTaskTreeSettled(primaryID)
			finalBuilt := s.BuildTaskOutcome(primaryID, "", "", "", !finalSettled)
			finalSteps := CollectOutcomeSteps(finalBuilt)
			if finalSettled {
				s.markForegroundStepTask(primaryID, true)
				s.recordStepProgress(primaryID, baseline, len(finalSteps))
				s.recordActProgress(primaryID, baseActs, totalOutcomeActivityCount(finalBuilt))
				return false
			}
			s.markForegroundStepTask(primaryID, false)
			nextCount := baseline
			if len(finalSteps) > baseline {
				nextCount = baseline + 1
			}
			s.recordStepProgress(primaryID, baseline, nextCount)
			s.recordActProgress(primaryID, baseActs, totalOutcomeActivityCount(finalBuilt))
			return true
		case <-ticker.C:
		}
	}
}

func (s *BusSession) getReportedStepCount(taskID string) int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.lastReportedSteps == nil {
		return 0
	}
	return s.lastReportedSteps[taskID]
}

func (s *BusSession) getStepProgress(taskID string) (prev int, reported int) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.prevReportedSteps != nil {
		prev = s.prevReportedSteps[taskID]
	}
	if s.lastReportedSteps != nil {
		reported = s.lastReportedSteps[taskID]
	}
	return prev, reported
}

func (s *BusSession) getActProgress(taskID string) (prev int, reported int) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.prevReportedActs != nil {
		prev = s.prevReportedActs[taskID]
	}
	if s.lastReportedActs != nil {
		reported = s.lastReportedActs[taskID]
	}
	return prev, reported
}

func (s *BusSession) recordActProgress(taskID string, prev int, reported int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.prevReportedActs == nil {
		s.prevReportedActs = make(map[string]int)
	}
	if s.lastReportedActs == nil {
		s.lastReportedActs = make(map[string]int)
	}
	s.prevReportedActs[taskID] = prev
	s.lastReportedActs[taskID] = reported
}

func (s *BusSession) resetStepProgress(taskID string) {
	s.recordStepProgress(taskID, 0, 0)
	s.recordActProgress(taskID, 0, 0)
	s.setLastWaitStepTitle(taskID, "")
}

func (s *BusSession) getLastWaitStepTitle(taskID string) string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.lastWaitStepTitle == nil {
		return ""
	}
	return s.lastWaitStepTitle[taskID]
}

func (s *BusSession) setLastWaitStepTitle(taskID, title string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.lastWaitStepTitle == nil {
		s.lastWaitStepTitle = make(map[string]string)
	}
	s.lastWaitStepTitle[taskID] = title
}

func meaningfulDeltaActivityStepTitle(outcome *TurnOutcome, baseActs, curActs int) string {
	all := collectOutcomeActivities(outcome)
	if len(all) == 0 || baseActs >= len(all) {
		return ""
	}
	if baseActs < 0 {
		baseActs = 0
	}
	if curActs > len(all) || curActs <= baseActs {
		curActs = len(all)
	}
	delta := all[baseActs:curActs]
	var lastDiagAct *ActivityItem
	for i := len(delta) - 1; i >= 0; i-- {
		t := delta[i].Tool
		if t == "kanban_heartbeat" {
			args := parseActivityArgMap(delta[i].Input)
			if note := firstNonEmptyArgString(args, "note", "status", "message"); note != "" {
				lastDiagAct = &delta[i]
				break
			}
		}
		if !strings.HasPrefix(t, "kanban_") && t != "tool_search" && t != "tool_describe" {
			lastDiagAct = &delta[i]
			break
		}
	}
	if lastDiagAct == nil {
		return ""
	}
	cards, _, _, _, _ := summarizeOutcomeKanbanState(outcome)
	step, ok := specificActivityWaitStep(*lastDiagAct, cardLabels(cards))
	if !ok {
		return ""
	}
	return strings.TrimSuffix(step, ".")
}

func (s *BusSession) recordStepProgress(taskID string, prev int, reported int) {
	s.mu.Lock()
	if s.prevReportedSteps == nil {
		s.prevReportedSteps = make(map[string]int)
	}
	if s.lastReportedSteps == nil {
		s.lastReportedSteps = make(map[string]int)
	}
	s.prevReportedSteps[taskID] = prev
	s.lastReportedSteps[taskID] = reported
	if len(s.lastReportedSteps) > maxPersistedStepTasks {
		type candidate struct {
			id       string
			terminal bool
			ts       time.Time
		}
		var cands []candidate
		for id := range s.lastReportedSteps {
			if id == taskID {
				continue
			}
			var ts time.Time
			term := true
			if rec := s.tasks[id]; rec != nil {
				ts = rec.UpdatedAt
				term = rec.Terminal
			}
			cands = append(cands, candidate{id: id, terminal: term, ts: ts})
		}
		sort.Slice(cands, func(i, j int) bool {
			if cands[i].terminal != cands[j].terminal {
				return cands[i].terminal
			}
			if !cands[i].ts.Equal(cands[j].ts) {
				return cands[i].ts.Before(cands[j].ts)
			}
			return cands[i].id < cands[j].id
		})
		for len(s.lastReportedSteps) > maxPersistedStepTasks && len(cands) > 0 {
			evictID := cands[0].id
			cands = cands[1:]
			delete(s.lastReportedSteps, evictID)
			delete(s.prevReportedSteps, evictID)
			delete(s.prevReportedActs, evictID)
			delete(s.lastReportedActs, evictID)
			delete(s.lastWaitStepTitle, evictID)
		}
	}
	path := s.stepProgressPath
	var snapshot map[string][2]int
	if path != "" {
		snapshot = make(map[string][2]int, len(s.lastReportedSteps))
		for id, rep := range s.lastReportedSteps {
			snapshot[id] = [2]int{s.prevReportedSteps[id], rep}
		}
	}
	s.mu.Unlock()
	if path != "" {
		s.stepFileMu.Lock()
		saveStepProgress(path, snapshot)
		s.stepFileMu.Unlock()
	}
}

// stepProgressFile is the on-disk form of the per-task step cursor. It survives an MCP server
// restart (Antigravity relaunches the process on the next tool call after a crash or an upgrade) so a
// task that was mid-stream resumes at its last reported step instead of re-streaming every step
// as duplicate rows.
type stepProgressFile struct {
	UpdatedAt time.Time         `json:"updatedAt"`
	Tasks     map[string][2]int `json:"tasks"` // taskID -> [prevReported, reported]
}

func saveStepProgress(path string, snapshot map[string][2]int) {
	data, err := json.Marshal(stepProgressFile{UpdatedAt: time.Now().UTC(), Tasks: snapshot})
	if err != nil {
		return
	}
	if err := writeFileAtomic(path, data); err != nil {
		slog.Warn("kube-agents-mcp: step progress save failed", "path", path, "err", err)
	}
}

func loadStepProgress(path string) (prev map[string]int, last map[string]int) {
	prev = make(map[string]int)
	last = make(map[string]int)
	if path == "" {
		return prev, last
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return prev, last
	}
	var f stepProgressFile
	if json.Unmarshal(raw, &f) != nil || time.Since(f.UpdatedAt) > stepProgressMaxAge {
		return prev, last
	}
	for id, v := range f.Tasks {
		prev[id] = v[0]
		last[id] = v[1]
	}
	return prev, last
}

func (s *BusSession) sendControlCommand(ctx context.Context, token string, cmdText string) ([]ConsoleOutFrame, error) {
	cursor := s.consoleNoticeCursor()
	if err := s.publishConsoleFrame(token, cmdText); err != nil {
		return nil, err
	}
	waitCtx, cancel := context.WithTimeout(ctx, controlNoticeWait)
	defer cancel()
	ticker := time.NewTicker(pollTickInterval)
	defer ticker.Stop()
	for {
		notices := s.consoleNoticesSince(token, cursor)
		if len(notices) > 0 {
			return notices, nil
		}
		select {
		case <-waitCtx.Done():
			return s.consoleNoticesSince(token, cursor), nil
		case <-ticker.C:
		}
	}
}

func (s *BusSession) publishConsoleFrame(token string, text string) error {
	frame := ConsoleInFrame{
		MessageID: fmt.Sprintf("%s%d", msgIDPrefix, time.Now().UnixNano()),
		Text:      text,
		Kind:      consoleFrameKindText,
	}
	raw, err := json.Marshal(frame)
	if err != nil {
		return err
	}
	subj := subjectConsoleInPrefix + token + subjectConsoleInSuffix
	if err := s.nc.Publish(subj, raw); err != nil {
		return fmt.Errorf("publishing to %s: %w", subj, err)
	}
	return s.nc.Flush()
}

func (s *BusSession) awaitPrimaryTask(
	ctx context.Context,
	convName string,
	baseline map[string]int,
	activeBefore *TaskRecord,
	token string,
	noticeCursor int,
) (string, error) {
	ticker := time.NewTicker(pollTickInterval)
	defer ticker.Stop()

	for {
		s.mu.RLock()
		for id, rec := range s.tasks {
			if rec.Conversation == convName && rec.DelegatedFromTaskID == "" && rec.WakeFromChildTaskID == "" {
				if _, existed := baseline[id]; !existed {
					s.mu.RUnlock()
					return id, nil
				}
			}
		}
		if activeBefore != nil {
			if cur, ok := s.tasks[activeBefore.TaskID]; ok && cur.EnvelopeCount > baseline[activeBefore.TaskID] {
				s.mu.RUnlock()
				return activeBefore.TaskID, nil
			}
		}
		s.mu.RUnlock()

		select {
		case <-ctx.Done():
			if activeBefore != nil {
				return activeBefore.TaskID, nil
			}
			if len(s.consoleNoticesSince(token, noticeCursor)) > 0 {
				return "", nil
			}
			return "", fmt.Errorf("timed out waiting for task creation on %s", convName)
		case <-ticker.C:
		}
	}
}

func (s *BusSession) waitForTaskAndDelegations(ctx context.Context, primaryID string) bool {
	if !s.waitUntilTaskSettled(ctx, primaryID) {
		return true
	}
	primary := s.GetTask(primaryID)
	if primary == nil {
		return false
	}

	children := s.findChildTasks(primaryID)
	if len(children) == 0 && len(primary.Delegates) > 0 {
		childWaitCtx, cancel := context.WithTimeout(ctx, delegationChildWait)
		children = s.awaitChildTasks(childWaitCtx, primaryID)
		cancel()
	}
	if len(children) == 0 {
		return false
	}

	for _, child := range children {
		s.watchTask(child.TaskID, 1)
		settled := s.waitUntilTaskSettled(ctx, child.TaskID)
		s.watchTask(child.TaskID, -1)
		if !settled {
			return true
		}
		wakeWaitCtx, cancel := context.WithTimeout(ctx, delegationWakeWait)
		wake := s.awaitWakeTask(wakeWaitCtx, child.TaskID)
		cancel()
		if wake != nil {
			s.watchTask(wake.TaskID, 1)
			wakeSettled := s.waitUntilTaskSettled(ctx, wake.TaskID)
			s.watchTask(wake.TaskID, -1)
			if !wakeSettled {
				return true
			}
		}
	}
	return false
}

func (s *BusSession) waitUntilTaskSettled(ctx context.Context, taskID string) bool {
	ticker := time.NewTicker(pollTickInterval)
	defer ticker.Stop()
	cb := progressCallbackFromContext(ctx)
	startedWait := time.Now()
	lastEnvCount := -1
	lastReportAt := time.Time{}
	step := 1
	for {
		rec := s.GetTask(taskID)
		if rec != nil {
			now := time.Now()
			if cb != nil && (rec.EnvelopeCount != lastEnvCount || now.Sub(lastReportAt) >= progressHeartbeatInterval) {
				step++
				lastEnvCount = rec.EnvelopeCount
				lastReportAt = now
				elapsed := now.Sub(startedWait)
				if !rec.CreatedAt.IsZero() && now.After(rec.CreatedAt) {
					elapsed = now.Sub(rec.CreatedAt)
				}
				children := s.findChildTasks(taskID)
				var wake *TaskRecord
				for _, ch := range children {
					if w := s.findWakeTask(ch.TaskID); w != nil {
						wake = w
					}
				}
				cb(step, FormatLiveTaskProgress(rec, children, wake, elapsed))
			}
			if rec.Terminal || rec.State == lib.StateInputRequired {
				return true
			}
		}
		select {
		case <-ctx.Done():
			return false
		case <-ticker.C:
		}
	}
}

func (s *BusSession) awaitChildTasks(ctx context.Context, parentID string) []*TaskRecord {
	ticker := time.NewTicker(pollTickInterval)
	defer ticker.Stop()
	for {
		if children := s.findChildTasks(parentID); len(children) > 0 {
			return children
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}

func (s *BusSession) awaitWakeTask(ctx context.Context, childID string) *TaskRecord {
	ticker := time.NewTicker(pollTickInterval)
	defer ticker.Stop()
	for {
		if wake := s.findWakeTask(childID); wake != nil {
			return wake
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}
