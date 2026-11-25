package gateway

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"sync"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// The resolved scope (#4119): the projects a folder, organization, Shared VPC
// host or Metrics Scope declaration reaches, which the scope publisher Job
// writes to one ConfigMap and the gateway reads to confirm a profile task's
// target. The publisher, the operator and this reader share the document's
// shape and the member hash (docs/designs/multi-project-scope.md, section 5).
const (
	// resolvedScopeVersion is the document version this reader knows; any
	// other version reads as no document.
	resolvedScopeVersion = 1
	// resolvedScopeDataKey is the ConfigMap key that holds the document.
	resolvedScopeDataKey = "resolved-scope.json"
	// resolvedScopeStateOK is the only document state that admits a target.
	resolvedScopeStateOK = "ok"
	// resolvedOutcomeOK is the only row outcome that admits a target.
	resolvedOutcomeOK = "ok"
	// resolvedScopeCacheTTL is how long one ConfigMap read answers routed
	// tasks, so a burst of tasks does not read the API server each time.
	resolvedScopeCacheTTL = 30 * time.Second
	// resolvedScopeReadTimeout bounds one ConfigMap read.
	resolvedScopeReadTimeout = 5 * time.Second
	// publisherInterval is how often the operator starts a publisher Job. It
	// and the two below are the operator's scopePublishInterval and
	// scopeListingBudgetPerBlock and the reconcile's LIST_BUDGET_SECONDS; the
	// bound they give must stay the operator's ScopeResolved bound.
	publisherInterval = 15 * time.Minute
	// publisherListBudget is the publisher's lookup budget per
	// publisherProjectsPerBudget projects of the cap, the reconcile's
	// LIST_BUDGET_SECONDS per 100 projects.
	publisherListBudget        = 300 * time.Second
	publisherProjectsPerBudget = 100
	// defaultScopeMaxProjects is the CRD's default for spec.scope.maxProjects.
	defaultScopeMaxProjects = 100
	// memberHashPrefix names the hash the member hash is.
	memberHashPrefix = "sha256:"
	// resolvedScopeClockSkew is how far ahead of this clock a stamp may be
	// and still count as fresh: the operator's scopeClockSkewAllowance. A stamp
	// further ahead would never go stale.
	resolvedScopeClockSkew = time.Minute
)

// resolvedScope is the publisher's document.
type resolvedScope struct {
	V                int           `json:"v"`
	ResolvedAt       string        `json:"resolvedAt"`
	MemberHash       string        `json:"memberHash"`
	State            string        `json:"state"`
	ExcludedProjects []string      `json:"excludedProjects"`
	Projects         []resolvedRow `json:"projects"`
	// Clusters is the publisher's listing of each member project's
	// clusters (#5621), kept raw: it is read only for the scopeClusters
	// page (resolvedClustersOf), so a malformed listing costs the page, never
	// the admissions the rows decide.
	Clusters json.RawMessage `json:"clusters,omitempty"`
}

// resolvedClusters is one member project's entry in the document's
// `clusters` map: when its listing was read, and its clusters as
// [location, name] pairs. A probe that fails keeps the previous entry, `at`
// included; a project with no entry is one no probe has listed.
type resolvedClusters struct {
	At       string     `json:"at"`
	Clusters [][]string `json:"clusters"`
}

// resolvedClustersOf is doc's `clusters` map, or false when the document
// has none to give: no document, one whose state admits nothing, no map (the
// publisher drops it first when the document is over its budget, and omits
// an empty one), or a value that isn't a map. Each entry is read on its own,
// as the publisher's `_clean_clusters` reads a previous document's: one whose
// `at` isn't the publisher's time format, or whose clusters aren't a list of
// [location, name] string pairs, is left out, so it reads as a project no
// probe has listed and the rest of the map still counts.
func resolvedClustersOf(doc *resolvedScope) (map[string]resolvedClusters, bool) {
	if doc == nil || doc.V != resolvedScopeVersion || doc.State != resolvedScopeStateOK || len(doc.Clusters) == 0 {
		return nil, false
	}
	var entries map[string]json.RawMessage
	if err := json.Unmarshal(doc.Clusters, &entries); err != nil || entries == nil {
		return nil, false
	}
	out := make(map[string]resolvedClusters, len(entries))
	for project, raw := range entries {
		if e, ok := resolvedClustersEntry(raw); ok {
			out[project] = e
		}
	}
	return out, true
}

// resolvedClustersEntry is one `clusters` entry, or false when it isn't
// whole: an object whose `at` is a string in the publisher's time format and
// whose `clusters` is a list of two-string lists.
func resolvedClustersEntry(raw json.RawMessage) (resolvedClusters, bool) {
	var e struct {
		At       json.RawMessage   `json:"at"`
		Clusters []json.RawMessage `json:"clusters"`
	}
	if json.Unmarshal(raw, &e) != nil || e.Clusters == nil {
		return resolvedClusters{}, false
	}
	at, ok := jsonString(e.At)
	if !ok {
		return resolvedClusters{}, false
	}
	if _, err := time.Parse(resolvedListingTimeFormat, at); err != nil {
		return resolvedClusters{}, false
	}
	out := resolvedClusters{At: at, Clusters: make([][]string, 0, len(e.Clusters))}
	for _, p := range e.Clusters {
		var pair []json.RawMessage
		if json.Unmarshal(p, &pair) != nil || len(pair) != 2 {
			return resolvedClusters{}, false
		}
		location, ok1 := jsonString(pair[0])
		name, ok2 := jsonString(pair[1])
		if !ok1 || !ok2 {
			return resolvedClusters{}, false
		}
		out.Clusters = append(out.Clusters, []string{location, name})
	}
	return out, true
}

// jsonString is raw as a JSON string, and false for anything else (null
// included, which json.Unmarshal into a string would leave as "").
func jsonString(raw json.RawMessage) (string, bool) {
	var v string
	if len(raw) == 0 || raw[0] != '"' || json.Unmarshal(raw, &v) != nil {
		return "", false
	}
	return v, true
}

// resolvedListingTimeFormat is the `at` stamp's format, the publisher's
// SNAPSHOT_TIME_FORMAT.
const resolvedListingTimeFormat = "2006-01-02T15:04:05Z"

// resolvedRow is one container- or selector-derived member project.
type resolvedRow struct {
	ID          string   `json:"id"`
	Via         []string `json:"via"`
	Outcome     string   `json:"outcome"`
	ConfirmedAt string   `json:"confirmedAt"`
}

// errUnresolved is checkTargetScope's refusal for a target only the resolved
// scope can confirm: a project the declaration does not list, under a
// declaration with a container or selector. It reads as the "can't confirm"
// refusal to a caller that does not look further, and profileRouteFor turns
// it into the resolved scope's own answer.
type errUnresolved struct{ errRefused }

func (e errUnresolved) Unwrap() error { return e.errRefused }

// declaresResolution reports whether decl names anything only a resolution
// can expand into projects.
func declaresResolution(decl *scopeDeclaration) bool {
	return len(decl.Folders)+len(decl.Organizations)+len(decl.SharedVpcHosts)+len(decl.MetricsScopes) > 0
}

// scopeMaxProjects is decl's resolved-set cap, or the CRD's default.
func scopeMaxProjects(decl *scopeDeclaration) int {
	if decl.MaxProjects == nil || *decl.MaxProjects <= 0 {
		return defaultScopeMaxProjects
	}
	return *decl.MaxProjects
}

// resolvedScopeBound is how old a document or a row may be before the
// gateway stops trusting it: two publisher cycles, each the interval plus the
// lookup budget for the cap.
func resolvedScopeBound(maxProjects int) time.Duration {
	budgets := (maxProjects + publisherProjectsPerBudget - 1) / publisherProjectsPerBudget
	return 2 * (publisherInterval + time.Duration(budgets)*publisherListBudget)
}

// memberHash is the hash of the declaration fields that decide membership,
// as canonical JSON: keys sorted, no whitespace, each list sorted with
// duplicates removed, maxProjects defaulted. exclude.clusters is left out:
// the gateway applies it from the declaration directly. The publisher
// computes the same bytes in Python.
func memberHash(decl *scopeDeclaration) string {
	canonical := map[string]any{
		"exclude":        map[string]any{"projects": sortedUnique(decl.Exclude.Projects)},
		"folders":        sortedUnique(decl.Folders),
		"maxProjects":    scopeMaxProjects(decl),
		"metricsScopes":  sortedUnique(decl.MetricsScopes),
		"organizations":  sortedUnique(decl.Organizations),
		"projects":       sortedUnique(decl.Projects),
		"sharedVpcHosts": sortedUnique(decl.SharedVpcHosts),
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(canonical); err != nil {
		return ""
	}
	sum := sha256.Sum256(bytes.TrimRight(buf.Bytes(), "\n"))
	return memberHashPrefix + hex.EncodeToString(sum[:])
}

func sortedUnique(values []string) []string {
	out := slices.Clone(values)
	slices.Sort(out)
	out = slices.Compact(out)
	if out == nil {
		return []string{}
	}
	return out
}

// checkResolvedScope confirms t against the resolved scope doc (nil when
// none could be read), for a declaration with a container or selector and a
// target project it does not list. It admits only a fresh `ok` row from a
// fresh, current document.
func checkResolvedScope(doc *resolvedScope, decl *scopeDeclaration, now time.Time, t clusterTarget) error {
	return newResolvedIndex(doc, decl).check(now, t)
}

// resolvedIndex is doc read once for decl: the bound, whether the member
// hash matches, and the rows and exclusions by project, so a caller checking
// many targets (the scopeClusters page) pays for the hash and the scan once
// rather than per target. check is checkResolvedScope's one statement.
type resolvedIndex struct {
	doc      *resolvedScope
	bound    time.Duration
	current  bool
	rows     map[string]resolvedRow
	excluded map[string]bool
}

// onResolvedIndex, when set (tests only), runs each time newResolvedIndex
// builds an index, so a test can count builds instead of timing them.
var onResolvedIndex func()

func newResolvedIndex(doc *resolvedScope, decl *scopeDeclaration) *resolvedIndex {
	if onResolvedIndex != nil {
		onResolvedIndex()
	}
	x := &resolvedIndex{doc: doc, bound: resolvedScopeBound(scopeMaxProjects(decl))}
	if doc == nil || doc.V != resolvedScopeVersion {
		return x
	}
	x.current = doc.MemberHash == memberHash(decl)
	x.rows = make(map[string]resolvedRow, len(doc.Projects))
	for _, r := range doc.Projects {
		if _, dup := x.rows[r.ID]; !dup { // the first row for a project decides, as a scan would
			x.rows[r.ID] = r
		}
	}
	x.excluded = make(map[string]bool, len(doc.ExcludedProjects))
	for _, p := range doc.ExcludedProjects {
		x.excluded[p] = true
	}
	return x
}

func (x *resolvedIndex) check(now time.Time, t clusterTarget) error {
	doc := x.doc
	if doc != nil && doc.V == resolvedScopeVersion && doc.State != resolvedScopeStateOK {
		// too-large or error: no refresh clears it until the scope or the cap changes.
		return refused(noticeTargetUnpublished, t, doc.State)
	}
	if doc == nil || doc.V != resolvedScopeVersion || !x.current {
		return refused(noticeTargetStale, t)
	}
	if !freshWithin(doc.ResolvedAt, now, x.bound) {
		return refused(noticeTargetStale, t)
	}
	if x.excluded[t.Project] {
		return refused(noticeTargetExcluded, t)
	}
	row, ok := x.rows[t.Project]
	if !ok {
		return refused(noticeTargetUnconfirmed, t)
	}
	bound := x.bound
	if row.Outcome != resolvedOutcomeOK {
		return refused(noticeTargetUnreadable, t, row.Outcome)
	}
	if !freshWithin(row.ConfirmedAt, now, bound) {
		return refused(noticeTargetUnconfirmed, t)
	}
	return nil
}

// freshWithin reports whether stamp (RFC 3339) is no older than bound at now
// and not ahead of now by more than resolvedScopeClockSkew. An unparsable
// stamp is not fresh.
func freshWithin(stamp string, now time.Time, bound time.Duration) bool {
	at, err := time.Parse(time.RFC3339, stamp)
	return err == nil && now.Sub(at) <= bound && !at.After(now.Add(resolvedScopeClockSkew))
}

// resolvedListingCache is a resolvedScopeSource that keeps the parsed
// `clusters` map of the document it last served, so the scopeClusters page
// doesn't re-parse up to the publisher's 900 KiB budget on every submission.
type resolvedListingCache interface {
	clustersOf(doc *resolvedScope) (map[string]resolvedClusters, bool)
}

// resolvedScopeSource reads the resolved scope document.
type resolvedScopeSource interface {
	load(ctx context.Context) (*resolvedScope, error)
}

// errNoResolvedScope is a ConfigMap with no document under its key: the
// operator's empty object before the first publisher run.
var errNoResolvedScope = errors.New("no resolved scope document")

// configMapResolvedScope reads the document from the publisher's ConfigMap
// with the gateway's own ServiceAccount, whose Role allows get on that one
// name, and keeps each answer for resolvedScopeCacheTTL.
type configMapResolvedScope struct {
	client    kubernetes.Interface
	namespace string
	name      string
	now       func() time.Time

	mu     sync.Mutex
	readAt time.Time
	doc    *resolvedScope
	err    error

	// The parsed `clusters` map of listingDoc, the document load served:
	// a fresh read is a new pointer, so the cache follows the document.
	listingDoc   *resolvedScope
	listing      map[string]resolvedClusters
	listingKnown bool
}

// clustersOf is resolvedClustersOf(doc), parsed once per document read.
func (s *configMapResolvedScope) clustersOf(doc *resolvedScope) (map[string]resolvedClusters, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if doc == nil || doc != s.listingDoc {
		listing, known := resolvedClustersOf(doc)
		if doc == nil {
			return listing, known
		}
		s.listingDoc, s.listing, s.listingKnown = doc, listing, known
	}
	return s.listing, s.listingKnown
}

func newConfigMapResolvedScope(client kubernetes.Interface, namespace, name string) *configMapResolvedScope {
	return &configMapResolvedScope{client: client, namespace: namespace, name: name, now: time.Now}
}

func (s *configMapResolvedScope) load(ctx context.Context) (*resolvedScope, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.readAt.IsZero() && s.now().Sub(s.readAt) < resolvedScopeCacheTTL {
		return s.doc, s.err
	}
	s.doc, s.err = s.read(ctx)
	s.readAt = s.now()
	return s.doc, s.err
}

func (s *configMapResolvedScope) read(ctx context.Context) (*resolvedScope, error) {
	cm, err := s.client.CoreV1().ConfigMaps(s.namespace).Get(ctx, s.name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	raw, ok := cm.Data[resolvedScopeDataKey]
	if !ok || raw == "" {
		return nil, errNoResolvedScope
	}
	var doc resolvedScope
	if err := json.Unmarshal([]byte(raw), &doc); err != nil {
		return nil, fmt.Errorf("resolved scope %s/%s: %w", s.namespace, s.name, err)
	}
	return &doc, nil
}

// SetResolvedScope arms the resolved-scope check with the ConfigMap the
// publisher writes (A2A_RESOLVED_SCOPE_CONFIGMAP). Without it, a target only
// a resolution can confirm is refused as unconfirmed.
func (g *Gateway) SetResolvedScope(client kubernetes.Interface, namespace, name string) {
	g.resolvedScope = newConfigMapResolvedScope(client, namespace, name)
}

// checkScope checks t against decl and, for a target only the resolved scope
// can confirm, against the resolved scope.
func (g *Gateway) checkScope(decl *scopeDeclaration, t clusterTarget) error {
	err := checkTargetScope(decl, g.cfg.ManagementProject, t)
	var unresolved errUnresolved
	if !errors.As(err, &unresolved) || g.resolvedScope == nil {
		return err
	}
	doc, lerr := g.loadResolvedScope()
	if lerr != nil {
		g.log.Warn("resolved scope unreadable; refusing the target", "target", t.String(), "err", lerr)
	}
	return checkResolvedScope(doc, decl, time.Now(), t)
}

// loadResolvedScope is the resolved scope document, bounded by
// resolvedScopeReadTimeout: nil with the error when it can't be read, and nil
// when the gateway reads none.
func (g *Gateway) loadResolvedScope() (*resolvedScope, error) {
	if g.resolvedScope == nil {
		return nil, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), resolvedScopeReadTimeout)
	defer cancel()
	doc, err := g.resolvedScope.load(ctx)
	if err != nil {
		return nil, err
	}
	return doc, nil
}

// scopeVerdict is checkScope against a document already read and indexed
// (newResolvedIndex; armed false when the gateway reads none): the same two
// steps, so a caller checking many targets reads and indexes the document
// once and every verdict comes from the same one.
func (g *Gateway) scopeVerdict(decl *scopeDeclaration, idx *resolvedIndex, armed bool, now time.Time) func(clusterTarget) error {
	return func(t clusterTarget) error {
		err := checkTargetScope(decl, g.cfg.ManagementProject, t)
		var unresolved errUnresolved
		if !errors.As(err, &unresolved) || !armed {
			return err
		}
		return idx.check(now, t)
	}
}

// resolvedClusters is the document's parsed `clusters` map, from the
// source's cache when it keeps one.
func (g *Gateway) resolvedClusters(doc *resolvedScope) (map[string]resolvedClusters, bool) {
	if c, ok := g.resolvedScope.(resolvedListingCache); ok {
		return c.clustersOf(doc)
	}
	return resolvedClustersOf(doc)
}
