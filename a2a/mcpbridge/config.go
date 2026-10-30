package mcpbridge

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
	configDirRelPath       = ".config/kube-agents"
	configFileName         = "mcp-config.json"
	prCacheDirRelPath      = ".cache/kube-agents/prs"
	DirPermMode            = 0o700
	FilePermMode           = 0o600
	DefaultConvAlias       = "default"
	defaultConvTokenPrefix = "conv-"
	randomTokenBytes       = 6
	lastUsedCoalesceWindow = 5 * time.Second
	envConfigPath          = "KUBE_AGENTS_MCP_CONFIG"
	maxConsoleTokenLen     = 64
	tempFileSuffix         = ".tmp"
	lockFileSuffix         = ".lock"
	defaultConvDescription = "Default console conversation"
	ConsoleConvPrefix      = "console:"
)

var (
	errDeploymentNotFound  = errors.New("deployment profile not found")
	errDeploymentAmbiguous = errors.New("more than one saved deployment matches")
	errNoDeploymentsSaved  = errors.New("no deployments configured or discovered")
	errNotConnected        = errors.New("not connected to a kube-agents deployment")
	errConversationExists  = errors.New("alias already exists; pick another or delete it first")
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
//
// MCPToken is held in memory only (json:"-") and is never persisted to disk.
type DeploymentProfile struct {
	Name                     string    `json:"name"`
	KubeContext              string    `json:"kubeContext,omitempty"`
	Namespace                string    `json:"namespace"`
	PlatformAgentName        string    `json:"platformAgentName"`
	Mode                     string    `json:"mode,omitempty"`
	Version                  string    `json:"version,omitempty"`
	HelmChartVersion         string    `json:"helmChartVersion,omitempty"`
	LatestReleaseTag         string    `json:"latestReleaseTag,omitempty"`
	LatestReleaseCommit      string    `json:"latestReleaseCommit,omitempty"`
	LatestReleaseURL         string    `json:"latestReleaseUrl,omitempty"`
	LatestReleasePublishedAt string    `json:"latestReleasePublishedAt,omitempty"`
	UpgradeAvailable         bool      `json:"upgradeAvailable,omitempty"`
	CommitsBehind            int       `json:"commitsBehind,omitempty"`
	VersionCheckedAt         time.Time `json:"versionCheckedAt,omitempty"`
	ProjectID                string    `json:"projectId,omitempty"`
	ClusterName              string    `json:"clusterName,omitempty"`
	Location                 string    `json:"location,omitempty"`
	GitHubRepo               string    `json:"githubRepo,omitempty"`
	ChatPlatform             string    `json:"chatPlatform,omitempty"`
	MCPService               string    `json:"mcpService,omitempty"`
	MCPPort                  int       `json:"mcpPort,omitempty"`
	DirectMCPURL             string    `json:"directMcpUrl,omitempty"`
	TokenSecretName          string    `json:"tokenSecretName,omitempty"`
	MCPToken                 string    `json:"-"`
	Ready                    bool      `json:"ready"`
	StatusMessage            string    `json:"statusMessage,omitempty"`
	AgentProfiles            []string  `json:"agentProfiles,omitempty"`
	// AgentProfilesUnknown marks a discovered profile whose AgentProfile list could not be
	// read (RBAC, timeout), so an empty AgentProfiles is not "none" and the saved list stays.
	AgentProfilesUnknown bool             `json:"-"`
	ActiveSessionPods    []SessionPodInfo `json:"activeSessionPods,omitempty"`
	SessionPodsProbedAt  time.Time        `json:"sessionPodsProbedAt,omitempty"`
	ClustersMonitored    int              `json:"clustersMonitored,omitempty"`
	ToolExecutionsTotal  int              `json:"toolExecutionsTotal,omitempty"`
	RemediationsProposed int              `json:"remediationsProposed,omitempty"`
	RemediationsMerged   int              `json:"remediationsMerged,omitempty"`
	LastConnectedAt      time.Time        `json:"lastConnectedAt,omitempty"`
	LastDiscoveredAt     time.Time        `json:"lastDiscoveredAt,omitempty"`
}

// ConversationEntry tracks a named console conversation token and its mode.
type ConversationEntry struct {
	Alias                    string    `json:"alias"`
	Token                    string    `json:"token"`
	SessionMode              bool      `json:"sessionMode"`
	SessionTerminated        bool      `json:"sessionTerminated,omitempty"`
	Description              string    `json:"description,omitempty"`
	HostConversationID       string    `json:"hostConversationId,omitempty"`
	ParentHostConversationID string    `json:"parentHostConversationId,omitempty"`
	SubagentRole             string    `json:"subagentRole,omitempty"`
	SessionWorkerID          string    `json:"sessionWorkerId,omitempty"`
	AutoBound                bool      `json:"autoBound,omitempty"`
	DebugMode                bool      `json:"debugMode,omitempty"`
	Disconnected             bool      `json:"disconnected,omitempty"`
	CreatedAt                time.Time `json:"createdAt"`
	LastUsedAt               time.Time `json:"lastUsedAt"`
}

// PushConfig configures optional host push notification routing.
type PushConfig struct {
	Enabled                bool      `json:"enabled"`
	HostConversationID     string    `json:"hostConversationId,omitempty"`
	KnownHostConversations []string  `json:"knownHostConversations,omitempty"`
	NotifyCommandPath      string    `json:"notifyCommandPath,omitempty"`
	UpdatedAt              time.Time `json:"updatedAt,omitempty"`
}

// PersistentConfig is the on-disk schema stored in ~/.config/kube-agents/mcp-config.json.
type PersistentConfig struct {
	ActiveDeployment   string                       `json:"activeDeployment,omitempty"`
	ActiveConversation string                       `json:"activeConversation,omitempty"`
	DebugMode          bool                         `json:"debugMode,omitempty"`
	Disconnected       bool                         `json:"disconnected,omitempty"`
	Deployments        map[string]DeploymentProfile `json:"deployments"`
	Conversations      map[string]ConversationEntry `json:"conversations"`
	Push               PushConfig                   `json:"push"`
}

// ConfigStore manages thread-safe access and atomic persistence for PersistentConfig.
// Several processes may read and write it at once, so every method re-reads the file
// when another process changed it, and every mutation holds an exclusive file lock
// across its read-modify-write; saves are atomic renames.
type ConfigStore struct {
	mu       sync.Mutex
	path     string
	lockPath string
	cfg      PersistentConfig
	loaded   fileStamp
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
	if err := os.MkdirAll(baseDir, DirPermMode); err != nil {
		return nil, fmt.Errorf("creating config dir %s: %w", baseDir, err)
	}
	s := &ConfigStore{
		path:     configPath,
		lockPath: configPath + lockFileSuffix,
	}
	unlock, lockErr := LockFile(s.lockPath)
	if lockErr == nil {
		defer unlock()
	}
	if err := s.loadLocked(); err != nil {
		return nil, err
	}
	return s, nil
}

// WithReadLock runs fn under the store's read lock after refreshing in-memory state if needed.
func (s *ConfigStore) WithReadLock(fn func(cfg *PersistentConfig)) {
	if s == nil || fn == nil {
		return
	}
	defer s.lockForRead()()
	fn(&s.cfg)
}

// WithUpdateLock runs fn under the store's cross-process update lock after refreshing in-memory
// state, and atomically saves the config if fn returns true.
func (s *ConfigStore) WithUpdateLock(fn func(cfg *PersistentConfig) bool) error {
	if s == nil || fn == nil {
		return nil
	}
	defer s.lockForUpdate()()
	if fn(&s.cfg) {
		return s.saveLocked()
	}
	return nil
}

// lockForUpdate serializes a read-modify-write of the store against other processes
// and goroutines, and brings the in-memory state up to date first.
func (s *ConfigStore) lockForUpdate() func() {
	s.mu.Lock()
	unlock, err := LockFile(s.lockPath)
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
	s.refreshLocked()
	return s.mu.Unlock
}

// refreshLocked re-reads the config file when another process has written it since this
// process last loaded or saved it.
func (s *ConfigStore) refreshLocked() {
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
		token := MintConversationToken()
		now := time.Now().UTC()
		s.cfg.Conversations[DefaultConvAlias] = ConversationEntry{
			Alias:       DefaultConvAlias,
			Token:       token,
			Description: defaultConvDescription,
			CreatedAt:   now,
			LastUsedAt:  now,
		}
		s.cfg.ActiveConversation = DefaultConvAlias
		dirty = true
	}
	if s.cfg.ActiveConversation == "" {
		for alias := range s.cfg.Conversations {
			s.cfg.ActiveConversation = alias
			dirty = true
			break
		}
	}
	if HealActiveConversationLocked(&s.cfg) {
		dirty = true
	}
	if !dirty {
		return nil
	}
	return s.saveLocked()
}

func (s *ConfigStore) saveLocked() error {
	data, err := json.MarshalIndent(s.cfg, "", "  ")
	if err != nil {
		return fmt.Errorf("marshaling config: %w", err)
	}
	if err := WriteFileAtomic(s.path, data); err != nil {
		slog.Warn("kube-agents-mcp: config save failed", "path", s.path, "err", err)
		return err
	}
	s.loaded = stampOf(s.path)
	return nil
}

// WriteFileAtomic atomically writes data to path via a temp file in the same directory.
func WriteFileAtomic(path string, data []byte) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, DirPermMode); err != nil {
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
	if err := f.Chmod(FilePermMode); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return err
	}
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

// WriteFileAtomicIfChanged atomically writes data to path only when the file does not exist
// or its current contents differ from data.
func WriteFileAtomicIfChanged(path string, data []byte) error {
	if existing, err := os.ReadFile(path); err == nil && bytes.Equal(existing, data) {
		return nil
	}
	return WriteFileAtomic(path, data)
}

// MintConversationToken returns a new random console conversation token.
func MintConversationToken() string {
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

// LockPath returns the filesystem path of the config lock file.
func (s *ConfigStore) LockPath() string {
	return s.lockPath
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
	out.Push.KnownHostConversations = append([]string(nil), s.cfg.Push.KnownHostConversations...)
	return out
}

func sameKubeDeploymentIdentity(a, b DeploymentProfile) bool {
	return a.KubeContext != "" &&
		a.KubeContext == b.KubeContext &&
		a.Namespace == b.Namespace &&
		a.PlatformAgentName != "" &&
		a.PlatformAgentName == b.PlatformAgentName
}

// resolveDeploymentKeyLocked determines the canonical map key for p in s.cfg.Deployments,
// preserving any existing key for the same (KubeContext, Namespace, PlatformAgentName) and
// disambiguating cross-context collisions as "<kubeContext>/<name>".
func (s *ConfigStore) resolveDeploymentKeyLocked(p DeploymentProfile, batch []DeploymentProfile) string {
	baseName := strings.TrimSpace(p.Name)
	if baseName == "" {
		return ""
	}
	// Check if this exact kube deployment already exists in the store under any key.
	existingKey := ""
	if p.KubeContext != "" && p.PlatformAgentName != "" {
		for k, existing := range s.cfg.Deployments {
			if sameKubeDeploymentIdentity(existing, p) {
				existingKey = k
				break
			}
		}
	}
	// Check whether another deployment in the current batch or in the store shares baseName
	// from a different kubeconfig context.
	collides := false
	if p.KubeContext != "" {
		for _, other := range batch {
			if other.Name == "" || other.IsContextProbeFailure() {
				continue
			}
			if other.Name == baseName && other.KubeContext != "" && !sameKubeDeploymentIdentity(other, p) {
				collides = true
				break
			}
		}
		if !collides {
			// Any saved deployment of this name from another identity collides, whether it is
			// stored bare or already qualified as "<context>/<name>".
			for k, existing := range s.cfg.Deployments {
				if existing.KubeContext != "" && deploymentBaseName(k, existing) == baseName && !sameKubeDeploymentIdentity(existing, p) {
					collides = true
					break
				}
			}
		}
	}
	if collides {
		disambiguated := p.KubeContext
		if !strings.HasPrefix(baseName, p.KubeContext+"/") {
			disambiguated = p.KubeContext + "/" + baseName
		} else {
			disambiguated = baseName
		}
		// If this deployment was previously stored under bare baseName before the collision
		// appeared, migrate its key to the disambiguated name.
		if existingKey == baseName && disambiguated != baseName {
			if prev, ok := s.cfg.Deployments[baseName]; ok && sameKubeDeploymentIdentity(prev, p) {
				prev.Name = disambiguated
				s.cfg.Deployments[disambiguated] = prev
				delete(s.cfg.Deployments, baseName)
				if s.cfg.ActiveDeployment == baseName {
					s.cfg.ActiveDeployment = disambiguated
				}
			}
		}
		return disambiguated
	}
	if existingKey != "" {
		return existingKey
	}
	return baseName
}

// UpsertDeployments merges discovered or configured deployments into the store.
// ContextProbeFailure entries describe no deployment and are skipped.
func (s *ConfigStore) UpsertDeployments(profiles []DeploymentProfile) error {
	defer s.lockForUpdate()()
	s.upsertDeploymentsLocked(profiles)
	return s.saveLocked()
}

// UpsertDiscoveredForAutoConnect saves a discovery scan made because no deployment was
// active, and returns the deployment to connect to. One discovered deployment is it; more
// than one is the same ambiguity error an empty connect_deployment selector gets, and none
// of them is made active, so the user names one instead of the bridge picking by sort order.
// A deployment made active meanwhile (another bridge process) is returned as is.
func (s *ConfigStore) UpsertDiscoveredForAutoConnect(profiles []DeploymentProfile) (DeploymentProfile, error) {
	defer s.lockForUpdate()()
	_, wasActive := s.cfg.Deployments[s.cfg.ActiveDeployment]
	wasActive = wasActive && s.cfg.ActiveDeployment != ""
	keys := s.upsertDeploymentsLocked(profiles)
	if !wasActive && len(keys) > 1 {
		s.cfg.ActiveDeployment = ""
		_ = s.saveLocked()
		sort.Strings(keys)
		return DeploymentProfile{}, fmt.Errorf("%w: no deployment is active; name one of %s", errDeploymentAmbiguous, strings.Join(keys, ", "))
	}
	if err := s.saveLocked(); err != nil {
		return DeploymentProfile{}, err
	}
	p, ok := s.cfg.Deployments[s.cfg.ActiveDeployment]
	if !ok || s.cfg.ActiveDeployment == "" {
		return DeploymentProfile{}, errNoDeploymentsSaved
	}
	return p, nil
}

// upsertDeploymentsLocked is UpsertDeployments without the save; it returns the distinct
// saved keys the profiles were merged under.
func (s *ConfigStore) upsertDeploymentsLocked(profiles []DeploymentProfile) []string {
	var keys []string
	seen := make(map[string]bool, len(profiles))
	for _, p := range profiles {
		if p.Name == "" || p.IsContextProbeFailure() {
			continue
		}
		key := s.resolveDeploymentKeyLocked(p, profiles)
		p.Name = key
		if !seen[key] {
			seen[key] = true
			keys = append(keys, key)
		}
		if existing, ok := s.cfg.Deployments[key]; ok {
			if p.DirectMCPURL == "" {
				p.DirectMCPURL = existing.DirectMCPURL
			}
			if p.MCPService == "" {
				p.MCPService = existing.MCPService
			}
			if p.MCPPort == 0 {
				p.MCPPort = existing.MCPPort
			}
			if p.TokenSecretName == "" {
				p.TokenSecretName = existing.TokenSecretName
			}
			freshDiscovery := !p.LastDiscoveredAt.IsZero() && p.LastDiscoveredAt.After(existing.LastDiscoveredAt)
			if len(p.AgentProfiles) == 0 && len(existing.AgentProfiles) > 0 && (!freshDiscovery || p.AgentProfilesUnknown) {
				p.AgentProfiles = append([]string(nil), existing.AgentProfiles...)
			}
			if p.SessionPodsProbedAt.IsZero() && !existing.SessionPodsProbedAt.IsZero() {
				p.ActiveSessionPods = append([]SessionPodInfo(nil), existing.ActiveSessionPods...)
				p.SessionPodsProbedAt = existing.SessionPodsProbedAt
			}
			if p.LastConnectedAt.IsZero() {
				p.LastConnectedAt = existing.LastConnectedAt
			}
			if p.HelmChartVersion == "" {
				p.HelmChartVersion = existing.HelmChartVersion
			}
			if p.LatestReleaseTag == "" && existing.LatestReleaseTag != "" {
				p.LatestReleaseTag = existing.LatestReleaseTag
				p.LatestReleaseCommit = existing.LatestReleaseCommit
				p.LatestReleaseURL = existing.LatestReleaseURL
				p.LatestReleasePublishedAt = existing.LatestReleasePublishedAt
				p.UpgradeAvailable = existing.UpgradeAvailable
				p.CommitsBehind = existing.CommitsBehind
				p.VersionCheckedAt = existing.VersionCheckedAt
			}
			if p.ClustersMonitored == 0 && existing.ClustersMonitored > 0 && !freshDiscovery {
				p.ClustersMonitored = existing.ClustersMonitored
				p.ToolExecutionsTotal = existing.ToolExecutionsTotal
				p.RemediationsProposed = existing.RemediationsProposed
				p.RemediationsMerged = existing.RemediationsMerged
			}
		}
		// Never persist in-memory tokens on the stored profile copy.
		p.MCPToken = ""
		p.AgentProfilesUnknown = false
		s.cfg.Deployments[key] = p
		if s.cfg.ActiveDeployment == "" && p.Ready {
			s.cfg.ActiveDeployment = key
		}
	}
	if s.cfg.ActiveDeployment != "" {
		if _, exists := s.cfg.Deployments[s.cfg.ActiveDeployment]; !exists {
			s.cfg.ActiveDeployment = ""
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
	return keys
}

// SyncDiscoveredDeployments merges a full kubeconfig scan into the store: it upserts the
// discovered profiles and removes kubeconfig-backed profiles the scan no longer lists
// (manually configured DirectMCPURL entries are always preserved).
//
// A profile is pruned only when its kubeconfig context was actually scanned (or is no longer
// in the kubeconfig at all). A context the scan could not reach — reported by
// DiscoverDeployments as a ContextProbeFailure entry — keeps its saved profiles, marked Not
// Ready with the failure, so an expired credential or a VPN hiccup never deletes a saved
// deployment or the user's active selection.
func (s *ConfigStore) SyncDiscoveredDeployments(discovered []DeploymentProfile) error {
	if s == nil {
		return nil
	}
	seenByName := make(map[string]bool, len(discovered))
	unscanned := make(map[string]string)
	for _, p := range discovered {
		switch {
		case p.IsContextProbeFailure():
			unscanned[p.KubeContext] = p.StatusMessage
		case p.Name != "":
			seenByName[p.Name] = true
		}
	}
	wasDiscovered := func(name string, existing DeploymentProfile) bool {
		// A discovered profile of the same name may be another context's deployment, so the
		// name stands in for the identity only for a profile that has none.
		if existing.PlatformAgentName == "" {
			return seenByName[name]
		}
		for _, d := range discovered {
			if !d.IsContextProbeFailure() && sameKubeDeploymentIdentity(existing, d) {
				return true
			}
		}
		return false
	}
	func() {
		defer s.lockForUpdate()()
		changed := false
		for name, existing := range s.cfg.Deployments {
			if existing.KubeContext == "" || existing.DirectMCPURL != "" || wasDiscovered(name, existing) {
				continue
			}
			if reason, failed := unscanned[existing.KubeContext]; failed {
				existing.Ready = false
				existing.StatusMessage = fmt.Sprintf("kubeconfig context %q could not be scanned: %s (keeping the profile from the last successful scan)", existing.KubeContext, FirstNonEmptyStr(reason, "unknown error"))
				s.cfg.Deployments[name] = existing
				changed = true
				continue
			}
			delete(s.cfg.Deployments, name)
			if s.cfg.ActiveDeployment == name {
				s.cfg.ActiveDeployment = ""
			}
			changed = true
		}
		if changed {
			_ = s.saveLocked()
		}
	}()
	return s.UpsertDeployments(discovered)
}

// findDeploymentLocked resolves selector to one saved deployment: an exact key first, then an
// exact field (base name, kube context, cluster, project, PlatformAgent), then a substring of
// any of them. A tier that matches more than one deployment is an error naming them, never a
// map-order pick; an empty selector with no active deployment resolves only when one is saved.
func (s *ConfigStore) findDeploymentLocked(selector string) (string, DeploymentProfile, error) {
	selector = strings.TrimSpace(selector)
	if selector == "" {
		selector = s.cfg.ActiveDeployment
	}
	if len(s.cfg.Deployments) == 0 {
		if selector == "" {
			return "", DeploymentProfile{}, errNoDeploymentsSaved
		}
		return "", DeploymentProfile{}, fmt.Errorf("%w: %q", errDeploymentNotFound, selector)
	}
	if p, ok := s.cfg.Deployments[selector]; ok {
		return selector, p, nil
	}
	if selector == "" {
		if len(s.cfg.Deployments) == 1 {
			for name, p := range s.cfg.Deployments {
				return name, p, nil
			}
		}
		return "", DeploymentProfile{}, fmt.Errorf("%w: no deployment is active; name one of %s", errDeploymentAmbiguous, strings.Join(s.sortedDeploymentKeysLocked(nil), ", "))
	}
	eq := func(field string) bool { return field != "" && field == selector }
	lowerSel := strings.ToLower(selector)
	contains := func(field string) bool { return field != "" && strings.Contains(strings.ToLower(field), lowerSel) }
	tiers := []func(name string, p DeploymentProfile) bool{
		func(name string, p DeploymentProfile) bool {
			return eq(deploymentBaseName(name, p)) || eq(p.KubeContext) || eq(p.ClusterName) || eq(p.PlatformAgentName) || eq(p.ProjectID)
		},
		func(name string, p DeploymentProfile) bool {
			return contains(name) || contains(p.KubeContext) || contains(p.ClusterName) ||
				contains(p.ProjectID) || contains(p.PlatformAgentName) || contains(p.GitHubRepo)
		},
	}
	for _, match := range tiers {
		matches := s.sortedDeploymentKeysLocked(match)
		switch len(matches) {
		case 0:
			continue
		case 1:
			return matches[0], s.cfg.Deployments[matches[0]], nil
		default:
			return "", DeploymentProfile{}, fmt.Errorf("%w %q: %s; name one of them", errDeploymentAmbiguous, selector, strings.Join(matches, ", "))
		}
	}
	return "", DeploymentProfile{}, fmt.Errorf("%w: %q", errDeploymentNotFound, selector)
}

// sortedDeploymentKeysLocked is the saved deployment keys match accepts (all with nil), sorted.
func (s *ConfigStore) sortedDeploymentKeysLocked(match func(string, DeploymentProfile) bool) []string {
	var keys []string
	for name, p := range s.cfg.Deployments {
		if match == nil || match(name, p) {
			keys = append(keys, name)
		}
	}
	sort.Strings(keys)
	return keys
}

func (s *ConfigStore) markDeploymentConnectedLocked(name string, p DeploymentProfile) DeploymentProfile {
	token := p.MCPToken
	p.LastConnectedAt = time.Now().UTC()
	p.MCPToken = ""
	s.cfg.Deployments[name] = p
	s.cfg.ActiveDeployment = name
	s.cfg.Disconnected = false
	if s.cfg.ActiveConversation != "" {
		if entry, ok := s.cfg.Conversations[s.cfg.ActiveConversation]; ok && entry.Disconnected {
			entry.Disconnected = false
			s.cfg.Conversations[s.cfg.ActiveConversation] = entry
		}
	}
	_ = s.saveLocked()
	p.MCPToken = token
	return p
}

// SelectDeployment resolves a deployment by name, kubeContext, clusterName, projectId,
// platformAgentName, or githubRepo without mutating LastConnectedAt or ActiveDeployment.
func (s *ConfigStore) SelectDeployment(selector string) (DeploymentProfile, error) {
	defer s.lockForRead()()
	_, p, err := s.findDeploymentLocked(selector)
	return p, err
}

// MarkDeploymentConnected stamps LastConnectedAt, sets ActiveDeployment, and clears
// Disconnected after a live connection to prof has succeeded.
func (s *ConfigStore) MarkDeploymentConnected(prof DeploymentProfile) (DeploymentProfile, error) {
	defer s.lockForUpdate()()
	key := s.resolveDeploymentKeyLocked(prof, nil)
	if key == "" {
		return DeploymentProfile{}, errNoDeploymentsSaved
	}
	prof.Name = key
	return s.markDeploymentConnectedLocked(key, prof), nil
}

// SetActiveDeployment selects a deployment by name, kubeContext, or clusterName
// and marks it as the active connected deployment.
func (s *ConfigStore) SetActiveDeployment(selector string) (DeploymentProfile, error) {
	defer s.lockForUpdate()()
	name, p, err := s.findDeploymentLocked(selector)
	if err != nil {
		return DeploymentProfile{}, err
	}
	return s.markDeploymentConnectedLocked(name, p), nil
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
		Alias:      DefaultConvAlias,
		Token:      MintConversationToken(),
		CreatedAt:  now,
		LastUsedAt: now,
	}
	s.cfg.Conversations[DefaultConvAlias] = entry
	s.cfg.ActiveConversation = DefaultConvAlias
	_ = s.saveLocked()
	return entry
}

// LookupConversation returns a conversation by alias or console token without mutating
// ActiveConversation, LastUsedAt, or minting a new conversation.
func (s *ConfigStore) LookupConversation(aliasOrToken string) (ConversationEntry, bool) {
	if s == nil {
		return ConversationEntry{}, false
	}
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), ConsoleConvPrefix)
	if target == "" {
		return ConversationEntry{}, false
	}
	defer s.lockForRead()()
	if c, ok := s.cfg.Conversations[target]; ok {
		return c, true
	}
	for _, c := range s.cfg.Conversations {
		if c.Token == target || strings.TrimSpace(c.HostConversationID) == target {
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
	alias := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), ConsoleConvPrefix)
	if alias == "" {
		alias = s.cfg.ActiveConversation
		if alias == "" {
			alias = DefaultConvAlias
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
	token := MintConversationToken()
	if !forceNew && IsValidConsoleToken(alias) && alias != DefaultConvAlias {
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

// CreateConversation creates a conversation named alias with a fresh token and makes it the
// active one. An alias already in use is refused (errConversationExists) rather than replaced:
// replacing it would drop the old conversation's session state while the server kept it.
func (s *ConfigStore) CreateConversation(alias, description string) (ConversationEntry, error) {
	defer s.lockForUpdate()()
	alias = strings.TrimPrefix(strings.TrimSpace(alias), ConsoleConvPrefix)
	if alias == "" {
		return ConversationEntry{}, errors.New("a new conversation needs an alias")
	}
	if _, ok := s.cfg.Conversations[alias]; ok {
		return ConversationEntry{}, fmt.Errorf("%q: %w", alias, errConversationExists)
	}
	now := time.Now().UTC()
	entry := ConversationEntry{
		Alias:       alias,
		Token:       MintConversationToken(),
		Description: description,
		CreatedAt:   now,
		LastUsedAt:  now,
	}
	s.cfg.Conversations[alias] = entry
	s.cfg.ActiveConversation = alias
	return entry, s.saveLocked()
}

// IsDedicatedSubagentConversation reports whether c represents a child subagent conversation.
func IsDedicatedSubagentConversation(c ConversationEntry) bool {
	if strings.TrimSpace(c.ParentHostConversationID) != "" || strings.TrimSpace(c.SubagentRole) != "" {
		return true
	}
	alias := strings.TrimSpace(c.Alias)
	return strings.HasPrefix(alias, "session-") || strings.HasPrefix(alias, "s-") || strings.HasPrefix(alias, "chat-")
}

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
	if activate && !IsDedicatedSubagentConversation(existing) && s.cfg.ActiveConversation != key {
		s.cfg.ActiveConversation = key
		changed = true
	}
	if !changed {
		return existing, nil
	}
	s.cfg.Conversations[key] = existing
	return existing, s.saveLocked()
}

// HealActiveConversationLocked ensures cfg.ActiveConversation points to a non-subagent
// conversation entry when available.
func HealActiveConversationLocked(cfg *PersistentConfig) bool {
	if cfg == nil {
		return false
	}
	cur, hasCur := cfg.Conversations[cfg.ActiveConversation]
	if hasCur && !IsDedicatedSubagentConversation(cur) && (strings.TrimSpace(cur.HostConversationID) != "" || cfg.ActiveConversation != DefaultConvAlias || strings.TrimSpace(cfg.Push.HostConversationID) == "") {
		return false
	}
	pushID := strings.TrimSpace(cfg.Push.HostConversationID)
	if pushID != "" {
		for alias, entry := range cfg.Conversations {
			if strings.TrimSpace(entry.HostConversationID) == pushID && !IsDedicatedSubagentConversation(entry) {
				if cfg.ActiveConversation != alias {
					cfg.ActiveConversation = alias
					return true
				}
				return false
			}
		}
	}
	var bestBoundAlias string
	var bestBoundTime time.Time
	for alias, entry := range cfg.Conversations {
		if IsDedicatedSubagentConversation(entry) || entry.SessionTerminated || strings.TrimSpace(entry.HostConversationID) == "" {
			continue
		}
		if bestBoundAlias == "" || entry.LastUsedAt.After(bestBoundTime) || (entry.LastUsedAt.Equal(bestBoundTime) && alias < bestBoundAlias) {
			bestBoundAlias = alias
			bestBoundTime = entry.LastUsedAt
		}
	}
	if bestBoundAlias != "" {
		if cfg.ActiveConversation != bestBoundAlias {
			cfg.ActiveConversation = bestBoundAlias
			return true
		}
		return false
	}
	if def, ok := cfg.Conversations[DefaultConvAlias]; ok && !IsDedicatedSubagentConversation(def) {
		if cfg.ActiveConversation != DefaultConvAlias {
			cfg.ActiveConversation = DefaultConvAlias
			return true
		}
		return false
	}
	for alias, entry := range cfg.Conversations {
		if !IsDedicatedSubagentConversation(entry) {
			if cfg.ActiveConversation != alias {
				cfg.ActiveConversation = alias
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
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), ConsoleConvPrefix)
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

// ResolveConversation finds the conversation DeleteConversation would delete for aliasOrToken,
// without changing anything: by alias, then by console token, host conversation ID or session
// worker ID, then loosely by sanitized alias or subagent role. ambiguous reports that more than
// one conversation matched at the step that decided, and then ok is false.
func (s *ConfigStore) ResolveConversation(aliasOrToken string) (entry ConversationEntry, ok, ambiguous bool) {
	if s == nil {
		return ConversationEntry{}, false, false
	}
	defer s.lockForRead()()
	key, ambiguous := s.resolveConversationKeyLocked(aliasOrToken)
	if key == "" {
		return ConversationEntry{}, false, ambiguous
	}
	return s.cfg.Conversations[key], true, false
}

// resolveConversationKeyLocked is the matching ResolveConversation and DeleteConversation share.
// It returns "" when nothing, or more than one conversation, matches.
func (s *ConfigStore) resolveConversationKeyLocked(aliasOrToken string) (key string, ambiguous bool) {
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), ConsoleConvPrefix)
	if target == "" {
		return "", false
	}
	if _, ok := s.cfg.Conversations[target]; ok {
		return target, false
	}
	var exact []string
	for alias, entry := range s.cfg.Conversations {
		if entry.Token == target ||
			strings.TrimSpace(entry.HostConversationID) == target ||
			strings.TrimSpace(entry.SessionWorkerID) == target {
			exact = append(exact, alias)
		}
	}
	switch len(exact) {
	case 0:
	case 1:
		return exact[0], false
	default:
		return "", true
	}
	trimmedSessionPrefix := strings.TrimPrefix(target, "session-")
	var loose []string
	for alias, entry := range s.cfg.Conversations {
		roleSlug := SanitizeAliasSlug(entry.SubagentRole)
		if (trimmedSessionPrefix != target && alias == trimmedSessionPrefix) ||
			SanitizeAliasSlug(alias) == target ||
			"session-"+SanitizeAliasSlug(alias) == target ||
			(roleSlug != "" && (roleSlug == target || "session-"+roleSlug == target || roleSlug == trimmedSessionPrefix)) {
			loose = append(loose, alias)
		}
	}
	switch len(loose) {
	case 1:
		return loose[0], false
	case 0:
		return "", false
	default:
		return "", true
	}
}

// DeleteConversation removes a conversation entry by alias, console token, or session member name.
//
// The alias wins, then an exact identifier (console token, host conversation ID or session
// worker ID) that names one entry. The looser forms (a "session-" prefix, an alias slug, or a subagent role slug) delete only when
// exactly one entry matches: the store is shared by every bridge process on the machine, and
// several windows' workers can share a role such as "Session Worker", so an ambiguous match
// deletes nothing rather than whichever entry map iteration reaches first.
func (s *ConfigStore) DeleteConversation(aliasOrToken string) (ConversationEntry, bool) {
	if s == nil {
		return ConversationEntry{}, false
	}
	defer s.lockForUpdate()()
	matchKey, _ := s.resolveConversationKeyLocked(aliasOrToken)
	if matchKey == "" {
		return ConversationEntry{}, false
	}
	entry := s.cfg.Conversations[matchKey]
	delete(s.cfg.Conversations, matchKey)
	RemoveEntrySessionPodsLocked(&s.cfg, entry)
	HealActiveConversationLocked(&s.cfg)
	_ = s.saveLocked()
	return entry, true
}

// RemoveEntrySessionPodsLocked removes any cached ActiveSessionPods matching entry from cfg.Deployments.
func RemoveEntrySessionPodsLocked(cfg *PersistentConfig, entry ConversationEntry) {
	if cfg == nil {
		return
	}
	token := strings.TrimSpace(entry.Token)
	wid := strings.TrimSpace(entry.SessionWorkerID)
	if token == "" && wid == "" {
		return
	}
	for depName, prof := range cfg.Deployments {
		if len(prof.ActiveSessionPods) == 0 {
			continue
		}
		kept := make([]SessionPodInfo, 0, len(prof.ActiveSessionPods))
		changed := false
		for _, pod := range prof.ActiveSessionPods {
			podTok := strings.TrimPrefix(strings.TrimSpace(pod.SessionKey), ConsoleConvPrefix)
			podAddr := FirstNonEmptyStr(strings.TrimSpace(pod.Addressee), strings.TrimSpace(pod.Name))
			if (token != "" && podTok == token) || (wid != "" && (podAddr == wid || strings.TrimSpace(pod.Name) == wid)) {
				changed = true
				continue
			}
			kept = append(kept, pod)
		}
		if changed {
			prof.ActiveSessionPods = kept
			cfg.Deployments[depName] = prof
		}
	}
}

// SetConversationSessionWorkerID records the remote s-<id> / chat-<id> worker addressee observed on TASKS
// for a console conversation, structurally linking the console token to its /session pod.
func (s *ConfigStore) SetConversationSessionWorkerID(aliasOrToken, workerID string) {
	if s == nil {
		return
	}
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), ConsoleConvPrefix)
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
// default.
func (s *ConfigStore) DebugModeFor(aliasOrToken string) bool {
	if s == nil {
		return false
	}
	defer s.lockForRead()()
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), ConsoleConvPrefix)
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
	target := strings.TrimPrefix(strings.TrimSpace(aliasOrToken), ConsoleConvPrefix)
	for alias, entry := range s.cfg.Conversations {
		if alias == target || entry.Token == target {
			entry.DebugMode = enabled
			s.cfg.Conversations[alias] = entry
			return s.saveLocked()
		}
	}
	return fmt.Errorf("conversation %q not found", target)
}

// MergeRemoteConversations merges a conversation map (and optional active conversation alias)
// synced from the remote MCP server or local bridge into this ConfigStore.
func (s *ConfigStore) MergeRemoteConversations(convs map[string]ConversationEntry, activeAlias string) {
	if s == nil || (len(convs) == 0 && strings.TrimSpace(activeAlias) == "") {
		return
	}
	defer s.lockForUpdate()()
	changed := false
	for alias, entry := range convs {
		if strings.TrimSpace(alias) == "" {
			continue
		}
		if entry.Alias == "" {
			entry.Alias = alias
		}
		existing, ok := s.cfg.Conversations[alias]
		if !ok ||
			existing.Token != entry.Token ||
			existing.SessionMode != entry.SessionMode ||
			existing.SessionTerminated != entry.SessionTerminated ||
			existing.SessionWorkerID != entry.SessionWorkerID ||
			existing.HostConversationID != entry.HostConversationID ||
			existing.ParentHostConversationID != entry.ParentHostConversationID ||
			existing.SubagentRole != entry.SubagentRole ||
			existing.DebugMode != entry.DebugMode {
			if ok {
				if entry.SessionWorkerID == "" && existing.SessionWorkerID != "" {
					entry.SessionWorkerID = existing.SessionWorkerID
				}
				entry.Disconnected = existing.Disconnected
			}
			s.cfg.Conversations[alias] = entry
			changed = true
		}
	}
	if act := strings.TrimSpace(activeAlias); act != "" {
		if _, ok := s.cfg.Conversations[act]; ok && s.cfg.ActiveConversation != act {
			s.cfg.ActiveConversation = act
			changed = true
		}
	}
	if changed {
		_ = s.saveLocked()
	}
}

// SanitizeAliasSlug normalizes a conversation or role label into a lowercase slug.
func SanitizeAliasSlug(raw string) string {
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

// IsValidConsoleToken reports whether token is a safe console conversation token.
func IsValidConsoleToken(token string) bool {
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

// FirstNonEmptyStr returns the first string in vals with non-empty trimmed content.
func FirstNonEmptyStr(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}

// TruncateText truncates s to at most maxChars runes, appending an ellipsis when truncated.
func TruncateText(s string, maxChars int) string {
	s = strings.TrimSpace(s)
	runes := []rune(s)
	if len(runes) <= maxChars || maxChars <= 3 {
		return s
	}
	return string(runes[:maxChars-1]) + "…"
}
