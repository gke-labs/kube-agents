package lib

import (
	"fmt"
	"strings"
)

// TriggerOriginBuiltin is the one origin an unattended trigger may name today
// (#4685): a schedule or collector the operator ships. A user's schedule runs
// at its creator's tier and carries no trigger, so the origin field stays on
// the wire for a later source but admits nothing else yet.
const TriggerOriginBuiltin = "builtin"

// triggerScheduleSource is the source whose triggers name a schedule, and the
// only one that may bind a report stream.
const triggerScheduleSource = "schedule"

// ValidateTrigger is the check every holder of an unattended trigger shares:
// the capability grant that binds it, the authority block that claims it, and
// the task credential that carries it to the broker. name is <source> or
// <source>/<name>, each a single subject token; origin is builtin; stream is
// empty or the schedule's own name, so a trigger can bind no report stream but
// its own: a stream is allowed only on schedule/<stream>.
func ValidateTrigger(name, origin, stream string) error {
	source, rest, hasRest := strings.Cut(name, "/")
	if !ValidSubjectToken(source) || (hasRest && !ValidSubjectToken(rest)) {
		return fmt.Errorf("trigger name %q is not <source> or <source>/<name> of dot-free DNS-1123 labels", name)
	}
	if origin != TriggerOriginBuiltin {
		return fmt.Errorf("trigger origin %q is not %q", origin, TriggerOriginBuiltin)
	}
	if stream != "" && (source != triggerScheduleSource || rest != stream) {
		return fmt.Errorf("trigger stream %q is not the trigger's own schedule name (%s/<stream>)", stream, triggerScheduleSource)
	}
	return nil
}
