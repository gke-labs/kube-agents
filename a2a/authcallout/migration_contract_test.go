package authcallout

import (
	"os"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// The bus migration's stream identity as the operator renders it mid-run
// (k8s-operator: TestRenderedBusMigrationFixture writes the fixture), run
// through the callout and a real server. It is the operator's snapshot,
// restore and rollback pods, and the only principal holding SNAPSHOT,
// RESTORE or DELETE.
const (
	migrationFixturePath   = "testdata/rendered-identity-map-migration.json"
	renderedMigrationSA    = "system:serviceaccount:kubeagents-system:agent-bus-stream-3-1"
	renderedMigrationUser  = "migrate-stream-3-1"
	tokenMigrationSnapshot = "token-for-the-rendered-migration-stream-serviceaccount-padded"
)

func renderedMigrationMap(t *testing.T) string {
	t.Helper()
	raw, err := os.ReadFile(migrationFixturePath)
	if err != nil {
		t.Fatalf("reading %s: %v\nRegenerate with: go test ./internal/controller/ -run 'Fixture' -update (in k8s-operator)", migrationFixturePath, err)
	}
	return string(raw)
}

// The map parses, and the stream identity carries the run's deadline.
func TestTheOperatorsMigrationMapParses(t *testing.T) {
	m, err := ParseIdentityMap([]byte(renderedMigrationMap(t)))
	if err != nil {
		t.Fatalf("the operator's map mid-migration does not parse: %v", err)
	}
	id, ok := m.Lookup(renderedMigrationSA)
	if !ok {
		t.Fatal("the operator's map has no stream identity")
	}
	if id.User != renderedMigrationUser || id.NotAfter == nil || !id.NotAfter.Equal(time.Date(2099, 1, 1, 0, 0, 0, 0, time.UTC)) {
		t.Errorf("stream identity entry = user %q notAfter %v", id.User, id.NotAfter)
	}
}

// What the stream identity may publish, against the rendered grants, and
// the nearest things it may not: every verb only on the provisioned streams
// by name, restore chunks only on those names (the server derives the
// chunk subject from the config's stream name, so this is what keeps a
// RESTORE request naming another stream from receiving data), no purge,
// leader step-down, stream create or message delete, no task subject.
func TestTheRenderedStreamIdentityReachesItsWorkAndNothingNear(t *testing.T) {
	h := startHarness(t, renderedMigrationMap(t), map[string]Attested{
		tokenMigrationSnapshot: {ServiceAccount: renderedMigrationSA},
	})
	nc, violations := h.connectAs(t, renderedMigrationUser, tokenMigrationSnapshot)
	checkPublish(t, nc, violations, map[string]bool{
		"$JS.API.STREAM.INFO.TASKS":            false,
		"$JS.API.STREAM.SNAPSHOT.TASKS":        false,
		"$JS.API.STREAM.SNAPSHOT.KV_cap":       false,
		"$JS.SNAPSHOT.ACK.TASKS.abc":           false,
		"$JS.API.STREAM.RESTORE.TASKS":         false,
		"$JS.SNAPSHOT.RESTORE.TASKS.abc":       false,
		"$JS.API.STREAM.UPDATE.KV_cap":         false,
		"$JS.API.STREAM.DELETE.TASKS":          false,
		"$JS.API.STREAM.RESTORE.OTHER":         true,
		"$JS.SNAPSHOT.RESTORE.OTHER.abc":       true,
		"$JS.API.STREAM.DELETE.OTHER":          true,
		"$JS.API.STREAM.SNAPSHOT.OTHER":        true,
		"$JS.SNAPSHOT.ACK.OTHER.abc":           true,
		"$JS.API.STREAM.PURGE.TASKS":           true,
		"$JS.API.STREAM.LEADER.STEPDOWN.TASKS": true,
		"$JS.API.STREAM.CREATE.TASKS":          true,
		"$JS.API.STREAM.MSG.DELETE.TASKS":      true,
		"a2a.tasks.x":                          true,
	})
}

// Past its notAfter the same identity does not connect at all.
func TestTheRenderedStreamIdentityPastItsDeadlineCannotConnect(t *testing.T) {
	expired := strings.Replace(renderedMigrationMap(t), "2099-01-01T00:00:00Z", time.Now().Add(-time.Minute).UTC().Format(time.RFC3339), 1)
	h := startHarness(t, expired, map[string]Attested{
		tokenMigrationSnapshot: {ServiceAccount: renderedMigrationSA},
	})
	nc, err := nats.Connect(h.url, h.dial, nats.Token(tokenMigrationSnapshot), nats.CustomInboxPrefix("_INBOX."+renderedMigrationUser), nats.MaxReconnects(0))
	if err == nil {
		nc.Close()
		t.Fatal("connected as the stream identity past its notAfter")
	}
	if !strings.Contains(strings.ToLower(err.Error()), "authorization") {
		t.Errorf("connect error %v, want an authorization refusal", err)
	}
}
