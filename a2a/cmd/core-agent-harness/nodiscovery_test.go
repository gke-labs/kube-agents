package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The harness loads nothing on its own: no call in this package reaches
// core-agent's discovery (config.Load, instruction.Load, skills.Load or
// skills.LoadAll, mcp.Build), which read AGENTS.md, .agents/ and HOME.
func TestTheHarnessCallsNoDiscovery(t *testing.T) {
	banned := map[string][]string{
		"config":      {"Load", "LoadFrom", "Discover"},
		"instruction": {"Load", "LoadAll"},
		"skills":      {"Load", "LoadAll"},
		"mcp":         {"Build"},
		"compose":     nil, // any use
	}
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		src, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		file, err := parser.ParseFile(fset, f, src, 0)
		if err != nil {
			t.Fatal(err)
		}
		imports := map[string]string{} // local name -> path
		for _, imp := range file.Imports {
			path := strings.Trim(imp.Path.Value, `"`)
			name := filepath.Base(path)
			if imp.Name != nil {
				name = imp.Name.Name
			}
			imports[name] = path
			if strings.HasPrefix(path, "github.com/go-steer/core-agent/") && strings.HasSuffix(path, "/pkg/compose") {
				t.Errorf("%s imports %s", f, path)
			}
		}
		ast.Inspect(file, func(n ast.Node) bool {
			sel, ok := n.(*ast.SelectorExpr)
			if !ok {
				return true
			}
			pkg, ok := sel.X.(*ast.Ident)
			if !ok {
				return true
			}
			path := imports[pkg.Name]
			if !strings.HasPrefix(path, "github.com/go-steer/core-agent/") {
				return true
			}
			for _, fn := range banned[filepath.Base(path)] {
				if sel.Sel.Name == fn {
					t.Errorf("%s calls %s.%s, core-agent's discovery", fset.Position(sel.Pos()), pkg.Name, fn)
				}
			}
			return true
		})
	}
}
