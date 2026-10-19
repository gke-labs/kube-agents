package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"testing"
)

// Two lines in main() decide how much of the cluster this process holds in
// memory, and neither can be observed from a test that does not start a
// manager against a real API server: the cache's transform
// (controller.ManagerCacheOptions) and the live reader the Pod reads fall back
// off (PlatformAgentReconciler.APIReader, without which podReader returns the
// cached client and every Pod read starts a cluster-wide informer again).
// Delete either and every other test in the repository still passes, which is
// what this one is for. It reads main.go rather than running it, because
// main() dials a cluster and calls os.Exit.
const (
	mainSourceFile           = "main.go"
	managerOptionsType       = "ctrl.Options"
	managerCacheField        = "Cache"
	reconcilerType           = "controller.PlatformAgentReconciler"
	reconcilerAPIReaderField = "APIReader"
)

func TestMainHandsTheManagerItsCacheAndTheReconcilerALiveReader(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, mainSourceFile, nil, parser.SkipObjectResolution)
	if err != nil {
		t.Fatalf("parsing %s: %v", mainSourceFile, err)
	}

	for _, tc := range []struct {
		literal string
		field   string
		why     string
	}{
		{managerOptionsType, managerCacheField,
			"the manager is built with controller-runtime's default cache, which keeps the managedFields of every cached object"},
		{reconcilerType, reconcilerAPIReaderField,
			"podReader falls back to the cached client, so every Pod read starts a cluster-wide Pod informer"},
	} {
		if !literalSetsField(file, tc.literal, tc.field) {
			t.Errorf("%s in %s sets no %s: %s", tc.literal, mainSourceFile, tc.field, tc.why)
		}
	}
}

// literalSetsField reports whether some composite literal of the named type in
// file assigns the named field.
func literalSetsField(file *ast.File, typeName, field string) bool {
	found := false
	ast.Inspect(file, func(n ast.Node) bool {
		lit, ok := n.(*ast.CompositeLit)
		if !ok || literalTypeName(lit.Type) != typeName {
			return true
		}
		for _, elt := range lit.Elts {
			kv, ok := elt.(*ast.KeyValueExpr)
			if !ok {
				continue
			}
			if key, ok := kv.Key.(*ast.Ident); ok && key.Name == field {
				found = true
				return false
			}
		}
		return true
	})
	return found
}

// literalTypeName renders a composite literal's type as `package.Name`, the
// only shape the two literals above have.
func literalTypeName(expr ast.Expr) string {
	sel, ok := expr.(*ast.SelectorExpr)
	if !ok {
		return ""
	}
	pkg, ok := sel.X.(*ast.Ident)
	if !ok {
		return ""
	}
	return pkg.Name + "." + sel.Sel.Name
}
