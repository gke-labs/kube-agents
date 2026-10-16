package authcallout

import (
	"fmt"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Reserved addressee names: the addressees a narrowed pod may not be named
// after.
//
// A narrowed user is named for its pod, and sessionGrants keys every task
// subject it hands out on that name: the events it may publish, the consumers
// it may create over `.in`, and the capability verify and reply subjects. The
// name is the session's addressee. A pod named after another addressee would
// therefore be handed that addressee's subjects: it could read the prompts
// sent to it, publish its events, and ask the verifier as it. For the bridge's
// `platform` that is every turn a user types on a stock install.
//
// Session addressees are pod names the gateway mints, so they need no entry
// here: the pod that holds the name is the addressee. The ones that do are
// addressees with a fixed name that some other principal executes. The
// operator renders them into the callout's environment as a comma-separated
// list, from the same constant the bridge's grants are built from. NewService
// folds them into the one reserved set reservedAs checks (see reserved.go).

const (
	// reservedAddresseeSeparator splits the operator-rendered list.
	reservedAddresseeSeparator = ","
)

// ParseReservedAddressees reads the operator-rendered list of addressee names.
// It fails closed: an empty list, an empty element, or a name that is not a
// single dot-free DNS-1123 label is an error, never a smaller set. A smaller
// set would admit a narrowed pod named after the dropped addressee.
//
// The label check is also what makes an exact comparison the right one. Pod
// names are DNS-1123 and so lowercase; a reserved name that is lowercase
// DNS-1123 too can be compared byte for byte, and a rendered name in any other
// case is refused here rather than silently never matching a pod.
func ParseReservedAddressees(raw string) ([]string, error) {
	if strings.TrimSpace(raw) == "" {
		return nil, fmt.Errorf("the reserved addressee list is empty; a narrowed pod could take any addressee's name and task subjects")
	}
	var names []string
	for _, field := range strings.Split(raw, reservedAddresseeSeparator) {
		name := strings.TrimSpace(field)
		if name == "" {
			return nil, fmt.Errorf("the reserved addressee list %q has an empty element", raw)
		}
		if !lib.ValidSubjectToken(name) {
			return nil, fmt.Errorf("reserved addressee %q is not a dot-free DNS-1123 label, so no pod could be refused for it", name)
		}
		names = append(names, name)
	}
	return names, nil
}
