package taskgate

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/taskcred"
)

// fixturesPath is the file a2a/taskcred and the broker's Python verifier are both held to:
// credentials for the credential-proxy audience, each with the verdict taskcred.Verify
// gives it (go_ok).
const fixturesPath = "../../a2a/taskcred/testdata/credential_proxy_fixtures.json"

type fixtureFile struct {
	Audience           string    `json:"audience"`
	Trusted            []string  `json:"trusted"`
	MaxLifetimeSeconds int64     `json:"max_lifetime_seconds"`
	SkewSeconds        int64     `json:"skew_seconds"`
	Cases              []fixture `json:"cases"`
}

type fixture struct {
	Name    string   `json:"name"`
	Token   string   `json:"token"`
	Now     int64    `json:"now"`
	GoOK    bool     `json:"go_ok"`
	Trusted []string `json:"trusted,omitempty"`
}

// The gate verifies with taskcred.Verify, for the audience, the skew and the ceiling the
// fixtures were written for, so its verdict on each is Verify's: a credential Verify
// refuses is refused here, and one it accepts is admitted when it names a cluster and the
// caller is the pod, profile and tier it was minted for. Where the Python verifier is
// stricter than Verify (proxy_ok false beside go_ok true: a non-canonical encoding, a
// JSON null, an extra header key), the gate follows Verify.
func TestSharedFixtures(t *testing.T) {
	raw, err := os.ReadFile(fixturesPath)
	if err != nil {
		t.Fatalf("the shared fixtures are not readable: %v", err)
	}
	var file fixtureFile
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	if file.Audience != string(taskcred.AudienceCredentialProxy) {
		t.Fatalf("fixtures are for audience %q, the gate verifies %q", file.Audience, taskcred.AudienceCredentialProxy)
	}
	if skew := time.Duration(file.SkewSeconds) * time.Second; skew != taskcred.Skew {
		t.Fatalf("fixtures assume a skew of %s, the gate verifies with %s", skew, taskcred.Skew)
	}
	if len(file.Cases) == 0 {
		t.Fatal("the shared fixtures hold no case")
	}
	lifetime := time.Duration(file.MaxLifetimeSeconds) * time.Second

	var admitted, refused int
	for _, tc := range file.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			keys := tc.Trusted
			if keys == nil {
				keys = file.Trusted
			}
			now := time.Unix(tc.Now, 0)
			claims, verr := taskcred.Verify(tc.Token, keys, taskcred.AudienceCredentialProxy, now, lifetime, taskcred.Skew)
			if (verr == nil) != tc.GoOK {
				t.Fatalf("taskcred.Verify = %v, the fixture says go_ok=%v", verr, tc.GoOK)
			}

			// The caller is the pod, profile and tier the credential names, when it
			// verifies, so what is left to refuse it for is the credential itself.
			identity := Identity{Profile: testProfile, Tier: testTier}
			extra := testExtra()
			clusterID := testClusterID
			if verr == nil {
				identity = Identity{Profile: claims.Profile, Tier: claims.Tier}
				extra = map[string][]string{podNameExtra: {claims.Pod}, podUIDExtra: {claims.PodUID}}
				if claims.Target != nil {
					clusterID = fmt.Sprintf(clusterIDFormat, claims.Target.Project, claims.Target.Location, claims.Target.Cluster)
				}
			}
			hook := New(Config{Identities: map[string]Identity{testWorker: identity}, SigningKeys: keys, MaxLifetime: lifetime},
				slog.New(slog.DiscardHandler), func() time.Time { return now })

			err := hook(testAttrs(testWorker, clusterID, extra, tc.Token))
			if verr == nil && claims.Target != nil {
				admitted++
				if err != nil {
					t.Fatalf("refused a credential taskcred.Verify accepts for this caller and cluster: %v", err)
				}
				// The same credential reaches no other cluster.
				assertRefused(t, hook(testAttrs(testWorker, testOtherID, extra, tc.Token)), ReasonTaskTarget, tc.Token)
				return
			}
			refused++
			assertRefused(t, err, ReasonTaskCredential, tc.Token)
		})
	}
	if admitted == 0 || refused == 0 {
		t.Errorf("admitted %d and refused %d fixtures; want some of each", admitted, refused)
	}
}
