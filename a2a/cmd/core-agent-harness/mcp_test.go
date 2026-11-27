package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/genai"

	"github.com/go-steer/core-agent/v3/pkg/agent"
	coremcp "github.com/go-steer/core-agent/v3/pkg/mcp"
	"github.com/go-steer/core-agent/v3/pkg/permissions"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/toolgrant"
)

// The test binary doubles as a fake stdio MCP server, chosen by env:
// FAKE_MCP_TOOLS lists the tools it offers (comma-separated), FAKE_MCP_EXIT
// makes it exit at once, FAKE_MCP_PIDFILE gets its pid, FAKE_MCP_CALLS
// gets a line per tool call, and on each call FAKE_MCP_SAW gets what the
// directory FAKE_MCP_WATCH held at that moment.
func TestMain(m *testing.M) {
	if pf := os.Getenv("FAKE_MCP_PIDFILE"); pf != "" {
		_ = os.WriteFile(pf, []byte(strconv.Itoa(os.Getpid())), 0o600)
	}
	if os.Getenv("FAKE_MCP_EXIT") != "" {
		os.Exit(3)
	}
	if tools, ok := os.LookupEnv("FAKE_MCP_TOOLS"); ok {
		s := mcp.NewServer(&mcp.Implementation{Name: "fake", Version: "0"}, nil)
		for _, name := range strings.Split(tools, ",") {
			s.AddTool(&mcp.Tool{Name: name, InputSchema: map[string]any{"type": "object"}},
				func(context.Context, *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
					if w, f := os.Getenv("FAKE_MCP_WATCH"), os.Getenv("FAKE_MCP_SAW"); w != "" && f != "" {
						ents, err := os.ReadDir(w)
						names := []string{}
						for _, e := range ents {
							names = append(names, e.Name())
						}
						_ = os.WriteFile(f, []byte(fmt.Sprintf("entries=%q err=%v", names, err)), 0o600)
					}
					if f := os.Getenv("FAKE_MCP_CALLS"); f != "" {
						if fh, err := os.OpenFile(f, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600); err == nil {
							fmt.Fprintln(fh, name)
							fh.Close()
						}
					}
					return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "ok:" + name}}}, nil
				})
		}
		if err := s.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
			fmt.Fprintln(os.Stderr, "fake mcp:", err)
			os.Exit(1)
		}
		os.Exit(0)
	}
	os.Exit(m.Run())
}

// fakeServer is an Envelope entry for the fake server named name, offering
// offers and granted tools, with its pid written to the returned file.
func fakeServer(t *testing.T, name string, offers string, tools ...string) (coreagentwire.MCPServer, string) {
	t.Helper()
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	pidFile := filepath.Join(t.TempDir(), "pid")
	return coreagentwire.MCPServer{
		Name:    name,
		Command: self,
		Env:     map[string]string{"FAKE_MCP_TOOLS": offers, "FAKE_MCP_PIDFILE": pidFile},
		Tools:   tools,
	}, pidFile
}

// gone reports whether the process whose pid is in pidFile has exited (or
// never started).
func gone(t *testing.T, pidFile string) bool {
	t.Helper()
	b, err := os.ReadFile(pidFile)
	if err != nil {
		return true
	}
	pid, _ := strconv.Atoi(string(b))
	return pid <= 0 || syscall.Kill(pid, 0) != nil
}

// TestEachServerRegistersOnlyItsGrantedTools: a server offering a, b and c,
// granted a and b, registers <server>_a and <server>_b and nothing else,
// and stopping the turn's servers ends its process.
func TestEachServerRegistersOnlyItsGrantedTools(t *testing.T) {
	srv, pidFile := fakeServer(t, "fake", "a,b,c", "a", "b")
	deleg, _ := fakeServer(t, "a2a", "delegate", "delegate")
	parent := t.TempDir()
	prev := mcpConfigParent
	mcpConfigParent = parent
	t.Cleanup(func() { mcpConfigParent = prev })
	sets, closeAll, err := buildMCP(context.Background(), []coreagentwire.MCPServer{srv, deleg}, nil)
	if err != nil {
		t.Fatal(err)
	}
	// The config core-agent read is gone before any tool can run.
	if left, _ := os.ReadDir(parent); len(left) != 0 {
		t.Errorf("the MCP config directory outlived buildMCP: %v", left)
	}
	var names []string
	for _, ts := range sets {
		tools, err := coremcp.ListTools(context.Background(), ts)
		if err != nil {
			t.Fatal(err)
		}
		for _, tl := range tools {
			names = append(names, tl.Name())
		}
	}
	slices.Sort(names)
	if want := []string{"a2a_delegate", "fake_a", "fake_b"}; !slices.Equal(names, want) {
		t.Errorf("registered %q; want %q", names, want)
	}
	if gone(t, pidFile) {
		t.Fatal("the server is not running before the turn's servers are stopped")
	}
	closeAll()
	if !gone(t, pidFile) {
		t.Error("the server is still running after the turn's servers were stopped")
	}
}

// TestAGrantedServerThatCannotServeFailsTheTurn: the turn fails, with any
// server it started stopped, when a granted server doesn't start, doesn't
// list a granted tool, or is granted nothing.
func TestAGrantedServerThatCannotServeFailsTheTurn(t *testing.T) {
	prev := mcpStartBound
	mcpStartBound = 20 * time.Second
	t.Cleanup(func() { mcpStartBound = prev })
	cases := map[string]struct {
		server func(t *testing.T) (coreagentwire.MCPServer, string)
		want   string
	}{
		"a granted tool it does not list": {
			server: func(t *testing.T) (coreagentwire.MCPServer, string) { return fakeServer(t, "fake", "a,b,c", "a", "z") },
			want:   `"fake" does not list z`,
		},
		"a command that exits at once": {
			server: func(t *testing.T) (coreagentwire.MCPServer, string) {
				s, pf := fakeServer(t, "fake", "a", "a")
				s.Env["FAKE_MCP_EXIT"] = "1"
				return s, pf
			},
			want: `"fake" did not start`,
		},
		"a command that does not exist": {
			server: func(t *testing.T) (coreagentwire.MCPServer, string) {
				s, pf := fakeServer(t, "fake", "a", "a")
				s.Command = filepath.Join(t.TempDir(), "missing")
				return s, pf
			},
			want: `"fake" did not start`,
		},
		"no granted tools (the whole catalog to core-agent)": {
			server: func(t *testing.T) (coreagentwire.MCPServer, string) { return fakeServer(t, "fake", "a") },
			want:   `"fake" names no tools`,
		},
		"an env value asking for interpolation": {
			server: func(t *testing.T) (coreagentwire.MCPServer, string) {
				s, pf := fakeServer(t, "fake", "a", "a")
				s.Env["LEAK"] = "${env:HOME}"
				return s, pf
			},
			want: "interpolation",
		},
		"both a command and a URL": {
			server: func(t *testing.T) (coreagentwire.MCPServer, string) {
				s, pf := fakeServer(t, "fake", "a", "a")
				s.URL = "http://127.0.0.1:1/mcp"
				return s, pf
			},
			want: "exactly one of a command or a URL",
		},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			srv, pidFile := c.server(t)
			good, goodPID := fakeServer(t, "other", "x", "x")
			sets, closeAll, err := buildMCP(context.Background(), []coreagentwire.MCPServer{good, srv}, nil)
			closeAll()
			if err == nil {
				t.Fatalf("the turn's servers built (%d toolsets); want a failure", len(sets))
			}
			if !strings.Contains(err.Error(), c.want) {
				t.Errorf("err = %v; want it to say %s", err, c.want)
			}
			for _, pf := range []string{pidFile, goodPID} {
				if !gone(t, pf) {
					t.Errorf("a server (%s) is still running after the turn failed", pf)
				}
			}
		})
	}
}

// TestNoMCPServersStartsNothing: a turn with no servers builds none.
func TestNoMCPServersStartsNothing(t *testing.T) {
	sets, closeAll, err := buildMCP(context.Background(), nil, nil)
	defer closeAll()
	if err != nil || len(sets) != 0 {
		t.Fatalf("sets=%d err=%v", len(sets), err)
	}
}

// TestAGrantedMCPToolRunsThroughThePolicy: with the turn's real gate and
// policy plugin, built from a persona grant as the adapter renders it, the model's call to a granted MCP tool reaches the server,
// and a call to a tool the server offers but the grant doesn't name reaches
// nothing.
func TestAGrantedMCPToolRunsThroughThePolicy(t *testing.T) {
	for _, c := range []struct {
		name, call, wantRan string
		args                map[string]any
	}{
		{"granted", "fake_a", "a", map[string]any{"q": "x"}},
		{"granted, nil args", "fake_a", "a", nil},
		{"offered but not granted", "fake_c", "", map[string]any{}},
	} {
		t.Run(c.name, func(t *testing.T) {
			srv, _ := fakeServer(t, "fake", "a,b,c", "a", "b")
			calls := filepath.Join(t.TempDir(), "calls")
			srv.Env["FAKE_MCP_CALLS"] = calls
			// Where buildMCP makes its config directory, which the tool
			// lists when it runs: it must find nothing there.
			watch, saw := t.TempDir(), filepath.Join(t.TempDir(), "saw")
			prev := mcpConfigParent
			mcpConfigParent = watch
			t.Cleanup(func() { mcpConfigParent = prev })
			srv.Env["FAKE_MCP_WATCH"], srv.Env["FAKE_MCP_SAW"] = watch, saw
			servers := []coreagentwire.MCPServer{srv}
			// The policy as the adapter renders it from the persona's grant.
			ws := t.TempDir()
			g, err := toolgrant.Policy{MCP: []toolgrant.MCPGrant{{Server: "fake", Tools: []string{"a", "b"}}}}.CoreAgent(ws)
			if err != nil {
				t.Fatal(err)
			}
			_, opts, plugins, err := policySetup(coreagentwire.Policy{Tools: g.Builtins, Allow: g.Allow, Deny: g.Deny}, servers, ws)
			if err != nil {
				t.Fatal(err)
			}
			gate := permissions.New(opts)
			sets, closeAll, err := buildMCP(context.Background(), servers, gate)
			if err != nil {
				t.Fatal(err)
			}
			defer closeAll()
			llm := &fakeLLM{calls: []*genai.FunctionCall{{Name: c.call, Args: c.args}}}
			a, err := agent.New(llm,
				agent.WithMode(agent.ModeAutonomous),
				agent.WithGate(gate),
				agent.WithToolsets(sets),
				agent.WithPlugins(plugins...))
			if err != nil {
				t.Fatal(err)
			}
			for ev, err := range a.Run(context.Background(), "go") {
				if err != nil {
					t.Logf("run: %v", err)
					break
				}
				if ev != nil && ev.Content != nil {
					for _, p := range ev.Content.Parts {
						if p.FunctionResponse != nil {
							t.Logf("%s answered %v", p.FunctionResponse.Name, p.FunctionResponse.Response)
						}
					}
				}
			}
			b, _ := os.ReadFile(calls)
			if got := strings.TrimSpace(string(b)); got != c.wantRan {
				t.Errorf("the server ran %q; want %q", got, c.wantRan)
			}
			if c.wantRan != "" {
				if got, _ := os.ReadFile(saw); string(got) != `entries=[] err=<nil>` {
					t.Errorf("when the tool ran, the MCP config directory's parent held %s; want nothing", got)
				}
			}
		})
	}
}

// TestTheStartedCheckRefusesWhatTheGrantDoesNotDescribe: a server no entry
// grants, a tool its entry doesn't name, and a tool name two servers both
// register each fail the check.
func TestTheStartedCheckRefusesWhatTheGrantDoesNotDescribe(t *testing.T) {
	ok := func(name string, tools ...string) *coremcp.Server {
		return &coremcp.Server{Name: name, Status: coremcp.StatusOK, Tools: tools}
	}
	for name, c := range map[string]struct {
		granted []coreagentwire.MCPServer
		srvs    []*coremcp.Server
		want    string
	}{
		"an extra server": {
			granted: []coreagentwire.MCPServer{{Name: "a", Tools: []string{"x"}}},
			srvs:    []*coremcp.Server{ok("a", "a_x"), ok("evil", "evil_y")},
			want:    `"evil" started, but no entry grants it`,
		},
		"an extra tool": {
			granted: []coreagentwire.MCPServer{{Name: "a", Tools: []string{"x"}}},
			srvs:    []*coremcp.Server{ok("a", "a_x", "a_y")},
			want:    "registered a_y",
		},
		"one name from two servers": {
			granted: []coreagentwire.MCPServer{{Name: "a", Tools: []string{"b_c"}}, {Name: "a_b", Tools: []string{"c"}}},
			srvs:    []*coremcp.Server{ok("a", "a_b_c"), ok("a_b", "a_b_c")},
			want:    "a_b_c is registered by both",
		},
	} {
		t.Run(name, func(t *testing.T) {
			if err := checkStarted(c.granted, c.srvs); err == nil || !strings.Contains(err.Error(), c.want) {
				t.Errorf("err = %v; want it to say %s", err, c.want)
			}
		})
	}
	if err := checkStarted([]coreagentwire.MCPServer{{Name: "a", Tools: []string{"x"}}}, []*coremcp.Server{ok("a", "a_x")}); err != nil {
		t.Errorf("an exact match failed: %v", err)
	}
}
