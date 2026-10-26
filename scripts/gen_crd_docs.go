// Command gen_crd_docs reads the operator's API types with go/ast and prints
// the fields of the PlatformAgent, AgentProfile and AgentPlugin resources as
// JSON. scripts/generate_docs.py renders that model into the field reference
// pages; this program renders no Markdown.
//
// Standard library only, and no module: generate_docs.py runs it as
//
//	GOTOOLCHAIN=local go run scripts/gen_crd_docs.go -dir k8s-operator/api/v1alpha1
//
// from the repository root, so the docs-check workflow needs no Go setup step
// and no module download.
//
// The walk follows controller-gen's reading of a struct: json tags name the
// fields (",inline" embeds flatten into the parent, "-" drops the field),
// pointers are transparent, slices and maps carry their element, and a local
// named type resolves to what it is declared as. A type from another package
// (metav1, corev1, networkingv1, resource.Quantity) is a leaf: the row records
// its Go name and the walk does not expand it. A field is required under
// controller-gen's rule: +required (or the kubebuilder spelling) wins, then
// +optional, then a field with neither is required unless its tag says
// omitempty. Each field's description is the first paragraph of its doc
// comment with the marker lines and issue citations removed.
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
	"reflect"
	"regexp"
	"sort"
	"strings"
)

const (
	defaultAPIDir = "k8s-operator/api/v1alpha1"
	defaultKinds  = "PlatformAgent,AgentProfile,AgentPlugin"
	kindSeparator = ","

	testFileSuffix      = "_test.go"
	generatedFilePrefix = "zz_generated."
	goFileSuffix        = ".go"

	// The two trees a page documents, by the json name each kind gives them.
	specFieldJSON   = "spec"
	statusFieldJSON = "status"

	jsonTagKey       = "json"
	jsonTagInline    = "inline"
	jsonTagOmitEmpty = "omitempty"
	jsonTagSkip      = "-"

	markerPrefix           = "+"
	requiredMarker         = "+required"
	optionalMarker         = "+optional"
	requiredMarkerKB       = "+kubebuilder:validation:Required"
	optionalMarkerKB       = "+kubebuilder:validation:Optional"
	validationMarkerPrefix = "+kubebuilder:validation:"
	defaultMarkerPrefix    = "+kubebuilder:default"
	// Validation markers that shape a value rather than constrain it: CEL
	// rules carry a message for the API server, not a bound a table can show.
	xValidationMarker = "XValidation"

	// Path suffixes for the element of a list and the value of a map, so a
	// nested field's row reads `tools[].name` and `labels{}.key`.
	listItemSuffix = "[]"
	mapValueSuffix = "{}"
	pathSeparator  = "."

	// Rendered type names, OpenAPI's for the scalars so the table reads like
	// the CRD schema rather than like Go.
	typeObject  = "object"
	typeString  = "string"
	typeInteger = "integer"
	typeNumber  = "number"
	typeBoolean = "boolean"
	listPrefix  = "[]"
	mapPrefix   = "map[string]"
	byteIdent   = "byte"

	kindScalar   = "scalar"
	kindObject   = "object"
	kindList     = "list"
	kindMap      = "map"
	kindExternal = "external"

	// A first paragraph that introduces the example below it ends in a colon;
	// on its own in a table cell it ends in a full stop instead.
	colonEnding  = ":"
	periodEnding = "."

	jsonIndent = "  "
)

var (
	// The Go scalar types the API files use, by the OpenAPI type each maps to.
	scalarTypes = map[string]string{
		"string":  typeString,
		"bool":    typeBoolean,
		"int":     typeInteger,
		"int8":    typeInteger,
		"int16":   typeInteger,
		"int32":   typeInteger,
		"int64":   typeInteger,
		"uint":    typeInteger,
		"uint8":   typeInteger,
		"uint16":  typeInteger,
		"uint32":  typeInteger,
		"uint64":  typeInteger,
		"float32": typeNumber,
		"float64": typeNumber,
	}
	// A parenthetical that cites an issue or pull request, and a bare
	// citation: `(#3660, "Tiers and identity")`, `(see gke-labs#3029)`, `#3029`.
	// They point at threads a site reader cannot follow.
	citationParenRE = regexp.MustCompile(`\s*\([^()]*?(?:gke-labs)?#\d+[^()]*\)`)
	citationBareRE  = regexp.MustCompile(`\s*(?:gke-labs)?#\d+\b`)
	spaceRunRE      = regexp.MustCompile(`\s+`)
	spaceBeforeRE   = regexp.MustCompile(`\s+([,.;:])(\s|$)`)
)

// Field is one row of a tree: a dotted path from the tree's root, its type,
// and what the markers on it say.
type Field struct {
	Path        string       `json:"path"`
	Name        string       `json:"name"`
	Type        string       `json:"type"`
	Kind        string       `json:"kind"`
	External    bool         `json:"external"`
	Required    bool         `json:"required"`
	Default     string       `json:"default"`
	Constraints []Constraint `json:"constraints"`
	Description string       `json:"description"`
}

// Constraint is one validation marker: its name after the
// `+kubebuilder:validation:` prefix (`Enum`, `items:Pattern`) and its value.
type Constraint struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// Kind is one resource with its two trees.
type Kind struct {
	Kind   string  `json:"kind"`
	Spec   []Field `json:"spec"`
	Status []Field `json:"status"`
}

// Model is what the program prints.
type Model struct {
	Kinds []Kind `json:"kinds"`
}

// typeDecl is one named type of the package with the doc comment that
// controller-gen reads markers from.
type typeDecl struct {
	name string
	expr ast.Expr
	doc  []string
}

// typeRef is a field type as the walk reads it: what to print, how to
// descend, and which local struct (if any) holds the children.
type typeRef struct {
	rendered string
	kind     string
	local    string
	external bool
}

// pkg is the parsed API package.
type pkg struct {
	types map[string]*typeDecl
}

func main() {
	dir := flag.String("dir", defaultAPIDir, "directory holding the API package")
	kinds := flag.String("kinds", defaultKinds, "comma-separated kinds to document")
	flag.Parse()

	p, err := parsePackage(*dir)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	model := Model{}
	for _, kind := range strings.Split(*kinds, kindSeparator) {
		kind = strings.TrimSpace(kind)
		if kind == "" {
			continue
		}
		k, err := p.kind(kind)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		model.Kinds = append(model.Kinds, k)
	}
	out, err := json.MarshalIndent(model, "", jsonIndent)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Println(string(out))
}

// parsePackage reads every hand-written, non-test Go file in dir and indexes
// its type declarations.
func parsePackage(dir string) (*pkg, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", dir, err)
	}
	fset := token.NewFileSet()
	p := &pkg{types: map[string]*typeDecl{}}
	var names []string
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, goFileSuffix) ||
			strings.HasSuffix(name, testFileSuffix) || strings.HasPrefix(name, generatedFilePrefix) {
			continue
		}
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		file, err := parser.ParseFile(fset, filepath.Join(dir, name), nil, parser.ParseComments)
		if err != nil {
			return nil, fmt.Errorf("parsing %s: %w", name, err)
		}
		for _, decl := range file.Decls {
			gen, ok := decl.(*ast.GenDecl)
			if !ok || gen.Tok != token.TYPE {
				continue
			}
			for _, spec := range gen.Specs {
				ts, ok := spec.(*ast.TypeSpec)
				if !ok {
					continue
				}
				doc := ts.Doc
				if doc == nil {
					doc = gen.Doc
				}
				p.types[ts.Name.Name] = &typeDecl{name: ts.Name.Name, expr: ts.Type, doc: commentLines(doc)}
			}
		}
	}
	if len(p.types) == 0 {
		return nil, fmt.Errorf("%s: no type declarations found", dir)
	}
	return p, nil
}

// kind builds the two trees of one resource from its root struct.
func (p *pkg) kind(name string) (Kind, error) {
	root, ok := p.structType(name)
	if !ok {
		return Kind{}, fmt.Errorf("kind %s: no struct of that name in the package", name)
	}
	k := Kind{Kind: name, Spec: []Field{}, Status: []Field{}}
	for _, field := range root.Fields.List {
		jsonName, _, _, skip := jsonTag(field)
		if skip {
			continue
		}
		var target *[]Field
		switch jsonName {
		case specFieldJSON:
			target = &k.Spec
		case statusFieldJSON:
			target = &k.Status
		default:
			continue
		}
		ref, err := p.resolve(field.Type)
		if err != nil {
			return Kind{}, fmt.Errorf("kind %s, %s: %w", name, jsonName, err)
		}
		if ref.kind != kindObject {
			return Kind{}, fmt.Errorf("kind %s: %s is %s, not a struct of this package", name, jsonName, ref.rendered)
		}
		fields, err := p.walk(ref.local, "")
		if err != nil {
			return Kind{}, fmt.Errorf("kind %s, %s: %w", name, jsonName, err)
		}
		*target = fields
	}
	return k, nil
}

// structType returns the struct declared under name, when there is one.
func (p *pkg) structType(name string) (*ast.StructType, bool) {
	decl, ok := p.types[name]
	if !ok {
		return nil, false
	}
	st, ok := decl.expr.(*ast.StructType)
	return st, ok
}

// walk lists the fields of the struct named typeName under prefix, in source
// order, descending into the structs of this package.
func (p *pkg) walk(typeName, prefix string) ([]Field, error) {
	st, ok := p.structType(typeName)
	if !ok {
		return nil, fmt.Errorf("%s is not a struct of the package", typeName)
	}
	fields := []Field{}
	for _, field := range st.Fields.List {
		jsonName, omitEmpty, inline, skip := jsonTag(field)
		if skip {
			continue
		}
		ref, err := p.resolve(field.Type)
		if err != nil {
			return nil, fmt.Errorf("%s.%s: %w", typeName, fieldName(field), err)
		}
		if inline || (jsonName == "" && len(field.Names) == 0) {
			// An embedded struct flattens into its parent. One from another
			// package cannot be expanded here, and a row that hid its fields
			// would make the page disagree with the schema.
			if ref.kind != kindObject {
				return nil, fmt.Errorf("%s embeds %s inline, which this generator cannot expand", typeName, ref.rendered)
			}
			nested, err := p.walk(ref.local, prefix)
			if err != nil {
				return nil, err
			}
			fields = append(fields, nested...)
			continue
		}
		if jsonName == "" {
			jsonName = fieldName(field)
		}
		path := jsonName
		if prefix != "" {
			path = prefix + pathSeparator + jsonName
		}
		doc := commentLines(field.Doc)
		markers := markerLines(doc)
		// A local named type carries its own markers (an enum on the type
		// rather than on each field); the field's come after and win.
		if typeMarkers := p.typeMarkers(field.Type); len(typeMarkers) > 0 {
			markers = append(typeMarkers, markers...)
		}
		row := Field{
			Path:        path,
			Name:        jsonName,
			Type:        ref.rendered,
			Kind:        ref.kind,
			External:    ref.external,
			Required:    required(markers, omitEmpty),
			Default:     defaultValue(markers),
			Constraints: constraints(markers),
			Description: description(doc),
		}
		fields = append(fields, row)
		if ref.local == "" {
			continue
		}
		childPrefix := path
		switch ref.kind {
		case kindList:
			childPrefix = path + listItemSuffix
		case kindMap:
			childPrefix = path + mapValueSuffix
		}
		nested, err := p.walk(ref.local, childPrefix)
		if err != nil {
			return nil, err
		}
		fields = append(fields, nested...)
	}
	return fields, nil
}

// resolve reads a field's type expression into a typeRef.
func (p *pkg) resolve(expr ast.Expr) (typeRef, error) {
	switch t := expr.(type) {
	case *ast.StarExpr:
		return p.resolve(t.X)
	case *ast.ParenExpr:
		return p.resolve(t.X)
	case *ast.SelectorExpr:
		pkgIdent, ok := t.X.(*ast.Ident)
		if !ok {
			return typeRef{}, fmt.Errorf("unsupported selector type")
		}
		return typeRef{rendered: pkgIdent.Name + pathSeparator + t.Sel.Name, kind: kindExternal, external: true}, nil
	case *ast.Ident:
		if scalar, ok := scalarTypes[t.Name]; ok {
			return typeRef{rendered: scalar, kind: kindScalar}, nil
		}
		decl, ok := p.types[t.Name]
		if !ok {
			return typeRef{}, fmt.Errorf("type %s is neither a Go scalar nor declared in the package", t.Name)
		}
		if _, isStruct := decl.expr.(*ast.StructType); isStruct {
			return typeRef{rendered: typeObject, kind: kindObject, local: t.Name}, nil
		}
		return p.resolve(decl.expr)
	case *ast.ArrayType:
		if ident, ok := t.Elt.(*ast.Ident); ok && ident.Name == byteIdent {
			return typeRef{rendered: typeString, kind: kindScalar}, nil
		}
		elem, err := p.resolve(t.Elt)
		if err != nil {
			return typeRef{}, err
		}
		if elem.kind == kindList || elem.kind == kindMap {
			return typeRef{}, fmt.Errorf("nested collection type %s%s is not supported", listPrefix, elem.rendered)
		}
		return typeRef{rendered: listPrefix + elem.rendered, kind: kindList, local: elem.local, external: elem.external}, nil
	case *ast.MapType:
		key, err := p.resolve(t.Key)
		if err != nil {
			return typeRef{}, err
		}
		if key.rendered != typeString {
			return typeRef{}, fmt.Errorf("map key type %s is not a string", key.rendered)
		}
		value, err := p.resolve(t.Value)
		if err != nil {
			return typeRef{}, err
		}
		if value.kind == kindList || value.kind == kindMap {
			return typeRef{}, fmt.Errorf("nested collection type %s%s is not supported", mapPrefix, value.rendered)
		}
		return typeRef{rendered: mapPrefix + value.rendered, kind: kindMap, local: value.local, external: value.external}, nil
	default:
		return typeRef{}, fmt.Errorf("unsupported type expression %T", expr)
	}
}

// typeMarkers returns the markers declared on a local named type the field
// uses directly (through a pointer or not). A struct's markers describe the
// object, not the field, so only non-struct types contribute.
func (p *pkg) typeMarkers(expr ast.Expr) []string {
	for {
		star, ok := expr.(*ast.StarExpr)
		if !ok {
			break
		}
		expr = star.X
	}
	ident, ok := expr.(*ast.Ident)
	if !ok {
		return nil
	}
	decl, ok := p.types[ident.Name]
	if !ok {
		return nil
	}
	if _, isStruct := decl.expr.(*ast.StructType); isStruct {
		return nil
	}
	return markerLines(decl.doc)
}

// jsonTag reads a field's json tag: its name, and whether it says omitempty,
// inline, or "-".
func jsonTag(field *ast.Field) (name string, omitEmpty, inline, skip bool) {
	if field.Tag == nil {
		return "", false, false, false
	}
	raw := strings.Trim(field.Tag.Value, "`")
	tag, ok := reflect.StructTag(raw).Lookup(jsonTagKey)
	if !ok {
		return "", false, false, false
	}
	parts := strings.Split(tag, ",")
	name = parts[0]
	if name == jsonTagSkip {
		return "", false, false, true
	}
	for _, opt := range parts[1:] {
		switch opt {
		case jsonTagOmitEmpty:
			omitEmpty = true
		case jsonTagInline:
			inline = true
		}
	}
	return name, omitEmpty, inline, false
}

// fieldName is the Go name of a field, or the embedded type's name.
func fieldName(field *ast.Field) string {
	if len(field.Names) > 0 {
		return field.Names[0].Name
	}
	switch t := field.Type.(type) {
	case *ast.Ident:
		return t.Name
	case *ast.StarExpr:
		if ident, ok := t.X.(*ast.Ident); ok {
			return ident.Name
		}
	case *ast.SelectorExpr:
		return t.Sel.Name
	}
	return ""
}

// commentLines returns a comment group's lines without the comment syntax.
func commentLines(group *ast.CommentGroup) []string {
	if group == nil {
		return nil
	}
	var lines []string
	for _, c := range group.List {
		text := c.Text
		switch {
		case strings.HasPrefix(text, "//"):
			lines = append(lines, strings.TrimPrefix(strings.TrimPrefix(text, "//"), " "))
		case strings.HasPrefix(text, "/*"):
			text = strings.TrimSuffix(strings.TrimPrefix(text, "/*"), "*/")
			for _, line := range strings.Split(text, "\n") {
				lines = append(lines, strings.TrimSpace(line))
			}
		}
	}
	return lines
}

// markerLines picks the `+marker` lines out of a doc comment.
func markerLines(doc []string) []string {
	var markers []string
	for _, line := range doc {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, markerPrefix) {
			markers = append(markers, trimmed)
		}
	}
	return markers
}

// required applies controller-gen's rule to a field's markers and tag.
func required(markers []string, omitEmpty bool) bool {
	for _, m := range markers {
		switch m {
		case optionalMarkerKB, optionalMarker:
			return false
		case requiredMarkerKB, requiredMarker:
			return true
		}
	}
	return !omitEmpty
}

// defaultValue returns the `+kubebuilder:default` value, unquoted when it is
// a JSON string, or "" when the field has none.
func defaultValue(markers []string) string {
	for _, m := range markers {
		if !strings.HasPrefix(m, defaultMarkerPrefix) {
			continue
		}
		rest := strings.TrimPrefix(m, defaultMarkerPrefix)
		rest = strings.TrimLeft(rest, ":=")
		rest = strings.TrimSpace(rest)
		var unquoted string
		if json.Unmarshal([]byte(rest), &unquoted) == nil {
			return unquoted
		}
		return rest
	}
	return ""
}

// constraints returns the validation markers that bound a value, in marker
// order, with Required, Optional and XValidation left out.
func constraints(markers []string) []Constraint {
	out := []Constraint{}
	for _, m := range markers {
		if !strings.HasPrefix(m, validationMarkerPrefix) {
			continue
		}
		body := strings.TrimPrefix(m, validationMarkerPrefix)
		name, value, hasValue := strings.Cut(body, "=")
		if !hasValue {
			continue
		}
		if strings.HasPrefix(name, xValidationMarker) {
			continue
		}
		value = strings.TrimSpace(value)
		value = strings.Trim(value, "`")
		out = append(out, Constraint{Name: name, Value: value})
	}
	return out
}

// description is the first paragraph of a doc comment, markers and issue
// citations removed, on one line.
func description(doc []string) string {
	var paragraph []string
	for _, line := range doc {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, markerPrefix) {
			continue
		}
		if trimmed == "" {
			if len(paragraph) > 0 {
				break
			}
			continue
		}
		paragraph = append(paragraph, trimmed)
	}
	text := strings.Join(paragraph, " ")
	text = citationParenRE.ReplaceAllString(text, "")
	text = citationBareRE.ReplaceAllString(text, "")
	text = spaceRunRE.ReplaceAllString(text, " ")
	text = spaceBeforeRE.ReplaceAllString(text, "$1$2")
	text = strings.TrimSpace(text)
	if strings.HasSuffix(text, colonEnding) {
		text = strings.TrimSuffix(text, colonEnding) + periodEnding
	}
	return text
}
