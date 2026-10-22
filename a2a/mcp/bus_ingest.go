package mcp

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

const (
	maxSeenEnvelopes = 16384
	maxRetainedTasks = 1024
	// apiSessionIDPrefix starts the hermes session id of a platform (API-executor) turn; any
	// other session id on an activity entry is a specialist worker's.
	apiSessionIDPrefix = "a2a-"
)

func (s *BusSession) pruneSessionMapsLocked() {
	if len(s.seenEnvelopes) >= maxSeenEnvelopes {
		target := maxSeenEnvelopes * 3 / 4
		if len(s.seenEnvelopeOrder) > 0 {
			drop := len(s.seenEnvelopeOrder) - target
			if drop < 1 {
				drop = len(s.seenEnvelopeOrder) / 4
			}
			if drop > len(s.seenEnvelopeOrder) {
				drop = len(s.seenEnvelopeOrder)
			}
			for _, id := range s.seenEnvelopeOrder[:drop] {
				delete(s.seenEnvelopes, id)
			}
			s.seenEnvelopeOrder = append([]string(nil), s.seenEnvelopeOrder[drop:]...)
		}
		for id := range s.seenEnvelopes {
			if len(s.seenEnvelopes) <= target {
				break
			}
			delete(s.seenEnvelopes, id)
		}
	}
	now := time.Now()
	for id, exp := range s.foregroundTasks {
		if now.After(exp) {
			delete(s.foregroundTasks, id)
		}
	}
	if len(s.tasks) > maxRetainedTasks {
		type taskAge struct {
			id string
			ts time.Time
		}
		var candidates []taskAge
		for id, rec := range s.tasks {
			if rec != nil && rec.Terminal && s.watchedTasks[id] == 0 {
				candidates = append(candidates, taskAge{id: id, ts: rec.UpdatedAt})
			}
		}
		sort.Slice(candidates, func(i, j int) bool {
			return candidates[i].ts.Before(candidates[j].ts)
		})
		toDrop := len(s.tasks) - (maxRetainedTasks * 3 / 4)
		for i := 0; i < len(candidates) && i < toDrop; i++ {
			id := candidates[i].id
			delete(s.tasks, id)
			delete(s.backfilledTasks, id)
			delete(s.prevReportedSteps, id)
			delete(s.lastReportedSteps, id)
			delete(s.prevReportedActs, id)
			delete(s.lastReportedActs, id)
			delete(s.lastWaitStepTitle, id)
		}
	}
}

func (s *BusSession) ingestA2AMessage(subject string, data []byte, isLive bool) {
	switch {
	case strings.HasPrefix(subject, subjectTasksPrefix):
		s.ingestTaskEnvelope(subject, data, isLive)
	case strings.HasPrefix(subject, subjectAgentsPrefix):
		s.ingestAgentCard(subject, data)
	case strings.HasPrefix(subject, subjectTopicsPrefix):
		s.ingestTopicEnvelope(subject, data)
	}
}

func (s *BusSession) isContextOrParentCanceledLocked(rec *TaskRecord) bool {
	if rec == nil {
		return false
	}
	if rec.State == lib.StateCanceled {
		return true
	}
	if strings.HasPrefix(rec.TaskID, cardTaskIDPrefix) {
		if parentID := firstNonEmptyStr(rec.CardParentTaskID, rec.DelegatedFromTaskID); parentID != "" {
			if p, ok := s.tasks[parentID]; ok && (p.State == lib.StateCanceled || p.State == lib.StateCompleted) {
				return true
			}
		}
		if rec.ContextID != "" {
			var latest *TaskRecord
			for _, other := range s.tasks {
				if other != rec && other.ContextID == rec.ContextID && !strings.HasPrefix(other.TaskID, cardTaskIDPrefix) {
					if latest == nil || other.CreatedAt.After(latest.CreatedAt) {
						latest = other
					}
				}
			}
			if latest != nil && (latest.State == lib.StateCanceled || latest.State == lib.StateCompleted) {
				return true
			}
		}
	}
	return false
}

func (s *BusSession) ingestTaskEnvelope(subject string, data []byte, isLive bool) {
	var env lib.Envelope
	if err := json.Unmarshal(data, &env); err != nil || env.TaskID == "" {
		return
	}

	s.mu.Lock()
	if env.EnvelopeID != "" && s.seenEnvelopes[env.EnvelopeID] {
		s.mu.Unlock()
		return
	}
	if env.EnvelopeID != "" {
		if len(s.seenEnvelopes) >= maxSeenEnvelopes {
			s.pruneSessionMapsLocked()
		}
		s.seenEnvelopes[env.EnvelopeID] = true
		s.seenEnvelopeOrder = append(s.seenEnvelopeOrder, env.EnvelopeID)
	}

	addressee := extractTaskAddressee(subject)
	rec, exists := s.tasks[env.TaskID]
	if !exists {
		if len(s.tasks) >= maxRetainedTasks {
			s.pruneSessionMapsLocked()
		}
		rec = &TaskRecord{
			TaskID:        env.TaskID,
			ContextID:     env.ContextID,
			CorrelationID: env.CorrelationID,
			Addressee:     addressee,
			State:         lib.StateSubmitted,
			CreatedAt:     env.TS,
			UpdatedAt:     env.TS,
		}
		s.tasks[env.TaskID] = rec
	}
	if rec.ContextID == "" {
		rec.ContextID = env.ContextID
	}
	if rec.CorrelationID == "" {
		rec.CorrelationID = env.CorrelationID
	}
	if rec.Addressee == "" && addressee != "" {
		rec.Addressee = addressee
	}
	if env.TS.After(rec.UpdatedAt) {
		rec.UpdatedAt = env.TS
	}
	// Live delivery can race the stream bootstrap: a task first seen through a live envelope gets
	// its true start once the earlier history arrives, so elapsed time is measured from the
	// submission rather than from the moment this process (re)connected.
	if !env.TS.IsZero() && (rec.CreatedAt.IsZero() || env.TS.Before(rec.CreatedAt)) {
		rec.CreatedAt = env.TS
	}
	rec.EnvelopeCount++
	if rec.EnvelopeCountsByKind == nil {
		rec.EnvelopeCountsByKind = make(map[string]int)
	}
	rec.EnvelopeCountsByKind[string(env.Kind)]++

	if len(env.Authority) > 0 {
		var auth authorityWire
		if json.Unmarshal(env.Authority, &auth) == nil {
			if auth.Audience.Conversation != "" && rec.Conversation == "" {
				rec.Conversation = auth.Audience.Conversation
			}
			parentTaskID := ""
			if auth.Via != nil && auth.Via.TaskID != "" {
				parentTaskID = auth.Via.TaskID
			} else if auth.DelegatedFrom != nil && auth.DelegatedFrom.TaskID != "" {
				parentTaskID = auth.DelegatedFrom.TaskID
			}
			switch {
			case parentTaskID == "":
			case isWakeAuthority(auth, rec):
				// The gateway publishes a wake as the session's next turn: addressed to the
				// session worker and naming the finished child in Via. The link is read from
				// the authority block, not from the wake's templated prompt text.
				if rec.WakeFromChildTaskID == "" {
					rec.WakeFromChildTaskID = parentTaskID
				}
			case rec.DelegatedFromTaskID == "":
				rec.DelegatedFromTaskID = parentTaskID
				if parent, ok := s.tasks[rec.DelegatedFromTaskID]; ok {
					for i := range parent.Delegates {
						if parent.Delegates[i].ChildTaskID == "" || parent.Delegates[i].ChildTaskID == rec.TaskID {
							parent.Delegates[i].ChildTaskID = rec.TaskID
							break
						}
					}
				}
			}
		}
	}
	if rec.Conversation == "" && rec.ContextID != "" {
		for _, other := range s.tasks {
			if other != rec && other.ContextID == rec.ContextID && other.Conversation != "" {
				rec.Conversation = other.Conversation
				break
			}
		}
	}
	if rec.CardParentTaskID == "" && strings.HasPrefix(rec.TaskID, cardTaskIDPrefix) {
		rec.CardParentTaskID = s.findCardParentLocked(rec)
	}

	var pushToFire *PushEvent
	// A wake's structural parent is the child that woke it: through it the wake inherits the
	// foreground turn's watch (child -> delegating turn), exactly as a delegated child does.
	watched := s.isWatchedLocked(rec.Conversation, rec.TaskID, firstNonEmptyStr(rec.DelegatedFromTaskID, rec.CardParentTaskID, rec.WakeFromChildTaskID))

	switch env.Kind {
	case lib.KindMessage:
		var msg lib.Message
		if json.Unmarshal(env.Payload, &msg) == nil {
			text := joinMessageText(msg.Parts)
			if rec.PromptText == "" {
				rec.PromptText = text
				rec.promptTS = env.TS
				if childID := extractWakeChildTaskID(text); childID != "" {
					rec.WakeFromChildTaskID = childID
				}
			} else if !env.TS.IsZero() && !rec.promptTS.IsZero() && env.TS.Before(rec.promptTS) {
				if rec.PromptText != "" {
					rec.SteerTurns = append([]string{rec.PromptText}, rec.SteerTurns...)
					rec.SteerTimestamps = append([]time.Time{rec.promptTS}, rec.SteerTimestamps...)
				}
				rec.PromptText = text
				rec.promptTS = env.TS
				if childID := extractWakeChildTaskID(text); childID != "" {
					rec.WakeFromChildTaskID = childID
				}
			} else if text != "" {
				rec.SteerTurns = append(rec.SteerTurns, text)
				rec.SteerTimestamps = append(rec.SteerTimestamps, env.TS)
			}
		}
	case lib.KindStatusUpdate:
		var st lib.StatusUpdate
		if json.Unmarshal(env.Payload, &st) == nil {
			prevTerminal := rec.Terminal
			prevState := rec.State
			newTerminal := st.Final || st.Status.State.Terminal()
			isOlder := !rec.lastStatusTS.IsZero() && !env.TS.IsZero() && env.TS.Before(rec.lastStatusTS)
			if (!prevTerminal || newTerminal) && (!isOlder || newTerminal) &&
				!(st.Status.State == lib.StateSubmitted && (prevState == lib.StateWorking || prevState == lib.StateInputRequired || prevTerminal)) {
				rec.State = st.Status.State
				rec.Terminal = prevTerminal || newTerminal
				if !env.TS.IsZero() && (rec.lastStatusTS.IsZero() || env.TS.After(rec.lastStatusTS)) {
					rec.lastStatusTS = env.TS
				}
				if st.Status.Message != nil {
					if msgTxt := joinMessageText(st.Status.Message.Parts); msgTxt != "" || !isOlder {
						rec.StatusText = msgTxt
					}
				}
			}
			if isLive && s.bootstrapped && !watched {
				if rec.Terminal && !prevTerminal && !s.isContextOrParentCanceledLocked(rec) {
					kind := eventKindTaskCompleted
					title := fmt.Sprintf("Task %s (%s) %s", rec.TaskID, rec.Addressee, rec.State)
					if rec.State == lib.StateFailed || rec.State == lib.StateRejected || rec.State == lib.StateCanceled {
						kind = eventKindTaskFailed
					}
					summary := rec.ResultText
					if summary == "" {
						summary = rec.StatusText
					}
					if summary == "" {
						summary = rec.ProgressText
					}
					pushToFire = &PushEvent{
						ID:           fmt.Sprintf("task-term-%s-%s", rec.TaskID, rec.State),
						Kind:         kind,
						Deployment:   s.profile.Name,
						Conversation: rec.Conversation,
						TaskID:       rec.TaskID,
						ContextID:    rec.ContextID,
						Subject:      subject,
						Title:        title,
						Summary:      truncateText(summary, pushSummaryMaxChars),
						PRLinks:      append([]string(nil), rec.PRLinks...),
						Timestamp:    env.TS,
					}
				} else if st.Status.State == lib.StateInputRequired && prevState != lib.StateInputRequired {
					pushToFire = &PushEvent{
						ID:           fmt.Sprintf("task-input-%s-%d", rec.TaskID, rec.EnvelopeCount),
						Kind:         eventKindInputRequired,
						Deployment:   s.profile.Name,
						Conversation: rec.Conversation,
						TaskID:       rec.TaskID,
						ContextID:    rec.ContextID,
						Subject:      subject,
						Title:        fmt.Sprintf("Input Required on Task %s (%s)", rec.TaskID, rec.Addressee),
						Summary:      firstNonEmptyStr(rec.StatusText, rec.ProgressText, rec.ResultText),
						Timestamp:    env.TS,
					}
				}
			}
		}
	case lib.KindArtifactUpdate:
		var au lib.ArtifactUpdate
		if json.Unmarshal(env.Payload, &au) == nil {
			art := au.Artifact
			if art.Name != "" {
				rec.EnvelopeCountsByKind["artifact:"+art.Name]++
			}
			text := joinMessageText(art.Parts)
			dataText := joinDataOrTextParts(art.Parts)
			chatMsg := extractChatMessage(art.Parts)
			switch art.Name {
			case stepmsg.ArtifactName:
				for _, p := range art.Parts {
					if st, ok := stepmsg.Decode(p); ok {
						rec.Steps = appendStepRecord(rec.Steps, StepRecord{Step: st, At: env.TS, seq: rec.EnvelopeCount})
					}
				}
				// A card task's own step record names the task whose turn created it.
				if parent := declaredCardParent(rec); parent != "" && parent != rec.CardParentTaskID && strings.HasPrefix(rec.TaskID, cardTaskIDPrefix) {
					rec.CardParentTaskID = parent
				}
			case lib.ArtifactThinking:
				if text != "" || !au.Append {
					rec.thinkingChunks = append(rec.thinkingChunks, timestampedChunk{
						ts:     env.TS,
						seq:    rec.EnvelopeCount,
						append: au.Append,
						text:   text,
					})
					rec.Thinking = foldTimestampedChunks(rec.thinkingChunks, "\n")
				}
			case lib.ArtifactActivity:
				for _, p := range art.Parts {
					raw := ""
					if p.Kind == partKindData && len(p.Data) > 0 {
						raw = string(p.Data)
					} else if p.Kind == partKindText && p.Text != "" {
						raw = p.Text
					}
					if item, ok := parseActivityItem(raw); ok {
						if item.At == "" && !env.TS.IsZero() {
							item.At = env.TS.UTC().Format(time.RFC3339Nano)
						}
						rec.Activities = upsertActivityItem(rec.Activities, item)
					}
				}
			case lib.ArtifactProgress:
				if text != "" || !au.Append {
					rec.progressChunks = append(rec.progressChunks, timestampedChunk{
						ts:     env.TS,
						seq:    rec.EnvelopeCount,
						append: au.Append,
						text:   text,
					})
					rec.ProgressText = foldTimestampedChunks(rec.progressChunks, "\n")
				}
				if chatMsg != nil && (rec.lastProgressTS.IsZero() || !env.TS.Before(rec.lastProgressTS)) {
					rec.ProgressChat = chatMsg
					rec.lastProgressTS = env.TS
				}
			case lib.ArtifactDelegate:
				var del DelegateItem
				if json.Unmarshal([]byte(dataText), &del) == nil && del.Addressee != "" {
					if del.ChildTaskID == "" {
						for _, other := range s.tasks {
							if other.DelegatedFromTaskID == rec.TaskID {
								alreadyLinked := false
								for _, existing := range rec.Delegates {
									if existing.ChildTaskID == other.TaskID {
										alreadyLinked = true
										break
									}
								}
								if !alreadyLinked {
									del.ChildTaskID = other.TaskID
									break
								}
							}
						}
					}
					dupDel := false
					for i := range rec.Delegates {
						if rec.Delegates[i].Addressee == del.Addressee && rec.Delegates[i].Text == del.Text {
							if rec.Delegates[i].ChildTaskID == "" && del.ChildTaskID != "" {
								rec.Delegates[i].ChildTaskID = del.ChildTaskID
							}
							if rec.Delegates[i].ChildTaskID == del.ChildTaskID || del.ChildTaskID == "" {
								dupDel = true
								break
							}
						}
					}
					if !dupDel {
						rec.Delegates = append(rec.Delegates, del)
					}
				}
			case lib.ArtifactNotice:
				if text != "" {
					isDupNotice := len(rec.Notices) > 0 && rec.Notices[len(rec.Notices)-1] == text
					if !isDupNotice {
						rec.Notices = append(rec.Notices, text)
					}
					prs := ExtractPRReferences(text)
					for _, pr := range prs {
						if !containsString(rec.PRLinks, pr) {
							rec.PRLinks = append(rec.PRLinks, pr)
						}
					}
					if !isDupNotice && isLive && s.bootstrapped && !watched && len(prs) > 0 {
						pushToFire = &PushEvent{
							ID:           fmt.Sprintf("notice-pr-%s-%d", rec.TaskID, len(rec.Notices)),
							Kind:         eventKindNoticePR,
							Deployment:   s.profile.Name,
							Conversation: rec.Conversation,
							TaskID:       rec.TaskID,
							ContextID:    rec.ContextID,
							Subject:      subject,
							Title:        fmt.Sprintf("Pull Request Notice on Task %s", rec.TaskID),
							Summary:      text,
							PRLinks:      prs,
							Timestamp:    env.TS,
						}
					}
				}
			case lib.ArtifactTurn:
				if text != "" {
					if len(rec.IntermediateTurns) == 0 || rec.IntermediateTurns[len(rec.IntermediateTurns)-1] != text {
						rec.IntermediateTurns = append(rec.IntermediateTurns, text)
					}
				}
			case lib.ArtifactResult:
				if text != "" || !au.Append {
					rec.resultChunks = append(rec.resultChunks, timestampedChunk{
						ts:     env.TS,
						seq:    rec.EnvelopeCount,
						append: au.Append,
						text:   text,
					})
					rec.ResultText = foldTimestampedChunks(rec.resultChunks, "")
				}
				if chatMsg != nil && (rec.lastResultTS.IsZero() || !env.TS.Before(rec.lastResultTS)) {
					rec.ResultChat = chatMsg
					rec.lastResultTS = env.TS
					for _, lnk := range chatMsg.Links {
						for _, pr := range ExtractPRReferences(lnk.URL) {
							if !containsString(rec.PRLinks, pr) {
								rec.PRLinks = append(rec.PRLinks, pr)
							}
						}
					}
				}
				for _, pr := range ExtractPRReferences(rec.ResultText) {
					if !containsString(rec.PRLinks, pr) {
						rec.PRLinks = append(rec.PRLinks, pr)
					}
				}
			}
		}
	}
	s.mu.Unlock()

	if pushToFire != nil && s.onPushEvent != nil {
		s.onPushEvent(*pushToFire)
	}
	if isLive {
		s.notifyStateChange()
	}
}

func (s *BusSession) isWatchedLocked(conv, taskID, parentTaskID string) bool {
	if conv != "" && s.watchedConvs[conv] > 0 {
		return true
	}
	if taskID != "" && s.watchedTasks[taskID] > 0 {
		return true
	}
	if parentTaskID != "" && s.watchedTasks[parentTaskID] > 0 {
		return true
	}
	// A card's parent may itself be a delegated child of the foreground task: walk one more hop.
	if parentTaskID != "" {
		if parent, ok := s.tasks[parentTaskID]; ok {
			if gp := firstNonEmptyStr(parent.DelegatedFromTaskID, parent.CardParentTaskID); gp != "" && s.watchedTasks[gp] > 0 {
				return true
			}
			if gp := firstNonEmptyStr(parent.DelegatedFromTaskID, parent.CardParentTaskID); gp != "" && s.foregroundTasks != nil {
				if exp, ok := s.foregroundTasks[gp]; ok && time.Now().Before(exp) {
					return true
				}
			}
		}
	}
	now := time.Now()
	if taskID != "" && s.foregroundTasks != nil {
		if exp, ok := s.foregroundTasks[taskID]; ok && now.Before(exp) {
			return true
		}
	}
	if parentTaskID != "" && s.foregroundTasks != nil {
		if exp, ok := s.foregroundTasks[parentTaskID]; ok && now.Before(exp) {
			return true
		}
	}
	return false
}

func (s *BusSession) markForegroundStepTask(taskID string, settled bool) {
	if taskID == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.foregroundTasks == nil {
		s.foregroundTasks = make(map[string]time.Time)
	}
	if settled {
		s.foregroundTasks[taskID] = time.Now().Add(24 * time.Hour)
	} else {
		s.foregroundTasks[taskID] = time.Now().Add(90 * time.Second)
	}
}

func (s *BusSession) ingestAgentCard(subject string, data []byte) {
	name := strings.TrimPrefix(subject, subjectAgentsPrefix)
	name = strings.TrimSuffix(name, subjectAgentsCardSuffix)
	name = strings.TrimSpace(name)
	if name == "" {
		return
	}
	var env lib.Envelope
	if err := json.Unmarshal(data, &env); err != nil {
		return
	}
	if env.Kind == lib.KindAgentClosed {
		s.mu.Lock()
		delete(s.agentCards, name)
		s.mu.Unlock()
		s.notifyStateChange()
		return
	}
	payload := env.Payload
	if len(payload) == 0 {
		payload = data
	}
	var card map[string]any
	if err := json.Unmarshal(payload, &card); err != nil || len(card) == 0 {
		return
	}
	s.mu.Lock()
	s.agentCards[name] = card
	s.mu.Unlock()
	s.notifyStateChange()
}

func (s *BusSession) ingestTopicEnvelope(subject string, data []byte) {
	var env lib.Envelope
	if err := json.Unmarshal(data, &env); err != nil {
		return
	}
	payload := env.Payload
	if len(payload) == 0 {
		payload = data
	}
	var art lib.Artifact
	var summary string
	var rawData json.RawMessage
	if err := json.Unmarshal(payload, &art); err == nil && (len(art.Parts) > 0 || art.Name != "") {
		for _, p := range art.Parts {
			switch p.Kind {
			case partKindText:
				summary = strings.TrimSpace(p.Text)
			case partKindData:
				rawData = p.Data
			}
		}
		if len(rawData) == 0 && len(art.Parts) == 0 {
			rawData = payload
		}
	} else {
		rawData = payload
		var generic map[string]any
		if json.Unmarshal(payload, &generic) == nil {
			for _, k := range []string{"summary", "headline", "text", "description"} {
				if v, ok := generic[k].(string); ok && strings.TrimSpace(v) != "" {
					summary = strings.TrimSpace(v)
					break
				}
			}
		}
	}
	stream := streamTopicsState
	class := topicClassState
	if strings.HasPrefix(subject, subjectTopicsJournalPrefix) || strings.HasSuffix(subject, ".annotations") {
		stream = streamTopicsJournal
		class = topicClassJournal
	}
	topicName := art.Name
	if topicName == "" {
		if _, _, parsedTopic, ok := lib.ParseTopicSubject(subject); ok {
			topicName = parsedTopic
		} else {
			parts := strings.Split(subject, ".")
			if len(parts) > 0 {
				topicName = parts[len(parts)-1]
			}
		}
	}
	snap := TopicSnapshot{
		Stream:        stream,
		Subject:       subject,
		Topic:         topicName,
		Class:         class,
		WriterSession: env.From.Session,
		WriterProfile: env.From.Profile,
		TaskID:        env.TaskID,
		CorrelationID: env.CorrelationID,
		Summary:       summary,
		Data:          rawData,
		UpdatedAt:     env.TS,
	}
	s.mu.Lock()
	if prov, ok := s.provisionedTopics[subject]; ok {
		if prov.Stream != "" {
			snap.Stream = prov.Stream
		}
		if prov.Class != "" {
			snap.Class = prov.Class
			class = prov.Class
		}
	}
	if existing, ok := s.topics[subject]; ok {
		if existing.Stream != "" {
			snap.Stream = existing.Stream
		}
		if existing.Class != "" {
			snap.Class = existing.Class
			class = existing.Class
		}
	}
	s.topics[subject] = snap
	if class == topicClassState && len(rawData) > 0 {
		s.enrichProfileFromTopicLocked(rawData)
	}
	s.mu.Unlock()
	s.notifyStateChange()
}

func (s *BusSession) enrichProfileFromTopicLocked(rawData json.RawMessage) {
	var m map[string]any
	if err := json.Unmarshal(rawData, &m); err != nil || len(m) == 0 {
		return
	}
	strVal := func(keys ...string) string {
		for _, k := range keys {
			if v, ok := m[k].(string); ok && strings.TrimSpace(v) != "" {
				return strings.TrimSpace(v)
			}
		}
		for _, subKey := range []string{"cluster", "target", "metadata", "gitops", "github"} {
			if sub, ok := m[subKey].(map[string]any); ok {
				for _, k := range keys {
					if v, ok := sub[k].(string); ok && strings.TrimSpace(v) != "" {
						return strings.TrimSpace(v)
					}
				}
			}
		}
		return ""
	}
	if s.profile.ClusterName == "" {
		s.profile.ClusterName = strVal("clusterName", "cluster_name", "cluster")
	}
	if s.profile.ProjectID == "" {
		s.profile.ProjectID = strVal("projectId", "project_id", "project")
	}
	if s.profile.Location == "" {
		s.profile.Location = strVal("location", "region", "zone")
	}
	if s.profile.GitHubRepo == "" {
		s.profile.GitHubRepo = strVal("githubRepo", "github_repo", "gitRepo", "git_repo", "repo")
	}
	if s.profile.Version == "" {
		s.profile.Version = strVal("version", "operatorVersion")
	}
}

func (s *BusSession) ingestConsoleOut(subject string, data []byte) {
	var frame ConsoleOutFrame
	if err := json.Unmarshal(data, &frame); err != nil {
		return
	}
	token := strings.TrimPrefix(subject, subjectConsoleOutPrefix)
	token = strings.TrimSuffix(token, subjectConsoleOutSuffix)
	frame.Token = token
	frame.Timestamp = time.Now().UTC()

	isEdit := false
	switch v := frame.Edit.(type) {
	case bool:
		isEdit = v
	case string:
		isEdit = strings.TrimSpace(v) != ""
	}

	s.mu.Lock()
	if isEdit && frame.MessageID != "" {
		updated := false
		for i := len(s.consoleNotices) - 1; i >= 0; i-- {
			if s.consoleNotices[i].Token == token && s.consoleNotices[i].MessageID == frame.MessageID {
				s.consoleNotices[i].Text = frame.Text
				s.consoleNotices[i].Timestamp = frame.Timestamp
				updated = true
				break
			}
		}
		if !updated {
			s.consoleNotices = append(s.consoleNotices, frame)
		}
	} else {
		s.consoleNotices = append(s.consoleNotices, frame)
	}
	if len(s.consoleNotices) > maxConsoleNoticesRetained {
		s.consoleNotices = s.consoleNotices[len(s.consoleNotices)-maxConsoleNoticesRetained:]
	}
	s.mu.Unlock()
	s.notifyStateChange()
}

func (s *BusSession) handlePassiveNotify(msg *nats.Msg) {
	var req lib.NotifyRequest
	if err := json.Unmarshal(msg.Data, &req); err != nil {
		return
	}
	title := defaultNotifyTitle
	summary := strings.TrimSpace(req.Text)
	var parsedChat *chatmsg.Message
	if len(req.Chat) > 0 {
		if cm, cErr := chatmsg.Parse(req.Chat); cErr == nil {
			parsedChat = &cm
		}
	}
	if parsedChat != nil {
		if parsedChat.Headline != "" {
			title = parsedChat.Headline
		}
		if summary == "" {
			summary = parsedChat.Detail
		}
	}
	if summary == "" {
		summary = title
	}
	prs := ExtractPRReferences(summary)
	if parsedChat != nil {
		for _, lnk := range parsedChat.Links {
			for _, pr := range ExtractPRReferences(lnk.URL) {
				if !containsString(prs, pr) {
					prs = append(prs, pr)
				}
			}
		}
	}
	if s.onPushEvent != nil {
		s.onPushEvent(PushEvent{
			ID:           fmt.Sprintf("notify-%d", time.Now().UnixNano()),
			Kind:         eventKindProactiveNotify,
			Deployment:   s.profile.Name,
			Conversation: req.Conversation,
			ContextID:    req.ContextID,
			Subject:      msg.Subject,
			Title:        title,
			Summary:      summary,
			PRLinks:      prs,
			Raw:          append(json.RawMessage(nil), msg.Data...),
			Timestamp:    time.Now().UTC(),
		})
	}
	s.notifyStateChange()
}

func extractTaskAddressee(subject string) string {
	trimmed := strings.TrimPrefix(subject, subjectTasksPrefix)
	parts := strings.Split(trimmed, ".")
	if len(parts) >= 2 {
		return parts[0]
	}
	return ""
}

func extractWakeChildTaskID(prompt string) string {
	// Fallback only: the authoritative wake link is Authority.Via (see isWakeAuthority); this
	// reads the gateway's templated wake header when an envelope carried no authority block.
	if strings.HasPrefix(prompt, wakePromptPrefix) {
		rest := strings.TrimPrefix(prompt, wakePromptPrefix)
		fields := strings.Fields(rest)
		if len(fields) > 0 {
			return strings.TrimRight(fields[0], "]")
		}
		return ""
	}
	for _, marker := range []string{"The task you delegated to ", "You delegated to "} {
		if mIdx := strings.Index(prompt, marker); mIdx >= 0 {
			afterMarker := prompt[mIdx+len(marker):]
			if idx := strings.Index(afterMarker, "(task "); idx >= 0 && idx < 80 {
				rest := afterMarker[idx+len("(task "):]
				if end := strings.IndexByte(rest, ')'); end > 0 {
					return strings.TrimSpace(rest[:end])
				}
			}
		}
	}
	return ""
}

func joinMessageText(parts []lib.Part) string {
	var chunks []string
	for _, p := range parts {
		if p.Kind == partKindText && p.Text != "" {
			chunks = append(chunks, p.Text)
		}
	}
	return strings.Join(chunks, "\n")
}

func joinDataOrTextParts(parts []lib.Part) string {
	for _, p := range parts {
		if p.Kind == partKindData && len(p.Data) > 0 {
			return string(p.Data)
		}
		if p.Kind == partKindText && p.Text != "" {
			return p.Text
		}
	}
	return ""
}

func extractChatMessage(parts []lib.Part) *chatmsg.Message {
	for _, p := range parts {
		if p.Kind == partKindData && len(p.Data) > 0 {
			if msg, err := chatmsg.Parse(p.Data); err == nil && msg.Schema != "" {
				return &msg
			}
		}
	}
	return nil
}

func parseActivityItem(raw string) (ActivityItem, bool) {
	var wire struct {
		Tool            string          `json:"tool"`
		Input           json.RawMessage `json:"input"`
		CallID          string          `json:"callId"`
		CallIDSnake     string          `json:"call_id"`
		Status          string          `json:"status"`
		ErrorType       string          `json:"errorType"`
		ErrTypeSnake    string          `json:"error_type"`
		ErrorMessage    string          `json:"errorMessage"`
		ErrMsgSnake     string          `json:"error_message"`
		ResultPreview   string          `json:"resultPreview"`
		ResPreviewSnake string          `json:"result_preview"`
		SessionID       string          `json:"sessionId"`
		SessionIDSnake  string          `json:"session_id"`
		Worker          string          `json:"worker"`
		Title           string          `json:"title"`
		CardID          string          `json:"cardId"`
		CardIDSnake     string          `json:"card_id"`
		DurationMs      int64           `json:"durationMs"`
		DurMsSnake      int64           `json:"duration_ms"`
		At              string          `json:"at"`
	}
	if err := json.Unmarshal([]byte(raw), &wire); err != nil || wire.Tool == "" {
		return ActivityItem{}, false
	}
	inputStr := ""
	if len(wire.Input) > 0 {
		var s string
		if json.Unmarshal(wire.Input, &s) == nil {
			inputStr = s
		} else {
			inputStr = strings.TrimSpace(string(wire.Input))
			if inputStr == jsonNullLiteral {
				inputStr = ""
			}
			inputStr = strings.ReplaceAll(inputStr, escapedUnicodeLt, literalLt)
			inputStr = strings.ReplaceAll(inputStr, escapedUnicodeGt, literalGt)
			inputStr = strings.ReplaceAll(inputStr, escapedUnicodeAmp, literalAmp)
		}
	}
	dur := wire.DurationMs
	if dur == 0 {
		dur = wire.DurMsSnake
	}
	return ActivityItem{
		Tool:          wire.Tool,
		Input:         inputStr,
		CallID:        firstNonEmptyStr(wire.CallID, wire.CallIDSnake),
		Status:        wire.Status,
		ErrorType:     firstNonEmptyStr(wire.ErrorType, wire.ErrTypeSnake),
		ErrorMessage:  firstNonEmptyStr(wire.ErrorMessage, wire.ErrMsgSnake),
		ResultPreview: firstNonEmptyStr(wire.ResultPreview, wire.ResPreviewSnake),
		SessionID:     firstNonEmptyStr(wire.SessionID, wire.SessionIDSnake),
		Worker:        wire.Worker,
		Title:         strings.TrimSpace(wire.Title),
		CardID:        strings.TrimSpace(firstNonEmptyStr(wire.CardID, wire.CardIDSnake)),
		DurationMs:    dur,
		At:            wire.At,
	}, true
}

// appendStepRecord adds a folded step record, keeping the list in envelope-time order when a
// replay delivers history out of order.
func appendStepRecord(list []StepRecord, st StepRecord) []StepRecord {
	list = append(list, st)
	for i := len(list) - 1; i > 0; i-- {
		prev, cur := list[i-1], list[i]
		if prev.At.IsZero() || cur.At.IsZero() || !cur.At.Before(prev.At) {
			break
		}
		list[i-1], list[i] = cur, prev
	}
	return list
}

func parseActivityTimestamp(at string) (time.Time, bool) {
	at = strings.TrimSpace(at)
	if at == "" {
		return time.Time{}, false
	}
	if t, err := time.Parse(time.RFC3339Nano, at); err == nil {
		return t, true
	}
	if t, err := time.Parse(time.RFC3339, at); err == nil {
		return t, true
	}
	return time.Time{}, false
}

func upsertActivityItem(list []ActivityItem, item ActivityItem) []ActivityItem {
	replaced := false
	if item.CallID != "" {
		for i := range list {
			if list[i].CallID == item.CallID {
				item.Index = i + 1
				list[i] = item
				replaced = true
				break
			}
		}
	}
	if !replaced {
		item.Index = len(list) + 1
		list = append(list, item)
	}
	if len(list) > 1 {
		needSort := false
		for i := 1; i < len(list); i++ {
			ti, okI := parseActivityTimestamp(list[i].At)
			tj, okJ := parseActivityTimestamp(list[i-1].At)
			if okI && okJ && ti.Before(tj) {
				needSort = true
				break
			}
		}
		if needSort {
			sort.SliceStable(list, func(i, j int) bool {
				ti, okI := parseActivityTimestamp(list[i].At)
				tj, okJ := parseActivityTimestamp(list[j].At)
				if okI && okJ && !ti.Equal(tj) {
					return ti.Before(tj)
				}
				return false
			})
		}
	}
	labelActivityWorkers(list)
	return list
}

func isWorkerSessionID(sessionID string) bool {
	return sessionID != "" && !strings.HasPrefix(sessionID, apiSessionIDPrefix)
}

// labelActivityWorkers numbers the entries and gives each one the executor left unlabeled a
// worker label from the facts it carries: the card it ran for, else its session.
func labelActivityWorkers(list []ActivityItem) {
	for i := range list {
		list[i].Index = i + 1
		act := &list[i]
		if act.Worker != "" {
			continue
		}
		switch {
		case isWorkerSessionID(act.SessionID) && act.CardID != "":
			act.Worker = "card:" + act.CardID
		case isWorkerSessionID(act.SessionID):
			act.Worker = "worker:" + truncateText(act.SessionID, 20)
		case act.SessionID != "":
			act.Worker = "platform"
		}
	}
}

// isWakeAuthority tells a session wake from a delegated child by the shape of its authority
// block. Both carry Via; the gateway addresses a delegated child to the platform agent (Via
// names the delegating session turn) and a wake to a session worker (Via names the finished
// child). Kanban card tasks are never wakes.
func isWakeAuthority(auth authorityWire, rec *TaskRecord) bool {
	if auth.Via == nil || rec == nil || strings.HasPrefix(rec.TaskID, cardTaskIDPrefix) {
		return false
	}
	addr := strings.TrimSpace(rec.Addressee)
	return addr != "" && addr != swarmMemberPlatform && addr != swarmMemberGateway
}

func isPureControlCommand(text string) bool {
	lower := strings.ToLower(strings.TrimSpace(text))
	return lower == cmdSessionOn || lower == cmdSessionOff || lower == cmdStatus || lower == cmdStop || lower == cmdHelp
}

func firstNonEmptyStr(vals ...string) string {
	for _, v := range vals {
		if s := strings.TrimSpace(v); s != "" {
			return s
		}
	}
	return ""
}

func truncateText(s string, maxLen int) string {
	s = strings.TrimSpace(s)
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen] + ellipsisSuffix
}

func containsString(slice []string, target string) bool {
	for _, s := range slice {
		if s == target {
			return true
		}
	}
	return false
}
