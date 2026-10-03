package capability

import "os"

// RequiredEnvVar is the mixed-version switch, and it is one name read by four
// programs: the gateway that mints, the two executors that check, and the
// operator that renders it onto both halves of a rendered install
// (a2aCapabilityRequiredEnvVar in k8s-operator/internal/controller/
// platformagent_a2a_manifests.go, which is a separate Go module and so cannot
// share this constant — the operator's manifest tests pin the spelling).
const RequiredEnvVar = "A2A_CAPABILITY_REQUIRED"

// OptionalFromEnv resolves that switch into the `CapabilityOptional` field
// every executor and the gateway carry.
//
// The comparison is against the exact string "false" and nothing else, which
// is the whole security property: unset is required, and so is "False",
// "FALSE", "0", "no", " false " and every typo anyone will ever make. The
// tempting spelling is `!= "true"`, and it is the bug — it turns an unset
// variable, which is what a default install has, into a relaxed one. That is
// fail-open on the default route, arrived at by a one-token edit, which is why
// this lives in one function with a table test over it
// (TestOnlyTheExactStringFalseRelaxesTheCapabilityRequirement) rather than
// being written out at each of the three call sites.
//
// What it relaxes is narrow and it is not enforcement: on the gateway a mint
// failure sends `grants: null` instead of refusing the turn, and on an
// executor a submission that carries no capability at all runs instead of
// being refused. A capability that IS present is always checked, at every
// setting.
func OptionalFromEnv() bool {
	return os.Getenv(RequiredEnvVar) == "false"
}
