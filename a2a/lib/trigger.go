package lib

import (
	"fmt"
	"strings"
)

// The origins an unattended trigger may name (#4685): a schedule the operator
// ships, or one a person created.
const (
	TriggerOriginBuiltin = "builtin"
	TriggerOriginUser    = "user"
)

// TriggerUserStreamPrefix is the one stream path an origin-user schedule's
// report stream may take: user/<schedule-name>.
const TriggerUserStreamPrefix = "user/"

// ValidateTrigger is the shape check every holder of an unattended trigger
// shares: the capability grant that binds it and the task credential that
// carries it to the broker. name is <source> or <source>/<name>, each a
// single subject token; origin is builtin or user; stream is empty, a single
// subject token, or user/<token>.
//
// It checks shape only. Whether a stream belongs to its trigger, and whether
// a user schedule may name it, is the gateway's rule at mint and the broker's
// at write.
func ValidateTrigger(name, origin, stream string) error {
	source, rest, hasRest := strings.Cut(name, "/")
	if !ValidSubjectToken(source) || (hasRest && !ValidSubjectToken(rest)) {
		return fmt.Errorf("trigger name %q is not <source> or <source>/<name> of dot-free DNS-1123 labels", name)
	}
	if origin != TriggerOriginBuiltin && origin != TriggerOriginUser {
		return fmt.Errorf("trigger origin %q is not %q or %q", origin, TriggerOriginBuiltin, TriggerOriginUser)
	}
	if stream != "" {
		bare, _ := strings.CutPrefix(stream, TriggerUserStreamPrefix)
		if !ValidSubjectToken(bare) {
			return fmt.Errorf("trigger stream %q is not a dot-free DNS-1123 label or %s<label>", stream, TriggerUserStreamPrefix)
		}
	}
	return nil
}
