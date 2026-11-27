package taskgate

import (
	"bytes"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/taskcred"
)

const (
	testIdentityMap = `{"` + testWorker + `": {"profile": "` + testProfile + `", "tier": "` + testTier + `"}}`
	testEntry       = `{"profile":"a","tier":"b"}`
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
		"one key":         {map[string]string{IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub}, 1, taskcred.DefaultMaxLifetime},
		"two keys":        {map[string]string{IdentitiesEnv: testIdentityMap, SigningKeysEnv: " " + pub + " , " + second + "\n"}, 2, taskcred.DefaultMaxLifetime},
		"no key":          {map[string]string{IdentitiesEnv: testIdentityMap}, 0, taskcred.DefaultMaxLifetime},
		"a lifetime":      {map[string]string{IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub, MaxLifetimeEnv: " 2h30m\n"}, 1, 150 * time.Minute},
		"the ceiling":     {map[string]string{IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub, MaxLifetimeEnv: "168h"}, 1, maxLifetimeCeiling},
		"a blank ceiling": {map[string]string{IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub, MaxLifetimeEnv: "  "}, 1, taskcred.DefaultMaxLifetime},
	} {
		cfg, err := LoadConfig(envOf(tc.env))
		if err != nil || cfg == nil {
			t.Errorf("%s: LoadConfig = %v, %v; want a config", name, cfg, err)
			continue
		}
		if len(cfg.SigningKeys) != tc.keys || cfg.MaxLifetime != tc.lifetime || cfg.Identities[testWorker] != (Identity{Profile: testProfile, Tier: testTier}) {
			t.Errorf("%s: config = %+v, want %d keys, lifetime %s and the worker's identity", name, *cfg, tc.keys, tc.lifetime)
		}
	}

	// A startup error.
	for name, env := range map[string]map[string]string{
		"a malformed map":       {IdentitiesEnv: `{"alice": ` + testEntry + `}`, SigningKeysEnv: pub},
		"a map that is a list":  {IdentitiesEnv: `[]`},
		"a key that is not one": {IdentitiesEnv: testIdentityMap, SigningKeysEnv: "not-a-key"},
		"an empty key element":  {IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub + ",," + second},
		"one key twice":         {IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub + "," + pub},
		"three keys":            {IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub + "," + second + "," + third},
		"a lifetime of words":   {IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub, MaxLifetimeEnv: "soon"},
		"a bare number":         {IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub, MaxLifetimeEnv: "3600"},
		"a zero lifetime":       {IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub, MaxLifetimeEnv: "0s"},
		"a negative lifetime":   {IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub, MaxLifetimeEnv: "-1h"},
		"past the ceiling":      {IdentitiesEnv: testIdentityMap, SigningKeysEnv: pub, MaxLifetimeEnv: "169h"},
	} {
		if cfg, err := LoadConfig(envOf(env)); err == nil {
			t.Errorf("%s: LoadConfig = %+v, want an error", name, cfg)
		}
		if hook, err := FromEnv(envOf(env), slog.New(slog.DiscardHandler)); err == nil || hook != nil {
			t.Errorf("%s: FromEnv = (set=%v, %v); want an error and no hook", name, hook != nil, err)
		}
	}
}

// Workers named and no key: the binary starts, says so once, and refuses every worker.
func TestFromEnvWithNoKeyStartsAndWarnsOnce(t *testing.T) {
	var logs bytes.Buffer
	hook, err := FromEnv(envOf(map[string]string{IdentitiesEnv: testIdentityMap}), slog.New(slog.NewJSONHandler(&logs, nil)))
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
