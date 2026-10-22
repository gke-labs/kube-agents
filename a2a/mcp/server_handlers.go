package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const (
	sessionPodsProbeTTL       = 3 * time.Second
	deepBootstrapWaitTimeout  = 5 * time.Second
	swarmBoardLink            = "[Open the Swarm UI](sidecar://swarm/swarm_board/)"
	maxConversationsShown     = 12
	maxTranscriptBytes        = 3550
	transcriptPrimaryCapRunes = 1800
	transcriptDebugCapRunes   = 900
	transcriptOlderCapLarge   = 800
	transcriptOlderCapMedium  = 450
	transcriptOlderCapSmall   = 200
)

func (s *Server) readOnlyConversationFor(ctx context.Context, convArg string) ConversationEntry {
	if s == nil || s.store == nil {
		return ConversationEntry{}
	}
	if trimmed := strings.TrimSpace(convArg); trimmed != "" {
		if c, ok := s.store.LookupConversation(trimmed); ok {
			return c
		}
		return ConversationEntry{
			Alias: trimmed,
			Token: strings.TrimPrefix(trimmed, consoleConvPrefix),
		}
	}
	meta := callMetaFromContext(ctx)
	if cid := strings.TrimSpace(meta.ConversationID); cid != "" {
		if c, ok := s.store.LookupConversation(cid); ok {
			return c
		}
	}
	return s.store.ActiveConversation()
}

func (s *Server) conversationFor(ctx context.Context, convArg string, explicitAGYID string) ConversationEntry {
	if s == nil || s.store == nil {
		return ConversationEntry{}
	}
	meta := callMetaFromContext(ctx)
	agyID := firstNonEmptyStr(explicitAGYID, meta.ConversationID)
	if trimmed := strings.TrimSpace(convArg); trimmed != "" {
		if c, err := s.store.EnsureConversationWithOptions(trimmed, false, "", true); err == nil {
			if agyID != "" && !isSessionSubagentEntry(c) && !c.SessionMode && strings.TrimSpace(c.SessionWorkerID) == "" && (c.AGYConversationID == "" || c.AGYConversationID == agyID) {
				s.store.BindConversationAGYID(c.Alias, agyID)
				c.AGYConversationID = agyID
			}
			return c
		}
	}
	if agyID != "" {
		if c, ok := s.store.LookupConversation(agyID); ok {
			return c
		}
		activeBoundID := strings.TrimSpace(s.store.ActiveConversation().AGYConversationID)
		activate := strings.TrimSpace(explicitAGYID) != "" || activeBoundID == "" || activeBoundID == agyID
		return s.store.ConversationForAGYWithOptions(ConversationLineage{
			ConversationID:     agyID,
			RootConversationID: agyID,
		}, activate)
	}
	return s.store.ActiveConversation()
}

func (s *Server) handleDiscoverDeployments(_ context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		KubeContext string `json:"kube_context"`
		Context     string `json:"context"`
		Filter      string `json:"filter"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	ctxFilter := strings.ToLower(strings.TrimSpace(firstNonEmptyStr(args.KubeContext, args.Context, args.Filter)))
	snap := s.store.Snapshot()
	all := make([]DeploymentProfile, 0, len(snap.Deployments))
	for _, p := range snap.Deployments {
		if ctxFilter != "" &&
			!strings.Contains(strings.ToLower(p.Name), ctxFilter) &&
			!strings.Contains(strings.ToLower(p.KubeContext), ctxFilter) &&
			!strings.Contains(strings.ToLower(p.ClusterName), ctxFilter) &&
			!strings.Contains(strings.ToLower(p.PlatformAgentName), ctxFilter) &&
			!strings.Contains(strings.ToLower(p.ProjectID), ctxFilter) {
			continue
		}
		all = append(all, p)
	}
	sort.Slice(all, func(i, j int) bool {
		if all[i].Name == snap.ActiveDeployment {
			return true
		}
		if all[j].Name == snap.ActiveDeployment {
			return false
		}
		if all[i].Ready != all[j].Ready {
			return all[i].Ready
		}
		return all[i].Name < all[j].Name
	})
	return FormatDeploymentsMarkdown(all, snap.ActiveDeployment), nil
}

func (s *Server) handleConnectDeployment(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		Deployment        string `json:"deployment"`
		Name              string `json:"name"`
		Cluster           string `json:"cluster"`
		KubeContext       string `json:"kube_context"`
		Context           string `json:"context"`
		Namespace         string `json:"namespace"`
		PlatformAgentName string `json:"platform_agent_name"`
		DirectNATSURL     string `json:"direct_nats_url"`
		BusUser           string `json:"bus_user"`
		BusAuthValue      string `json:"bus_auth_value"`
		AGYConversationID string `json:"agy_conversation_id"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	if args.AGYConversationID != "" {
		enabled := true
		_, _ = s.store.UpdatePushConfig(args.AGYConversationID, &enabled, "")
	}

	var targetProfile DeploymentProfile
	selector := firstNonEmptyStr(args.Deployment, args.Name, args.Cluster, args.KubeContext, args.Context)

	if args.DirectNATSURL != "" {
		name := firstNonEmptyStr(args.Deployment, args.Name, directProfileDefaultName)
		targetProfile = DeploymentProfile{
			Name:              name,
			KubeContext:       firstNonEmptyStr(args.KubeContext, args.Context),
			Namespace:         firstNonEmptyStr(args.Namespace, directProfileDefaultNS),
			PlatformAgentName: firstNonEmptyStr(args.PlatformAgentName, directProfileDefaultPAName),
			DirectNATSURL:     args.DirectNATSURL,
			BusUser:           firstNonEmptyStr(args.BusUser, defaultBusUser),
			BusAuthValue:      args.BusAuthValue,
			Ready:             true,
			LastDiscoveredAt:  time.Now().UTC(),
		}
		_ = s.store.UpsertDeployments([]DeploymentProfile{targetProfile})
	} else {
		p, err := s.store.SetActiveDeployment(selector)
		if err != nil {
			return "", err
		}
		targetProfile = p
	}

	sess, err := s.connectSession(ctx, targetProfile)
	if err != nil {
		return "", err
	}
	_, _ = s.store.SetActiveDeployment(targetProfile.Name)
	sess.WaitForDeepBootstrap(2 * time.Second)
	conv := s.conversationFor(ctx, "", args.AGYConversationID)

	rep := sess.StatusReport(ctx)
	snap := s.store.Snapshot()
	rep.SessionConversations = collectSessionConversations(snap.Conversations)
	return FormatBusStatusMarkdown(rep, conv, snap.Push, s.store.ResolveAGYConversationID()), nil
}

func collectSessionConversations(convs map[string]ConversationEntry) []ConversationEntry {
	var out []ConversationEntry
	for _, c := range convs {
		if c.SessionMode {
			out = append(out, c)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].Alias < out[j].Alias
	})
	return out
}

func (s *Server) refreshActiveSessionPods(ctx context.Context) {
	s.mu.Lock()
	sess := s.session
	s.mu.Unlock()
	if sess == nil || !sess.IsHealthy() {
		if ensured, err := s.ensureConnected(ctx); err == nil {
			sess = ensured
		}
	}
	if sess == nil {
		return
	}
	curProf := sess.Profile()
	if !curProf.SessionPodsProbedAt.IsZero() && time.Since(curProf.SessionPodsProbedAt) < sessionPodsProbeTTL {
		return
	}
	sess.WaitForDeepBootstrap(2 * time.Second)
	sess.DeriveActiveSessionPods(ctx)
	if s.store != nil {
		_ = s.store.UpsertDeployments([]DeploymentProfile{sess.Profile()})
	}
}

func (s *Server) handleConnectionStatus(ctx context.Context) (string, error) {
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", err
	}
	s.refreshActiveSessionPods(ctx)
	rep := sess.StatusReport(ctx)
	snap := s.store.Snapshot()
	rep.SessionConversations = collectSessionConversations(snap.Conversations)
	conv := s.readOnlyConversationFor(ctx, "")
	return FormatBusStatusMarkdown(rep, conv, snap.Push, s.store.ResolveAGYConversationID()), nil
}

func (s *Server) handleSendMessage(ctx context.Context, rawArgs json.RawMessage) (string, string, string, error) {
	var args struct {
		Message            string `json:"message"`
		Prompt             string `json:"prompt"`
		Mode               string `json:"mode"`
		Conversation       string `json:"conversation"`
		WaitTimeoutSeconds int    `json:"wait_timeout_seconds"`
		TimeoutSeconds     int    `json:"timeout_seconds"`
		AGYConversationID  string `json:"agy_conversation_id"`
		Debug              bool   `json:"debug"`
		StreamSteps        *bool  `json:"stream_steps"`
	}
	if err := json.Unmarshal(rawArgs, &args); err != nil {
		return "", "", "", fmt.Errorf("invalid arguments: %w", err)
	}
	if args.AGYConversationID != "" {
		enabled := true
		_, _ = s.store.UpdatePushConfig(args.AGYConversationID, &enabled, "")
	}
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", "", "", err
	}
	conv := s.conversationFor(ctx, args.Conversation, args.AGYConversationID)

	waitTimeout := defaultTurnWaitTimeout
	if args.WaitTimeoutSeconds > 0 {
		waitTimeout = time.Duration(args.WaitTimeoutSeconds) * time.Second
	} else if args.TimeoutSeconds > 0 {
		waitTimeout = time.Duration(args.TimeoutSeconds) * time.Second
	}

	debug := args.Debug || s.store.DebugModeFor(conv.Alias)
	turnCtx := ctx
	if !debug {
		turnCtx = WithoutProgressCallback(ctx)
	}

	meta := callMetaFromContext(ctx)
	isAGYCall := meta.ConversationID != "" || meta.ProgressToken != nil || strings.TrimSpace(args.AGYConversationID) != ""
	streamSteps := isAGYCall
	if args.StreamSteps != nil {
		streamSteps = *args.StreamSteps
	}

	msgText := firstNonEmptyStr(args.Message, args.Prompt)
	outcome, err := sess.SendTurnWithOptions(turnCtx, s.store, conv, msgText, args.Mode, waitTimeout, streamSteps)
	title, customMD, llmResult := FormatTurnCustomToolEnvelope(outcome, debug)
	if outcome != nil && !outcome.TimedOut {
		customMD, llmResult = s.appendSessionSwarmLink(ctx, conv, customMD, llmResult)
	}
	if !streamSteps && !isAGYCall {
		return FormatTurnOutcomeMarkdownWithOptions(outcome, debug), title, customMD, err
	}
	return llmResult, title, customMD, err
}

func (s *Server) appendSessionSwarmLink(_ context.Context, conv ConversationEntry, customMD, llmResult string) (string, string) {
	if !isSessionSubagentEntry(conv) {
		return customMD, llmResult
	}
	if customMD != "" && !strings.Contains(customMD, "sidecar://swarm/swarm_board/") {
		customMD = strings.TrimRight(customMD, "\n") + "\n- **Swarm UI**: " + swarmBoardLink
	}
	if llmResult != "" && !strings.Contains(llmResult, "sidecar://swarm/swarm_board/") {
		llmResult = strings.TrimRight(llmResult, "\n") + "\n\n" + swarmBoardLink + "\n"
	}
	return customMD, llmResult
}

func (s *Server) handleManageSession(ctx context.Context, rawArgs json.RawMessage) (string, string, string, error) {
	var args struct {
		Action       string `json:"action"`
		Alias        string `json:"alias"`
		Conversation string `json:"conversation"`
		Description  string `json:"description"`
		Debug        bool   `json:"debug"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	targetAlias := firstNonEmptyStr(args.Alias, args.Conversation)
	action := strings.ToLower(strings.TrimSpace(args.Action))
	if action == "" {
		action = actionList
	}
	debug := args.Debug || s.store.DebugModeFor(s.readOnlyConversationFor(ctx, targetAlias).Alias)

	switch action {
	case actionList:
		s.refreshActiveSessionPods(ctx)
		snap := s.store.Snapshot()
		activeConv := s.readOnlyConversationFor(ctx, "")
		activeAlias := firstNonEmptyStr(activeConv.Alias, snap.ActiveConversation)

		podBySessionKey := make(map[string]SessionPodInfo)
		var runningPodList []SessionPodInfo
		if prof, ok := s.store.ActiveDeployment(); ok {
			runningPodList = prof.ActiveSessionPods
			for _, pod := range prof.ActiveSessionPods {
				if k := strings.TrimSpace(pod.SessionKey); k != "" {
					podBySessionKey[k] = pod
					podBySessionKey[strings.TrimPrefix(k, consoleConvPrefix)] = pod
				}
			}
		}

		sessionConvCount := 0
		for _, c := range snap.Conversations {
			if c.SessionMode {
				sessionConvCount++
			}
		}

		var sb strings.Builder
		sb.WriteString("## 💬 Saved Kube-Agents Conversations & Sessions\n\n")
		fmt.Fprintf(&sb, "- **Active Conversation**: **`%s`** (`console:%s`)\n", activeAlias, firstNonEmptyStr(activeConv.Token, activeAlias))
		fmt.Fprintf(&sb, "- **Debug Mode (Active Conversation)**: `%v`\n", s.store.DebugModeFor(activeAlias))
		fmt.Fprintf(&sb, "- **`/session` Worker Conversations**: `%d` configured (`%d` live K8s pod(s) currently running; idle `/session` pods are reaped after 30m of inactivity and **automatically respawned with full transcript rehydration** when you message them)\n\n",
			sessionConvCount, len(runningPodList))
		sb.WriteString("| Active | Alias | Console Token | Mode | Cluster Pod Status | Last Used | Description |\n")
		sb.WriteString("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
		allAliases := make([]string, 0, len(snap.Conversations))
		for k := range snap.Conversations {
			allAliases = append(allAliases, k)
		}
		sort.Slice(allAliases, func(i, j int) bool {
			ai, aj := allAliases[i], allAliases[j]
			if (ai == activeAlias) != (aj == activeAlias) {
				return ai == activeAlias
			}
			ci, cj := snap.Conversations[ai], snap.Conversations[aj]
			if (ci.SessionMode || ci.DebugMode) != (cj.SessionMode || cj.DebugMode) {
				return ci.SessionMode || ci.DebugMode
			}
			if !ci.LastUsedAt.Equal(cj.LastUsedAt) {
				return ci.LastUsedAt.After(cj.LastUsedAt)
			}
			return ai < aj
		})
		omittedCount := 0
		allShown := allAliases
		if len(allShown) > maxConversationsShown {
			omittedCount = len(allShown) - maxConversationsShown
			allShown = allShown[:maxConversationsShown]
		}
		for _, a := range allShown {
			c := snap.Conversations[a]
			act := badgeStandby
			if a == activeAlias {
				act = badgeActive
			}
			mLabel := "`platform`"
			podStatus := "🟢 `platform` (Hermes)"
			if c.SessionMode {
				mLabel = "`/session` (persistent worker)"
				if pod, hasPod := podBySessionKey[c.Token]; hasPod {
					addr := firstNonEmptyStr(pod.Addressee, pod.Name)
					if pod.Name != "" && pod.Name != addr {
						podStatus = fmt.Sprintf("🟢 **Pod Running** (`%s`, `%s`)", pod.Name, addr)
					} else {
						podStatus = fmt.Sprintf("🟢 **Pod Running** (`%s`)", firstNonEmptyStr(pod.Name, addr))
					}
				} else {
					podStatus = "💤 **On-Demand** *(auto-spawns pod + rehydrates transcript on message)*"
				}
			}
			if c.DebugMode {
				mLabel += " (🐞 `debug`)"
			}
			fmt.Fprintf(&sb, "| %s | **`%s`** | `console:%s` | %s | %s | `%s` | %s |\n",
				act, c.Alias, c.Token, mLabel, podStatus, c.LastUsedAt.Format(time.RFC3339), firstNonEmptyStr(c.Description, "—"))
		}
		if omittedCount > 0 {
			fmt.Fprintf(&sb, "\n*(+ %d older idle conversations omitted)*\n", omittedCount)
		}
		sb.WriteString("\n- **Swarm Fleet & Session Board**: [Open the Swarm UI](sidecar://swarm/swarm_board/)\n")
		return sb.String(), "", "", nil

	case actionDebugOn:
		conv := s.conversationFor(ctx, targetAlias, "")
		if err := s.store.SetConversationDebugMode(conv.Alias, true); err != nil {
			return "", "", "", err
		}
		return fmt.Sprintf("✅ Enabled `kube-agents` **Debug Mode** for conversation **`%s`** (`console:%s`). Tool call tables, timings, and task metadata will be shown on this conversation only.", conv.Alias, conv.Token), "", "", nil

	case actionDebugOff:
		conv := s.conversationFor(ctx, targetAlias, "")
		if err := s.store.SetConversationDebugMode(conv.Alias, false); err != nil {
			return "", "", "", err
		}
		_ = s.store.SetDebugMode(false) // also clear any legacy global flag
		return fmt.Sprintf("✅ Disabled `kube-agents` **Debug Mode** for conversation **`%s`**. Thinking is collapsed by default and tool call tables/timings are hidden.", conv.Alias), "", "", nil

	case actionNew:
		alias := strings.TrimSpace(targetAlias)
		if alias == "" {
			alias = fmt.Sprintf("session-%d", time.Now().UTC().Unix())
		}
		meta := callMetaFromContext(ctx)
		entry, err := s.store.EnsureConversationWithOptions(alias, true, args.Description, true)
		if err != nil {
			return "", "", "", err
		}
		if strings.TrimSpace(meta.ConversationID) != "" {
			s.store.BindConversationAGYID(entry.Alias, meta.ConversationID)
		}
		return fmt.Sprintf("✅ Created and switched to new conversation **`%s`** (token `console:%s`). [Open the Swarm UI](sidecar://swarm/swarm_board/)", entry.Alias, entry.Token), "", "", nil

	case actionSwitch:
		if strings.TrimSpace(targetAlias) == "" {
			return "", "", "", errors.New("switch requires alias")
		}
		meta := callMetaFromContext(ctx)
		entry, err := s.store.EnsureConversationWithOptions(targetAlias, false, args.Description, true)
		if err != nil {
			return "", "", "", err
		}
		if strings.TrimSpace(meta.ConversationID) != "" && !isSessionSubagentEntry(entry) && !entry.SessionMode && strings.TrimSpace(entry.SessionWorkerID) == "" {
			s.store.BindConversationAGYID(entry.Alias, meta.ConversationID)
		}
		return fmt.Sprintf("✅ Switched active conversation to **`%s`** (token `console:%s`, sessionMode=`%v`).", entry.Alias, entry.Token, entry.SessionMode), "", "", nil

	case actionSessionOn, actionSessionStartAlias, actionSessionOff, actionSessionEndAlias, actionStatus, actionHelp:
		sess, err := s.ensureConnected(ctx)
		if err != nil {
			return "", "", "", err
		}
		var conv ConversationEntry
		meta := callMetaFromContext(ctx)
		if strings.TrimSpace(targetAlias) != "" {
			conv, err = s.store.EnsureConversationWithOptions(targetAlias, false, args.Description, true)
			if err != nil {
				return "", "", "", err
			}
			if strings.TrimSpace(meta.ConversationID) != "" && !isSessionSubagentEntry(conv) && !conv.SessionMode && strings.TrimSpace(conv.SessionWorkerID) == "" {
				s.store.BindConversationAGYID(conv.Alias, meta.ConversationID)
			}
			conv = s.conversationFor(ctx, conv.Alias, "")
		} else {
			conv = s.conversationFor(ctx, "", "")
		}
		cmd := cmdSessionOn
		switch action {
		case actionSessionOff, actionSessionEndAlias:
			cmd = cmdSessionOff
		case actionStatus:
			cmd = cmdStatus
		case actionHelp:
			cmd = cmdHelp
		}
		turnCtx := ctx
		if !debug {
			turnCtx = WithoutProgressCallback(ctx)
		}
		outcome, err := sess.SendTurn(turnCtx, s.store, conv, cmd, modeAuto, controlNoticeWait)
		md := FormatTurnOutcomeMarkdownWithOptions(outcome, debug)
		if cmd == cmdSessionOn || cmd == cmdSessionOff {
			md += "\n\n- **Swarm Fleet & Session Board**: [Open the Swarm UI](sidecar://swarm/swarm_board/)\n"
		}
		return md, "", "", err

	case actionTranscript:
		sess, err := s.ensureConnected(ctx)
		if err != nil {
			return "", "", "", err
		}
		sess.WaitForDeepBootstrap(deepBootstrapWaitTimeout)
		conv := s.readOnlyConversationFor(ctx, targetAlias)
		rawTasks := sess.ListTasks(conv.Token, false, defaultTranscriptLookupLimit)
		var tasks []*TaskRecord
		for _, t := range rawTasks {
			if t != nil && t.CardParentTaskID == "" && t.DelegatedFromTaskID == "" && t.WakeFromChildTaskID == "" {
				tasks = append(tasks, t)
			}
		}
		if len(tasks) == 0 {
			return fmt.Sprintf("No tasks recorded yet on conversation **`%s`** (`console:%s`).", conv.Alias, conv.Token), "", "", nil
		}
		var sb strings.Builder
		header := fmt.Sprintf("## 📜 Conversation Transcript: `%s` (`console:%s`)\n\n", conv.Alias, conv.Token)
		sb.WriteString(header)
		// Render newest-last, ensuring newest turns are preserved and older turns are compactly
		// summarized before ever omitting a turn so multi-turn transcripts stay inline (<3.55 KB).
		var sections []string
		totalBytes := sb.Len()
		omittedOlder := 0
		for i := 0; i < len(tasks); i++ {
			t := tasks[i]
			outcome := sess.BuildTaskOutcome(t.TaskID, conv.Alias, conv.Token, t.PromptText, false)
			sStr := FormatTranscriptTurnMarkdown(outcome, 0, debug)
			if i == 0 && totalBytes+len(sStr) > maxTranscriptBytes {
				sStr = FormatTranscriptTurnMarkdown(outcome, transcriptPrimaryCapRunes, debug)
			} else if i == 0 && len(tasks) > 1 && debug && len(sStr) > transcriptPrimaryCapRunes {
				sStr = FormatTranscriptTurnMarkdown(outcome, transcriptDebugCapRunes, debug)
			} else if len(sections) > 0 && totalBytes+len(sStr) > maxTranscriptBytes {
				// Try compacting older turn answer bodies (800, 450, then 200 runes) before omitting the turn.
				for _, capRunes := range []int{transcriptOlderCapLarge, transcriptOlderCapMedium, transcriptOlderCapSmall} {
					candidate := FormatTranscriptTurnMarkdown(outcome, capRunes, debug)
					if totalBytes+len(candidate) <= maxTranscriptBytes {
						sStr = candidate
						break
					}
				}
			}
			if len(sections) > 0 && totalBytes+len(sStr) > maxTranscriptBytes {
				omittedOlder = len(tasks) - i
				break
			}
			sections = append(sections, sStr)
			totalBytes += len(sStr)
		}
		if omittedOlder > 0 {
			note := fmt.Sprintf("*(%d older task(s) omitted to keep transcript concise; use `task_control` `action: \"list\"` to inspect earlier task IDs)*\n\n---\n", omittedOlder)
			sb.WriteString(note)
		}
		for i := len(sections) - 1; i >= 0; i-- {
			sb.WriteString(sections[i])
		}
		return sb.String(), "", "", nil

	case actionDelete, actionRemoveAlias:
		if strings.TrimSpace(targetAlias) == "" {
			return "", "", "", errors.New("delete requires alias (or token / session member name)")
		}
		if c, ok := s.store.LookupConversation(targetAlias); ok && (c.SessionMode || c.SessionWorkerID != "") {
			if sess, err := s.ensureConnected(ctx); err == nil && sess != nil {
				turnCtx := WithoutProgressCallback(ctx)
				_, _ = sess.SendTurn(turnCtx, s.store, c, cmdSessionOff, modeAuto, controlNoticeWait)
			}
		}
		deleted, ok := s.store.DeleteConversation(targetAlias)
		if !ok {
			return "", "", "", fmt.Errorf("conversation or session %q not found", targetAlias)
		}
		aliasLabel := firstNonEmptyStr(deleted.Alias, targetAlias)
		tokenLabel := firstNonEmptyStr(deleted.Token, aliasLabel)
		return fmt.Sprintf("🗑️ Deleted conversation/session **`%s`** (`console:%s`) and removed it from the Swarm board. [Open the Swarm UI](sidecar://swarm/swarm_board/)", aliasLabel, tokenLabel), "", "", nil

	case actionClearKilled, actionPruneAlias:
		s.refreshActiveSessionPods(ctx)
		activePodTokens := make(map[string]bool)
		if prof, ok := s.store.ActiveDeployment(); ok {
			for _, pod := range prof.ActiveSessionPods {
				tok := strings.TrimPrefix(strings.TrimSpace(pod.SessionKey), consoleConvPrefix)
				if tok != "" {
					activePodTokens[tok] = true
				}
			}
		}
		removed := s.store.ClearKilledSessions(activePodTokens)
		if len(removed) == 0 {
			return "✅ No killed or terminated sessions found to clear. [Open the Swarm UI](sidecar://swarm/swarm_board/)", "", "", nil
		}
		names := make([]string, 0, len(removed))
		for _, r := range removed {
			names = append(names, fmt.Sprintf("`%s`", r.Alias))
		}
		sort.Strings(names)
		return fmt.Sprintf("🧹 Cleared **%d** killed/inactive session(s): %s. [Open the Swarm UI](sidecar://swarm/swarm_board/)", len(removed), strings.Join(names, ", ")), "", "", nil

	default:
		return "", "", "", fmt.Errorf("unknown manage_session action %q (expected list, new, switch, session_on, session_off, delete, clear_killed, debug_on, debug_off, status, help, transcript)", action)
	}
}

func (s *Server) handleTaskControl(ctx context.Context, rawArgs json.RawMessage) (string, string, string, error) {
	var args struct {
		Action             string `json:"action"`
		TaskID             string `json:"task_id"`
		Conversation       string `json:"conversation"`
		Message            string `json:"message"`
		OnlyActive         bool   `json:"only_active"`
		Limit              int    `json:"limit"`
		WaitTimeoutSeconds int    `json:"wait_timeout_seconds"`
		Debug              bool   `json:"debug"`
		StreamSteps        *bool  `json:"stream_steps"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", "", "", err
	}
	action := strings.ToLower(strings.TrimSpace(args.Action))
	if action == "" {
		if args.TaskID != "" {
			action = actionGet
		} else {
			action = actionList
		}
	}
	debug := args.Debug || s.store.DebugModeFor(s.readOnlyConversationFor(ctx, args.Conversation).Alias)
	turnCtx := ctx
	if !debug {
		turnCtx = WithoutProgressCallback(ctx)
	}
	meta := callMetaFromContext(ctx)
	isAGYCall := meta.ConversationID != "" || meta.ProgressToken != nil
	streamSteps := isAGYCall
	if args.StreamSteps != nil {
		streamSteps = *args.StreamSteps
	}

	switch action {
	case actionGet:
		taskID := strings.Trim(args.TaskID, " \t\n\r`\"'")
		if taskID == "" {
			return "", "", "", errors.New("task_control action 'get' requires task_id")
		}
		if streamSteps {
			stepWait := defaultStepStreamWindow
			if args.WaitTimeoutSeconds > 0 {
				reqWait := time.Duration(args.WaitTimeoutSeconds) * time.Second
				if reqWait < stepWait {
					stepWait = reqWait
				}
			}
			waitCtx, cancel := context.WithTimeout(turnCtx, stepWait)
			_ = sess.WaitForTaskStepOrSettlement(waitCtx, taskID, true)
			cancel()
		} else if args.WaitTimeoutSeconds > 0 && !sess.IsTaskTreeSettled(taskID) {
			waitCtx, cancel := context.WithTimeout(turnCtx, time.Duration(args.WaitTimeoutSeconds)*time.Second)
			_ = sess.WaitForTaskStepOrSettlement(waitCtx, taskID, false)
			cancel()
		}
		rec := sess.GetTask(taskID)
		if rec == nil {
			return "", "", "", fmt.Errorf("task %q not found on TASKS stream", args.TaskID)
		}
		settled := sess.IsTaskTreeSettled(taskID)
		conv := s.readOnlyConversationFor(ctx, args.Conversation)
		outcome := sess.BuildTaskOutcome(taskID, conv.Alias, conv.Token, rec.PromptText, !settled)
		title, customMD, llmResult := FormatTurnCustomToolEnvelope(outcome, debug)
		if outcome != nil && !outcome.TimedOut {
			customMD, llmResult = s.appendSessionSwarmLink(ctx, conv, customMD, llmResult)
		}
		if !streamSteps && !isAGYCall {
			var sb strings.Builder
			sb.WriteString(FormatTaskRecordMarkdownWithOptions(rec, "Task Detail", debug))
			children := sess.findChildTasks(rec.TaskID)
			for i, c := range children {
				sb.WriteString("\n---\n")
				sb.WriteString(FormatTaskRecordMarkdownWithOptions(c, fmt.Sprintf("Delegated Child Task #%d", i+1), debug))
				if wake := sess.findWakeTask(c.TaskID); wake != nil {
					sb.WriteString("\n---\n")
					sb.WriteString(FormatTaskRecordMarkdownWithOptions(wake, "Session Wake Synthesis Task", debug))
				}
			}
			return sb.String(), title, customMD, nil
		}
		return llmResult, title, customMD, nil

	case actionList:
		if !args.OnlyActive {
			sess.WaitForDeepBootstrap(deepBootstrapWaitTimeout)
		}
		limit := args.Limit
		if limit <= 0 {
			limit = defaultTaskListLimit
		}
		convFilter := strings.TrimSpace(args.Conversation)
		if convFilter != "" && s.store != nil {
			if c, ok := s.store.LookupConversation(convFilter); ok && c.Token != "" {
				convFilter = c.Token
			}
		}
		tasks := sess.ListTasks(convFilter, args.OnlyActive, limit)
		if len(tasks) == 0 {
			return "No matching tasks found on `TASKS` stream.\n", "", "", nil
		}
		var sb strings.Builder
		sb.WriteString("## 📋 Kube-Agents Tasks (`TASKS`)\n\n")
		sb.WriteString("| Task ID | Addressee | State | Conversation | Delegated From | Updated At | Prompt / Result Summary |\n")
		sb.WriteString("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
		for _, t := range tasks {
			summary := firstNonEmptyStr(t.PromptText, t.ResultText, t.ProgressText)
			if t.ResultChat != nil && t.ResultChat.Headline != "" {
				summary = t.ResultChat.Headline
			}
			fmt.Fprintf(&sb, "| **`%s`** | `%s` | %s | `%s` | `%s` | `%s` | %s |\n",
				t.TaskID,
				t.Addressee,
				formatTaskStateBadge(t.State),
				firstNonEmptyStr(t.Conversation, "—"),
				firstNonEmptyStr(t.DelegatedFromTaskID, "—"),
				t.UpdatedAt.Format(time.RFC3339),
				escapeTableCell(truncateText(summary, taskSummaryPreviewChars)),
			)
		}
		return sb.String(), "", "", nil

	case actionStatus:
		conv := s.conversationFor(ctx, args.Conversation, "")
		outcome, err := sess.SendTurn(turnCtx, s.store, conv, cmdStatus, modeAuto, controlNoticeWait)
		title, customMD, _ := FormatTurnCustomToolEnvelope(outcome, debug)
		return FormatTurnOutcomeMarkdownWithOptions(outcome, debug), title, customMD, err

	case actionStop, actionCancelAlias:
		convArg := strings.TrimSpace(args.Conversation)
		if convArg == "" && strings.TrimSpace(args.TaskID) != "" {
			if rec := sess.GetTask(strings.Trim(args.TaskID, " \t\n\r`\"'")); rec != nil && strings.HasPrefix(rec.Conversation, consoleConvPrefix) {
				convArg = strings.TrimPrefix(rec.Conversation, consoleConvPrefix)
			}
		}
		conv := s.conversationFor(ctx, convArg, "")
		outcome, err := sess.SendTurn(turnCtx, s.store, conv, cmdStop, modeAuto, controlNoticeWait)
		title, customMD, _ := FormatTurnCustomToolEnvelope(outcome, debug)
		return FormatTurnOutcomeMarkdownWithOptions(outcome, debug), title, customMD, err

	case actionSteer:
		if strings.TrimSpace(args.Message) == "" {
			return "", "", "", errors.New("task_control action 'steer' requires message")
		}
		conv := s.conversationFor(ctx, args.Conversation, "")
		waitTimeout := defaultTurnWaitTimeout
		if args.WaitTimeoutSeconds > 0 {
			waitTimeout = time.Duration(args.WaitTimeoutSeconds) * time.Second
		}
		outcome, err := sess.SendTurnWithOptions(turnCtx, s.store, conv, args.Message, modeAuto, waitTimeout, streamSteps)
		title, customMD, llmResult := FormatTurnCustomToolEnvelope(outcome, debug)
		if !streamSteps && !isAGYCall {
			return FormatTurnOutcomeMarkdownWithOptions(outcome, debug), title, customMD, err
		}
		return llmResult, title, customMD, err

	default:
		return "", "", "", fmt.Errorf("unknown task_control action %q (expected get, list, status, steer, stop)", action)
	}
}

func (s *Server) handleReadTopics(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		Action string `json:"action"`
		Topic  string `json:"topic"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", err
	}
	action := strings.ToLower(strings.TrimSpace(args.Action))
	if action == "" {
		if strings.TrimSpace(args.Topic) != "" {
			action = actionRead
		} else {
			action = actionList
		}
	}

	switch action {
	case actionList:
		return FormatTopicsMarkdown(sess.ListTopicsOrProvisioned()), nil
	case actionRead:
		if strings.TrimSpace(args.Topic) == "" {
			return FormatTopicsMarkdown(sess.ListTopicsOrProvisioned()), nil
		}
		snap, err := sess.ReadTopic(args.Topic)
		if err != nil {
			return "", err
		}
		return FormatTopicDetailMarkdown(snap), nil
	case actionCards:
		s.refreshActiveSessionPods(ctx)
		rep := sess.StatusReport(ctx)
		snap := s.store.Snapshot()
		return FormatBusStatusMarkdown(rep, s.readOnlyConversationFor(ctx, ""), snap.Push, s.store.ResolveAGYConversationID()), nil
	default:
		return "", fmt.Errorf("unknown read_topics action %q (expected list, read, cards)", action)
	}
}

func (s *Server) handleInspectPR(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		PR        string `json:"pr"`
		Repo      string `json:"repo"`
		OutputDir string `json:"output_dir"`
	}
	if err := json.Unmarshal(rawArgs, &args); err != nil || strings.TrimSpace(args.PR) == "" {
		return "", errors.New("inspect_pr requires 'pr' (GitHub PR URL, owner/repo#number, or PR number)")
	}
	defaultRepo := strings.TrimSpace(args.Repo)
	if defaultRepo == "" {
		if prof, ok := s.store.ActiveDeployment(); ok {
			defaultRepo = prof.GitHubRepo
		}
	}
	outputDir := strings.TrimSpace(args.OutputDir)
	if outputDir == "" {
		if meta := callMetaFromContext(ctx); meta.ArtifactsDir != "" {
			if repo, num, pErr := ParsePRReference(args.PR, defaultRepo); pErr == nil {
				safeRepo := strings.ReplaceAll(repo, "/", "-")
				outputDir = filepath.Join(meta.ArtifactsDir, "scratch", "prs", fmt.Sprintf("%s-pr-%d", safeRepo, num))
			} else {
				outputDir = filepath.Join(meta.ArtifactsDir, "scratch", "prs")
			}
		}
	}
	insp, err := InspectPullRequest(ctx, s.runner, s.apiBaseURL, args.PR, defaultRepo, outputDir)
	if err != nil {
		return "", err
	}
	return FormatPRInspectionMarkdown(insp), nil
}

func (s *Server) handlePollEvents(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		AGYConversationID string `json:"agy_conversation_id"`
		EnablePush        *bool  `json:"enable_push"`
		AgentAPIPath      string `json:"agentapi_path"`
		PeekOnly          bool   `json:"peek_only"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	if args.AGYConversationID != "" || args.EnablePush != nil || args.AgentAPIPath != "" {
		_, err := s.store.UpdatePushConfig(args.AGYConversationID, args.EnablePush, args.AgentAPIPath)
		if err != nil {
			return "", err
		}
	}
	// Draining the local queue must never block on a bus reconnect; repair it in the background.
	s.ensureConnectedAsync()

	callerConvID := firstNonEmptyStr(strings.TrimSpace(args.AGYConversationID), strings.TrimSpace(callMetaFromContext(ctx).ConversationID))
	var events []PushEvent
	if args.PeekOnly {
		events = s.store.PeekPendingEventsForAGY(callerConvID)
	} else {
		events, _ = s.store.DrainPendingEventsForAGY(callerConvID)
	}

	snap := s.store.Snapshot()
	activeConvID := s.store.ResolveAGYConversationID()
	var sb strings.Builder
	sb.WriteString("## 🔔 Kube-Agents Bi-Directional Push & Event Queue\n\n")
	fmt.Fprintf(&sb, "- **Reactive `agentapi` Push Enabled**: `%v`\n", snap.Push.Enabled)
	fmt.Fprintf(&sb, "- **Bound Antigravity Conversation ID**: `%s`\n", firstNonEmptyStr(activeConvID, "(not bound)"))
	fmt.Fprintf(&sb, "- **AgentAPI Binary**: `%s`\n", snap.Push.AgentAPIPath)
	sb.WriteString("\n")

	if len(events) == 0 {
		sb.WriteString("No unread push events currently queued.\n")
	} else {
		fmt.Fprintf(&sb, "### 📬 Drained %d Event(s)\n\n", len(events))
		sb.WriteString(FormatPushEventsMarkdown(events))
	}
	return sb.String(), nil
}
