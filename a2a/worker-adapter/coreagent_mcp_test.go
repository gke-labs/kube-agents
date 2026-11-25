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

// Each turn's driver carries the persona's servers, rebuilt from source: a
// turn without a delegate doesn't keep one an earlier turn had. The Claude
// Code driver, which takes its servers as flags, is left alone.
func TestEachTurnsEnvelopeCarriesItsOwnServers(t *testing.T) {
	delegate := persona.MCPServer{Name: "a2a", Command: "/worker-adapter", Tools: []string{"delegate"}}
	pod := Driver(newCoreAgentDriver())
	first := turnDriver(pod, []persona.MCPServer{relayServer}, &delegate)
	second := turnDriver(first, []persona.MCPServer{relayServer}, nil)
	for i, tc := range []struct {
		d    Driver
		want []string
	}{{first, []string{"developer_knowledge", "a2a"}}, {second, []string{"developer_knowledge"}}} {
		line, err := tc.d.EncodeUser("hello")
		if err != nil {
			t.Fatal(err)
		}
		var env coreagentwire.Envelope
		if err := json.Unmarshal(line, &env); err != nil {
			t.Fatal(err)
		}
		var names []string
		for _, s := range env.MCP {
			names = append(names, s.Name)
		}
		if !reflect.DeepEqual(names, tc.want) {
			t.Errorf("turn %d servers = %v, want %v", i+1, names, tc.want)
		}
	}
	if _, ok := turnDriver(claudeDriver{}, []persona.MCPServer{relayServer}, nil).(claudeDriver); !ok {
		t.Error("the Claude Code driver was changed")
	}
}
