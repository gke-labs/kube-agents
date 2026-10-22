package mcp

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	configDirRelPath        = ".config/kube-agents"
	configFileName          = "mcp-config.json"
	pendingEventsFileName   = "pending-events.json"
	stepProgressFileName    = "step-progress.json"
	activeConvFileName      = "active_conversation.json"
	prCacheDirRelPath       = ".cache/kube-agents/prs"
	dirPermMode             = 0o700
	filePermMode            = 0o600
	defaultConvAlias        = "default"
	defaultConvTokenPrefix  = "agy-"
	randomTokenBytes        = 6
	lastUsedCoalesceWindow  = 5 * time.Second
	envConfigPath           = "KUBE_AGENTS_MCP_CONFIG"
	envAGYConversationID    = "ANTIGRAVITY_CONVERSATION_ID"
	maxStoredEvents         = 200
	maxConsoleTokenLen      = 64
	tempFileSuffix          = ".tmp"
	lockFileSuffix          = ".lock"
	defaultConvDescription  = "Default Antigravity conversation"
	agyAliasPrefix          = "agy-"
	agyAliasIDChars         = 8
	subagentTypeKubeSession = "kube-session"
)

// ConversationLineage describes a caller conversation's position in a parent/subagent hierarchy.
type ConversationLineage struct {
	ConversationID       string `json:"conversationId"`
	ParentConversationID string `json:"parentConversationId,omitempty"`
	RootConversationID   string `json:"rootConversationId"`
	SubagentType         string `json:"subagentType,omitempty"`
	SubagentRole         string `json:"subagentRole,omitempty"`
	SubagentState        string `json:"subagentState,omitempty"`
	IsSubagent           bool   `json:"isSubagent"`
}

// IsSessionSubagent reports whether this lineage descriptor represents a dedicated
// Kube-Agents /session subagent, i.e. one invoked with the kube-session type.
func (l ConversationLineage) IsSessionSubagent() bool {
	if !l.IsSubagent {
		return false
	}
	return strings.EqualFold(strings.TrimSpace(l.SubagentType), subagentTypeKubeSession)
}

// IsKilled reports whether the subagent is in SUBAGENT_STATE_KILLED.
func (l ConversationLineage) IsKilled() bool {
	return strings.EqualFold(strings.TrimSpace(l.SubagentState), "SUBAGENT_STATE_KILLED")
}

// isSessionSubagentEntry reports whether a persisted ConversationEntry represents a
// dedicated /session subagent rather than a root coordinator conversation.
func isSessionSubagentEntry(c ConversationEntry) bool {
	if strings.TrimSpace(c.ParentAGYConversationID) != "" &&
		strings.TrimSpace(c.ParentAGYConversationID) != strings.TrimSpace(c.AGYConversationID) {
		return true
	}
	return strings.TrimSpace(c.SubagentRole) != ""
}

var (
	errDeploymentNotFound = errors.New("deployment profile not found")
	errNoDeploymentsSaved = errors.New("no deployments configured or discovered")
)

// SessionPodInfo describes a live a2a-session worker pod discovered in Kubernetes.
type SessionPodInfo struct {
	Name       string    `json:"name"`
	Addressee  string    `json:"addressee,omitempty"`
	SessionKey string    `json:"sessionKey,omitempty"`
	TaskID     string    `json:"taskId,omitempty"`
	Phase      string    `json:"phase,omitempty"`
	CreatedAt  time.Time `json:"createdAt,omitempty"`
}

// DeploymentProfile stores connection and cluster metadata for a discovered
// or manually registered kube-agents deployment.
type DeploymentProfile struct {
	Name                string           `json:"name"`
	KubeContext         string           `json:"kubeContext,omitempty"`
	Namespace           string           `json:"namespace"`
	PlatformAgentName   string           `json:"platformAgentName"`
	Mode                string           `json:"mode,omitempty"`
	Version             string           `json:"version,omitempty"`
	ProjectID           string           `json:"projectId,omitempty"`
	ClusterName         string           `json:"clusterName,omitempty"`
	Location            string           `json:"location,omitempty"`
	GitHubRepo          string           `json:"githubRepo,omitempty"`
	ChatPlatform        string           `json:"chatPlatform,omitempty"`
	NATSService         string           `json:"natsService,omitempty"`
	NATSPort            int              `json:"natsPort,omitempty"`
	DirectNATSURL       string           `json:"directNatsUrl,omitempty"`
	CredsObjectName     string           `json:"credsObjectName,omitempty"`
	BusUser             string           `json:"busUser,omitempty"`
	BusAuthValue        string           `json:"busAuthValue,omitempty"`
	ObserverAuthValue   string           `json:"observerAuthValue,omitempty"`
	Ready               bool             `json:"ready"`
	StatusMessage       string           `json:"statusMessage,omitempty"`
	AgentProfiles       []string         `json:"agentProfiles,omitempty"`
	ActiveSessionPods   []SessionPodInfo `json:"activeSessionPods,omitempty"`
	SessionPodsProbedAt time.Time        `json:"sessionPodsProbedAt,omitempty"`
	LastConnectedAt     time.Time        `json:"lastConnectedAt,omitempty"`
	LastDiscoveredAt    time.Time        `json:"lastDiscoveredAt,omitempty"`
}

// ConversationEntry tracks a named console conversation token and its mode.
type ConversationEntry struct {
	Alias             string `json:"alias"`
	Token             string `json:"token"`
	SessionMode       bool   `json:"sessionMode"`
	SessionTerminated bool   `json:"sessionTerminated,omitempty"`
	Description       string `json:"description,omitempty"`
	AGYConversationID string `json:"agyConversationId,omitempty"`
	// ParentAGYConversationID links a subagent-owned conversation to its root coordinator
	// Antigravity conversation so both project onto the same unified Swarm board.
	ParentAGYConversationID string `json:"parentAgyConversationId,omitempty"`
	// SubagentRole stores the human-readable role from the Antigravity subagent descriptor.
	SubagentRole string `json:"subagentRole,omitempty"`
	// SessionWorkerID caches the remote s-<id> worker addressee observed on TASKS for this conversation.
	SessionWorkerID string `json:"sessionWorkerId,omitempty"`
	// AutoBound marks a conversation the server minted on its own to back an Antigravity conversation
	// (as opposed to one a user or agent created by name), so housekeeping can expire it more
	// aggressively without inferring that from how its alias happens to be spelled.
	AutoBound bool `json:"autoBound,omitempty"`
	// DebugMode scopes kube-agents debug output (tool tables, timings, ids) to this conversation,
	// so one user turning it on does not change what every other concurrent conversation sees.
	DebugMode  bool      `json:"debugMode,omitempty"`
	CreatedAt  time.Time `json:"createdAt"`
	LastUsedAt time.Time `json:"lastUsedAt"`
}

// PushConfig configures bi-directional event push into Antigravity.
type PushConfig struct {
	Enabled               bool      `json:"enabled"`
	AGYConversationID     string    `json:"agyConversationId,omitempty"`
	KnownAGYConversations []string  `json:"knownAgyConversations,omitempty"`
	AgentAPIPath          string    `json:"agentApiPath,omitempty"`
	UpdatedAt             time.Time `json:"updatedAt,omitempty"`
}

// PersistentConfig is the on-disk schema stored in ~/.config/kube-agents/mcp-config.json.
type PersistentConfig struct {
	ActiveDeployment   string                       `json:"activeDeployment,omitempty"`
	ActiveConversation string                       `json:"activeConversation,omitempty"`
	DebugMode          bool                         `json:"debugMode,omitempty"`
	Deployments        map[string]DeploymentProfile `json:"deployments"`
	Conversations      map[string]ConversationEntry `json:"conversations"`
	Push               PushConfig                   `json:"push"`
}

// ActiveConversationState is written by Antigravity hooks or MCP tools to
// ~/.config/kube-agents/active_conversation.json so the MCP server knows
// which Antigravity conversation ID to wake via agentapi.
type ActiveConversationState struct {
	AGYConversationID string    `json:"agyConversationId"`
	UpdatedAt         time.Time `json:"updatedAt"`
}

// ConfigStore manages thread-safe access and atomic persistence for
// PersistentConfig and pending push events.
// ConfigStore is the on-disk state every kube-agents MCP process on this machine shares:
// deployments and their cached bus credentials, console conversations and their Antigravity
// bindings, push settings, and the pending push-event queue. Several processes read and
// write it at once (one MCP server per Antigravity conversation, plus each `--hook` run), so every
// method re-reads the file when another process changed it, and every mutation holds an
// exclusive file lock across its read-modify-write; saves are atomic renames.
type ConfigStore struct {
	mu         sync.Mutex
	path       string
	eventsPath string
	convPath   string
	lockPath   string
	cfg        PersistentConfig
	memEvents  []PushEvent
	loaded     fileStamp
}

// fileStamp identifies one on-disk version of the config file.
type fileStamp struct {
	modTime time.Time
	size    int64
}

func stampOf(path string) fileStamp {
	info, err := os.Stat(path)
	if err != nil {
		return fileStamp{}
	}
	return fileStamp{modTime: info.ModTime(), size: info.Size()}
}

// NewMemoryConfigStore initializes a purely in-memory ConfigStore with no filesystem writes
// (suitable for read-only container root filesystems such as distroless a2a-gateway pods).
func NewMemoryConfigStore() *ConfigStore {
	now := time.Now().UTC()
	s := &ConfigStore{
		cfg: PersistentConfig{
			Deployments: make(map[string]DeploymentProfile),
			Conversations: map[string]ConversationEntry{
				defaultConvAlias: {
					Alias:       defaultConvAlias,
					Token:       mintConversationToken(),
					Description: defaultConvDescription,
					CreatedAt:   now,
					LastUsedAt:  now,
				},
			},
			ActiveConversation: defaultConvAlias,
		},
	}
	return s
}

// NewConfigStore loads or initializes a ConfigStore at configPath.
// If configPath is empty, it uses KUBE_AGENTS_MCP_CONFIG or ~/.config/kube-agents/mcp-config.json.
func NewConfigStore(configPath string) (*ConfigStore, error) {
	if configPath == "" {
		configPath = os.Getenv(envConfigPath)
	}
	if configPath == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return nil, fmt.Errorf("resolving user home dir: %w", err)
		}
		configPath = filepath.Join(home, configDirRelPath, configFileName)
	}
	baseDir := filepath.Dir(configPath)
	if err := os.MkdirAll(baseDir, dirPermMode); err != nil {
		return nil, fmt.Errorf("creating config dir %s: %w", baseDir, err)
	}
	s := &ConfigStore{
		path:       configPath,
		eventsPath: filepath.Join(baseDir, pendingEventsFileName),
		convPath:   filepath.Join(baseDir, activeConvFileName),
		lockPath:   configPath + lockFileSuffix,
	}
	unlock, lockErr := lockFile(s.lockPath)
	if lockErr == nil {
		defer unlock()
	}
	if err := s.loadLocked(); err != nil {
		return nil, err
	}
	return s, nil
}

// lockForUpdate serializes a read-modify-write of the store against the other processes
// sharing it and against this process's other goroutines, and brings the in-memory state up
// to date first. The returned func releases both locks.
func (s *ConfigStore) lockForUpdate() func() {
	s.mu.Lock()
	if s.lockPath == "" {
		return s.mu.Unlock
	}
	unlock, err := lockFile(s.lockPath)
	s.refreshLocked()
	return func() {
		if err == nil {
			unlock()
		}
		s.mu.Unlock()
	}
}

// lockForRead takes the in-process lock and brings the in-memory state up to date; a read
// needs no file lock because saves are atomic renames.
func (s *ConfigStore) lockForRead() func() {
	s.mu.Lock()
	if s.path != "" {
		s.refreshLocked()
	}
	return s.mu.Unlock
}

// refreshLocked re-reads the config file when another process has written it since this
// process last loaded or saved it. A missing or unreadable file leaves the in-memory state
// as is; the next save rewrites it.
func (s *ConfigStore) refreshLocked() {
	if s.path == "" {
		return
	}
	stamp := stampOf(s.path)
	if stamp == s.loaded || stamp == (fileStamp{}) {
		return
	}
	raw, err := os.ReadFile(s.path)
	if err != nil || len(raw) == 0 {
		return
	}
	var cfg PersistentConfig
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return
	}
	if cfg.Deployments == nil {
		cfg.Deployments = make(map[string]DeploymentProfile)
	}
	if cfg.Conversations == nil {
		cfg.Conversations = make(map[string]ConversationEntry)
	}
	s.cfg = cfg
	s.loaded = stamp
}

func (s *ConfigStore) loadLocked() error {
	s.loaded = stampOf(s.path)
	raw, err := os.ReadFile(s.path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("reading config %s: %w", s.path, err)
	}
	dirty := errors.Is(err, os.ErrNotExist) || len(raw) == 0
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &s.cfg); err != nil {
			return fmt.Errorf("parsing config %s: %w", s.path, err)
		}
	}
	if s.cfg.Deployments == nil {
		s.cfg.Deployments = make(map[string]DeploymentProfile)
	}
	if s.cfg.Conversations == nil {
		s.cfg.Conversations = make(map[string]ConversationEntry)
	}
	if len(s.cfg.Conversations) == 0 {
		token := mintConversationToken()
		now := time.Now().UTC()
		s.cfg.Conversations[defaultConvAlias] = ConversationEntry{
			Alias:       defaultConvAlias,
			Token:       token,
			Description: defaultConvDescription,
			CreatedAt:   now,
			LastUsedAt:  now,
		}
		s.cfg.ActiveConversation = defaultConvAlias
		dirty = true
	}
	if s.cfg.ActiveConversation == "" {
		for alias := range s.cfg.Conversations {
			s.cfg.ActiveConversation = alias
			dirty = true
			break
		}
	}
	if s.healActiveConversationLocked() {
		dirty = true
	}
	if envConv := strings.TrimSpace(os.Getenv(envAGYConversationID)); envConv != "" && s.cfg.Push.AGYConversationID == "" {
		s.cfg.Push.AGYConversationID = envConv
		s.cfg.Push.Enabled = true
		dirty = true
	}
	if !dirty {
		return nil
	}
	return s.saveLocked()
}

func (s *ConfigStore) saveLocked() error {
	if s.path == "" {
		return nil
	}
	data, err := json.MarshalIndent(s.cfg, "", "  ")
	if err != nil {
		return fmt.Errorf("marshaling config: %w", err)
	}
	if err := writeFileAtomic(s.path, data); err != nil {
		// Every caller treats a save as best effort; the failure is said once, here.
		slog.Warn("kube-agents-mcp: config save failed", "path", s.path, "err", err)
		return err
	}
	s.loaded = stampOf(s.path)
	return nil
}

func writeFileAtomic(path string, data []byte) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, dirPermMode); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, filepath.Base(path)+".*"+tempFileSuffix)
	if err != nil {
		return err
	}
	tmp := f.Name()
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return err
	}
	if err := f.Chmod(filePermMode); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return err
	}
	// Flush to stable storage before the rename publishes the file: a crash between the two
	// must leave the previous complete version, never an empty or truncated one.
	if err := f.Sync(); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return err
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}

// writeFileAtomicIfChanged atomically writes data to path only when the file does not exist
// or its current contents differ from data, avoiding redundant temp-file creation, fsync,
// and rename churn on hot sync paths.
func writeFileAtomicIfChanged(path string, data []byte) error {
	if existing, err := os.ReadFile(path); err == nil && bytes.Equal(existing, data) {
		return nil
	}
	return writeFileAtomic(path, data)
}

func mintConversationToken() string {
	buf := make([]byte, randomTokenBytes)
	if _, err := rand.Read(buf); err != nil {
		return fmt.Sprintf("%s%d", defaultConvTokenPrefix, time.Now().UnixNano())
	}
	return defaultConvTokenPrefix + hex.EncodeToString(buf)
}

// Path returns the filesystem path of the persistent configuration file.
func (s *ConfigStore) Path() string {
	return s.path
}

// EventsPath returns the filesystem path of the pending push events file.
func (s *ConfigStore) EventsPath() string {
	return s.eventsPath
}

// StepProgressPath returns the filesystem path of the persisted per-task step cursor file.
func (s *ConfigStore) StepProgressPath() string {
	return filepath.Join(filepath.Dir(s.eventsPath), stepProgressFileName)
}

// Snapshot returns a deep copy of the current configuration.
func (s *ConfigStore) Snapshot() PersistentConfig {
	defer s.lockForRead()()
	out := s.cfg
	out.Deployments = make(map[string]DeploymentProfile, len(s.cfg.Deployments))
	for k, v := range s.cfg.Deployments {
		vCopy := v
		vCopy.AgentProfiles = append([]string(nil), v.AgentProfiles...)
		vCopy.ActiveSessionPods = append([]SessionPodInfo(nil), v.ActiveSessionPods...)
		out.Deployments[k] = vCopy
	}
	out.Conversations = make(map[string]ConversationEntry, len(s.cfg.Conversations))
	for k, v := range s.cfg.Conversations {
		out.Conversations[k] = v
	}
	return out
}

// UpsertDeployments merges discovered or configured deployments into the store,
// preserving existing cached credentials if the new profile does not overwrite them.
func (s *ConfigStore) UpsertDeployments(profiles []DeploymentProfile) error {
	defer s.lockForUpdate()()
	for _, p := range profiles {
		if p.Name == "" {
			continue
		}
		if existing, ok := s.cfg.Deployments[p.Name]; ok {
			if p.BusAuthValue == "" {
				p.BusAuthValue = existing.BusAuthValue
			}
			if p.ObserverAuthValue == "" {
				p.ObserverAuthValue = existing.ObserverAuthValue
			}
			if p.BusUser == "" {
				p.BusUser = existing.BusUser
			}
			if p.DirectNATSURL == "" {
				p.DirectNATSURL = existing.DirectNATSURL
			}
			// A fresh discovery is authoritative for the fleet profile list (an empty list means
			// the cluster has no interactive profiles); other upserts keep the cached list.
			freshDiscovery := !p.LastDiscoveredAt.IsZero() && p.LastDiscoveredAt.After(existing.LastDiscoveredAt)
			if len(p.AgentProfiles) == 0 && len(existing.AgentProfiles) > 0 && !freshDiscovery {
				p.AgentProfiles = append([]string(nil), existing.AgentProfiles...)
			}
			if p.SessionPodsProbedAt.IsZero() && !existing.SessionPodsProbedAt.IsZero() {
				p.ActiveSessionPods = append([]SessionPodInfo(nil), existing.ActiveSessionPods...)
				p.SessionPodsProbedAt = existing.SessionPodsProbedAt
			}
			if p.LastConnectedAt.IsZero() {
				p.LastConnectedAt = existing.LastConnectedAt
			}
		}
		s.cfg.Deployments[p.Name] = p
		if s.cfg.ActiveDeployment == "" && p.Ready {
			s.cfg.ActiveDeployment = p.Name
		}
	}
	if s.cfg.ActiveDeployment == "" && len(s.cfg.Deployments) > 0 {
		names := make([]string, 0, len(s.cfg.Deployments))
		for k := range s.cfg.Deployments {
			names = append(names, k)
		}
		sort.Strings(names)
		s.cfg.ActiveDeployment = names[0]
	}
	return s.saveLocked()
}

// SetActiveDeployment selects a deployment by name, kubeContext, or clusterName
// and marks it as the active deployment.
func (s *ConfigStore) SetActiveDeployment(selector string) (DeploymentProfile, error) {
	defer s.lockForUpdate()()
	if selector == "" {
		selector = s.cfg.ActiveDeployment
	}
	if selector == "" && len(s.cfg.Deployments) == 0 {
		return DeploymentProfile{}, errNoDeploymentsSaved
	}
	if p, ok := s.cfg.Deployments[selector]; ok {
		p.LastConnectedAt = time.Now().UTC()
		s.cfg.Deployments[selector] = p
		s.cfg.ActiveDeployment = selector
		_ = s.saveLocked()
		return p, nil
	}
	for name, p := range s.cfg.Deployments {
		if p.KubeContext == selector || p.ClusterName == selector || p.PlatformAgentName == selector || p.ProjectID == selector {
			p.LastConnectedAt = time.Now().UTC()
			s.cfg.Deployments[name] = p
			s.cfg.ActiveDeployment = name
			_ = s.saveLocked()
			return p, nil
		}
	}
	lowerSel := strings.ToLower(strings.TrimSpace(selector))
	if lowerSel != "" {
		for name, p := range s.cfg.Deployments {
			if strings.Contains(strings.ToLower(name), lowerSel) ||
				strings.Contains(strings.ToLower(p.KubeContext), lowerSel) ||
				strings.Contains(strings.ToLower(p.ClusterName), lowerSel) ||
				strings.Contains(strings.ToLower(p.ProjectID), lowerSel) ||
				strings.Contains(strings.ToLower(p.PlatformAgentName), lowerSel) ||
				strings.Contains(strings.ToLower(p.GitHubRepo), lowerSel) {
				p.LastConnectedAt = time.Now().UTC()
				s.cfg.Deployments[name] = p
				s.cfg.ActiveDeployment = name
				_ = s.saveLocked()
				return p, nil
			}
		}
	}
	return DeploymentProfile{}, fmt.Errorf("%w: %q", errDeploymentNotFound, selector)
}

// ActiveDeployment returns the currently selected DeploymentProfile, if any.
func (s *ConfigStore) ActiveDeployment() (DeploymentProfile, bool) {
	defer s.lockForRead()()
	if s.cfg.ActiveDeployment == "" {
		return DeploymentProfile{}, false
	}
	p, ok := s.cfg.Deployments[s.cfg.ActiveDeployment]
	return p, ok
}

// ActiveConversation returns the active ConversationEntry, minting default if needed.
func (s *ConfigStore) ActiveConversation() ConversationEntry {
	defer s.lockForUpdate()()
	if entry, ok := s.cfg.Conversations[s.cfg.ActiveConversation]; ok {
		return entry
	}
	now := time.Now().UTC()
	entry := ConversationEntry{
		Alias:      defaultConvAlias,
		Token:      mintConversationToken(),
		CreatedAt:  now,
		LastUsedAt: now,
	}
	s.cfg.Conversations[defaultConvAlias] = entry
	s.cfg.ActiveConversation = defaultConvAlias
	_ = s.saveLocked()
	return entry
}

// LookupConversation returns a conversation by alias or console token without mutating
// ActiveConversation, LastUsedAt, or minting a new conversation.
func (s *ConfigStore) LookupConversation(aliasOrToken string) (ConversationEntry, bool) {
	if s == nil {
		return ConversationEntry{}, false
	}
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), consoleConvPrefix)
	if target == "" {
		return ConversationEntry{}, false
	}
	defer s.lockForRead()()
	if c, ok := s.cfg.Conversations[target]; ok {
		return c, true
	}
	for _, c := range s.cfg.Conversations {
		if c.Token == target || strings.TrimSpace(c.AGYConversationID) == target {
			return c, true
		}
	}
	return ConversationEntry{}, false
}

// EnsureConversation creates or switches to a conversation alias (or raw token).
func (s *ConfigStore) EnsureConversation(aliasOrToken string, forceNew bool, description string) (ConversationEntry, error) {
	return s.EnsureConversationWithOptions(aliasOrToken, forceNew, description, true)
}

// EnsureConversationWithOptions creates or looks up a conversation alias (or raw token),
// optionally updating ActiveConversation when activate is true and the target is not a child subagent.
func (s *ConfigStore) EnsureConversationWithOptions(aliasOrToken string, forceNew bool, description string, activate bool) (ConversationEntry, error) {
	defer s.lockForUpdate()()
	now := time.Now().UTC()
	alias := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), consoleConvPrefix)
	if alias == "" {
		alias = s.cfg.ActiveConversation
		if alias == "" {
			alias = defaultConvAlias
		}
	}
	if !forceNew {
		if existing, ok := s.cfg.Conversations[alias]; ok {
			return s.touchExistingConversationLocked(alias, existing, now, description, activate)
		}
		for k, existing := range s.cfg.Conversations {
			if existing.Token == alias {
				return s.touchExistingConversationLocked(k, existing, now, "", activate)
			}
		}
	}
	token := mintConversationToken()
	if !forceNew && isValidConsoleToken(alias) && alias != defaultConvAlias {
		token = alias
	}
	entry := ConversationEntry{
		Alias:       alias,
		Token:       token,
		Description: description,
		CreatedAt:   now,
		LastUsedAt:  now,
	}
	s.cfg.Conversations[alias] = entry
	if activate {
		s.cfg.ActiveConversation = alias
	}
	return entry, s.saveLocked()
}

func isDedicatedSubagentConversation(c ConversationEntry) bool {
	return isSessionSubagentEntry(c)
}

// touchExistingConversationLocked records a use of an existing conversation. LastUsedAt is
// only persisted when it moved by more than lastUsedCoalesceWindow (it drives ordering and
// pruning, not auditing), so a burst of calls on one conversation costs one config write
// instead of one per call; description and activation changes always persist.
func (s *ConfigStore) touchExistingConversationLocked(key string, existing ConversationEntry, now time.Time, description string, activate bool) (ConversationEntry, error) {
	changed := false
	if now.Sub(existing.LastUsedAt) >= lastUsedCoalesceWindow || existing.LastUsedAt.After(now) {
		existing.LastUsedAt = now
		changed = true
	}
	if description != "" && description != existing.Description {
		existing.Description = description
		changed = true
	}
	if activate && !isDedicatedSubagentConversation(existing) && s.cfg.ActiveConversation != key {
		s.cfg.ActiveConversation = key
		changed = true
	}
	if !changed {
		return existing, nil
	}
	s.cfg.Conversations[key] = existing
	return existing, s.saveLocked()
}

func (s *ConfigStore) healActiveConversationLocked() bool {
	cur, hasCur := s.cfg.Conversations[s.cfg.ActiveConversation]
	if hasCur && !isDedicatedSubagentConversation(cur) && (strings.TrimSpace(cur.AGYConversationID) != "" || s.cfg.ActiveConversation != defaultConvAlias || strings.TrimSpace(s.cfg.Push.AGYConversationID) == "") {
		return false
	}
	pushID := strings.TrimSpace(s.cfg.Push.AGYConversationID)
	if pushID != "" {
		for alias, entry := range s.cfg.Conversations {
			if strings.TrimSpace(entry.AGYConversationID) == pushID && !isDedicatedSubagentConversation(entry) {
				if s.cfg.ActiveConversation != alias {
					s.cfg.ActiveConversation = alias
					return true
				}
				return false
			}
		}
	}
	// Prefer the most recently used non-subagent conversation bound to a real Antigravity conversation ID
	// before falling back to an unbound "default" entry.
	var bestBoundAlias string
	var bestBoundTime time.Time
	for alias, entry := range s.cfg.Conversations {
		if isDedicatedSubagentConversation(entry) || entry.SessionTerminated || strings.TrimSpace(entry.AGYConversationID) == "" {
			continue
		}
		if bestBoundAlias == "" || entry.LastUsedAt.After(bestBoundTime) || (entry.LastUsedAt.Equal(bestBoundTime) && alias < bestBoundAlias) {
			bestBoundAlias = alias
			bestBoundTime = entry.LastUsedAt
		}
	}
	if bestBoundAlias != "" {
		if s.cfg.ActiveConversation != bestBoundAlias {
			s.cfg.ActiveConversation = bestBoundAlias
			return true
		}
		return false
	}
	if def, ok := s.cfg.Conversations[defaultConvAlias]; ok && !isDedicatedSubagentConversation(def) {
		if s.cfg.ActiveConversation != defaultConvAlias {
			s.cfg.ActiveConversation = defaultConvAlias
			return true
		}
		return false
	}
	for alias, entry := range s.cfg.Conversations {
		if !isDedicatedSubagentConversation(entry) {
			if s.cfg.ActiveConversation != alias {
				s.cfg.ActiveConversation = alias
				return true
			}
			return false
		}
	}
	return false
}

// SetConversationSessionMode records whether /session mode is active on the conversation.
func (s *ConfigStore) SetConversationSessionMode(aliasOrToken string, sessionMode bool) (ConversationEntry, error) {
	defer s.lockForUpdate()()
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), "console:")
	if target == "" {
		target = s.cfg.ActiveConversation
	}
	for alias, entry := range s.cfg.Conversations {
		if alias == target || entry.Token == target {
			wasSession := entry.SessionMode || entry.SessionWorkerID != ""
			entry.SessionMode = sessionMode
			if sessionMode {
				entry.SessionTerminated = false
			} else if wasSession {
				entry.SessionTerminated = true
			}
			entry.LastUsedAt = time.Now().UTC()
			s.cfg.Conversations[alias] = entry
			return entry, s.saveLocked()
		}
	}
	return ConversationEntry{}, fmt.Errorf("conversation %q not found", target)
}

// DeleteConversation removes a conversation entry by alias, console token, or swarm member name
// (e.g. "session-soak-sess-1" -> "soak-sess-1"). Returns the deleted entry and true if found.
func (s *ConfigStore) DeleteConversation(aliasOrToken string) (ConversationEntry, bool) {
	if s == nil {
		return ConversationEntry{}, false
	}
	defer s.lockForUpdate()()
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), consoleConvPrefix)
	if target == "" {
		return ConversationEntry{}, false
	}
	trimmedSessionPrefix := strings.TrimPrefix(target, "session-")
	for alias, entry := range s.cfg.Conversations {
		roleSlug := sanitizeMemberName(entry.SubagentRole)
		if alias == target ||
			entry.Token == target ||
			strings.TrimSpace(entry.AGYConversationID) == target ||
			strings.TrimSpace(entry.SessionWorkerID) == target ||
			(trimmedSessionPrefix != target && alias == trimmedSessionPrefix) ||
			sanitizeMemberName(alias) == target ||
			"session-"+sanitizeMemberName(alias) == target ||
			(roleSlug != "" && (roleSlug == target || "session-"+roleSlug == target || roleSlug == trimmedSessionPrefix)) {
			delete(s.cfg.Conversations, alias)
			s.removeEntrySessionPodsLocked(entry)
			s.healActiveConversationLocked()
			_ = s.saveLocked()
			return entry, true
		}
	}
	return ConversationEntry{}, false
}

// ClearKilledSessions deletes all terminated or inactive non-active conversation entries
// that do not have a live session pod in activePodTokens. Returns the removed entries.
func (s *ConfigStore) ClearKilledSessions(activePodTokens map[string]bool) []ConversationEntry {
	if s == nil {
		return nil
	}
	defer s.lockForUpdate()()
	pushID := strings.TrimSpace(s.cfg.Push.AGYConversationID)
	var removed []ConversationEntry
	for alias, entry := range s.cfg.Conversations {
		if alias == defaultConvAlias || alias == s.cfg.ActiveConversation {
			continue
		}
		if pushID != "" && strings.TrimSpace(entry.AGYConversationID) == pushID && !isDedicatedSubagentConversation(entry) {
			continue
		}
		if activePodTokens != nil && activePodTokens[entry.Token] {
			continue
		}
		if entry.SessionMode && !entry.SessionTerminated {
			continue
		}
		if entry.SessionTerminated || !entry.SessionMode {
			removed = append(removed, entry)
			s.removeEntrySessionPodsLocked(entry)
			delete(s.cfg.Conversations, alias)
		}
	}
	if len(removed) > 0 {
		s.healActiveConversationLocked()
		_ = s.saveLocked()
	}
	return removed
}

func (s *ConfigStore) removeEntrySessionPodsLocked(entry ConversationEntry) {
	token := strings.TrimSpace(entry.Token)
	wid := strings.TrimSpace(entry.SessionWorkerID)
	if token == "" && wid == "" {
		return
	}
	for depName, prof := range s.cfg.Deployments {
		if len(prof.ActiveSessionPods) == 0 {
			continue
		}
		kept := make([]SessionPodInfo, 0, len(prof.ActiveSessionPods))
		changed := false
		for _, pod := range prof.ActiveSessionPods {
			podTok := strings.TrimPrefix(strings.TrimSpace(pod.SessionKey), consoleConvPrefix)
			podAddr := firstNonEmptyStr(strings.TrimSpace(pod.Addressee), strings.TrimSpace(pod.Name))
			if (token != "" && podTok == token) || (wid != "" && (podAddr == wid || strings.TrimSpace(pod.Name) == wid)) {
				changed = true
				continue
			}
			kept = append(kept, pod)
		}
		if changed {
			prof.ActiveSessionPods = kept
			s.cfg.Deployments[depName] = prof
		}
	}
}

// SetConversationSessionWorkerID records the remote s-<id> / chat-<id> worker addressee observed on TASKS
// for a console conversation, structurally linking the console token to its /session pod.
func (s *ConfigStore) SetConversationSessionWorkerID(aliasOrToken, workerID string) {
	if s == nil {
		return
	}
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), "console:")
	wid := strings.TrimSpace(workerID)
	if target == "" || wid == "" {
		return
	}
	defer s.lockForUpdate()()
	for alias, entry := range s.cfg.Conversations {
		if alias == target || entry.Token == target {
			if entry.SessionWorkerID == wid {
				return
			}
			entry.SessionWorkerID = wid
			s.cfg.Conversations[alias] = entry
			_ = s.saveLocked()
			return
		}
	}
}

// SetConversationParentAGYID updates the parent/root coordinator Antigravity conversation UUID
// (and optional role) for a subagent-owned console conversation.
func (s *ConfigStore) SetConversationParentAGYID(aliasOrToken, parentAGYID, role string) {
	if s == nil {
		return
	}
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), "console:")
	pid := strings.TrimSpace(parentAGYID)
	r := strings.TrimSpace(role)
	if target == "" || pid == "" {
		return
	}
	defer s.lockForUpdate()()
	for alias, entry := range s.cfg.Conversations {
		if alias == target || entry.Token == target {
			if entry.ParentAGYConversationID == pid && (r == "" || entry.SubagentRole == r) {
				return
			}
			entry.ParentAGYConversationID = pid
			if r != "" && entry.SubagentRole == "" {
				entry.SubagentRole = r
			}
			s.cfg.Conversations[alias] = entry
			_ = s.saveLocked()
			return
		}
	}
}

// DebugMode returns whether kube-agents debug output (tool call tables & timings) is enabled by default.
func (s *ConfigStore) DebugMode() bool {
	if s == nil {
		return false
	}
	defer s.lockForRead()()
	return s.cfg.DebugMode
}

// SetDebugMode sets whether kube-agents debug output (tool call tables & timings) is enabled by default.
func (s *ConfigStore) SetDebugMode(enabled bool) error {
	defer s.lockForUpdate()()
	s.cfg.DebugMode = enabled
	return s.saveLocked()
}

// DebugModeFor reports whether debug output is on for a conversation: its own flag, or the global
// default (kept for non-Antigravity callers and backwards compatibility).
func (s *ConfigStore) DebugModeFor(aliasOrToken string) bool {
	if s == nil {
		return false
	}
	defer s.lockForRead()()
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), "console:")
	for alias, entry := range s.cfg.Conversations {
		if alias == target || entry.Token == target {
			return entry.DebugMode || s.cfg.DebugMode
		}
	}
	return s.cfg.DebugMode
}

// SetConversationDebugMode sets the per-conversation debug flag.
func (s *ConfigStore) SetConversationDebugMode(aliasOrToken string, enabled bool) error {
	defer s.lockForUpdate()()
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), "console:")
	for alias, entry := range s.cfg.Conversations {
		if alias == target || entry.Token == target {
			entry.DebugMode = enabled
			s.cfg.Conversations[alias] = entry
			return s.saveLocked()
		}
	}
	return fmt.Errorf("conversation %q not found", target)
}

// BindConversationAGYID associates a console conversation (by alias or token) with an Antigravity conversation UUID.
func (s *ConfigStore) BindConversationAGYID(aliasOrToken, agyConvID string) {
	trimmedID := strings.TrimSpace(agyConvID)
	if trimmedID == "" {
		return
	}
	s.BindConversationWithLineage(aliasOrToken, ConversationLineage{
		ConversationID:     trimmedID,
		RootConversationID: trimmedID,
	})
}

// BindConversationWithLineage associates a console conversation with an Antigravity conversation and
// records its parent/root lineage if it belongs to a child subagent.
func (s *ConfigStore) BindConversationWithLineage(aliasOrToken string, lineage ConversationLineage) {
	trimmedID := strings.TrimSpace(lineage.ConversationID)
	if trimmedID == "" {
		return
	}
	defer s.lockForUpdate()()
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), "console:")
	if target == "" {
		target = s.cfg.ActiveConversation
	}
	isActiveTarget := target == s.cfg.ActiveConversation
	if active, ok := s.cfg.Conversations[s.cfg.ActiveConversation]; ok {
		if target == active.Token || active.AGYConversationID == "" || active.AGYConversationID == trimmedID {
			isActiveTarget = true
		}
	}
	if !lineage.IsSubagent && (isActiveTarget || s.cfg.Push.AGYConversationID == "") {
		s.cfg.Push.AGYConversationID = trimmedID
	} else if s.cfg.Push.AGYConversationID == "" && lineage.RootConversationID != "" {
		s.cfg.Push.AGYConversationID = lineage.RootConversationID
	}
	s.cfg.Push.Enabled = true
	s.addKnownAGYConvLocked(trimmedID)
	if lineage.RootConversationID != "" {
		s.addKnownAGYConvLocked(lineage.RootConversationID)
	}
	subagentAlreadyHasOwnEntry := false
	if lineage.IsSubagent {
		for alias, entry := range s.cfg.Conversations {
			if alias != target && entry.Token != target && strings.TrimSpace(entry.AGYConversationID) == trimmedID {
				subagentAlreadyHasOwnEntry = true
				break
			}
		}
	}
	boundAlias := ""
	for alias, entry := range s.cfg.Conversations {
		if alias != target && entry.Token != target {
			continue
		}
		existingCID := strings.TrimSpace(entry.AGYConversationID)
		if lineage.IsSubagent {
			if existingCID != "" && existingCID != trimmedID {
				continue
			}
			if existingCID == "" && (subagentAlreadyHasOwnEntry || strings.TrimSpace(entry.SessionWorkerID) != "" || entry.SessionTerminated) {
				continue
			}
		} else if isDedicatedSubagentConversation(entry) || entry.SessionMode || strings.TrimSpace(entry.SessionWorkerID) != "" {
			continue
		}
		entry.AGYConversationID = trimmedID
		if lineage.IsSubagent {
			entry.ParentAGYConversationID = firstNonEmptyStr(lineage.RootConversationID, lineage.ParentConversationID, entry.ParentAGYConversationID)
			if lineage.SubagentRole != "" {
				entry.SubagentRole = lineage.SubagentRole
			}
		}
		s.cfg.Conversations[alias] = entry
		boundAlias = alias
		break
	}
	if boundAlias != "" {
		for alias, entry := range s.cfg.Conversations {
			if alias != boundAlias && entry.AGYConversationID == trimmedID {
				// An Antigravity conversation maps to exactly one console conversation at a time: switching
				// (manage_session new/switch, or an explicit `conversation` arg) moves the binding.
				entry.AGYConversationID = ""
				s.cfg.Conversations[alias] = entry
			}
		}
	}
	_ = s.saveLocked()
}

// EnsureConversationForSubagent resolves or creates the isolated console conversation owned by a
// child subagent (lineage.IsSubagent). A subagent never steals an existing ConversationEntry owned
// by another subagent or an already-active/historical /session worker, and once bound to its own
// entry it stays on that entry across connect_deployment, session_on, and send_message calls.
func (s *ConfigStore) EnsureConversationForSubagent(
	lineage ConversationLineage,
	requestedAlias string,
	forceNew bool,
	description string,
) (ConversationEntry, error) {
	id := strings.TrimSpace(lineage.ConversationID)
	if id == "" {
		return s.EnsureConversationWithOptions(requestedAlias, forceNew, description, false)
	}
	defer s.lockForUpdate()()
	now := time.Now().UTC()
	if s.cfg.Push.AGYConversationID == "" && lineage.RootConversationID != "" {
		s.cfg.Push.AGYConversationID = lineage.RootConversationID
	}
	s.cfg.Push.Enabled = true
	s.addKnownAGYConvLocked(id)
	if lineage.RootConversationID != "" {
		s.addKnownAGYConvLocked(lineage.RootConversationID)
	}

	reqClean := strings.TrimPrefix(strings.TrimSpace(requestedAlias), consoleConvPrefix)
	short := id
	if len(short) > agyAliasIDChars {
		short = short[:agyAliasIDChars]
	}

	if !forceNew {
		for ownAlias, ownEntry := range s.cfg.Conversations {
			if strings.TrimSpace(ownEntry.AGYConversationID) != id {
				continue
			}
			ownEntry.LastUsedAt = now
			parent := firstNonEmptyStr(lineage.RootConversationID, lineage.ParentConversationID, ownEntry.ParentAGYConversationID)
			if parent != "" {
				ownEntry.ParentAGYConversationID = parent
			}
			if lineage.SubagentRole != "" && ownEntry.SubagentRole == "" {
				ownEntry.SubagentRole = lineage.SubagentRole
			}
			if description != "" && (ownEntry.Description == "" ||
				strings.HasPrefix(ownEntry.Description, "Auto-bound to Antigravity conversation ") ||
				strings.Contains(ownEntry.Description, "(Antigravity subagent ")) {
				ownEntry.Description = description
			}
			if reqClean != "" &&
				ownAlias == "session-"+short &&
				!ownEntry.SessionMode &&
				strings.TrimSpace(ownEntry.SessionWorkerID) == "" {
				betterAlias := s.deriveConversationAliasWithHintLocked(lineage, reqClean)
				if betterAlias != "" && betterAlias != ownAlias {
					delete(s.cfg.Conversations, ownAlias)
					ownAlias = betterAlias
					ownEntry.Alias = betterAlias
				}
			}
			s.cfg.Conversations[ownAlias] = ownEntry
			return ownEntry, s.saveLocked()
		}

		if reqClean != "" && reqClean != defaultConvAlias && reqClean != s.cfg.ActiveConversation {
			if existing, ok := s.cfg.Conversations[reqClean]; ok {
				if strings.TrimSpace(existing.AGYConversationID) == "" &&
					!existing.SessionMode &&
					strings.TrimSpace(existing.SessionWorkerID) == "" &&
					!existing.SessionTerminated {
					existing.AGYConversationID = id
					existing.ParentAGYConversationID = firstNonEmptyStr(lineage.RootConversationID, lineage.ParentConversationID, existing.ParentAGYConversationID)
					if lineage.SubagentRole != "" {
						existing.SubagentRole = lineage.SubagentRole
					}
					if description != "" {
						existing.Description = description
					}
					existing.LastUsedAt = now
					s.cfg.Conversations[reqClean] = existing
					return existing, s.saveLocked()
				}
			}
		}
	}

	alias := s.deriveConversationAliasWithHintLocked(lineage, reqClean)
	desc := strings.TrimSpace(description)
	if desc == "" {
		if lineage.SubagentRole != "" {
			desc = lineage.SubagentRole + " (Antigravity subagent " + id + ")"
		} else {
			desc = "Auto-bound to Antigravity conversation " + id
		}
	}
	entry := ConversationEntry{
		Alias:                   alias,
		Token:                   mintConversationToken(),
		Description:             desc,
		AGYConversationID:       id,
		ParentAGYConversationID: firstNonEmptyStr(lineage.RootConversationID, lineage.ParentConversationID),
		SubagentRole:            lineage.SubagentRole,
		AutoBound:               reqClean == "",
		CreatedAt:               now,
		LastUsedAt:              now,
	}
	s.cfg.Conversations[alias] = entry
	return entry, s.saveLocked()
}

// ConversationForAGY returns the console conversation that belongs to an Antigravity conversation.
func (s *ConfigStore) ConversationForAGY(agyConvID string) ConversationEntry {
	id := strings.TrimSpace(agyConvID)
	if id == "" {
		return s.ActiveConversation()
	}
	return s.ConversationForAGYWithLineage(ConversationLineage{
		ConversationID:     id,
		RootConversationID: id,
	})
}

// ConversationForAGYWithLineage returns the console conversation that belongs to an Antigravity
// conversation (or subagent), isolating child subagents onto their own console token without
// stealing the root coordinator's default conversation or active pointer.
func (s *ConfigStore) ConversationForAGYWithLineage(lineage ConversationLineage) ConversationEntry {
	return s.ConversationForAGYWithOptions(lineage, true)
}

// ConversationForAGYWithOptions returns (or mints) the console conversation bound to an Antigravity
// conversation or subagent. When activate is false (e.g. a concurrent secondary Antigravity window
// while another live root coordinator owns the primary Swarm board), it isolates the caller onto
// its own ConversationEntry without overwriting ActiveConversation or Push.AGYConversationID.
func (s *ConfigStore) ConversationForAGYWithOptions(lineage ConversationLineage, activate bool) ConversationEntry {
	id := strings.TrimSpace(lineage.ConversationID)
	if id == "" {
		return s.ActiveConversation()
	}
	unlock := s.lockForUpdate()
	now := time.Now().UTC()
	changed := false
	setPushID := func(v string) {
		if s.cfg.Push.AGYConversationID != v {
			s.cfg.Push.AGYConversationID = v
			changed = true
		}
	}
	if !lineage.IsSubagent && (activate || s.cfg.Push.AGYConversationID == "") {
		setPushID(id)
	} else if s.cfg.Push.AGYConversationID == "" && lineage.RootConversationID != "" {
		setPushID(lineage.RootConversationID)
	}
	if !s.cfg.Push.Enabled {
		s.cfg.Push.Enabled = true
		changed = true
	}
	knownBefore := len(s.cfg.Push.KnownAGYConversations)
	s.addKnownAGYConvLocked(id)
	if lineage.RootConversationID != "" {
		s.addKnownAGYConvLocked(lineage.RootConversationID)
	}
	if len(s.cfg.Push.KnownAGYConversations) != knownBefore {
		changed = true
	}
	for alias, entry := range s.cfg.Conversations {
		if entry.AGYConversationID == id {
			if now.Sub(entry.LastUsedAt) >= lastUsedCoalesceWindow || entry.LastUsedAt.After(now) {
				entry.LastUsedAt = now
				changed = true
			}
			if lineage.IsSubagent {
				parent := firstNonEmptyStr(lineage.RootConversationID, lineage.ParentConversationID, entry.ParentAGYConversationID)
				if parent != entry.ParentAGYConversationID {
					entry.ParentAGYConversationID = parent
					changed = true
				}
				if lineage.SubagentRole != "" && lineage.SubagentRole != entry.SubagentRole {
					entry.SubagentRole = lineage.SubagentRole
					changed = true
				}
			}
			s.cfg.Conversations[alias] = entry
			if !lineage.IsSubagent && activate && s.cfg.ActiveConversation != alias {
				s.cfg.ActiveConversation = alias
				changed = true
			}
			if changed {
				_ = s.saveLocked()
			}
			unlock()
			return entry
		}
	}
	if !lineage.IsSubagent && activate {
		if active, ok := s.cfg.Conversations[s.cfg.ActiveConversation]; ok && active.AGYConversationID == "" {
			active.AGYConversationID = id
			active.LastUsedAt = now
			s.cfg.Conversations[s.cfg.ActiveConversation] = active
			_ = s.saveLocked()
			unlock()
			return active
		}
	}

	alias := s.deriveConversationAliasLocked(lineage)
	desc := "Auto-bound to Antigravity conversation " + id
	if lineage.IsSubagent && lineage.SubagentRole != "" {
		desc = lineage.SubagentRole + " (Antigravity subagent " + id + ")"
	}
	entry := ConversationEntry{
		Alias:             alias,
		Token:             mintConversationToken(),
		Description:       desc,
		AGYConversationID: id,
		AutoBound:         true,
		CreatedAt:         now,
		LastUsedAt:        now,
	}
	if lineage.IsSubagent {
		entry.ParentAGYConversationID = firstNonEmptyStr(lineage.RootConversationID, lineage.ParentConversationID)
		entry.SubagentRole = lineage.SubagentRole
	}
	s.cfg.Conversations[alias] = entry
	if !lineage.IsSubagent && activate {
		s.cfg.ActiveConversation = alias
	}
	_ = s.saveLocked()
	unlock()
	return entry
}

func (s *ConfigStore) deriveConversationAliasLocked(lineage ConversationLineage) string {
	return s.deriveConversationAliasWithHintLocked(lineage, "")
}

func (s *ConfigStore) deriveConversationAliasWithHintLocked(lineage ConversationLineage, aliasHint string) string {
	short := lineage.ConversationID
	if len(short) > agyAliasIDChars {
		short = short[:agyAliasIDChars]
	}
	if lineage.IsSubagent {
		slug := sanitizeAliasSlug(aliasHint)
		if slug == "" || slug == "session" || slug == defaultConvAlias {
			slug = sanitizeAliasSlug(lineage.SubagentRole)
		}
		if slug != "" && slug != "session" && slug != defaultConvAlias {
			candidate := "session-" + slug
			if strings.HasPrefix(slug, "session-") || strings.HasPrefix(slug, "s-") {
				candidate = slug
			}
			if _, exists := s.cfg.Conversations[candidate]; !exists {
				return candidate
			}
			for i := 2; i < 100; i++ {
				numbered := fmt.Sprintf("%s-%d", candidate, i)
				if _, exists := s.cfg.Conversations[numbered]; !exists {
					return numbered
				}
			}
			withShort := candidate + "-" + short[:minInt(4, len(short))]
			if _, exists := s.cfg.Conversations[withShort]; !exists {
				return withShort
			}
		}
		base := "session-" + short
		if _, exists := s.cfg.Conversations[base]; !exists {
			return base
		}
		for i := 2; i < 100; i++ {
			numbered := fmt.Sprintf("%s-%d", base, i)
			if _, exists := s.cfg.Conversations[numbered]; !exists {
				return numbered
			}
		}
		return base
	}
	return agyAliasPrefix + short
}

func sanitizeAliasSlug(raw string) string {
	lower := strings.ToLower(strings.TrimSpace(raw))
	if lower == "" {
		return ""
	}
	var b strings.Builder
	lastDash := false
	for _, r := range lower {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
			lastDash = false
		} else if !lastDash && b.Len() > 0 {
			b.WriteByte('-')
			lastDash = true
		}
	}
	res := strings.Trim(b.String(), "-")
	for _, trimWord := range []string{"kube-agents-", "kube-session-", "kube-", "-session-worker", "-worker", "-subagent"} {
		res = strings.TrimPrefix(res, trimWord)
		res = strings.TrimSuffix(res, trimWord)
	}
	res = strings.Trim(res, "-")
	if len(res) > 28 {
		res = strings.Trim(res[:28], "-")
	}
	return res
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func isValidConsoleToken(token string) bool {
	if len(token) == 0 || len(token) > maxConsoleTokenLen {
		return false
	}
	for _, r := range token {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '_' || r == '-' {
			continue
		}
		return false
	}
	return true
}

// MergeRemoteConversations merges a map of ConversationEntry records from a remote peer
// (bridge <-> gateway) into the store, preserving newer LastUsedAt timestamps and non-empty
// session/Antigravity bindings.
func (s *ConfigStore) MergeRemoteConversations(convs map[string]ConversationEntry, activeConv string) {
	if s == nil || (len(convs) == 0 && activeConv == "") {
		return
	}
	defer s.lockForUpdate()()
	changed := false
	for alias, incoming := range convs {
		if strings.TrimSpace(alias) == "" || strings.TrimSpace(incoming.Token) == "" {
			continue
		}
		if incoming.Alias == "" {
			incoming.Alias = alias
		}
		existing, ok := s.cfg.Conversations[alias]
		if !ok {
			s.cfg.Conversations[alias] = incoming
			changed = true
			continue
		}
		merged := existing
		if incoming.LastUsedAt.After(existing.LastUsedAt) {
			merged.LastUsedAt = incoming.LastUsedAt
			merged.SessionMode = incoming.SessionMode
			merged.SessionTerminated = incoming.SessionTerminated
			merged.DebugMode = incoming.DebugMode
		}
		if incoming.SessionWorkerID != "" && merged.SessionWorkerID != incoming.SessionWorkerID {
			merged.SessionWorkerID = incoming.SessionWorkerID
		}
		if incoming.AGYConversationID != "" && merged.AGYConversationID == "" {
			merged.AGYConversationID = incoming.AGYConversationID
		}
		if incoming.ParentAGYConversationID != "" && merged.ParentAGYConversationID == "" {
			merged.ParentAGYConversationID = incoming.ParentAGYConversationID
		}
		if incoming.SubagentRole != "" && merged.SubagentRole == "" {
			merged.SubagentRole = incoming.SubagentRole
		}
		if incoming.Description != "" && merged.Description == "" {
			merged.Description = incoming.Description
		}
		if merged != existing {
			s.cfg.Conversations[alias] = merged
			changed = true
		}
	}
	if activeConv != "" && s.cfg.ActiveConversation != activeConv {
		if _, ok := s.cfg.Conversations[activeConv]; ok {
			s.cfg.ActiveConversation = activeConv
			changed = true
		}
	}
	if changed {
		_ = s.saveLocked()
	}
}
