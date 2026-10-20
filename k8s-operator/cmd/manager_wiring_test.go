package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"go/types"
	"testing"
)

// Two lines in main() decide how much of the cluster this process holds in
// memory, and neither can be observed from a test that does not start a
// manager against a real API server. It reads main.go rather than running it,
// because main() dials a cluster and calls os.Exit.
//
// The value matters as much as the field, and more for one of the two than
// the other:
//
//   - Cache. Nothing else sets it. Delete the line and the manager is built
//     with controller-runtime's default options, keeping the managedFields of
//     every object it caches; set it to a bare cache.Options{} and the same
//     is true with the field still there. Every other test in the repository
//     passes either way -- manager_cache_test.go exercises
//     ManagerCacheOptions() directly and never reaches main.go.
//   - Client. controller.ManagerClientOptions is what makes "no Pod informer"
//     a property of the client rather than a convention the four known call
//     sites keep; drop it and the next cached Pod read anyone writes starts
//     one again.
//   - APIReader. SetupWithManager backfills it from the manager when it is
//     nil, so deleting the line is caught there rather than here; what the
//     backfill cannot catch is the line pointing somewhere else, because a
//     non-nil APIReader is left alone. mgr.GetClient() would put every Pod
//     read back on the cache with the field set and the reconciler running —
//     or it would, without the Client option above, which is why the two are
//     asserted together. The deletion is asserted too, so all three lines are
//     read the same way.

const (
	mainSourceFile           = "main.go"
	managerOptionsType       = "ctrl.Options"
	managerCacheField        = "Cache"
	managerCacheValue        = "controller.ManagerCacheOptions()"
	managerClientField       = "Client"
	managerClientValue       = "controller.ManagerClientOptions()"
	reconcilerType           = "controller.PlatformAgentReconciler"
	reconcilerAPIReaderField = "APIReader"
	reconcilerAPIReaderValue = "mgr.GetAPIReader()"
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
		value   string
		why     string
	}{
		{managerOptionsType, managerCacheField, managerCacheValue,
			"the manager is built with a cache that keeps the managedFields of every cached object"},
		{managerOptionsType, managerClientField, managerClientValue,
			"any Pod read through the manager's client is served from the cache, which starts a cluster-wide Pod informer"},
		{reconcilerType, reconcilerAPIReaderField, reconcilerAPIReaderValue,
			"podReader serves Pod reads from the cache unless SetupWithManager's nil-backfill catches it, and a non-nil wrong value it cannot"},
	} {
		got, set := literalField(file, tc.literal, tc.field)
		if !set {
			t.Errorf("%s in %s sets no %s: %s", tc.literal, mainSourceFile, tc.field, tc.why)
			continue
		}
		if got != tc.value {
			t.Errorf("%s in %s sets %s to %s, want %s: %s", tc.literal, mainSourceFile, tc.field, got, tc.value, tc.why)
		}
	}
}

// literalField returns the rendered value some composite literal of the named
// type in file assigns to the named field, and whether it assigns it at all.
func literalField(file *ast.File, typeName, field string) (string, bool) {
	value, found := "", false
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
				value, found = types.ExprString(kv.Value), true
				return false
			}
		}
		return true
	})
	return value, found
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
