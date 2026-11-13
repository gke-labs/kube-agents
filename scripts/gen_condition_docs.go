// gen_condition_docs reads the PlatformAgent conditions table the operator
// keeps in k8s-operator/internal/controller/condition_docs.go and prints it as
// JSON for scripts/generate_docs.py, which renders the CRD reference page's
// conditions table from it.
//
// It reads the package's syntax with the standard library rather than
// importing it, like gen_crd_docs.go beside it: building the controller
// package would pull in the operator module and its toolchain, which the docs
// check does not install. So the table may only hold what syntax can resolve:
// string literals, string constants declared in the package, and, for
// Reasons, a package-level []string variable of those. Anything else is an
// error naming the position, never a guess.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type row struct {
	Type    string   `json:"type"`
	Written string   `json:"written"`
	Meaning string   `json:"meaning"`
	Reasons []string `json:"reasons"`
}

type pkg struct {
	fset   *token.FileSet
	consts map[string]ast.Expr
	vars   map[string]ast.Expr
}

func main() {
	dir := flag.String("dir", "k8s-operator/internal/controller", "the controller package directory")
	table := flag.String("var", "platformAgentConditionDocs", "the []conditionDoc variable to read")
	flag.Parse()
	rows, err := read(*dir, *table)
	if err != nil {
		fmt.Fprintln(os.Stderr, "gen_condition_docs:", err)
		os.Exit(1)
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(map[string][]row{"conditions": rows}); err != nil {
		fmt.Fprintln(os.Stderr, "gen_condition_docs:", err)
		os.Exit(1)
	}
}

func read(dir, table string) ([]row, error) {
	names, err := filepath.Glob(filepath.Join(dir, "*.go"))
	if err != nil {
		return nil, err
	}
	p := pkg{fset: token.NewFileSet(), consts: map[string]ast.Expr{}, vars: map[string]ast.Expr{}}
	for _, name := range names {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(p.fset, name, nil, 0)
		if err != nil {
			return nil, err
		}
		for _, decl := range f.Decls {
			gen, ok := decl.(*ast.GenDecl)
			if !ok || (gen.Tok != token.CONST && gen.Tok != token.VAR) {
				continue
			}
			for _, spec := range gen.Specs {
				vs := spec.(*ast.ValueSpec)
				for i, id := range vs.Names {
					if i >= len(vs.Values) {
						continue
					}
					if gen.Tok == token.CONST {
						p.consts[id.Name] = vs.Values[i]
					} else {
						p.vars[id.Name] = vs.Values[i]
					}
				}
			}
		}
	}
	lit, ok := p.vars[table].(*ast.CompositeLit)
	if !ok {
		return nil, fmt.Errorf("%s: no package-level composite literal named %s", dir, table)
	}
	rows := make([]row, 0, len(lit.Elts))
	for _, elt := range lit.Elts {
		entry, ok := elt.(*ast.CompositeLit)
		if !ok {
			return nil, p.errorf(elt, "an entry of %s is not a composite literal", table)
		}
		var r row
		for _, field := range entry.Elts {
			kv, ok := field.(*ast.KeyValueExpr)
			if !ok {
				return nil, p.errorf(field, "entries must name their fields")
			}
			key, _ := kv.Key.(*ast.Ident)
			if key == nil {
				return nil, p.errorf(kv.Key, "unreadable field name")
			}
			var err error
			switch key.Name {
			case "Type":
				r.Type, err = p.str(kv.Value, 0)
			case "Written":
				r.Written, err = p.str(kv.Value, 0)
			case "Meaning":
				r.Meaning, err = p.str(kv.Value, 0)
			case "Reasons":
				r.Reasons, err = p.strs(kv.Value)
			default:
				err = p.errorf(key, "unknown field %s", key.Name)
			}
			if err != nil {
				return nil, err
			}
		}
		rows = append(rows, r)
	}
	return rows, nil
}

// str resolves a string literal, a package string constant, or a constant
// defined as another constant.
func (p pkg) str(e ast.Expr, depth int) (string, error) {
	switch v := e.(type) {
	case *ast.BasicLit:
		if v.Kind != token.STRING {
			return "", p.errorf(v, "%s is not a string", v.Value)
		}
		return strconv.Unquote(v.Value)
	case *ast.Ident:
		c, ok := p.consts[v.Name]
		if !ok || depth > 8 {
			return "", p.errorf(v, "%s is not a string constant of the package", v.Name)
		}
		return p.str(c, depth+1)
	}
	return "", p.errorf(e, "a %T is not a string literal or constant; the docs generator reads syntax only", e)
}

// strs resolves a package-level []string variable of literals and constants.
func (p pkg) strs(e ast.Expr) ([]string, error) {
	id, ok := e.(*ast.Ident)
	if !ok {
		return nil, p.errorf(e, "Reasons must name a package-level []string variable")
	}
	lit, ok := p.vars[id.Name].(*ast.CompositeLit)
	if !ok {
		return nil, p.errorf(e, "%s is not a package-level composite literal", id.Name)
	}
	out := make([]string, 0, len(lit.Elts))
	for _, elt := range lit.Elts {
		s, err := p.str(elt, 0)
		if err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, nil
}

func (p pkg) errorf(n ast.Node, format string, args ...any) error {
	return fmt.Errorf("%s: %s", p.fset.Position(n.Pos()), fmt.Sprintf(format, args...))
}
