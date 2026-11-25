package workeradapter

import (
	"encoding/json"
	"reflect"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

var relayServer = persona.MCPServer{
	Name: "developer_knowledge", Command: "/worker-adapter",
	Args:  []string{"mcp-relay", "http://broker:8080/v1/mcp/developer_knowledge"},
	Env:   map[string]string{"CREDENTIAL_PROXY_URL": "http://broker:8080"},
	Tools: []string{"search_documents"},
}

// A turn's servers are the persona's, then the delegate server when the turn
// may delegate, field for field.
func TestACoreAgentTurnCarriesThePersonaServersThenTheDelegate(t *testing.T) {
	delegate := persona.MCPServer{Name: "a2a", Command: "/worker-adapter", Args: []string{"delegate-mcp"}, Env: map[string]string{"A2A_DELEGATE_SOCKET": "/run/d.sock"}, Tools: []string{"delegate"}}
	got := coreAgentTurnMCP([]persona.MCPServer{relayServer}, &delegate)
	want := []coreagentwire.MCPServer{
		{Name: relayServer.Name, Command: relayServer.Command, Args: relayServer.Args, Env: relayServer.Env, Tools: relayServer.Tools},
		{Name: "a2a", Command: "/worker-adapter", Args: []string{"delegate-mcp"}, Env: delegate.Env, Tools: []string{"delegate"}},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("servers = %+v\nwant %+v", got, want)
	}
	if got := coreAgentTurnMCP(nil, nil); len(got) != 0 {
		t.Errorf("no persona servers and no delegate gave %+v", got)
	}
	// The wire copy is its own: changing it leaves the persona's alone.
	got[0].Args[0] = "changed"
	if relayServer.Args[0] != "mcp-relay" {
		t.Error("the turn's servers share the persona's slices")
	}
}

// A profile worker with a rendered persona puts its MCP servers in every
// core-agent turn's stdin envelope; the Claude Code driver, which takes them
// as flags, is left alone.
func TestAProfileWorkersCoreAgentTurnsCarryThePersonaServers(t *testing.T) {
	w := &profileWorker{persona: &persona.Output{MCPServers: []persona.MCPServer{relayServer}}}
	d := w.personaDriver(newCoreAgentDriver())
	line, err := d.EncodeUser("hello")
	if err != nil {
		t.Fatal(err)
	}
	var env coreagentwire.Envelope
	if err := json.Unmarshal(line, &env); err != nil {
		t.Fatal(err)
	}
	if len(env.MCP) != 1 || env.MCP[0].Name != "developer_knowledge" || !reflect.DeepEqual(env.MCP[0].Tools, []string{"search_documents"}) || !reflect.DeepEqual(env.MCP[0].Args, relayServer.Args) {
		t.Errorf("envelope MCP = %+v", env.MCP)
	}
	if _, ok := w.personaDriver(claudeDriver{}).(claudeDriver); !ok {
		t.Error("the Claude Code driver was changed")
	}
	if got := (&profileWorker{}).personaDriver(newCoreAgentDriver()).(coreAgentDriver); got.MCP != nil {
		t.Errorf("a worker with no persona set MCP %+v", got.MCP)
	}
}
