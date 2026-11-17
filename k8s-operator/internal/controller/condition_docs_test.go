package controller

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// writtenConditionTypes scans this package's non-test sources for every
// metav1.Condition literal and returns each Type it sets, by value, with the
// constant (or "literal") and the positions that set it. A Type taken from
// another package (agentv1alpha1.X, the AgentProfile's conditions) belongs to
// another CRD's page and is skipped; a Type the scan cannot resolve to a
// string fails the test, so a new writing pattern cannot slip past it.
func writtenConditionTypes(t *testing.T) map[string]writtenCondition {
	t.Helper()
	fset := token.NewFileSet()
	names, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	consts := map[string]string{}
	var files []*ast.File
	for _, name := range names {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, name, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		files = append(files, f)
		for _, decl := range f.Decls {
			gen, ok := decl.(*ast.GenDecl)
			if !ok || gen.Tok != token.CONST {
				continue
			}
			for _, spec := range gen.Specs {
				vs := spec.(*ast.ValueSpec)
				for i, id := range vs.Names {
					if i >= len(vs.Values) {
						continue
					}
					if lit, ok := vs.Values[i].(*ast.BasicLit); ok && lit.Kind == token.STRING {
						v, _ := strconv.Unquote(lit.Value)
						consts[id.Name] = v
					}
				}
			}
		}
	}
	found := map[string]writtenCondition{}
	note := func(value, constant string, pos token.Pos) {
		p := fset.Position(pos)
		w := found[value]
		if w.constant == "" || w.constant == "literal" {
			w.constant = constant
		}
		w.at = append(w.at, fmt.Sprintf("%s:%d", p.Filename, p.Line))
		found[value] = w
	}
	readConditionLiteral := func(lit *ast.CompositeLit) {
		for _, elt := range lit.Elts {
			kv, ok := elt.(*ast.KeyValueExpr)
			if !ok {
				t.Errorf("%s: an unkeyed Condition literal; key its fields so the scan can read its Type",
					fset.Position(elt.Pos()))
				return
			}
			if key, ok := kv.Key.(*ast.Ident); !ok || key.Name != "Type" {
				continue
			}
			switch v := kv.Value.(type) {
			case *ast.Ident:
				value, ok := consts[v.Name]
				// The parser resolves names declared in the same file: a
				// local variable shadowing a constant is not the constant.
				if v.Obj != nil && v.Obj.Kind != ast.Con {
					ok = false
				}
				if !ok {
					t.Errorf("%s: a condition's Type is %s, which is not a string constant of this package; "+
						"use a constant so TestEveryWrittenConditionIsDocumented can name it", fset.Position(v.Pos()), v.Name)
					continue
				}
				note(value, v.Name, v.Pos())
			case *ast.BasicLit:
				value, _ := strconv.Unquote(v.Value)
				note(value, conditionConstantFor(consts, value), v.Pos())
			case *ast.SelectorExpr:
				// Another package's constant: another CRD's condition.
			default:
				t.Errorf("%s: a condition's Type is a %T the scan cannot read; use a constant", fset.Position(v.Pos()), v)
			}
		}
	}
	for _, f := range files {
		ast.Inspect(f, func(n ast.Node) bool {
			// A Type set after construction is one the scan cannot tie to a
			// Condition, so the package may not do it: write the literal keyed.
			if assign, ok := n.(*ast.AssignStmt); ok {
				for _, lhs := range assign.Lhs {
					if sel, ok := lhs.(*ast.SelectorExpr); ok && sel.Sel.Name == "Type" {
						t.Errorf("%s: a .Type assignment; set a condition's Type in a keyed metav1.Condition literal "+
							"so TestEveryWrittenConditionIsDocumented can read it", fset.Position(sel.Pos()))
					}
				}
				return true
			}
			lit, ok := n.(*ast.CompositeLit)
			if !ok {
				return true
			}
			if isConditionType(lit.Type) {
				readConditionLiteral(lit)
				return true
			}
			// A slice or map literal of X.Condition elides its elements' type,
			// so each element literal without one is a Condition too.
			var elem ast.Expr
			switch typ := lit.Type.(type) {
			case *ast.ArrayType:
				elem = typ.Elt
			case *ast.MapType:
				elem = typ.Value
			}
			if elem == nil || !isConditionType(elem) {
				return true
			}
			for _, elt := range lit.Elts {
				if kv, ok := elt.(*ast.KeyValueExpr); ok {
					elt = kv.Value
				}
				if inner, ok := elt.(*ast.CompositeLit); ok && inner.Type == nil {
					readConditionLiteral(inner)
				}
			}
			return true
		})
	}
	return found
}

func isConditionType(e ast.Expr) bool {
	sel, ok := e.(*ast.SelectorExpr)
	return ok && sel.Sel.Name == "Condition"
}

// conditionConstantFor names the condition-type constant whose value a
// literal Type repeats, so the row a failure suggests is keyed by it; "literal"
// when there is none.
func conditionConstantFor(consts map[string]string, value string) string {
	var names []string
	for name, v := range consts {
		if v == value && strings.Contains(name, "Condition") && !strings.Contains(name, "Reason") {
			names = append(names, name)
		}
	}
	if len(names) == 0 {
		return "literal"
	}
	sort.Strings(names)
	return names[0]
}

type writtenCondition struct {
	constant string
	at       []string
}

// The CRD reference page's conditions table is generated from
// platformAgentConditionDocs, so a condition the controller writes and the
// slice lacks would be missing from the page, and a row for a condition it no
// longer writes would document something that never appears.
func TestEveryWrittenConditionIsDocumented(t *testing.T) {
	written := writtenConditionTypes(t)
	documented := map[string]bool{}
	for _, d := range platformAgentConditionDocs {
		if documented[d.Type] {
			t.Errorf("platformAgentConditionDocs has two rows for %q; remove one", d.Type)
		}
		documented[d.Type] = true
		if d.Written == "" || d.Meaning == "" {
			t.Errorf("platformAgentConditionDocs row %q needs both Written and Meaning", d.Type)
		}
		if strings.Contains(d.Meaning, "{reasons}") != (len(d.Reasons) > 0) {
			t.Errorf("platformAgentConditionDocs row %q: Meaning carries {reasons} exactly when Reasons is set", d.Type)
		}
	}
	var missing []string
	for value := range written {
		if !documented[value] {
			missing = append(missing, value)
		}
	}
	sort.Strings(missing)
	for _, value := range missing {
		w := written[value]
		name := w.constant
		if name == "literal" {
			name = strconv.Quote(value)
		}
		t.Errorf("the controller writes condition %q (at %s) and the docs table has no row for it.\n"+
			"  Add to platformAgentConditionDocs in k8s-operator/internal/controller/condition_docs.go:\n"+
			"    {Type: %s, Written: \"<when it is present>\", Meaning: \"<what it holds and what to check>\"},\n"+
			"  then run `make docs-generate` to update docs/site/src/content/docs/operator/platformagent-crd.md.",
			value, strings.Join(w.at, ", "), name)
	}
	for _, d := range platformAgentConditionDocs {
		if _, ok := written[d.Type]; !ok {
			t.Errorf("platformAgentConditionDocs has a row for %q, which nothing in the controller writes.\n"+
				"  Remove it from k8s-operator/internal/controller/condition_docs.go, then run `make docs-generate`.", d.Type)
		}
	}
}
