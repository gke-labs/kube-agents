package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/mcp"
)

func env(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func file(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "f")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestConfigFromEnv(t *testing.T) {
	token, pass := file(t, "tok\n"), file(t, "pw\n")
	s, err := configFromEnv(env(map[string]string{
		envBusURL:       "tls://platform-agent-a2a-nats.kubeagents-system.svc:4222",
		envTokenFile:    token,
		envPasswordFile: pass,
		envAgent:        "platform-agent",
		envNamespace:    "kubeagents-system",
	}))
	if err != nil {
		t.Fatal(err)
	}
	if s.listen != defaultListen || s.tokenFile != token {
		t.Errorf("listen %q token %q", s.listen, s.tokenFile)
	}
	if s.bus.User != defaultBusUser || s.bus.Password != "pw" {
		t.Errorf("bus user %q password %q, want the console user and the trimmed file", s.bus.User, s.bus.Password)
	}
	if s.bus.Deployment.Agent != "platform-agent" || s.bus.Deployment.Namespace != "kubeagents-system" {
		t.Errorf("deployment = %+v", s.bus.Deployment)
	}
}

// Each of these is a pod that must not start: there is no default bus, no
// running without a token, and no running without the bus credential.
func TestConfigFromEnvRefusesToStartWithoutWhatItNeeds(t *testing.T) {
	token, pass := file(t, "tok"), file(t, "pw")
	base := map[string]string{envBusURL: "tls://bus:4222", envTokenFile: token, envPasswordFile: pass}
	for name, override := range map[string]map[string]string{
		"no bus URL":          {envBusURL: ""},
		"a plaintext bus URL": {envBusURL: "nats://bus:4222"},
		"a missing token":     {envTokenFile: filepath.Join(t.TempDir(), "absent")},
		"an empty token":      {envTokenFile: file(t, " \n")},
		"a missing password":  {envPasswordFile: filepath.Join(t.TempDir(), "absent")},
		"an empty password":   {envPasswordFile: file(t, "\n")},
	} {
		t.Run(name, func(t *testing.T) {
			m := map[string]string{}
			for k, v := range base {
				m[k] = v
			}
			for k, v := range override {
				m[k] = v
			}
			if _, err := configFromEnv(env(m)); err == nil {
				t.Errorf("started with %s", name)
			}
		})
	}
}

func toolNames(tools []mcp.Tool) []string {
	names := make([]string, 0, len(tools))
	for _, tool := range tools {
		names = append(names, tool.Definition.Name)
	}
	return names
}

// With no broker URL the pod has no broker, and inspect_pr is not offered:
// a listed tool that can only fail misleads the caller's model.
func TestConfigFromEnvWithoutABrokerOffersNoInspectPR(t *testing.T) {
	s, err := configFromEnv(env(map[string]string{
		envBusURL:          "tls://bus:4222",
		envTokenFile:       file(t, "tok"),
		envPasswordFile:    file(t, "pw"),
		envBrokerTokenFile: filepath.Join(t.TempDir(), "absent"),
	}))
	if err != nil {
		t.Fatal(err)
	}
	if s.broker != nil {
		t.Error("a broker client with no broker URL")
	}
	names := toolNames(toolsFor(nil, s.broker))
	if len(names) != 1 || names[0] != mcp.ToolConnectionStatus {
		t.Errorf("tools = %v, want only %s", names, mcp.ToolConnectionStatus)
	}
}

func TestConfigFromEnvWithABrokerOffersInspectPR(t *testing.T) {
	s, err := configFromEnv(env(map[string]string{
		envBusURL:          "tls://bus:4222",
		envTokenFile:       file(t, "tok"),
		envPasswordFile:    file(t, "pw"),
		envBrokerURL:       "http://platform-agent-credential-proxy.kubeagents-system.svc:8080",
		envBrokerTokenFile: file(t, "broker-tok\n"),
	}))
	if err != nil {
		t.Fatal(err)
	}
	if s.broker == nil {
		t.Fatal("no broker client with a broker URL")
	}
	names := toolNames(toolsFor(nil, s.broker))
	if len(names) != 2 || names[0] != mcp.ToolConnectionStatus || names[1] != mcp.ToolInspectPR {
		t.Errorf("tools = %v, want %s and %s", names, mcp.ToolConnectionStatus, mcp.ToolInspectPR)
	}
}

// A broker URL set means inspect_pr was meant: without a usable broker token
// the pod must not start, rather than start and drop the tool.
func TestConfigFromEnvRefusesToStartWithABrokerAndNoUsableToken(t *testing.T) {
	token, pass, brokerToken := file(t, "tok"), file(t, "pw"), file(t, "broker-tok")
	base := map[string]string{
		envBusURL: "tls://bus:4222", envTokenFile: token, envPasswordFile: pass,
		envBrokerURL: "http://broker:8080", envBrokerTokenFile: brokerToken,
	}
	for name, override := range map[string]map[string]string{
		"no broker token file":      {envBrokerTokenFile: ""},
		"a missing broker token":    {envBrokerTokenFile: filepath.Join(t.TempDir(), "absent")},
		"an empty broker token":     {envBrokerTokenFile: file(t, " \n")},
		"a directory for the token": {envBrokerTokenFile: t.TempDir()},
		"a broker URL with no host": {envBrokerURL: "http://"},
		"a broker URL of no scheme": {envBrokerURL: "broker:8080"},
		"a broker URL with a query": {envBrokerURL: "http://broker:8080?x=1"},
	} {
		t.Run(name, func(t *testing.T) {
			m := map[string]string{}
			for k, v := range base {
				m[k] = v
			}
			for k, v := range override {
				m[k] = v
			}
			if _, err := configFromEnv(env(m)); err == nil {
				t.Errorf("started with %s", name)
			}
		})
	}
}
