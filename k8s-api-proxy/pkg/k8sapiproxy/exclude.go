package k8sapiproxy

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"regexp"
	"sync"
	"syscall"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Rejection reasons for spec.scope.exclude.clusters, named as the credential broker
// names its rules (scope_exclude.py), so one install's refusals read the same on both.
const (
	ReasonExcludedCluster   RejectionReason = "scope.excluded-cluster"
	ReasonExcludeUnreadable RejectionReason = "scope.exclude-unreadable"
)

var (
	excludeNamePart      = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}\z`)
	excludeProjectNumber = regexp.MustCompile(`^[0-9]+\z`)
	// A GKE context name, as validClusterID admits it: gke_{project}_{location}_{cluster},
	// where a legacy domain-scoped project is {domain}:{name}.
	gkeClusterID = regexp.MustCompile(`^gke_((?:[a-z0-9][a-z0-9.-]*[a-z0-9]:)?[a-z0-9-]+)_([a-z0-9-]+)_([a-z0-9-]+)\z`)
)

type clusterRef struct{ project, location, cluster string }

// Exclusions is the proxy's view of spec.scope.exclude.clusters, read from the scope.json
// the operator mounts and re-read when the file changes. It refuses every request to an
// excluded cluster's API, as the credential broker does for the routes it serves.
//
// Matching is exact on project, location and cluster. A project number can't be matched to
// a project ID without a lookup, so while any cluster is excluded a target named by its
// project number is refused, and an entry spelled by its project number refuses its
// location and cluster name in any project.
//
// Fail-closed: a configured file the proxy can't read or parse refuses every cluster. An
// empty path, a proxy started outside the operator, excludes nothing.
type Exclusions struct {
	path string
	// hostClusterID is the gke_ ID of the cluster the proxy runs in, which in-cluster
	// addresses. Empty when the operator couldn't name it; then in-cluster is refused
	// while any cluster is excluded.
	hostClusterID string

	mu       sync.Mutex
	seen     string
	refs     map[clusterRef]bool
	numbered map[[2]string]bool
	err      error
}

// NewExclusions reads the exclusions at path; an empty path excludes nothing.
func NewExclusions(path, hostClusterID string) *Exclusions {
	return &Exclusions{path: path, hostClusterID: hostClusterID}
}

func (e *Exclusions) load() (map[clusterRef]bool, map[[2]string]bool, error) {
	if e == nil || e.path == "" {
		return nil, nil, nil
	}
	info, err := os.Stat(e.path)
	if err != nil {
		return nil, nil, fmt.Errorf("the install's scope file can't be read")
	}
	identity := fmt.Sprintf("%d/%d", info.Size(), info.ModTime().UnixNano())
	if sys, ok := fileIdentity(info); ok {
		identity += "/" + sys
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if identity != e.seen {
		e.refs, e.numbered, e.err = parseExclusions(e.path)
		e.seen = identity
	}
	return e.refs, e.numbered, e.err
}

func parseExclusions(path string) (map[clusterRef]bool, map[[2]string]bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, fmt.Errorf("the install's scope file can't be read")
	}
	var doc struct {
		Exclude *struct {
			Clusters []map[string]any `json:"clusters"`
		} `json:"exclude"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, nil, fmt.Errorf("the install's scope file can't be parsed")
	}
	refs, numbered := map[clusterRef]bool{}, map[[2]string]bool{}
	if doc.Exclude == nil {
		return refs, numbered, nil
	}
	for _, entry := range doc.Exclude.Clusters {
		parts := [3]string{}
		for i, key := range []string{"projectId", "location", "clusterName"} {
			value, ok := entry[key].(string)
			if !ok || !excludeNamePart.MatchString(value) {
				return nil, nil, fmt.Errorf("the install's scope file can't be parsed")
			}
			parts[i] = value
		}
		refs[clusterRef{parts[0], parts[1], parts[2]}] = true
		if excludeProjectNumber.MatchString(parts[0]) {
			numbered[[2]string{parts[1], parts[2]}] = true
		}
	}
	return refs, numbered, nil
}

// check returns the refusal for a request to clusterID, or nil to let it through.
func (e *Exclusions) check(clusterID string) *PolicyError {
	refs, numbered, err := e.load()
	if err != nil {
		return excludeRefusal(ReasonExcludeUnreadable,
			err.Error()+", so kube-proxy can't tell whether this cluster is excluded and reaches no cluster until it can.")
	}
	if len(refs) == 0 {
		return nil
	}
	if clusterID == InClusterID {
		if e.hostClusterID == "" {
			return excludeRefusal(ReasonExcludedCluster,
				"this install excludes clusters and kube-proxy can't name the cluster it runs in; address it by its gke_ ID.")
		}
		clusterID = e.hostClusterID
	}
	m := gkeClusterID.FindStringSubmatch(clusterID)
	if m == nil {
		// kind-{name}: a local harness, never a GKE cluster the scope can name.
		return nil
	}
	ref := clusterRef{m[1], m[2], m[3]}
	switch {
	case refs[ref], numbered[[2]string{ref.location, ref.cluster}]:
		return excludeRefusal(ReasonExcludedCluster, fmt.Sprintf(
			"cluster %s (%s, %s) is excluded by this install's spec.scope.exclude.clusters; the agents don't reach its Kubernetes API.",
			ref.cluster, ref.project, ref.location))
	case excludeProjectNumber.MatchString(ref.project):
		return excludeRefusal(ReasonExcludedCluster, fmt.Sprintf(
			"project %s is named by its number while this install excludes clusters by project ID; name the project by its ID.",
			ref.project))
	}
	return nil
}

func excludeRefusal(reason RejectionReason, message string) *PolicyError {
	return &PolicyError{
		Code:            http.StatusForbidden,
		Reason:          metav1.StatusReasonForbidden,
		RejectionReason: reason,
		Message:         policyPrefix + "this cluster: " + message + boundaryNotice + "\npolicy rule: " + string(reason),
	}
}

// fileIdentity is the file's device and inode where the platform has them: a ConfigMap
// update swaps the file behind its symlink, which the size and mtime alone can miss.
func fileIdentity(info os.FileInfo) (string, bool) {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return "", false
	}
	return fmt.Sprintf("%d/%d", st.Dev, st.Ino), true
}
