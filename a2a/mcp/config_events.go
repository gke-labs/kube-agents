package mcp

import (
	"encoding/json"
	"log/slog"
	"os"
	"strings"
	"time"
)

// PushEvent represents an asynchronous message, notification, or task transition
// received from the kube-agents bus.
type PushEvent struct {
	ID           string          `json:"id"`
	Kind         string          `json:"kind"`
	Deployment   string          `json:"deployment,omitempty"`
	Conversation string          `json:"conversation,omitempty"`
	TaskID       string          `json:"taskId,omitempty"`
	ContextID    string          `json:"contextId,omitempty"`
	Subject      string          `json:"subject,omitempty"`
	Title        string          `json:"title"`
	Summary      string          `json:"summary"`
	Details      string          `json:"details,omitempty"`
	PRLinks      []string        `json:"prLinks,omitempty"`
	Raw          json.RawMessage `json:"raw,omitempty"`
	Timestamp    time.Time       `json:"timestamp"`
	Delivered    bool            `json:"delivered"`
}

// UpdatePushConfig updates the bi-directional Antigravity push configuration.
func (s *ConfigStore) UpdatePushConfig(agyConvID string, enabled *bool, agentAPIPath string) (PushConfig, error) {
	id := strings.TrimSpace(agyConvID)
	return s.UpdatePushConfigWithLineage(ConversationLineage{
		ConversationID:     id,
		RootConversationID: id,
	}, enabled, agentAPIPath)
}

// UpdatePushConfigWithLineage updates the bi-directional Antigravity push configuration while
// preserving the root coordinator conversation as the default cluster-wide push target when
// called from a child subagent.
func (s *ConfigStore) UpdatePushConfigWithLineage(lineage ConversationLineage, enabled *bool, agentAPIPath string) (PushConfig, error) {
	defer s.lockForUpdate()()
	trimmedID := strings.TrimSpace(lineage.ConversationID)
	rootID := firstNonEmptyStr(strings.TrimSpace(lineage.RootConversationID), trimmedID)
	wasUnbound := s.cfg.Push.AGYConversationID == ""
	before := s.cfg.Push
	beforeKnown := len(before.KnownAGYConversations)
	activeBound := ""
	if active, ok := s.cfg.Conversations[s.cfg.ActiveConversation]; ok {
		activeBound = active.AGYConversationID
	}
	if trimmedID != "" {
		s.addKnownAGYConvLocked(trimmedID)
		if rootID != "" && rootID != trimmedID {
			s.addKnownAGYConvLocked(rootID)
		}
		if !lineage.IsSubagent {
			s.cfg.Push.AGYConversationID = trimmedID
			// Only adopt the active console conversation if nobody owns it yet.
			if s.cfg.ActiveConversation != "" {
				if active, ok := s.cfg.Conversations[s.cfg.ActiveConversation]; ok && active.AGYConversationID == "" {
					active.AGYConversationID = trimmedID
					s.cfg.Conversations[s.cfg.ActiveConversation] = active
				}
			}
		} else if s.cfg.Push.AGYConversationID == "" && rootID != "" {
			s.cfg.Push.AGYConversationID = rootID
		}
	}
	if enabled != nil {
		s.cfg.Push.Enabled = *enabled
	} else if wasUnbound && (s.cfg.Push.AGYConversationID != "" || trimmedID != "") {
		s.cfg.Push.Enabled = true
	}
	if strings.TrimSpace(agentAPIPath) != "" {
		s.cfg.Push.AgentAPIPath = strings.TrimSpace(agentAPIPath)
	}
	afterActiveBound := ""
	if active, ok := s.cfg.Conversations[s.cfg.ActiveConversation]; ok {
		afterActiveBound = active.AGYConversationID
	}
	// This runs on nearly every tool call from the coordinator; persist (and fsync) only when
	// the effective configuration actually moved.
	unchanged := s.cfg.Push.Enabled == before.Enabled &&
		s.cfg.Push.AGYConversationID == before.AGYConversationID &&
		s.cfg.Push.AgentAPIPath == before.AgentAPIPath &&
		len(s.cfg.Push.KnownAGYConversations) == beforeKnown &&
		afterActiveBound == activeBound
	if unchanged && !before.UpdatedAt.IsZero() {
		return s.cfg.Push, nil
	}
	s.cfg.Push.UpdatedAt = time.Now().UTC()
	if s.cfg.Push.AGYConversationID != "" && s.convPath != "" {
		stateData, err := json.MarshalIndent(ActiveConversationState{
			AGYConversationID: s.cfg.Push.AGYConversationID,
			UpdatedAt:         s.cfg.Push.UpdatedAt,
		}, "", "  ")
		if err == nil {
			_ = writeFileAtomicIfChanged(s.convPath, stateData)
		}
	}
	return s.cfg.Push, s.saveLocked()
}

func (s *ConfigStore) addKnownAGYConvLocked(id string) {
	if id == "" {
		return
	}
	for _, existing := range s.cfg.Push.KnownAGYConversations {
		if existing == id {
			return
		}
	}
	s.cfg.Push.KnownAGYConversations = append(s.cfg.Push.KnownAGYConversations, id)
}

// IsKnownAGYConversation returns true if agyConvID has been registered via an MCP tool call or conversation binding.
func (s *ConfigStore) IsKnownAGYConversation(agyConvID string) bool {
	trimmed := strings.TrimSpace(agyConvID)
	if trimmed == "" {
		return false
	}
	defer s.lockForRead()()
	if s.cfg.Push.AGYConversationID == trimmed {
		return true
	}
	for _, id := range s.cfg.Push.KnownAGYConversations {
		if id == trimmed {
			return true
		}
	}
	for _, entry := range s.cfg.Conversations {
		if entry.AGYConversationID == trimmed {
			return true
		}
	}
	return false
}

// OwnsConsoleConversation reports whether consoleConv (e.g. "console:agy-abc")
// belongs to a conversation registered in this ConfigStore. Non-console or empty
// conversation strings (e.g. "", "cron", proactive notifications) return true so
// cluster-wide background events are still delivered.
func (s *ConfigStore) OwnsConsoleConversation(consoleConv string) bool {
	defer s.lockForRead()()
	return s.ownsConsoleConversationLocked(consoleConv)
}

func (s *ConfigStore) ownsConsoleConversationLocked(consoleConv string) bool {
	trimmed := strings.TrimSpace(consoleConv)
	if !strings.HasPrefix(trimmed, "console:") {
		return true
	}
	token := strings.TrimPrefix(trimmed, "console:")
	if token == "" {
		return true
	}
	for alias, entry := range s.cfg.Conversations {
		if alias == token || entry.Token == token {
			return true
		}
	}
	return false
}

func (s *ConfigStore) eventMatchesAGYConvLocked(ev PushEvent, agyConvID string) bool {
	if !s.ownsConsoleConversationLocked(ev.Conversation) {
		return false
	}
	callerID := strings.TrimSpace(agyConvID)
	if callerID == "" {
		return true
	}
	trimmedConv := strings.TrimSpace(ev.Conversation)
	if strings.HasPrefix(trimmedConv, "console:") {
		token := strings.TrimPrefix(trimmedConv, "console:")
		for alias, entry := range s.cfg.Conversations {
			if (alias == token || entry.Token == token) && strings.TrimSpace(entry.AGYConversationID) != "" {
				return strings.TrimSpace(entry.AGYConversationID) == callerID
			}
		}
	}
	activeID := strings.TrimSpace(s.cfg.Push.AGYConversationID)
	return activeID == "" || activeID == callerID
}

// ResolveAGYConversationIDForEvent returns the Antigravity conversation ID bound to a specific console conversation
// (e.g. "console:agy-ba15db52c803"), falling back to ResolveAGYConversationID().
func (s *ConfigStore) ResolveAGYConversationIDForEvent(consoleConv string) string {
	token := strings.TrimPrefix(strings.TrimSpace(consoleConv), "console:")
	if token != "" {
		unlock := s.lockForRead()
		for _, entry := range s.cfg.Conversations {
			if (entry.Token == token || entry.Alias == token) && strings.TrimSpace(entry.AGYConversationID) != "" {
				id := strings.TrimSpace(entry.AGYConversationID)
				unlock()
				return id
			}
		}
		unlock()
	}
	return s.ResolveAGYConversationID()
}

// ResolveAGYConversationID returns the best known active Antigravity conversation ID,
// checking the on-disk active_conversation.json file (updated by hooks), the config store,
// and ANTIGRAVITY_CONVERSATION_ID environment variable.
func (s *ConfigStore) ResolveAGYConversationID() string {
	unlock := s.lockForRead()
	convPath := s.convPath
	cfgID := s.cfg.Push.AGYConversationID
	unlock()

	if raw, err := os.ReadFile(convPath); err == nil && len(raw) > 0 {
		var st ActiveConversationState
		if json.Unmarshal(raw, &st) == nil && strings.TrimSpace(st.AGYConversationID) != "" {
			return strings.TrimSpace(st.AGYConversationID)
		}
	}
	if cfgID != "" {
		return cfgID
	}
	return strings.TrimSpace(os.Getenv(envAGYConversationID))
}

// AppendPendingEvents appends new PushEvents to the on-disk pending-events.json queue.
func (s *ConfigStore) AppendPendingEvents(events ...PushEvent) error {
	if len(events) == 0 {
		return nil
	}
	defer s.lockForUpdate()()
	existing := s.readPendingEventsLocked()
	seen := make(map[string]bool, len(existing))
	for _, e := range existing {
		seen[e.ID] = true
	}
	for _, e := range events {
		if e.ID != "" && seen[e.ID] {
			continue
		}
		existing = append(existing, e)
		if e.ID != "" {
			seen[e.ID] = true
		}
	}
	if len(existing) > maxStoredEvents {
		existing = existing[len(existing)-maxStoredEvents:]
	}
	return s.writePendingEventsLocked(existing)
}

// DrainPendingEvents returns all undelivered PushEvents and marks them delivered
// (or removes them from the pending file).
func (s *ConfigStore) DrainPendingEvents() ([]PushEvent, error) {
	return s.DrainPendingEventsForAGY("")
}

// DrainPendingEventsForAGY drains undelivered PushEvents matching agyConvID (or all
// undelivered events when agyConvID is empty), keeping events destined for other local
// Antigravity conversations queued in pending-events.json.
func (s *ConfigStore) DrainPendingEventsForAGY(agyConvID string) ([]PushEvent, error) {
	defer s.lockForUpdate()()
	existing := s.readPendingEventsLocked()
	var undelivered []PushEvent
	var remaining []PushEvent
	for _, e := range existing {
		if e.Delivered || !s.ownsConsoleConversationLocked(e.Conversation) {
			continue
		}
		if s.eventMatchesAGYConvLocked(e, agyConvID) {
			undelivered = append(undelivered, e)
		} else {
			remaining = append(remaining, e)
		}
	}
	if len(undelivered) == 0 && len(remaining) == len(existing) {
		return nil, nil
	}
	if remaining == nil {
		remaining = []PushEvent{}
	}
	if err := s.writePendingEventsLocked(remaining); err != nil {
		return undelivered, err
	}
	return undelivered, nil
}

// DrainPendingEventsByID removes exactly the events with the given IDs from the pending queue.
// Delivery paths that snapshot a batch, spend time sending it, and only then acknowledge it
// must use this rather than a conversation-wide drain, so events that arrived during the send
// stay queued for the next delivery instead of being acknowledged unseen.
func (s *ConfigStore) DrainPendingEventsByID(ids []string) error {
	if len(ids) == 0 {
		return nil
	}
	want := make(map[string]bool, len(ids))
	for _, id := range ids {
		if id != "" {
			want[id] = true
		}
	}
	defer s.lockForUpdate()()
	existing := s.readPendingEventsLocked()
	remaining := make([]PushEvent, 0, len(existing))
	for _, e := range existing {
		if e.ID != "" && want[e.ID] {
			continue
		}
		remaining = append(remaining, e)
	}
	if len(remaining) == len(existing) {
		return nil
	}
	return s.writePendingEventsLocked(remaining)
}

// PeekPendingEvents returns all undelivered PushEvents without draining them.
func (s *ConfigStore) PeekPendingEvents() []PushEvent {
	return s.PeekPendingEventsForAGY("")
}

// PeekPendingEventsForAGY returns all undelivered PushEvents matching agyConvID
// (or all undelivered events when agyConvID is empty) without draining them.
func (s *ConfigStore) PeekPendingEventsForAGY(agyConvID string) []PushEvent {
	defer s.lockForRead()()
	existing := s.readPendingEventsLocked()
	var out []PushEvent
	for _, e := range existing {
		if !e.Delivered && s.eventMatchesAGYConvLocked(e, agyConvID) {
			out = append(out, e)
		}
	}
	return out
}

func (s *ConfigStore) readPendingEventsLocked() []PushEvent {
	if s.eventsPath == "" {
		return append([]PushEvent(nil), s.memEvents...)
	}
	raw, err := os.ReadFile(s.eventsPath)
	if err != nil || len(raw) == 0 {
		return nil
	}
	var list []PushEvent
	if err := json.Unmarshal(raw, &list); err != nil {
		return nil
	}
	return list
}

func (s *ConfigStore) writePendingEventsLocked(list []PushEvent) error {
	if s.eventsPath == "" {
		s.memEvents = append([]PushEvent(nil), list...)
		return nil
	}
	data, err := json.MarshalIndent(list, "", "  ")
	if err != nil {
		return err
	}
	if err := writeFileAtomic(s.eventsPath, data); err != nil {
		slog.Warn("kube-agents-mcp: pending push events save failed", "path", s.eventsPath, "err", err)
		return err
	}
	return nil
}
