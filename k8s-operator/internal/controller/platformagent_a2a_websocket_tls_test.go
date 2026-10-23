/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"bufio"
	"crypto/tls"
	"crypto/x509"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
)

// wsUpgradeRequest is a websocket upgrade with no Origin, as a CLI or the
// console proxy's own probe would send; allowed_origins permits a missing
// Origin, so the only thing between it and a 101 is the transport.
const wsUpgradeRequest = "GET / HTTP/1.1\r\n" +
	"Host: bus\r\n" +
	"Upgrade: websocket\r\n" +
	"Connection: Upgrade\r\n" +
	"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n" +
	"Sec-WebSocket-Version: 13\r\n\r\n"

const (
	wsSwitchingProtocols = "HTTP/1.1 101"
	wsTestDeadline       = 5 * time.Second
)

// a2aWriteServingPair mints a CA and a serving certificate for agent's bus with
// the operator's own issuers, writes the serving pair under dir by the names
// nats.conf uses, and returns the CA to verify against.
func a2aWriteServingPair(t *testing.T, dir string, names []string) *x509.CertPool {
	t.Helper()
	now := time.Now()
	caCert, caKey, _, _, err := generateA2ANATSCA(now)
	if err != nil {
		t.Fatal(err)
	}
	_, certPEM, keyPEM, err := generateA2ANATSServing(caCert, caKey, names, now)
	if err != nil {
		t.Fatal(err)
	}
	for name, data := range map[string][]byte{a2aNATSTLSCertKey: certPEM, a2aNATSTLSKeyKey: keyPEM} {
		if err := os.WriteFile(filepath.Join(dir, name), data, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	pool := x509.NewCertPool()
	pool.AddCert(caCert)
	return pool
}

// a2aStartRenderedWebsocket runs the rendered nats.conf with its websocket
// listener on, the serving pair swapped for one minted into a temp dir, and
// returns the listener's address and the CA that signed its certificate.
func a2aStartRenderedWebsocket(t *testing.T) (string, *x509.CertPool) {
	t.Helper()
	agent := a2aTestAgent()
	conf := string(buildA2ANATSConfigSecret(agent, a2aTestCreds(), a2aTestCalloutKeys(t)).Data["nats.conf"])

	dir := t.TempDir()
	pool := a2aWriteServingPair(t, dir, a2aNATSDNSNames(agent))
	mount := a2aNATSTLSMountDir + "/"
	if !strings.Contains(conf, mount) {
		t.Fatalf("the rendered nats.conf no longer names %s; the substitution below would be inert", mount)
	}
	conf = strings.ReplaceAll(conf, mount, dir+"/")

	path := filepath.Join(t.TempDir(), "nats.conf")
	if err := os.WriteFile(path, []byte(conf), 0o600); err != nil {
		t.Fatal(err)
	}
	opts, err := natsserver.ProcessConfigFile(path)
	if err != nil {
		t.Fatalf("the rendered nats.conf does not parse: %v", err)
	}
	if opts.Websocket.NoTLS || opts.Websocket.TLSConfig == nil {
		t.Fatalf("the rendered websocket listener has no TLS (no_tls=%v)", opts.Websocket.NoTLS)
	}
	opts.Host = "127.0.0.1"
	opts.Port = -1
	opts.HTTPPort = 0
	opts.Websocket.Host = "127.0.0.1"
	opts.Websocket.Port = -1
	opts.StoreDir = filepath.Join(t.TempDir(), "store")
	opts.NoSigs = true
	s, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("nats-server refused the rendered config: %v", err)
	}
	go s.Start()
	t.Cleanup(func() {
		s.Shutdown()
		s.WaitForShutdown()
	})
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("embedded nats-server did not come up")
	}
	ports := s.PortsInfo(10 * time.Second)
	if ports == nil || len(ports.WebSocket) == 0 {
		t.Fatal("embedded nats-server reports no websocket listener")
	}
	u, err := url.Parse(ports.WebSocket[0])
	if err != nil {
		t.Fatal(err)
	}
	if u.Scheme != "wss" {
		t.Errorf("websocket listener advertises %q, want wss", ports.WebSocket[0])
	}
	return u.Host, pool
}

// wsFirstLine sends the upgrade on conn and returns the status line it gets,
// or the read error.
func wsFirstLine(conn net.Conn) (string, error) {
	_ = conn.SetDeadline(time.Now().Add(wsTestDeadline))
	if _, err := conn.Write([]byte(wsUpgradeRequest)); err != nil {
		return "", err
	}
	return bufio.NewReader(conn).ReadString('\n')
}

// The websocket listener the operator renders serves TLS from the bus's
// serving certificate, verified by the bus CA under the Service's own name,
// and a plaintext upgrade, which is what the console proxy sent before
// #3651, gets no websocket.
func TestTheRenderedWebsocketListenerServesTLS(t *testing.T) {
	addr, pool := a2aStartRenderedWebsocket(t)
	serverName := a2aNATSDNSNames(a2aTestAgent())[2] // <svc>.<ns>.svc, what the console dials

	t.Run("verified TLS upgrades", func(t *testing.T) {
		conn, err := tls.DialWithDialer(&net.Dialer{Timeout: wsTestDeadline}, "tcp", addr,
			&tls.Config{RootCAs: pool, ServerName: serverName, MinVersion: tls.VersionTLS12})
		if err != nil {
			t.Fatalf("TLS to the websocket listener under %s with the bus CA: %v", serverName, err)
		}
		defer func() { _ = conn.Close() }()
		line, err := wsFirstLine(conn)
		if err != nil || !strings.HasPrefix(line, wsSwitchingProtocols) {
			t.Fatalf("upgrade over TLS = %q, %v; want %s", line, err, wsSwitchingProtocols)
		}
	})

	t.Run("plaintext gets no websocket", func(t *testing.T) {
		conn, err := net.DialTimeout("tcp", addr, wsTestDeadline)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = conn.Close() }()
		if line, err := wsFirstLine(conn); err == nil && strings.HasPrefix(line, wsSwitchingProtocols) {
			t.Fatalf("a plaintext upgrade got %q; the listener still speaks plain ws", line)
		}
	})

	t.Run("another CA is refused", func(t *testing.T) {
		other := a2aWriteServingPair(t, t.TempDir(), a2aNATSDNSNames(a2aTestAgent()))
		_, err := tls.DialWithDialer(&net.Dialer{Timeout: wsTestDeadline}, "tcp", addr,
			&tls.Config{RootCAs: other, ServerName: serverName, MinVersion: tls.VersionTLS12})
		if err == nil {
			t.Fatal("TLS verified against a CA that did not sign the serving certificate")
		}
	})

	t.Run("a name outside the SANs is refused", func(t *testing.T) {
		_, err := tls.DialWithDialer(&net.Dialer{Timeout: wsTestDeadline}, "tcp", addr,
			&tls.Config{RootCAs: pool, ServerName: "localhost", MinVersion: tls.VersionTLS12})
		if err == nil {
			t.Fatal("TLS verified under localhost; the serving certificate names the Service alone")
		}
	})
}

// The console dials the bus by a name its serving certificate carries, or the
// proxy's verification refuses every page load.
func TestTheConsoleDialsTheBusByANameItsCertificateCarries(t *testing.T) {
	agent := a2aTestAgent()
	u, err := url.Parse(a2aNATSWebSocketURL(agent))
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(a2aNATSDNSNames(agent), u.Hostname()) {
		t.Errorf("the console dials %q, which is not among the serving certificate's names %v", u.Hostname(), a2aNATSDNSNames(agent))
	}
}
