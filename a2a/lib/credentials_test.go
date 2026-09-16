package lib

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

// tokenServer starts a server that accepts exactly one bearer token, on the
// given port (-1 for random). The deployment's real server delegates this
// decision to the auth callout; for the client option under test the two are
// the same thing — a CONNECT carrying a token the server accepts or not.
func tokenServer(t *testing.T, port int, token string) *natsserver.Server {
	t.Helper()
	return runJetStreamServer(t, port, t.TempDir(), func(o *natsserver.Options) {
		o.Authorization = token
	})
}

func writeToken(t *testing.T, path, token string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(token+"\n"), 0o600); err != nil {
		t.Fatalf("writing the token file: %v", err)
	}
}

func TestWithKSATokenPresentsTheFileAndPinsTheInbox(t *testing.T) {
	s := tokenServer(t, -1, "tok-a")
	t.Cleanup(s.Shutdown)
	path := filepath.Join(t.TempDir(), "token")
	writeToken(t, path, "tok-a")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	c, err := Connect(ctx, clientURL(s), WithKSAToken(path, "chat-otter-1a2b"))
	if err != nil {
		t.Fatalf("Connect with the token file: %v", err)
	}
	defer c.Close()

	// The inbox prefix is the half of the option that fails silently when
	// missing: replies go to a subject the caller's grants do not cover and
	// every request times out. Assert it from the connection itself.
	if inbox := c.nc.NewRespInbox(); !strings.HasPrefix(inbox, "_INBOX.chat-otter-1a2b.") {
		t.Errorf("reply inbox = %q, want the owner's _INBOX.chat-otter-1a2b. prefix", inbox)
	}
}

// The deployment spec's MUST for a long-lived client: re-read the token file
// on reconnect rather than caching the first read, because the projected
// token rotates and a bus restart is a routine operation. A client that
// cached "tok-a" can never rejoin a server that now wants "tok-b".
func TestWithKSATokenRereadsTheFileOnReconnect(t *testing.T) {
	s1 := tokenServer(t, -1, "tok-a")
	port := serverPort(s1)
	path := filepath.Join(t.TempDir(), "token")
	writeToken(t, path, "tok-a")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	c, err := Connect(ctx, clientURL(s1), WithKSAToken(path, "chat-otter-1a2b"))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c.Close()

	// Rotate the file, then take the server away and bring back one that
	// only knows the rotated value.
	writeToken(t, path, "tok-b")
	s1.Shutdown()
	s1.WaitForShutdown()
	s2 := tokenServer(t, port, "tok-b")
	t.Cleanup(s2.Shutdown)

	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		if c.nc.Status() == nats.CONNECTED {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("never reconnected with the rotated token; status %v, last error %v", c.nc.Status(), c.nc.LastError())
}

// A missing file is a misconfiguration the caller should hear about at the
// first connect, not as an Authorization Violation that reads like the bus
// refusing a legitimate identity.
func TestWithKSATokenNamesAMissingFileAtConnect(t *testing.T) {
	s := tokenServer(t, -1, "tok-a")
	t.Cleanup(s.Shutdown)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err := Connect(ctx, clientURL(s), WithKSAToken(filepath.Join(t.TempDir(), "absent"), "chat-otter-1a2b"))
	if err == nil {
		t.Fatal("Connect succeeded with no token file")
	}
	if !strings.Contains(err.Error(), "absent") {
		t.Errorf("error does not name the file: %v", err)
	}
}

// An empty owner would fall back to nats.go's default _INBOX.<nuid>, which no
// per-principal grant covers — the hang the option exists to prevent. Refuse
// it where it can be named.
func TestWithKSATokenRefusesAnEmptyInboxOwner(t *testing.T) {
	s := tokenServer(t, -1, "tok-a")
	t.Cleanup(s.Shutdown)
	path := filepath.Join(t.TempDir(), "token")
	writeToken(t, path, "tok-a")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := Connect(ctx, clientURL(s), WithKSAToken(path, "")); err == nil {
		t.Fatal("Connect accepted an empty inbox owner")
	}
	if _, err := Connect(ctx, clientURL(s), WithKSAToken(path, "has.a.dot")); err == nil {
		t.Fatal("Connect accepted an inbox owner that is not a single subject token")
	}
}
