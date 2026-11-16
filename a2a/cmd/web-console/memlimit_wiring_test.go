package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"go/types"
	"strings"
	"testing"
)

const (
	mainSourceFile        = "main.go"
	memoryLimitCall       = "memlimit.Apply"
	memoryLimitCallArgs   = "memoryLimitEnv, memoryLimitFraction"
	configLoadCall        = "loadConfig"
	memoryLimitWiringNote = "the console runs under the runtime's default pacing whatever the container's limit"
)

// The web console applies its soft memory limit in main(), which dials the
// bus and calls os.Exit, so no test runs it. This reads main.go instead, as
// k8s-operator/cmd/manager_wiring_test.go does for the manager: memlimit's
// own tests cover the derivation, and nothing but this reads the call from
// the main path. It has to come before loadConfig, the first thing main does
// that can allocate or exit.
func TestMainAppliesTheMemoryLimitBeforeLoadingTheConfig(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, mainSourceFile, nil, parser.SkipObjectResolution)
	if err != nil {
		t.Fatalf("parsing %s: %v", mainSourceFile, err)
	}
	applyPos, applyArgs := callSite(file, memoryLimitCall)
	if !applyPos.IsValid() {
		t.Fatalf("%s never calls %s: %s", mainSourceFile, memoryLimitCall, memoryLimitWiringNote)
	}
	if applyArgs != memoryLimitCallArgs {
		t.Errorf("%s is called with (%s), want (%s): the variable and the share are the two constants the chart template and the comments are written to", memoryLimitCall, applyArgs, memoryLimitCallArgs)
	}
	loadPos, _ := callSite(file, configLoadCall)
	if !loadPos.IsValid() {
		t.Fatalf("%s never calls %s", mainSourceFile, configLoadCall)
	}
	if applyPos >= loadPos {
		t.Errorf("%s is called after %s: %s", memoryLimitCall, configLoadCall, memoryLimitWiringNote)
	}
}

// callSite returns the position of the first call to callee in file
// (`package.Func` or a bare `Func`) and its arguments rendered as source, or
// an invalid position when there is none.
func callSite(file *ast.File, callee string) (token.Pos, string) {
	pos, args := token.NoPos, ""
	ast.Inspect(file, func(n ast.Node) bool {
		if pos.IsValid() {
			return false
		}
		call, ok := n.(*ast.CallExpr)
		if !ok || types.ExprString(call.Fun) != callee {
			return true
		}
		rendered := make([]string, 0, len(call.Args))
		for _, arg := range call.Args {
			rendered = append(rendered, types.ExprString(arg))
		}
		pos, args = call.Pos(), strings.Join(rendered, ", ")
		return false
	})
	return pos, args
}
