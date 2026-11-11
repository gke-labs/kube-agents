package capability

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"reflect"
	"slices"
	"sync"
	"sync/atomic"
	"time"
)

// The tier table as data (#3659): an install defines profile tiers with the
// Tier custom resource, and the verifier, the gateway and the callout learn
// them through LoadTiers. What a tier may do still follows from code, not
// from the resource: a tier's mode picks one of two verb sets, so a resource
// can't name a verb, and the vocabulary stays closed.
//
// What stays compiled in, whatever is loaded:
//   - the three legacy tiers (platform, cluster-admin, developer-team), whose
//     sets nest and which no resource may redefine;
//   - the built-in profile tiers read and unattended, at their sets in
//     tierVerbs. A resource with one of their names changes nothing; the
//     chart's defaults name them only so an install can see them.
//
// So a loaded definition can only add a profile tier, at one of the two
// sets the built-ins already hold. A missing, empty or refused source leaves
// exactly the compiled-in table: it never falls back to anything wider.

// TierMode is how a profile tier runs, which picks its verb set.
type TierMode string

const (
	// TierModeInteractive is a person's tier: task.execute and profile.run,
	// read's set.
	TierModeInteractive TierMode = "interactive"
	// TierModeUnattended is a trigger's tier: profile.run alone,
	// unattended's set.
	TierModeUnattended TierMode = "unattended"
)

// modeVerbs is the verb set each mode gives, the built-in profile tiers'.
var modeVerbs = map[TierMode]Tier{
	TierModeInteractive: TierRead,
	TierModeUnattended:  TierUnattended,
}

// TierDefinition is one Tier resource as the table reads it.
type TierDefinition struct {
	Name string
	Mode TierMode
}

// tierTable is one consistent view of the tiers: each tier's verb set, and
// which tiers are profile tiers.
type tierTable struct {
	verbs   map[Tier]verbSet
	profile map[Tier]bool
}

var activeTiers atomic.Pointer[tierTable]

// tierWatchers are called after each LoadTiers that changes the table.
var (
	tierWatchersMu sync.Mutex
	tierWatchers   []func()
)

// OnTiersChanged registers fn to run after every LoadTiers that changes the
// table, on the loader's goroutine. The callout uses it to re-validate an
// identity map whose profile entries name a tier that loaded after the map
// did (#3659).
func OnTiersChanged(fn func()) {
	tierWatchersMu.Lock()
	defer tierWatchersMu.Unlock()
	tierWatchers = append(tierWatchers, fn)
}

func init() { activeTiers.Store(builtinTierTable()) }

func builtinTierTable() *tierTable {
	t := &tierTable{verbs: make(map[Tier]verbSet, len(tierVerbs)), profile: make(map[Tier]bool, len(profileTiers))}
	for tier, set := range tierVerbs {
		t.verbs[tier] = set
	}
	for tier := range profileTiers {
		t.profile[tier] = true
	}
	return t
}

// tiers is the table in force. Readers take it once per decision, so one
// decision never sees two tables.
func tiers() *tierTable { return activeTiers.Load() }

// LoadTiers installs the compiled-in table plus defs, and returns why each
// definition it skipped was skipped. A skipped definition adds nothing; the
// rest still load. Calling it with nil restores the compiled-in table.
//
// A definition is skipped when its name isn't a DNS label, names a
// compiled-in tier, or repeats an earlier definition, or when its mode is
// neither interactive nor unattended.
func LoadTiers(defs []TierDefinition) []string {
	t := builtinTierTable()
	var skipped []string
	seen := map[string]bool{}
	for _, d := range defs {
		name := Tier(d.Name)
		switch {
		case !validProfileName(d.Name):
			skipped = append(skipped, fmt.Sprintf("tier %q: the name is not a DNS label", d.Name))
		case slices.Contains([]Tier{TierPlatform, TierClusterAdmin, TierDeveloperTeam}, name):
			skipped = append(skipped, fmt.Sprintf("tier %q: a legacy tier is compiled in and can't be redefined", d.Name))
		case profileTiers[name]:
			if modeVerbs[d.Mode] != name {
				skipped = append(skipped, fmt.Sprintf("tier %q: the built-in tier's set is compiled in; mode %q changes nothing", d.Name, d.Mode))
			}
		case seen[d.Name]:
			skipped = append(skipped, fmt.Sprintf("tier %q: defined twice; the first stands", d.Name))
		default:
			base, ok := modeVerbs[d.Mode]
			if !ok {
				skipped = append(skipped, fmt.Sprintf("tier %q: mode %q is not interactive or unattended", d.Name, d.Mode))
				break
			}
			t.verbs[name] = tierVerbs[base]
			t.profile[name] = true
		}
		seen[d.Name] = true
	}
	old := activeTiers.Swap(t)
	if old == nil || !reflect.DeepEqual(old.verbs, t.verbs) || !reflect.DeepEqual(old.profile, t.profile) {
		tierWatchersMu.Lock()
		fns := append([]func(){}, tierWatchers...)
		tierWatchersMu.Unlock()
		for _, fn := range fns {
			fn()
		}
	}
	return skipped
}

// LoadedTiers is the profile tiers in force, sorted: the built-ins and every
// loaded definition.
func LoadedTiers() []Tier {
	t := tiers()
	out := make([]Tier, 0, len(t.profile))
	for tier := range t.profile {
		out = append(out, tier)
	}
	slices.Sort(out)
	return out
}

// IsProfileTier reports whether tier is a profile tier in the table in
// force: a built-in one or a loaded definition.
func IsProfileTier(tier Tier) bool { return tiers().profile[tier] }

// tierFile is the operator-rendered file's shape (the <agent>-a2a-tiers
// ConfigMap's tiers.json): every Tier resource in the install's namespace,
// by name and mode.
type tierFile struct {
	Tiers []struct {
		Name string `json:"name"`
		Mode string `json:"mode"`
	} `json:"tiers"`
}

// ParseTierFile reads a tiers file. A file that isn't the shape is an error,
// so the caller falls back to the compiled-in table instead of guessing.
func ParseTierFile(raw []byte) ([]TierDefinition, error) {
	var f tierFile
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&f); err != nil {
		return nil, fmt.Errorf("tiers file: %w", err)
	}
	out := make([]TierDefinition, 0, len(f.Tiers))
	for _, t := range f.Tiers {
		out = append(out, TierDefinition{Name: t.Name, Mode: TierMode(t.Mode)})
	}
	return out, nil
}

// LoadTierFile reads path and installs it with LoadTiers. A file that can't
// be read or parsed installs the compiled-in table (LoadTiers(nil)), so
// trouble with the source can only narrow the tiers, never widen them. It
// returns the skipped definitions' reasons and the read or parse error.
func LoadTierFile(path string) ([]string, error) {
	raw, err := os.ReadFile(path) // #nosec G304 -- the operator-rendered tiers mount
	if err != nil {
		LoadTiers(nil)
		return nil, err
	}
	defs, err := ParseTierFile(raw)
	if err != nil {
		LoadTiers(nil)
		return nil, err
	}
	return LoadTiers(defs), nil
}

// WatchTierFile loads path now and again whenever its content changes,
// checked every interval until ctx ends. The kubelet updates a ConfigMap
// mount by swapping a symlink, so the check compares the bytes, not a
// stamp. log may be nil.
func WatchTierFile(ctx context.Context, path string, interval time.Duration, log *slog.Logger) {
	if log == nil {
		log = slog.Default()
	}
	var last []byte
	load := func() {
		raw, _ := os.ReadFile(path) // #nosec G304 -- the operator-rendered tiers mount
		if last != nil && bytes.Equal(raw, last) {
			return
		}
		last = append([]byte{}, raw...)
		if last == nil {
			last = []byte{}
		}
		skipped, err := LoadTierFile(path)
		if err != nil {
			log.Warn("tiers file unusable; serving the compiled-in tiers", "path", path, "err", err)
			return
		}
		for _, s := range skipped {
			log.Warn("tier definition skipped", "reason", s)
		}
		log.Info("tiers loaded", "path", path, "tiers", LoadedTiers())
	}
	load()
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			load()
		}
	}
}

// TiersFileEnv names the env var carrying the tiers file's path, which the
// operator sets on the verifier, the gateway and the callout beside the
// mount.
const TiersFileEnv = "A2A_TIERS_FILE"

// tierPollInterval is how often WatchTierFile re-reads the file: the kubelet
// itself takes up to a minute to update a ConfigMap mount.
const tierPollInterval = 30 * time.Second

// StartTierFile loads the tiers file named by TiersFileEnv, synchronously so
// the first request sees it, then watches it on a goroutine until ctx ends.
// Unset, the compiled-in table serves. log may be nil.
func StartTierFile(ctx context.Context, getenv func(string) string, log *slog.Logger) {
	path := getenv(TiersFileEnv)
	if path == "" {
		return
	}
	if log == nil {
		log = slog.Default()
	}
	if _, err := LoadTierFile(path); err != nil {
		log.Warn("tiers file unusable at start; serving the compiled-in tiers", "path", path, "err", err)
	}
	go WatchTierFile(ctx, path, tierPollInterval, log)
}
