package authcallout

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/jwt/v2"
	"github.com/nats-io/nkeys"
)

// An entry's notAfter (the bus migration's per-run principals carry the
// run's deadline): before it, a connection is admitted and its JWT expires
// by then; from it on, nothing authenticates as the entry.

const (
	migrateSA    = "system:serviceaccount:kubeagents-system:agent-bus-pin-3-1"
	migrateToken = "migrate-token"
)

func notAfterMap(notAfter string) string {
	return fmt.Sprintf(`{
  "version": "notafter-1",
  "identities": [
    {
      "serviceAccount": %q,
      "user": "migrate-pin-3-1",
      "account": "APP",
      "grants": {
        "publish": ["$JS.API.STREAM.INFO.TASKS"],
        "subscribe": ["_INBOX.migrate-pin-3-1.>"]
      },
      "notAfter": %q
    }
  ]
}`, migrateSA, notAfter)
}

func newNotAfterService(t *testing.T, doc string, now time.Time, grantTTL time.Duration) *Service {
	t.Helper()
	issuerKP, err := nkeys.CreateAccount()
	if err != nil {
		t.Fatal(err)
	}
	issuerSeed, _ := issuerKP.Seed()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	store := NewStore(log)
	if err := store.Update([]byte(doc)); err != nil {
		t.Fatalf("loading the identity map: %v", err)
	}
	validator, err := NewTokenValidator(tokenReviewer(map[string]Attested{migrateToken: {ServiceAccount: migrateSA}}, nil), testAudience)
	if err != nil {
		t.Fatal(err)
	}
	svc, err := NewService(store, validator, Config{IssuerSeed: string(issuerSeed), ReservedPrincipals: renderedFixtureAuthUsers(t), ReservedAddressees: renderedFixtureAddressees(t), GrantTTL: grantTTL, Now: func() time.Time { return now }}, log)
	if err != nil {
		t.Fatal(err)
	}
	return svc
}

func authorizeMigrate(svc *Service) (decision, error) {
	req := &jwt.AuthorizationRequestClaims{}
	req.ConnectOptions.Token = migrateToken
	return svc.authorize(context.Background(), req)
}

func TestAnEntryBeforeItsNotAfterMintsNoJWTThatOutlivesIt(t *testing.T) {
	now := time.Date(2026, 10, 31, 12, 0, 0, 0, time.UTC)
	deadline := now.Add(10 * time.Minute)
	svc := newNotAfterService(t, notAfterMap(deadline.Format(time.RFC3339)), now, time.Hour)
	d, err := authorizeMigrate(svc)
	if err != nil {
		t.Fatalf("refused before its notAfter: %v", err)
	}
	userKP, _ := nkeys.CreateUser()
	userPub, _ := userKP.PublicKey()
	req := &jwt.AuthorizationRequestClaims{}
	req.UserNkey = userPub
	raw, err := svc.mint(req, d)
	if err != nil {
		t.Fatal(err)
	}
	uc, err := jwt.DecodeUserClaims(raw)
	if err != nil {
		t.Fatal(err)
	}
	if uc.Expires != deadline.Unix() {
		t.Errorf("minted exp %d, want the entry's notAfter %d (GrantTTL is an hour)", uc.Expires, deadline.Unix())
	}

	// A GrantTTL shorter than the time left still wins.
	short := newNotAfterService(t, notAfterMap(deadline.Format(time.RFC3339)), now, 2*time.Minute)
	d, err = authorizeMigrate(short)
	if err != nil {
		t.Fatal(err)
	}
	raw, err = short.mint(req, d)
	if err != nil {
		t.Fatal(err)
	}
	if uc, err = jwt.DecodeUserClaims(raw); err != nil {
		t.Fatal(err)
	}
	if uc.Expires > now.Add(2*time.Minute).Unix() {
		t.Errorf("minted exp %d is past GrantTTL", uc.Expires)
	}
}

func TestAnEntryPastItsNotAfterAuthenticatesNothing(t *testing.T) {
	now := time.Date(2026, 10, 31, 12, 0, 0, 0, time.UTC)
	for name, notAfter := range map[string]time.Time{"at it": now, "after it": now.Add(-time.Second)} {
		svc := newNotAfterService(t, notAfterMap(notAfter.Format(time.RFC3339)), now, time.Hour)
		if _, err := authorizeMigrate(svc); err == nil || !strings.Contains(err.Error(), "expired") {
			t.Errorf("%s: authorize error %v, want a refusal naming the expiry", name, err)
		}
	}
}

// notAfter on a narrowed entry is refused with the map: its lifetime is its
// pod's and its task credential's, and a field read by nothing is a limit
// someone believes exists.
func TestNotAfterOnANarrowedEntryRefusesTheMap(t *testing.T) {
	doc := strings.Replace(profileMap, `"narrowing": "pod"`, `"narrowing": "pod", "notAfter": "2026-10-31T12:00:00Z"`, 1)
	if doc == profileMap {
		t.Fatal("the fixture has no pod-narrowed entry to edit")
	}
	if _, err := ParseIdentityMap([]byte(doc)); err == nil || !strings.Contains(err.Error(), "notAfter") {
		t.Errorf("ParseIdentityMap error %v, want a refusal naming notAfter", err)
	}
}
