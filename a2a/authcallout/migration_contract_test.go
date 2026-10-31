package authcallout

import (
	"os"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// The bus migration's snapshot principal as the operator renders it mid-run
// (k8s-operator: TestRenderedBusMigrationFixture writes the fixture), run
// through the callout and a real server.
const (
	migrationFixturePath   = "testdata/rendered-identity-map-migration.json"
	renderedMigrationSA    = "system:serviceaccount:kubeagents-system:agent-bus-snapshot-3-1"
	renderedMigrationUser  = "migrate-snapshot-3-1"
	tokenMigrationSnapshot = "token-for-the-rendered-migration-snapshot-serviceaccount-pad"
)

func renderedMigrationMap(t *testing.T) string {
	t.Helper()
	raw, err := os.ReadFile(migrationFixturePath)
	if err != nil {
		t.Fatalf("reading %s: %v\nRegenerate with: go test ./internal/controller/ -run 'Fixture' -update (in k8s-operator)", migrationFixturePath, err)
	}
	return string(raw)
}

// The map parses, and the snapshot principal carries the run's deadline.
func TestTheOperatorsMigrationMapParses(t *testing.T) {
	m, err := ParseIdentityMap([]byte(renderedMigrationMap(t)))
	if err != nil {
		t.Fatalf("the operator's map mid-migration does not parse: %v", err)
	}
	id, ok := m.Lookup(renderedMigrationSA)
	if !ok {
		t.Fatal("the operator's map has no snapshot principal")
	}
	if id.User != renderedMigrationUser || id.NotAfter == nil || !id.NotAfter.Equal(time.Date(2099, 1, 1, 0, 0, 0, 0, time.UTC)) {
		t.Errorf("snapshot entry = user %q notAfter %v", id.User, id.NotAfter)
	}
}

// What the snapshot step may publish, against the rendered grants, and the
// nearest things it may not: no restore, delete, update or purge, no stream
// outside the provisioned set, no task subject.
func TestTheRenderedSnapshotPrincipalReachesItsStepAndNothingNear(t *testing.T) {
	h := startHarness(t, renderedMigrationMap(t), map[string]Attested{
		tokenMigrationSnapshot: {ServiceAccount: renderedMigrationSA},
	})
	nc, violations := h.connectAs(t, renderedMigrationUser, tokenMigrationSnapshot)
	checkPublish(t, nc, violations, map[string]bool{
		"$JS.API.STREAM.INFO.TASKS":            false,
		"$JS.API.STREAM.SNAPSHOT.TASKS":        false,
		"$JS.API.STREAM.SNAPSHOT.KV_cap":       false,
		"$JS.SNAPSHOT.ACK.TASKS.abc":           false,
		"$JS.API.STREAM.RESTORE.TASKS":         true,
		"$JS.SNAPSHOT.RESTORE.TASKS.abc":       true,
		"$JS.API.STREAM.DELETE.TASKS":          true,
		"$JS.API.STREAM.UPDATE.TASKS":          true,
		"$JS.API.STREAM.PURGE.TASKS":           true,
		"$JS.API.STREAM.LEADER.STEPDOWN.TASKS": true,
		"$JS.API.STREAM.SNAPSHOT.OTHER":        true,
		"$JS.SNAPSHOT.ACK.OTHER.abc":           true,
		"a2a.tasks.x":                          true,
	})
}

// Past its notAfter the same principal does not connect at all.
func TestTheRenderedSnapshotPrincipalPastItsDeadlineCannotConnect(t *testing.T) {
	expired := strings.Replace(renderedMigrationMap(t), "2099-01-01T00:00:00Z", time.Now().Add(-time.Minute).UTC().Format(time.RFC3339), 1)
	h := startHarness(t, expired, map[string]Attested{
		tokenMigrationSnapshot: {ServiceAccount: renderedMigrationSA},
	})
	nc, err := nats.Connect(h.url, h.dial, nats.Token(tokenMigrationSnapshot), nats.CustomInboxPrefix("_INBOX."+renderedMigrationUser), nats.MaxReconnects(0))
	if err == nil {
		nc.Close()
		t.Fatal("connected as the snapshot principal past its notAfter")
	}
	if !strings.Contains(strings.ToLower(err.Error()), "authorization") {
		t.Errorf("connect error %v, want an authorization refusal", err)
	}
}
