package k8sapiproxy

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
)

// Rejection reasons for spec.scope.hold, named as the credential broker names its rules
// (scope_exclude.py), so one install's refusals read the same on both.
const (
	ReasonHeldCluster    RejectionReason = "scope.held-cluster"
	ReasonHoldUnreadable RejectionReason = "scope.hold-unreadable"
)

const (
	holdUndecidedNotice = ", so kube-proxy can't tell whether this cluster is on hold and reaches no cluster until it can."
	holdStopNotice      = " An operator paused agent access to it; report this and stop, do not retry or work around it."
)

var (
	errHoldUnreadable = errors.New("the install's hold file can't be read")
	errHoldUnparsed   = errors.New("the install's hold file can't be parsed")
)

// Holds is the proxy's view of spec.scope.hold, read from the hold.json the operator
// mounts from a ConfigMap of its own and re-read when the file changes, so a hold applies
// without restarting anything. A held cluster is refused exactly as an excluded one is
// (Exclusions): matching is exact on project, location and cluster, in-cluster is checked
// as hostClusterID, and a target named by its project number is refused while any cluster
// is held. All holds every cluster, in-cluster and kind-* targets included.
//
// Fail-closed: a configured file the proxy can't read or parse refuses every cluster. An
// empty path, a proxy started outside the operator, holds nothing.
type Holds struct {
	path          string
	hostClusterID string

	mu    sync.Mutex
	seen  string
	state holdState
	err   error
}

type holdState struct {
	all      bool
	reason   string
	refs     map[clusterRef]bool
	numbered map[[2]string]bool
}

// NewHolds reads the holds at path; an empty path holds nothing.
func NewHolds(path, hostClusterID string) *Holds {
	return &Holds{path: path, hostClusterID: hostClusterID}
}

func (h *Holds) load() (holdState, error) {
	if h == nil || h.path == "" {
		return holdState{}, nil
	}
	info, err := os.Stat(h.path)
	if err != nil {
		return holdState{}, errHoldUnreadable
	}
	identity := fmt.Sprintf("%d/%d", info.Size(), info.ModTime().UnixNano())
	if sys, ok := fileIdentity(info); ok {
		identity += "/" + sys
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if identity != h.seen {
		h.state, h.err = parseHolds(h.path)
		h.seen = identity
	}
	return h.state, h.err
}

func parseHolds(path string) (holdState, error) {
	data, err := os.ReadFile(path) // #nosec G304 -- the operator-set --hold-file (KUBE_PROXY_HOLD_FILE) path, not request data
	if err != nil {
		return holdState{}, errHoldUnreadable
	}
	var doc struct {
		All      *bool            `json:"all"`
		Clusters []map[string]any `json:"clusters"`
		Reason   *string          `json:"reason"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return holdState{}, errHoldUnparsed
	}
	refs, numbered, ok := parseClusterRefs(doc.Clusters)
	if !ok {
		return holdState{}, errHoldUnparsed
	}
	state := holdState{refs: refs, numbered: numbered}
	if doc.All != nil {
		state.all = *doc.All
	}
	if doc.Reason != nil {
		state.reason = strings.TrimSpace(*doc.Reason)
	}
	return state, nil
}

// check returns the refusal for a request to clusterID, or nil to let it through.
func (h *Holds) check(clusterID string) *PolicyError {
	state, err := h.load()
	if err != nil {
		return excludeRefusal(ReasonHoldUnreadable, err.Error()+holdUndecidedNotice)
	}
	if state.all {
		return excludeRefusal(ReasonHeldCluster, "every cluster is on hold (spec.scope.hold.all)"+state.reasonSuffix()+"."+holdStopNotice)
	}
	if len(state.refs) == 0 {
		return nil
	}
	if clusterID == InClusterID {
		if h.hostClusterID == "" {
			return excludeRefusal(ReasonHeldCluster,
				"this install holds clusters and kube-proxy can't name the cluster it runs in; address it by its gke_ ID.")
		}
		clusterID = h.hostClusterID
	}
	m := gkeClusterID.FindStringSubmatch(clusterID)
	if m == nil {
		// kind-{name}: a local harness, never a GKE cluster the hold can name.
		return nil
	}
	ref := clusterRef{m[1], m[2], m[3]}
	switch {
	case state.refs[ref], state.numbered[[2]string{ref.location, ref.cluster}]:
		return excludeRefusal(ReasonHeldCluster, fmt.Sprintf("cluster %s (%s, %s) is on hold (spec.scope.hold)%s.%s",
			ref.cluster, ref.project, ref.location, state.reasonSuffix(), holdStopNotice))
	case excludeProjectNumber.MatchString(ref.project):
		return excludeRefusal(ReasonHeldCluster, fmt.Sprintf(
			"project %s is named by its number while this install holds clusters by project ID; name the project by its ID.",
			ref.project))
	}
	return nil
}

func (s holdState) reasonSuffix() string {
	if s.reason == "" {
		return ""
	}
	return ": " + s.reason
}
