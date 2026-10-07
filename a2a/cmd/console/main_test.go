package main

import (
	"context"
	"io"
	"log/slog"
	"net"
	"net/http"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func env(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func quietLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestConfigFromEnvDefaults(t *testing.T) {
	s, err := configFromEnv(env(map[string]string{"CONSOLE_BUS_URL": "http://nats.ns.svc:9222"}))
	if err != nil {
		t.Fatal(err)
	}
	if s.listen != ":8080" {
		t.Errorf("listen = %q", s.listen)
	}
	c := s.console
	if c.StaticDir != "/srv/console" || c.User != "console" ||
		c.PasswordFile != "/var/run/secrets/a2a-console/console-password" ||
		c.BusURL.String() != "http://nats.ns.svc:9222" {
		t.Errorf("defaults = %+v", c)
	}
	if want := []string{"localhost:8080", "127.0.0.1:8080"}; !reflect.DeepEqual(c.AllowedHosts, want) {
		t.Errorf("allowed hosts = %v, want %v", c.AllowedHosts, want)
	}
}

func TestConfigFromEnvRequiresTheBusURL(t *testing.T) {
	if _, err := configFromEnv(env(nil)); err == nil {
		t.Error("no CONSOLE_BUS_URL was accepted")
	}
}

func TestConfigFromEnvRefusesAnUnparseableBusURL(t *testing.T) {
	if _, err := configFromEnv(env(map[string]string{"CONSOLE_BUS_URL": "http://[::1"})); err == nil {
		t.Error("an unparseable CONSOLE_BUS_URL was accepted")
	}
}

func TestConfigFromEnvTrimsTheHostList(t *testing.T) {
	s, err := configFromEnv(env(map[string]string{
		"CONSOLE_BUS_URL":       "http://nats:9222",
		"CONSOLE_ALLOWED_HOSTS": " localhost:9000 , ,127.0.0.1:9000",
	}))
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"localhost:9000", "127.0.0.1:9000"}; !reflect.DeepEqual(s.console.AllowedHosts, want) {
		t.Errorf("allowed hosts = %v, want %v", s.console.AllowedHosts, want)
	}
}

func TestRealMainRefusesAConfigTheHandlerRejects(t *testing.T) {
	err := realMain(context.Background(), quietLog(), env(map[string]string{
		"CONSOLE_BUS_URL":    "http://nats:9222",
		"CONSOLE_STATIC_DIR": filepath.Join(t.TempDir(), "absent"),
	}))
	if err == nil {
		t.Error("realMain started with a static dir that does not exist")
	}
}

func TestServeStopsOnCancel(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- serve(ctx, quietLog(), ln, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			_, _ = io.WriteString(w, "up")
		}))
	}()

	resp, err := http.Get("http://" + ln.Addr().String() + "/")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Errorf("serve returned %v after a clean cancel", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("serve did not return after cancel")
	}
}
