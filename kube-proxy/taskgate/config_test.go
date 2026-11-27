package taskgate

import (
	"bytes"
	"log/slog"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/taskcred"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	testIdentityMap = `{"` + testWorker + `": {"profile": "` + testProfile + `", "tier": "` + testTier + `"}}`
	testEntry       = `{"profile":"a","tier":"b"}`
	testAudience    = "kubeagents-profile-audience"
	testSecondUser  = "system:serviceaccount:agents:fixer-write"
	testThirdUser   = "system:serviceaccount:a-first:worker"
)

func envOf(values map[string]string) func(string) string {
	return func(name string) string { return values[name] }
}

func TestParseIdentityMap(t *testing.T) {
	got, err := ParseIdentityMap("\n " + `{
		"system:serviceaccount:agents:auditor-read": {"profile": "auditor", "tier": "read"},
		"system:serviceaccount:agents:fixer.write": {"tier": "write", "profile": "fixer"}
	}` + "\n")
	if err != nil {
		t.Fatalf("ParseIdentityMap: %v", err)
	}
	want := map[string]Identity{
		"system:serviceaccount:agents:auditor-read": {Profile: "auditor", Tier: "read"},
		"system:serviceaccount:agents:fixer.write":  {Profile: "fixer", Tier: "write"},
	}
	if len(got) != len(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	for user, identity := range want {
		if got[user] != identity {
			t.Errorf("%s = %+v, want %+v", user, got[user], identity)
		}
	}

	for _, blank := range []string{"", "  ", "\n\t"} {
		if got, err := ParseIdentityMap(blank); err != nil || len(got) != 0 {
			t.Errorf("ParseIdentityMap(%q) = %v, %v; want an empty map", blank, got, err)
		}
	}
	if got, err := ParseIdentityMap("{}"); err != nil || len(got) != 0 {
		t.Errorf("ParseIdentityMap({}) = %v, %v; want an empty map", got, err)
	}
}

func TestParseIdentityMapRefuses(t *testing.T) {
	const user = `"system:serviceaccount:ns:sa"`
	for name, raw := range map[string]string{
		"not JSON":                    `{`,
		"a list":                      `[]`,
		"a string":                    `"x"`,
		"null":                        `null`,
		"trailing data":               `{} {}`,
		"trailing text":               `{} x`,
		"not UTF-8":                   "{\"\xff\": " + testEntry + "}",
		"a key that is not a user":    `{"alice": ` + testEntry + `}`,
		"a user with no name":         `{"system:serviceaccount:ns:": ` + testEntry + `}`,
		"an uppercase namespace":      `{"system:serviceaccount:NS:sa": ` + testEntry + `}`,
		"a user with a trailing line": `{"system:serviceaccount:ns:sa\n": ` + testEntry + `}`,
		"a user twice":                `{` + user + `: ` + testEntry + `, ` + user + `: ` + testEntry + `}`,
		"an entry that is a string":   `{` + user + `: "auditor"}`,
		"an entry that is null":       `{` + user + `: null}`,
		"an entry that is a list":     `{` + user + `: ["a","b"]}`,
		"no tier":                     `{` + user + `: {"profile":"a"}}`,
		"no profile":                  `{` + user + `: {"tier":"b"}}`,
		"an unknown key":              `{` + user + `: {"profile":"a","tier":"b","role":"c"}}`,
		"a case-variant key":          `{` + user + `: {"Profile":"a","tier":"b"}}`,
		"a key twice":                 `{` + user + `: {"profile":"a","profile":"a","tier":"b"}}`,
		"a profile that is a number":  `{` + user + `: {"profile":1,"tier":"b"}}`,
		"a tier that is null":         `{` + user + `: {"profile":"a","tier":null}}`,
		"a tier that is an object":    `{` + user + `: {"profile":"a","tier":{}}}`,
		"a dotted profile":            `{` + user + `: {"profile":"a.b","tier":"b"}}`,
		"an uppercase tier":           `{` + user + `: {"profile":"a","tier":"Read"}}`,
		"an empty profile":            `{` + user + `: {"profile":"","tier":"b"}}`,
		"a profile past 63 bytes":     `{` + user + `: {"profile":"` + strings.Repeat("a", 64) + `","tier":"b"}}`,
	} {
		if got, err := ParseIdentityMap(raw); err == nil {
			t.Errorf("%s: ParseIdentityMap(%q) = %v, want an error", name, raw, got)
		}
	}
}

func TestLoadConfig(t *testing.T) {
	_, pub := newTestKey(t)
	_, second := newTestKey(t)
	_, third := newTestKey(t)

	// Off: the map is unset, blank or empty, and nothing else is read.
	for name, env := range map[string]map[string]string{
		"nothing set":                 {},
		"a blank map":                 {IdentitiesEnv: "  "},
		"an empty map":                {IdentitiesEnv: "{}"},
		"keys alone":                  {SigningKeysEnv: pub},
		"a malformed key list alone":  {SigningKeysEnv: "not-a-key"},
		"a malformed lifetime alone":  {MaxLifetimeEnv: "soon"},
		"an empty map and a bad list": {IdentitiesEnv: "{}", SigningKeysEnv: "not-a-key"},
		"a profile audience alone":    {ProfileAudienceEnv: testAudience},
		"an empty map and audience":   {IdentitiesEnv: "{}", ProfileAudienceEnv: testAudience},
		"a blank map and audience":    {IdentitiesEnv: " ", ProfileAudienceEnv: testAudience, SigningKeysEnv: pub},
	} {
		cfg, err := LoadConfig(envOf(env))
		if err != nil || cfg != nil {
			t.Errorf("%s: LoadConfig = %v, %v; want the gate off", name, cfg, err)
		}
		hook, err := FromEnv(envOf(env), slog.New(slog.DiscardHandler))
		if err != nil || hook != nil {
			t.Errorf("%s: FromEnv = (set=%v, %v); want no hook", name, hook != nil, err)
		}
	}

	// On.
	for name, tc := range map[string]struct {
		env      map[string]string
		keys     int
		lifetime time.Duration
	}{
		"one key":         {map[string]string{IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: pub}, 1, taskcred.DefaultMaxLifetime},
		"two keys":        {map[string]string{IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: " " + pub + " , " + second + "\n"}, 2, taskcred.DefaultMaxLifetime},
		"no key":          {map[string]string{IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience}, 0, taskcred.DefaultMaxLifetime},
		"a lifetime":      {map[string]string{IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: pub, MaxLifetimeEnv: " 2h30m\n"}, 1, 150 * time.Minute},
		"the ceiling":     {map[string]string{IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: pub, MaxLifetimeEnv: "168h"}, 1, maxLifetimeCeiling},
		"a blank ceiling": {map[string]string{IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: pub, MaxLifetimeEnv: "  "}, 1, taskcred.DefaultMaxLifetime},
	} {
		cfg, err := LoadConfig(envOf(tc.env))
		if err != nil || cfg == nil {
			t.Errorf("%s: LoadConfig = %v, %v; want a config", name, cfg, err)
			continue
		}
		if len(cfg.SigningKeys) != tc.keys || cfg.MaxLifetime != tc.lifetime || cfg.Identities[testWorker] != (Identity{Profile: testProfile, Tier: testTier}) {
			t.Errorf("%s: config = %+v, want %d keys, lifetime %s and the worker's identity", name, *cfg, tc.keys, tc.lifetime)
		}
		if cfg.ProfileAudience != testAudience || !slices.Equal(cfg.WorkerUsernames(), []string{testWorker}) {
			t.Errorf("%s: profile audience %q for %v, want %q for the map's one worker", name, cfg.ProfileAudience, cfg.WorkerUsernames(), testAudience)
		}
	}

	// A startup error.
	for name, env := range map[string]map[string]string{
		"a malformed map":       {IdentitiesEnv: `{"alice": ` + testEntry + `}`, SigningKeysEnv: pub, ProfileAudienceEnv: testAudience},
		"a map that is a list":  {IdentitiesEnv: `[]`, ProfileAudienceEnv: testAudience},
		"no profile audience":   {IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub},
		"a blank audience":      {IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub, ProfileAudienceEnv: " \n"},
		"no audience, no key":   {IdentitiesEnv: testIdentityMap},
		"a key that is not one": {IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: "not-a-key"},
		"an empty key element":  {IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: pub + ",," + second},
		"one key twice":         {IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: pub + "," + pub},
		"three keys":            {IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: pub + "," + second + "," + third},
		"a lifetime of words":   {IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: pub, MaxLifetimeEnv: "soon"},
		"a bare number":         {IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: pub, MaxLifetimeEnv: "3600"},
		"a zero lifetime":       {IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: pub, MaxLifetimeEnv: "0s"},
		"a negative lifetime":   {IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: pub, MaxLifetimeEnv: "-1h"},
		"past the ceiling":      {IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience, SigningKeysEnv: pub, MaxLifetimeEnv: "169h"},
	} {
		if cfg, err := LoadConfig(envOf(env)); err == nil {
			t.Errorf("%s: LoadConfig = %+v, want an error", name, cfg)
		}
		if hook, err := FromEnv(envOf(env), slog.New(slog.DiscardHandler)); err == nil || hook != nil {
			t.Errorf("%s: FromEnv = (set=%v, %v); want an error and no hook", name, hook != nil, err)
		}
	}
}

// A map that names workers needs the audience the proxy admits them for, and the error
// names both variables; the audience is read with the whitespace around it dropped.
func TestLoadConfigProfileAudience(t *testing.T) {
	_, err := LoadConfig(envOf(map[string]string{IdentitiesEnv: testIdentityMap}))
	if err == nil || !strings.Contains(err.Error(), ProfileAudienceEnv) || !strings.Contains(err.Error(), IdentitiesEnv) {
		t.Errorf("LoadConfig with workers and no audience: err = %v, want one naming %s and %s", err, ProfileAudienceEnv, IdentitiesEnv)
	}
	cfg, err := LoadConfig(envOf(map[string]string{IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: " " + testAudience + "\n"}))
	if err != nil || cfg == nil || cfg.ProfileAudience != testAudience {
		t.Errorf("LoadConfig with a padded audience = %+v, %v; want the audience %q", cfg, err, testAudience)
	}
}

// The usernames the proxy admits for the profile audience are the identity map's keys, all
// of them, sorted, in a slice that is not the configuration's.
func TestWorkerUsernames(t *testing.T) {
	cfg := Config{Identities: map[string]Identity{
		testWorker:     {Profile: testProfile, Tier: testTier},
		testSecondUser: {Profile: "fixer", Tier: "write"},
		testThirdUser:  {Profile: testProfile, Tier: testTier},
	}}
	want := []string{testThirdUser, testWorker, testSecondUser}
	got := cfg.WorkerUsernames()
	if !slices.Equal(got, want) {
		t.Fatalf("WorkerUsernames = %v, want %v", got, want)
	}
	got[0] = testNonWorker
	if again := cfg.WorkerUsernames(); !slices.Equal(again, want) {
		t.Errorf("WorkerUsernames after the caller changed its slice = %v, want %v", again, want)
	}
	if _, named := cfg.Identities[testNonWorker]; named || len(cfg.Identities) != len(want) {
		t.Errorf("the identity map changed: %v", cfg.Identities)
	}
	if got := (Config{}).WorkerUsernames(); len(got) != 0 {
		t.Errorf("WorkerUsernames of no map = %v, want none", got)
	}
}

// Workers named and no key: the binary starts, says so once, and refuses every worker.
func TestFromEnvWithNoKeyStartsAndWarnsOnce(t *testing.T) {
	var logs bytes.Buffer
	hook, err := FromEnv(envOf(map[string]string{IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience}), slog.New(slog.NewJSONHandler(&logs, nil)))
	if err != nil || hook == nil {
		t.Fatalf("FromEnv = (set=%v, %v); want a hook", hook != nil, err)
	}
	if got := strings.Count(logs.String(), SigningKeysEnv); got != 1 {
		t.Errorf("startup log names %s %d times, want once: %s", SigningKeysEnv, got, logs.String())
	}
	logs.Reset()

	assertRefused(t, hook(testAttrs(testWorker, testClusterID, testExtra())), ReasonTaskCredential)
	if err := hook(testAttrs(testNonWorker, testClusterID, nil)); err != nil {
		t.Errorf("non-worker refused: %v", err)
	}
	if logs.Len() != 0 {
		t.Errorf("a request logged again: %s", logs.String())
	}
}

// The worker limits default, take a non-negative integer, and are an error otherwise; with
// no worker in the map they are not read at all.
func TestLoadConfigWorkerLimits(t *testing.T) {
	gate := func(more map[string]string) func(string) string {
		env := map[string]string{IdentitiesEnv: testIdentityMap, ProfileAudienceEnv: testAudience}
		for k, v := range more {
			env[k] = v
		}
		return envOf(env)
	}
	defaults := k8sapiproxy.CallerLimits{
		MaxInFlight: DefaultWorkerMaxInFlight, MaxLongRunning: DefaultWorkerMaxLongRunning,
		GroupMaxInFlight: DefaultWorkerGroupMaxInFlight, GroupMaxLongRunning: DefaultWorkerGroupMaxLongRunning,
	}
	oneSet := defaults
	oneSet.MaxLongRunning = 1
	for name, tc := range map[string]struct {
		env  map[string]string
		want k8sapiproxy.CallerLimits
	}{
		"unset": {nil, defaults},
		"blank": {map[string]string{WorkerMaxInFlightEnv: "  ", WorkerMaxLongRunningEnv: "\n", WorkerGroupMaxInFlightEnv: "", WorkerGroupMaxLongRunningEnv: " "}, defaults},
		"all set": {
			map[string]string{WorkerMaxInFlightEnv: " 4 ", WorkerMaxLongRunningEnv: "12", WorkerGroupMaxInFlightEnv: "10\n", WorkerGroupMaxLongRunningEnv: "20"},
			k8sapiproxy.CallerLimits{MaxInFlight: 4, MaxLongRunning: 12, GroupMaxInFlight: 10, GroupMaxLongRunning: 20},
		},
		"one set": {map[string]string{WorkerMaxLongRunningEnv: "1"}, oneSet},
		"zero, no limits": {
			map[string]string{WorkerMaxInFlightEnv: "0", WorkerMaxLongRunningEnv: "0", WorkerGroupMaxInFlightEnv: "0", WorkerGroupMaxLongRunningEnv: "0"},
			k8sapiproxy.CallerLimits{},
		},
	} {
		cfg, err := LoadConfig(gate(tc.env))
		if err != nil || cfg == nil {
			t.Errorf("%s: LoadConfig = %v, %v; want a config", name, cfg, err)
			continue
		}
		if cfg.WorkerLimits != tc.want {
			t.Errorf("%s: worker limits = %+v, want %+v", name, cfg.WorkerLimits, tc.want)
		}
	}

	for _, env := range []string{WorkerMaxInFlightEnv, WorkerMaxLongRunningEnv, WorkerGroupMaxInFlightEnv, WorkerGroupMaxLongRunningEnv} {
		for _, raw := range []string{"many", "-1", "1.5", "4 requests", "0x10"} {
			cfg, err := LoadConfig(gate(map[string]string{env: raw}))
			if err == nil || !strings.Contains(err.Error(), env) {
				t.Errorf("%s=%q: LoadConfig = %+v, %v; want an error naming the variable", env, raw, cfg, err)
			}
			if cfg, err := LoadConfig(envOf(map[string]string{env: raw})); err != nil || cfg != nil {
				t.Errorf("%s=%q with no identity map: LoadConfig = %v, %v; want the gate off and the value unread", env, raw, cfg, err)
			}
		}
	}

	// Against the shared cap's default of 24: all workers together leave the other callers a
	// third of it, and one pod cannot take the workers' whole share.
	const sharedDefault = 24
	if got, want := sharedDefault-DefaultWorkerGroupMaxInFlight, sharedDefault/3; got < want {
		t.Errorf("DefaultWorkerGroupMaxInFlight = %d leaves %d of the %d shared slots, want at least %d", DefaultWorkerGroupMaxInFlight, got, sharedDefault, want)
	}
	if DefaultWorkerMaxInFlight >= DefaultWorkerGroupMaxInFlight || DefaultWorkerMaxLongRunning >= DefaultWorkerGroupMaxLongRunning {
		t.Errorf("one pod's defaults (%d, %d) are not under the group's (%d, %d)",
			DefaultWorkerMaxInFlight, DefaultWorkerMaxLongRunning, DefaultWorkerGroupMaxInFlight, DefaultWorkerGroupMaxLongRunning)
	}
}

func TestConfigCallerLimits(t *testing.T) {
	const pod = "auditor-read-x7k2p"
	identities := map[string]Identity{
		testWorker:     {Profile: testProfile, Tier: testTier},
		testSecondUser: {Profile: "fixer", Tier: "write"},
	}
	podOf := func(username string, pods ...string) *k8sapiproxy.CallerIdentity {
		return &k8sapiproxy.CallerIdentity{Username: username, Extra: map[string][]string{podNameExtra: pods}}
	}
	limits := Config{
		Identities:   identities,
		WorkerLimits: k8sapiproxy.CallerLimits{MaxInFlight: 3, MaxLongRunning: 2, GroupMaxInFlight: 9, GroupMaxLongRunning: 7},
	}.CallerLimits()

	want := k8sapiproxy.CallerLimits{
		Key: testWorker + "/" + pod, MaxInFlight: 3, MaxLongRunning: 2,
		Group: workerGroup, GroupMaxInFlight: 9, GroupMaxLongRunning: 7,
	}
	if got := limits(podOf(testWorker, pod)); got != want {
		t.Errorf("a worker pod's limits = %+v, want %+v", got, want)
	}
	// Another pod of the ServiceAccount, and another worker's pod of the same name, are
	// counted apart; every worker is in the one group.
	other, second := limits(podOf(testWorker, "auditor-read-9zq4c")), limits(podOf(testSecondUser, pod))
	if other.Key == want.Key || second.Key == want.Key || other.Key == second.Key {
		t.Errorf("keys %q, %q and %q, want three different ones", want.Key, other.Key, second.Key)
	}
	if other.Group != workerGroup || second.Group != workerGroup {
		t.Errorf("groups %q and %q, want %q for every worker", other.Group, second.Group, workerGroup)
	}
	// A TokenReview that names no pod, or more than one, falls back to the username.
	want.Key = ""
	for name, caller := range map[string]*k8sapiproxy.CallerIdentity{
		"no extra":    {Username: testWorker},
		"no pod":      podOf(testWorker),
		"two pods":    podOf(testWorker, pod, "another"),
		"a blank pod": podOf(testWorker, ""),
	} {
		if got := limits(caller); got != want {
			t.Errorf("%s: limits = %+v, want %+v, keyed by the username", name, got, want)
		}
	}
	for name, caller := range map[string]*k8sapiproxy.CallerIdentity{
		"a caller outside the map": podOf("system:serviceaccount:agents:shell", pod),
		"a nameless caller":        {},
		"no caller":                nil,
	} {
		if got := limits(caller); got != (k8sapiproxy.CallerLimits{}) {
			t.Errorf("%s: limits = %+v, want none", name, got)
		}
	}

	// A limit of zero reaches the proxy as "no limit", which its per-caller flags do not
	// replace, where a zero field would take the flag's value. The group has no flag.
	unlimited := Config{Identities: identities}.CallerLimits()(podOf(testWorker, pod))
	if unlimited.MaxInFlight >= 0 || unlimited.MaxLongRunning >= 0 {
		t.Errorf("limits of zero = (%d, %d), want both negative", unlimited.MaxInFlight, unlimited.MaxLongRunning)
	}
	if unlimited.GroupMaxInFlight > 0 || unlimited.GroupMaxLongRunning > 0 {
		t.Errorf("group limits of zero = (%d, %d), want none", unlimited.GroupMaxInFlight, unlimited.GroupMaxLongRunning)
	}
}
